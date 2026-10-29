# -*- coding: utf-8 -*-
"""NGV enquiry 表单的**两个版本** —— 2025-01 版（`PR-00078025` / 100 Dinge 实单）。

背景（2026-10-29）：NGV 的 `enquiry` 表单存在两个版本的排布，行顺序不同：

| 项 | 2026 版（4 份权威样本） | 2025 版（本文件） |
|---|---|---|
| `offer deadline` | enquiry 分区 | **schedule 分区** |
| `TPS`（尺寸） | versions 分区 | **content 分区** |
| cover 分区 | paper → printing → finishing | **boards → padding → paper → …** |
| 装订工艺 | 单行 | **跨两行**（`Flexcover` / `thread-stitching`） |
| 包装 | 只有 specials | 多 `shrinkwrapping`（no）+ `sticker` 两行 |
| 数量 | 单一总数 | **三档** `5.000 / 7.500 / 10.000` |

旧引擎严格「按行号对齐」→ 遇到 2025 版整段错位：19 条 WARN、`CD/KD=0`、
纸张/印色全丢、`CPSL1~3` 全 0，而四道门照样全绿（静默丢数据）。

本文件钉住四件事：
1. 自适应分区（`layout.sections_adaptive`）能跨版本按**标签**定位；
2. 值跨行（`value_continue`）把 `Flexcover` + `thread-stitching` 拼起来；
3. 一排档位（`5.000 / 7.500 / 10.000`）落 `CPSL1..3`，不是全 0；
4. `Flexcover` → `CPLB=22.03;假精装书` + `ZDFS=精装书`（历史同款 `MC25011601` 口径）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from customer_profile import load_profile          # noqa: E402
from extract_fields import CustomerData, _extract_sections  # noqa: E402
from main import map_fields, parse_tier_list       # noqa: E402

NGV = load_profile("ngv")
TEMPLATE = json.loads((REPO_ROOT / "assets" / "add_customer_quote_request.json")
                      .read_text(encoding="utf-8"))

# 逐行复刻 2025-01 版 enquiry 表的 A:L 列（空格子补 ""）
ROWS_2025 = [
    ["enquiry \nAnfrage", "100 Dinge, die du in der Schule nicht lernst", "", "", "", "",
     "", "", "", "", "SST"],
    ["schedule\nTermine", "offer deadline\nAngebotsfrist", "2025-01-22 00:00:00"],
    ["", "ETA w/h\nLieferung", "04.03.26"],
    ["quantity \nAuflage", "5.000 / 7.500 / 10.000"],
    ["versions\nVersionen"],
    ["content \nInhalt", "TPS\nFormat", "160", "x", "200", "mm", "portrait"],
    ["", "extent\nUmfang", "112", "pp", "incl. ends"],
    ["", "paper\nPapier", "130", "gsm", "woodfree white demimatt art paper",
     "", "", "", "FSC mix", "vol.", "0,9"],
    ["", "printing\nDruck", "4x4", "col.", "", "", "", "", "", "6.552",
     "mm bookblock thickness"],
    ["ends \nVor-/Nachsatz", "paper\nPapier", "", "", "", "", "", "", "FSC mix"],
    ["", "printing\nDruck"],
    ["cover \nUmschlag", "boards\nMGP", "", "", "", "", "", "", "FSC mix", "6.552",
     "mm  book thickness"],
    ["", "padding\nWattierung"],
    ["", "paper\nPapier", "300", "gsm glossy art paper (FSC Mix)", "", "", "", "",
     "FSC mix"],
    ["", "printing\nDruck", "4x0", "col."],
    ["", "finishing\nVeredelung", "glossy lamination"],
    ["jacket \nSchutzumschlag", "paper\nPapier"],
    ["", "printing\nDruck"],
    ["", "finishing\nVeredelung"],
    ["finishing \nVerarbeitung", "binding\nBindung", "Flexcover"],
    ["", "", "thread-stitching"],
    ["", "back\nRücken", "round"],
    ["specials \nSonstiges"],
    ["packaging\nVerpackung", "shrinkwrapping\nEinschweißung", "no"],
    ["", "sticker\nAufkleber", "easily removable price sticker (front)"],
    ["", "copies per carton\nExpl. pro Karton"],
    ["", "assortment\nSortierung/VE"],
    ["", "specials\nSonstiges", "Bei Europa-Produktion: Zeitfracht-Menge 3.000;"],
    ["notes\nBemerkungen", "Der Titel soll technisch identisch zu 84197 sein."],
]


class _Quiet:
    def info(self, *a, **k): pass
    def warn(self, *a, **k): pass
    def error(self, *a, **k): pass
    def field_mapping(self, *a, **k): pass


@pytest.fixture(scope="module")
def values() -> dict:
    vals, _, _, _ = _extract_sections(ROWS_2025, NGV, _Quiet())
    return vals


def test_尺寸跨五格也能拼回整串(values):
    """`160 | x | 200 | mm | portrait` → 一个尺寸串（旧引擎只取到 160）。"""
    assert values.get("size") == "160 x 200 mm portrait"


def test_2025版的报价截止在schedule分区里也能取到(values):
    assert values.get("offer_deadline", "").startswith("2025-01-22")


def test_纸张与印色不被拆格丢掉(values):
    assert values.get("text_material", "").startswith("130 gsm woodfree")
    assert values.get("text_printing_spec") == "4x4 col."
    assert values.get("cover_material", "").startswith("300 gsm glossy art paper")
    assert values.get("cover_printing_spec") == "4x0 col."
    assert values.get("finishes") == "glossy lamination"
    assert values.get("extent") == "112 pp incl. ends"


def test_装订工艺跨两行要拼起来(values):
    """`Flexcover` 与下一行 `thread-stitching` 同属装订工艺（2025 版跨行写法）。"""
    # 跨行的是 finishing 分区的 `Bindung` 行（cover 分区的 `binding` 只放 Softcover 这类值）
    assert values.get("binding_instructions") == "Flexcover thread-stitching"
    assert values.get("back") == "round"


def test_包装分区多出的两行也要取到(values):
    assert values.get("primary_packaging") == "no"
    assert values.get("sticker") == "easily removable price sticker (front)"


def test_一排档位解析成三档():
    assert parse_tier_list("5.000 / 7.500 / 10.000") == [5000, 7500, 10000]
    # 不是档位写法的别乱认（只认整段是数字的片段）
    assert parse_tier_list("perfect binding at the top") == []
    assert parse_tier_list("3k") == []


def _map(values: dict) -> dict:
    return map_fields(CustomerData(values=dict(values)), TEMPLATE, NGV, _Quiet())


def test_Flexcover落假精装书与精装书(values):
    got = _map(values)
    assert got["CPLB"] == "22.03;假精装书", "软封套硬壳 = 假精装（历史同款 MC25011601 口径）"
    assert got["ZDFS"] == "精装书", "Flexcover 是皮壳类，不该落空"


def test_尺寸与厚度落库(values):
    got = _map(values)
    assert (got["CD"], got["KD"]) == (160, 200)
    assert got["GD"] == 0, "表里没给厚度 → 0（不做推算）"
