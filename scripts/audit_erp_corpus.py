#!/usr/bin/env python3
"""
audit_erp_corpus.py - 用真实 ERP 语料（18476 份历史报价单 JSON）校准我们的口径

语料：/home/claw/train/custom_cpms_requie/<单号>/<单号>_erp.json
      每份是**一条** ERP 报价需求记录（59 键），2020-2026 年。

为什么需要它：
    我们的 schema 枚举、客户默认值、推导规则，过去都是「业务口头确认 + 少数几份
    导出」定的。语料是 6 年全量真值，任何一条规则都可以在这里被证实或证伪。

四个模式：
    --fill        字段填充率（哪些字段真实单据根本没人填，逐年看）
    --enums       真实取值 vs assets/erp_form_schema.yaml 双向差分
    --customers   按客户（KHZL）统计默认三件套，可 --check 对比 customers/*.yaml
    --derive      推导规则可推导性打分（正文关键词是否真的能推出字段）

用法：
    uv run ./scripts/audit_erp_corpus.py --fill
    uv run ./scripts/audit_erp_corpus.py --enums --since 2023
    uv run ./scripts/audit_erp_corpus.py --customers --min 30 --check
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_CORPUS = Path("/home/claw/train/custom_cpms_requie")
SCHEMA = REPO / "assets" / "erp_form_schema.yaml"
CUSTOMERS = REPO / "customers"

# 报价阶段「三件套」= 真实单据里唯一稳定有值的字段；其余见 --fill 的实测填充率
CORE = ("BZFS", "TBLX", "SHFS")
WATCH = ("BZ", "CPDL", "SHDQ", "YSLX", "ZXLB", "ZBLX", "ZXFS", "DKMC") + CORE + (
    "SFYPJ", "chkBSYTB", "chkZDBJF", "chkXBJF", "chkBSYJZX", "Drop_SJFX",
)


def load(corpus: Path) -> list[dict]:
    files = sorted(corpus.glob("*/*_erp.json"))
    if not files:
        sys.exit(f"语料不存在或为空：{corpus}\n（语料在 /home/claw/train/custom_cpms_requie，非仓库内容）")
    out = []
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        if isinstance(d, list):
            d = d[0] if d else {}
        d["__path"] = str(f)
        out.append(d)
    return out


def year_of(d: dict) -> int | None:
    # BDBH = BJXQ220210006-01（报价需求号，无年月）；KHDDH = CM22021001-01（有）
    for k in ("KHDDH", "OLD_KHDDH", "BDBH"):
        m = re.match(r"^[A-Z]{2}(\d{2})", str(d.get(k) or ""))
        if m:
            y = int(m.group(1))
            return 2000 + y if y < 80 else 1900 + y
    return None


def filled(v) -> bool:
    return v not in (None, "", [], {})


def fmt_val(v) -> str:
    return str(v).strip()


# ── 1. 填充率 ────────────────────────────────────────────────────────────────
def mode_fill(recs: list[dict]) -> None:
    years = Counter(year_of(d) for d in recs)
    print(f"语料 {len(recs)} 份；年份分布 "
          + ", ".join(f"{y}:{n}" for y, n in sorted(years.items(), key=lambda kv: (kv[0] is None, kv[0]))) + "\n")

    print("字段                    总填充率        2023+ 是否仍空")
    recent = [d for d in recs if (year_of(d) or 0) >= 2023]
    for f in WATCH:
        n = sum(1 for d in recs if filled(d.get(f)))
        r = sum(1 for d in recent if filled(d.get(f)))
        flag = "  ← 近三年也全空（报价阶段不填）" if r == 0 and n == 0 else ""
        print(f"  {f:<20} {n:6d}/{len(recs)} ({n*100//max(1,len(recs)):3d}%)  "
              f"{r:6d}/{len(recent)} ({r*100//max(1,len(recent)):3d}%){flag}")

    print("\n取值分布（有值的字段）：")
    for f in WATCH:
        c = Counter(fmt_val(d.get(f)) for d in recs if filled(d.get(f)))
        if not c:
            continue
        print(f"\n  ### {f}  {len(c)} 种")
        for v, n in c.most_common(10):
            print(f"      {n:6d}  {v!r}")
        if len(c) > 10:
            print(f"      ...其余 {len(c)-10} 种")


# ── 2. 枚举差分 ──────────────────────────────────────────────────────────────
def schema_enums() -> dict[str, list[str]]:
    import yaml
    raw = yaml.safe_load(SCHEMA.read_text(encoding="utf-8"))
    fields = raw.get("fields") or raw
    out = {}
    for name, spec in fields.items():
        if not isinstance(spec, dict):
            continue
        # legacy_options：表单侧历史下拉（如 SFYPJ 的 是/否），我们导出布尔、ERP 映射。
        # 真实导出的就是这个历史写法，所以校验时要把它一并算作合法值。
        opts = spec.get("options") or spec.get("enum") or []
        vals = []
        for it in (opts if isinstance(opts, list) else []):
            if isinstance(it, dict):
                v = it.get("value") or it.get("label")
            else:
                v = it
            if v is not None:
                vals.append(str(v).strip())
        legacy = spec.get("legacy_options")
        if isinstance(legacy, list):
            for it in legacy:
                v = it.get("label") if isinstance(it, dict) else it
                if v is not None:
                    vals.append(str(v).strip())
        if vals:
            out[name] = vals
    return out


def mode_enums(recs: list[dict], since: int) -> None:
    sub = [d for d in recs if (year_of(d) or 0) >= since]
    print(f"只看 {since}+ 的记录（{len(sub)} 份）—— 更早的写法是下拉规范化前的废弃值\n")
    enums = schema_enums()
    for f in ("BZFS", "TBLX", "SHFS", "SFYPJ"):
        real = Counter(fmt_val(d.get(f)) for d in sub if filled(d.get(f)))
        sch = enums.get(f, [])
        outside = [(v, n) for v, n in real.most_common() if v not in sch]
        unused = [v for v in sch if v not in real]
        status = "✓ 一致" if not outside else f"⚠ {len(outside)} 个值不在 schema"
        print(f"--- {f}: 真实 {len(real)} 种 / schema {len(sch)} 种  {status}")
        for v, n in outside:
            print(f"      schema 外 {n:5d}  {v!r}   ← ERP 会静默丢弃")
        if unused:
            print(f"      schema 有但从未用过: {unused}")


# ── 3. 客户级默认值 ──────────────────────────────────────────────────────────
def resolve_profile(khzl: str, profiles: dict[str, dict]) -> dict:
    """ERP 的 KHZL 是 NGV10 / NGVS16 / IBL10 这种「档案名 + 数字」的写法，
    档案文件名是 ngv.yaml / igl.yaml。按前缀匹配，取最长命中的那个。"""
    low = khzl.lower()
    hits = [k for k in profiles if low.startswith(k) or k.startswith(low)]
    if not hits:
        return {}
    return profiles[max(hits, key=len)]


def mode_customers(recs: list[dict], minimum: int, check: bool) -> None:
    by_cust: dict[str, list[dict]] = defaultdict(list)
    for d in recs:
        k = fmt_val(d.get("KHZL") or "")
        if k:
            by_cust[k].append(d)

    profiles = {}
    if check:
        import yaml
        for p in sorted(CUSTOMERS.glob("*.yaml")):
            if p.name.startswith("_"):
                continue
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            profiles[p.stem] = data.get("erp_defaults") or {}
        print("档案 erp_defaults vs 语料众数（只列有档案的客户）：\n")

    for name, ds in sorted(by_cust.items(), key=lambda kv: -len(kv[1])):
        if len(ds) < minimum:
            continue
        prof = resolve_profile(name, profiles) if check else {}
        if check and not prof:
            continue
        parts, notes = [], []
        for f in ("BZFS", "TBLX", "SHFS", "SHDQ", "YSLX", "ZXLB", "ZBLX", "ZXFS", "DKMC", "BZ"):
            c = Counter(fmt_val(d.get(f)) for d in ds if filled(d.get(f)))
            want = prof.get(f)
            if not c:
                parts.append(f"{f}=（全空）")
                if want not in (None, ""):
                    notes.append(f"{f}: 档案填了 {want!r}，但 {len(ds)} 份真实单据从未填过")
                continue
            v, n = c.most_common(1)[0]
            tot = sum(c.values())
            parts.append(f"{f}={v}({n*100//tot}%)")
            if want is not None and str(want).strip() and str(want).strip() != v:
                notes.append(f"{f}: 档案={want!r} 众数={v!r}({n*100//tot}%)")
        print(f"  {name:<10}{len(ds):>5}  " + "  ".join(parts))
        for nt in notes:
            print(f"           ⚠ {nt}")

    if check:
        print("\n布尔/勾选框现状（真实单据里是否出现过 true）：")
        for f in ("chkBSYTB", "chkZDBJF", "chkXBJF", "chkBSYJZX"):
            c = Counter(repr(d.get(f)) for d in recs)
            for pname, prof in profiles.items():
                want = prof.get(f)
                if want in (None, ""):
                    continue
                truthy = sum(1 for d in recs if d.get(f) is True)
                print(f"  {pname}.{f} 档案={want!r}；全语料 true 出现 {truthy} 次，分布 {dict(c.most_common(3))}")


# ── 4. 可推导性 ──────────────────────────────────────────────────────────────
def mode_derive(recs: list[dict]) -> None:
    def txt(d):
        return " ".join(str(d.get(k) or "") for k in ("CPMS", "BGMS"))

    def has(d, pat):
        return bool(re.search(pat, txt(d), re.I))

    rules = [
        ("BZFS=纸箱包装 ↔ 正文提「箱」", lambda d: fmt_val(d.get("BZFS")) == "纸箱包装", r"箱"),
        ("BZFS=牛皮纸包装 ↔ 正文提「牛皮纸」", lambda d: fmt_val(d.get("BZFS")) == "牛皮纸包装", r"牛皮纸"),
        ("SHFS=FOB 盐田 ↔ 正文提「盐田」", lambda d: fmt_val(d.get("SHFS")) == "FOB 盐田", r"盐田"),
        ("SHFS 提到口岸 ↔ 正文提「盐田|香港|蛇口」",
         lambda d: fmt_val(d.get("SHFS")) in ("FOB 盐田", "FOB 香港", "FOB蛇口"), r"盐田|香港|蛇口"),
        ("TBLX=不打卡板 ↔ 正文提「卡板」",
         lambda d: fmt_val(d.get("TBLX")) in ("不打卡板",), r"卡板|卡版|pallet"),
    ]
    print("命中 = 字段取值与正文关键词一致；漏 = 字段有值但正文没提（说明靠默认值，不能推导）\n")
    for label, cond, pat in rules:
        base = [d for d in recs if cond(d)]
        hit = sum(1 for d in base if has(d, pat))
        print(f"  {label}")
        print(f"      字段有值 {len(base):6d} 份，正文关键词命中 {hit:6d} "
              f"({hit*100//max(1,len(base)):3d}%)，漏 {len(base)-hit:6d}")


def main() -> None:
    ap = argparse.ArgumentParser(description="用真实 ERP 语料校准口径")
    ap.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    ap.add_argument("--fill", action="store_true", help="字段填充率与取值分布")
    ap.add_argument("--enums", action="store_true", help="真实取值 vs schema 枚举差分")
    ap.add_argument("--customers", action="store_true", help="按客户统计默认三件套")
    ap.add_argument("--derive", action="store_true", help="推导规则可推导性打分")
    ap.add_argument("--since", type=int, default=2023, help="--enums 的年份下限（默认 2023）")
    ap.add_argument("--min", type=int, default=30, help="--customers 的最少单据数（默认 30）")
    ap.add_argument("--check", action="store_true", help="--customers 时对比 customers/*.yaml")
    args = ap.parse_args()

    if not any([args.fill, args.enums, args.customers, args.derive]):
        ap.print_help()
        return

    recs = load(args.corpus)
    if args.fill:
        mode_fill(recs)
    if args.enums:
        print() if args.fill else None
        mode_enums(recs, args.since)
    if args.customers:
        print() if (args.fill or args.enums) else None
        mode_customers(recs, args.min, args.check)
    if args.derive:
        print() if (args.fill or args.enums or args.customers) else None
        mode_derive(recs)


if __name__ == "__main__":
    main()
