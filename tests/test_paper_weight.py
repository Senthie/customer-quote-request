"""paper_weight：客户克重 → 我们常用克重（assets/page_thickness.xlsx）。

业务口径（2026-11-10 森西）：客户给的克重我们不常用时（例 310g单粉），
CPMS 里写**最近的常用克重**（300g），并在备注里向客户说明「我们按 300g 报价」。
所以判据只有三条：命中 / 就近（并列取大）/ 不认识的纸型不许猜。
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import paper_weight as PW  # noqa: E402


def test_表能读到且单粉清单对得上():
    t = PW.load_table()
    assert "单粉" in t and "书纸" in t, "page_thickness 的 page_style 被改名了？"
    gsms = [g for g, _ in t["单粉"]]
    assert gsms == sorted(gsms), "克重要升序，就近匹配依赖它"
    assert 300 in gsms and 310 not in gsms


def test_常用克重原样通过():
    r = PW.resolve("单粉", 230)
    assert r["used"] == 230 and r["substituted"] is False and r["delta"] == 0
    assert PW.note(r) == "", "没换算就不该有备注"


def test_不常用克重取最近并生成备注():
    """森西举的例子：310g单粉 → 300g单粉。"""
    r = PW.resolve("单粉", 310)
    assert r["used"] == 300, "最近常用克重是 300（不是 350）"
    assert r["substituted"] is True and r["delta"] == -10
    note = PW.note(r)
    assert "300g单粉" in note and "310g单粉" in note and "不常用" in note


def test_并列时取较大克重():
    """275g 距 250 与 300 都是 25 —— 宁厚勿薄（书脊算窄会装不下）。"""
    assert PW.resolve("单粉", 275)["used"] == 300


def test_纸型不在表里就不猜():
    """page_thickness 没有「双粉」这一行；光粉/哑粉是两种不同表面，猜错等于报错价。"""
    r = PW.resolve("双粉咭", 230)
    assert r["used"] is None and "未收录" in r["reason"]


def test_多了限定词就是另一种纸_不许按同族顶替():
    """历史产物里真实出现过这些写法 —— 都不是「单粉/光粉/贴纸」本身。"""
    for raw in ("高松厚单粉", "光粉不干胶", "铜版贴纸", "木浆纸", "光粉裱", "单粉纸盒"):
        assert PW.normalize_style(raw) is None, f"{raw} 不该被当成同族纸型"
    # 但同一种纸的另一种叫法必须认
    for raw, want in (("单粉咭", "单粉"), ("单粉卡", "单粉"), ("单粉纸", "单粉"),
                      ("哑粉咭", "哑粉"), ("粉灰咭", "粉灰")):
        assert PW.normalize_style(raw) == want


def test_差距过大不自动换算():
    """贴纸表里只有 200g，客户要 80g —— 差 150%，一定不是同一支纸。"""
    r = PW.resolve("贴纸", 80)
    assert r["used"] is None and "需人工确认" in r["reason"]


def test_料号里的数字不当克重():
    """`6366gISBN` 这种是料号，不该进换算。"""
    assert [s for g, s in PW.parse_tokens("6366gISBN") ] == []


def test_客户原文与CPMS里的写法都能认():
    assert PW.normalize_style("单粉咭") == "单粉"
    assert PW.normalize_style("哑粉纸") == "哑粉"
    assert PW.normalize_style("wood-free white offset paper") == "书纸"
    assert PW.normalize_style("art card C1S") == "单粉"

    toks = PW.parse_tokens("Content: 100 gsm wood-free white offset paper (FSC-Mix)")
    assert (100.0, "wood-free white offset paper") in [(g, s) for g, s in toks]


def test_体检一段CPMS能抓出所有克重():
    text = ("内文：80pp，100g书纸，4C/4C\n"
            "封面：310g单粉，4C+UV/0C")
    r = PW.check(text)
    assert r["bad"] == 1, "310g 要换算，100g 是常用克重不该报警"
    subs = [x for x in r["results"] if x["substituted"]]
    assert subs and subs[0]["used"] == 300


def test_换算值要能落进无出处白名单():
    """CPMS 写 300g 而客户文件写 310g —— cpms_content_audit 会判「无出处」，
    所以换算必须同时给出 cpms_accepted.json 条目（含理由）。"""
    entry = PW.accepted_entry(PW.resolve("单粉", 310))
    assert "300g" in entry
    assert "310g单粉" in entry["300g"] and "business" not in entry["300g"]
    assert PW.accepted_entry(PW.resolve("单粉", 230)) == {}
