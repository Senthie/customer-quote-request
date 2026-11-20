"""product_type.py：CPLX（产品类型）判定的回归。

口径来源 docs/产品类型判定.md（森西 2026-11-20 上传的原文）。不打网络：历史单用手写 dict 喂。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import product_type as PT  # noqa: E402


def hist(bdbh="KHDD25120028", date="2025-12-18 14:50:24", cust="NGVS16",
         spec="207*243*14", ptype="半返单旧产品", matched="客户单号=16258-01") -> list[dict]:
    return [{"表单编号": bdbh, "制单日期": date, "客户名称": cust, "规格": spec,
             "产品类型": ptype, "matched_by": matched}]


# ── 没有历史单 ─────────────────────────────────────────────────────────
def test_new_product_when_nothing_found():
    r = PT.classify("Please quote 3 titles, 5000pcs each. New project for 2027.",
                    customer="MAC24", history=[])
    assert r["product_type"] == PT.NEW
    assert r["evidence"]["new_project"]


def test_special_new_product_with_special_process():
    r = PT.classify("New project: Taschenlampenbuch mit LED, 10 spreads, sound module.", customer="NGV10")
    assert r["product_type"] == PT.NEW_SPECIAL


def test_ngv_reprint_without_history_defaults_to_half_old():
    """NGV 没查到历史单，但客户称返单 → 半返单旧产品（默认档，需业务确认）。"""
    r = PT.classify("Reprint enquiry, please offer again.", customer="NGVS16")
    assert r["product_type"] == PT.REPEAT_OLD
    assert r["confidence"] == "medium"


def test_non_ngv_reprint_without_history_is_low_confidence():
    r = PT.classify("re-order please", customer="MAC24")
    assert r["product_type"] == PT.REPEAT_OLD
    assert r["confidence"] == "low"


# ── 有历史单 ───────────────────────────────────────────────────────────
def test_reprint_enquiry_with_history_is_half_old():
    text = "RE: Reprint enquiry PO#16258-01 Taschenlampenbuch. Target price US$1.25/pc."
    r = PT.classify(text, customer="NGV10", history=hist())
    assert r["product_type"] == PT.REPEAT_OLD
    assert r["confidence"] == "high"


def test_explicit_same_as_before_is_full_repeat():
    text = "Reprint. Files, material and size exactly the same as last order."
    r = PT.classify(text, customer="NGV10", history=hist())
    assert r["product_type"] == PT.REPEAT_SAME


def test_die_line_change_is_half_new():
    text = "Reprint, but we changed the die line and window shape."
    r = PT.classify(text, customer="NGV10", history=hist())
    assert r["product_type"] == PT.REPEAT_NEW
    assert r["evidence"]["die_or_shape"]


def test_material_change_is_half_new():
    text = "Reprint with cheaper material: 350g instead of 400g (cost down)."
    r = PT.classify(text, customer="NGV10", history=hist())
    assert r["product_type"] == PT.REPEAT_NEW


def test_process_optimisation_is_half_new():
    text = "Reprint, please optimise the binding / 工艺优化."
    r = PT.classify(text, customer="NGV10", history=hist())
    assert r["product_type"] == PT.REPEAT_NEW


def test_file_only_change_is_half_old():
    text = "Reprint PO#16258-01 with new cover file, packing unchanged."
    r = PT.classify(text, customer="NGV10", history=hist())
    assert r["product_type"] == PT.REPEAT_OLD


def test_history_type_is_quoted_as_corroboration():
    r = PT.classify("Reprint please", customer="NGV10", history=hist(ptype="半返单新产品"))
    assert "半返单新产品" in r["reason"]


# ── 多款优先级 ─────────────────────────────────────────────────────────
def test_priority_order():
    assert PT.pick_highest([PT.REPEAT_SAME, PT.NEW]) == PT.NEW
    assert PT.pick_highest([PT.NEW, PT.NEW_SPECIAL]) == PT.NEW_SPECIAL
    assert PT.pick_highest([PT.REPEAT_SAME, PT.REPEAT_OLD, PT.REPEAT_NEW]) == PT.REPEAT_NEW
    assert PT.pick_highest([]) == PT.NEW


def test_mixed_titles_take_highest():
    """一个订单里部分款改文件、部分款改刀线 → 半返单新产品（原文 §半返单新产品 第 4 条）。"""
    text = ("Reprint: title A only new cover file, title B changed die line and window.")
    r = PT.classify(text, customer="NGV10", history=hist())
    assert r["product_type"] == PT.REPEAT_NEW


# ── 工具函数 ───────────────────────────────────────────────────────────
def test_extract_po():
    got = PT.extract_po("RE: Reprint enquiry PO#16258-01 / PO 16528-01 / POR460115608")
    assert "16258-01" in got
    assert any(g.startswith("460115608") or "460115608" in g for g in got)


def test_read_text_from_task_dir(tmp_path):
    unit = tmp_path / "unit"
    unit.mkdir()
    (unit / "_email.meta.json").write_text(
        '{"subject": "Reprint enquiry PO#16258-01", "body": "please quote again"}', encoding="utf-8")
    (unit / "x.txt").write_text("Taschenlampenbuch 207x243", encoding="utf-8")
    text = PT.read_text(tmp_path)
    assert "Reprint enquiry" in text and "Taschenlampenbuch" in text


def test_lookup_sorting_prefers_po_match():
    """PO 命中的历史单必须排第一，别被更近的同类老单顶掉（实测 MOP17 单会顶掉 PO 单）。"""
    rows = [
        {"表单编号": "KHDD26080026", "制单日期": "2026-08-19 11:09:32", "matched_by": "产品英文名=Taschenlampe"},
        {"表单编号": "KHDD25120028", "制单日期": "2025-12-18 14:50:24", "matched_by": "客户单号=16258-01"},
    ]
    rank = {"客户单号": 0, "产品英文名": 1, "规格": 2}
    rows_sorted = sorted(rows, key=lambda r: str(r.get("制单日期") or ""), reverse=True)
    rows_sorted.sort(key=lambda r: rank.get(str(r["matched_by"]).split("=")[0], 9))
    assert rows_sorted[0]["表单编号"] == "KHDD25120028"


def test_cli_on_task_dir(capsys, tmp_path):
    unit = tmp_path / "unit"
    unit.mkdir()
    (unit / "_email.meta.json").write_text(
        '{"subject": "Reprint enquiry PO#16258-01 Taschenlampenbuch"}', encoding="utf-8")
    rc = PT.main(["--text", str(tmp_path), "--customer", "NGV10"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "CPLX（产品类型）建议：半返单旧产品" in out
