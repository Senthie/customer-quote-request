# -*- coding: utf-8 -*-
"""`scripts/erp_push.py` —— 流水线最后一步「推进 ERP」的回归测试（全离线）。

协议依据 `docs/erp-quote-create.md`（2026-11-12 版：明细 51 字段 = `products[0].fields`）。
这里钉住：

  1. 59 键 → 51 字段的映射（`PRODUCT_FIELD_MAP` / `QTY_SLOT_MAP` / 表头 4 个）
  2. **ERP 服务端 14 个必填**缺一个就拦下（否则 create 会留下空主表）
  3. 一条产品记录 = 一次 create（ERP 一次只收 1 条明细）
  4. `YWY=KM` 不传；`SFYPJ` 布尔 → 「是/否」；`CPSL1..10` → `qty_1..qty_10`
  5. 幂等 / 六种 ERP 响应落点 / not_writable 只剩 4 个
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import erp_push  # noqa: E402


def _rec(**over):
    """一个「14 个 ERP 必填全齐」的最小记录，单项测试只覆盖自己关心的键。"""
    rec = {
        "ID": "task-1", "KHZL": "NGV10", "YWY": "KM", "GDY": "肖也",
        "CPMC": "冬季找找图画书", "CPMC_ZH": "冬季找找图画书",
        "ECPMC": "Wimmelbücher (Winter)", "CPLB": "27.01;普通BB书",
        "CD": 240, "KD": 330, "GD": 0, "TZS": 10, "YYS": 10,
        "CPSL1": 0, "KHDDH": "LG26090401", "KHDH": "CU001",
        "CPMS": "书籍方向：【长书脊】产品规格：【240*330*0】共【10】款，每款【10】语言",
        # ↓ 14 个 ERP 必填里剩下的
        "ZDFS": "快线BB书", "BZFS": "纸箱包装", "TBLX": "不打卡板", "SHFS": "FOB(NGV)",
        "JHRQ": "2027/08/13", "WCRQ": "2027/08/06", "Remark1": "无",
        "CPSX": "三岁以上", "CPLX": "新产品", "Remark3": "无",
        "Remark4": "EN71",
        # 其余
        "FSCMC": "FSC Mix", "SFYPJ": False, "Remark": "", "CPSX_": "",
        "DWMC": "本", "CPDL": "书版", "Drop_SJFX": "", "PXH": 1,
        "YSLX": "FOB(拼柜)", "SHDQ": "FOB(NGV)", "ZXLB": "普通纸箱",
        "ZBLX": "A=B双坑纸板", "ZXFS": "平装", "DKMC": "B3B坑纸板",
        "chkBSYTB": False, "chkBSYJZX": False, "chkZDBJF": False, "chkXBJF": False,
        "BZ": "美元", "KHMBDJ": 0, "OLD_KHDDH": "", "BGMS": "", "CPMX": "",
    }
    rec.update(over)
    return rec


def _artifact(tmp_path: Path, records=None) -> Path:
    p = tmp_path / "task" / "ERP_task-1_20261111_100506.erp-import.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(records or [_rec()], ensure_ascii=False), encoding="utf-8")
    return p


class FakeClient:
    def __init__(self, responses):
        self.responses = responses if isinstance(responses, list) else [responses]
        self.calls: list[tuple[str, dict]] = []

    def initialize(self):
        return {"serverInfo": {"name": "fake"}}

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        idx = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[idx]


@pytest.fixture(autouse=True)
def _isolated_log(tmp_path, monkeypatch):
    monkeypatch.setattr(erp_push, "PUSH_LOG", tmp_path / "push_log.jsonl")
    yield


# ── 映射 ────────────────────────────────────────────────────
def test_51字段映射(tmp_path: Path):
    payload, problems = erp_push.build_payload([_rec()], customer="NGV10", qty_override=410000)
    assert problems == []
    p = payload["products"][0]
    assert p["name"] == "冬季找找图画书"
    assert p["spec"] == "240*330*0"
    assert p["qty"] == 410000
    f = p["fields"]
    # 明细级（以前被我误判成「写不进」的那些，现在都能写）
    assert f["binding"] == "快线BB书"           # ZDFS
    assert f["packing"] == "纸箱包装"           # BZFS
    assert f["pallet"] == "不打卡板"            # TBLX
    assert f["delivery"] == "FOB(NGV)"          # SHFS
    assert f["delivery_date"] == "2027/08/13"   # JHRQ
    assert f["expected_date"] == "2027/08/06"   # WCRQ
    assert f["parts_desc"] == "无"              # Remark1
    assert f["modify_log"] == "无"              # Remark3
    assert f["inspection"] == "EN71"            # Remark4
    assert f["attribute"] == "三岁以上"         # CPSX
    assert f["product_type"] == "新产品"        # CPLX
    assert f["cases"] == 10                     # TZS
    assert f["languages"] == 10                 # YYS
    assert f["product_desc"].startswith("书籍方向")  # CPMS
    assert f["unit"] == "本"                    # DWMC
    assert f["product_group"] == "书版"         # CPDL
    assert f["customer_order_no"] == "CU001"    # KHDH（明细级 txtKHCPMC）
    assert f["product_name_en"] == "Wimmelbücher (Winter)"  # ECPMC
    assert f["length_mm"] == 240 and f["width_mm"] == 330 and f["height_mm"] == 0
    assert f["fsc_category"] == "FSC Mix"
    assert f["has_parts"] == "否"               # SFYPJ 布尔 → 是/否
    assert f["target_price_currency"] == "美元"
    assert f["carton_type"] == "普通纸箱" and f["board_type"] == "A=B双坑纸板"
    assert f["packing_orientation"] == "平装" and f["bottom_card"] == "B3B坑纸板"
    assert f["transport_type"] == "FOB(拼柜)"
    assert f["qty_1"] == 410000                 # qty 必须等于 qty_1


def test_表头不传KM(tmp_path: Path):
    payload, _ = erp_push.build_payload([_rec(Remark="表头备注", CPMX="表头产品描述")],
                                        customer="NGV10", qty_override=1)
    assert payload["customer"] == "NGV10"
    assert payload["remark"] == "表头备注"
    assert payload["product_memo"] == "表头产品描述"
    assert "sales" not in payload and "merchandiser" not in payload


def test_空值不塞进fields(tmp_path: Path):
    payload, _ = erp_push.build_payload([_rec(Drop_SJFX="", OLD_KHDDH="", BGMS="")],
                                        customer="NGV10", qty_override=5)
    f = payload["products"][0]["fields"]
    assert "spine_direction" not in f and "old_hk_order_no" not in f and "extra_desc" not in f


def test_数量档位拆到qty_n(tmp_path: Path):
    payload, _ = erp_push.build_payload([_rec(CPSL1=3000, CPSL2=5000, CPSL3=7000)],
                                        customer="NGV10")
    f = payload["products"][0]["fields"]
    assert f["qty_1"] == 3000 and f["qty_2"] == 5000 and f["qty_3"] == 7000
    # 协议 §4：qty 必须等于第 1 档，不是三档相加
    assert payload["products"][0]["qty"] == 3000


# ── ERP 必填 ────────────────────────────────────────────────
@pytest.mark.parametrize("key", ["ZDFS", "JHRQ", "WCRQ", "Remark1", "CPSX", "CPLX", "Remark3"])
def test_ERP必填缺一个就拦(key: str, tmp_path: Path):
    """14 个必填缺任何一个 → 不许推（否则 create 建了主表、明细被拒 = 空单）。"""
    rec = _rec(**{key: ""})
    _, problems = erp_push.build_payload([rec], customer="NGV10", qty_override=100)
    assert any("ERP 必填缺失" in p for p in problems), key


def test_必填齐全就放行(tmp_path: Path):
    _, problems = erp_push.build_payload([_rec()], customer="NGV10", qty_override=100)
    assert problems == []


# ── 一次 create 一条明细 ────────────────────────────────────
def test_三条记录拆三次create(tmp_path: Path):
    recs = [_rec(CPMC="甲书"), _rec(CPMC="乙书"), _rec(CPMC="丙书")]
    calls, problems = erp_push.build_calls(recs, customer="NGV10", qty=100)
    assert problems == []
    assert len(calls) == 3
    assert [c["products"][0]["name"] for c in calls] == ["甲书", "乙书", "丙书"]
    assert all(len(c["products"]) == 1 for c in calls)


def test_多记录问题带条号(tmp_path: Path):
    recs = [_rec(CPMC="甲书"), _rec(CPMC="【中文品名待定】")]
    _, problems = erp_push.build_calls(recs, customer="NGV10", qty=100)
    assert any(p.startswith("第2条") and "占位符" in p for p in problems)


# ── 门口三道闸 ──────────────────────────────────────────────
def test_数量未知不许推(tmp_path: Path):
    _, problems = erp_push.build_payload([_rec(CPSL1=0)], customer="NGV10")
    assert any("数量未知" in p for p in problems)


def test_CPMC占位符不许推(tmp_path: Path):
    _, problems = erp_push.build_payload([_rec(CPMC="", CPMC_ZH="【中文品名待定】")],
                                         customer="NGV10", qty_override=1)
    assert any("占位符" in p for p in problems)


def test_客户代号未知不许猜(tmp_path: Path):
    _, problems = erp_push.build_payload([_rec(KHZL="")], qty_override=1)
    assert any("--customer" in p for p in problems)


def test_客户档案兜底取erp_khzl(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(erp_push, "_profile_customer_code", lambda pid: "NGVS16")
    payload, problems = erp_push.build_payload([_rec(KHZL="")], profile_id="ngv", qty_override=1)
    assert problems == []
    assert payload["customer"] == "NGVS16"


# ── 写不进的只剩 4 个 ──────────────────────────────────────
def test_not_writable只剩4个(tmp_path: Path):
    fields = erp_push.unwritable_fields([_rec(SFYPJ=True)])
    # 真实产物里 KHDQ 是空的，所以只报 CPLB + KHDDH；BDBH 由 ERP 生成也不报
    assert set(fields) == {"CPLB", "KHDDH"}
    assert fields["CPLB"] == "27.01;普通BB书"
    # 之前误判成写不进的，现在都在 fields 里了
    for k in ("CPMS", "TZS", "YYS", "ZDFS", "FSCMC", "KHDH", "ECPMC", "SFYPJ"):
        assert k not in fields


# ── 响应落点 ────────────────────────────────────────────────
def test_成功建单并幂等(tmp_path: Path):
    art = _artifact(tmp_path)
    client = FakeClient({"status": "created", "bdbh": "BJXQ261112001", "record_id": "u1"})
    rep = erp_push.push(art, client=client, customer="NGV10", qty=410000, confirm=True)
    assert rep["status"] == "created" and rep["bdbh"] == ["BJXQ261112001"]
    assert client.calls[0][1]["confirm"] is True
    again = erp_push.push(art, client=client, customer="NGV10", qty=410000, confirm=True)
    assert again["status"] == "skipped" and "重复建单" in again["reason"]


def test_干跑状态(tmp_path: Path):
    art = _artifact(tmp_path)
    client = FakeClient({"status": "needs_confirmation", "ok": True})
    rep = erp_push.push(art, client=client, customer="NGV10", qty=10, confirm=False)
    assert rep["status"] == "dry_run" and client.calls[0][1]["confirm"] is False


def test_产品档案找不到_是error(tmp_path: Path):
    art = _artifact(tmp_path)
    client = FakeClient({"raw": "Error executing tool erp_quote_create: 产品档案里找不到 '冬季找找图画书'"})
    rep = erp_push.push(art, client=client, customer="NGV10", qty=410000, confirm=True)
    assert rep["status"] == "error" and "产品档案里找不到" in rep["error"]


def test_部分成功要点出来(tmp_path: Path):
    art = _artifact(tmp_path, [_rec(CPMC="甲书"), _rec(CPMC="乙书")])
    client = FakeClient([{"status": "created", "bdbh": "BJXQ261112002"},
                         {"status": "failed", "message": "明细行没建出来"}])
    rep = erp_push.push(art, client=client, customer="NGV10", qty=10, confirm=True)
    assert rep["status"] == "partial"
    assert [r["status"] for r in rep["results"]] == ["created", "failed"]


def test_rejected带ERP原文(tmp_path: Path):
    art = _artifact(tmp_path)
    client = FakeClient({"status": "rejected", "message": "【产品数量】不能为空！"})
    rep = erp_push.push(art, client=client, customer="NGV10", qty=10, confirm=True)
    assert rep["status"] == "rejected" and "产品数量" in rep["error"]


def test_sidecar命名(tmp_path: Path):
    assert erp_push.sidecar_path(_artifact(tmp_path)).name == \
        "ERP_task-1_20261111_100506.erp-push.json"
