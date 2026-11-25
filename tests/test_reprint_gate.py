# -*- coding: utf-8 -*-
"""reprint_gate 的回归测试：关键词识别、PO 提取、离线降级、找到原单时的条目文案。"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import reprint_gate as G  # noqa: E402
from question_answers import ITEM_KEYS  # noqa: E402

REPRINT_TEXT = """Subject: Reprint PO 16186 Preisanfrage Tedi / Schultüten
Hi Alex,
Tedi has requested the following reprint from PO 16186:
School gift bags (2-titles) ... 2 x 18,000 cps = 36,000 cps in total
New: Packaging units must be marked with red adhesive tape
ETD 05.01.2027
"""

PLAIN_TEXT = "New series, 2 titles, 24pp, 170x297mm, please quote."

RECORD = {
    "ECPMC": "测试冲形状骑订书带贴纸/Preisanfrage Tedi / Schultüten",
    "CPMC": "冲形状骑订书带贴纸",
    "KHZL": "NGV10",
}
PLAIN_RECORD = {"ECPMC": "Wimmelbücher Winter", "CPMC": "普通BOBO书", "KHZL": "NGV10"}


class FakeClient:
    def __init__(self, records):
        self.records = records
        self.calls = []

    def call_tool(self, name, args):
        self.calls.append((name, args))
        return {"records": self.records}


def test_detect_reprint_hits_english_german_and_chinese():
    assert G.detect_reprint(REPRINT_TEXT)
    assert G.detect_reprint("Es handelt sich um einen Nachdruck von PO 16186")
    assert G.detect_reprint("这单是翻单，重新印刷")
    assert G.detect_reprint(PLAIN_TEXT) is None


def test_extract_pos():
    assert G.extract_pos(REPRINT_TEXT) == ["16186"]
    assert G.extract_pos("PO#16186 和 PO-15965") == ["16186", "15965"]
    assert G.extract_pos(PLAIN_TEXT) == []


def test_product_keyword_keeps_umlaut():
    kw = G.product_keyword(RECORD)
    assert "Schult" in kw and "ü" in kw, kw
    assert "测试" not in kw


def test_keywords_fall_back_to_source_text_when_record_empty():
    kws = G.product_keywords({}, REPRINT_TEXT)
    assert kws and any("Schult" in k for k in kws), kws
    assert not any(k.lower() in G.STOPWORDS for k in kws)


def test_base_quote_lookup_skips_test_quotes():
    fake = FakeClient([{
        "表单编号": "BJXQ261125010", "客户名称": "NGV10",
        "产品名称": "测试冲形状骑订书带贴纸/Preisanfrage Tedi / Schultüten",
        "制单日期": "2026-11-25 10:32:34",
    }])
    items = G.gap_items(RECORD, REPRINT_TEXT, customer="NGV10",
                        today="2026-11-25", client=fake)
    assert len(items) == 1 and "未能自动找到" in items[0]["question"]


def test_offline_gap_item_is_valid_and_not_blocking():
    items = G.gap_items(RECORD, REPRINT_TEXT, customer="NGV10",
                        today="2026-11-25", online=False)
    assert len(items) == 1
    it = items[0]
    assert tuple(it.keys()) == ITEM_KEYS
    assert it["id"] == "Q_REPRINT"
    assert it["field"] == "CPMS"
    assert it["status"] == "open" and it["blocking"] is False
    assert it["asked_at"] == "2026-11-25"
    assert "PO#16186" in it["question"]
    assert "未能自动找到" in it["question"]


def test_non_reprint_enquiry_produces_nothing():
    assert G.gap_items(PLAIN_RECORD, PLAIN_TEXT, customer="NGV10", online=False) == []


def test_online_with_base_quote_mentions_bdbh():
    fake = FakeClient([
        {"表单编号": "BJXQ250920001", "客户名称": "NGV10",
         "产品名称": "Kathrin/冲形状骑订书带贴纸/Tedi 3/2026 – Schultüten",
         "制单日期": "2025-09-20 09:14:57"},
        {"表单编号": "BJXQ260826002", "客户名称": "NGV10",
         "产品名称": "Ulrich/冲形状骑订书带贴纸/Preisanfrage Tedi / Schultüten",
         "制单日期": "2026-08-26 09:14:06"},
    ])
    items = G.gap_items(RECORD, REPRINT_TEXT, customer="NGV10",
                        today="2026-11-25", client=fake)
    assert len(items) == 1
    q = items[0]["question"]
    # 取最近的一张（260826002 更新）
    assert "BJXQ260826002" in q
    assert "reprint_crosscheck.py" in q
    assert fake.calls and fake.calls[0][0] == "erp_quote_list"
    assert fake.calls[0][1]["key"].startswith("Schult")


def test_candidate_ranking_prefers_product_word_over_contact_name():
    fake = FakeClient([
        {"表单编号": "BJXQ261110006-01", "客户名称": "NGV10",
         "产品名称": "Ulrich/B款 64PP 5按键音乐器 方脊锁线精装书/Type B Furzipups - New Soundbooks",
         "制单日期": "2026-11-13 10:00:00"},
        {"表单编号": "BJXQ250920001", "客户名称": "NGV10",
         "产品名称": "Kathrin/冲形状骑订书带贴纸/Tedi 3/2026 – Schultüten, 2-fach(FSC Mix)",
         "制单日期": "2025-09-20 09:14:57"},
    ])
    items = G.gap_items(RECORD, REPRINT_TEXT, customer="NGV10",
                        today="2026-11-25", client=fake)
    q = items[0]["question"]
    assert "BJXQ250920001" in q
    assert "候选共" not in q, "只有一张同强度候选时不该报候选数"
    assert G.match_strength("Kathrin/…Schultüten", REPRINT_TEXT) > \
        G.match_strength("Ulrich/Furzipups Soundbooks", REPRINT_TEXT)


def test_client_failure_degrades_without_raising():
    class Boom:
        def call_tool(self, name, args):
            raise RuntimeError("erp down")

    items = G.gap_items(RECORD, REPRINT_TEXT, customer="NGV10",
                        today="2026-11-25", client=Boom())
    assert len(items) == 1 and "未能自动找到" in items[0]["question"]
