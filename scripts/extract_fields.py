#!/usr/bin/env python3
"""
extract_fields.py - 客户无关的字段抽取引擎（由客户档案驱动）

职责：把「任意客户的报价文件」（XLSX/CSV/PDF/DOCX/TXT）抽成 **规范键 → 值** 的字典，
      供 main.py 映射成 ERP 字段。

这里**不允许出现任何客户的列名** —— 列名一律来自 `customers/<id>.yaml` 的 `fields:`。
新增客户不需要改这个文件；只有当客户的文件布局是全新形态时，才需要在这里加一个
布局策略（并在 SKILL.md「布局策略」里登记）。

核心是 `auto_pairs` 策略（一条规则覆盖大多数报价单），对每个「标签单元格」：

    * 先看**同一列下方**一格：非空、且本身不是标签 → 就用它（表单式：标签行 + 数据行）
    * 再看**同一行右侧**最近的「非空且非标签」单元格（横向键值对式）
    * 所在行是「密集标签行」（≥ min_label_row_hits 个标签）时优先取下方，
      否则优先取右侧 —— 这两种写法在实际报价单里经常混在同一张表里。

实测（igl `PUZZLE COLOUR DISNEY.xlsx`）：这一条规则同时吃下了
`Imprint|Autumn`、`Requested By|(空)|Christine`、`Group/Series Number|(空)|6366`
（横向）与 `Pub Month`/数据行、`Size…`/下一列（纵向）两种布局。

命令行（调试/审计用）：
    uv run ./scripts/extract_fields.py <文件> --customer igl [--json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from customer_profile import CustomerProfile, load_profile, norm_label, resolve_profile  # noqa: E402

# 特殊键：透传给 main.py 的非规范数据（双下划线前缀，不会被当成规范键）
QUANTITY_TIER_KEY = "__quantity_tiers__"
RAW_LABELS_KEY = "__raw_labels__"
UNMATCHED_KEY = "__unmatched_labels__"
TEXT_BLOCKS_KEY = "__text_blocks__"
LAYOUT_KEY = "__layout__"

ALL_STRATEGIES = ("auto_pairs", "column_pairs", "shifted_pairs", "section_pairs")

# 说明：表头即标签的整列表格（旧文档里的「纵向格式」）不需要单独策略 ——
# 引擎统一用 header=None 读表，真正的表头会落在第 0 行，
# 由 auto_pairs 按「密集标签行 → 取其下方单元格」处理。


# ── 结果对象 ─────────────────────────────────────────────────────────────
@dataclass
class CustomerData:
    """一份客户文件抽出来的全部信息。"""

    values: dict = field(default_factory=dict)            # 规范键 → 值(str)
    raw_labels: dict = field(default_factory=dict)        # 原始标签文本 → 值
    quantity_tiers: list = field(default_factory=list)    # 数量档位（CPSL1~10 来源）
    text_blocks: list = field(default_factory=list)       # 全部文本（供 agent 写 CPMS / 校准）
    unmatched: list = field(default_factory=list)         # 认不出来的标签（校准用）
    extras_found: list = field(default_factory=list)      # 命中的「已知附加信息」→ CPMS 素材
    scanned: dict = field(default_factory=dict)           # rules.scan 正则扫描到的值
    scan_notes: list = field(default_factory=list)        # 扫描过程中的「为什么没落值」（审计用）
    layout: str = ""                                      # 实际命中的抽取路径（审计用）
    profile_id: str = ""
    # ── 输入单元（一份报价 = 单文件 / 文件夹 / 压缩包，2026-10-20）──────────
    unit_kind: str = "file"                               # file | folder | archive
    unit_label: str = ""                                  # 人看的说明（「文件夹（3 个参考文件）」）
    members: list = field(default_factory=list)           # 这份报价由哪些文件组成
    sources: dict = field(default_factory=dict)           # 规范键 → 出自哪个文件（溯源）
    conflicts: list = field(default_factory=list)         # 同键不同值（交付前必须核对）

    def get(self, key: str, default: Any = "") -> Any:
        """先看分区/平铺抽出来的值，再退回 rules.scan 正则扫描出来的值。

        注意必须用 `values.get(key)`（缺省 None）而不是 `values.get(key, default)`：
        后者在缺键时会返回 ``''``，于是 `is None` 判不成立、**scanned 永远读不到** ——
        实测踩过：KHDH / FSCMC 明明扫到了值，结果全是空串。
        """
        value = self.values.get(key)
        if value is None:
            value = self.scanned.get(key)
        if value is None:
            return default
        return value

    def has(self, key: str) -> bool:
        return bool(str(self.get(key, "") or "").strip())

    def as_dict(self) -> dict:
        out = {
            "profile_id": self.profile_id,
            "layout": self.layout,
            "values": self.values,
            "scanned": self.scanned,
            "scan_notes": self.scan_notes,
            "quantity_tiers": self.quantity_tiers,
            "unmatched_labels": self.unmatched,
            "extras_found": self.extras_found,
        }
        # 单文件保持老结构（下游/测试都按这 8 个键读），单元才追加溯源信息
        if self.unit_kind != "file" or self.members:
            out["unit"] = {
                "kind": self.unit_kind,
                "label": self.unit_label,
                "members": self.members,
            }
            out["sources"] = self.sources
            out["conflicts"] = self.conflicts
        return out

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, indent=2)


# ── 通用工具 ─────────────────────────────────────────────────────────────
def _clean(cell: Any) -> str:
    """单元格 → 干净字符串（NaN → 空串）。"""
    try:
        import pandas as pd
        if cell is None or (isinstance(cell, float) and pd.isna(cell)):
            return ""
    except ImportError:  # pragma: no cover
        if cell is None:
            return ""
    s = str(cell).replace("\u00a0", " ").replace("\u3000", " ")
    s = re.sub(r"[ \t]+", " ", s).strip()
    return "" if s.lower() in ("nan", "nat", "none") else s


def _rows_to_matrix(df) -> list:
    return [[_clean(v) for v in row] for row in df.itertuples(index=False, name=None)]


# ── 数量档位 ─────────────────────────────────────────────────────────────
def extract_quantity_tiers(rows: list, profile: CustomerProfile, logger=None) -> list:
    """从表格里抽数量档位（CPSL1~CPSL10 的取值来源）。

    判定刻意做严（宁可漏判不可误判），写法由档案 `rules.quantity_tiers` 控制：
      * k_suffix: `3k` / `7.5k` 这种带后缀的写法 —— 同一行 ≥2 个即认定；
      * plain:    裸数字 —— 同一行 ≥2 个、列方向连续，且前面标签命中 plain_label_hint。
    多组档位行取「最长的一组」，值去重并保持首次出现顺序。
    """
    def log(msg: str, level: str = "info"):
        if logger is not None:
            getattr(logger, level, logger.info)(msg)

    k_re = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*[kK]\s*$")
    plain_re = re.compile(r"^\s*(\d{3,7})\s*$")
    hint_re = re.compile(
        str(profile.rule("quantity_tiers.plain_label_hint", "quantity|qty|total|cost|数量")),
        re.IGNORECASE,
    )

    use_k = bool(profile.rule("quantity_tiers.k_suffix", True))
    use_plain = bool(profile.rule("quantity_tiers.plain", True))
    max_tiers = int(profile.rule("quantity_tiers.max_tiers", 10))

    def parse_k(cell: str) -> Optional[int]:
        m = k_re.match(cell)
        return int(round(float(m.group(1)) * 1000)) if m else None

    def parse_plain(cell: str) -> Optional[int]:
        m = plain_re.match(cell)
        return int(m.group(1)) if m else None

    def row_run(cells: list, parser) -> list:
        hits = [(i, parser(c)) for i, c in enumerate(cells)]
        hits = [(i, v) for i, v in hits if v is not None]
        if len(hits) < 2:
            return []
        first, last = hits[0][0], hits[-1][0]
        if last - first + 1 != len(hits):
            return []                      # 中间夹着别的非空单元格 → 不是档位行
        if not any(cells[:first]):         # 档位前面必须有标签
            return []
        return [v for _, v in hits]

    best: list = []
    for cells in rows:
        run = []
        if use_k:
            run = row_run(cells, parse_k)
        if not run and use_plain:
            label = next((c for c in cells if c), "")
            if hint_re.search(label):
                run = row_run(cells, parse_plain)
        if len(run) > len(best):
            best = run

    tiers: list = []
    for qty in best:
        if qty not in tiers:
            tiers.append(qty)

    if len(tiers) > max_tiers:
        log(f"数量档位 {len(tiers)} 个，超过上限 {max_tiers}，只取前 {max_tiers} 个", "warn")
        tiers = tiers[:max_tiers]

    if tiers:
        log(f"数量档位识别: {tiers}")
    else:
        log("数量档位识别: 未识别到数量档位行（CPSL1~CPSL10 将保持 0）", "warn")
    return tiers


# ── 分区式表单（section_pairs）────────────────────────────────────────────
def _label_matches(profile: CustomerProfile, alias: str, cell: str) -> bool:
    """声明标签与单元格「整词包含」匹配（客户常把双语标签挤在一格）。"""
    a, t = norm_label(alias), norm_label(cell)
    if not a or not t:
        return False
    if a == t:
        return True
    return bool(re.search(r"(?<![a-z0-9])" + re.escape(a) + r"s?(?![a-z0-9])", t))


def _values_to_the_right(cells: list, start: int, profile: CustomerProfile,
                         mode: str = "first") -> str:
    """取标签右边的值。

    mode="first"：第一个非空格（跳过空格，也跳过「正好等于某个标签」的格）——
                  排期行就是这种写法（值出现在很右边的 G 列）。
    mode="run"  ：**从值列起连续**取，遇到第一个空格即停，再拼起来 ——
                  材质/印刷/尺寸/装订这类行是「C 列起一整块」，
                  右边的 FSC / 厚度列（I/J/K）属于另一组信息，不能混进来
                  （否则封面纸为空的行会被填成 `FSC mix`）。
                  必须连续取的典型：`145 | x | 210 | mm | portrait`。
    """
    if mode == "run":
        picked = []
        for j in range(start, len(cells)):
            cell = cells[j]
            if not cell:
                break
            if profile.match_label_exact(cell):     # 正好等于标签 → 不是值
                break
            picked.append(cell)
        return " ".join(picked)

    for j in range(start, len(cells)):
        cell = cells[j]
        if not cell:
            continue
        if profile.match_label_exact(cell):
            continue
        return cell
    return ""


def _extract_sections(rows: list, profile: CustomerProfile, logger=None) -> tuple:
    """按档案 `sections:` 走一遍「分区表单」（NGV 的 enquiry 表就是这种）。

    表格形态（NGV 四份样本实测完全一致）：

        A 列 = 分区名（双语挤一格：`finishing\\nVerarbeitung`），向下继承
        B 列 = 标签（`extent\\nUmfang` / `paper\\nPapier` / `printing\\nDruck`）
        C 列起 = 值

    **按行位置对齐**：分区从它那一行开始，第 i 行对应 entries[i]。
    这种方式对 NGV 这种固定模板最稳，而且天生处理两件麻烦事：

      * 同一分区里标签会重复（content 下 paper ×2、cover 下 paper/printing/
        finishing 各 ×2 —— 封面与护封），位置对齐不需要靠标签区分；
      * 某些行的值是空的（环衬、护封、sticker 常年为空），按序「消费」会在
        空行处错位（实测：会把联系人写进 notes）；位置对齐不会。

    entries[i] 两种形态：
      - `section_value`：该行 B 列本身是值（产品名 / 数量 / 款数 / specials / notes）
      - `label` + `key`：B 列是标签，值取右侧；声明的 label 同时用作**断言** ——
        对不上就打 WARN 提示「表单结构可能变了」，避免静默取错值。

    分区名只认档案里**已声明**的；A 列其它文字（`ends` / `Vor-/Nachsatz` /
    `jacket` / `Schutzumschlag`）是该行的子说明，不切换分区 —— 这正是 NGV 表
    的实际语义（环衬/护封仍属于内文/封面分区，用行序区分）。
    """
    def log(msg: str, level: str = "info"):
        if logger is not None:
            getattr(logger, level, logger.info)(msg)

    sections = profile.sections or []
    if not sections:
        return {}, {}, "", []

    section_col = int(profile.rule("layout.section_col", 0))
    label_col = int(profile.rule("layout.label_col", 1))
    first_value_col = int(profile.rule("layout.first_value_col", label_col + 1))

    values: dict = {}
    raw_labels: dict = {}
    unmatched: list = []
    current = None
    current_no = -1
    start_row = -1
    gained = 0
    consumed_rows = 0

    for r, cells in enumerate(rows):
        # ① 分区切换（只有已声明的分区名才算）
        if section_col < len(cells) and cells[section_col]:
            hit = profile.match_section(cells[section_col])
            if hit is not None:
                no = next(i for i, s in enumerate(sections) if s is hit)
                if no != current_no:
                    current, current_no, start_row = hit, no, r
        if current is None:
            continue

        entries = current.get("entries") or []
        offset = r - start_row
        if offset >= len(entries):          # 分区声明的行数已用完 —— 多余行忽略
            continue
        entry = entries[offset]
        label_cell = cells[label_col] if label_col < len(cells) else ""
        consumed_rows += 1

        if "section_value" in entry:
            key = entry.get("section_value")
            if key and label_cell:
                values.setdefault(key, label_cell)      # 先到先得，不覆盖
                gained += 1
            continue

        # 标签行：先断言声明的标签与表里的一致
        if not _label_matches(profile, entry.get("label", ""), label_cell):
            log(f"  分区「{(current.get('names') or ['?'])[0]}」第 {r + 1} 行期望标签 "
                f"{entry.get('label')!r}，实际是 {label_cell!r} —— "
                f"表单结构可能已变化，该字段按声明位置取值，请核对", "warn")
            if label_cell:
                unmatched.append(label_cell)
            continue

        mode = str(entry.get("value_mode", "first"))
        value = _values_to_the_right(cells, first_value_col, profile, mode)
        key = entry.get("key")
        if key and value:
            values.setdefault(key, value)
            raw_labels.setdefault(label_cell, value)
            gained += 1
        elif label_cell:
            unmatched.append(label_cell)

    layout = f"section_pairs×{gained}" if gained else ""
    if not gained:
        log("分区表未命中任何条目 —— 检查档案 sections: 与 rules.layout 的列号", "warn")
    return values, raw_labels, layout, unmatched


# ── 全表正则扫描（rules.scan）────────────────────────────────────────────
def scan_values(texts: list, profile: CustomerProfile, logger=None,
                notes: list | None = None) -> dict:
    """按档案 `rules.scan` 在**所有文本**里跑正则，取第一个命中的捕获组。

    适用场景：值不挂在任何标签上，只在某处出现一次 ——
      * NGV 页脚的 `PR-00093270`（我方编号）
      * 纸张行里的 `FSC Mix`（FSC 类别）
      * 文件名里的 `香港单号 CU26072301`（客户单号）

    档案写法：

        rules:
          scan:
            - { key: our_ref, pattern: 'PR-\\\\d{5,}' }
            - { key: fsc,     pattern: 'FSC\\\\s*(?:Mix(?:\\\\s*\\\\d+%)?|100%|Recycled\\\\s*\\\\d*%?)',
                normalize: title, unique: true }

    捕获组可选：有捕获组取组 1，没有就取整个匹配。

    `unique: true`（业务确认 2026-09-24）—— **不唯一就留空**：
    全文扫描该正则的**所有**命中，去重后只剩一个才落值；出现两个及以上不同取值时
    不落值（交给档案固定值/留空）并把原因写进 `notes`。理由：多材质混用时
    「这个字段该填哪一个」推不出来（实测 CU26072301 的 FSC 同时有 Recycled / Mix /
    100%，权威 ERP 导出里 FSCMC 是**空的**）。猜第一个命中就是在造错值。
    """
    entries = profile.rule("scan", []) or []
    if not entries:
        return {}

    haystack = "\n".join(str(t) for t in texts or [] if t)
    out: dict = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        pattern = entry.get("pattern")
        if not key or not pattern:
            continue
        try:
            found = re.findall(str(pattern), haystack, re.IGNORECASE)
        except re.error:
            if logger is not None:
                logger.warn(f"rules.scan 正则非法，已跳过: {key} / {pattern}")
            continue
        # findall 有捕获组时返回组（多个组则为元组）——统一取第一个组/整体
        matches = []
        for f in found:
            if isinstance(f, tuple):
                f = next((x for x in f if x), "")
            matches.append(str(f).strip())
        matches = [m for m in matches if m]
        if not matches:
            continue

        mode = str(entry.get("normalize", "")).lower()

        def _norm(v: str) -> str:
            if mode == "title":
                return v.title()
            if mode == "upper":
                return v.upper()
            if mode == "lower":
                return v.lower()
            return v

        if entry.get("unique"):
            distinct: list = []

            def _dedupe_key(v: str) -> str:
                """「不唯一」的判等口径。

                `unique_ignore_separators: true` 时把空格/连字符/短破折号一律当空格 ——
                实测（CU26051601-01 / CU26070802）：同一个 FSC 值客户会写成
                `FSC Mix`、`FSC mix`、`FSC-Mix` 三种，旧实现按字面判等会把它当成
                「2 个不同取值」而误留空；反过来只认空格分隔时又会把真正的
                `FSC-Mix` 混用漏掉、错落成单一取值。两种错都在真单上出现过。
                """
                s = v.strip().lower()
                if entry.get("unique_ignore_separators"):
                    s = re.sub(r"[\s\-–—_]+", " ", s)
                return s

            keys: list = []
            for m in matches:
                n = _norm(m)
                k = _dedupe_key(n)
                if k not in keys:
                    keys.append(k)
                    distinct.append(n)
            if len(distinct) > 1:
                msg = (f"rules.scan: {key} 在文件里出现 {len(distinct)} 个不同取值 "
                       f"{distinct} → 按档案 unique: true **不落值**（不唯一就留空，业务确认 2026-09-24）")
                if logger is not None:
                    logger.warn("  " + msg)
                if notes is not None:
                    notes.append(msg)
                continue
            value = distinct[0]
        else:
            value = _norm(matches[0])

        if logger is not None:
            logger.info(f"  正则扫描: {key} = {value}（命中 /{pattern}/）")
        out[key] = value
    return out


# ── 表格抽取 ─────────────────────────────────────────────────────────────
def _first_value_to_the_right(cells: list, start: int, profile: CustomerProfile) -> str:
    for j in range(start + 1, len(cells)):
        cell = cells[j]
        if not cell:
            continue
        if profile.match_label(cell):
            continue
        return cell
    return ""


def _extract_rows(rows: list, profile: CustomerProfile, logger=None) -> tuple:
    """对一张表的二维单元格矩阵跑布局策略，返回 (values, raw_labels, layout, unmatched)。

    策略按档案 `rules.layout.strategies` 的顺序执行；**先到先得**（不覆盖已有值），
    这样多个策略可以互补（如 section_pairs 抓分区表单、auto_pairs 抓平铺键值对）。
    """
    def log(msg: str, level: str = "info"):
        if logger is not None:
            getattr(logger, level, logger.info)(msg)

    values: dict = {}
    raw_labels: dict = {}
    unmatched: list = []
    used: list = []

    strategies = profile.rule("layout.strategies", list(ALL_STRATEGIES)) or list(ALL_STRATEGIES)
    min_hits = int(profile.rule("layout.min_label_row_hits", 2))
    label_col = int(profile.rule("layout.label_col", 1))
    value_col = int(profile.rule("layout.value_col", 2))

    def assign(canonical: str, value: str, label_text: str = ""):
        # quantity_tiers 是特殊键：档位值由 extract_quantity_tiers() 专门识别，
        # 这里不要把「档位行标签右边那一格」当成它的值（那只是第一档而已）。
        if canonical == "quantity_tiers":
            return False
        if not canonical or not str(value).strip():
            return False
        if values.get(canonical, "").strip():        # 先到先得，不覆盖
            return False
        values[canonical] = str(value).strip()
        if label_text:
            raw_labels.setdefault(label_text, str(value).strip())
        return True

    # ── 策略 0：section_pairs（分区表单：分区列 + 标签列 + 值）──────────
    # 这是最「具体」的策略，所以放最前；档案没写 sections: 时自动跳过。
    if "section_pairs" in strategies and profile.sections:
        sec_values, sec_labels, sec_layout, sec_unmatched = _extract_sections(
            rows, profile, logger)
        gained = 0
        for key, value in sec_values.items():
            if not values.get(key, "").strip():
                values[key] = value
                gained += 1
        for key, value in sec_labels.items():
            raw_labels.setdefault(key, value)
        unmatched.extend(sec_unmatched)
        if gained:
            used.append(sec_layout or f"section_pairs×{gained}")
        elif sec_values:
            used.append(f"section_pairs×0")

    # ── 策略 1：auto_pairs（平铺键值对主策略）──────────────────
    if "auto_pairs" in strategies:
        gained = 0
        for r, cells in enumerate(rows):
            matched = [(c, profile.match_label(cell)) for c, cell in enumerate(cells)]
            matched = [(c, k) for c, k in matched if k]
            if not matched:
                continue
            dense = len(matched) >= min_hits
            below = rows[r + 1] if r + 1 < len(rows) else []

            for c, canonical in matched:
                label_text = cells[c]
                got = ""

                below_cell = below[c] if c < len(below) else ""
                below_ok = bool(below_cell) and not profile.match_label(below_cell)
                right = _first_value_to_the_right(cells, c, profile)

                if dense and below_ok:
                    got = below_cell
                elif right:
                    got = right
                elif below_ok:
                    got = below_cell

                if assign(canonical, got, label_text):
                    gained += 1
        if gained:
            used.append(f"auto_pairs×{gained}")

    # ── 策略 2：column_pairs（固定列：标签列 + 值列）───────────
    if "column_pairs" in strategies:
        gained = 0
        for cells in rows:
            if label_col >= len(cells):
                continue
            canonical = profile.match_label(cells[label_col])
            if not canonical:
                continue
            value = cells[value_col] if value_col < len(cells) else ""
            if value and not profile.match_label(value):
                if assign(canonical, value, cells[label_col]):
                    gained += 1
        if gained:
            used.append(f"column_pairs×{gained}")

    # ── 策略 3：shifted_pairs（只向右扫，不看下方）─────────────
    if "shifted_pairs" in strategies:
        gained = 0
        for cells in rows:
            for c, cell in enumerate(cells):
                if not cell:
                    continue
                canonical = profile.match_label(cell)
                if not canonical:
                    continue
                right = _first_value_to_the_right(cells, c, profile)
                if right and assign(canonical, right, cell):
                    gained += 1
        if gained:
            used.append(f"shifted_pairs×{gained}")

    return values, raw_labels, "+".join(used), unmatched


def extract_from_workbook(file_path: str, profile: CustomerProfile, logger=None,
                          filename: str = "") -> CustomerData:
    """XLSX/XLS/CSV → CustomerData（遍历所有工作表，取命中最多的一张为主）。"""
    import pandas as pd

    def log(msg: str, level: str = "info"):
        if logger is not None:
            getattr(logger, level, logger.info)(msg)

    path = Path(file_path)
    if path.suffix.lower() == ".csv":
        sheets = {"(csv)": pd.read_csv(path, header=None, dtype=str)}
    else:
        sheets = pd.read_excel(path, sheet_name=None, header=None, dtype=str)

    qty_mode = str(profile.rule("quantity.mode", "tiers")).strip().lower()
    wants_tiers = qty_mode != "single"

    per_sheet = []
    for name, df in sheets.items():
        if df is None or df.empty:
            continue
        rows = _rows_to_matrix(df)
        values, raw_labels, layout, unmatched = _extract_rows(rows, profile, logger)
        # 档位识别先不逐表打日志（会在错误的表上报 WARN），选定主表后统一打一次
        tiers = extract_quantity_tiers(rows, profile, logger=None) if wants_tiers else []
        blocks = [cell for row in rows for cell in row if cell]
        # 整行拼接：把同一行的非空格再拼一份进扫描 haystack。
        # 单格 haystack 本身是按换行拼的，`\s` 能跨行，所以 `210` / `mm side`
        # 挨着两格时已经能认；这一份是为了**隔了空格**的写法也稳
        # （`145 | (空) | 210 | mm | portrait` 这类）。
        row_texts = [" ".join(c for c in row if c) for row in rows]
        row_texts = [t for t in row_texts if t]
        per_sheet.append({
            "name": str(name), "rows": rows, "values": values,
            "raw_labels": raw_labels, "layout": layout, "tiers": tiers, "blocks": blocks,
            "unmatched": unmatched, "row_texts": row_texts,
        })

    if not per_sheet:
        raise ValueError("文件为空：没有任何可读的工作表/行")

    per_sheet.sort(key=lambda s: len(s["values"]), reverse=True)
    primary = per_sheet[0]
    log(f"主工作表: {primary['name']}（识别 {len(primary['values'])} 个规范键，"
        f"布局 {primary['layout'] or '无命中'}）")

    values = dict(primary["values"])
    raw_labels = dict(primary["raw_labels"])
    unmatched = list(primary.get("unmatched") or [])
    if len(per_sheet) > 1:
        for extra in per_sheet[1:]:
            filled = [k for k, v in extra["values"].items() if not values.get(k, "").strip()]
            for k in filled:
                values[k] = extra["values"][k]
            for k, v in extra["raw_labels"].items():
                raw_labels.setdefault(k, v)
            unmatched.extend(extra.get("unmatched") or [])
            if filled:
                log(f"  从工作表 {extra['name']} 补到 {len(filled)} 个字段: {filled}")

    tiers = primary["tiers"] or next((s["tiers"] for s in per_sheet if s["tiers"]), [])
    if not wants_tiers:
        log(f"数量档位: 档案 quantity.mode=single —— 客户只给一个总数，"
            f"不识别档位（数量落 CPSL1）")
    elif tiers:
        log(f"数量档位识别: {tiers}")
    else:
        log("数量档位识别: 未识别到数量档位行（CPSL1~CPSL10 将保持 0）", "warn")
    blocks = [b for s in per_sheet for b in s["blocks"]]

    # 全表正则扫描：有些值不挂在任何标签上，只在某个角落出现一次
    # （NGV 的 PR-00093270 在页脚、FSC Mix 在纸张行、香港单号在文件名），
    # 这类用档案 rules.scan 声明「规范键 + 正则」，比硬塞进分区表可读得多。
    scan_hay = blocks + [t for s in per_sheet for t in s.get("row_texts") or []]
    scan_notes: list = []
    scanned = scan_values(scan_hay + ([filename] if filename else []), profile, logger,
                          notes=scan_notes)

    # 认不出的标签统计：已登记的纯标签（ignored_labels）不算「认不出」
    unmatched = [lab for lab in set(unmatched)
                 if not profile.match_label(lab) and not profile.is_ignored_label(lab)]
    unmatched += [lab for lab in raw_labels
                  if not profile.match_label(lab) and not profile.is_ignored_label(lab)]
    data = CustomerData(
        values=values,
        raw_labels=raw_labels,
        quantity_tiers=tiers,
        text_blocks=blocks,
        unmatched=sorted(set(unmatched)),
        extras_found=sorted({b for b in blocks
                             if profile.is_known_extra(b)
                             and not profile.is_ignored_label(b)}),
        scanned=scanned,
        scan_notes=scan_notes,
        layout=primary["layout"],
        profile_id=profile.id,
    )
    return data


# ── 文本类文档抽取 ───────────────────────────────────────────────────────
def read_text_document(file_path: str) -> str:
    """PDF / DOCX / TXT → 纯文本。"""
    path = Path(file_path)
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        import fitz
        with fitz.open(str(path)) as doc:
            return "\n".join(page.get_text() for page in doc)
    if suffix == ".docx":
        from docx import Document
        doc = Document(str(path))
        parts = [p.text for p in doc.paragraphs]
        for table in doc.tables:                       # 表格类报价单常藏在 docx 表格里
            for row in table.rows:
                parts.append(" | ".join(cell.text for cell in row.cells))
        return "\n".join(parts)
    return path.read_text(encoding="utf-8", errors="ignore")


def extract_from_text(file_path: str, profile: CustomerProfile, logger=None,
                      filename: str = "") -> CustomerData:
    """自由文本（PDF/DOCX/TXT）→ CustomerData。

    规则：同一行里「标签 + 分隔符 + 值」优先；标签独自成行时取下一非空行。
    比旧实现（只要行里含 ':' 就整行当键值对）严格得多，但仍然只认档案里的标签。
    """
    def log(msg: str, level: str = "info"):
        if logger is not None:
            getattr(logger, level, logger.info)(msg)

    text = read_text_document(file_path)
    lines = [_clean(line) for line in text.splitlines()]
    values: dict = {}
    raw_labels: dict = {}

    for i, line in enumerate(lines):
        if not line:
            continue
        # 找「标签 + 分隔符(+值)」的切分点：
        #   ① 前缀必须**正好等于**某个标签（精确匹配）。用模糊匹配的话
        #      「Series Nam」这种前缀也算命中，于是「最长前缀」永远是整行、值反而丢了。
        #   ② 命中多个标签时**取最长的那个**（档案里 "Series" 与 "Series Name" 并存时，
        #      后者才是真正的标签，不能被前者截胡）。
        #   ③ 切完还剩内容 → 值在本行；切完没内容 → 值在下一非空行。
        best_split = -1
        for split_at in range(len(line) + 1):
            if profile.match_label_exact(line[:split_at]) and split_at > best_split:
                best_split = split_at

        if best_split < 0:
            continue
        canonical = profile.match_label_exact(line[:best_split])
        end = best_split
        rest = line[end:].strip(" :=\t-|")
        if not rest:
            for nxt in lines[i + 1:i + 4]:
                if nxt:
                    rest = nxt
                    break

        if not canonical or not rest:
            continue
        # `quantity_tiers` 是「多档位行」的合成键，文本模式里没有档位行：
        # 客户写 `Quantity: 30.100cps` 时，这个字符串就是**单一数量**，
        # 交给 main.parse_single_quantity() 解析（德式千分位、`= 总数` 都在那儿处理）。
        if canonical == "quantity_tiers":
            canonical = "quantity"
        # 只拒绝「整行就是一个标签」（密集标签行的下一行还是标签）；
        # 不能用 match_label（整词包含）—— 值里带标签词很常见：
        # `64 pp + cover`（含 cover）、`100 gsm offset paper`（含 paper）、
        # `30.100cps (one title, one language)`（含 title），那样这些值会被整条丢掉。
        rest_is_label = bool(profile.match_label_exact(rest)
                             or profile.match_label_exact(rest.rstrip(" :：=")))
        if not rest_is_label:
            values.setdefault(canonical, rest)
            raw_labels.setdefault(line[:end].strip(" :=\t-|"), rest)

    blocks = [line for line in lines if line]
    if not values:
        log("文本类文件未匹配到任何已知标签 —— 请用 calibrate_customer.py 校准该客户档案", "warn")
    scan_notes = []
    scanned = scan_values(blocks + ([filename] if filename else []), profile, logger,
                          notes=scan_notes)
    unmatched = [lab for lab in raw_labels
                 if not profile.match_label(lab) and not profile.is_ignored_label(lab)]
    return CustomerData(
        values=values,
        raw_labels=raw_labels,
        quantity_tiers=[],
        text_blocks=blocks,
        unmatched=unmatched,
        extras_found=sorted({b for b in blocks
                             if profile.is_known_extra(b)
                             and not profile.is_ignored_label(b)}),
        scanned=scanned,
        scan_notes=scan_notes,
        layout="text_lines",
        profile_id=profile.id,
    )


# ── 统一入口 ─────────────────────────────────────────────────────────────
TABLE_SUFFIXES = (".xlsx", ".xls", ".csv")
TEXT_SUFFIXES = (".pdf", ".docx", ".txt")


def load_customer_data(file_path: str, profile: CustomerProfile, logger=None,
                       filename: str = "") -> CustomerData:
    """按后缀分派到表格/文本抽取。

    filename 单独传（而不是从 file_path 取）是为了支持邮件副档侧的同名文件 ——
    rules.scan 的正则要能扫到文件名里的「香港单号 CU26072301」。
    """
    suffix = Path(file_path).suffix.lower()
    name = filename or Path(file_path).name
    if suffix in TABLE_SUFFIXES:
        return extract_from_workbook(file_path, profile, logger, filename=name)
    if suffix in TEXT_SUFFIXES:
        return extract_from_text(file_path, profile, logger, filename=name)
    raise ValueError(f"不支持的文件格式: {suffix}（支持 {TABLE_SUFFIXES + TEXT_SUFFIXES}）")


# ── CLI ──────────────────────────────────────────────────────────────────
def merge_customer_data(items: list, profile: CustomerProfile, logger=None,
                        unit_kind: str = "folder", unit_label: str = "") -> CustomerData:
    """把「一份报价的多个参考文件」合成**一个** CustomerData。

    业务口径（2026-10-20）：文件夹 / 压缩包里的文件是**同一份报价**的参考材料
    （规格表 + 确认单 + 补充说明 ……），所以字段要合并成一份，而不是一个文件一份。

    合并规则（都要能在产物里看得见）：
      1. **主文件** = 抽到规范键最多的那个；同分时表格类优先，再按文件名排序。
      2. 同一规范键，主文件的值胜出；**其它文件给了不同的值 → 记进 `conflicts`**
         （交付前必须人工核对，不许静默丢）。
      3. `rules.scan`（FSC 这类）**在整个单元上重跑**，而不是各文件各判 ——
         否则「规格表写 FSC-Mix、备注写 FSC Recycled」这种真混用会被单文件视角漏掉。
      4. 文本块按文件分段拼接（`# 文件：xxx`），CPMS 与溯源审计都靠它。

    items: [(文件名, CustomerData), ...]
    """
    def log(msg: str, level: str = "info"):
        if logger is not None:
            getattr(logger, level, logger.info)(msg)

    if not items:
        raise ValueError("merge_customer_data: 空输入")

    table_suffixes = TABLE_SUFFIXES
    ordered = sorted(
        items,
        key=lambda it: (
            -len(it[1].values),                                    # 规范键多的优先
            0 if Path(it[0]).suffix.lower() in table_suffixes else 1,  # 表格类优先
            it[0],                                                  # 再按文件名，保证可复现
        ),
    )
    primary_name = ordered[0][0]
    merged = CustomerData(profile_id=ordered[0][1].profile_id or profile.id,
                          unit_kind=unit_kind, unit_label=unit_label,
                          members=[name for name, _ in items])

    # ① 规范键：主文件优先，冲突留痕
    for name, data in ordered:
        for key, value in (data.values or {}).items():
            val = str(value).strip() if value is not None else ""
            if not val:
                continue
            if key not in merged.values:
                merged.values[key] = value
                merged.sources[key] = name
                continue
            kept = str(merged.values.get(key) or "").strip()
            if _norm_for_compare(kept) != _norm_for_compare(val):
                merged.conflicts.append({
                    "key": key,
                    "kept": kept,
                    "kept_from": merged.sources.get(key, primary_name),
                    "other": val,
                    "other_from": name,
                })

    # ② 数量档位 / 原始标签：第一个非空
    for _, data in ordered:
        if not merged.quantity_tiers and data.quantity_tiers:
            merged.quantity_tiers = list(data.quantity_tiers)
        for label, value in (data.raw_labels or {}).items():
            merged.raw_labels.setdefault(label, value)

    # ③ 文本块：按文件分段（CPMS / 溯源 / 人工核对都靠它）
    for name, data in ordered:
        merged.text_blocks.append(f"# 文件：{name}")
        merged.text_blocks.extend(str(b) for b in (data.text_blocks or []))
    # 文件名也是线索（香港单号常只出现在文件名里）→ 拼进扫描语料
    file_names = [name for name, _ in items]
    merged.text_blocks.extend(f"# 文件名：{n}" for n in file_names)

    # ④ 未识别标签 / 附加信息：合并 + 标明出处
    for name, data in ordered:
        for label in (data.unmatched or []):
            merged.unmatched.append(f"{name}: {label}")
        for extra in (data.extras_found or []):
            if extra not in merged.extras_found:
                merged.extras_found.append(extra)

    # ⑤ 扫描类（unique 键）：整个单元重跑一次
    notes: list = []
    merged.scanned = scan_values(list(merged.text_blocks) + file_names, profile,
                                 logger, notes=notes)
    for name, data in ordered:
        for note in (data.scan_notes or []):
            if note not in notes:
                notes.append(note)
    merged.scan_notes = notes
    merged.layout = f"unit[{unit_kind}]:" + "+".join(
        (d.layout or "unknown") for _, d in ordered)

    if merged.conflicts:
        log(f"  ⚠ 单元内有 {len(merged.conflicts)} 处同键不同值（已保留主文件值，差异记账）:", "warn")
        for c in merged.conflicts:
            log(f"    - {c['key']}: 用 {c['kept']!r}（{c['kept_from']}）"
                f"，另见 {c['other']!r}（{c['other_from']}）", "warn")
    return merged


def _norm_for_compare(value: str) -> str:
    """判断「两个文件是不是给了同一个值」：只在空白/大小写上放宽，不做语义猜测。"""
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def load_customer_unit(unit, profile: CustomerProfile, logger=None) -> CustomerData:
    """按输入单元（单文件 / 文件夹 / 压缩包）抽取数据。

    单文件 = 独一份报价 → 走原来的单文件路径，行为一个字节都不变；
    单元 → 逐个文件抽取后合并（见 merge_customer_data）。
    """
    members = list(getattr(unit, "members", []) or [])
    kind = str(getattr(unit, "kind", "file"))
    if kind == "file" or len(members) <= 1:
        path = members[0] if members else getattr(unit, "path", None)
        return load_customer_data(str(path), profile, logger)

    items = []
    for member in members:
        if logger is not None:
            logger.info(f"  · 参考文件: {member.name}")
        try:
            data = load_customer_data(str(member), profile, logger, filename=member.name)
        except Exception as exc:  # 单个文件读不了不该拖垮整份报价
            if logger is not None:
                logger.warn(f"    读取失败，已跳过: {member.name}（{exc}）")
            continue
        items.append((member.name, data))
    if not items:
        raise ValueError(f"单元里的文件全部读取失败（{len(members)} 个）")
    return merge_customer_data(items, profile, logger, unit_kind=kind,
                               unit_label=str(getattr(unit, "label", "")))


# ── CLI ──────────────────────────────────────────────────────────────────
def main() -> int:
    import warnings
    warnings.filterwarnings("ignore", message=".*is not supported and will be removed.*")

    parser = argparse.ArgumentParser(description="字段抽取（客户档案驱动）")
    parser.add_argument("input_file", help="客户报价文件")
    parser.add_argument("--customer", "-c", default="auto", help="客户 id（默认自动识别）")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON")
    args = parser.parse_args()

    class _Logger:
        def info(self, m, *a):
            print(m)

        def warn(self, m, *a):
            print(f"[WARN] {m}", file=sys.stderr)

        def error(self, m, *a):
            print(f"[ERROR] {m}", file=sys.stderr)

    logger = _Logger()
    path = Path(args.input_file)
    if not path.exists():
        print(f"文件不存在: {path}", file=sys.stderr)
        return 1

    profile, source = resolve_profile(args.customer, texts=[], filename=path.name, logger=logger)
    print(f"档案: {profile.id}（{source}）")
    data = load_customer_data(str(path), profile, logger)

    if args.json:
        print(data.to_json())
        return 0

    print(f"\n布局: {data.layout}")
    print(f"规范键命中 {len(data.values)} 个:")
    for key, value in data.values.items():
        print(f"  {key:20s} = {value}")
    if data.scanned:
        print(f"\n正则扫描命中 {len(data.scanned)} 个:")
        for key, value in data.scanned.items():
            print(f"  {key:20s} = {value}")
    print(f"\n数量档位: {data.quantity_tiers or '(无)'}")
    if data.unmatched:
        print(f"\n未识别的标签 {len(data.unmatched)} 个（校准用）:")
        for lab in data.unmatched:
            print(f"  - {lab}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
