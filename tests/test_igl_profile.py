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


def test_没有厚度就不推算_GD落0():
    """2026-10-21 业务拍板：**不做厚度推算**，客户没给厚度 → GD = 0。

    这单客户只给 `Size (tps) 250 x 210mm` + `Spine width: To suit extent`，
    旧逻辑会按「64PP/128g + 3mm 灰板」推成 11.16mm —— 业务否掉了：
    推算值没有客户出处（审计对不上、报价也没人认），留 0 更安全。
    """
    got = _map(BIBLE)
    assert got["CD"] == 250 and got["KD"] == 210
    assert got["GD"] == 0, f"客户没给厚度 → GD=0，实际 {got['GD']!r}"
    # 档案两处开关都必须是关的（少关一处就会悄悄推算出 5.16 / 11.16）
    assert IGL.rule("thickness.enabled") is False
    assert (IGL.rule("thickness.boards") or {}).get("enabled") is False


def test_客户给了厚度就照抄():
    """客户写了 `D`（`Size … H x W x D`）就是客户的事实 → 照抄，不覆盖。"""
    got = _map({**BIBLE, "size": "250 x 210 x 10mm"})
    assert got["GD"] == 10


def test_灰板补偿能力仍在_只是igl关掉了():
    """`board_allowance` 是通用能力（开关在档案）：igl 关掉后返回 0，
    但把 boards 打开时仍按「板厚 × 块数」算 —— 防止以后有人删代码把能力弄丢。
    """
    class _BoardsOn:
        def rule(self, path, default=None):
            if path == "thickness.boards":
                return {"enabled": True, "source_keys": ["cover_material", "binding"],
                        "trigger": "cased|hardcover|精装",
                        "pattern": r"(\d+(?:\.\d+)?)\s*mm\s*(?:boards?|灰板)",
                        "panels": 2}
            return default

    assert board_allowance(IGL, CustomerData(values=dict(BIBLE)), _Quiet()) == 0.0, \
        "igl 档案关掉 boards → 不补偿"
    assert board_allowance(_BoardsOn(), CustomerData(values=dict(BIBLE)), _Quiet()) == 6.0, \
        "3mm × 2 块 = 6mm（能力保留）"


def test_系列名去掉Working_Title前缀后进ECPMC():
    """`Working Title: 'BIBLE HEROES Treasury'` → `BIBLE HEROES Treasury`（不翻译）。

    2026-10-21 起系列名不再拼进 CPMC（那是中文译名的位置），它的去处是
    `ECPMC`（报价原始产品名，客户原文照抄）。
    """
    got = _map(BIBLE)
    assert got["ECPMC"] == "BIBLE HEROES Treasury"
    assert "Working Title" not in got["ECPMC"]
    assert got["CPMC"] == "【中文品名待定】", "CPMC 是中文译名位（agent 补），脚本给占位符"
    assert got["CPMC"] == got["CPMC_ZH"] if "CPMC_ZH" in got else True


def test_CPMC口径是中文译名_而不是旧英文串():
    """业务 2026-10-21：IGL 按 NGV 口径 —— `CPMC` = 产品名称中文译名。

    旧口径 `23.01;无线胶装书-<系列名>` 是历史 ERP 记录的写法，已作废。
    档案必须：`cpmc.needs_agent: true` + `cpmc.allow_chinese: true` + 中文占位符。
    """
    assert IGL.rule("cpmc.needs_agent") is True
    assert IGL.rule("cpmc.allow_chinese") is True
    assert IGL.rule("cpmc.template") == "【中文品名待定】"
    assert IGL.rule("cpmc.zh_template") == "【中文品名待定】"
    assert IGL.rule("ecpmc.source_keys") == ["series_name"]
    got = _map(BIBLE)
    assert not got["CPMC"].startswith("22.02;"), "CPMC 不许再是「类别;名称-系列」的英文串"


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
