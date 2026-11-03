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
import re
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


def build_customer_map() -> dict[str, list[str]]:
    """档案 id → ERP 客户代码列表。

    代码来自档案自己的 `erp_khzl` 声明（CustomerProfile 已解析好，这里不再读 yaml）。
    没声明 erp_khzl 的档案不参与归组 —— 宁可漏统计，也不要按 id 前缀瞎猜
    （`IBL10` 和 `igl` 前缀根本对不上）。
    """
    from customer_profile import list_profiles, load_profile
    out: dict[str, list[str]] = {}
    for cid in list_profiles():
        codes = load_profile(cid).erp_khzl
        if codes:
            out[cid] = codes
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


def sanitize_id(code: str) -> str:
    """ERP 客户代码 → 档案 id：小写、只留字母数字（`FES 22` → `fes22`）。"""
    return re.sub(r"[^a-z0-9]+", "", code.lower())


def corpus_profile(subset: list[dict]) -> str:
    """一句话画出这个客户的「语料画像」——写进 notes，方便人认人。"""
    years = sorted({y for d in subset if (y := year_of(d)) is not None})
    span = f"{years[0]}-{years[-1]}" if years else "年份未知"
    bits = [f"全部 {len(subset)} 份单据（{span}）"]
    for f, label in (("CPLB", "常见产品类别"), ("SHFS", "送货方式"), ("BZFS", "包装方式")):
        c = Counter(fmt_val(d.get(f)) for d in subset if filled(d.get(f)))
        if c:
            v, n = c.most_common(1)[0]
            bits.append(f"{label}「{v}」({n * 100 // sum(c.values())}%)")
    return "，".join(bits)


def bootstrap_profile(cust: str, pid: str, subset: list[dict], res: dict,
                      args, common_fixed: set[str]) -> Path:
    """给「还没收到询价文件」的客户建一份**只含语料能证明的部分**的档案。"""
    adopted = {f: r for f, r in res["rows"].items()
               if r["tier"] == "采纳" and f in WRITABLE and f not in common_fixed}
    lines = [
        "# ═══════════════════════════════════════════════════════════════",
        f"# 客户档案：{cust}（自动生成草稿 —— 尚未收到询价文件）",
        "#",
        f"# 来源：真实 ERP 语料 {args.corpus}",
        f"#       客户代码 {cust}，{res['total']} 份单据（{args.since}+），年份 {res['years']}",
        "# 生成：uv run ./scripts/derive_customer_defaults.py --bootstrap",
        "#",
        f"# 这份档案只含**语料能证明的部分**：erp_defaults 的门槛是覆盖率 ≥{args.min_cov:.0%}",
        f"# 且一致率 ≥{args.min_rate:.0%}。fields / detect / rules **必须等第一份真实询价",
        "# 文件来了再校准** —— 现在写等于凭空猜列名（用 scripts/calibrate_customer.py）。",
        "# status 保持 draft：没被真实询价文件验证过，不进自动识别。",
        "# ═══════════════════════════════════════════════════════════════",
        f"id: {pid}",
        f"name: {cust}",
        "priority: 10",
        "status: draft",
        f"erp_khzl: [{cust}]",
        "notes: >",
        f"  语料画像：{corpus_profile(subset)}。",
        f"  默认值只用其中 {res['total']} 份（{args.since}+）统计 —— 更早的取值是 ERP 下拉"
        "规范化前的废弃写法。",
        "  尚未收到询价文件；解析规则（fields / detect / rules）待校准。",
        "",
        "# ── 1. 身份识别：还不知道他们的文件名/标签长什么样，先留空 ──────────",
        "detect:",
        "  filename_keywords: []",
        "  label_signatures: []",
        "",
        "# ── 2. 列名别名表：等真实询价文件──────────────────────────────",
        "fields: {}",
        "",
        "# ── 3. 默认值层：语料统计出来的常用值（只补空，客户写明了听客户的）──",
        "erp_defaults:",
    ]
    if not adopted:
        lines.append("  {}   # 语料里没有字段同时满足覆盖率与一致率阈值")
    for f, r in adopted.items():
        val = coerce(f, r["mode"])
        lines.append(
            f"  {f}: {json.dumps(val, ensure_ascii=False)}"
            f"    # {WRITABLE[f]}：覆盖 {r['coverage'] * 100:.0f}%"
            f"（{r['n_filled']}/{res['total']}），一致 {r['rate'] * 100:.0f}%"
        )

    lines += ["", "# ── 人看的偏好表（必须与上面落库值一致，校验器会拦）───────────"]
    if adopted:
        lines.append("preferences:")
        for f, r in adopted.items():
            val = coerce(f, r["mode"])
            shown = json.dumps(val, ensure_ascii=False) if not isinstance(val, bool) else str(val).lower()
            lines.append(
                f"  - {{ field: {f}, value: {shown},"
                f" reason: \"语料 {r['n_filled']}/{res['total']} 份一致 {r['rate'] * 100:.0f}%"
                f"（覆盖 {r['coverage'] * 100:.0f}%）\" }}"
            )
    else:
        lines.append("preferences: []")

    leftovers = [(f, r) for f, r in res["rows"].items()
                 if f in WRITABLE and r["tier"] != "采纳" and f not in common_fixed]
    if leftovers:
        lines += ["", "# ── 没给默认值的字段（不是漏了，是数据不支持）──────────────"]
        for f, r in leftovers:
            if r["tier"] == "从不填":
                why = "语料 0 填充（报价阶段不填）"
                val = ""
            elif r["tier"] == "需业务定(不稳定)":
                why = f"只 {r['rate'] * 100:.0f}% 一致，每单在变"
                val = str(r["mode"])
            else:
                why = f"覆盖仅 {r['coverage'] * 100:.0f}%"
                val = str(r["mode"])
            lines.append(f"#   {f:<10} {val[:24]:<26} {why}")

    out = CUSTOMERS / f"{pid}.yaml"
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
    ap.add_argument("--bootstrap", action="store_true",
                    help="给单据数达标的客户直接建档案 customers/<id>.yaml（已存在则跳过）")
    ap.add_argument("--force", action="store_true", help="--bootstrap 时覆盖已存在档案")
    args = ap.parse_args()

    recs = load(args.corpus)
    profiles = build_customer_map()

    groups: dict[str, list[dict]] = {}
    for d in recs:
        k = fmt_val(d.get("KHZL") or "")
        if k:
            groups.setdefault(k, []).append(d)

    # ── 批量建档：单据数达标的客户，即使还没收到询价文件也先把业务数据铺好 ──
    if args.bootstrap:
        from customer_profile import DEFAULTS_ID, load_profile
        common_fixed = set(load_profile(DEFAULTS_ID).erp_fixed_values())
        made, existed, small = [], [], []
        for code in sorted(groups, key=lambda c: -len(groups[c])):
            n = len(groups[code])
            if n < args.min_records:
                small.append((code, n))
                continue
            pid = sanitize_id(code)
            target = CUSTOMERS / f"{pid}.yaml"
            # 已经有档案认领了这个客户代码（如 NGV10→ngv），别再建一份重复的
            owner = resolve(code, profiles)
            if owner or (target.exists() and not args.force):
                existed.append(f"{code}→{owner or pid}")
                continue
            res = analyse(groups[code], args.since, args.min_rate, args.min_cov)
            if args.dry_run:
                adopted = sum(1 for f, r in res["rows"].items()
                              if r["tier"] == "采纳" and f in WRITABLE and f not in common_fixed)
                print(f"  （dry-run）{pid:<10} {n:5d} 份  默认值 {adopted} 个")
                made.append((code, pid, n, adopted))
                continue
            out = bootstrap_profile(code, pid, groups[code], res, args, common_fixed)
            adopted = sum(1 for f, r in res["rows"].items()
                          if r["tier"] == "采纳" and f in WRITABLE and f not in common_fixed)
            made.append((code, pid, n, adopted))
            print(f"  建档 {pid:<10} {n:5d} 份  默认值 {adopted} 个  → {out.relative_to(REPO)}")
        print(f"\n新建 {len(made)} 份档案；已存在跳过 {len(existed)} 个"
              f"（{', '.join(existed) if existed else '无'}）；"
              f"单据 < {args.min_records} 份的 {len(small)} 个客户未建档"
              f"（共 {sum(n for _, n in small)} 份）")
        return

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
