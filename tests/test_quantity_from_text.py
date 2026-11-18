#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""正文数量兜底（assets/quantity_from_text.yaml）：唯一才落值，多个不猜。"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import main as m  # noqa: E402


class _Log:
    def __init__(self):
        self.warns, self.infos = [], []

    def info(self, msg="", *a, **k):
        self.infos.append(str(msg))

    def warn(self, msg="", *a, **k):
        self.warns.append(str(msg))

    def field_mapping(self, *a, **k):
        pass


class _CD:
    def __init__(self, blocks, layout="text_lines"):
        self.text_blocks = blocks
        self.layout = layout


def run(blocks, layout="text_lines"):
    log = _Log()
    return m._quantity_from_text(_CD(blocks, layout), log), log


def test_唯一数量落值():
    v, _ = run(["Bienvenue ! - 3 000 ex. suédois"])
    assert v == 3000


def test_德文_auflage_stück():
    v, _ = run(["Auflage: 12.000 Stück"])
    assert v == 12000


def test_多个不同数量不猜并告警():
    v, log = run(["3 000 ex. suédois + 2 000 ex. danois"])
    assert v is None
    assert any("不猜" in w for w in log.warns)


def test_同一数量重复出现仍算唯一():
    v, _ = run(["3 000 ex.", "still 3 000 ex."])
    assert v == 3000


def test_表格类不走这条():
    v, _ = run(["3 000 ex."], layout="section_pairs")
    assert v is None


def test_没有单位就不猜():
    v, _ = run(["64 pp + cover, 300 gsm board, 12 sheets"])
    assert v is None
