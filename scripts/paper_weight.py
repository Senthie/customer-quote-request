#!/usr/bin/env python3
"""paper_weight.py —— 客户克重 → 我们常用克重（查 `assets/page_thickness.xlsx`）

业务口径（2026-11-10 森西）
--------------------------
客户询价里有时给的是**我们不常用的克重**（例：`310g单粉`）。真实的做法不是照抄，
也不是问客户，而是：

1. 拿克重去 `page_thickness.xlsx` 的 `faw/g` 列对比 **同一纸型** 的常用克重清单；
2. 不在清单里 → **取最接近的常用克重**（并列时取较大者：宁厚勿薄，书脊算窄会装不下）；
3. 产品描述（CPMS）里写 300g，**同时在备注里写明**「我们用 300g单粉报价，
   因为 310g单粉不常用」—— 让客户知道拿到的价格是按哪个克重报的。

所以本脚本干两件事：**解析**（客户写 `310g单粉` → 纸型 单粉 / 克重 310）和
**换算**（→ 300g，含差额与可直接粘进 CPMS 的备注句）。

表里有什么（2026-11-10 实读，72 行 / 11 个纸型）
------------------------------------------------
    光粉纸 80 95 105 115 128 140 157 200 210 230 250 300 350 400
    书纸   60 70 80 100 120 140 160 180 200 230 250 280 300 350 400
    哑粉   65 80 95 105 115 128 140 157 170 200 250 300
    单粉   210 230 250 300 350 400
    粉灰   250 300 350 400 450 500
    灰板   600 900 1200 1500 1800 2100
    牛油纸 75 95 115 135 180 ；贴纸 200 ；坑纸 1 2
⚠️ **表里没有「双粉」**（C2S 通用双面涂布）—— 客户写 `双粉咭` 时脚本会报「纸型未收录」，
   不猜（光粉/哑粉是两种不同表面，猜错等于报错价）。要支持得先把双粉的常用克重补进表。

用法
----
    uv run ./scripts/paper_weight.py --parse "310g单粉"        # 解析 + 换算 + 备注句
    uv run ./scripts/paper_weight.py --style 单粉 --gsm 310
    uv run ./scripts/paper_weight.py --list 单粉               # 该纸型的常用克重
    uv run ./scripts/paper_weight.py --check-cpms "<CPMS>"      # 体检一段产品描述
    uv run ./scripts/paper_weight.py --check-json <ERP_JSON>    # 体检整份产物
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import openpyxl

SHEET_PATH = (Path(__file__).resolve().parent.parent / "assets" / "page_thickness.xlsx")
SHEET_NAME = "paper_thickness"

# CPMS / 客户原文里的纸型写法 → 表里的 `page_style`
# 只收**能确定**的：单粉=C1S、光粉纸=光面、哑粉=哑面、书纸=woodfree…
# 「双粉 C2S」故意不映射（表里没有这一行，见文件头）。
STYLE_ALIASES = {
    "单粉咭": "单粉", "单粉卡": "单粉", "单粉纸": "单粉", "单粉": "单粉",
    "光粉纸": "光粉纸", "光粉咭": "光粉纸", "光粉": "光粉纸",
    "哑粉纸": "哑粉", "哑粉咭": "哑粉", "哑粉": "哑粉",
    "书纸": "书纸", "道林纸": "书纸", "胶版纸": "书纸",
    "粉灰咭": "粉灰", "粉灰纸": "粉灰", "粉灰": "粉灰",
    "灰板": "灰板", "板纸": "灰板",
    "牛油纸": "牛油纸", "坑纸": "坑纸", "贴纸": "贴纸",
}
# 客户原文里的英文纸名（与 docs/华辉内部中英单词表.txt 一致）
STYLE_ALIASES_EN = {
    "art card c1s": "单粉", "coated one side": "单粉", "art paper": "单粉",
    "gloss": "光粉纸", "glossy art paper": "光粉纸", "cast coated": "光粉纸",
    "matt art": "哑粉", "matte art": "哑粉",
    "woodfree": "书纸", "wood-free": "书纸", "offset paper": "书纸",
    "greyboard": "灰板", "grey board": "灰板", "greyboard/chipboard": "灰板",
}
# 「斤两」类非重量数字不能当克重（页数 80pp / 尺寸 210*210）
_TOKEN_RE = re.compile(r"(?<![0-9.])(?P<n>\d{2,4}(?:\.\d+)?)\s*(?:g|gsm|克)\s*(?P<style>[\u4e00-\u9fff]{1,5})",
                       re.IGNORECASE)

# 纸型后面只允许跟这些后缀（都是同一支纸的另一种叫法）。
# `高松厚单粉` / `光粉不干胶` / `铜版贴纸` 这类**多了限定词就是另一种东西**，
# 后缀不在白名单 → 整条判定未收录，绝不按同族纸型顶替（厚度会跟着错，GD 也就错了）。
_ALLOWED_SUFFIX = ("", "咭", "卡", "纸")
# 就近换算的容差：差距过大说明客户要的可能是另一种东西，宁可报「需人工确认」
MAX_DELTA_ABS = 15.0
MAX_DELTA_RATIO = 0.12


def load_table(path: Path | None = None) -> dict[str, list[tuple[int, float]]]:
    """→ {page_style: [(克重, 厚度mm), …]（按克重升序）}"""
    wb = openpyxl.load_workbook(Path(path or SHEET_PATH), data_only=True)
    ws = wb[SHEET_NAME]
    out: dict[str, list[tuple[int, float]]] = {}
    for r in range(2, ws.max_row + 1):
        style = ws.cell(r, 1).value
        if style is None or str(style).strip() == "":
            continue
        try:
            gsm = int(float(ws.cell(r, 2).value))
            thick = float(ws.cell(r, 3).value)
        except (TypeError, ValueError):
            continue
        out.setdefault(str(style).strip(), []).append((gsm, thick))
    return {k: sorted(v) for k, v in out.items()}


def normalize_style(raw: str) -> str | None:
    """客户/CPMS 里的纸型写法 → 表里的 page_style；认不出 → None（不猜）。

    判定是**整词**，不是「包含」：`单粉咭`/`单粉卡`/`单粉纸` 都是 `单粉`，
    但 `高松厚单粉`、`光粉不干胶`、`铜版贴纸` 都不是 —— 多了限定词就是另一种纸，
    按同族顶替会让厚度/克重一起错。
    """
    s = (raw or "").strip()
    if not s:
        return None
    low = s.lower()
    for en, style in STYLE_ALIASES_EN.items():          # 英文长名优先
        if low == en or low.startswith(en + " "):
            return style
    for alias in sorted(STYLE_ALIASES, key=len, reverse=True):   # 中文按长度优先
        if s.startswith(alias) and s[len(alias):] in _ALLOWED_SUFFIX:
            return STYLE_ALIASES[alias]
    return None


def resolve(style_raw: str, gsm: float, table: dict | None = None) -> dict:
    """克重换算。→ {style, requested, used, substituted, delta, thickness, reason}

    * 命中表内克重 → `substituted=False`，`used == requested`
    * 未命中但纸型认识 → 取最近克重（并列取较大），`substituted=True`
    * 纸型不认识 / 该纸型没有克重数据 → `used=None`，`reason` 说明为什么不猜
    """
    table = table if table is not None else load_table()
    style = normalize_style(style_raw)
    res = {"style": style, "raw_style": (style_raw or "").strip(),
           "requested": gsm, "used": None, "substituted": False,
           "delta": None, "thickness": None, "reason": ""}
    if style is None:
        res["reason"] = f"纸型「{style_raw}」未收录（表里没有这个样式，不猜）"
        return res
    rows = table.get(style) or []
    if not rows:
        res["reason"] = f"纸型「{style}」在 page_thickness 里没有克重数据"
        return res

    exact = [r for r in rows if abs(r[0] - gsm) < 0.51]
    if exact:
        used, thick = exact[0]
        res.update(used=used, thickness=thick, delta=0.0)
        return res

    used, thick = min(rows, key=lambda r: (abs(r[0] - gsm), -r[0]))
    delta = used - gsm
    if abs(delta) > max(MAX_DELTA_ABS, gsm * MAX_DELTA_RATIO):
        res["used"] = None
        res["reason"] = (f"{gsm:g}g{style} 与本表最近克重 {used}g 差 {delta:+g}g（超容差），"
                         f"不自动换算 —— 客户可能要的是另一种纸，需人工确认")
        return res
    res.update(used=used, thickness=thick, substituted=True, delta=delta)
    return res


def note(r: dict, unit: str = "") -> str:
    """可直接写进 CPMS「备注」的一句（未换算 → 空串）。"""
    if not r.get("substituted") or r.get("used") is None:
        return ""
    raw = r.get("raw_style") or r["style"]     # 用客户原文的纸名，别把「单粉卡」改成「单粉」
    return (f"我们用{r['used']}g{raw}{unit}报价，因为客户询价的"
            f"{r['requested']:g}g{raw}{unit}不常用（本品最近常用克重）")


def parse_tokens(text: str) -> list[tuple[float, str]]:
    """从客户原文 / CPMS 里抓 `310g单粉`、`100gsm wood-free` 这类「克重 + 纸型」。"""
    out: list[tuple[float, str]] = []
    for m in _TOKEN_RE.finditer(text or ""):
        n = float(m.group("n"))
        if n > 2500:                     # 灰板最大 2100g；再大是 ISBN/料号之类的假克重
            continue
        out.append((n, m.group("style").strip()))
    # 英文写法：`230 art card C1S` / `100 gsm wood-free white offset paper`
    for m in re.finditer(r"(?<![0-9.])(?P<n>\d{2,4}(?:\.\d+)?)\s*(?:gsm|g)?\s+"
                         r"(?P<style>[A-Za-z][A-Za-z \-]{3,30})", text or "", re.IGNORECASE):
        n = float(m.group("n"))
        if n > 2500:
            continue
        out.append((n, m.group("style").strip(" -")))
    return out


def check(text: str, table: dict | None = None) -> dict:
    """体检一段 CPMS / 客户原文。→ {'results': [ {..resolve.., 'raw': …} ], 'bad': n}"""
    table = table if table is not None else load_table()
    seen: set[tuple[float, str]] = set()
    results = []
    for gsm, raw in parse_tokens(text):
        key = (gsm, raw)
        if key in seen:
            continue
        seen.add(key)
        r = resolve(raw, gsm, table)
        r["raw"] = f"{gsm:g}g{raw}"
        results.append(r)
    bad = sum(1 for r in results if r["used"] is None or r["substituted"])
    return {"results": results, "bad": bad}


def accepted_entry(r: dict) -> dict[str, str]:
    """换算值要进 `customers/<客户>.cpms_accepted.json` —— 否则 `cpms_content_audit`
    会把它当「客户文件里没有的数字」判无出处。返回可直接合并的 {token: 理由}。"""
    if not r.get("substituted") or r.get("used") is None:
        return {}
    token = f"{r['used']}g"
    return {token: (f"克重换算（业务 2026-11-10 口径）：客户询价写 {r['requested']:g}g{r['style']}，"
                    f"该克重不在我们常用清单（assets/page_thickness.xlsx），"
                    f"按最近常用克重 {r['used']}g 报价；CPMS 备注已向客户说明")}


def _print(results: list[dict]) -> int:
    bad = 0
    for r in results:
        if r["used"] is None:
            bad += 1
            print(f"  ⚠ {r['raw']}: {r['reason']}")
        elif r["substituted"]:
            bad += 1
            print(f"  ✎ {r['raw']} → {r['used']}g{r['style']}"
                  f"（差 {r['delta']:+g}g，厚度 {r['thickness']}mm）")
            n = note(r)
            if n:
                print(f"    备注建议：{n}")
        else:
            print(f"  ✓ {r['raw']} 在常用克重内（厚度 {r['thickness']}mm）")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser(description="客户克重 → 我们常用克重（page_thickness）")
    ap.add_argument("--file", help="page_thickness 路径（默认 assets/page_thickness.xlsx）")
    ap.add_argument("--parse", metavar="TEXT", help="解析一句客户原文，如 '310g单粉'")
    ap.add_argument("--style", help="纸型（单粉/光粉纸/哑粉/书纸/粉灰/灰板…）")
    ap.add_argument("--gsm", type=float, help="克重裸数字")
    ap.add_argument("--list", metavar="STYLE", help="列出该纸型的常用克重")
    ap.add_argument("--check-cpms", metavar="TEXT", help="体检一段产品描述")
    ap.add_argument("--check-json", metavar="ERP_JSON", help="体检整份 ERP 产物")
    ap.add_argument("--emit-accepted", action="store_true",
                    help="把换算值输出成 cpms_accepted.json 的条目（无出处审计要用）")
    args = ap.parse_args()

    table = load_table(Path(args.file) if args.file else None)

    def _maybe_accepted(results: list[dict]) -> None:
        if not args.emit_accepted:
            return
        merged: dict[str, str] = {}
        for r in results:
            merged.update(accepted_entry(r))
        if merged:
            print("── cpms_accepted.json 建议条目 ──")
            print(json.dumps(merged, ensure_ascii=False, indent=2))

    if args.list:
        style = normalize_style(args.list)
        if style is None:
            print(f"纸型「{args.list}」未收录")
            return 1
        print(f"{style}: " + " ".join(str(g) for g, _ in table[style]))
        return 0
    if args.parse or (args.style and args.gsm is not None):
        text = args.parse or f"{args.gsm:g}g{args.style}"
        results = [{**resolve(s, g, table), "raw": f"{g:g}g{s}"}
                   for g, s in parse_tokens(text)]
        _print(results)
        _maybe_accepted(results)
        return 0
    if args.check_cpms:
        res = check(args.check_cpms, table)["results"]
        bad = _print(res)
        _maybe_accepted(res)
        print(f"小结：{len(res)} 处克重，{bad} 处需处理（换算或未收录）")
        return 0
    if args.check_json:
        doc = json.loads(Path(args.check_json).read_text(encoding="utf-8"))
        records = doc if isinstance(doc, list) else [doc]
        total_bad = 0
        allres: list[dict] = []
        for i, rec in enumerate(records, 1):
            res = check(rec.get("CPMS") or "", table)["results"]
            print(f"[{i}/{len(records)}] {rec.get('CPMC') or ''}")
            total_bad += _print(res)
            allres += res
        _maybe_accepted(allres)
        return 1 if total_bad else 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
