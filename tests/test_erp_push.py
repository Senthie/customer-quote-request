# -*- coding: utf-8 -*-
"""`scripts/erp_push.py` —— 流水线最后一步「推进 ERP」的回归测试（全离线）。

背景（2026-11-11）：业务问「179d3e1f… 的产出为什么没有上传 ERP」。
查证结论：流水线**原本没有这一步**（`main.py` 只落盘），且 MCP 只能写 59 键里的
10 个（ERP 新增页只有 15 个控件）。本模块补上「建单」这一步，这里钉住：

  1. 载荷映射：产物 → `erp_quote_create` 的入参（客户 / 产品名 / 规格 / 数量 / 备注）
  2. 挡在门口的三件事：CPMC 占位符、数量未知、客户代号未知（**不许硬推**）
  3. `YWY=KM` 不传（生成标记不是 ERP 真人；传了会被拒）
  4. 幂等：同一 task 已 created → 不再推（ERP 里重复建单最麻烦）
  5. 三种 ERP 响应都要落到明确状态：created / dry_run / error
  6. 「推不进去的字段」必须报出来给业务签字（CPLB/CPMS/TZS…）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import erp_push  # noqa: E402


def _artifact(tmp_path: Path, **over) -> Path:
    rec = {
        "ID": "task-1", "KHZL": "NGV10", "YWY": "KM", "CPMC": "冬季找找图画书",
        "CPMC_ZH": "冬季找找图画书", "ECPMC": "Wimmelbücher (Winter)",
        "CPLB": "27.01;普通BB书", "CD": 240, "KD": 330, "GD": 0,
        "TZS": 10, "YYS": 10, "CPSL1": 0, "KHDDH": "LG26090401",
        "CPMS": "书籍方向：【长书脊】产品规格：【240*330*0】共【10】款，每款【10】语言",
        "Remark": "", "ZDFS": "",
    }
    rec.update(over)
    p = tmp_path / "task" / "ERP_task-1_20261111_100506.erp-import.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps([rec], ensure_ascii=False), encoding="utf-8")
    return p


class FakeClient:
    """记录调用参数的假 MCP 客户端。"""

    def __init__(self, response):
        self.response = response
        self.calls: list[tuple[str, dict]] = []

    def initialize(self):  # 兼容 main() 的调用顺序
        return {"serverInfo": {"name": "fake"}}

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return self.response


@pytest.fixture(autouse=True)
def _isolated_log(tmp_path, monkeypatch):
    """推送日志指到 tmp，别污染真实 `customer-files/.erp_push_log.jsonl`。"""
    monkeypatch.setattr(erp_push, "PUSH_LOG", tmp_path / "push_log.jsonl")
    yield


def test_载荷映射_只带ERP能写的键(tmp_path: Path):
    art = _artifact(tmp_path)
    records = erp_push.load_bundle(art)
    payload, problems = erp_push.build_payload(records, customer="NGV10", qty_override=410000)
    assert problems == []
    assert payload["customer"] == "NGV10"
    assert payload["products"] == [{"name": "冬季找找图画书", "spec": "240*330*0", "qty": 410000}]
    # 追溯串：香港单号 / 原始品名
    assert payload["remark"] == "LG26090401 / Wimmelbücher (Winter)"
    # ★ YWY=KM 是生成标记，不是 ERP 真人 —— 传了会被下游拒
    assert "sales" not in payload and "ywY" not in payload


def test_数量从CPSL合计拿(tmp_path: Path):
    art = _artifact(tmp_path, CPSL1=12000, CPSL2=3000)
    payload, problems = erp_push.build_payload(erp_push.load_bundle(art), customer="NGV10")
    assert problems == []
    assert payload["products"][0]["qty"] == 15000


def test_CPSL全空又没给qty_不许推(tmp_path: Path):
    art = _artifact(tmp_path)
    _, problems = erp_push.build_payload(erp_push.load_bundle(art), customer="NGV10")
    assert any("数量未知" in p for p in problems)


def test_CPMC占位符不许推(tmp_path: Path):
    art = _artifact(tmp_path, CPMC="【中文品名待定】", CPMC_ZH="【中文品名待定】")
    _, problems = erp_push.build_payload(erp_push.load_bundle(art), customer="NGV10",
                                         qty_override=410000)
    assert any("占位符" in p for p in problems)


def test_客户代号未知不许猜(tmp_path: Path):
    art = _artifact(tmp_path, KHZL="")
    _, problems = erp_push.build_payload(erp_push.load_bundle(art), qty_override=410000)
    assert any("--customer" in p for p in problems)


def test_客户档案兜底取erp_khzl(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(erp_push, "_profile_customer_code", lambda pid: "NGVS16")
    art = _artifact(tmp_path, KHZL="")
    payload, problems = erp_push.build_payload(erp_push.load_bundle(art),
                                               profile_id="ngv", qty_override=100)
    assert problems == []
    assert payload["customer"] == "NGVS16"


def test_推不进去的字段要列给业务(tmp_path: Path):
    art = _artifact(tmp_path)
    fields = erp_push.unwritable_fields(erp_push.load_bundle(art))
    for key in ("CPLB", "CPMS", "TZS", "YYS", "KHDDH", "ECPMC"):
        assert key in fields
    assert fields["CPLB"] == "27.01;普通BB书"


def test_成功建单_返回BDBH并记日志(tmp_path: Path):
    art = _artifact(tmp_path)
    client = FakeClient({"status": "created", "bdbh": "BJXQ261111008",
                         "record_id": "uuid-1"})
    rep = erp_push.push(art, client=client, customer="NGV10", qty=410000, confirm=True)
    assert rep["status"] == "created"
    assert rep["bdbh"] == "BJXQ261111008"
    name, args = client.calls[0]
    assert name == "erp_quote_create" and args["confirm"] is True
    # 落日志：同 task 再推要被挡住
    erp_push.append_log({"task_id": "task-1", "status": "created", "bdbh": "BJXQ261111008"})
    again = erp_push.push(art, client=client, customer="NGV10", qty=410000, confirm=True)
    assert again["status"] == "skipped"
    assert "重复建单" in again["reason"]


def test_干跑_状态是dry_run(tmp_path: Path):
    art = _artifact(tmp_path)
    client = FakeClient({"status": "needs_confirmation", "ok": True})
    rep = erp_push.push(art, client=client, customer="NGV10", qty=410000, confirm=False)
    assert rep["status"] == "dry_run"
    assert client.calls[0][1]["confirm"] is False


def test_MCP回到文本错误_状态是error且带原文(tmp_path: Path):
    """真实踩到的形态：产品档案里找不到品名 → MCP 把异常包成一条 text。"""
    art = _artifact(tmp_path)
    client = FakeClient({"raw": "Error executing tool erp_quote_create: 产品档案里找不到 '冬季找找图画书'"})
    rep = erp_push.push(art, client=client, customer="NGV10", qty=410000, confirm=True)
    assert rep["status"] == "error"
    assert "产品档案里找不到" in rep["error"]


def test_sidecar命名不叠后缀(tmp_path: Path):
    art = _artifact(tmp_path)
    assert erp_push.sidecar_path(art).name == "ERP_task-1_20261111_100506.erp-push.json"
