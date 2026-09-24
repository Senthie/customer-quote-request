#!/usr/bin/env python3
"""
calibrate_customer.py - 客户档案校准器（新增客户的第一步）

拿一份真实报价文件，把「这个客户是怎么写字段的」摸清楚，直接告诉你：
  1. 现有档案已经能识别哪些规范键
  2. 哪些标签**认不出来**（这些就是要补进 fields: 的）
  3. 对每个认不出的标签给出**候选规范键建议**（按关键词 + 相似度）
  4. 数量档位有没有识别到、布局策略命中情况
并把建议写成 `customers/_draft_<id>.yaml`，对照着填进正式档案即可。

用法：
    uv run ./scripts/calibrate_customer.py --customer ngv --file <样本文件>
    uv run ./scripts/calibrate_customer.py --customer ngv --file <样本> --layout   # 顺带打印表格布局

工作流（见 customers/README.md）：
    校准 → 填档案 → `main.py --customer <id>` 回归 → status: calibrated
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from customer_profile import (  # noqa: E402
    CANONICAL_KEYS,
    ProfileError,
    load_profile,
    norm_label,
    profiles_dir,
)
from extract_fields import load_customer_data  # noqa: E402

# 关键词 → 规范键（给「认不出的标签」提建议，按顺序取第一个命中的）
KEYWORD_HINTS = [
    (r"imprint|brand|publisher", "imprint"),
    (r"complex", "complexity"),
    (r"request(ed)?\s*by|contact|requestor", "requested_by"),
    (r"pub(lishing|lication)?\s*month|publication|delivery\s*month", "pub_month"),
    (r"series\s*name|series\s*title|^series$", "series_name"),
    (r"number\s*of\s*titles|no\.?\s*of\s*titles|^titles$", "number_of_titles"),
    (r"^group$", "group"),
    (r"^category$", "category"),
    (r"safety", "safety_checks"),
    (r"age", "target_age_group"),
    (r"works?\s*references?|our\s*ref|customer\s*ref|^ref", "works_references"),
    (r"group\s*/\s*series", "group_series_number"),
    (r"isbn", "isbn"),
    (r"project\s*desc|description", "project_description"),
    (r"^size|trim\s*size|finished\s*size|dimension", "size"),
    (r"^format$|binding\s*style", "format"),
    (r"extent|page\s*count|^pages$|pp\b", "extent"),
    (r"text\s*print", "text_printing_spec"),
    (r"text\s*(material|paper)|body\s*(material|paper)", "text_material"),
    (r"cover\s*print", "cover_printing_spec"),
    (r"cover\s*(material|paper)", "cover_material"),
    (r"spine", "spine_width"),
    (r"binding", "binding"),
    (r"finish|lamination|lam\b", "finishes"),
    (r"quantity|qty|total\s*cost|^cost|印数|数量", "quantity_tiers"),
    (r"customer\s*(no|number|ref)|^po\b|order\s*no", "khdh"),
]


def _looks_like_label(cell: str, profile) -> bool:
    """判断一个单元格「像标签」：短、非纯数字、含字母/汉字、不是已知标签。"""
    if not cell or len(cell) > 60:
        return False
    if norm_label(cell) in ("nan", "none", "nat", "null"):
        return False
    if re.fullmatch(r"[\d\s.,%/x×*-]+", cell):          # 纯数字/尺寸/百分比
        return False
    if re.fullmatch(r"\d+(?:[.,]\d+)?\s*[A-Za-z]{0,4}", cell):   # 3k / 14kgs / 127mm
        return False
    if not re.search(r"[A-Za-z\u4e00-\u9fff]", cell):
        return False
    return True


def suggest_canonical(label: str) -> tuple:
    """给一个认不出的标签提候选规范键。返回 (建议, 依据)。"""
    norm = norm_label(label)
    for pattern, canonical in KEYWORD_HINTS:
        if re.search(pattern, norm):
            return canonical, f"关键词 /{pattern}/"
    # 相似度兜底
    pool = {k.replace("_", " "): k for k in CANONICAL_KEYS}
    close = difflib.get_close_matches(norm, list(pool), n=1, cutoff=0.6)
    if close:
        return pool[close[0]], f"相似度 {difflib.SequenceMatcher(None, norm, close[0]).ratio():.2f}"
    return "", "无建议（先确认它的中文语义）"


def collect_label_like_cells(file_path: str, profile) -> list:
    """把文件里所有「像标签」的单元格捞出来，返回 [(列号, 文本)]（去重、保持顺序）。

    列号用于过滤「下拉候选值清单」——客户表格里常夹着整列的分类/年龄段候选值，
    它们也「像标签」，但成列出现，不是字段名。
    文本文件没有列概念，统一给列号 -1。
    """
    path = Path(file_path)
    cells = []
    seen = set()
    if path.suffix.lower() in (".xlsx", ".xls", ".csv"):
        import pandas as pd
        sheets = ({"(csv)": pd.read_csv(path, header=None, dtype=str)}
                  if path.suffix.lower() == ".csv"
                  else pd.read_excel(path, sheet_name=None, header=None, dtype=str))
        for df in sheets.values():
            if df is None:
                continue
            for row in df.itertuples(index=False, name=None):
                for col, cell in enumerate(row):
                    text = str(cell).strip() if cell is not None else ""
                    if _looks_like_label(text, profile) and text not in seen:
                        seen.add(text)
                        cells.append((col, text))
    else:
        from extract_fields import read_text_document
        text = read_text_document(file_path)
        for line in text.splitlines():
            for piece in re.split(r"[:=|\t]{1,2}|\s{2,}", line):
                piece = piece.strip()
                if _looks_like_label(piece, profile) and piece not in seen:
                    seen.add(piece)
                    cells.append((-1, piece))
    return cells


# 同一列里出现这么多个「像标签」的单元格 → 判定为候选值清单（下拉选项），不是字段
LOOKUP_LIST_COLUMN_THRESHOLD = 8


def main() -> int:
    import warnings
    # openpyxl 对客户表格里的数据校验/自定义扩展会刷一堆 UserWarning，与校准无关
    warnings.filterwarnings("ignore", message=".*is not supported and will be removed.*")

    parser = argparse.ArgumentParser(description="客户档案校准器")
    parser.add_argument("--customer", "-c", required=True, help="要校准的客户 id")
    parser.add_argument("--file", "-f", required=True, help="真实报价文件（样本）")
    parser.add_argument("--layout", action="store_true", help="打印表格原始布局（前 25 行）")
    args = parser.parse_args()

    sample = Path(args.file)
    if not sample.exists():
        print(f"样本文件不存在: {sample}", file=sys.stderr)
        return 1

    try:
        profile = load_profile(args.customer)
    except ProfileError as exc:
        print(f"档案不可用: {exc}", file=sys.stderr)
        print(f"提示: 先 cp customers/_template.yaml customers/{args.customer}.yaml", file=sys.stderr)
        return 1

    print("=" * 66)
    print(f"校准客户: {profile.id}（{profile.name}）  状态: {profile.status}")
    print(f"样本文件: {sample.name}")
    print("=" * 66)

    if args.layout and sample.suffix.lower() in (".xlsx", ".xls", ".csv"):
        import pandas as pd
        df = (pd.read_csv(sample, header=None, dtype=str) if sample.suffix.lower() == ".csv"
              else pd.read_excel(sample, header=None, dtype=str))
        pd.set_option("display.width", 240)
        pd.set_option("display.max_colwidth", 28)
        print("\n── 原始布局（header=None，前 25 行）──")
        print(df.head(25).to_string())
        print()

    data = load_customer_data(str(sample), profile)

    print(f"\n── 1. 已识别（布局 {data.layout or '无命中'}）──")
    if data.values:
        for key, value in data.values.items():
            print(f"  ✓ {key:20s} = {value}")
    else:
        print("  （一个都没识别到 —— 档案的 fields 与这个客户的写法完全不匹配）")

    print(f"\n── 2. 数量档位 ──")
    print(f"  {data.quantity_tiers or '未识别到（检查 rules.quantity_tiers 的写法开关）'}")

    # 已被抽出来的「值」不算未识别标签 —— 否则报告里全是 Autumn / Low / Y 这类值，
    # 把真正要补的标签淹掉。
    seen_values = {norm_label(v) for v in list(data.raw_labels.values())
                   + list(data.values.values())}

    candidates = []
    seen = set()
    for col, label in collect_label_like_cells(str(sample), profile):
        if profile.match_label(label):
            continue
        norm = norm_label(label)
        if norm in seen or norm in seen_values:
            continue
        seen.add(norm)
        candidates.append((col, label))

    # 档案 extras: 里登记过的「非 ERP 字段」不算未识别 —— 它们本来就该写进 CPMS
    known_extras = [label for _, label in candidates if profile.is_known_extra(label)]
    candidates = [(col, label) for col, label in candidates
                  if not profile.is_known_extra(label)
                  and not profile.is_ignored_label(label)]

    # 成列出现的（同一列 ≥8 个）判为下拉候选值清单 / 成本明细表，不算字段标签
    from collections import Counter
    col_counts = Counter(col for col, _ in candidates)
    unmatched = [label for col, label in candidates
                 if col_counts[col] < LOOKUP_LIST_COLUMN_THRESHOLD]
    dropped = len(candidates) - len(unmatched)

    print(f"\n── 3. 像标签但认不出的单元格（{len(unmatched)} 个）──")
    if known_extras:
        print(f"  （另有 {len(known_extras)} 个已在档案 extras: 里登记为「非 ERP 字段」"
              f"，会写进 CPMS，不算缺口）")
    if dropped:
        print(f"  （另有 {dropped} 个集中在同一列，判为下拉候选值清单/明细表，已略过）")
    suggestions = {}
    if not unmatched:
        print("  （没有 —— 档案对这个样本已经够用了）")
    for label in unmatched:
        canonical, basis = suggest_canonical(label)
        suggestions.setdefault(canonical or "_uncertain", []).append(label)
        mark = canonical if canonical else "?"
        print(f"  · {label:42s} → 建议 {mark:20s}（{basis}）")

    print(f"\n── 4. 结论 ──")
    if not unmatched:
        print(f"  档案 {profile.id} 已能覆盖该样本的主要字段。")
        print(f"  请跑回归确认字段值正确：uv run ./scripts/main.py {sample} --customer {profile.id}")
    else:
        print(f"  有 {len(unmatched)} 个没进档案。逐个确认它们是**字段标签**还是别的东西")
        print(f"  （值 / 尺寸 / 数量档位 / 明细列都容易被误判成标签）：")
        print(f"    · 确实是字段 → 补进 customers/{profile.id}.yaml 的 fields: 别名列表")
        print(f"    · 都不是      → 说明档案对该样本已够用，可以跑回归了")
        print(f"  改完重跑本脚本，看列表是否收敛。")

    draft_path = profiles_dir() / f"_draft_{profile.id}.yaml"
    with open(draft_path, "w", encoding="utf-8") as f:
        f.write(f"# calibrate_customer.py 于样本 {sample.name} 生成的校准草稿\n")
        f.write(f"# 仅供对照，不参与加载。确认后手工并入 customers/{profile.id}.yaml。\n")
        f.write("# 把下面的标签原文抄进正式档案 fields: 对应规范键的别名列表\n\n")
        f.write("sample_file: " + json.dumps(sample.name, ensure_ascii=False) + "\n")
        f.write("layout_hit: " + json.dumps(data.layout) + "\n")
        f.write("recognized:\n")
        for key, value in data.values.items():
            f.write(f"  {key}: {json.dumps(value, ensure_ascii=False)}\n")
        f.write("quantity_tiers: " + json.dumps(data.quantity_tiers) + "\n")
        f.write("suggested_aliases:\n")
        for canonical, labels in sorted(suggestions.items()):
            f.write(f"  {canonical}:\n")
            for label in labels:
                f.write(f"    - {json.dumps(label, ensure_ascii=False)}\n")
    print(f"\n  草稿已写出: {draft_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
