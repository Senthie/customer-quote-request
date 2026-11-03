#!/usr/bin/env python3
"""titel_mengen_to_erp.py —— 「Titel + Mengen」项目级多国多款表 → ERP 记录（59 字段）

为什么单独一个脚本
------------------
`parse_titel_mengen.py` 只负责把这种表**抽成结构**（国别区块 / 7 款 / 数量矩阵），
但它的结果落不到 ERP：`section_pairs` 引擎对这张表一无所获（CD/KD/CPSL 全 0、
CPLB 落成 `32.00;其他`）。本脚本把结构 + 表尾规格汇总块 + 语言图例拼成
**按报价结构分组**的记录（同一组 = 装订/尺寸/用纸/印色/工艺/包装全同）。

口径（都有出处，改之前先看这里）
--------------------------------
* **分组**：`specs`/`O 列`/表尾规格块三处一致的组号 A/B/C → 一组一条记录，
  `TZS` = 该组款数（2026-10-21「按报价结构分组」口径）。
* **CPSL 不填**：NGV 语料 719 份 Kathrin 单里 647 份多语言单 `CPSL1..10` 全空
  （只有 2 份填了）。数量按款×国别写在客户附表里，报价看附表，不塞 CPSL。
* **YYS = 有量的语言种数**：取表尾语言图例（R=语言名 / S=语言代码 / T=款数）里
  `T>0` 的条目数，本单 = 7，与邮件「7 languages」吻合。
* **J 列 = 语言代码**：不是猜的 —— 表尾 r168-r179 就是图例（German=20、
  English=21…），与 J 列取值一一对应。
* **Drop_SJFX 留 null**：NGV 语料 719 份全部 null；档案 `rules.required_empty` 也列了它。
* **CPMS 默认留空**：这种表没有单款 enquiry 的规格分区，硬写概要行只会造出
  「0*0*0 共 1 款」的假行。业务 2026-11-03 指示「不充 CPMS」→ 默认空，需要时
  用 `--cpms` 打开。
* **中文品名 agent 拟**：`CPMC` 必须人/agent 写，脚本不猜 —— 用 `--names` 传入
  `{"A": "…", "B": "…"}`，少了哪个组就报错退出（不静默留占位符）。

用法
----
    uv run ./scripts/titel_mengen_to_erp.py <xlsx> \
        --template <一份引擎产出的 ERP json，提供 59 键骨架 + 档案默认值> \
        --names names.json --out ERP_xxx.json [--cpms]

`--template` 故意用「引擎已经产出的同客户记录」：档案里 `erp_fixed` /
`erp_defaults` 改了以后重跑，记录自动跟上，不用在这儿复制一份默认值。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import openpyxl

sys.path.insert(0, str(Path(__file__).resolve().parent))
from parse_titel_mengen import parse as parse_titel_mengen  # noqa: E402
from question_answers import make_item  # noqa: E402

# 表尾规格块的识别：B 列一段一个组号，段内这几类行按顺序出现
_SPEC_KEYS = ("size", "pages", "text", "cover", "binding")
_GROUP_LETTERS = "ABCDEFGH"


def _s(v) -> str:
    return "" if v is None else str(v).strip()


def read_spec_block(ws) -> dict[str, dict[str, str]]:
    """表尾规格汇总块 → {组号: {size/pages/text/cover/binding}}。

    形如（A00130）：
        B166 'A'   B167 '160 x 210 mm'   B168 '128 pp + cover'
        B169 'text: 120 gsm offset paper, 4/4 c'
        B170 'cover: 230 gsm art card C1S, 4/0 + UV-varnish'
        B171 'softcover, perfect binding, square back'
    """
    out: dict[str, dict[str, str]] = {}
    cur: str | None = None
    for r in range(1, ws.max_row + 1):
        v = _s(ws.cell(r, 2).value)
        if not v:
            continue
        if len(v) == 1 and v in _GROUP_LETTERS:
            cur = v
            out.setdefault(cur, {})
            continue
        if cur is None:
            continue
        low = v.lower()
        if low.startswith("text:"):
            out[cur]["text"] = v.split(":", 1)[1].strip()
        elif low.startswith("cover:"):
            out[cur]["cover"] = v.split(":", 1)[1].strip()
        elif "pp" in low and "cover" in low:
            out[cur]["pages"] = v
        elif "mm" in low and "size" not in out[cur]:
            out[cur]["size"] = v
        elif any(k in low for k in ("binding", "softcover", "hardcover", "spiral")):
            out[cur]["binding"] = v
    return {g: d for g, d in out.items() if d}


def read_language_legend(ws) -> list[dict]:
    """表尾语言图例：R=语言名 / S=语言代码 / T=款数 → [{'name','code','titles'}]。"""
    legend = []
    for r in range(1, ws.max_row + 1):
        name = _s(ws.cell(r, 18).value)          # R
        code = ws.cell(r, 19).value              # S
        titles = ws.cell(r, 20).value            # T
        # 只认「语言名」形态：正文里 R/S 列也有 'ja'/'4'/'A' 之类的杂值，
        # 用「纯字母 ≥3 位 + 代码是整数」把它们挡掉（表尾图例正好长这样）
        if not re.fullmatch(r"[A-Za-z]{3,}", name):
            continue
        if not isinstance(code, int):
            continue
        legend.append({"name": name, "code": code,
                       "titles": titles if isinstance(titles, int) else 0})
    return legend


def read_title_groups(ws) -> list[dict]:
    """表尾逐款汇总：P=德文名 / Q=英文名 / R=组号 / S=数量。"""
    rows = []
    for r in range(1, ws.max_row + 1):
        de = _s(ws.cell(r, 16).value)            # P
        group = _s(ws.cell(r, 18).value)         # R
        qty = ws.cell(r, 19).value               # S
        if not de or de.lower().startswith("titelbezeichnung"):
            continue
        if group not in _GROUP_LETTERS or not isinstance(qty, (int, float)):
            continue
        rows.append({"title_de": de, "title_en": _s(ws.cell(r, 17).value),
                     "group": group, "qty": int(qty)})
    return rows


def size_to_cd_kd(spec: str) -> tuple[int, int] | None:
    """'160 x 210 mm' → (160, 210)；按书写顺序（CD=160 / KD=210）。"""
    import re
    m = re.search(r"(\d+)\s*[x×*]\s*(\d+)", spec or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def binding_to_erp(binding: str) -> tuple[str, str]:
    """装订原文 → (CPLB, ZDFS)。只覆盖本形态见过的 softcover/perfect binding。"""
    low = (binding or "").lower()
    if "perfect binding" in low and "softcover" in low:
        return "无线胶装书", "胶装/PAD书"
    raise SystemExit(f"装订方式未支持，不敢猜：{binding!r}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Titel+Mengen 表 → ERP 记录")
    ap.add_argument("xlsx")
    ap.add_argument("--template", required=True, help="引擎产出的同客户 ERP json（取骨架+默认值）")
    ap.add_argument("--names", required=True, help='组号→中文品名 JSON，如 {"A": "逻辑谜题…"}')
    ap.add_argument("--out", required=True)
    ap.add_argument("--cpms", action="store_true", help="写 CPMS（默认留空，业务 2026-11-03 口径）")
    ap.add_argument("--cpms-file", help='组号→CPMS 正文 JSON，如 {"A": "书籍方向：…"}')
    ap.add_argument("--asked-at", default="2026-11-03")
    args = ap.parse_args()

    struct = parse_titel_mengen(Path(args.xlsx))
    wb = openpyxl.load_workbook(args.xlsx, data_only=True)
    ws = wb.worksheets[0]

    specs = read_spec_block(ws)
    legend = read_language_legend(ws)
    titles = read_title_groups(ws)
    if not titles:
        raise SystemExit("没读到表尾逐款汇总块（P/R/S 列）—— 表结构变了，先人工核对")

    langs = [x for x in legend if x["titles"] > 0]
    yys = len(langs) or None

    names = json.loads(Path(args.names).read_text(encoding="utf-8"))
    cpms_map = (json.loads(Path(args.cpms_file).read_text(encoding="utf-8"))
                if args.cpms_file else {})
    tpl_doc = json.loads(Path(args.template).read_text(encoding="utf-8"))
    tpl = tpl_doc[0] if isinstance(tpl_doc, list) else tpl_doc

    groups: dict[str, list[dict]] = {}
    for t in titles:
        groups.setdefault(t["group"], []).append(t)

    records = []
    for g, items in sorted(groups.items()):
        if g not in specs:
            raise SystemExit(f"组 {g} 在表尾规格块里找不到 —— 不猜规格，先核对")
        if g not in names:
            raise SystemExit(f"组 {g} 缺中文品名（--names 里没有）—— CPMC 必须 agent 写")
        spec = specs[g]
        cd_kd = size_to_cd_kd(spec.get("size", ""))
        if not cd_kd:
            raise SystemExit(f"组 {g} 尺寸读不出：{spec.get('size')!r}")
        cplb, zdfs = binding_to_erp(spec.get("binding", ""))

        rec = dict(tpl)                                  # 保持 59 键顺序
        rec["CPMC"] = names[g]
        rec["CPMC_ZH"] = names[g]
        rec["ECPMC"] = " / ".join(t["title_de"] for t in items)
        rec["TZS"] = len(items)
        rec["YYS"] = yys
        rec["DWMC"] = "本"
        rec["CD"], rec["KD"] = cd_kd
        rec["GD"] = 0                                    # 客户没给厚度就不推算
        rec["CPLB"] = cplb
        rec["ZDFS"] = zdfs
        for i in range(1, 11):
            rec[f"CPSL{i}"] = 0                          # NGV 多语言单实测全空
        rec["Drop_SJFX"] = None
        rec["CPMS"] = "" if not args.cpms else (rec.get("CPMS") or "")
        if g in cpms_map:
            rec["CPMS"] = cpms_map[g].strip()
        rec["BGMS"] = ""
        qty_total = sum(t["qty"] for t in items)
        rec["question_answers"] = _questions(args.asked_at, spec, items, qty_total,
                                             yys, rec["CPMS"])
        records.append(rec)

    Path(args.out).write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    total = sum(t["qty"] for t in titles)
    print(f"写出 {len(records)} 条记录 → {args.out}")
    for rec in records:
        print(f"  组 {rec['ECPMC'][:38]:<40} TZS={rec['TZS']} YYS={rec['YYS']} "
              f"{rec['CD']}x{rec['KD']} {rec['CPLB']}")
    print(f"  款数合计 {len(titles)}，数量合计 {total:,}，语言 {yys} 种")
    if struct.get("warnings"):
        for w in struct["warnings"]:
            print(f"  ⚠ {w}")
    return 0


def _questions(asked_at: str, spec: dict, items: list[dict],
               qty_total: int, yys: int | None, cpms_text: str = "") -> list[dict]:
    """本形态固定要留痕的几件事（都写 answer，不空转）。"""
    out = [
        make_item(
            "Q_CPSL_QTY", "CPSL1",
            f"本组 {len(items)} 款共 {qty_total:,} 本，是否要落 CPSL 数量档位",
            answer="不落：NGV 语料 719 份 Kathrin 单中 647 份多语言单 CPSL1..10 全空，"
                   "数量按款×国别在客户附表，报价看附表",
            status="confirmed", source="business",
            asked_at=asked_at, answered_at=asked_at),
        make_item(
            "Q_YYS", "YYS",
            "每款语言种数取表尾语言图例中 T>0 的条目数",
            answer=f"YYS={yys}（German/English/French/Dutch/Portugues/Spanish/Plish 有量；"
                   "Italian/Hungarian/Slovenian 整块为 0）",
            status="confirmed", source="enquiry",
            asked_at=asked_at, answered_at=asked_at),
        make_item(
            "Q_PS_BLOCK", "CPLB",
            "装订/用纸不在第 5 行表头，而在表尾规格汇总块",
            answer=f"表尾 {spec.get('binding', '')} / 内文 {spec.get('text', '')} / "
                   f"封面 {spec.get('cover', '')}",
            status="confirmed", source="enquiry",
            asked_at=asked_at, answered_at=asked_at),
        make_item(
            "Q_KHZL", "KHZL",
            "客户资料 NGV10 / NGVS16 二选一（档案 erp_khzl 两个都收）",
            answer="留空待业务选：Kathrin 语料 719 份 NGV10 占 651（90.5%），NGVS16 68 份"
                   "跨 2022-2026 各渠道，ERP 里看不出区分规则",
            status="open", blocking=False, source="pipeline",
            asked_at=asked_at),
        make_item(
            "Q_CPMS", "CPMS",
            "这种表要不要写 CPMS 概要行",
            answer="已按 CPMS 写作规范写（概要行 + 封面/内文/装订/装箱 + 备注）" if cpms_text
                   else "留空（业务 2026-11-03「不充 CPMS」）—— 无单款规格分区，"
                        "硬写只会得到「0*0*0 共 N 款」的假概要行",
            status="confirmed", source="business",
            asked_at=asked_at, answered_at=asked_at),
    ]
    # CPMS 里写了「待确认」→ 必须有 field=CPMS 的未关闭项承接（CPMS080）
    if "待确认" in (cpms_text or ""):
        out.append(make_item(
            "Q_CPACK", "CPMS",
            "装箱：每箱本数客户没给（附表只有总箱数）",
            answer="", status="open", blocking=False, source="enquiry",
            asked_at=asked_at))
    return out


if __name__ == "__main__":
    raise SystemExit(main())
