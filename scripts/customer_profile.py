#!/usr/bin/env python3
"""
customer_profile.py - 客户档案：加载 / 校验 / 自动识别（多客户支持的核心）

一个「客户档案」= `customers/<id>.yaml` 一份文件，声明该客户的：

  1. detect      —— 怎么从邮件/文件里认出是这个客户
  2. fields      —— 客户表里的列名 → 规范键（客户习惯全在这）
  3. erp_fixed   —— 该客户的 ERP 固定值
  4. rules       —— 解析规则（布局策略、尺寸顺序、数量档位写法、材质表、装订映射…）
  5. email       —— 邮件规则（客户单号 KHDH 从标题/正文怎么取）

所有档案都 deep-merge 到 `customers/_defaults.yaml` 之上（客户档案覆盖默认），
所以新增客户只需写「与默认不同的部分」。

设计约束（重要）：**`main.py` 里不允许出现任何客户的列名。**
列名只属于档案；档案只负责「客户怎么写 → 规范键叫什么」。
详见 SKILL.md「客户档案规范」与 customers/README.md。

命令行：
    uv run ./scripts/customer_profile.py --list              # 列出所有档案
    uv run ./scripts/customer_profile.py --show igl          # 打印某份档案摘要
    uv run ./scripts/customer_profile.py --validate          # 校验全部档案
    uv run ./scripts/customer_profile.py --detect <文件>      # 用文件内容试识别

退出码：0 成功，1 失败（校验不通过 / 档案不存在）。
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

try:
    import yaml
except ImportError:  # pragma: no cover
    print("错误：需要 pyyaml。请运行: uv add pyyaml", file=sys.stderr)
    raise

DEFAULTS_ID = "generic"          # 兜底档案的 id（文件名 _defaults.yaml）
DEFAULTS_FILE = "_defaults.yaml"

# ── 规范键（canonical keys）───────────────────────────────────────────────
# 档案的 fields: 只能使用这里的键。新增规范键 = 同时改 main.py 的赋值逻辑。
CANONICAL_KEYS = {
    # 信息类
    "imprint", "complexity", "requested_by", "pub_month", "series_name",
    "number_of_titles", "group", "category", "safety_checks", "target_age_group",
    "works_references", "group_series_number", "isbn", "project_description",
    # 规格类
    "size", "format", "extent", "text_printing_spec", "text_material",
    "cover_printing_spec", "cover_material", "spine_width", "binding", "finishes",
    # 数量档位
    "quantity_tiers",
    # 客户单号（来自邮件正文/标题的独立列，可选）
    "khdh",
    # 香港单号（附件文件名/表内的「香港单号 CU26051601-01」；2026-09-24 NGV 校准加）
    "khddh",
    # ── NGV 类「分区表单」客户的规范键（2026-09-23 校准加）───────────────
    # 这些键由 layout.strategies 里的 section_pairs 策略赋值；
    # 用不到的客户档案不必声明它们，不影响既有客户。
    "product_name",          # 成品名（NGV B1：Peppa Pig Stickern mit Peppa）
    "quantity",              # 订单数量原文（"512580cps" / "4 x 17.500"）
    "versions",              # 款数/语言原文（"12 titles, 8 countries, 5 languages"）
    "orientation",           # 横竖（portrait / landscape）
    "offer_deadline",        # 报价截止
    "files_to_printer",      # 文件到印厂
    "delivery_date",         # 交货/ETA
    "fsc",                   # FSC 类别
    "contact",               # 客户联系人
    "our_ref",               # 我方编号（PR-xxxxxxxx）
    "text_thickness",        # 书芯厚度（mm）
    "binding_instructions",  # 装订工艺原文（perfect binding / lose / see below）
    "binding_side",          # 装订所在边（mm side）
    "back",                  # 书脊形态（square / round）
    "ends_material", "ends_printing_spec",   # 环衬（ends / Vor-/Nachsatz）
    "boards", "padding",                     # 纸板 / 裱棉
    "jacket_material", "jacket_printing_spec", "jacket_finishes",  # 护封
    "primary_packaging", "sticker",          # 一级包装 / 贴纸
    "packaging_unit", "assortment",          # 装箱数 / 混装比例
    "specials", "notes",                     # 特别说明 / 备注（长文本）
    "packaging_specials",                    # 包装区的 specials 行（与 specials 分区不同行）
}


class ProfileError(Exception):
    """档案加载/校验失败。"""


# ── 工具 ─────────────────────────────────────────────────────────────────
def norm_label(value: Any) -> str:
    """标签归一化：转字符串、去不换行空格、压缩空白、去首尾、转小写。

    XLSX/CSV 里的列名常有杂空格（`Series  Name `）与 NBSP，必须先归一化再比较。
    """
    if value is None:
        return ""
    s = str(value).replace("\u00a0", " ").replace("\u3000", " ")
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def profiles_dir() -> Path:
    """customer 档案目录（仓库根目录下的 customers/）。"""
    return Path(__file__).resolve().parent.parent / "customers"


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并：dict 逐键合并，其它类型（含 list）整体替换。"""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader，但**遇到重复键直接报错**。

    PyYAML 默认对重复键「静默保留最后一个」：档案里同一层级写两遍 `unit:`，
    前一份被无声吃掉。实测踩过（rules.unit 写了两遍，运行行为与预期不符却毫无提示），
    所以这里在解析阶段就把重复键变成硬错误 —— 比事后手工扫文本可靠，也能给出准确行号。
    """


def _construct_unique_mapping(loader, node, deep=False):
    loader.flatten_mapping(node)
    mapping: dict = {}
    lines: dict = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError:                     # 不可哈希的键（YAML 里极少见）
            duplicate = False
        if duplicate:
            raise ProfileError(
                f"档案存在重复键 {key!r}（第 {key_node.start_mark.line + 1} 行，"
                f"首次出现在第 {lines[key]} 行）—— YAML 默认静默保留最后一个，"
                f"改前一处不会生效"
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
        try:
            lines[key] = key_node.start_mark.line + 1
        except TypeError:
            pass
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


def _read_yaml(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as exc:
        raise ProfileError(f"读取档案失败 {path}: {exc}") from exc

    try:
        data = yaml.load(text, Loader=_UniqueKeyLoader) or {}
    except yaml.YAMLError as exc:
        raise ProfileError(f"YAML 解析失败 {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ProfileError(f"档案内容必须是映射（dict）: {path}")
    return data


# ── 档案对象 ─────────────────────────────────────────────────────────────
@dataclass
class CustomerProfile:
    id: str
    name: str
    priority: int
    status: str
    detect: dict
    fields: dict
    erp_fixed: dict
    rules: dict
    email: dict
    notes: str
    extras: list = field(default_factory=list)
    # 「纯标签」：只是表单的栏目标题（双语挤一格，如 `TPS\nFormat`），**不含值**。
    # 常见的两处误用：① 校准报告把它们当成「认不出的标签」；
    #              ② 落进 extras_found 当成「CPMS 素材」，让 agent 以为漏了信息。
    # 登记在这里就两处都不再出现。与 extras: 的区别：extras 是**真信息**（要写进 CPMS）。
    ignored_labels: list = field(default_factory=list)
    sections: list = field(default_factory=list)
    preferences: list = field(default_factory=list)
    source_path: Optional[Path] = None
    # 内部：别名索引 [(归一化别名, 规范键)]，按别名长度倒序（最长优先）
    _alias_index: list = field(default_factory=list, repr=False)

    # ---- 构造 ----
    @classmethod
    def from_raw(cls, raw: dict, source_path: Optional[Path] = None) -> "CustomerProfile":
        prof = cls(
            id=str(raw.get("id", "")).strip(),
            name=str(raw.get("name", "")).strip(),
            priority=int(raw.get("priority", 0) or 0),
            status=str(raw.get("status", "draft")).strip() or "draft",
            detect=raw.get("detect") or {},
            fields=raw.get("fields") or {},
            erp_fixed=raw.get("erp_fixed") or {},
            rules=raw.get("rules") or {},
            email=raw.get("email") or {},
            notes=str(raw.get("notes", "")).strip(),
            extras=list(raw.get("extras") or []),
            ignored_labels=list(raw.get("ignored_labels") or []),
            sections=list(raw.get("sections") or []),
            preferences=list(raw.get("preferences") or []),
            source_path=source_path,
        )
        prof._build_alias_index()
        return prof

    def _build_alias_index(self) -> None:
        index = []
        for canonical, aliases in self.fields.items():
            if isinstance(aliases, str):
                aliases = [aliases]
            for alias in aliases or []:
                a = norm_label(alias)
                if a:
                    index.append((a, canonical))
        # 最长别名优先，避免 "Group" 抢走 "Group/Series Number"
        index.sort(key=lambda item: len(item[0]), reverse=True)
        self._alias_index = index

    # ---- 查询 ----
    def rule(self, dotted_path: str, default: Any = None) -> Any:
        """按点号路径读 rules: rule("layout.max_label_len", 60)。"""
        node: Any = self.rules
        for part in dotted_path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node if node is not None else default

    @property
    def is_calibrated(self) -> bool:
        return self.status.lower() == "calibrated"

    def match_label(self, cell: Any) -> Optional[str]:
        """判断单元格是不是某个已知标签；是则返回规范键，否则 None。

        匹配顺序：最长别名优先 → 完全相等，或整词包含（允许复数 s）。
        超过 max_label_len 的单元格一律不当标签（防止值被误判成标签）。
        """
        text = norm_label(cell)
        if not text:
            return None
        max_len = int(self.rule("layout.max_label_len", 60))
        if len(text) > max_len:
            return None
        for alias, canonical in self._alias_index:
            if text == alias:
                return canonical
            if alias in text and re.search(
                r"(?<![a-z0-9])" + re.escape(alias) + r"s?(?![a-z0-9])", text
            ):
                return canonical
        return None

    def match_label_exact(self, cell: Any) -> Optional[str]:
        """只做归一化后的**完全相等**匹配。

        自由文本（TXT/DOCX/PDF）按「标签 + 分隔符 + 值」切行时必须用它：
        `match_label` 是「整词包含」，会把 `Series Nam`（行的前缀）也算成命中
        `Series Name`，于是「最长前缀」永远是整行、值反而丢了。
        精确匹配下只有切到标签末尾那一点才算命中。
        """
        text = norm_label(cell)
        if not text:
            return None
        if len(text) > int(self.rule("layout.max_label_len", 60)):
            return None
        for alias, canonical in self._alias_index:
            if text == alias:
                return canonical
        return None

    def match_section(self, cell: Any) -> Optional[dict]:
        """判断某个单元格是不是「已声明的分区名」（section_pairs 策略用）。

        返回命中的分区定义（dict），否则 None。匹配用「归一化后整词包含」，
        因为客户常把双语分区名挤在一个单元格里（`finishing\\nVerarbeitung`）。
        多个分区名同时命中时取**最长的那个**（避免 'specials' 抢走更具体的名字）。
        """
        text = norm_label(cell)
        if not text:
            return None
        best = None
        best_len = -1
        for section in self.sections or []:
            for name in section.get("names") or []:
                alias = norm_label(name)
                if not alias:
                    continue
                if alias == text or re.search(
                    r"(?<![a-z0-9])" + re.escape(alias) + r"s?(?![a-z0-9])", text
                ):
                    if len(alias) > best_len:
                        best, best_len = section, len(alias)
        return best

    def is_known_extra(self, cell: Any) -> bool:
        """该单元格是不是「已知的附加信息」（不是 ERP 字段，但要写进 CPMS）。

        客户表格里除了 ERP 字段，还常有包装/纸箱/报价有效期/生产地址之类的补充栏位。
        它们不进 ERP JSON，但**必须**体现在 CPMS 里 —— 在档案 extras: 登记后，
        校准报告就不再把它们算成「认不出的标签」，main.py 也会把它们列成 CPMS 素材。
        """
        text = norm_label(cell)
        if not text:
            return False
        for extra in self.extras:
            alias = norm_label(extra)
            if not alias:
                continue
            if text == alias or re.search(
                r"(?<![a-z0-9])" + re.escape(alias) + r"s?(?![a-z0-9])", text
            ):
                return True
        return False

    def is_ignored_label(self, cell: Any) -> bool:
        """该文本块是不是「纯标签」—— 只有栏目标题、没有任何值。

        与 `is_known_extra()` 的分工（2026-09-24 拆开，实测踩过）：
          * `extras:`          —— **真信息**（纸箱规格、报价有效期、生产地址…）→ 必须写进 CPMS
          * `ignored_labels:`  —— **纯标题**（NGV 的 `TPS\\nFormat`、`back\\nRücken`）→ 哪都不进

        NGV 表里栏目标题是「英文 ⏎ 德文」挤在一格，以前为了压掉校准噪音把它们塞进
        `extras:`，结果它们又出现在 `extras_found`（「CPMS 素材」）里，看起来像漏了信息。

        判定：块内**每一行**都命中 ignored_labels 才算纯标签（多行块只要有一行是值，
        整块仍算素材）。所以「标签格 + 同行值」这类合并块不会被误杀。
        """
        lines = [l for l in str(cell or "").splitlines() if l.strip()]
        if not lines:
            return False
        aliases = [norm_label(x) for x in self.ignored_labels]
        aliases = [a for a in aliases if a]
        if not aliases:
            return False

        def is_title(line: str) -> bool:
            text = norm_label(line)
            return any(
                text == alias or re.search(
                    r"(?<![a-z0-9])" + re.escape(alias) + r"s?(?![a-z0-9])", text)
                for alias in aliases
            )

        return all(is_title(line) for line in lines)

    def erp_fixed_values(self) -> dict:
        """ERP 固定值（只保留值是标量的项）。"""
        return {
            k: v for k, v in self.erp_fixed.items()
            if not isinstance(v, (dict, list))
        }

    # ---- 客户偏好表 ----
    def preference_table(self) -> list:
        """客户偏好表：`[{field, value, reason, effective, ok}]`。

        业务 2026-09-24 提出：「有些客户合作比较久，会有一些默认值，需要按客户
        设置一份偏好表，方便快速填写用户习惯。」于是档案里新增 `preferences:` 段，
        把「这个客户哪些字段固定填什么、哪些字段就该空着」连同**依据**写在一处。

        与 `erp_fixed` 的分工（刻意分开，别合并）：
          * `erp_fixed`   —— 机器读的落库值（`main.py` 只认它）
          * `preferences` —— 人看的清单（含依据，供业务快速核对/填写）
        两者的一致性由 `validate()` 与 `tests/test_customer_profile.py` 保证，
        避免「文档说留空、代码里还填着 1」这类漂移。
        """
        effective = self.erp_fixed_values()
        rows = []
        for entry in self.preferences or []:
            if not isinstance(entry, dict):
                continue
            field_name = str(entry.get("field") or "").strip()
            value = entry.get("value")
            value = "" if value is None else str(value)
            eff = effective.get(field_name)
            eff = "" if eff is None else str(eff)
            rows.append({
                "field": field_name,
                "value": value,
                "reason": str(entry.get("reason") or "").strip(),
                "effective": eff,
                "ok": eff == value,
            })
        return rows

    def describe_preferences(self) -> str:
        """把客户偏好表打成给人看的表（`--prefs`）。"""
        rows = self.preference_table()
        lines = [f"客户偏好表: {self.id}（{self.name}）",
                 "  口径: 写了值就直接落库；值为空 = 该字段就该空着（推不出来不猜）",
                 ""]
        if not rows:
            lines.append("  （未声明 preferences 段 —— 该客户没有固定口径）")
            return "\n".join(lines)
        lines.append(f"  {'字段':<10} {'落库值':<14} {'档案实际值':<14} 依据")
        lines.append("  " + "-" * 96)
        for r in rows:
            shown = r["value"] or "（留空）"
            eff = r["effective"] or "（空）"
            flag = "" if r["ok"] else "  ⚠️ 与 erp_fixed 不一致"
            lines.append(f"  {r['field']:<10} {shown:<14} {eff:<14} {r['reason']}{flag}")
        return "\n".join(lines)

    def number_rules(self) -> list:
        """email 段里声明的「单号」规则 → [(规则名, 配置, 目标 ERP 字段)]。

        一个客户可能同时有两个单号，**必须分开**（NGV 实测，2026-09-24）：
            email.khdh:  { target: KHDH,  attachment_keys: [],        ... }  # 客户单号
            email.khddh: { target: KHDDH, attachment_keys: [khddh],   ... }  # 香港单号
        `target` 不写时默认取规则名大写（老档案 email.khdh → KHDH，行为不变）。
        """
        rules = []
        for rule_key, cfg in (self.email or {}).items():
            if not isinstance(cfg, dict):
                continue
            if not any(k in cfg for k in ("attachment_keys", "subject_patterns", "body_patterns")):
                continue
            target = str(cfg.get("target") or rule_key.upper()).strip()
            if target:
                rules.append((str(rule_key), cfg, target))
        return rules

    # ---- 识别 ----
    def detect_score(
        self,
        texts: list,
        filename: str = "",
        sender: str = "",
        subject: str = "",
    ) -> tuple:
        """给本档案对某个样本打匹配分。返回 (score, reasons)。"""
        score = 0
        reasons: list = []
        haystack = norm_label("\n".join(str(t) for t in texts or []))
        fname = norm_label(filename)
        sender_n = norm_label(sender)
        subject_n = norm_label(subject)

        signatures = [norm_label(s) for s in (self.detect.get("label_signatures") or [])]
        hits = [s for s in signatures if s and s in haystack]
        if hits:
            needed = int(self.detect.get("min_signature_hits", 1) or 1)
            if len(hits) >= needed:
                score += 100 * len(hits)
                reasons.append(f"命中特征标签 {len(hits)} 个: {hits[:3]}")

        for kw in self.detect.get("filename_keywords") or []:
            if norm_label(kw) and norm_label(kw) in fname:
                score += 10
                reasons.append(f"文件名含 '{kw}'")

        for dom in self.detect.get("sender_domains") or []:
            if norm_label(dom) and norm_label(dom) in sender_n:
                score += 30
                reasons.append(f"发件人域名 '{dom}'")

        for kw in self.detect.get("subject_keywords") or []:
            if norm_label(kw) and norm_label(kw) in subject_n:
                score += 5
                reasons.append(f"主题含 '{kw}'")

        return score, reasons

    # ---- 校验 ----
    def validate(self) -> list:
        """返回问题清单（空 = 通过）。"""
        problems = []
        if not self.id:
            problems.append("缺少 id")
        if self.status not in ("draft", "calibrated"):
            problems.append(f"status 取值非法: {self.status}（应为 draft / calibrated）")

        # 规范键校验
        for canonical in self.fields:
            if canonical not in CANONICAL_KEYS:
                problems.append(
                    f"fields 里有未知规范键 '{canonical}'"
                    f"（可用键见 customer_profile.CANONICAL_KEYS）"
                )

        # erp_fixed 的键必须是 ERP 模板里的字段
        tpl_path = profiles_dir().parent / "assets" / "add_customer_quote_request.json"
        tpl_keys: set = set()
        if tpl_path.exists():
            with open(tpl_path, "r", encoding="utf-8") as f:
                tpl_keys = set(json.load(f).keys())
            for key in self.erp_fixed:
                if key not in tpl_keys:
                    problems.append(f"erp_fixed 里有模板不存在的 ERP 字段 '{key}'")

        # 客户偏好表（preferences）—— 与人看的清单必须和机器读的 erp_fixed 一致。
        # 这是防漂移的关键一处：文档说「留空」而 erp_fixed 里还写着 1，
        # 或者偏好表填了值却没落进 erp_fixed（等于没生效），都在这里拦下。
        effective = self.erp_fixed_values()
        seen_pref: set = set()
        for p_idx, pref in enumerate(self.preferences or []):
            if not isinstance(pref, dict):
                problems.append(f"preferences[{p_idx}] 必须是映射（dict）")
                continue
            field_name = str(pref.get("field") or "").strip()
            if not field_name:
                problems.append(f"preferences[{p_idx}] 缺少 field")
                continue
            if field_name in seen_pref:
                problems.append(f"preferences 里 '{field_name}' 重复声明（只留一条）")
            seen_pref.add(field_name)
            if tpl_keys and field_name not in tpl_keys:
                problems.append(f"preferences 里有模板不存在的 ERP 字段 '{field_name}'")
            value = "" if pref.get("value") is None else str(pref.get("value"))
            eff = effective.get(field_name)
            eff = "" if eff is None else str(eff)
            if eff != value:
                problems.append(
                    f"preferences 里 {field_name}={value!r} 与 erp_fixed 实际值 {eff!r} 不一致 —— "
                    f"偏好表是给人看的、erp_fixed 才是落库值，两者必须同步"
                    + ("（想留空就在 erp_fixed 里显式写 ''，否则模板默认值会留在产物里）"
                       if value == "" else "")
                )
            if not str(pref.get("reason") or "").strip():
                problems.append(f"preferences 里 {field_name} 缺少 reason —— 偏好表必须能一眼看出依据")

        # 布局策略白名单
        allowed = {"auto_pairs", "column_pairs", "shifted_pairs", "section_pairs"}
        for strategy in self.rule("layout.strategies", []) or []:
            if strategy not in allowed:
                problems.append(f"未知布局策略 '{strategy}'（可用: {sorted(allowed)}）")

        # sections 段（section_pairs 策略用）：结构必须完整，否则抽取会静默漏字段
        for idx, section in enumerate(self.sections or []):
            if not isinstance(section, dict):
                problems.append(f"sections[{idx}] 必须是映射（dict）")
                continue
            names = section.get("names") or []
            if not names:
                problems.append(f"sections[{idx}] 缺少 names（分区名/别名）")
            entries = section.get("entries") or []
            if not entries:
                problems.append(f"sections[{idx}]（{names}）缺少 entries")
            for e_idx, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    problems.append(f"sections[{idx}].entries[{e_idx}] 必须是映射")
                    continue
                if "section_value" in entry:
                    key = entry["section_value"]
                    if key is not None and key not in CANONICAL_KEYS:
                        problems.append(
                            f"sections[{idx}].entries[{e_idx}] section_value='{key}' 不是规范键"
                        )
                    continue
                key = entry.get("key")
                if not entry.get("label"):
                    problems.append(f"sections[{idx}].entries[{e_idx}] 缺少 label")
                if key not in CANONICAL_KEYS:
                    problems.append(
                        f"sections[{idx}].entries[{e_idx}] key='{key}' 不是规范键"
                        f"（可用键见 customer_profile.CANONICAL_KEYS）"
                    )

        # 正则是否可编译
        for section in ("subject_patterns", "body_patterns"):
            for pattern in (self.email.get("khdh") or {}).get(section) or []:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    problems.append(f"email.khdh.{section} 正则非法: {pattern}（{exc}）")

        for entry in self.rule("materials.text.map", []) or []:
            if "match" not in entry or "page_style" not in entry:
                problems.append(f"materials.text.map 条目缺 match/page_style: {entry}")

        if self.id != DEFAULTS_ID and not self.detect:
            problems.append("非兜底档案必须有 detect 段，否则永远无法自动识别")

        return problems

    def describe(self) -> str:
        lines = [
            f"档案: {self.id}（{self.name}）",
            f"  状态: {self.status}"
            f"{'  ⚠️ 未校准，字段值需人工核对' if not self.is_calibrated else ''}",
            f"  优先级: {self.priority}",
            f"  文件: {self.source_path or '(内置)'}",
            f"  别名: {len(self._alias_index)} 条 / 规范键 {len(self.fields)} 个",
            f"  布局策略: {self.rule('layout.strategies', [])}",
            f"  分区表(sections): {len(self.sections)} 个分区"
            + (f"（{', '.join(str((s.get('names') or ['?'])[0]) for s in self.sections)}）"
               if self.sections else ""),
            f"  ERP 固定值: {self.erp_fixed_values()}",
        ]
        if self.preferences:
            ok = sum(1 for r in self.preference_table() if r["ok"])
            lines.append(f"  客户偏好表: {len(self.preferences)} 条"
                         f"（与 erp_fixed 一致 {ok} 条）")
        if self.notes:
            lines.append(f"  备注: {' '.join(self.notes.split())}")
        return "\n".join(lines)


# ── 加载 ─────────────────────────────────────────────────────────────────
def _profile_files() -> list:
    """所有客户档案文件（排除 _ 开头、排除校准草稿）。"""
    d = profiles_dir()
    if not d.exists():
        return []
    return sorted(
        p for p in d.glob("*.yaml")
        if not p.name.startswith("_") and not p.name.startswith("_draft")
    )


def list_profiles() -> list:
    return [p.stem for p in _profile_files()]


def load_defaults() -> dict:
    path = profiles_dir() / DEFAULTS_FILE
    if not path.exists():
        raise ProfileError(f"缺少公共默认档案: {path}")
    return _read_yaml(path)


def load_profile(customer_id: str) -> CustomerProfile:
    """按 id 加载档案（自动叠加公共默认值）。"""
    cid = (customer_id or "").strip()
    if cid in ("", "auto", DEFAULTS_ID):
        raw = load_defaults()
        raw["id"] = DEFAULTS_ID
        raw["name"] = raw.get("name", "未识别客户")
        prof = CustomerProfile.from_raw(raw, profiles_dir() / DEFAULTS_FILE)
        prof._build_alias_index()
        return prof

    path = profiles_dir() / f"{cid}.yaml"
    if not path.exists():
        available = ", ".join(list_profiles()) or "(无)"
        raise ProfileError(f"客户档案不存在: {cid}（可用: {available}, 或 {DEFAULTS_ID}）")

    merged = _deep_merge(load_defaults(), _read_yaml(path))
    merged["id"] = cid
    merged["name"] = merged.get("name") or cid
    prof = CustomerProfile.from_raw(merged, path)
    prof._build_alias_index()
    return prof


def collect_probe_texts(path: Path) -> list:
    """公开入口：把样本文件里的文本倒出来，供客户识别使用。

    CLI 的 `--detect` 与 main.py 流水线共用这一份实现 —— 客户识别必须
    **看正文**，不能只看文件名（见 main.py 里 2026-09-24 那条 bug 注释）。
    """
    return _collect_texts_from_file(Path(path))


def auto_detect(
    texts: list,
    filename: str = "",
    sender: str = "",
    subject: str = "",
) -> list:
    """对所有档案打分，返回 [(profile, score, reasons)]，按分数倒序。"""
    results = []
    for cid in list_profiles():
        try:
            prof = load_profile(cid)
        except ProfileError:
            continue
        score, reasons = prof.detect_score(texts, filename, sender, subject)
        if score > 0:
            results.append((prof, score, reasons))
    results.sort(key=lambda item: (item[1], item[0].priority), reverse=True)
    return results


def resolve_profile(
    customer_arg: Optional[str],
    texts: Optional[list] = None,
    filename: str = "",
    sender: str = "",
    subject: str = "",
    logger=None,
) -> tuple:
    """决定用哪份档案。返回 (profile, source)。

    customer_arg: 'auto' / None / '' → 自动识别；其它值 → 显式指定
    source: '显式指定' | '自动识别' | '兜底'
    """
    def _log(msg: str, level: str = "info"):
        if logger is not None:
            getattr(logger, level, logger.info)(msg)

    if customer_arg and str(customer_arg).strip().lower() not in ("auto", ""):
        prof = load_profile(str(customer_arg))
        _log(f"客户档案: {prof.id}（来源: 显式指定 --customer {prof.id}）")
        return prof, "显式指定"

    candidates = auto_detect(texts or [], filename=filename, sender=sender, subject=subject)
    if candidates:
        prof, score, reasons = candidates[0]
        _log(f"客户档案: {prof.id}（来源: 自动识别，得分 {score}）")
        for reason in reasons:
            _log(f"  识别依据: {reason}")
        if len(candidates) > 1:
            runners = ", ".join(f"{p.id}:{s}" for p, s, _ in candidates[1:4])
            _log(f"  其它候选: {runners}")
        if score < 30:
            _log(f"  [WARN] 识别得分偏低（{score}）—— 只有文件名/主题这类弱证据，"
                 f"没有正文特征命中，建议人工确认客户是否对了", "warn")
        if not prof.is_calibrated:
            _log(f"  [WARN] 档案 {prof.id} 状态为 {prof.status} —— 未经校准，字段值必须人工核对", "warn")
        return prof, "自动识别"

    prof = load_profile(DEFAULTS_ID)
    _log("  [WARN] 未能识别客户，使用兜底档案 generic —— 字段值必须人工核对，"
         "并考虑为该客户建档案（见 customers/README.md）", "warn")
    return prof, "兜底"


# ── CLI ──────────────────────────────────────────────────────────────────
def _collect_texts_from_file(path: Path) -> list:
    """把样本文件里的文本/单元格倒出来，供 --detect 使用。"""
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xls", ".csv"):
        import pandas as pd
        df = pd.read_csv(path) if suffix == ".csv" else pd.read_excel(path)
        return [str(v) for v in df.astype(str).values.ravel().tolist()]
    if suffix == ".pdf":
        import fitz
        with fitz.open(str(path)) as doc:
            return [page.get_text() for page in doc]
    if suffix == ".docx":
        from docx import Document
        return [p.text for p in Document(str(path)).paragraphs]
    return [path.read_text(encoding="utf-8", errors="ignore")]


def main() -> int:
    parser = argparse.ArgumentParser(description="客户档案：加载/校验/识别")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true", help="列出所有客户档案")
    group.add_argument("--show", metavar="ID", help="打印某份档案的摘要")
    group.add_argument("--validate", action="store_true", help="校验全部档案")
    group.add_argument("--detect", metavar="FILE", help="用样本文件试识别客户")
    group.add_argument("--prefs", metavar="ID", nargs="?", const="*",
                       help="打印客户偏好表（不填 ID = 全部档案）；口述口径也以它为准")
    parser.add_argument("--sender", default="", help="配合 --detect：发件人")
    parser.add_argument("--subject", default="", help="配合 --detect：邮件主题")
    args = parser.parse_args()

    if args.prefs:
        ids = list_profiles() if args.prefs == "*" else [args.prefs]
        first = True
        for cid in ids:
            prof = load_profile(cid)
            if not prof.preferences:
                continue
            if not first:
                print()
            first = False
            print(prof.describe_preferences())
        if first:
            print("（没有档案声明 preferences 段）")
        return 0

    if args.list:
        ids = list_profiles()
        if not ids:
            print("(没有客户档案)")
            return 0
        print(f"共 {len(ids)} 份客户档案：")
        for cid in ids:
            prof = load_profile(cid)
            flag = "" if prof.is_calibrated else "  ⚠️ 未校准"
            print(f"  - {cid:10s} {prof.name}{flag}")
        print(f"  - {DEFAULTS_ID:10s} （兜底/公共默认，文件 {DEFAULTS_FILE}）")
        return 0

    if args.show:
        print(load_profile(args.show).describe())
        return 0

    if args.validate:
        failed = 0
        for cid in list_profiles():
            prof = load_profile(cid)
            problems = prof.validate()
            if problems:
                failed += 1
                print(f"✗ {cid}")
                for p in problems:
                    print(f"    - {p}")
            else:
                print(f"✓ {cid}")
        # 兜底档案也校验
        base = load_profile(DEFAULTS_ID)
        problems = base.validate()
        if problems:
            failed += 1
            print(f"✗ {DEFAULTS_ID}")
            for p in problems:
                print(f"    - {p}")
        else:
            print(f"✓ {DEFAULTS_ID}")
        if failed:
            print(f"\n{failed} 份档案未通过校验")
            return 1
        print("\n全部档案校验通过")
        return 0

    if args.detect:
        path = Path(args.detect)
        if not path.exists():
            print(f"文件不存在: {path}", file=sys.stderr)
            return 1
        texts = _collect_texts_from_file(path)
        results = auto_detect(texts, filename=path.name, sender=args.sender, subject=args.subject)
        print(f"样本: {path.name}（{len(texts)} 个文本块）")
        if not results:
            print("未匹配到任何客户档案 → 会走兜底档案 generic")
            return 0
        for prof, score, reasons in results:
            print(f"  {prof.id:10s} 得分 {score:4d}  状态 {prof.status}")
            for reason in reasons:
                print(f"      · {reason}")
        print(f"\n结论: {results[0][0].id}")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
