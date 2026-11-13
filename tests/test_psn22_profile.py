"""PSN22（Magic Cat Publishing）客户档案回归测试。

2026-11-13 收到该客户第一份真实询价（`Sleuth and Clue`，docx 规格 + 两份刀模 PDF）
之后做的校准，这里把校准结论钉住：

1. **docx 没有标签** —— 值是裸行。所以抽取靠两条路：
   `fields` 只覆盖以 `Text` / `Case` 开头的行；尺寸和斜杠串档位靠 `rules.scan`。
2. **斜杠串档位**（`2000/3000/…/30000`）必须拆成**多档**，不能当单一数量
   （当单值是 11 个数字 → `parse_single_quantity` 不敢猜 → 落 0，十档全丢）。
3. **类别**：客户把 accordion 拼成 `accordian`，`cplb.map` 两种拼法都要认，
   否则会漏到 `case bound` → 落 22.02;方脊精装书。语料里 3 单「精装封面 + 内文风琴折」
   业务落的就是 `风琴揭页BB书`。
4. **装订**：`Case bound` 不能当标签吃掉 —— 值里少了这几个字，
   `binding.map` 就判不出精装（`ZDFS` 会空）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import REPO_ROOT

from customer_profile import load_profile
from extract_fields import extract_from_text, split_tier_list
from main import map_fields


class _Quiet:
    """吞掉 main 的日志（映射过程会打几十行，测试里不需要）。"""

    def info(self, *a, **k): pass
    def warn(self, *a, **k): pass
    def error(self, *a, **k): pass
    def field_mapping(self, *a, **k): pass


def _template() -> dict:
    return json.loads(
        (REPO_ROOT / "assets" / "add_customer_quote_request.json").read_text(encoding="utf-8"))

PSN22 = load_profile("psn22")

# 客户 docx 的真实行（逐字，含客户自己的拼写错误 accordian / 11 档数量）
DOC_LINES = [
    "Sleuth and Clue",
    "270 x 210mmm (P)",
    "12 spreads + case + 21 tip-on flaps.",
    "Text 4/0 on 190gsm C1S artboard fsc + gloss press varnish",
    "Case 4/0 on 157gsm matt art paper fsc + matt lamination",
    "Case bound, accordian binding, text folded and glued on foredge only.",
    "Interior tip ins",
    "9 tip on flaps 4/4 sheet size 520 x 490mm + gloss press varnish on 190gsm C1S fsc",
    "12 tip on flaps 4/4 sheet size 520 x 440mm + gloss press varnish on 160gsm woodfree fsc",
    "2000/3000/5000/7000/10000/12000/15000/18000/20000/25000/30000",
    "1c plate change to text",
    "90% foil to front case",
]


def _write_docx(tmp_path, lines=DOC_LINES):
    from docx import Document

    doc = Document()
    for line in lines:
        doc.add_paragraph(line)
    path = tmp_path / "Sleuth and Clue.docx"
    doc.save(path)
    return path


# ── 1. split_tier_list 机制 ──────────────────────────────────
def test_斜杠串档位拆成多档():
    assert split_tier_list("2000/3000/5000") == [2000, 3000, 5000]


def test_斜杠串超过上限按顺序截断():
    """ERP 只有 CPSL1..10；客户给 11 档 → 保留前 10 档（业务 2026-11-13：删最后一档）。"""
    text = "2000/3000/5000/7000/10000/12000/15000/18000/20000/25000/30000"
    tiers = split_tier_list(text, max_tiers=10)
    assert len(tiers) == 10
    assert tiers[0] == 2000 and tiers[-1] == 25000
    assert 30000 not in tiers


def test_两档不认_交回单值解析():
    """`1/2` 更像别的写法，不能当档位表 —— 至少 3 档才认。"""
    assert split_tier_list("1/2") == []


def test_千分位写法也能认():
    assert split_tier_list("1.500/3.000/5.000") == [1500, 3000, 5000]


def test_混了文字就不认():
    assert split_tier_list("2000/3000/about 5000") == []


# ── 2. 整份 docx 抽取 ────────────────────────────────────────
def test_docx抽取尺寸_档位_装订(tmp_path):
    data = extract_from_text(str(_write_docx(tmp_path)), PSN22, filename="Sleuth and Clue.docx")

    # 尺寸：`270 x 210mmm (P)` → `270 x 210`（按书写顺序落 CD/KD）
    assert data.get("size") == "270 x 210"

    # 档位：11 档截成 10 档，顺序不变
    assert data.quantity_tiers == [2000, 3000, 5000, 7000, 10000,
                                   12000, 15000, 18000, 20000, 25000]
    # 拆成档位后不能再留成「单一数量」，否则 main 会走单值分支
    assert not str(data.get("quantity") or "").strip()
    assert not str(data.get("quantity_tiers") or "").strip()

    # 装订：整行抓，`Case bound` 这几个字必须在值里（装订映射靠它）
    assert "case bound" in str(data.get("binding") or "").lower()
    assert "accordian" in str(data.get("binding") or "").lower()

    # 内文 / 封面各自的纸张+印色
    assert "190gsm" in str(data.get("text_printing_spec") or "")
    assert "157gsm" in str(data.get("cover_printing_spec") or "")


def test_风琴折类别落2721而不是2202(tmp_path):
    """`accordian`（客户拼写）+ `case bound` 同时出现 → 27.21 优先，不能落 22.02。"""
    data = extract_from_text(str(_write_docx(tmp_path)), PSN22, filename="Sleuth and Clue.docx")
    result = map_fields(data, _template(), PSN22, _Quiet())

    assert result["CPLB"] == "27.21;风琴揭页BB书"
    assert result["ZDFS"] == "精装书"
    # 尺寸按书写顺序，不交换
    assert (result["CD"], result["KD"]) == (270, 210)
    # 11 档截成 10 档，第 11 档（30000）不落库
    tiers = [result[f"CPSL{i}"] for i in range(1, 11)]
    assert tiers == [2000, 3000, 5000, 7000, 10000, 12000, 15000, 18000, 20000, 25000]
    # 日期按档案口径留空（推送时才兜底）
    assert result["JHRQ"] == "" and result["WCRQ"] == ""


# ── 3. 身份识别 ─────────────────────────────────────────────
def test_识别只看docx写法特征(tmp_path):
    """发件人域名靠不住（都是 ghouseproducts 转发），识别靠 docx 的写法组合。"""
    texts = DOC_LINES
    score, reasons = PSN22.detect_score(texts, filename="Sleuth and Clue.docx")
    assert score >= 200, reasons
    assert PSN22.detect_score(["Batch report 2024"], filename="x.xlsx")[0] == 0


# ── 4. 档案结构 ─────────────────────────────────────────────
def test_档案已校准且必填豁免正确():
    assert PSN22.status == "calibrated"
    assert PSN22.validate() == []
    # 报价阶段客户不给单号：不豁免的话每单都报假 error
    assert set(PSN22.rule("required_empty", []) or []) >= {"KHDH", "KHDDH", "Drop_SJFX"}
    # 日期：语料 454/454 全空 → 档案口径「不落库」，推送时靠 default_date 兜底
    assert str(PSN22.rule("dates.JHRQ.mode", "")) == "empty"
    assert str(PSN22.rule("dates.WCRQ.mode", "")) == "empty"
