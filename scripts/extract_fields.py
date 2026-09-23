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

ALL_STRATEGIES = ("auto_pairs", "column_pairs", "shifted_pairs")

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
    layout: str = ""                                      # 实际命中的抽取路径（审计用）
    profile_id: str = ""

    def get(self, key: str, default: Any = "") -> Any:
        value = self.values.get(key, default)
        if value is None:
            return default
        return value

    def has(self, key: str) -> bool:
        return bool(str(self.values.get(key, "")).strip())

    def as_dict(self) -> dict:
        return {
            "profile_id": self.profile_id,
            "layout": self.layout,
            "values": self.values,
            "quantity_tiers": self.quantity_tiers,
            "unmatched_labels": self.unmatched,
            "extras_found": self.extras_found,
        }

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
    """对一张表的二维单元格矩阵跑布局策略，返回 (values, raw_labels, layout)。

    策略按档案 `rules.layout.strategies` 的顺序执行；**先到先得**（不覆盖已有值），
    这样多个策略可以互补（如 auto_pairs 抓主体、column_pairs 补固定列）。
    """
    def log(msg: str, level: str = "info"):
        if logger is not None:
            getattr(logger, level, logger.info)(msg)

    values: dict = {}
    raw_labels: dict = {}
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

    # ── 策略 1：auto_pairs（主策略）────────────────────────────
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

    return values, raw_labels, "+".join(used)


def extract_from_workbook(file_path: str, profile: CustomerProfile, logger=None) -> CustomerData:
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

    per_sheet = []
    for name, df in sheets.items():
        if df is None or df.empty:
            continue
        rows = _rows_to_matrix(df)
        values, raw_labels, layout = _extract_rows(rows, profile, logger)
        # 档位识别先不逐表打日志（会在错误的表上报 WARN），选定主表后统一打一次
        tiers = extract_quantity_tiers(rows, profile, logger=None)
        blocks = [cell for row in rows for cell in row if cell]
        per_sheet.append({
            "name": str(name), "rows": rows, "values": values,
            "raw_labels": raw_labels, "layout": layout, "tiers": tiers, "blocks": blocks,
        })

    if not per_sheet:
        raise ValueError("文件为空：没有任何可读的工作表/行")

    per_sheet.sort(key=lambda s: len(s["values"]), reverse=True)
    primary = per_sheet[0]
    log(f"主工作表: {primary['name']}（识别 {len(primary['values'])} 个规范键，"
        f"布局 {primary['layout'] or '无命中'}）")

    values = dict(primary["values"])
    raw_labels = dict(primary["raw_labels"])
    if len(per_sheet) > 1:
        for extra in per_sheet[1:]:
            filled = [k for k, v in extra["values"].items() if not values.get(k, "").strip()]
            for k in filled:
                values[k] = extra["values"][k]
            for k, v in extra["raw_labels"].items():
                raw_labels.setdefault(k, v)
            if filled:
                log(f"  从工作表 {extra['name']} 补到 {len(filled)} 个字段: {filled}")

    tiers = primary["tiers"] or next((s["tiers"] for s in per_sheet if s["tiers"]), [])
    if tiers:
        log(f"数量档位识别: {tiers}")
    else:
        log("数量档位识别: 未识别到数量档位行（CPSL1~CPSL10 将保持 0）", "warn")
    blocks = [b for s in per_sheet for b in s["blocks"]]

    unmatched = [lab for lab in raw_labels if not profile.match_label(lab)]
    data = CustomerData(
        values=values,
        raw_labels=raw_labels,
        quantity_tiers=tiers,
        text_blocks=blocks,
        unmatched=unmatched,
        extras_found=sorted({b for b in blocks if profile.is_known_extra(b)}),
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


def extract_from_text(file_path: str, profile: CustomerProfile, logger=None) -> CustomerData:
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
        if not profile.match_label(rest):
            values.setdefault(canonical, rest)
            raw_labels.setdefault(line[:end].strip(" :=\t-|"), rest)

    blocks = [line for line in lines if line]
    unmatched = [lab for lab in raw_labels if not profile.match_label(lab)]
    if not values:
        log("文本类文件未匹配到任何已知标签 —— 请用 calibrate_customer.py 校准该客户档案", "warn")
    return CustomerData(
        values=values,
        raw_labels=raw_labels,
        quantity_tiers=[],
        text_blocks=blocks,
        unmatched=unmatched,
        extras_found=sorted({b for b in blocks if profile.is_known_extra(b)}),
        layout="text_lines",
        profile_id=profile.id,
    )


# ── 统一入口 ─────────────────────────────────────────────────────────────
TABLE_SUFFIXES = (".xlsx", ".xls", ".csv")
TEXT_SUFFIXES = (".pdf", ".docx", ".txt")


def load_customer_data(file_path: str, profile: CustomerProfile, logger=None) -> CustomerData:
    """按后缀分派到表格/文本抽取。"""
    suffix = Path(file_path).suffix.lower()
    if suffix in TABLE_SUFFIXES:
        return extract_from_workbook(file_path, profile, logger)
    if suffix in TEXT_SUFFIXES:
        return extract_from_text(file_path, profile, logger)
    raise ValueError(f"不支持的文件格式: {suffix}（支持 {TABLE_SUFFIXES + TEXT_SUFFIXES}）")


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
    print(f"\n数量档位: {data.quantity_tiers or '(无)'}")
    if data.unmatched:
        print(f"\n未识别的标签 {len(data.unmatched)} 个（校准用）:")
        for lab in data.unmatched:
            print(f"  - {lab}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
