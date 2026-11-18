#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用形态推断（assets/cplb_from_binding.yaml）—— 档案 cplb.map 没写全时的兜底。

2026-11-18 基线：30 份产物 12 份回落 `32.00;其他`，其中带装订词的 3 份由这层接住。
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import main as m  # noqa: E402


class _Log:
    def info(self, *_a, **_k): pass
    def warn(self, *_a, **_k): pass
    def field_mapping(self, *_a, **_k): pass


def infer(**kw) -> str:
    label, _src = m._cplb_from_binding(kw, _Log())
    return label


def test_软封面_perfect_binding_是无线胶装书():
    assert infer(binding="Softcover", binding_instructions="perfect binding") == "23.01;无线胶装书"


def test_cased_book_是方脊精装书():
    assert infer(binding="Cased book, section sewn, cover drawn on over 3mm boards") == "22.02;方脊精装书"


def test_cased_board_book_优先判精装BB书():
    assert infer(binding="Cased board book binding, rounded corners") == "27.02;精装BB书"


def test_纯_board_book_是普通BB书():
    assert infer(binding="Board book binding, punch cut") == "27.01;普通BB书"


def test_骑马钉与车线():
    assert infer(binding="Saddle stitch") == "24.01;普通骑钉书"
    assert infer(binding="thread sewn") == "25.01;普通车线书"


def test_硬盒是糊盒():
    assert infer(binding="Rigid set up box with lift up lid") == "30.01;糊盒"


def test_没有装订线索就不猜():
    assert infer(project_description="Please quote the attached list") == ""
