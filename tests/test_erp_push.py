# -*- coding: utf-8 -*-
"""`scripts/erp_push.py` —— 流水线最后一步「推进 ERP」的回归测试（全离线）。

协议依据 `docs/erp-quote-create.md`（2026-11-12）。这里钉住：

  1. 载荷映射：59 键 → `erp_quote_create` 的表头 9 参 + 明细 14 参（`PRODUCT_ARG_MAP`）
  2. **一条产品记录 = 一次 create**（ERP 一次只收 1 条明细，多条 `unsupported`）
  3. 挡在门口的三件事：CPMC 占位符、数量未知、客户代号未知（不许硬推）
  4. `YWY=KM`/`GDY` 不传（生成标记不是 ERP 真人；ERP 按客户资料自动带）
  5. 幂等：同一 task 已 created → 不再推
  6. 六种 ERP 响应的落点：created / dry_run / invalid / partial / failed / error
  7. 「推不进去的字段」= 由产品档案反查或页面无控件的那些，必须报出来给业务补
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
    rec = {
        "ID": "task-1", "KHZL": "NGV10", "YWY": "KM", "GDY": "肖也",
        "CPMC": "冬季找找图画书", "CPMC_ZH": "冬季找找图画书",
        "ECPMC": "Wimmelbücher (Winter)", "CPLB": "27.01;普通BB书",
        "CD": 240, "KD": 330, "GD": 0, "TZS": 10, "YYS": 10,
        "CPSL1": 0, "KHDDH": "LG26090401",
        "CPMS": "书籍方向：【长书脊】产品规格：【240*330*0】共【10】款，每款【10】语言",
        "ZDFS": "", "BZFS": "纸箱包装", "TBLX": "不打卡板", "SHFS": "FOB(NGV)",
        "FSCMC": "FSC Mix", "SFYPJ": False, "Remark": "", "Remark4": "EN71", "Remark1": "",
        "JHRQ": "", "WCRQ": "", "CPSX": "", "CPLX": "", "CPMX": "",
        "DWMC": "本", "KHDH": "CU001",
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


# ── 载荷映射 ────────────────────────────────────────────────
def test_明细14参按协议映射(tmp_path: Path):
    rec = _rec(ZDFS="普通BB书", JHRQ="2027/08/13", WCRQ="2027/08/06",
               Remark1="无", Remark3="无", CPSX="三岁以上", CPLX="新产品")
    payload, problems = erp_push.build_payload([rec], customer="NGV10", qty_override=410000)
    assert problems == []
    p = payload["products"][0]
    assert p["name"] == "冬季找找图画书"
    assert p["spec"] == "240*330*0"          # 只校验，ERP 不落库
    assert p["qty"] == 410000
    assert p["binding"] == "普通BB书"        # ZDFS
    assert p["packing"] == "纸箱包装"        # BZFS
    assert p["pallet"] == "不打卡板"         # TBLX
    assert p["delivery"] == "FOB(NGV)"       # SHFS
    assert p["delivery_date"] == "2027/08/13"  # JHRQ
    assert p["expected_date"] == "2027/08/06"  # WCRQ
    assert p["parts_desc"] == "无"           # Remark1
    assert p["modify_log"] == "无"           # Remark3
    assert p["inspection"] == "EN71"         # Remark4
    assert p["attribute"] == "三岁以上"      # CPSX
    assert p["product_type"] == "新产品"     # CPLX
    assert p["cases"] == 10                  # TZS
    assert p["languages"] == 10              # YYS
    assert p["product_desc"].startswith("书籍方向")  # CPMS


def test_表头9参映射且不传KM(tmp_path: Path):
    rec = _rec(LXR="Ebers, Lea", LXDH="+49 1", SJ="+49 2", CZ="+49 3", ZKL="10",
               CPMX="产品描述（表头）")
    payload, _ = erp_push.build_payload([rec], customer="NGV10", qty_override=1)
    assert payload["customer"] == "NGV10"
    assert payload["contact"] == "Ebers, Lea"
    assert payload["phone"] == "+49 1"
    assert payload["mobile"] == "+49 2"
    assert payload["fax"] == "+49 3"
    assert payload["discount"] == "10"
    assert payload["product_memo"] == "产品描述（表头）"
    # ★ YWY=KM / GDY 都不是 ERP 真人账号 → 不能传
    assert "sales" not in payload and "merchandiser" not in payload


def test_空值不塞进明细(tmp_path: Path):
    payload, _ = erp_push.build_payload([_rec(ZDFS="", TZS=0, YYS=0)],
                                        customer="NGV10", qty_override=5)
    p = payload["products"][0]
    assert "binding" not in p and "cases" not in p and "languages" not in p


# ── 一条记录 = 一次 create ──────────────────────────────────
def test_三条记录拆成三次create(tmp_path: Path):
    recs = [_rec(CPMC="甲书"), _rec(CPMC="乙书", CPLB="22.02;方脊精装书"), _rec(CPMC="丙书")]
    calls, problems = erp_push.build_calls(recs, customer="NGV10", qty=100)
    assert problems == []
    assert len(calls) == 3
    assert [c["products"][0]["name"] for c in calls] == ["甲书", "乙书", "丙书"]
    assert all(len(c["products"]) == 1 for c in calls)  # ERP 一次只收 1 条明细


def test_多记录时问题带条号(tmp_path: Path):
    recs = [_rec(CPMC="甲书"), _rec(CPMC="【中文品名待定】")]
    _, problems = erp_push.build_calls(recs, customer="NGV10", qty=100)
    assert any(p.startswith("第2条") and "占位符" in p for p in problems)


# ── 门口三道闸 ──────────────────────────────────────────────
def test_数量未知不许推(tmp_path: Path):
    _, problems = erp_push.build_payload([_rec()], customer="NGV10")
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


def test_数量从CPSL合计拿(tmp_path: Path):
    payload, _ = erp_push.build_payload([_rec(CPSL1=12000, CPSL2=3000)], customer="NGV10")
    assert payload["products"][0]["qty"] == 15000


# ── 推不进去的字段 ──────────────────────────────────────────
def test_推不进去的字段清单准确(tmp_path: Path):
    fields = erp_push.unwritable_fields([_rec(SFYPJ=True)])
    # 由产品档案反查 / 页面无控件的
    for key in ("CPLB", "CD", "KD", "GD", "DWMC", "FSCMC", "KHDH", "KHDDH", "ECPMC", "SFYPJ"):
        assert key in fields
    # 这些已经能写了（2026-11-12 协议：明细 14 参）→ 不该再出现在清单里
    for key in ("TZS", "YYS", "CPMS", "ZDFS", "BZFS", "TBLX", "SHFS", "Remark4"):
        assert key not in fields
    # GD=0 是「客户没给厚度」的有意义 0，必须报；CPSL2=0 这种默认值不报
    assert fields["GD"] == "0"


def test_布尔默认值不噪音(tmp_path: Path):
    """`SFYPJ=False` / `chk*=False` 是默认值，逐单调出来只会淹没真差异。"""
    fields = erp_push.unwritable_fields([_rec(SFYPJ=False)])
    assert "SFYPJ" not in fields
    assert "CPSL2" not in fields


# ── 响应落点 ────────────────────────────────────────────────
def test_成功建单_记录BDBH并幂等(tmp_path: Path):
    art = _artifact(tmp_path)
    client = FakeClient({"status": "created", "bdbh": "BJXQ261112001", "record_id": "u1"})
    rep = erp_push.push(art, client=client, customer="NGV10", qty=410000, confirm=True)
    assert rep["status"] == "created"
    assert rep["bdbh"] == ["BJXQ261112001"]
    assert client.calls[0][1]["confirm"] is True
    again = erp_push.push(art, client=client, customer="NGV10", qty=410000, confirm=True)
    assert again["status"] == "skipped" and "重复建单" in again["reason"]


def test_干跑状态(tmp_path: Path):
    art = _artifact(tmp_path)
    client = FakeClient({"status": "needs_confirmation", "ok": True})
    rep = erp_push.push(art, client=client, customer="NGV10", qty=10, confirm=False)
    assert rep["status"] == "dry_run"
    assert client.calls[0][1]["confirm"] is False


def test_产品档案找不到_是error且带原文(tmp_path: Path):
    art = _artifact(tmp_path)
    client = FakeClient({"raw": "Error executing tool erp_quote_create: 产品档案里找不到 '冬季找找图画书'"})
    rep = erp_push.push(art, client=client, customer="NGV10", qty=410000, confirm=True)
    assert rep["status"] == "error"
    assert "产品档案里找不到" in rep["error"]


def test_部分成功要点出来(tmp_path: Path):
    art = _artifact(tmp_path, [_rec(CPMC="甲书"), _rec(CPMC="乙书")])
    client = FakeClient([{"status": "created", "bdbh": "BJXQ261112002"},
                         {"status": "failed", "message": "明细行没建出来"}])
    rep = erp_push.push(art, client=client, customer="NGV10", qty=10, confirm=True)
    assert rep["status"] == "partial"
    assert rep["results"][0]["status"] == "created"
    assert rep["results"][1]["status"] == "failed"


def test_rejected带ERP原文(tmp_path: Path):
    art = _artifact(tmp_path)
    client = FakeClient({"status": "rejected", "message": "【产品数量】不能为空！"})
    rep = erp_push.push(art, client=client, customer="NGV10", qty=10, confirm=True)
    assert rep["status"] == "rejected"
    assert "产品数量" in rep["error"]


def test_sidecar命名不叠后缀(tmp_path: Path):
    assert erp_push.sidecar_path(_artifact(tmp_path)).name == \
        "ERP_task-1_20261111_100506.erp-push.json"
