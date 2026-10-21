#!/usr/bin/env python3
"""CPMS 内容溯源审计（严格模式门禁）。

业务口径（2026-10-19）：`CPMS` 只允许写客户文件里出现过的事实。
这个脚本把「写得对不对」变成可机械检查的两件事：

1. **无出处**（`UNTRACED`）：CPMS 里的数字/单位/料号，在客户原始文件里找不到
   —— 极可能是从别处搬来或凭推断补的，**必须删或改**。
2. **漏抄**（`MISSING`）：客户文件里有数字/单位/料号，CPMS 里没写
   —— 可能是真漏，也可能是我们有意不写（如页脚无意义数字），人再判一次。

用法：

    uv run ./scripts/cpms_content_audit.py --cpms <ERP.json> --source <客户原始文件>
    uv run ./scripts/cpms_content_audit.py --cpms <ERP.json> --source <文件> --json

退出码 = 无出处条目数（0 才能交付）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# ── 数字 + 单位 / 料号 的抽取 ────────────────────────────────────────────────
UNIT_ALIASES = {
    "gsm": "g", "克": "g", "g": "g",
    "mm": "mm", "厘米": "cm", "cm": "cm", "丝": "丝",
    "pp": "pp", "p": "pp",
    "kpa": "kpa", "kn/m": "kn/m",
    "色": "色", "颗": "颗", "张": "张", "本": "本", "款": "款",
    "行": "行", "列": "列", "个": "个", "套": "套", "袋": "袋", "箱": "箱",
}
TOKEN_RE = re.compile(
    r"(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>gsm|kpa|kn/m|mm|cm|pp|p|g|丝|色|颗|张|本|款|行|列|个|套|袋|箱)?(?![a-z])",
    re.IGNORECASE,
)
# 料号/编号：字母开头 + ≥2 位数字；排除 `UV/0C` 这类印色写法
CODE_RE = re.compile(r"[A-Z]{2,}[-/]?\d{2,}[A-Z0-9/-]*|\d{3,}#", re.IGNORECASE)

# 需要「同数字 + 兼容单位」才认账的物理单位（其余计数词按数字溯源即可）
PHYSICAL_UNITS = {"g", "mm", "cm", "pp", "kpa", "kn/m", "丝"}


def normalize(text: str) -> str:
    """把各种写法收敛成同一形态，便于子串比对。"""
    s = text.lower()
    s = s.replace("×", "*").replace("x", "*").replace("＊", "*")
    s = re.sub(r"\s+", "", s)
    return s


def tokens(text: str) -> list[tuple[str, str, str]]:
    """→ [(原始片段, 数字, 单位)]，单位已归一。"""
    out: list[tuple[str, str, str]] = []
    for m in TOKEN_RE.finditer(text):
        unit = (m.group("unit") or "").strip().lower()
        unit = UNIT_ALIASES.get(unit, unit)
        out.append((m.group(0).strip(), m.group("num"), unit))
    return out


def codes(text: str) -> list[str]:
    return [c.strip() for c in CODE_RE.findall(text) if re.search(r"\d", c)]


# 灰板 thickness↔克重换算（业务 2026-10-19 拍的规范：灰板一律写 g）。
# 客户文件常写 `boards 2 mm`，CPMS 必须写 `1200g灰板` —— 这不是编数字，
# 是规范要求的换算，所以审计要认（CU26072401 实测：1200g 被判「无出处」）。
GREYBOARD_G_TO_MM = {"600": "1", "900": "1.5", "1200": "2", "1500": "2.5", "1800": "3"}


def _greyboard_equivalent(num: str, unit: str, src_norm: str) -> bool:
    """`1200g` 能不能对上原文里的 `2 mm`（灰板换算表）。"""
    if unit != "g":
        return False
    mm = GREYBOARD_G_TO_MM.get(num)
    if not mm:
        return False
    return bool(re.search(rf"(?<![\d.]){re.escape(mm)}mm", src_norm, re.IGNORECASE))


def traceable(num: str, unit: str, src_norm: str) -> bool:
    """数字能否在客户原文里找到。

    计数词（颗/张/款/行…）是译文，按**数字**溯源即可；
    物理单位（g/mm/pp/kpa/丝）要求同数字 + 兼容单位（`gsm≡g`），
    但允许「数字」和「单位」落在**不同单元格**（如 `vol. 1.25` + `mm bookblock thickness`）。
    灰板 mm→g 的换算按 `GREYBOARD_G_TO_MM` 认账。
    """
    if num not in src_norm:
        return _greyboard_equivalent(num, unit, src_norm)
    if unit not in PHYSICAL_UNITS:
        return True
    for m in TOKEN_RE.finditer(src_norm):
        if m.group("num") != num:
            continue
        su = (m.group("unit") or "").strip().lower()
        su = UNIT_ALIASES.get(su, su)
        if su in (unit, ""):
            return True
    # 单位写在别的单元格里：数字在、单位词在，就算可溯源（交给 `missing` 段提示人复核）
    return unit in src_norm


def audit(cpms: str, source: str) -> dict:
    src_norm = normalize(source)
    cpms_tokens = tokens(cpms)
    untraced = []
    seen = set()
    for raw, num, unit in cpms_tokens:
        key = (num, unit)
        if key in seen:
            continue
        seen.add(key)
        if not traceable(num, unit, src_norm):
            untraced.append({"token": raw, "num": num, "unit": unit})

    untraced_codes = []
    seen_c = set()
    for c in codes(cpms):
        cl = c.lower()
        if cl in seen_c:
            continue
        seen_c.add(cl)
        if cl not in src_norm:
            untraced_codes.append(c)

    # 漏抄：原文里有、CPMS 里没有的数字（只报物理单位，噪音小）
    cpms_nums = {n for _, n, _ in cpms_tokens}
    missing = []
    seen_m = set()
    for raw_tok, num, unit in tokens(source):
        if unit not in PHYSICAL_UNITS or num in cpms_nums or (num, unit) in seen_m:
            continue
        seen_m.add((num, unit))
        missing.append({"token": raw_tok, "num": num, "unit": unit})

    return {
        "untraced": untraced,
        "untraced_codes": untraced_codes,
        "missing": missing,
        "summary": {
            "untraced": len(untraced) + len(untraced_codes),
            "missing": len(missing),
        },
    }


def read_source(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xlsm"}:
        import openpyxl

        wb = openpyxl.load_workbook(path, data_only=True)
        parts: list[str] = [path.name]
        for ws in wb:
            for row in ws.iter_rows():
                for cell in row:
                    if cell.value not in (None, ""):
                        parts.append(str(cell.value))
        return "\n".join(parts)
    if suffix == ".csv":
        return path.read_text(encoding="utf-8", errors="ignore")
    if suffix in {".txt", ".md"}:
        return path.read_text(encoding="utf-8", errors="ignore")
    if suffix == ".docx":
        from docx import Document

        doc = Document(str(path))
        parts = [path.name] + [p.text for p in doc.paragraphs]
        for t in doc.tables:
            for row in t.rows:
                for cell in row.cells:
                    parts.append(cell.text)
        return "\n".join(parts)
    raise SystemExit(f"不支持的文件类型：{suffix}（支持 xlsx/csv/txt/docx）")


def expand_sources(paths: list[str]) -> list[Path]:
    """把 `--source` 展开成真正要读的文件列表：目录 → 目录里所有可读文件。

    业务口径（2026-10-20 起）：一份报价可以是**文件夹 / 压缩包**（规格表单 +
    Titel+Mengen + 补充说明）。溯源审计必须把这些文件当**同一份语料**看，
    否则只喂其中一个文件就会把另一个文件里的事实误报成「无出处」。
    """
    out: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            out.extend(sorted(
                f for f in p.rglob("*")
                if f.is_file() and f.suffix.lower() in {".xlsx", ".xlsm", ".csv", ".txt", ".md", ".docx"}
                and not f.name.endswith(".meta.json")
            ))
        else:
            out.append(p)
    if not out:
        raise SystemExit("--source 展开后没有可读文件")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="CPMS 内容溯源审计（严格模式）")
    ap.add_argument("--cpms", required=True, help="含 CPMS 的 ERP JSON")
    ap.add_argument("--source", required=True, nargs="+",
                    help="客户原始文件（xlsx/csv/txt/docx）或整个单元目录；可给多个")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    try:
        record = json.loads(Path(args.cpms).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"读不了产物 JSON：{args.cpms}（{exc}）", file=sys.stderr)
        return 1
    cpms = record.get("CPMS") or ""
    if not cpms.strip():
        print("CPMS 为空 —— 没有可审计的内容", file=sys.stderr)
        return 1
    try:
        # 一份报价常由「规格表单 + Titel+Mengen」等多个文件组成 → 语料要合起来审，
        # 否则「数量只在 Titel+Mengen 里」会被误报成无出处（LG26090401 实测）。
        source_text = "\n".join(read_source(Path(p)) for p in expand_sources(args.source))
    except OSError as exc:
        print(f"读不了客户原始文件：{exc}", file=sys.stderr)
        return 1
    result = audit(cpms, source_text)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("=" * 66)
        print(f"CPMS 溯源审计：{args.cpms}")
        if result["untraced"] or result["untraced_codes"]:
            print("\n!! 无出处（客户文件里找不到）—— 必须删或改 !!")
            for t in result["untraced"]:
                print(f"  - {t['token']}")
            for c in result["untraced_codes"]:
                print(f"  - {c}（料号/编号）")
        else:
            print("\n✅ 无出处条目：0 —— 每个数字/单位/料号都能在客户文件里找到")
        if result["missing"]:
            print("\n· 客户文件里有、CPMS 未写（人判：真漏 / 有意不写）")
            for t in result["missing"]:
                print(f"  - {t['token']}")
        print(f"\n小结：无出处 {result['summary']['untraced']} / 未写 {result['summary']['missing']}")
    return result["summary"]["untraced"]


if __name__ == "__main__":
    raise SystemExit(main())
