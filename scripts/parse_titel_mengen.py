#!/usr/bin/env python3
"""
parse_titel_mengen.py - 解析「Titel + Mengen（款别 + 数量）」型询价表

为什么单独一个脚本：
    客户（NGV / Naumann & Göbel）除了「单款 enquiry 分区表单」，还会发这种
    **项目级多国多款数量表**（附件名 `Anfrage_Titel+Mengen_<项目号>_<项目名>.xlsx`）。
    它没有分区、没有德英双语标签，而是一张横向表：
        第 1-3 行  项目头（VJ/LJ、项目号 A00130、项目名、涉及渠道 ALDI Süd/HOFER/…）
        第 5 行    表头（Kunde / Land / delivery date / specs / title name German / QTY per title …）
        第 6 行起  每 7 行一个「国家 × 数量档」区块，每区块 7 款

    现有 `section_pairs` 引擎是按标签取值，对这张表一无所获（实测 CD/KD/数量全是 0），
    所以先用这个脚本把**文件里确实写着的东西**抽出来，再谈怎么落 ERP。

输出（JSON，给 agent 和后续 parser 用）：
    header     项目头（项目号/项目名/渠道/版本）
    blocks     区块列表：国家、原表 J/L/M 三列原值、行内数量合计 vs 表内 total QTY
    titles     逐款汇总：中/英名、规格、涉及国家、不同数量档（去重升序）
    warnings   结构性提示（未带表头的列、数量档超过 10 个、总量为 0 的区块…）

用法：
    uv run ./scripts/parse_titel_mengen.py <xlsx> [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, OrderedDict
from pathlib import Path

import openpyxl

# 表头 → 内部键（表头在第 5 行；空表头的列单独记录，绝不猜含义）
HEADER_MAP = {
    "Kunde": "kunde",
    "Land": "land",
    "article country": "article_country",
    "delivery date": "delivery_date",
    "on sale week": "on_sale_week",
    "on sale date": "on_sale_date",
    "PR": "pr",
    "article number": "article_number",
    "specs": "specs",
    "title name German": "title_de",
    "title name English": "title_en",
    "new?": "new",
    "assortment": "assortment",
    "total QTY": "total_qty",
    "QTY per title": "qty_per_title",
    "in stock?": "in_stock",
    "QTY in stock": "qty_in_stock",
    "QTY to print": "qty_to_print",
}
HEADER_ROW_HINT = 5          # 第 5 行是表头（前 3 行是项目头）
MAX_CPSL = 10                # ERP 只有 CPSL1..10
# 同一区块内恒定的「无表头列」：J/L/M（含义待业务确认，只记录原值）
BLOCK_CONST_COLS = {10, 12, 13}


def parse(path: Path) -> dict:
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.worksheets[0]

    # ── 项目头：第 1-3 行 ──────────────────────────────────────────────
    header = {
        "sheet": ws.title,
        "VJ": ws.cell(1, 1).value,
        "LJ": ws.cell(2, 1).value,
        "project_no": ws.cell(2, 2).value,
        "project_name": ws.cell(2, 3).value,
        "version": ws.cell(3, 1).value,
        "channels": [ws.cell(r, 8).value for r in (1, 2, 3) if ws.cell(r, 8).value],
    }

    # ── 表头：第 5 行 → 列号索引；没表头的列记录下来 ──────────────────
    col_of: dict[str, int] = {}
    unlabeled: dict[int, list] = {}
    for c in range(1, ws.max_column + 1):
        label = ws.cell(HEADER_ROW_HINT, c).value
        if label is None or str(label).strip() == "":
            unlabeled[c] = []
            continue
        key = HEADER_MAP.get(str(label).strip())
        if key:
            col_of[key] = c

    rows = []
    carry: dict[str, object] = {}        # 合并单元格：只有左上角有值，其余行要向下继承
    for r in range(HEADER_ROW_HINT + 1, ws.max_row + 1):
        get = lambda key: ws.cell(r, col_of[key]).value if key in col_of else None  # noqa: E731
        title = get("title_de")
        if not title or str(title).strip().lower().startswith("titelbezeichnung"):
            continue                     # 空行 / 重复表头行
        for key in ("land", "kunde", "delivery_date"):
            v = get(key)
            if v not in (None, ""):
                carry[key] = v
        land = carry.get("land") if get("land") in (None, "") else get("land")
        qty = get("qty_per_title")
        rows.append({
            "row": r,
            "kunde": carry.get("kunde"),
            "land": land,
            "delivery_date": get("delivery_date"),
            "pr": get("pr"),
            "article_number": get("article_number"),
            "specs": get("specs"),
            "title_de": str(title).strip(),
            "title_en": get("title_en"),
            "new": get("new"),
            "assortment": get("assortment"),
            "total_qty": get("total_qty"),
            "qty_per_title": qty if isinstance(qty, (int, float)) else 0,
            "qty_to_print": get("qty_to_print"),
        })

    # 没表头的列：把它们的取值记进 blocks（只记录原值，不解释）
    for row in rows:
        for c in unlabeled:
            row[f"col{c}"] = ws.cell(row["row"], c).value

    # ── 区块：同一 (国家, 无表头列…) 连续若干行 ────────────────────────
    blocks: list[dict] = []
    for row in rows:
        key = row.get("land")
        if not blocks or blocks[-1]["land"] != key:
            blocks.append({"land": key, "rows": 0, "qty_sum": 0,
                           "total_qty": None, "titles": [],
                           "unlabeled_cols": {f"col{c}": row.get(f"col{c}") for c in unlabeled
                                              if c in BLOCK_CONST_COLS}})
        b = blocks[-1]
        b["rows"] += 1
        b["qty_sum"] += row["qty_per_title"]
        b["total_qty"] = row["total_qty"] or b["total_qty"]
        b["titles"].append(row["title_de"])
    # （区块内恒定的无表头列已记在 unlabeled_cols；逐行的分组列见 titles 前的行级原值）

    # ── 逐款汇总 ──────────────────────────────────────────────────────
    by_title: "OrderedDict[str, dict]" = OrderedDict()
    for row in rows:
        t = by_title.setdefault(row["title_de"], {
            "title_de": row["title_de"], "title_en": row["title_en"],
            "specs": set(), "countries": [], "quantities": Counter(),
        })
        if row["specs"]:
            t["specs"].add(str(row["specs"]).strip())
        t["countries"].append(row["land"])
        t["quantities"][row["qty_per_title"]] += 1
    titles = []
    for t in by_title.values():
        qtys = sorted(q for q in t["quantities"] if q > 0)
        titles.append({
            "title_de": t["title_de"],
            "title_en": t["title_en"],
            "specs": sorted(t["specs"]),
            "countries": len(set(t["countries"])),
            "total_qty": sum(q * n for q, n in t["quantities"].items()),
            "quantities": qtys,
            "n_quantities": len(qtys),
        })

    # ── 结构性提示（只是提示，不做判断）────────────────────────────────
    warnings = []
    if unlabeled:
        names = ", ".join(f"{openpyxl.utils.get_column_letter(c)}={rows[0].get(f'col{c}')!r}"
                          for c in unlabeled if rows and rows[0].get(f"col{c}") is not None)
        warnings.append(f"这些列没有表头，含义无法从文件确定（需业务确认）：{names}")
    over = [t["title_de"] for t in titles if t["n_quantities"] > MAX_CPSL]
    if over:
        warnings.append(f"{len(over)} 款的量级数 > {MAX_CPSL}（ERP 只有 CPSL1..10）："
                        + "; ".join(f"{t['title_de']} {t['n_quantities']}档" for t in titles
                                    if t["n_quantities"] > MAX_CPSL))
    zero = [b["land"] for b in blocks if b["qty_sum"] == 0]
    if zero:
        warnings.append(f"整块数量为 0（该国家本单不下量）：{', '.join(map(str, zero))}")
    mism = [b["land"] for b in blocks
            if b["total_qty"] not in (None, "") and abs(b["qty_sum"] - b["total_qty"]) > 0]
    if mism:
        warnings.append(f"行内数量之和与表内 total QTY 不一致的区块：{', '.join(map(str, mism))}")

    return {"file": path.name, "header": header, "n_rows": len(rows),
            "blocks": blocks, "titles": titles, "warnings": warnings}


def main() -> None:
    ap = argparse.ArgumentParser(description="解析 Titel+Mengen 多国多款询价表")
    ap.add_argument("xlsx", type=Path)
    ap.add_argument("--json", action="store_true", help="只输出 JSON（默认带人读摘要）")
    args = ap.parse_args()

    data = parse(args.xlsx)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
        return

    h = data["header"]
    print(f"项目：{h['project_no']} {h['project_name']}   渠道 {h['channels']}   版本 {h['version']}")
    print(f"数据行 {data['n_rows']}　区块 {len(data['blocks'])}　款 {len(data['titles'])}\n")
    print("逐款：")
    for t in data["titles"]:
        print(f"  {t['title_de']:<22} {t['specs']}  国家 {t['countries']:2d}  "
              f"合计 {t['total_qty']:>7}  量级 {t['n_quantities']:2d} 档 {t['quantities']}")
    if data["warnings"]:
        print("\n提示：")
        for w in data["warnings"]:
            print(f"  ⚠ {w}")


if __name__ == "__main__":
    main()
