#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NGV 款数正则：德文 `4 Titel` 也要认（2026-11-18 实测 CU26092401 / CU26082501 真值 4 款）。"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from customer_profile import resolve_profile  # noqa: E402


def test_ngv_款数认德文_titel():
    p = resolve_profile("ngv")[0]
    pat = p.rule("versions.title_pattern")
    assert pat, "ngv 档案缺 rules.versions.title_pattern"
    cases = {
        "4 Titel": 4,                        # ★ 本轮修复的写法（VEMAG/Tedi 表）
        "4 titles": 4,
        "8 versions: 6x version A, 2x version B": 8,
        "2 titles, 1 language": 2,
        "12 TITEL": 12,
        "4 titles in 7 languages": 4,
    }
    for raw, want in cases.items():
        m = re.search(str(pat), raw, re.IGNORECASE)
        assert m and int(m.group(1)) == want, f"{raw!r} 应提 {want}，实际 {m.group(1) if m else None}"


def test_ngv_款数不误吃版本号字母():
    pat = str(resolve_profile("ngv")[0].rule("versions.title_pattern"))
    # 「6x version A」里的 6 不该被当成款数（数字后面跟 x）
    m = re.search(pat, "versions: 6x version A", re.IGNORECASE)
    assert m is None or int(m.group(1)) != 6
