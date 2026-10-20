"""`scripts/cpms_content_audit.py` —— 严格模式（CPMS 只写客户文件里的事实）的门禁。

业务 2026-10-19：CPMS 里出现客户文件没有的信息 = 错误信息，比漏抄更严重。
这些测试钉住「什么算可溯源、什么算编的」。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import cpms_content_audit as A  # noqa: E402


def test_编造的数字会被抓出来():
    src = "Cover = 400 gsm art card C1S, 4/0c + UV varnish"
    cpms = "封面：400g单粉(FSC)，4C+UV/0C\n- 窗口约100*100mm"
    r = A.audit(cpms, src)
    tokens = {t["token"] for t in r["untraced"]}
    assert "100" in tokens or "100*100mm" in tokens, r["untraced"]


def test_计数词按数字溯源_英文单复数不影响():
    # 客户写 "570 gemstone stickers"，我们写 "570 颗"，应算可溯源
    r = A.audit("贴纸：570颗水钻贴", "diamond sticker sheet = 570 gemstone stickers")
    assert r["summary"]["untraced"] == 0, r["untraced"]


def test_数字与单位分处两格也算可溯源():
    # 客户把数字写在 J9（vol. 1.25）、单位写在 K10（mm bookblock thickness）
    r = A.audit("备注：书芯厚1.25mm", "vol. 1.25\nmm bookblock thickness")
    assert r["summary"]["untraced"] == 0, r["untraced"]


def test_克重单位gsm与g等价():
    r = A.audit("封面：400g单粉", "400 gsm art card C1S")
    assert r["summary"]["untraced"] == 0, r["untraced"]


def test_印色写法不被当成料号():
    r = A.audit("封面：400g单粉，4C+UV/0C", "400 gsm art card, 4/0c + UV varnish")
    assert r["untraced_codes"] == [], r["untraced_codes"]


def test_料号编造会被抓出来():
    r = A.audit("包装：价格贴（ZW80/G6552）", "price sticker (Ø 40 mm, 1/0c PMS)")
    assert "ZW80/G6552" in r["untraced_codes"], r["untraced_codes"]


def test_漏抄会以missing提示():
    r = A.audit("封面：400g单粉", "400 gsm art card C1S + 1500 gsm greyboard")
    nums = {t["num"] for t in r["missing"]}
    assert "1500" in nums, r["missing"]


def test_返回值可直接当退出码():
    assert A.main(["--cpms", "/nonexistent.json", "--source", "/nonexistent.xlsx"]) == 1


@pytest.mark.parametrize(
    "cpms,source,ok",
    [
        ("内文：42PP=8PP单粉卡+2PP贴纸+32PP书纸", "42 pp = 8 pp die-cuts, 2 pp sticker, 32 pp text", True),
        ("装箱：18本/箱", "packing unit 18 cps", True),
        ("装箱：20本/箱", "packing unit 18 cps", False),
    ],
)
def test_构成与装箱数量(cpms, source, ok):
    r = A.audit(cpms, source)
    assert (r["summary"]["untraced"] == 0) is ok, r
