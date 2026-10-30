# -*- coding: utf-8 -*-
"""按「报价结构」分组（2026-10-29 森西拍板）。

背景：ERP 导出改成数组以后，一封询价里的多个产品要各占一条记录。
分组口径由业务定成「**按报价结构**」：

    装订 / 尺寸 / 用纸 / 印色 / 表面工艺 / 包装 —— 任何一个不同 → 拆成两条；
    全部相同 → 合成一条，用 TZS 记款数、CPSL 记这一组的印刷总量。

本文件钉住这些行为：

1. 单产品询价**行为完全不变**（1 组、值不变、数量档位不丢）—— 这是回归底线；
2. 同结构多款合并：TZS = 款数，数量/档位求和，versions 重算；
3. 结构不同必拆：装订、尺寸、用纸、推导出的 CPLB 各一条判据；
4. 写法差异（`210 x 280` / `210x280`）不能造出假差异；
5. 元数据（品名/备注）只拼接、不参与签名；
6. 缺值的条目要继承整份询价的值，不能凭空拆单；
7. Lidl `Anlage` 表逐款产出条目；
8. `product_table` 策略默认关闭，显式打开才产出条目。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from customer_profile import load_profile  # noqa: E402
from extract_fields import CustomerData, _extract_lidl_anlage, _extract_rows  # noqa: E402
from product_groups import (  # noqa: E402
    build, entries_from, load_config, norm, summarize, to_customer_data,
)

NGV = load_profile("ngv")
TEMPLATE = json.loads((REPO_ROOT / "assets" / "add_customer_quote_request.json")
                      .read_text(encoding="utf-8"))


class _Quiet:
    def info(self, *a, **k):
        pass

    warn = error = debug = info

    def section(self, *a, **k):
        pass

    def field_mapping(self, *a, **k):
        pass

    def elapsed_time(self):
        return 0.0


def _entry(label: str, **values) -> dict:
    quantity = values.pop("quantity", None)
    titles = values.pop("titles", 1)
    return {"label": label, "values": dict(values), "quantity": quantity,
            "titles": titles, "source": f"test!{label}"}


# ── 归一化 ───────────────────────────────────────────────────────────────
def test_归一化只统一写法不改含义():
    assert norm("210 x 280 mm") == norm("210×280mm") == norm("210X280  MM")
    assert norm("  精装书 ; ") == norm("精装书")
    # 不同含义不能归一（否则会把两种结构并成一条）
    assert norm("210 x 280 mm") != norm("210 x 297 mm")
    assert norm("100g 书纸") != norm("120g 书纸")


def test_配置能读出来且含关键结构键():
    cfg = load_config()
    assert "binding" in cfg["keys"] and "size" in cfg["keys"]
    assert "product_name" not in cfg["keys"], "品名是元数据，不能进结构签名"


# ── ① 单产品：行为必须与改动前一致 ──────────────────────────────────────
def test_单产品不分组且档位不丢():
    data = CustomerData(values={"size": "210 x 280 mm", "binding": "perfect binding",
                                "quantity": "5000"},
                        quantity_tiers=[3000, 5000, 7500])
    groups = build(data, NGV, _Quiet())

    assert len(groups) == 1
    assert groups[0].titles == 1
    assert groups[0].aggregated is False
    assert groups[0].values["size"] == "210 x 280 mm"
    assert groups[0].values["quantity"] == "5000"
    assert groups[0].quantity_tiers == [3000, 5000, 7500], "整份询价的档位不能丢"


# ── ② 同结构多款：合并成一条，TZS 记款数 ────────────────────────────────
def test_同结构两款合并_TZS记款数_数量求和():
    data = CustomerData(
        values={"size": "210 x 280 mm", "binding": "perfect binding",
                "text_material": "100g 书纸", "extent": "24",
                "product_name": "Series Name", "notes": "交期 KW 12"},
        products=[
            _entry("Book One", quantity=5000),
            _entry("Book Two", quantity=7500),
        ],
    )
    groups = build(data, NGV, _Quiet())

    assert len(groups) == 1, [g.label for g in groups]
    group = groups[0]
    assert group.aggregated is True
    assert group.titles == 2, "两款同结构 → 一条记录，TZS=2"
    assert group.quantity == 12500, "数量要按款求和"
    assert group.values["versions"].startswith("2 titles")
    assert group.values["number_of_titles"] == "2"
    # 整份询价共用的值原样保留
    assert group.values["product_name"] == "Series Name"
    assert group.values["size"] == "210 x 280 mm"


def test_同结构两款_走_map_fields后TZS与CPSL正确():
    from main import map_fields

    data = CustomerData(
        values={"size": "210 x 280 mm", "binding": "softcover, perfect binding",
                "extent": "24 pp", "quantity": "12500"},
        products=[_entry("A", quantity=5000), _entry("B", quantity=7500)],
    )
    groups = build(data, NGV, _Quiet())
    result = map_fields(to_customer_data(groups[0], data), TEMPLATE, NGV, _Quiet())

    assert result["TZS"] == 2, result["TZS"]
    assert result["CPSL1"] == 12500, result["CPSL1"]


# ── ③ 结构不同必拆 ──────────────────────────────────────────────────────
@pytest.mark.parametrize("key,value_a,value_b", [
    ("binding", "perfect binding", "saddle stitching"),
    ("size", "210 x 280 mm", "170 x 240 mm"),
    ("text_material", "100g 书纸", "120g 哑粉"),
    ("specials", "sound module", "light module"),
    ("finishes", "matte lamination", "gloss lamination"),
])
def test_结构键不同必拆成两条(key, value_a, value_b):
    base = {"size": "210 x 280 mm", "binding": "perfect binding"}
    data = CustomerData(
        values=dict(base),
        products=[
            _entry("A", quantity=3000, **{key: value_a}),
            _entry("B", quantity=3000, **{key: value_b}),
        ],
    )
    groups = build(data, NGV, _Quiet())
    assert len(groups) == 2, f"{key} 不同却没拆开: {[g.label for g in groups]}"
    assert [g.titles for g in groups] == [1, 1]
    assert sum(g.quantity for g in groups) == 6000


def test_推导出的CPLB不同也要拆():
    """装订写法不同、字面键归一后也许相同，但 resolve_cplb 推导出的类别不同。"""
    data = CustomerData(
        values={"size": "210 x 280 mm"},
        products=[_entry("A", binding="perfect binding"),
                  _entry("B", binding="perfect binding")],
    )

    def resolver(values):
        return "23.01;无线胶装书" if "A" not in values.get("_label", "") else "24.01;骑马钉书"

    # 用真实推导：两款结构完全相同 → 不传 resolver 时 1 组
    assert len(build(data, NGV, _Quiet())) == 1
    # 人为让推导结果不同 → 必须拆（模拟「装订一句之差换类别」）
    calls = {"n": 0}

    def fake(values):
        calls["n"] += 1
        return "23.01;无线胶装书" if calls["n"] % 2 else "24.01;骑马钉书"

    assert len(build(data, NGV, _Quiet(), cplb_resolver=fake)) == 2


# ── ④ 写法差异不能造假差异 ──────────────────────────────────────────────
def test_同尺寸不同写法仍算同一结构():
    data = CustomerData(
        values={},
        products=[_entry("A", size="210 x 280 mm", quantity=1000),
                  _entry("B", size="210×280mm", quantity=1000)],
    )
    groups = build(data, NGV, _Quiet())
    assert len(groups) == 1
    assert groups[0].titles == 2


# ── ⑤ 元数据只拼接 ──────────────────────────────────────────────────────
def test_备注与品名拼接不拆单():
    data = CustomerData(
        values={"size": "210 x 280 mm", "binding": "perfect binding"},
        products=[_entry("A", product_name="Book One", quantity=1000),
                  _entry("B", product_name="Book Two", quantity=1000)],
    )
    groups = build(data, NGV, _Quiet())
    assert len(groups) == 1
    assert groups[0].values["product_name"] == "Book One / Book Two"


# ── ⑥ 缺值继承整份询价 ─────────────────────────────────────────────────
def test_条目没写的键继承整份询价的值_不虚拆():
    data = CustomerData(
        values={"size": "210 x 280 mm", "binding": "perfect binding"},
        products=[_entry("A", size="210 x 280 mm"),
                  _entry("B")],                       # B 的尺寸留空（= 同上）
    )
    groups = build(data, NGV, _Quiet())
    assert len(groups) == 1, "留空应继承整份询价的值，不能当成另一种结构"


# ── ⑦ 数量档位 ─────────────────────────────────────────────────────────
def test_档位逐档求和():
    data = CustomerData(values={"size": "210 x 280 mm", "binding": "perfect binding"})
    data.products = []
    groups = build(data, NGV, _Quiet(),
                   config={"keys": ("size", "binding"), "join_keys": ("product_name",),
                           "sum_keys": ("quantity",), "must_agree": ()})
    assert len(groups) == 1

    # 手工造两个带档位的条目
    a = _entry("A")
    a["quantity_tiers"] = [3000, 5000]
    b = _entry("B")
    b["quantity_tiers"] = [2000, 4000]
    data2 = CustomerData(values={"size": "210 x 280 mm", "binding": "perfect binding"},
                         products=[a, b])
    group = build(data2, NGV, _Quiet())[0]
    assert group.quantity_tiers == [5000, 9000], group.quantity_tiers


def test_档位长度不一致取第一组并留痕():
    a = _entry("A")
    a["quantity_tiers"] = [3000, 5000]
    b = _entry("B")
    b["quantity_tiers"] = [2000]
    data = CustomerData(values={"size": "210 x 280 mm", "binding": "perfect binding"},
                        products=[a, b])
    group = build(data, NGV, _Quiet())[0]
    assert group.quantity_tiers == [3000, 5000]
    assert any("档位" in n for n in group.notes), group.notes


# ── ⑧ Lidl Anlage 逐款条目 ──────────────────────────────────────────────
def _lidl_rows(titles: int = 2) -> list:
    def row(**kw):
        cells = [""] * 20
        for letter, value in kw.items():
            cells["ABCDEFGHIJKLMNOPQRST".index(letter)] = value
        return cells

    rows = [
        row(H="IAN 574239 K Motiv Puzzlebücher Overview of Countries, Quantities, and Titles"),
        row(A="Griechenland:"),
        row(A="ArtNr.", F="article:", H="title /style", J="quantity / title", O="dimension"),
    ]
    for idx in range(titles):
        letter = "ABCDE"[idx]
        rows.append(row(D="5/", E="28/", F=f"574239_{letter}",
                        H=f"Title {letter}", J=str(1000 * (idx + 1)),
                        O="25 x 25,3 x 2,3 cm", P="Cover + 5 Spreads + Rigid box",
                        S="Cover 350gsm C1S"))
    return rows


def test_Lidl逐款条目_一款一条():
    entries: list = []
    _extract_lidl_anlage(_lidl_rows(2), NGV, sheet_name="Anlage_574239",
                         entries_out=entries)

    assert len(entries) == 2
    assert entries[0]["label"].endswith("Title A")
    assert entries[0]["quantity"] == 1000
    assert entries[1]["quantity"] == 2000
    assert entries[0]["values"]["size"] == "250 x 253 x 23 mm", "cm→mm 逐款换算"
    assert "Rigid box" in entries[0]["values"]["specials"]
    assert entries[0]["source"].startswith("Anlage_574239!A")


def test_Lidl三款两种结构_拆两条记录():
    """IAN 574239 的真实形态：3 款滑动发声 + 2 款揭页发声 → 两条记录。"""
    def row(**kw):
        cells = [""] * 20
        for letter, value in kw.items():
            cells["ABCDEFGHIJKLMNOPQRST".index(letter)] = value
        return cells

    rows = [
        row(H="IAN 574239 K Sound Books Overview of Countries, Quantities, and Titles"),
        row(A="Griechenland:"),
        row(A="ArtNr.", F="article:", H="title /style", J="quantity / title", O="dimension"),
        row(F="574239_A", H="Slide A", J="1000", O="25 x 25,3 x 2,3 cm",
            P="Cover + sound module + 2mm chipboard", S="Cover 350gsm C1S"),
        row(F="574239_B", H="Slide B", J="1000", O="25 x 25,3 x 2,3 cm",
            P="Cover + sound module + 2mm chipboard", S="Cover 350gsm C1S"),
        row(F="574239_C", H="Slide C", J="1000", O="25 x 25,3 x 2,3 cm",
            P="Cover + sound module + 2mm chipboard", S="Cover 350gsm C1S"),
        row(F="574239_D", H="Flap D", J="500", O="25 x 25,3 x 2,3 cm",
            P="Cover + flaps, no module", S="Cover 350gsm C1S"),
        row(F="574239_E", H="Flap E", J="500", O="25 x 25,3 x 2,3 cm",
            P="Cover + flaps, no module", S="Cover 350gsm C1S"),
    ]
    entries: list = []
    values, _, _, _ = _extract_lidl_anlage(rows, NGV, sheet_name="Anlage_574239",
                                           entries_out=entries)
    assert values["quantity"] == "4000", "整份合计仍是 4000"

    data = CustomerData(values=values, products=entries)
    groups = build(data, NGV, _Quiet())
    assert len(groups) == 2, [g.label for g in groups]
    assert sorted(g.titles for g in groups) == [2, 3]
    assert sorted(g.quantity for g in groups) == [1000, 3000]
    assert sum(g.quantity for g in groups) == 4000, "分组不能凭空加/减数量"


# ── ⑨ 浇回 CustomerData ────────────────────────────────────────────────
def test_合并组不再继承整份询价的总量():
    """`scanned` 里的总量是整封询价的 —— 留在组里会让每组都拿到同一个总数。"""
    data = CustomerData(values={"size": "210 x 280 mm", "binding": "perfect binding"},
                        scanned={"quantity": "99999"},
                        products=[_entry("A", quantity=1000, size="210 x 280 mm"),
                                  _entry("B", quantity=500, size="170 x 240 mm")])
    groups = build(data, NGV, _Quiet())
    assert len(groups) == 2
    for group in groups:
        group_data = to_customer_data(group, data)
        assert "quantity" not in group_data.scanned, group_data.scanned
        assert group_data.products == [], "浇出来的数据不能再带产品条目（会二次分组）"


def test_条目里的出处会带进摘要():
    data = CustomerData(values={"size": "210 x 280 mm", "binding": "perfect binding"},
                        products=[_entry("A", quantity=1000), _entry("B", quantity=1000)])
    summary = summarize(build(data, NGV, _Quiet()))
    assert summary[0]["record"] == 1
    assert summary[0]["titles"] == 2
    assert [e["label"] for e in summary[0]["evidence"]] == ["A", "B"]


# ── ⑩ product_table 策略（默认关闭，显式开启才产出条目）─────────────────
def _table_profile(enabled: bool):
    return NGV.from_raw({
        "id": "tbl", "name": "TBL", "priority": 1, "status": "draft", "version": 1,
        "detect": {}, "erp_fixed": {}, "email": {},
        "fields": {
            "product_name": ["Title"],
            "binding": ["Binding"],
            "size": ["Size"],
            "extent": ["Extent"],
            "quantity": ["QTY"],
        },
        "rules": {"layout": {"product_table": {"enabled": enabled}}},
    })


TABLE_ROWS = [
    ["Multi-product enquiry"],
    ["Title", "Binding", "Size", "Extent", "QTY"],
    ["Book One", "Flexcover", "210x280", "24", "5.000"],
    ["Book Two", "Flexcover", "210x280", "24", "7.500"],
    ["Book Three", "Hardcover", "170x240", "32", "4.000"],
]


def test_product_table默认关闭():
    entries: list = []
    values, _, layout, _ = _extract_rows(TABLE_ROWS, _table_profile(False),
                                         products_out=entries)
    assert entries == []
    assert "product_table" not in layout


def test_product_table开启后每行一个条目():
    entries: list = []
    values, _, layout, _ = _extract_rows(TABLE_ROWS, _table_profile(True),
                                         products_out=entries)
    assert layout.startswith("product_table×")
    assert len(entries) == 3
    assert entries[0]["label"] == "Book One"
    assert entries[0]["values"]["binding"] == "Flexcover"
    assert entries[0]["quantity"] == 5000, "`5.000` 要按千分位解析"
    assert entries[2]["values"]["size"] == "170x240"


def test_product_table两张结构不同的行分成两条记录():
    entries: list = []
    _extract_rows(TABLE_ROWS, _table_profile(True), products_out=entries)
    data = CustomerData(values={"quantity": "16500"}, products=entries)
    groups = build(data, _table_profile(True), _Quiet())
    assert len(groups) == 2, [g.label for g in groups]
    assert sorted(g.titles for g in groups) == [1, 2]
    assert sorted(g.quantity for g in groups) == [4000, 12500]


# ── ⑪ 抽取明细要把条目落下去（agent 写多条 CPMS 要靠它）───────────────
def test_抽取明细包含产品条目():
    data = CustomerData(values={"size": "210 x 280 mm"},
                        products=[_entry("A", quantity=1000)])
    payload = json.loads(data.to_json())
    assert payload["products"][0]["label"] == "A"


def test_没有条目时抽取明细结构不变():
    """单产品单的 .extracted.json 不能因为这次改动多出键（历史测试都按 8 键读）。"""
    payload = json.loads(CustomerData(values={"size": "210 x 280 mm"}).to_json())
    assert "products" not in payload
    assert set(payload) == {"profile_id", "layout", "values", "scanned",
                            "scan_notes", "quantity_tiers", "unmatched_labels",
                            "extras_found"}


def test_多产品询价里单条目那一组TZS是1不是整单款数():
    """实测踩到的坑（IAN 574239）：6 款拆 3 组，其中一组只有 1 款，
    却沿用了整封询价的 `versions: 6 titles` → TZS 落成 6，数量对不上。

    规矩：整份询价是多产品时，**每一组**的款数都要按组自己算。
    """
    from main import map_fields

    base = {"size": "200 x 200 x 20 mm", "binding": "board book binding",
            "versions": "6 titles, 25 languages", "quantity": "365340"}
    data = CustomerData(values=dict(base), products=[
        _entry("A", quantity=60000, specials="sound module"),
        _entry("B", quantity=60000, specials="sound module"),
        _entry("C", quantity=40000, specials="sound module"),
        _entry("D", quantity=60890, specials="light module"),
        _entry("E", quantity=100000, specials="push and pull"),
        _entry("F", quantity=44450, specials="push and pull"),
    ])
    groups = build(data, NGV, _Quiet())
    assert len(groups) == 3

    titles = []
    for group in groups:
        result = map_fields(to_customer_data(group, data), TEMPLATE, NGV, _Quiet())
        titles.append(result["TZS"])
    assert sorted(titles) == [1, 2, 3], titles
    assert sum(g.quantity for g in groups) == 365340, "分组不能加/减总量"
