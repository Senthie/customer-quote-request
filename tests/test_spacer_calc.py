#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""spacer_calc 判定逻辑回归测试（森西 2026-11-24 口径）。

真实来源：Usborne「Nursery Rhymes」询价原文（2026-11-24）写了
`Spacers: we might need them to bulk up the spine.` —— 第一版正则
`\\bspacer\\b` 不匹配复数 "Spacers"，导致真单被判成「不插纸」。
这个用例就是那次踩坑的固化。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from spacer_calc import decide, detect_signals  # noqa: E402

USBORNE = """TPS: 170x170mm or 180x180mm
Extent: 10pp
Text: 4/0 on 350gsm C1S artboard + machine varnish
Binding: Board book binding (self-covered) with 5 sounds in a "shaped" panel
Spacers: we might need them to bulk up the spine. Attached is a photo of the spine
(image 66) - as you can see the sound panel is flush to the book.
Delivery: FOB HK
Quantity: 5K up to 40K"""


def test_spacers_plural_is_detected():
    """★ 回归：复数 Spacers 必须命中（Usborne 真单踩过的坑）。"""
    sig = detect_signals(USBORNE)
    assert sig["spacer_requested"] is True, "复数 Spacers 没被识别"


def test_usborne_verdict_is_pending():
    """提到加纸但没给厚度 → pending，不能是 no。"""
    d = decide(text=USBORNE)
    assert d["spacer_needed"] == "pending", d
    assert d["target_mm"] is None


def test_no_signal_is_no():
    """两条都没提 → no（薄款），这是缺省而不是待问。"""
    d = decide(text="TPS: 210x210mm\nExtent: 12pp\nBinding: board book")
    assert d["spacer_needed"] == "no"


def test_english_spine_width():
    d = decide(text="Format: 210 x 210 mm\nSpine width: approx. 42 mm")
    assert d["spacer_needed"] == "yes"
    assert d["target_mm"] == 42.0


def test_german_ruckenstaerke():
    d = decide(text="Format: 190 x 190 mm\nRückenstärke: ca. 25 mm")
    assert d["spacer_needed"] == "yes"
    assert d["target_mm"] == 25.0


def test_chinese_spine_thickness():
    d = decide(text="成品 210x210mm，内文需加纸，书脊厚度 20mm")
    assert d["spacer_needed"] == "yes"
    assert d["target_mm"] == 20.0


def test_german_past_participle():
    """aufgedoppelt 的 ge- 中缀也要命中。"""
    sig = detect_signals("Der Buchblock muss aufgedoppelt werden")
    assert sig["spacer_requested"] is True


def test_bulk_up_the_spine():
    sig = detect_signals("we might need them to bulk up the spine")
    assert sig["spacer_requested"] is True


def test_explicit_target_overrides_text():
    d = decide(text="Spacers: might need them", target_mm=18.0)
    assert d["spacer_needed"] == "yes"
    assert d["target_mm"] == 18.0


def test_total_thickness_wins_over_first_number():
    """★ 回归：Three Wishes 真单写 `15mm book + 3mm backing board totalling 18mm`。

    目标是 18（与发声模块齐平），不是第一个数字 15。
    """
    raw = ("15mm book + 3mm backing board totalling 18mm "
           "so front of book is flush with module")
    d = decide(spine_raw=raw)
    assert d["spacer_needed"] == "yes"
    assert d["target_mm"] == 18.0, d


def test_parse_spine_mm_plain():
    from spacer_calc import parse_spine_mm
    assert parse_spine_mm("approx. 42 mm") == 42.0
    assert parse_spine_mm("15,5 mm") == 15.5
    assert parse_spine_mm("") is None


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
