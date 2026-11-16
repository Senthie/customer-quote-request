# -*- coding: utf-8 -*-
"""CPMS 概要行去重 —— 推 ERP 时不能把「书籍方向/产品规格」那行再发一遍。

**问题（2026-11-16 实测）**：BJXQ261113003 / BJXQ261113004 两张单，我们发出的
`product_desc` 里概要行只出现 1 次，但 ERP 详情页读回来是 2 次 ——
需求录入页会按 `书籍方向 / 产品规格 / 款数` 字段**自己生成一行**概要。

**依据（不是猜的）**：历史语料 `train/custom_cpms_requie` 18395 份 ERP 快照里，
18348 份（99.74%）的 CPMS 中「产品规格：【」只出现 1 次，出现 2 次的 45 份、
3 次的 2 份 —— 属人工重复录入。所以正确形态是**只留 ERP 生成的那一行**。

这里钉住：

1. 一行式概要（书籍方向 + 产品规格同排）被剥掉，紧跟的空行一起剥
2. 两行式概要（书籍方向、产品规格各占一行）同样剥掉
3. 正文中间的同类字样**不动**（只剥开头连续段）
4. 整段 CPMS 只有概要行时**原样返回**（`product_desc` 是 ERP 必填，不能清空）
5. `build_call` / `build_payload` 出来的入参里已无概要行
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import erp_mcp_export  # noqa: E402
import erp_push  # noqa: E402


HEADER = "书籍方向：【长书脊】产品规格：【270*210*0】"


def _rec(**over):
    """14 个 ERP 必填齐的最小记录。"""
    rec = {
        "ID": "task-1", "KHZL": "NGV10", "YWY": "KM", "GDY": "肖也",
        "CPMC": "探案与线索", "CPMC_ZH": "探案与线索",
        "ECPMC": "Sleuth and Clue", "CPLB": "27.21;风琴揭页BB书",
        "CD": 270, "KD": 210, "GD": 0, "TZS": 1, "YYS": 1,
        "CPSL1": 2000, "KHDDH": "WY26111301",
        "CPMS": HEADER + "\n\n皮壳：157g 哑粉纸(FSC)，4C/0C，过哑胶",
        "ZDFS": "精装书", "BZFS": "纸箱包装", "TBLX": "不打卡板", "SHFS": "FOB 盐田",
        "JHRQ": "2026-11-23", "WCRQ": "2026-11-23", "Remark1": "无",
        "CPSX": "三岁以下", "CPLX": "新产品", "Remark3": "无", "Remark4": "无",
    }
    rec.update(over)
    return rec


# ────────────────────────── strip_cpms_header 单元 ──────────────────────────
def test_一行式概要被剥掉且吃掉空行():
    out = erp_mcp_export.strip_cpms_header(HEADER + "\n\n皮壳：157g 哑粉纸(FSC)")
    assert out == "皮壳：157g 哑粉纸(FSC)"


def test_两行式概要也剥():
    text = "书籍方向：【长书脊】\n产品规格：【270*210*0】共【8】款\n\n内文：64PP"
    assert erp_mcp_export.strip_cpms_header(text) == "内文：64PP"


def test_没有书籍方向只有产品规格也剥():
    text = "产品规格：【0*0*0】共【3】款\n1套=披萨盒+1本贴纸书"
    assert erp_mcp_export.strip_cpms_header(text) == "1套=披萨盒+1本贴纸书"


def test_正文中间的概要字样不动():
    text = "皮壳：157g 哑粉纸\n书籍方向：【长书脊】产品规格：【270*210*0】"
    assert erp_mcp_export.strip_cpms_header(text) == text


def test_整段只有概要行时原样返回():
    """product_desc 是 ERP 必填 —— 剥成空会让明细整行存不进去。"""
    assert erp_mcp_export.strip_cpms_header(HEADER) == HEADER


def test_本来就干净的不动():
    text = "封面：128g 铜版纸，4C/0C\n内文：157g 哑粉纸"
    assert erp_mcp_export.strip_cpms_header(text) == text


def test_空值安全():
    for val in (None, "", "   "):
        assert erp_mcp_export.strip_cpms_header(val) == val


# ────────────────────────── 走一遍导出链 ──────────────────────────
def test_导出入参里没有概要行():
    payload, problems = erp_push.build_payload([_rec()], customer="NGV10", qty_override=2000)
    desc = payload["products"][0]["fields"]["product_desc"]
    assert problems == []
    assert "书籍方向" not in desc and "产品规格" not in desc
    assert desc.startswith("皮壳：157g 哑粉纸(FSC)")


def test_59键产物本身仍保留概要行():
    """产物是给人看/给 cpms_lint 审的，概要行必须留着 —— 只在推送那一刻剥。"""
    rec = _rec()
    erp_push.build_payload([rec], customer="NGV10", qty_override=2000)
    assert rec["CPMS"].startswith("书籍方向：【长书脊】")
