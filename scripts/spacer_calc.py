#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""spacer_calc.py —— BB 书 / 精装书「夹层(Spacer)」补厚方案计算

为什么需要
----------
客户询价只给成品尺寸，不给书脊厚度，也不给夹层方案；但夹层是 BB 书的主要材料成本，
不给方案就无法报价。内部做法（2026-11-24 森西）：**查公司内部用纸厚度表
（`assets/page_thickness.xlsx`），用尽量少/尽量便宜的纸凑到目标厚度**。

公式
----
    Δ = T − Σ(已有层厚度)              # 还差多少
    已有层厚度 = Σ(纸种厚度 × 张数)      # 封面/内文/衬纸，查 page_thickness
    夹层方案   = 用夹层纸种组合出 Δ，偏差 ≤ 容差，张数最少

目标厚度 T 与「要不要加纸」的判定树（2026-11-24 森西口径，权威）
---------------------------------------------------------------
BB 书**可以做成薄款**，所以默认不插纸。按询价内容分三种，顺序判断：

    1. 询价里客户明确提到要「加纸」 → 加纸。
       目标厚度若同时给了，按 2 执行；没给则目标待定（器件厚度/业务指定）。
    2. 询价里给了「书脊厚度」 → 加纸到该厚度（T = 客户给的值）。
    3. 两条都没有 → **不插纸**（做薄款）。这是缺省，不是"待问"。

注意：器件（发声器/灯盒）厚度**不能自动当书脊厚度**——器件装在书右侧、不在书脊，
书脊厚度与器件无关。只有业务/客户明确要求「书芯与器件齐平」时才用器件厚度当 T。

「最便宜」怎么衡量
------------------
仓库里**没有纸价表**，所以本工具不按金额优化，只输出「张数最少」与「方案清单」，
成本由业务按实际纸价挑。经验口径：夹层首选灰板（单位厚度最便宜的板材料），
同厚度下张数越少越省人工。

用法
----
    # Hijaiyahku：发声器 14.5mm 厚，书芯用 300g 单粉卡对裱
    uv run ./scripts/spacer_calc.py --target 14.5 \
        --existing "单粉:300:1,单粉:300:10" --paper 灰板

    # 想看灰板+粉灰混搭
    uv run ./scripts/spacer_calc.py --target 9.5 \
        --existing "单粉:300:5" --paper 灰板,粉灰

    # 列可用纸种与厚度
    uv run ./scripts/spacer_calc.py --list
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from paper_weight import load_table, normalize_style  # noqa: E402

DEFAULT_SPACER_PAPERS = ("灰板",)
MAX_SHEETS = 24          # 单层夹层最多张数，超过说明该换厚规格
UNIT = 100               # 厚度精度：1/100 mm

# ── 询价原文信号检测（2026-11-24）────────────────────────────────────────────
# 客户明确要「加纸/夹层」的写法（中/英/德）。命中即情况 1。
SPACER_REQUEST_PATTERNS = (
    r"加纸", r"夹纸", r"夹层", r"插纸", r"裱厚", r"加厚纸板", r"内文加厚",
    r"(?i)\bspacer\b", r"(?i)added\s+board", r"(?i)extra\s+board",
    r"(?i)add(?:ed|ing)?\s+(?:a\s+)?(?:grey\s*)?board",
    r"(?i)auf(?:ge)?doppl\w*", r"(?i)verst(?:ä|a)rk\w*\s+(?:den\s+)?(?:r(?:ü|u)cken|buchblock)",
)
# 书脊厚度的写法及其后的数值（mm）。值在下面用独立正则抓。
SPINE_THICKNESS_PATTERNS = (
    r"(?i)spine\s*(?:width|thickness|depth|\bapprox\.?)?\s*[:=]?\s*"
    r"(?:approx\.?|ca\.?|about|~)?\s*(\d+(?:[.,]\d+)?)\s*mm",
    r"(?i)r(?:ü|u)ckenst(?:ä|a)rke\s*[:=]?\s*(?:ca\.?|approx\.?)?\s*(\d+(?:[.,]\d+)?)\s*mm",
    r"(?i)buchr(?:ü|u)cken\s*[:=]?\s*(?:ca\.?|approx\.?)?\s*(\d+(?:[.,]\d+)?)\s*mm",
    r"书脊(?:厚度|宽|宽厚)\s*[:：=]?\s*(?:约)?\s*(\d+(?:\.\d+)?)\s*mm?",
)


def detect_signals(text: str) -> dict:
    """从询价原文里找「加纸要求」和「书脊厚度」。

    返回 {"spacer_requested": bool, "spine_mm": float|None, "evidence": [str]}
    evidence 保留命中的原句片段，供人工复核（自动判定必须可追溯）。
    """
    import re as _re

    if not text:
        return {"spacer_requested": False, "spine_mm": None, "evidence": []}

    evidence: list[str] = []
    requested = False
    for pat in SPACER_REQUEST_PATTERNS:
        for m in _re.finditer(pat, text):
            requested = True
            s = max(0, m.start() - 40)
            evidence.append(text[s:m.end() + 40].replace("\n", " ").strip())

    spine_mm = None
    for pat in SPINE_THICKNESS_PATTERNS:
        m = _re.search(pat, text)
        if m:
            try:
                spine_mm = float(m.group(1).replace(",", "."))
            except (TypeError, ValueError):
                continue
            s = max(0, m.start() - 40)
            evidence.append(text[s:m.end() + 20].replace("\n", " ").strip())
            break

    # 去重保序
    seen, uniq = set(), []
    for e in evidence:
        if e and e not in seen:
            seen.add(e)
            uniq.append(e)
    return {"spacer_requested": requested, "spine_mm": spine_mm, "evidence": uniq}


def decide(*, spine_mm=None, spacer_requested=False,
           device_mm=None, target_mm=None, text: str = "") -> dict:
    """「要不要加纸」判定（森西 2026-11-24 口径）。不落库，只给结论。

    顺序：客户给的厚度 > 原文检测到的厚度 > 提到加纸但没厚度(pending) > 不插纸。
    """
    sig = detect_signals(text) if text else {"spacer_requested": False,
                                             "spine_mm": None, "evidence": []}
    requested = bool(spacer_requested or sig["spacer_requested"])
    spine = target_mm if target_mm is not None else (
        spine_mm if spine_mm is not None else sig["spine_mm"])

    if spine is not None:
        return {
            "spacer_needed": "yes",
            "target_mm": round(float(spine), 2),
            "reason": "询价给了书脊厚度 %.2fmm → 加纸到该厚度" % float(spine),
            "evidence": sig["evidence"],
            "source": "客户书脊厚度",
        }
    if requested and device_mm is not None:
        return {
            "spacer_needed": "yes",
            "target_mm": round(float(device_mm), 2),
            "reason": ("询价提到加纸；目标取器件厚度 %.2fmm（须业务确认书芯与器件齐平）"
                       % float(device_mm)),
            "evidence": sig["evidence"],
            "source": "器件厚度（待确认）",
        }
    if requested:
        return {
            "spacer_needed": "pending",
            "target_mm": None,
            "reason": "询价提到要加纸，但未给书脊厚度 → 目标厚度待业务/客户确认",
            "evidence": sig["evidence"],
            "source": "加纸要求（缺厚度）",
        }
    return {
        "spacer_needed": "no",
        "target_mm": None,
        "reason": "询价未提加纸、也未给书脊厚度 → 不插纸，做薄款（夹层留空即正确）",
        "evidence": [],
        "source": "缺省（薄款）",
    }


def _thickness(table: dict, style: str, gsm: float) -> float | None:
    norm = normalize_style(style)
    if not norm:
        return None
    for g, t in table.get(norm, []):
        if abs(float(g) - float(gsm)) < 1e-6:
            return float(t)
    return None


def parse_existing(spec: str, table: dict) -> list[dict]:
    """`"单粉:300:1,单粉:300:10"` → [{style, gsm, sheets, mm_each, mm_total}]"""
    out = []
    for chunk in (spec or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(":")
        if len(parts) != 3:
            raise SystemExit(f"已有层格式应为 纸种:克重:张数，收到：{chunk!r}")
        style, gsm, sheets = parts[0].strip(), float(parts[1]), float(parts[2])
        t = _thickness(table, style, gsm)
        if t is None:
            raise SystemExit(f"page_thickness 里查不到 {style} {gsm:g}g，请先确认纸型/克重")
        out.append({"style": style, "gsm": gsm, "sheets": sheets,
                    "mm_each": t, "mm_total": t * sheets})
    return out


def _sheets_for(papers: list[tuple[str, float, float]], need_units: int,
                tolerance_units: int) -> dict | None:
    """DP：用 papers 组合出最接近 need_units 的总厚度，张数最少。

    papers: [(style, gsm, mm_each)]
    返回 {total_units, sheets, plan:[(style, gsm, mm_each, count)]} 或 None
    """
    # 每种规格最多多少张
    caps = [min(MAX_SHEETS, max(1, -(-need_units // max(1, int(round(mm * UNIT))))))
            for _, _, mm in papers]
    target_max = need_units + tolerance_units
    best: dict[int, tuple[int, list[int]]] = {0: (0, [0] * len(papers))}

    for idx, (_, _, mm) in enumerate(papers):
        step = int(round(mm * UNIT))
        nxt: dict[int, tuple[int, list[int]]] = {}
        for total, (cnt, plan) in best.items():
            for k in range(0, caps[idx] + 1):
                nt = total + step * k
                if nt > target_max:
                    break
                nc = cnt + k
                cur = nxt.get(nt)
                if cur is None or nc < cur[0]:
                    np_ = list(plan)
                    np_[idx] = k
                    nxt[nt] = (nc, np_)
        for t, v in nxt.items():
            old = best.get(t)
            if old is None or v[0] < old[0]:
                best[t] = v
        best.update(nxt)

    # 选：偏差在容差内、总厚度最接近且不少于 need（宁厚勿薄），否则取最接近
    over = [(abs(t - need_units), t, v) for t, v in best.items()
            if need_units <= t <= target_max]
    if over:
        over.sort(key=lambda x: (x[0], x[2][0]))
        _, t, (cnt, plan) = over[0]
    else:
        cand = sorted(best.items(), key=lambda kv: (abs(kv[0] - need_units), kv[1][0]))
        if not cand:
            return None
        t, (cnt, plan) = cand[0]
    return {
        "total_units": t, "sheets": cnt,
        "plan": [{"style": p[0], "gsm": p[1], "mm_each": p[2], "count": plan[i]}
                 for i, p in enumerate(papers) if plan[i]],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="夹层补厚方案计算（page_thickness 为数据源）")
    ap.add_argument("--target", type=float, help="目标书脊/成品厚度 mm")
    ap.add_argument("--existing", default="", help='已有层，如 "单粉:300:1,单粉:300:10"')
    ap.add_argument("--paper", default=",".join(DEFAULT_SPACER_PAPERS),
                    help="夹层可选纸种，逗号分隔（默认 灰板）")
    ap.add_argument("--tolerance", type=float, default=0.4, help="允许超出目标的容差 mm")
    ap.add_argument("--decide", action="store_true",
                    help="只做「要不要加纸」判定（不传 --target 也可用）")
    ap.add_argument("--spacer-requested", action="store_true",
                    help="询价里客户明确提到要加纸（未给厚度时结论=目标待定）")
    ap.add_argument("--device-thickness", type=float,
                    help="器件厚度 mm；仅当业务确认「书芯与器件齐平」时才作为目标厚度")
    ap.add_argument("--text", default="",
                    help="询价原文（字符串或文件路径）；给了就自动检测「加纸/书脊厚度」")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--list", action="store_true", help="列出厚度表里所有纸种与规格")
    args = ap.parse_args()

    table = load_table()

    if args.list:
        for style in sorted(table):
            rows = sorted(table[style])
            print("%-10s %s" % (style, "  ".join("%gg→%.2fmm" % (g, t) for g, t in rows)))
        return 0

    # ---- 「要不要加纸」判定（2026-11-24 森西口径）----
    if args.decide or args.spacer_requested:
        text = ""
        if args.text:
            p = Path(args.text)
            text = p.read_text(encoding="utf-8", errors="ignore") if p.is_file() else args.text
        d = decide(spacer_requested=args.spacer_requested,
                   device_mm=args.device_thickness,
                   target_mm=args.target, text=text)
        if d["target_mm"] is not None:
            args.target = d["target_mm"]

        if d["spacer_needed"] != "yes":
            if args.json:
                print(json.dumps(d, ensure_ascii=False, indent=2))
            else:
                label = ("不加纸（薄款）" if d["spacer_needed"] == "no"
                         else "要加纸，但目标厚度待定")
                print("夹层判定：%s —— %s" % (label, d["reason"]))
                if d["spacer_needed"] == "no":
                    print("  → 夹层留空即为正确，不要凭经验补厚度。")
                for e in d["evidence"][:3]:
                    print("    证据: …%s…" % e)
            return 0
        if not args.json:
            print("夹层判定：加纸 —— %s\n" % d["reason"])
        # --decide 只给判定；没给 --existing 就不算方案（算出来的是「内文零厚度」的假方案）
        if args.decide and not args.existing:
            if not args.json:
                print("  → 算补厚方案请加 --existing \"纸种:克重:张数\"，"
                      "或去掉 --decide 直接给 --target")
            else:
                print(json.dumps(d, ensure_ascii=False, indent=2))
            return 0

    if args.target is None:
        ap.error("需要 --target（目标厚度 mm）；或 --decide / --spacer-requested 做判定；或 --list")

    existing = parse_existing(args.existing, table)
    have = sum(e["mm_total"] for e in existing)
    need = args.target - have
    if need <= 0:
        print(f"已有层 {have:.2f}mm 已达标（目标 {args.target:g}mm），不需要夹层")
        return 0

    papers = []
    for name in [p.strip() for p in args.paper.split(",") if p.strip()]:
        rows = table.get(normalize_style(name) or "", [])
        if not rows:
            raise SystemExit(f"厚度表里没有纸种 {name!r}")
        for g, t in sorted(rows, reverse=True):
            papers.append((name, float(g), float(t)))
    if not papers:
        raise SystemExit("没有可用的夹层纸种")

    need_units = int(round(need * UNIT))
    tol_units = int(round(args.tolerance * UNIT))
    sol = _sheets_for(papers, need_units, tol_units)
    if not sol:
        raise SystemExit("凑不出方案，放宽 --tolerance 或增加纸种")

    total = sol["total_units"] / UNIT
    result = {
        "target_mm": args.target,
        "existing": existing,
        "existing_mm": round(have, 2),
        "need_mm": round(need, 2),
        "solution": sol["plan"],
        "sheets": sol["sheets"],
        "spacer_mm": round(total, 2),
        "final_mm": round(have + total, 2),
        "deviation_mm": round(have + total - args.target, 2),
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    print(f"目标厚度 {args.target:g}mm")
    for e in existing:
        print(f"  已有  {e['style']}{e['gsm']:g}g × {e['sheets']:g} 张 = {e['mm_total']:.2f}mm")
    print(f"  已有合计 {have:.2f}mm → 需补 {need:.2f}mm")
    print(f"\n夹层方案（{sol['sheets']} 张，共 {total:.2f}mm）：")
    for p in sol["plan"]:
        print(f"  {p['style']}{p['gsm']:g}g × {p['count']} 张（{p['mm_each']:.2f}mm/张）= {p['mm_each']*p['count']:.2f}mm")
    print(f"\n成品厚 {have + total:.2f}mm，相对目标 {have + total - args.target:+.2f}mm")
    print("\n注意：「最便宜」需要纸价，本工具只保证张数最少；灰板为夹层默认材料。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
