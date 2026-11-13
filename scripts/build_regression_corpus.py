#!/usr/bin/env python3
"""建回归语料：9 份 zip 询价 × ERP 已有单。

ERP 口径：`erp_quote_list` 抓下来的快照 `customer-files/regression/erp_truth_snapshot.json`
          （刷新方式见 customer-files/regression/README.md），**不用 PDF**。
我方口径：`customer-files/processed/<task>/ERP_*.json`（59 键中文产物）。

用法：uv run python scripts/build_regression_corpus.py
"""
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PROC = REPO / "customer-files" / "processed"
OUT = REPO / "customer-files" / "regression"

TRUTH_PATH = OUT / "erp_truth_snapshot.json"
if not TRUTH_PATH.exists():
    TRUTH_PATH = Path("/tmp/erp_truth.json")
TRUTH = json.loads(TRUTH_PATH.read_text(encoding="utf-8"))

# 询价单号 → 我方任务目录（2026-11-10 那批产物；xlsx 直喂 main.py，多数没落单号）
TASK = {
    "CU26082501": "1ee0ff318db745ef9b119dc95429078b",
    "CU26082801": "3b9b4a5d34714a478dcc10a41a94fc6f",
    "CU26092401": "f247365adc7c4dd5810145296cbe965f",
    "CU26100601": "3fdfd3e1520541b09968cdfee9aa4084",
    "WQ26100601": "b3344b66816648c8960374f7ddcbbdc7",
    "LG26070701-01": "ec284997eafb4ebaa2134ee1dcdbb97c",
    "LG26080302": "f3f5fcc4efce4e2f9fb39507a8992856",
    "LG26082601": "4ab604bc6fec406bbd2e4ff98ccf5edd",
    "LG26090101": "74f94a32b2b844268c6259508e4b427c",
}

ERP_NO = {
    "CU26082501": "BJXQ260825007", "CU26082801": "BJXQ260828001",
    "CU26092401": "BJXQ260924004", "CU26100601": "BJXQ261006005",
    "WQ26100601": "BJXQ261006007", "LG26070701-01": "BJXQ260707001-01",
    "LG26080302": "BJXQ260803007", "LG26082601": "BJXQ260826002",
    "LG26090101": "BJXQ260901002",
}

# ERP 侧归我方「不推测」的字段（业务在 ERP 里手填，脚本故意留空）
BIZ_FILLED = {"交货日期 JHRQ", "预计下单 WCRQ"}


def load_task(tid):
    for art in sorted((PROC / tid).glob("ERP_*.json")):
        if "erp-import" in art.name or art.name.endswith(".erp-push.json"):
            continue
        obj = json.loads(art.read_text(encoding="utf-8"))
        return {"task": tid, "file": str(art.relative_to(REPO)),
                "record": obj[0] if isinstance(obj, list) else obj}
    return {}


def cat_name(v):
    return str(v).split(";")[-1].strip() if v else ""


def num(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


CMP = [
    ("产品类别 CPLB", lambda o: cat_name(o.get("CPLB")), lambda e: e.get("产品类别")),
    ("装订方式 ZDFS", lambda o: o.get("ZDFS"), lambda e: e.get("装订方式")),
    ("款数 TZS", lambda o: num(o.get("TZS")), lambda e: num(e.get("款数"))),
    ("语言数 YYS", lambda o: num(o.get("YYS")), lambda e: num(e.get("语言数"))),
    ("数量 CPSL1", lambda o: num(o.get("CPSL1")), lambda e: num(e.get("最小值"))),
    ("规格 CD", lambda o: num(o.get("CD")), lambda e: num((e.get("规格") or "*").split("*")[0])),
    ("规格 KD", lambda o: num(o.get("KD")), lambda e: num((e.get("规格") or "*").split("*")[1])),
    ("交货日期 JHRQ", lambda o: o.get("JHRQ"), lambda e: e.get("交货日期")),
    ("预计下单 WCRQ", lambda o: o.get("WCRQ"), lambda e: e.get("预计下单日期")),
    ("是否有配件 SFYPJ", lambda o: o.get("SFYPJ"),
     lambda e: {"是": True, "否": False}.get(e.get("是否有配件"))),
]

corpus = []
for no, tid in TASK.items():
    o = load_task(tid)
    erp_no = ERP_NO[no]
    e = next((c[erp_no] for c in TRUTH.values() if erp_no in c), None)
    row = {"单号": no, "erp_单号": erp_no, "erp_记录ID": (e or {}).get("记录ID"),
           "我方任务目录": tid, "字段对比": []}
    if not o or not e:
        row["缺失"] = "我方产物缺失" if not o else "ERP 记录缺失"
        corpus.append(row)
        continue
    orec = o["record"]
    row["我方单号"] = {"KHDH": orec.get("KHDH") or "", "KHDDH": orec.get("KHDDH") or ""}
    row["ERP_香港单号"] = e.get("香港单号")
    for label, fo, fe in CMP:
        ov, ev = fo(orec), fe(e)
        if ov in (None, "", 0):
            kind = "我方留空"
        elif str(ov).strip().lower() == str(ev).strip().lower():
            kind = "一致"
        else:
            kind = "真差异"
        row["字段对比"].append({"字段": label, "我方": ov, "ERP": ev, "判定": kind})
    corpus.append(row)

OUT.mkdir(parents=True, exist_ok=True)
(OUT / "erp_truth_snapshot.json").write_text(
    json.dumps(TRUTH, ensure_ascii=False, indent=2), encoding="utf-8")
(OUT / "corpus.json").write_text(
    json.dumps(corpus, ensure_ascii=False, indent=2), encoding="utf-8")

stat = {"一致": 0, "我方留空": 0, "真差异": 0}
for r in corpus:
    for c in r.get("字段对比", []):
        stat[c["判定"]] += 1

L = ["# 回归语料：9 份 zip 询价 × ERP 已有单", "",
     "> **ERP 口径**：`erp_quote_list` 抓取快照（2026-11-13），见 `erp_truth_snapshot.json`。",
     "> 不用 PDF —— PDF 要 OCR、且字段比列表页少。",
     "> **我方口径**：`customer-files/processed/<task>/ERP_*.json`（59 键中文产物）。",
     "> 机器可读版 `corpus.json`。", "",
     f"## 总览：{stat['一致']} 一致 / {stat['我方留空']} 我方留空 / {stat['真差异']} 真差异"
     f"（共 {sum(stat.values())}）", "",
     "说明：「我方留空」里 `JHRQ`/`WCRQ` 是脚本**故意不推测**（业务在 ERP 手填），算预期；",
     "`YYS`/`CPSL1`/`CD`/`KD` 留空则是**抽取漏了**，属真缺陷。", "",
     "## 逐单对账", ""]

for r in corpus:
    L += [f"### `{r['单号']}` → ERP `{r['erp_单号']}`", ""]
    if "缺失" in r:
        L += [f"- ❌ {r['缺失']}", ""]
        continue
    kh = r["我方单号"]
    if kh["KHDH"] or kh["KHDDH"]:
        L.append(f"- 我方产物单号：`KHDH={kh['KHDH']}` `KHDDH={kh['KHDDH']}`")
    else:
        L.append("- ⚠️ 我方产物**没落单号**（xlsx 直喂 `main.py`，无邮件上下文）→ 本表按内容人工对号")
    L.append(f"- ERP 记录ID：`{r['erp_记录ID']}`")
    L.append("")
    L.append("| 字段 | 我方 | ERP | 判定 |")
    L.append("|---|---|---|---|")
    for c in r["字段对比"]:
        mark = {"一致": "✅", "真差异": "❌", "我方留空": "⚠️"}[c["判定"]]
        L.append(f"| {c['字段']} | `{c['我方']}` | `{c['ERP']}` | {mark} {c['判定']} |")
    L.append("")

L += ["## 真差异清单（要改代码的）", ""]
for r in corpus:
    diffs = [c for c in r.get("字段对比", []) if c["判定"] == "真差异"]
    if diffs:
        items = "；".join(f"{c['字段']} 我方`{c['我方']}`≠ERP`{c['ERP']}`" for c in diffs)
        L.append(f"- `{r['单号']}`：{items}")

L += ["", "## 差异归因（逐条判过，别照单全收）", "",
      "| 单号 | 字段 | 归因 | 性质 |", "|---|---|---|---|",
      "| CU26082501 | CPLB 普通YO书 ≠ 精装YO日历 | 类别树取值错：客户档案把 "
      "`Aufstellbücher` 归到 26.07 普通YO书，业务录成「精装YO日历」（有三角纸座 = 精装） | **真缺陷** |",
      "| CU26082501 | CD/KD 165/120 ≠ 120/165 | 客户写 `165x120`，我方按**书写顺序**落 CD=165；"
      "ERP 录 `120*165`（业务按「宽×高」读） | **口径冲突，待森西裁定** |",
      "| CU26082801 | CPLB 冲形状无线胶装书 ≠ 冲形状骑钉书 | 邮件写 `safety stitching` 被翻成胶装；"
      "应命中「冲形状骑钉书」 | **真缺陷** |",
      "| CU26082801 | TZS 1 ≠ 2 | 客户 `2x 3000 cps` / `This title is a reprint, the other one is new` "
      "= 2 款，我方只抽到 1 | **真缺陷** |",
      "| CU26092401 | TZS 1 ≠ 4 | xlsx 里 4 款，只抽到 1 | **真缺陷** |",
      "| CU26092401 | CPSL1 300 ≠ 12000 | 数量级错（疑把某档位当总量） | **真缺陷** |",
      "| WQ26100601 | CPLB 普通BB书 ≠ 精装砌图BB书 | 客户写 `Board book binding, 2.0 mm boards, "
      "puzzle die-cut` = 精装砌图BB书；我方落到最外层普通BB书 | **真缺陷** |",
      "| LG26082601 | CPLB 冲形状无线胶装书 ≠ 冲形状骑钉书 | 同 CU26082801，`Softcover, safety "
      "stitching, shape die-cut` 应命中冲形状骑钉书 | **真缺陷** |",
      "| LG26082601 | TZS 1 ≠ 2 | 客户 `School gift bags (2-titles)`，只抽到 1 | **真缺陷** |",
      "| LG26090101 | CPLB 冲形状普通BB书 ≠ 普通BB书 | 客户 `2 rounded corners` 被当成 `shape die-cut`？"
      "我方给的是**不带「冲形状」的**普通BB书，方向相反 | **真缺陷** |",
      "| LG26090101 | TZS 8 ≠ 4；CPSL1 110000 ≠ 56722 | ERP **拆成主单+子单各 4 款**，合计 8 款；"
      "我方按一封询价合并成 8 款（对），但数量取了 110000 而非 56722×2=113444 | **口径差异 + 数量待核** |",
      "| LG26070701-01 | TZS 10 ≠ 6；YYS 10 ≠ 4 | **不是抽取错**：该目录里放的其实是 `LG26090401` 的询价邮件"
      "（10 titles 10 languages，正文指纹与 LG26090401 完全一致）；ERP 那张 6款4语 的单来自**另一封我们手上没有的邮件** | **语料错配，非缺陷** |",
      ]

(OUT / "corpus.md").write_text("\n".join(L), encoding="utf-8")

print(f"→ {OUT}   一致 {stat['一致']} / 留空 {stat['我方留空']} / 真差异 {stat['真差异']}")
for r in corpus:
    d = [c for c in r.get("字段对比", []) if c["判定"] == "真差异"]
    if d:
        print(f"  {r['单号']:14} ❌ {', '.join(c['字段'] for c in d)}")
