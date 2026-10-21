# -*- coding: utf-8 -*-
"""NGV「纯文本邮件 enquiry」回归 —— CU26070901 实单（2026-10-21）暴露的 3 个缺陷。

这一单的形态和之前 4 份 NGV 样本都不同：**没有附件，整封邮件正文就是询价单**，
而且是一封**来回邮件串**（客户最新回复在最上面，下面粘着原始 enquiry 和我们的提问）。
正文里因此同时存在互相矛盾的数字与款数。

钉住三件事：

1. `QTY: 60.000 St. in total (20.000 cps/title)` —— 总数写在数字**后面**，
   旧的正则只认 `total <数字>`，于是整条丢成 CPSL1=0。
2. `3 titles (all in one language):` —— 没有 `versions:` 标签，靠 `rules.scan` 取；
   但**必须锚行首**，否则会抓到客户自己的疑问句
   `... = 4 titles instead of 3 titles`（那是假设，不是订单事实）。
3. `FSC-Mix` —— 客户写连字符形态，ERP 枚举只有 `FSC Mix`，不归一化就报 error。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from customer_profile import load_profile  # noqa: E402
from extract_fields import extract_from_text  # noqa: E402
from erp_schema import normalize_value, validate_result  # noqa: E402
from main import map_fields, parse_single_quantity  # noqa: E402

NGV = load_profile("ngv")

# 这段正文按真实邮件串的结构重排（最新回复在上、引用在下），
# 关键点：`4 titles` 出现在引用里的疑问句中，只有 `3 titles (all in one language)` 是订单事实。
MAIL_BODY = """\
Hi Demo,

good thinking on your side 😊 Yes, it's QTY: 60.000 St. in total  (20.000 cps/title).

Betreff: Re: price enquiry. 3x Malbücher Tedi

After checking, I think there is the typo in below e-mail ---
15,000cps/title and 60,000cps in total, it should be 60,000cps/15,000cps = 4 titles instead of 3 titles.

1x3 Malbücher, 80 pp + cover, 210 x 210 mm portrait, softcover, perfect binding,
100 gsm wood-free white offset paper (FSC-Mix), 230 art card C1S (FSC-Mix)

3 titles (all in one language):
Hubschrauber / Haus / Ente

QTY: 60.000 St. in total  (15.000 cps/title)
"""


class _Quiet:
    def info(self, *a, **k): pass
    def warn(self, *a, **k): pass
    def error(self, *a, **k): pass
    def field_mapping(self, *a, **k): pass


def _template() -> dict:
    return json.loads(
        (REPO_ROOT / "assets" / "add_customer_quote_request.json").read_text(encoding="utf-8"))


def _extract(body: str = MAIL_BODY):
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
        fh.write(body)
        path = fh.name
    try:
        return extract_from_text(
            path, NGV,
            filename="CU26070901 - AW_ price enquiry. 3x Malbücher Tedi.txt")
    finally:
        Path(path).unlink(missing_ok=True)


# ── ① 数量写法 ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,expected", [
    ("QTY: 60.000 St. in total  (20.000 cps/title)", 60000),
    ("60,000cps in total", 60000),
    ("QTY: 60.000 St. in total  (15.000 cps/title)", 60000),
    ("512580cps", 512580),
    ("4 x 17.500", 70000),
    ("3 x 7000 cps. = 21.000 cps", 21000),
])
def test_总数写法都能解析(text, expected):
    assert parse_single_quantity(text) == expected


def test_数字在后的总数不许被括号里的每款数量顶掉():
    """`60.000 St. in total (20.000 cps/title)` —— 括号里是单款数量，不是总数。"""
    assert parse_single_quantity("QTY: 60.000 St. in total (20.000 cps/title)") == 60000


def test_解析不出来仍然返回None而不是瞎猜():
    assert parse_single_quantity("见附件") is None
    assert parse_single_quantity("") is None


# ── ② 款数：锚行首，不许抓到疑问句里的 4 ────────────────────────────────
def test_款数扫描只认行首带括号的写法():
    data = _extract()
    assert data.scanned.get("number_of_titles") == "3", (
        "抓到 4 说明锚点失效 —— 那是客户在问「是不是该 4 款」，不是订单事实")


def test_款数能进TZS():
    result = map_fields(_extract(), _template(), NGV, _Quiet())
    assert result.get("TZS") == 3


# ── ③ FSC 连字符形态 ────────────────────────────────────────────────────
def test_FSC连字符归一化成ERP枚举():
    assert normalize_value("FSCMC", "FSC-Mix")[0] == "FSC Mix"
    assert normalize_value("FSCMC", "FSC Mix")[0] == "FSC Mix"


def test_FSC连字符不再产生校验error():
    """main.py step5b 的顺序是：先 normalize_value、再 validate_result —— 这里照抄那个顺序。"""
    from erp_schema import normalize_value as norm

    value, status = norm("FSCMC", "FSC-Mix")
    assert status == "normalized"

    result = {f: "" for f in _template()}
    result["FSCMC"] = value
    problems = validate_result(result)
    fsc = [p for p in problems if p.get("field") == "FSCMC" and p.get("level") == "error"]
    assert not fsc, f"FSCMC 仍报 error：{fsc}"


# ── ④ 端到端：这一单的数量与款数同时落对 ────────────────────────────────
def test_整封邮件抽出数量与款数并落库():
    data = _extract()
    assert data.values.get("quantity"), "QTY 行没被认成 quantity 规范键"
    result = map_fields(data, _template(), NGV, _Quiet())
    assert result.get("CPSL1") == 60000, "QTY 行没被认成数量（CPSL1 仍是默认 0）"
    assert result.get("TZS") == 3
