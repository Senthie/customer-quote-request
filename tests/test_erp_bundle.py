# -*- coding: utf-8 -*-
"""ERP 导出容器格式：**数组**（2026-10-29 业务拍板）。

背景：原来「一个产品 = 一份 ERP JSON」，多产品询价要开很多单、很多文件，
下游（Web 列表、cpms_lint、cpms_todo、审计、schema 校验）都得挨个点开。
现在统一成「一个报价文件 = 一个数组，数组里每条 = 一个产品」。

本文件钉住六件事：
1. `main.py` 产出的**顶层必须是数组**（哪怕只有 1 条产品记录）；
2. `erp_bundle` 向下兼容：老的单对象文件照样读成 1 条；
3. `erp_schema --check` 逐条校验、报错带 `[第几条/共几条]`；
4. `cpms_lint` 逐条 lint；
5. `cpms_todo` 逐条进待办（多产品不能只报第一条）；
6. Web 列表摘要按记录数统计、交付闸门逐条判。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from conftest import load_erp, load_one

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from erp_bundle import load_bundle, load_bundle_text, wrap  # noqa: E402

TEMPLATE = json.loads((REPO_ROOT / "assets" / "add_customer_quote_request.json")
                      .read_text(encoding="utf-8"))


def _record(**over) -> dict:
    rec = dict(TEMPLATE)
    rec.update({"CPLB": "23.01;无线胶装书", "CPMC": "图画书", "CPMC_ZH": "图画书",
                "CPMS": "产品规格：【210*210*0】", "KHDH": "CU001"})
    rec.update(over)
    return rec


# ── ① 容器本身 ─────────────────────────────────────────────────────────
def test_wrap_单条也包成数组():
    assert wrap({"a": 1}) == [{"a": 1}]


def test_数组与单对象都能读(tmp_path):
    p = tmp_path / "ERP_x.json"
    p.write_text(json.dumps([_record(), _record(ECPMC="第二款")], ensure_ascii=False),
                 encoding="utf-8")
    assert len(load_bundle(p)) == 2

    # 老格式（单对象）→ 1 条记录（向下兼容，历史产物不用回炉）
    p.write_text(json.dumps(_record(), ensure_ascii=False), encoding="utf-8")
    assert len(load_bundle(p)) == 1

    # BOM 容忍
    assert len(load_bundle_text("\ufeff" + json.dumps([_record()]))) == 1


def test_不是对象就报错(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps([1, 2]), encoding="utf-8")
    with pytest.raises(ValueError):
        load_bundle(p)


# ── ③ schema 校验逐条 ───────────────────────────────────────────────────
def test_schema逐条校验并标出第几条(tmp_path, capsys):
    from erp_schema import _cmd_check

    good = _record()
    bad = _record(ZDFS="这不是合法枚举")
    p = tmp_path / "ERP_two.json"
    p.write_text(json.dumps([good, bad], ensure_ascii=False), encoding="utf-8")

    code = _cmd_check(str(p))
    out = capsys.readouterr().out
    assert code == 1, "第二条有非法枚举 → 必须非 0"
    assert "[2/2]" in out and "ZDFS" in out


# ── ④ lint 逐条 ─────────────────────────────────────────────────────────
def test_lint逐条(tmp_path, capsys):
    import cpms_lint

    p = tmp_path / "ERP_two.json"
    p.write_text(json.dumps([_record(CPMS="完整描述但缺概要行"),
                            _record(CPMS="书籍方向：【长书脊】产品规格：【210*210*0】")],
                           ensure_ascii=False), encoding="utf-8")
    code = cpms_lint.main([str(p)])
    out = capsys.readouterr().out
    assert "[1/2]" in out and "[2/2]" in out, out
    assert code >= 1, "第一条缺概要行 → 必须有 error"


# ── ⑤ cpms_todo 逐条进待办 ──────────────────────────────────────────────
def test_todo多记录逐条(tmp_path):
    import cpms_todo

    d = tmp_path / "processed" / "task1"
    d.mkdir(parents=True)
    (d / "ERP_task1_20260101_000000.json").write_text(
        json.dumps([_record(CPMS="", CPMC="", CPMC_ZH=""),
                    _record(CPMS="已写完整描述", CPMC="第二款名", CPMC_ZH="第二款名",
                            ECPMC="Original")],
                   ensure_ascii=False), encoding="utf-8")
    items = cpms_todo.iter_incomplete(tmp_path / "processed")
    assert len(items) == 1, "只有第 1 条欠 CPMS/CPMC，第 2 条不该进待办"
    assert items[0]["record"] == 1 and items[0]["record_count"] == 2
    assert "CPMS" in items[0]["missing"]


# ── ⑥ Web 列表摘要 ──────────────────────────────────────────────────────
def test_web摘要按记录数统计(tmp_path):
    from CQR_Web.data import _summarize

    root = tmp_path
    d = root / "task1"
    d.mkdir()
    p = d / "ERP_task1_20260101_000000.json"
    p.write_text(json.dumps([_record(CPMS="", CPMC="", CPMC_ZH=""),
                             _record(CPMS="完整描述", CPMC="名", CPMC_ZH="名")],
                            ensure_ascii=False), encoding="utf-8")

    item = _summarize(p, root)
    assert item["record_count"] == 2
    assert item["incomplete"], "有一条 CPMS 空 → 交付闸门必须报警"
    assert any("第1条" in g for g in item["incomplete"]), item["incomplete"]
