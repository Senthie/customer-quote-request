#!/usr/bin/env python3
"""product_groups.py — 按「报价结构」把一封询价拆成多条产品记录。

业务口径（森西 2026-10-29 拍板）：

    一个报价文件里可以包含很多个产品，多个产品开很多单不方便 → ERP 导出用数组。
    **按报价结构分组**：装订 / 尺寸 / 用纸 / 印色 / 表面工艺 / 包装 等一切影响
    成本的键都相同的几款 → 合成一条记录，用 `TZS` 记款数；
    任何一个键不同 → 拆成两条记录。

本模块只做两件事，**不碰 ERP 字段映射**（那是 `main.map_fields` 的事）：

    build()            CustomerData → [Group]      （先分组，签名见 assets/structure_signature.yaml）
    to_customer_data() Group → CustomerData        （浇成一份独立数据，交给 map_fields）

为什么不和 map_fields 揉在一起：分组是「客户文件长什么样」的事，映射是
「客户口径 → ERP 字段」的事。分开后，新增一个多产品布局策略只要在
`extract_fields` 里产出 `CustomerData.products`，分组与落库自动生效。

输入约定（`CustomerData.products`，每条 = 一个产品/一个款）：

    {
      "label": "Match it Puzzle Book - DINOSAURS",   # 人看的标识（款名/标题行）
      "values": {"size": "250 x 253 x 23 mm", ...},  # 只放**该款自己的**结构键
      "quantity": 1226,                               # 该款的数量（可缺）
      "titles": 1,                                    # 该条目代表几款（默认 1）
      "language_codes": ["5/28", "5/20"],             # 该款涉及的语言码（可缺）
      "languages": 2,                                 # 或者直接给语言数（可缺）
      "source": "Anlage_574239!A12:J17",              # 出处（审计/CPMS 来源对照用）
    }

没有 `products` 时退化成「整份数据 = 1 个条目」，行为与 1.0.54 之前**完全一致**
（单产品询价不会因为这次改动产生任何差异）。

命令行（调试/审计用）：

    uv run ./scripts/product_groups.py <文件> --customer ngv
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "assets" / "structure_signature.yaml"

# 配置文件读不到时的兜底（与 assets/structure_signature.yaml 的 keys 保持一致）
DEFAULT_KEYS = (
    "binding", "binding_instructions", "size", "format", "extent",
    "text_material", "cover_material", "ends_material", "jacket_material",
    "boards", "padding",
    "text_printing_spec", "cover_printing_spec", "ends_printing_spec",
    "finishes", "back",
    "specials", "primary_packaging", "packaging_unit", "assortment", "sticker",
)
DEFAULT_JOIN_KEYS = ("product_name", "series_name", "notes")
DEFAULT_SUM_KEYS = ("quantity", "qty_per_title", "qty_total")
DEFAULT_MUST_AGREE = ("fsc", "offer_deadline", "delivery_date", "files_to_printer")

# 分组后由本模块重算的键（不能从基础值里继承，否则整封询价的总量会落到每一组）
_RECOMPUTE_KEYS = ("quantity", "qty_per_title", "qty_total",
                   "versions", "number_of_titles")


# ── 归一化 ───────────────────────────────────────────────────────────────
_TIMES = re.compile(r"\s*[x×*]\s*", re.IGNORECASE)
_WS = re.compile(r"\s+")
# `100 g 书纸` / `100g书纸`、`24 pp` / `24pp` —— 数字与单位之间的空格不算差异
_DIGIT_SPACE = re.compile(r"(?<=\d)\s+(?=[^\d\s])|(?<=[^\d\s])\s+(?=\d)")


def norm(value: Any) -> str:
    """把「同一个意思的不同写法」归成一个签名片段。

    `210 x 280 mm` / `210×280mm` / `210X280 MM ` → `210x280 mm`

    只做**写法**归一，绝不改含义：不做单位换算（mm↔cm 由抽取层负责）、
    不删词、不猜同义词。归一后仍不同的值 = 真的不同结构，必须分开报价。
    """
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    text = text.replace("\u00a0", " ").replace("\u2013", "-").replace("\u2014", "-")
    text = _TIMES.sub("x", text)
    text = _WS.sub(" ", text).strip()
    text = _DIGIT_SPACE.sub("", text)
    text = text.strip(" ;,.")
    return text.casefold()


def _as_int(value: Any) -> Optional[int]:
    """`'12.500'` / `'12,500'` / `12500` / `12500.0` → 12500；取不到 → None。"""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    text = re.sub(r"[^\d,.\-]", "", text)
    if not text:
        return None
    # 德式 `12.500` 与英式 `12,500` 都当千分位；小数点后 1~2 位当小数
    if re.fullmatch(r"-?\d{1,3}([.,]\d{3})+", text):
        text = text.replace(".", "").replace(",", "")
    elif re.fullmatch(r"-?\d+[.,]\d{1,2}", text):
        text = text.replace(",", ".")
    else:
        text = text.replace(".", "").replace(",", "")
    try:
        return int(float(text))
    except ValueError:
        return None


# ── 配置 ─────────────────────────────────────────────────────────────────
def load_config(path: Optional[Path] = None) -> dict:
    """读 `assets/structure_signature.yaml`；文件缺失时用内置兜底（不炸）。"""
    cfg: dict = {}
    target = Path(path) if path else CONFIG_PATH
    if target.exists():
        try:
            import yaml

            cfg = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
        except Exception:                                    # pragma: no cover
            cfg = {}

    def pick(name: str, fallback: tuple) -> tuple:
        raw = cfg.get(name)
        if not raw:
            return tuple(fallback)
        return tuple(str(x) for x in raw)

    return {
        "keys": pick("keys", DEFAULT_KEYS),
        "join_keys": pick("join_keys", DEFAULT_JOIN_KEYS),
        "sum_keys": pick("sum_keys", DEFAULT_SUM_KEYS),
        "must_agree": pick("must_agree", DEFAULT_MUST_AGREE),
        "version": cfg.get("version", 1),
    }


# ── 分组结果 ─────────────────────────────────────────────────────────────
@dataclass
class Group:
    """一条 ERP 记录 = 一个报价结构 + 该结构下的 N 款。"""

    index: int                                  # 从 1 开始
    signature: tuple                            # 归一化后的结构签名（审计用）
    entries: list = field(default_factory=list)  # 参与合并的原始条目
    values: dict = field(default_factory=dict)   # 可直接喂 map_fields 的规范键
    titles: int = 1                             # 本组代表几款（= TZS）
    languages: Optional[int] = None             # 本组涉及几种语言（= YYS 来源）
    quantity: Optional[int] = None              # 本组印刷总量
    quantity_tiers: list = field(default_factory=list)
    label: str = ""                             # 人看的说明（日志/报告/文件名）
    notes: list = field(default_factory=list)   # 合并过程中要说给业务听的话
    from_multi: bool = False                    # 整份询价是不是多产品（不止 1 个条目）

    @property
    def aggregated(self) -> bool:
        """是否由多条条目合并而来（= 这次分组真的动手拆/并了）。"""
        return len(self.entries) > 1

    def evidence(self) -> list:
        """出处清单（CPMS 来源对照、报告用）。"""
        out = []
        for entry in self.entries:
            out.append({
                "label": entry.get("label", ""),
                "source": entry.get("source", ""),
                "titles": entry.get("titles", 1),
            })
        return out


# ── 条目 ─────────────────────────────────────────────────────────────────
def entries_from(customer_data: Any) -> list:
    """取 `CustomerData.products`；没有就退化成 1 个条目（只用基础值）。"""
    products = list(getattr(customer_data, "products", None) or [])
    if products:
        cleaned = []
        for raw in products:
            if not isinstance(raw, dict):
                continue
            cleaned.append({
                "label": str(raw.get("label") or "").strip(),
                "values": {str(k): v for k, v in (raw.get("values") or {}).items()
                           if str(v or "").strip()},
                "quantity": _as_int(raw.get("quantity")),
                "titles": max(1, _as_int(raw.get("titles")) or 1),
                "language_codes": [str(c) for c in (raw.get("language_codes") or []) if
                                   str(c or "").strip()],
                "languages": _as_int(raw.get("languages")),
                "quantity_tiers": [_as_int(t) for t in (raw.get("quantity_tiers") or [])
                                   if _as_int(t) is not None],
                "source": str(raw.get("source") or "").strip(),
            })
        if cleaned:
            return cleaned
    return [{
        "label": "", "values": {}, "quantity": None, "titles": 1,
        "language_codes": [], "languages": None, "source": "",
        # 整份询价 = 1 个条目时，它的数量档位就是整份数据的档位（不能丢）
        "quantity_tiers": list(getattr(customer_data, "quantity_tiers", None) or []),
        "_single": True,                            # 标记：整份数据就是一个条目
    }]


def _entry_lookup(entry: dict, base_get: Callable[[str], Any]) -> Callable[[str], Any]:
    """条目自己的值优先，缺了退回整份询价的值（邮件/表单里共用的部分）。"""

    def lookup(key: str) -> Any:
        value = entry.get("values", {}).get(key)
        if value is None or not str(value).strip():
            value = base_get(key)
        return value

    return lookup


# ── 分组 ─────────────────────────────────────────────────────────────────
def build(customer_data: Any, profile: Any = None, logger: Any = None,
          cplb_resolver: Optional[Callable[[dict], str]] = None,
          config: Optional[dict] = None) -> list:
    """`CustomerData` → `[Group]`（顺序 = 客户文件里首次出现的顺序）。

    参数：
      cplb_resolver : `dict(规范键) -> '22.03;假精装书'`。传了就把推导出的
                      CPLB 拼进签名尾部 —— 装订/工艺不同但字面键相同的两款
                      （例如 `specials` 写法不同却被归一成一样）也能拆开。
                      没传则只按 `config["keys"]` 分组（单元测试/独立调用）。
    """
    cfg = config or load_config()
    keys = list(cfg["keys"])
    base_get = getattr(customer_data, "get", lambda key, default="": default)
    base_values = dict(getattr(customer_data, "values", None) or {})

    entries = entries_from(customer_data)

    # 哪些签名键「至少有一个条目提供了值」——只对这些键做逐条目对比，
    # 其余键沿用整份询价的值（否则一个没人写的键会造出假的差异）。
    effective = [k for k in keys
                 if any(str(e["values"].get(k) or "").strip() for e in entries)]

    def signature_of(entry: dict, lookups: dict) -> tuple:
        parts = []
        for key in effective:
            parts.append(norm(lookups[id(entry)](key)))
        if cplb_resolver is not None:
            merged = {k: lookups[id(entry)](k) for k in effective}
            try:
                parts.append(norm(cplb_resolver(merged)))
            except Exception:                        # pragma: no cover - 推导失败不阻断分组
                parts.append("")
        return tuple(parts)

    lookups = {id(e): _entry_lookup(e, base_get) for e in entries}
    buckets: dict = {}
    order: list = []
    for entry in entries:
        sig = signature_of(entry, lookups)
        if sig not in buckets:
            buckets[sig] = []
            order.append(sig)
        buckets[sig].append(entry)

    # 整份询价是不是多产品 —— 决定两件事：
    #   ① 「整份合计」这类值（scanned 里的总量）还能不能继续用；
    #   ② 款数/语言数要不要**按组重算**。整份询价有多个产品时，
    #      哪怕某组只有 1 个条目，它的 TZS 也必须是 1，不能沿用整份的
    #      `versions`（那是整封询价的款数，实测会把单款组落成 TZS=6）。
    multi = len(entries) > 1

    groups: list = []
    for idx, sig in enumerate(order, start=1):
        members = buckets[sig]
        group = _merge_group(idx, sig, members, effective, cfg,
                             lookups, base_values, base_get, from_multi=multi)
        group.from_multi = multi
        groups.append(group)

    if logger is not None:
        if len(groups) > 1:
            logger.info(f"  报价结构分组: 拆成 {len(groups)} 组"
                        f"（{len(entries)} 个产品条目 → {len(groups)} 条 ERP 记录）")
            for group in groups:
                logger.info(f"    {group.label}")
        else:
            logger.info(f"  报价结构分组: 1 组（{len(entries)} 个条目，"
                        f"结构一致 → 一条记录，TZS={groups[0].titles}）")

    return groups


def _merge_group(index: int, signature: tuple, members: list, effective: list,
                 cfg: dict, lookups: dict, base_values: dict,
                 base_get: Callable[[str], Any], from_multi: bool = False) -> Group:
    """把一个桶里的条目合成一条记录的数据。"""
    notes: list = []

    # ① 签名键：同桶 ⇒ 归一后一致。取**首个非空原始值**（保留客户写法）。
    values = dict(base_values)
    for key in effective:
        raw_values = []
        for entry in members:
            value = lookups[id(entry)](key)
            if value is not None and str(value).strip():
                raw_values.append(str(value).strip())
        if not raw_values:
            continue
        if len({norm(v) for v in raw_values}) > 1:
            # 归一后已相同的键不该走到这里；真出现了说明 cplb 之外的推导键有冲突
            notes.append(f"「{key}」组内写法不一致（{len(set(raw_values))} 种），"
                         f"取第一条：{raw_values[0]!r}")
        values[key] = raw_values[0]

    # ② 元数据键：拼接 or 必须一致
    join_keys = list(cfg["join_keys"])
    for key in join_keys:
        seen: list = []
        for entry in members:
            value = str(entry.get("values", {}).get(key) or "").strip()
            if value and value not in seen:
                seen.append(value)
        if not seen:
            continue
        if len(seen) == 1:
            values[key] = seen[0]
        else:
            values[key] = " / ".join(seen)
            notes.append(f"「{key}」各款不同 → 拼接为 {values[key]!r}"
                         f"（一条记录装 {len(members)} 款，业务需确认这个写法）")

    for key in list(cfg["must_agree"]):
        seen = []
        for entry in members:
            value = str(lookups[id(entry)](key) or "").strip()
            if value and norm(value) not in seen:
                seen.append(norm(value))
        if len(seen) > 1:
            notes.append(f"「{key}」组内不一致（{seen}）—— 不影响结构判据，"
                         f"但请确认这几款确实可以并成一条记录")

    # ③ 数量：条目各自的数量求和（不能继承整份询价的总量）
    per_qty = [e["quantity"] for e in members if e.get("quantity") is not None]
    quantity = sum(per_qty) if per_qty else None
    if quantity is not None:
        values["quantity"] = str(quantity)
        values.pop("qty_per_title", None)
        values.pop("qty_total", None)

    # ④ 数量档位：长度一致才逐档求和（档位是对齐的报价阶梯）
    tiers = _merge_tiers(members, notes)
    if tiers and len(members) > 1:
        values.pop("quantity", None)         # 档位优先，避免两套数量打架

    # ⑤ 款数 / 语言数
    titles = sum(max(1, e.get("titles") or 1) for e in members)
    codes: set = set()
    langs: list = []
    for entry in members:
        codes.update(entry.get("language_codes") or [])
        if entry.get("languages"):
            langs.append(int(entry["languages"]))
    languages = len(codes) if codes else (max(langs) if langs else None)

    # ⑥ 只有「整份询价是多产品」时才重算 versions —— 单产品询价保持客户原文，
    #    这样老单的产物与改动前逐字节一致。
    #    多产品时**每一组都要重算**（哪怕这组只有 1 个条目）：整封询价的
    #    `versions` 是所有款的总数，直接沿用会让单款组落成 TZS=6。
    if from_multi:
        if languages:
            values["versions"] = f"{titles} titles, {languages} languages"
        else:
            values["versions"] = f"{titles} titles"
        values["number_of_titles"] = str(titles)
        if len(members) > 1:
            notes.append(f"本组由 {len(members)} 个条目合并，TZS 重算为 {titles}"
                         + (f"、YYS 重算为 {languages}" if languages else ""))
        else:
            notes.append(f"本组只有 1 个条目，TZS 重算为 {titles}"
                         + (f"、YYS 重算为 {languages}" if languages else ""))

    label = _describe(index, values, titles, len(members))
    return Group(index=index, signature=signature, entries=list(members),
                 values=values, titles=titles, languages=languages,
                 quantity=quantity, quantity_tiers=tiers, label=label, notes=notes)


def _merge_tiers(members: list, notes: list) -> list:
    """各条目的数量档位逐档求和。档位长度不一致时只取第一组并报警。"""
    stacks = [list(e.get("quantity_tiers") or []) for e in members]
    stacks = [s for s in stacks if s]
    if not stacks:
        return []
    if len(stacks) == 1:
        return [int(q) for q in stacks[0]]
    widths = {len(s) for s in stacks}
    if len(widths) == 1:
        merged = [sum(int(s[i]) for s in stacks) for i in range(len(stacks[0]))]
        notes.append(f"数量档位逐档求和: {stacks} → {merged}")
        return merged
    notes.append(f"各款数量档位不一致（长度 {sorted(widths)}）→ 只落第一组 "
                 f"{stacks[0]}，CPSL 需人工核对")
    return [int(q) for q in stacks[0]]


def _describe(index: int, values: dict, titles: int, member_count: int) -> str:
    """`第2组：22.03;假精装书 / 140x200 / 216PP / 3 款`（日志与报告用）。"""
    parts = [f"第{index}组"]
    head = str(values.get("cplb") or values.get("binding") or "").strip()
    if head:
        parts.append(head)
    size = str(values.get("size") or "").strip()
    if size:
        parts.append(size)
    extent = str(values.get("extent") or "").strip()
    if extent:
        parts.append(extent)
    head_line = " / ".join(parts)
    tail = f"{titles} 款" if titles != 1 else "1 款"
    if member_count > 1:
        tail += f"（{member_count} 个条目合并）"
    return f"{head_line} / {tail}"


# ── 浇回 CustomerData ────────────────────────────────────────────────────
def to_customer_data(group: "Group", base: Any) -> Any:
    """把一组浇成一份**独立**的 `CustomerData`，直接交给 `main.map_fields`。

    * `values` 用组内合并结果（含该组自己的尺寸/用纸/数量）；
    * `scanned` 里与数量相关的键要清掉 —— 那是整封询价的总量，
      留在组里会让每一组都拿到同一个总数；
    * `products` 清空，避免二次分组。
    """
    from extract_fields import CustomerData

    data = CustomerData(
        values=dict(group.values),
        raw_labels=dict(getattr(base, "raw_labels", None) or {}),
        quantity_tiers=list(group.quantity_tiers or []),
        text_blocks=list(getattr(base, "text_blocks", None) or []),
        unmatched=list(getattr(base, "unmatched", None) or []),
        extras_found=list(getattr(base, "extras_found", None) or []),
        scanned=dict(getattr(base, "scanned", None) or {}),
        scan_notes=list(getattr(base, "scan_notes", None) or []),
        layout=str(getattr(base, "layout", "") or ""),
        profile_id=str(getattr(base, "profile_id", "") or ""),
        unit_kind=str(getattr(base, "unit_kind", "file") or "file"),
        unit_label=str(getattr(base, "unit_label", "") or ""),
        members=list(getattr(base, "members", None) or []),
        sources=dict(getattr(base, "sources", None) or {}),
        conflicts=list(getattr(base, "conflicts", None) or []),
        products=[],                       # 已分组，别再分一次
    )
    if group.from_multi:
        for key in _RECOMPUTE_KEYS:
            data.scanned.pop(key, None)
    return data


def summarize(groups: list) -> list:
    """给 `validation.json` 用的分组摘要（业务/审计一眼看这次拆了几条）。"""
    return [{
        "record": group.index,
        "label": group.label,
        "titles": group.titles,
        "languages": group.languages,
        "quantity": group.quantity,
        "quantity_tiers": list(group.quantity_tiers or []),
        "entries": len(group.entries),
        "signature_keys": [k for k, v in zip(range(len(group.signature)), group.signature) if v],
        "evidence": group.evidence(),
        "notes": list(group.notes or []),
    } for group in groups]


# ── 命令行：只看分组结果（不产出 ERP JSON）─────────────────────────────
def main() -> int:                                        # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from customer_profile import resolve_profile
    from extract_fields import load_customer_data

    parser = argparse.ArgumentParser(description="按报价结构分组（调试/审计）")
    parser.add_argument("path", help="客户报价文件")
    parser.add_argument("--customer", help="客户档案 id（缺省自动识别）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    profile, source = resolve_profile(args.customer, filename=Path(args.path).stem)
    data = load_customer_data(args.path, profile)
    groups = build(data, profile)
    if args.json:
        print(json.dumps({"profile": profile.id, "profile_source": source,
                          "groups": summarize(groups)}, ensure_ascii=False, indent=2))
        return 0
    print(f"客户档案: {profile.id}（{source}）")
    print(f"产品条目: {len(entries_from(data))} 个 → 报价结构: {len(groups)} 组")
    for group in groups:
        print(f"\n{group.label}")
        print(f"  签名: {group.signature}")
        for key in sorted(group.values):
            print(f"  {key}: {group.values[key]}")
        for note in group.notes:
            print(f"  ! {note}")
    return 0


if __name__ == "__main__":                                # pragma: no cover
    raise SystemExit(main())
