#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_against_erp.py —— 拿抽取产物跟 ERP 真实录入值逐字段对账。

真值来源（都在 `customer-files/reference-eml/`，见该目录 README.md）
    summary.json      每单从 `BJ_KHBJXQ/Add.aspx` 明细页抓的实际录入值（59 键）
    list_truth.json   每单 ERP **列表页**的值：款数 / 语言数 / 数量档（最小-中间-最大）

为什么两个都要（2026-11-18 实测）
    明细页的 `CPSL1~CPSL10` 只在**单语言**单上有值，且存的是**总量**（含款数）；
    多语言单明细不落数量档（只写主表汇总），列表页的「最小值/最大值」才是全的。
    所以数量对账一律走列表页，按 `单款本数 × 款数` 还原成总量再比（**语言数不放大**：
    2026-11-25 实测 008 10款10语 81,600→816,000 只乘 10，不是 100）。

用法
----
    uv run python scripts/check_against_erp.py <产物>/ERP_xxx.json
    uv run python scripts/check_against_erp.py <产物目录>
    uv run python scripts/check_against_erp.py <目录> --manifest 清单.json   # 产物没单号时
    uv run python scripts/check_against_erp.py <目录> --with-cpms --json

判定六类
    一致 / 我方留空 / 我方多写 / 真差异（只有这类算错）/ 参考（业务手填，不算错）/ 口径待定
退出码：有「真差异」→ 1
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
REF_DIR = REPO / "customer-files" / "reference-eml"
SUMMARY = REF_DIR / "summary.json"
LIST_TRUTH = REF_DIR / "list_truth.json"

# ── 明文比较的字段（数量单列，见 quantity_rows）────────────────────────
COMPARE = [
    "CPLB", "ZDFS", "CPMC", "TZS", "YYS",
    "BZFS", "TBLX", "SHFS",
    "CPSX", "CPLX", "SFYPJ", "Remark1", "Remark4",
    "KHDDH", "JHRQ", "WCRQ",
]

# ── 只记录、不算错（业务手填 / 与抽取质量无关）────────────────────────
REF_ONLY = {
    "YWY": "业务员，ERP 里按客户分配，不是抽取出来的",
    "KHZL": "客户代码，两边命名体系不同",
    "KHDH": "客户单号，业务口径（有客户单号才填）",
}

# ── 不比较的字段与理由 ────────────────────────────────────────────────
SKIP_REASON = {
    "BDBH": "新单号由 ERP 生成",
    "GDY": "跟单员，客户文件里没有",
    "Remark": "主表备注，业务手填",
    "CPMX": "产品描述表头，业务手填",
    "CPDL": "折叠面板字段，工具写不进",
    "Drop_SJFX": "ERP 内部字段",
    "BZ": "币种，业务按市场填",
    "KHMBDJ": "客户目标价，业务谈",
    "PXH": "排序号，一律留空（NGV/IGL 档案口径）",
    "chkXBJF": "布尔开关", "chkZDBJF": "布尔开关",
    "chkBSYTB": "布尔开关（不使用台板）", "chkBSYJZX": "布尔开关（不使用集装箱）",
    "ZXLB": "装箱细项，折叠面板", "ZBLX": "装箱细项，折叠面板", "ZXFS": "装箱细项，折叠面板",
    "DKMC": "底卡名称，折叠面板", "YSLX": "印刷类型，折叠面板",
    "SHDQ": "送货地区，服务端渲染为空、写不进", "KHDQ": "客户地区，业务填",
    "ECPMC": "ERP 产品英文名，照抄产品档案", "OLD_KHDDH": "返单旧单号",
    "BGMS": "变更描述", "Remark3": "修改记录，业务手填",
    "CPMS": "产品描述（长文本，--with-cpms 才比）",
    "DWMC": "单位，ERP 从产品档案反查回填",
    "CD": "尺寸单列成「尺寸(CDxKD)」比较（ERP 长宽先后不统一）",
    "KD": "尺寸单列成「尺寸(CDxKD)」比较（ERP 长宽先后不统一）",
    "FSCMC": "FSC 类别，ERP 从产品档案反查回填",
    **{f"CPSL{i}": "数量档单列成「数量首档/末档」比较（ERP 存总量）" for i in range(1, 11)},
}

NUMERIC = {"TZS", "YYS", "CD", "KD", "GD"}
ZERO_AS_EMPTY = {"YYS"}          # ERP 里「0 本语言」= 没填


def norm(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v).strip()
    if s in ("None", "null"):
        return ""
    return s


def as_int(v: Any) -> int | None:
    s = norm(v).replace(",", "")
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def same(field: str, a: Any, b: Any) -> bool:
    sa, sb = norm(a), norm(b)
    if field in ZERO_AS_EMPTY:
        if sa in ("0", "") and sb in ("0", ""):
            return True
    if field == "CPLB":          # 产物偶写 "27.01;普通BB书"，ERP 只存中文名
        sa, sb = sa.split(";")[-1].strip(), sb.split(";")[-1].strip()
    if sa == sb:
        return True
    if field in NUMERIC:
        ia, ib = as_int(sa), as_int(sb)
        return ia is not None and ib is not None and ia == ib
    fa = sa.replace(" ", "").replace("\u3000", "").casefold()
    fb = sb.replace(" ", "").replace("\u3000", "").casefold()
    return fa == fb


# ── 数量：产物存单款本数，ERP 存总量（总量 = 单款 × 款数，**不乘语言数**）──
def cpsl_tiers(prod: dict) -> list[int]:
    return [x for x in (as_int(prod.get(f"CPSL{i}")) for i in range(1, 11)) if x]


def quantity_rows(prod: dict, lt: dict) -> list[dict]:
    tiers = cpsl_tiers(prod)
    tzs = as_int(prod.get("TZS")) or 1
    yys = as_int(prod.get("YYS")) or 1
    # ★ 2026-11-25 实测 7 单（BJXQ261125006~012）：储能读到的 CPSL1 / 产物 CPSL1 恰等于 TZS。
    #   008 10款10语 81,600→816,000（×10，不是 ×100）、012 10款6语 12,225→122,250（×10，不是 ×60）。
    #   所以总量只按款数放大；再把语言数乘进去会造出「真差异」误报。
    factor = tzs
    lo, hi = (lt.get("最小值"), lt.get("最大值")) if lt else (None, None)
    rows = []

    def add(field, mine, erp_raw, scale_note):
        m, e = as_int(mine), as_int(erp_raw)
        mine_v = (m * factor) if m is not None else ""
        if not tiers:
            verdict = "我方留空" if e else "一致"
            mine_v = ""
        elif e is None:
            verdict = "口径待定"
        elif mine_v == e:
            verdict = "一致"
        else:
            verdict = "真差异"
        rows.append({"字段": field, "我方": mine_v, "ERP": e, "判定": verdict, "备注": scale_note})

    add("数量首档", tiers[0] if tiers else "", lo,
        f"单款 {tiers[0] if tiers else '—'} × 款{tzs}（语{yys}不放大）")
    add("数量末档", tiers[-1] if tiers else "", hi,
        f"单款 {tiers[-1] if tiers else '—'} × 款{tzs}（语{yys}不放大）")
    return rows


def size_row(prod: dict, t: dict) -> dict:
    """尺寸单列：ERP 录单时长宽先后不统一（同一单 165x120 也会录成 120x165），
    所以按「两条边」无序比较，换序不算错。0 视同未填。"""
    cd_m, kd_m = as_int(prod.get("CD")), as_int(prod.get("KD"))
    cd_e, kd_e = as_int(t.get("CD")), as_int(t.get("KD"))
    mine = f"{cd_m}x{kd_m}" if (cd_m or kd_m) else ""
    erp = f"{cd_e}x{kd_e}" if (cd_e or kd_e) else ""
    if not (cd_m or kd_m):
        verdict = "我方留空" if (cd_e or kd_e) else "一致"
    elif not (cd_e or kd_e):
        verdict = "我方多写"
    elif sorted([cd_m or 0, kd_m or 0]) == sorted([cd_e or 0, kd_e or 0]):
        verdict = "一致"
    else:
        verdict = "真差异"
    note = ""
    if verdict == "一致" and mine and erp and mine != erp:
        note = "长宽换序（ERP 自身不统一，不算错）"
    elif cd_m and kd_m and cd_m != kd_m and cd_e == kd_m and kd_e == cd_m:
        note = "长宽互换"
    return {"字段": "尺寸(CDxKD)", "我方": mine, "ERP": erp, "判定": verdict, "备注": note}


def load_truth(path: Path) -> list[dict]:
    if not path.exists():
        sys.exit(f"真值文件不存在：{path}\n先建库（见 customer-files/reference-eml/README.md）")
    return json.loads(path.read_text(encoding="utf-8"))


def pick_truth(prod: dict, truth: list[dict], form: str | None,
               cands: list[str] | None = None) -> dict | None:
    def by_form(f: str):
        for t in truth:
            if str(t.get("表单编号", "")).upper() == f.upper() or \
               str(t.get("BDBH", "")).upper() == f.upper():
                return t
        return None

    if form:
        return by_form(form)
    if cands:
        for f in cands:
            t = by_form(f)
            if t:
                return t
    for key in ("BDBH", "KHDDH", "KHDH"):
        v = norm(prod.get(key)).upper()
        if not v:
            continue
        for t in truth:
            if norm(t.get(key)).upper() == v:
                return t
    return None


def compare(prod: dict, t: dict, lt: dict | None, with_cpms: bool) -> dict:
    rows, count = [], {"一致": 0, "我方留空": 0, "我方多写": 0, "真差异": 0, "参考": 0, "口径待定": 0}
    erp = dict(t)
    if lt:                     # 列表页更权威
        if lt.get("款数") not in (None, ""):
            erp["TZS"] = lt["款数"]
        if lt.get("语言数") not in (None, ""):
            erp["YYS"] = lt["语言数"]

    fields = list(COMPARE) + list(REF_ONLY) + (["CPMS"] if with_cpms else [])
    for f in fields:
        mine, truth_v = prod.get(f), erp.get(f)
        if same(f, mine, truth_v):
            verdict = "一致"
        elif not norm(mine):
            verdict = "我方留空"
        elif not norm(truth_v):
            verdict = "我方多写"
        else:
            verdict = "真差异"
        if f in REF_ONLY and verdict != "一致":
            verdict = "参考"
        count[verdict] += 1
        rows.append({"字段": f, "我方": mine, "ERP": truth_v, "判定": verdict})

    for row in [size_row(prod, erp)] + quantity_rows(prod, lt or {}):
        count[row["判定"]] += 1
        rows.append(row)
    return {"rows": rows, "count": count}


def run_one(path: Path, truth: list[dict], form: str | None, with_cpms: bool,
            manifest: dict | None = None, list_truth: dict | None = None) -> dict:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"产物": str(path), "错误": f"读不了：{e}"}
    prod = obj[0] if isinstance(obj, list) and obj else obj
    if not isinstance(prod, dict):
        return {"产物": str(path), "错误": "不是 59 键对象"}
    cands = None
    if manifest:
        if isinstance(manifest, dict) and "erp_forms" in manifest:
            cands = manifest["erp_forms"]
        else:
            ent = manifest.get(str(path)) or manifest.get(path.name) or {}
            cands = ent.get("erp_forms") if isinstance(ent, dict) else ent
    t = pick_truth(prod, truth, form, cands)
    if not t:
        return {"产物": str(path), "错误": "产物里没有可用的 BDBH/KHDDH/KHDH，对不上真值（可用 --form / --manifest 指定）"}
    lt = (list_truth or {}).get(t.get("表单编号"))
    res = compare(prod, t, lt, with_cpms)
    return {"产物": str(path), "表单编号": t.get("表单编号"), "香港单号": t.get("KHDDH"),
            "产品名称": t.get("CPMC"), **res}


FLAG = {"一致": "  ", "我方留空": "○ ", "我方多写": "＋ ", "真差异": "✗ ", "参考": "· ", "口径待定": "? "}


def print_human(r: dict) -> None:
    if "错误" in r:
        print(f"\n✗ {r['产物']}\n   {r['错误']}")
        return
    print(f"\n=== {r['表单编号']}（香港单号 {r['香港单号']}）")
    print(f"    产物：{r['产物']}")
    print(f"    ERP：{str(r.get('产品名称') or '')[:70]}")
    for row in r["rows"]:
        if row["判定"] == "一致":
            continue
        note = f"  {row.get('备注', '')}" if row.get("备注") else ""
        print(f"  {FLAG[row['判定']]}{row['字段']:9} 我方={norm(row['我方'])!r:26} ERP={norm(row['ERP'])!r:26} {row['判定']}{note}")
    c = r["count"]
    total = sum(c.values())
    print(f"    汇总：{total} 项 → 一致 {c['一致']} · 留空 {c['我方留空']} · 多写 {c['我方多写']}"
          f" · 真差异 {c['真差异']} · 参考 {c['参考']} · 口径待定 {c['口径待定']}"
          f"  （一致率 {c['一致'] / total * 100:.0f}%）")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="产物 × ERP 真实录入值 逐字段对账")
    ap.add_argument("paths", nargs="+", help="产物 JSON 文件或目录（目录递归找 ERP_*.json / *.erp.json）")
    ap.add_argument("--form", default=None, help="手工指定 ERP 表单编号（如 BJXQ260825007）")
    ap.add_argument("--manifest", default=None, help="JSON：{产物路径: {'erp_forms': [...]}}")
    ap.add_argument("--with-cpms", action="store_true", help="连 CPMS 产品描述一起比")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    ap.add_argument("--ref", default=str(SUMMARY), help="明细真值文件路径")
    ap.add_argument("--no-list", action="store_true", help="不读列表页真值（数量翻倍/款数就不比了）")
    args = ap.parse_args(argv)

    truth = load_truth(Path(args.ref))
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8")) if args.manifest else None
    list_truth = None
    if not args.no_list:
        p = Path(args.ref).parent / "list_truth.json"
        if p.exists():
            list_truth = json.loads(p.read_text(encoding="utf-8"))

    files: list[Path] = []
    for p in map(Path, args.paths):
        if p.is_dir():
            files += sorted(x for x in p.rglob("*.json")
                            if (x.name.startswith("ERP_") or x.name.endswith(".erp.json"))
                            and ".erp-import" not in x.name)
        elif p.exists():
            files.append(p)
        else:
            print(f"✗ 找不到 {p}", file=sys.stderr)
    if not files:
        print("✗ 没有可对账的产物文件", file=sys.stderr)
        return 2

    results = [run_one(f, truth, args.form, args.with_cpms, manifest, list_truth) for f in files]

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=1))
    else:
        for r in results:
            print_human(r)
        ok = [r for r in results if "count" in r]
        if len(ok) > 1:
            c = {k: sum(r["count"][k] for r in ok)
                 for k in ("一致", "我方留空", "我方多写", "真差异", "参考", "口径待定")}
            total = sum(c.values())
            print(f"\n── 合计 {len(ok)} 份产物 / {len(results) - len(ok)} 份对不上 ──")
            print(f"   {total} 项 → 一致 {c['一致']} · 留空 {c['我方留空']} · 多写 {c['我方多写']}"
                  f" · 真差异 {c['真差异']} · 参考 {c['参考']} · 口径待定 {c['口径待定']}"
                  f"  （一致率 {c['一致'] / total * 100:.0f}%）")

    bad = sum(r.get("count", {}).get("真差异", 0) for r in results)
    missing = sum(1 for r in results if "错误" in r)
    return 1 if (bad or missing) else 0


if __name__ == "__main__":
    sys.exit(main())
