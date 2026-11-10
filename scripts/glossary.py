#!/usr/bin/env python3
"""glossary.py —— 华辉内部中英单词表（`docs/华辉内部中英单词表.txt`）查询 / 校验

业务口径（2026-11-10 森西）：**对外/进 ERP 的中文用词必须优先采用内部单词表**。
`CPMC` 中文译名、`CPMS` 里的材料/装订/工艺词，能查到的一律照词表写，不许自己另造一个同义词。

词表形态：UTF-8，EN\\tZH，615 行，同一英文可对应多个中文（如 `perfect binding` →
`无线装订` / `无线胶装书`），含义按上下文挑，**两种都算合规**。

用法
----
    uv run ./scripts/glossary.py --lookup "perfect binding"      # 精确（不分大小写）
    uv run ./scripts/glossary.py --search "bind"                 # 前缀/包含模糊
    uv run ./scripts/glossary.py --zh 方脊                        # 反查：这个中文词表里有吗
    uv run ./scripts/glossary.py --check "<一段 CPMS / 品名>"      # 产物用词体检
    uv run ./scripts/glossary.py --dump                          # 全表（EN<TAB>ZH）

`--check` 的两类输出：
  * `EN→ZH`：正文里能识别的英文术语（写英文时该用它对应的中文）
  * `未收录`：正文里的中文词在词表找不到 —— **不是错误**，但要么改词表里的写法，
    要么确认该词属于「产品题材/客户自造名」这类本来就不进词表的东西
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "docs" / "华辉内部中英单词表.txt"

_CJK = re.compile(r"[\u4e00-\u9fff]+")
_SPLIT_ZH = re.compile(r"[/／,，、；;()（）]")


def load(path: Path | None = None) -> dict[str, list[str]]:
    """→ {英文小写键: [中文写法, …]}（保留词表原有顺序与多义项）"""
    p = Path(path or DEFAULT_PATH)
    table: dict[str, list[str]] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        if "\t" not in line:
            continue
        en, zh = line.split("\t", 1)
        en, zh = en.strip(), zh.strip()
        if not en or not zh:
            continue
        table.setdefault(en.lower(), []).append(zh)
    return table


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def lookup(term: str, table: dict[str, list[str]] | None = None) -> list[str]:
    """精确查（不分大小写）。未收录 → []。"""
    table = table if table is not None else load()
    return list(table.get(_norm(term), []))


def search(term: str, table: dict[str, list[str]] | None = None,
           limit: int = 20) -> list[tuple[str, str]]:
    """前缀命中优先，其次包含。→ [(英文原写, 中文)]"""
    table = table if table is not None else load()
    q = _norm(term)
    out: list[tuple[str, str]] = []
    for en, zhs in table.items():
        if en.startswith(q):
            out += [(en, z) for z in zhs]
    for en, zhs in table.items():
        if q in en and not en.startswith(q):
            out += [(en, z) for z in zhs]
    return out[:limit]


def zh_terms(table: dict[str, list[str]] | None = None) -> set[str]:
    """词表里出现过的全部中文片段（按 / ， ( ) 切开，便于比对）。"""
    table = table if table is not None else load()
    out: set[str] = set()
    for zhs in table.values():
        for zh in zhs:
            for part in _SPLIT_ZH.split(zh):
                part = part.strip()
                if part:
                    out.add(part)
    return out


def known_zh(word: str, table: dict[str, list[str]] | None = None) -> str:
    """中文词在词表里的收录程度：`exact` / `fuzzy`（仅子串命中）/ ``（未收录）。

    为什么要区分：`单粉卡` 会在 `单粉咭(C1S)` 上做子串命中，但词表真正认的写法是
    `单粉咭` —— 只看 True/False 会把「自己另造的近义词」当成合规。
    """
    table = table if table is not None else load()
    w = (word or "").strip()
    if not w:
        return ""
    allz = zh_terms(table)
    if w in allz:
        return "exact"
    if any(w in z or (z in w and len(z) >= 2) for z in allz):
        return "fuzzy"
    return ""


# `--check` 跳过这些段：它们是叙述性文字（含客户原话、待确认说明），
# 不是「材料/工艺用词」的规范区；拿材料词表去卡它们只会产生噪声。
_PROSE_LABELS = {"装箱", "备注", "客户目标价", "请拆分", "请选配", "此单半返", "BGMS"}


def _line_value(line: str) -> tuple[str, str]:
    """`标签：值` → (标签, 值)；不是标签行 → ('', 整行)。"""
    m = re.match(r"^[ ]*([^\s：:，。；]{1,12})[：:](.*)$", line)
    return (m.group(1), m.group(2)) if m else ("", line)


def check(text: str, table: dict[str, list[str]] | None = None) -> dict[str, list]:
    """正文用词体检。→ {'en_hits': [(en, [zh])], 'zh_unknown': [词]}

    只体检**材料/工艺值**：段标签行取冒号右边；`装箱`/`备注` 这类叙述段和
    概要行（第一行）整行跳过 —— 那些地方本该出现客户原话和说明文字。
    """
    table = table if table is not None else load()
    lines = (text or "").splitlines()
    body: list[str] = []
    for i, line in enumerate(lines):
        label, value = _line_value(line)
        if i == 0 and "产品规格" in line:
            continue                      # 概要行 = 派生数据，由字段决定
        if label in _PROSE_LABELS:
            continue
        body.append(value if label else line)
    text = "\n".join(body)

    low = text.lower()
    en_hits: list[tuple[str, list[str]]] = []
    for en in sorted(table, key=len, reverse=True):
        if len(en) < 3:
            continue
        if re.search(rf"(?<![a-z]){re.escape(en)}(?![a-z])", low):
            en_hits.append((en, table[en]))
    zh_unknown: list[str] = []
    for chunk in _CJK.findall(text):
        if known_zh(chunk, table) != "exact":
            zh_unknown.append(chunk)
    return {"en_hits": en_hits, "zh_unknown": zh_unknown}


def main() -> int:
    ap = argparse.ArgumentParser(description="华辉内部中英单词表查询 / 校验")
    ap.add_argument("--file", help="词表路径（默认 docs/华辉内部中英单词表.txt）")
    ap.add_argument("--lookup", metavar="EN", help="精确查一个英文术语")
    ap.add_argument("--search", metavar="EN", help="模糊查（前缀/包含）")
    ap.add_argument("--zh", metavar="中文", help="反查：中文词在词表里吗")
    ap.add_argument("--check", metavar="TEXT", help="体检一段中文/中英混排正文")
    ap.add_argument("--check-json", metavar="ERP_JSON",
                    help="体检整份 ERP 产物：逐条记录查 CPMS（交付前一条命令）")
    ap.add_argument("--dump", action="store_true", help="打印全表")
    args = ap.parse_args()

    table = load(Path(args.file) if args.file else None)
    if args.lookup:
        hits = lookup(args.lookup, table)
        print(f"{args.lookup} → {' / '.join(hits) if hits else '未收录'}")
        return 0 if hits else 1
    if args.search:
        for en, zh in search(args.search, table):
            print(f"{en}\t{zh}")
        return 0
    if args.zh:
        level = known_zh(args.zh, table)
        label = {"exact": "收录", "fuzzy": "仅子串命中（建议改用词表写法）"}.get(level, "未收录")
        print(f"{args.zh} → {label}")
        return 0 if level else 1
    if args.check:
        r = check(args.check, table)
        for en, zhs in r["en_hits"]:
            print(f"EN→ZH  {en}\t{' / '.join(zhs)}")
        for w in r["zh_unknown"]:
            print(f"未收录  {w}")
        return 0
    if args.check_json:
        doc = json.loads(Path(args.check_json).read_text(encoding="utf-8"))
        records = doc if isinstance(doc, list) else [doc]
        bad = 0
        for i, rec in enumerate(records, 1):
            r = check(rec.get("CPMS") or "", table)
            print(f"[{i}/{len(records)}] {rec.get('CPMC') or ''}")
            for en, zhs in r["en_hits"]:
                print(f"  EN→ZH  {en}\t{' / '.join(zhs)}")
            if r["zh_unknown"]:
                bad += 1
                for w in r["zh_unknown"]:
                    print(f"  词表外  {w}")
            else:
                print("  ✅ 用词全部在内部词表内")
        return 1 if bad else 0
    if args.dump:
        for en, zhs in table.items():
            for zh in zhs:
                print(f"{en}\t{zh}")
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
