#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_against_erp 的对账判定口径（钉住判定分类、字段归一、数量总量换算）。"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import check_against_erp as chk  # noqa: E402

FORM = "BJXQ260825007"


@pytest.fixture
def truth():
    return [{
        "表单编号": FORM, "BDBH": FORM, "KHDDH": "CU26082501",
        "CPLB": "精装YO日历", "ZDFS": "线圈书", "CPMC": "Kathrin/Lidl",
        "TZS": 4, "YYS": 7, "CD": 120, "KD": 165, "GD": 0,
        "BZFS": "纸箱包装", "TBLX": "不打卡板", "SHFS": "FOB(NGV)",
        "CPSX": "三岁以上", "CPLX": "新产品", "SFYPJ": "是",
        "Remark1": "否", "YWY": "陈纯", "KHZL": "NGV10",
    }]


@pytest.fixture
def list_truth():
    # ERP 列表页真值（reference-eml/list_truth.json 里 BJXQ260825007 的实测值）：
    # 单款 40,000 × 款 4 = 160,000，**语言数 7 不放大**（4×7×40000=1,120,000 是错的口径）。
    return {FORM: {"表单编号": FORM, "款数": "4", "语言数": "7",
                   "最小值": "160000", "最大值": "160000"}}


def run(tmp_path, prod, truth, list_truth=None, name="ERP_x.json"):
    f = tmp_path / name
    f.write_text(json.dumps([prod], ensure_ascii=False), encoding="utf-8")
    return chk.run_one(f, truth, None, False, None, list_truth)


def verdict(res, field):
    return next(r["判定"] for r in res["rows"] if r["字段"] == field)


def test_单号命中与基础判定(tmp_path, truth):
    prod = {"BDBH": "", "KHDDH": "CU26082501",
            "CPLB": "精装YO日历",              # 一致
            "ZDFS": "",                        # 我方留空
            "CPSX": "三岁以上（要寄第三方测试）",  # 真差异
            "KHZL": "",                        # 参考，不算错
            "CD": 120.0, "KD": "165"}          # 数值归一 → 一致
    res = run(tmp_path, prod, truth)
    assert res["表单编号"] == FORM
    assert verdict(res, "CPLB") == "一致"
    assert verdict(res, "ZDFS") == "我方留空"
    assert verdict(res, "CPSX") == "真差异"
    assert verdict(res, "KHZL") == "参考"
    assert verdict(res, "尺寸(CDxKD)") == "一致"


def test_数量按款数还原成总量_语言数不放大(tmp_path, truth, list_truth):
    prod = {"KHDDH": "CU26082501", "TZS": 4, "YYS": 7, "CPSL1": 40000}
    res = run(tmp_path, prod, truth, list_truth)
    assert verdict(res, "数量首档") == "一致"
    assert verdict(res, "数量末档") == "一致"
    row = next(r for r in res["rows"] if r["字段"] == "数量首档")
    assert row["我方"] == 160000          # 只乘款数
    assert "不放大" in row["备注"]


def test_多语言单不乘语言数(tmp_path, truth):
    """2026-11-25 实测 BJXQ261125008：10 款 10 语，单款 81,600 → ERP 816,000（×10，不是 ×100）。"""
    lt = {FORM: {"表单编号": FORM, "款数": "10", "语言数": "10",
                 "最小值": "816000", "最大值": "816000"}}
    res = run(tmp_path, {"KHDDH": "CU26082501", "TZS": 10, "YYS": 10, "CPSL1": 81600},
              truth, lt)
    assert verdict(res, "数量首档") == "一致"


def test_数量算错就是真差异(tmp_path, truth, list_truth):
    prod = {"KHDDH": "CU26082501", "TZS": 4, "YYS": 7, "CPSL1": 3000}
    res = run(tmp_path, prod, truth, list_truth)
    assert verdict(res, "数量首档") == "真差异"


def test_数量留空(tmp_path, truth, list_truth):
    res = run(tmp_path, {"KHDDH": "CU26082501", "TZS": 4, "YYS": 7}, truth, list_truth)
    assert verdict(res, "数量首档") == "我方留空"


def test_没有列表真值时数量标口径待定(tmp_path, truth):
    prod = {"KHDDH": "CU26082501", "TZS": 4, "CPSL1": 40000}
    res = run(tmp_path, prod, truth)
    assert verdict(res, "数量首档") == "口径待定"


def test_长宽换序不算错(tmp_path, truth):
    res = run(tmp_path, {"KHDDH": "CU26082501", "CD": 165, "KD": 120}, truth)
    assert verdict(res, "尺寸(CDxKD)") == "一致"
    row = next(r for r in res["rows"] if r["字段"] == "尺寸(CDxKD)")
    assert "换序" in row["备注"]


def test_产品类别带编码也能一致(tmp_path, truth):
    res = run(tmp_path, {"KHDDH": "CU26082501", "CPLB": "27.01;精装YO日历"}, truth)
    assert verdict(res, "CPLB") == "一致"


def test_对不上真值时报错而不是瞎比(tmp_path, truth):
    res = run(tmp_path, {"KHDDH": "", "KHDH": "NULL000"}, truth)
    assert "错误" in res and "对不上真值" in res["错误"]


def test_manifest_指定候选单(tmp_path, truth):
    f = tmp_path / "ERP_y.json"
    f.write_text(json.dumps([{"KHDDH": "", "CPLB": "精装YO日历"}], ensure_ascii=False), encoding="utf-8")
    res = chk.run_one(f, truth, None, False,
                      {"erp_forms": ["BJXQ000000000", FORM]})
    assert res.get("表单编号") == FORM
