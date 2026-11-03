#!/usr/bin/env python3
"""
derive_customer_defaults.py - 从真实 ERP 语料反推客户的「常用 ERP 信息」

做什么：
    把某个客户（ERP 里 KHZL 形如 NGV10 / IBL10）历年所有报价单里的字段值汇总，
    逐字段找出**出现最多的那个值**，出现率（覆盖 × 一致）过线才写进档案
    `erp_defaults`，这就是该客户的「常用 ERP 信息」。

为什么要两个率：
    * 覆盖率 coverage = 有值单据 / 全部单据
      —— 覆盖率低的字段说明报价阶段根本没人填（例：SHDQ/YSLX 六年 0%，SHDQ 全空），
         给它设默认值等于造出 6 年数据里从未出现过的值。
    * 一致率 rate = 众数出现次数 / 有值单据
      —— 一致率低说明这个字段每单都在变，设默认值就是瞎猜（例：IGL 的 TBLX 只有 32%）。
    两个率都 ≥ 阈值才算「常用信息」。

产出：
    customers/_draft_<id>.erp_defaults.yaml  —— 可并入 customers/<id>.yaml 的 erp_defaults 段
    （只写文件，不改正式档案；合并这一步由人确认）

用法：
    uv run ./scripts/derive_customer_defaults.py                 # 所有能识别的客户
    uv run ./scripts/derive_customer_defaults.py --customer ngv  # 单个
    uv run ./scripts/derive_customer_defaults.py --customer ngv --json
    uv run ./scripts/derive_customer_defaults.py --min-rate 0.8 --since 2020
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit_erp_corpus import DEFAULT_CORPUS, CUSTOMERS, filled, fmt_val, load, year_of  # noqa: E402

# ── 哪些字段适合当「默认值」 ────────────────────────────────────────────────
# AX 可写：与具体产品无关、这个客户长期稳定的口径
WRITABLE = {
    "BZFS": "包装方式",
    "TBLX": "台板类型",
    "SHFS": "送货方式",
    "BZ": "币种",
    "YSLX": "运输类型",
    "SHDQ": "送货地区",
    "ZXLB": "纸箱类别",
    "ZBLX": "纸板类型",
    "ZXFS": "装箱方式",
    "DKMC": "底卡名称",
    "CPDL": "产品大类",
    "KHMBDJ": "客户目标单价",
    "chkBSYTB": "不使用台板",
    "chkZDBJF": "自动报价",
    "chkXBJF": "需报箱价",
    "chkBSYJZX": "不使用纸张",
}
# 只看不写：要么逐单内容、要么由别的规则决定（写进默认值会和规则打架）
REFERENCE = {
    "SFYPJ": "是否有配件（由客户档案 accessories 规则推，不设死默认）",
    "PXH": "排序号（schema 已有默认 1）",
    "CPLB": "产品类别（逐单随产品变）",
    "CPLX": "产品类型（逐单变）",
    "KHDQ": "客户地区（表单只读，随客户带出，我们不填）",
    "Drop_SJFX": "书籍方向（正方书留 null）",
}
# 逐单唯一 / 内容类：统计出来也没意义
EXCLUDED = {
    "BDBH": "报价需求号（每单唯一）",
    "KHDDH": "香港单号（每单唯一）",
    "OLD_KHDDH": "返单号（每单唯一）",
    "KHDH": "客户单号（每单唯一）",
    "CPMC": "产品中文名（逐单）",
    "ECPMC": "原始产品名（逐单）",
    "KHCPMC": "客户产品名（逐单）",
    "CPMX": "产品明细（逐单）",
    "CPMS": "产品描述（逐单，业务点名排除）",
    "BGMS": "补充描述（逐单）",
    "CPSX": "产品属性（逐单）",
    "CD": "规格长（逐单）",
    "KD": "规格宽（逐单）",
    "GD": "规格高/厚度（逐单）",
    "ZDFS": "装订方式（逐单）",
    "TZS": "总款数（逐单）",
    "YYS": "样书数（逐单）",
    "DWMC": "单位名称（逐单）",
    "FSCMC": "FSC 名称（逐单）",
    "JHRQ": "交货日期（逐单）",
    "WCRQ": "预计下单日期（逐单）",
    "Remark": "备注（逐单）",
    "Remark1": "配件描述（逐单）",
    "Remark3": "修改记录（逐单）",
    "Remark4": "检测项目（逐单）",
}
EXCLUDED.update({f"CPSL{i}": "数量档位（逐单）" for i in range(1, 11)})

# 这些字段导出的必须是**真布尔**，不能是字符串 "False"（ERP 导入会当非法值丢掉）
BOOL_FIELDS = {"chkBSYTB", "chkZDBJF", "chkXBJF", "chkBSYJZX"}


def coerce(field: str, value: str):
    """把统计出来的字符串还原成 JSON 该有的类型。"""
    if field in BOOL_FIELDS:
        return {"True": True, "true": True, "是": True}.get(value, False)
    return value


def parse_khzl_list(profile: Path) -> list[str]:
    """档案可声明 erp_khzl: [NGV10, NGVS16]；没声明就用 id 前缀兜底。"""
    import yaml
    try:
        data = yaml.safe_load(profile.read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    v = data.get("erp_khzl")
    if isinstance(v, str):
        return [v]
    if isinstance(v, list):
        return [str(x) for x in v]
    return []


def build_customer_map() -> dict[str, list[str]]:
    """档案 id → ERP 客户代码列表（显式 erp_khzl 优先，否则按 id 前缀匹配）。"""
    out: dict[str, list[str]] = {}
    for p in sorted(CUSTOMERS.glob("*.yaml")):
        if p.name.startswith("_"):
            continue
        explicit = parse_khzl_list(p)
        if explicit:
            out[p.stem] = explicit
    return out


def load_erp_defaults() -> dict[str, dict]:
    """各档案当前已写的 erp_defaults（用来在报告里标「和现有档案是否一致」）。"""
    import yaml
    out: dict[str, dict] = {}
    for p in sorted(CUSTOMERS.glob("*.yaml")):
        if p.name.startswith("_"):
            continue
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        out[p.stem] = data.get("erp_defaults") or {}
    return out


def resolve(cust: str, profiles: dict[str, list[str]]) -> str | None:
    """KHZL ↔ 档案：显式声明优先，其次前缀包含（IBL10 ↔ igl 需档案声明）。"""
    for pid, codes in profiles.items():
        if cust in codes:
            return pid
    low = cust.lower()
    hits = [pid for pid in profiles if low.startswith(pid) or pid.startswith(low)] if profiles else []
    if hits:
        return max(hits, key=len)
    # 无档案时的前缀猜测（只在日志里提示，不写档案）
    for pid in ("ngv", "igl"):
        if low.startswith(pid[:3]):
            return pid
    return None


def analyse(recs: list[dict], since: int, min_rate: float, min_cov: float) -> dict:
    sub = [d for d in recs if (year_of(d) or 0) >= since] or recs
    total = len(sub)
    years = sorted({y for d in sub if (y := year_of(d)) is not None})
    rows = {}
    for f in list(WRITABLE) + list(REFERENCE):
        vals = [fmt_val(d.get(f)) for d in sub if filled(d.get(f))]
        cov = len(vals) / total if total else 0.0
        if not vals:
            rows[f] = {"tier": "从不填", "coverage": 0.0, "rate": 0.0, "mode": None, "dist": {}}
            continue
        c = Counter(vals)
        mode, n = c.most_common(1)[0]
        rate = n / len(vals)
        if cov >= min_cov and rate >= min_rate:
            tier = "采纳"
        elif rate >= min_rate:
            tier = "候选(覆盖低)"
        elif cov >= min_cov:
            tier = "需业务定(不稳定)"
        else:
            tier = "样本少"
        rows[f] = {
            "tier": tier, "coverage": cov, "rate": rate, "mode": mode,
            "n_mode": n, "n_filled": len(vals),
            "dist": {v: k for v, k in c.most_common(4)},
        }
    return {"total": total, "years": years, "rows": rows}


def print_report(cust: str, pid: str, res: dict, current: dict) -> None:
    print(f"\n{'='*78}\n客户 {cust} → 档案 {pid}   单据 {res['total']} 份   年份 {res['years']}")
    order = {"采纳": 0, "候选(覆盖低)": 1, "需业务定(不稳定)": 2, "样本少": 3, "从不填": 4}
    lines = []
    for f, r in sorted(res["rows"].items(), key=lambda kv: (order[kv[1]["tier"]], kv[0])):
        if f in REFERENCE:
            continue
        tag = f"[{r['tier']}]"
        if r["mode"] is None:
            lines.append(f"  {tag:<18} {f:<10} 有值 0/{res['total']}")
            continue
        top2 = list(r["dist"].items())[:2]
        dist = " / ".join(f"{v}:{n}" for v, n in top2)
        note = ""
        want = current.get(f)
        if want not in (None, ""):
            if str(want).strip() == str(r["mode"]).strip():
                note = "  ✓ 档案现有值一致"
            else:
                note = f"  ⚠ 档案现写 {want!r}"
        lines.append(f"  {tag:<18} {f:<10} 众数 {r['mode']!r:<26} "
                     f"覆盖 {r['coverage']*100:3.0f}%  一致 {r['rate']*100:3.0f}%   ({dist}){note}")
    print("\n".join(lines))
    ref = [(f, r) for f, r in res["rows"].items() if f in REFERENCE and r["mode"] is not None]
    if ref:
        print("\n  只看不写（不进 erp_defaults）：")
        for f, r in ref:
            print(f"    {f:<10} {r['mode']!r:<14} 一致 {r['rate']*100:3.0f}%"
                  f"   —— {REFERENCE[f]}")


def write_draft(cust: str, pid: str, res: dict, min_rate: float, min_cov: float,
                corpus: Path, since: int) -> Path:
    adopted = {f: r for f, r in res["rows"].items() if r["tier"] == "采纳" and f in WRITABLE}
    lines = [
        f"# 由 scripts/derive_customer_defaults.py 生成 —— 请人工确认后并入 customers/{pid}.yaml",
        f"# 语料：{corpus}",
        f"# 客户代码 {cust}，{res['total']} 份单据（{since}+），年份 {res['years']}",
        f"# 阈值：覆盖率 ≥ {min_cov:.0%} 且 一致率 ≥ {min_rate:.0%}",
        "#",
        f"# 采纳 {len(adopted)} 个字段；未采纳的见文件末尾，写清了为什么。",
        f"id: {pid}",
        "erp_defaults:",
    ]
    if not adopted:
        lines.append("  {}   # 语料里没有任何字段同时满足两个阈值")
    for f, r in adopted.items():
        val = coerce(f, r["mode"])
        exp = min(int(r["rate"] * 100), 100)
        second = list(r["dist"].items())[1][0] if len(r["dist"]) > 1 else "-"
        lines.append(
            f"  {f}: {json.dumps(val, ensure_ascii=False)}"
            f"    # {WRITABLE[f]}：覆盖 {r['coverage']*100:.0f}%（{r['n_filled']}/{res['total']}），"
            f"一致 {exp}%；次多 {second}"
        )
    lines.append("")
    lines.append("# ── 未采纳（不是没统计，是统计了不该写）───────────────")
    for f, r in res["rows"].items():
        if r["tier"] == "采纳" and f in WRITABLE:
            continue
        if r["tier"] == "从不填" and f not in WRITABLE:
            continue
        if f in REFERENCE:
            why = f"只看不写：{REFERENCE[f]}"
        elif r["tier"] == "从不填":
            why = "历史 0 填充 —— 设默认值会造出语料里从未出现过的值"
        elif r["tier"] == "候选(覆盖低)":
            why = f"出现即一致({r['rate']*100:.0f}%)，但只有 {r['coverage']*100:.0f}% 的单据填过"
        elif r["tier"] == "需业务定(不稳定)":
            why = f"覆盖 {r['coverage']*100:.0f}% 但只 {r['rate']*100:.0f}% 一致，需业务定"
        else:
            why = "样本不足"
        val = r["mode"] if r["mode"] is not None else ""
        lines.append(f"#   {f:<10} {str(val)[:24]:<26} {why}")
    out = CUSTOMERS / f"_draft_{pid}.erp_defaults.yaml"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="从真实 ERP 语料反推客户常用字段")
    ap.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    ap.add_argument("--customer", help="档案 id（ngv / igl）或 ERP 客户代码（NGV10）")
    ap.add_argument("--min-rate", type=float, default=0.9, help="一致率阈值（默认 0.9）")
    ap.add_argument("--min-cov", type=float, default=0.9, help="覆盖率阈值（默认 0.9）")
    ap.add_argument("--since", type=int, default=2023,
                    help="年份下限（默认 2023：更早的下拉枚举已被 ERP 规范化）")
    ap.add_argument("--min-records", type=int, default=30)
    ap.add_argument("--json", action="store_true", help="额外打印 JSON（erp_defaults 字典）")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写草稿文件")
    args = ap.parse_args()

    recs = load(args.corpus)
    profiles = build_customer_map()

    groups: dict[str, list[dict]] = {}
    for d in recs:
        k = fmt_val(d.get("KHZL") or "")
        if k:
            groups.setdefault(k, []).append(d)

    targets: list[tuple[str, str]] = []
    if args.customer:
        key = args.customer
        pid = resolve(key, profiles) or key.lower()
        codes = [c for c in groups if resolve(c, profiles) == pid]
        if key in groups:                      # 直接给了 ERP 代码
            codes = [key]
        if not codes:
            sys.exit(f"语料里没有客户 {key}")
        targets.append((",".join(sorted(codes)), pid))
    else:
        for pid, codes in profiles.items():
            hit = sorted(c for c in groups if c in codes or resolve(c, profiles) == pid)
            if hit:
                targets.append((",".join(hit), pid))
        orphans = [c for c in groups if resolve(c, profiles) is None]
        if orphans:
            print(f"（{len(orphans)} 个客户代码没有对应档案，共 {sum(len(groups[c]) for c in orphans)} 份，已跳过；"
                  f"其中最大的 3 个："
                  + ", ".join(f"{c}({len(groups[c])})" for c in sorted(orphans, key=lambda c: -len(groups[c]))[:3])
                  + "）")

    current = load_erp_defaults()
    for codes, pid in targets:
        subset = [d for g in codes.split(",") for d in groups.get(g, [])]
        if len(subset) < args.min_records:
            print(f"（跳过 {codes}：只有 {len(subset)} 份 < {args.min_records}）")
            continue
        res = analyse(subset, args.since, args.min_rate, args.min_cov)
        print_report(codes, pid, res, current.get(pid) or {})
        if args.json:
            adopted = {f: coerce(f, r["mode"]) for f, r in res["rows"].items()
                       if r["tier"] == "采纳" and f in WRITABLE}
            print("\njson:", json.dumps(adopted, ensure_ascii=False, indent=2))
        if not args.dry_run:
            out = write_draft(codes, pid, res, args.min_rate, args.min_cov, args.corpus, args.since)
            print(f"\n→ 草稿：{out.relative_to(REPO)}")


if __name__ == "__main__":
    main()
