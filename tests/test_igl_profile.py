"""IGL 客户档案回归测试（2026-10-29 `BIBLE HEROES Treasury` 实单暴露的三个问题）。

实单形态（`customer-files/inbox/20261029/Bible Heroes Treasury - Costs required.eml`
+ `BIBLE HEROES Treasury.xlsx`，Igloo Books / Christine Still）：

    Format            Hardback
    Size (tps)        250 x 210mm          ← 进 CD/KD
    Size (actual)     256 x 216mm          ← 只进 CPMS（皮壳外廓）
    Extent            64pp text + 8pp ends + cased cover
    Text Material     128gsm matt art paper
    End Paper        160gsm woodfree，4/4
    Cover Material    128gsm Gloss Artpaper over 3mm boards
    Binding           Cased book, section sewn, cover drawn on over 3mm boards,
                      ends secured, square backed

三条断言对应三个真 bug / 真缺口：

1. `cased` 不在 `binding.map` 里 → 落到最后的 `sewn` → `ZDFS=车线书`；
   而语料 1597 条 `CPLB=方脊精装书` 里 1588 条 `ZDFS=精装书`（99.4%）。
2. `GD` 只算「书芯 + 封面纸」，**漏了皮壳两块灰板** → 64PP/128g 出 5.16mm，
   成品实际约 11mm（业务非 0 的样本：`160*196*10` 3mm 灰板 / `210*285*8` 1200g 灰板）。
3. `Series Name` 写成 `Working Title: 'BIBLE HEROES Treasury'` → 直接拼 CPMC 带前缀与引号。
"""
from __future__ import annotations

import json
from pathlib import Path

from customer_profile import load_profile
from extract_fields import CustomerData
from main import board_allowance, map_fields

REPO_ROOT = Path(__file__).resolve().parent.parent
IGL = load_profile("igl")
TEMPLATE = json.loads((REPO_ROOT / "assets" / "add_customer_quote_request.json")
                      .read_text(encoding="utf-8"))

BIBLE = {
    "series_name": "Working Title: 'BIBLE HEROES Treasury'",
    "format": "Hardback",
    "size": "250 x 210mm",
    "size_actual": "256 x 216mm",
    "extent": "64pp text + 8pp ends + cased cover",
    "text_material": "128gsm matt art paper",
    "ends_material": "160gsm woodfree paper",
    "cover_material": "128gsm Gloss Artpaper over 3mm boards",
    "binding": ("Cased book, section sewn, cover drawn on over 3mm boards, "
                "ends secured, square backed"),
    "finishes": "Matt lam",
    "number_of_titles": "1",
    "target_age_group": "5+",
    "safety_checks": "y",
    "works_references": "24638",
}


class _Quiet:
    def info(self, *a, **k): pass
    def warn(self, *a, **k): pass
    def error(self, *a, **k): pass
    def field_mapping(self, *a, **k): pass


def _map(values: dict) -> dict:
    return map_fields(CustomerData(values=dict(values)), TEMPLATE, IGL, _Quiet())


def test_精装皮壳_cased要落精装书_不许落车线书():
    """`Cased book, section sewn` = 皮壳 + 锁线 = 精装。

    旧 map 只有 `case bound`（匹配不上 `cased`）与 `sewn` → 静默落 `车线书`。
    语料 1597 条 `CPLB=方脊精装书` 中 1588 条 `ZDFS=精装书`；`车线书` 那 157 条
    搭配的是 `普通车线书` / `精装车线书`（25 类，软封面车线），不是精装。
    """
    got = _map(BIBLE)
    assert got["CPLB"] == "22.02;方脊精装书"
    assert got["ZDFS"] == "精装书", "皮壳 + 锁线是精装；车线书是软封面车线"


def test_没有皮壳线索时_sewn仍然落车线书():
    """别把 `sewn` 整条删掉 —— 客户只写 `section sewn`（软封面）时仍是车线书。"""
    got = _map({"binding": "section sewn, soft cover", "format": "Paperback"})
    assert got["ZDFS"] == "车线书"


def test_精装灰板要算进GD():
    """GD = 书芯 + 前后两块灰板（档案 rules.thickness.boards）。

    64PP/128g + 8PP/160g 书芯算出 5.16mm；`over 3mm boards` → +6.0mm = 11.16mm。
    业务非 0 的 26 条精装记录全是「书芯 + 两块板」口径（8 / 10 / 16mm）。
    """
    allowance = board_allowance(IGL, CustomerData(values=dict(BIBLE)), _Quiet())
    assert allowance == 6.0, "3mm × 2 块 = 6mm"

    # 平装没有皮壳 → 不加，基线样本（PUZZLE COLOUR DISNEY）不受影响
    assert board_allowance(IGL, CustomerData(values={"binding": "perfect bound",
                                                     "cover_material": "250gsm art card"}),
                           _Quiet()) == 0.0


def test_系列名要去掉Working_Title前缀():
    """`Working Title: 'BIBLE HEROES Treasury'` → `BIBLE HEROES Treasury`（不翻译）。"""
    got = _map(BIBLE)
    assert got["CPMC"] == "22.02;方脊精装书-BIBLE HEROES Treasury"


def test_审计派生GD_只认与产物字段一致的值():
    """概要行 GD 是派生值（客户文件里没有）。审计按「等于产物 GD 字段」放行并打印理由。

    值改掉（对不上字段）就不放行 —— 否则任何编造的数字都能蹭 `derived`。
    """
    from cpms_content_audit import audit

    cpms = ("书籍方向：【长书脊】产品规格：【250*210*11.16】\n\n"
            "内文：64PP，128g哑粉，4C/4C\n")
    source = ("Size (tps) 250 x 210mm\n"
              "64pp text 128gsm matt art paper over 3mm boards")

    ok = audit(cpms, source, derived={"11.16": "派生 GD"})
    assert ok["summary"]["untraced"] == 0
    assert [t["token"] for t in ok["derived"]] == ["11.16"]

    bad = audit(cpms, source, derived={"9": "派生 GD"})
    assert bad["summary"]["untraced"] == 1, "对不上产物 GD 的派生值不许放行"
