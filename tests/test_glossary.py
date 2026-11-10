"""glossary：华辉内部中英单词表查询 / 用词体检。

词表是业务 2026-11-10 上传的内部口径（`docs/华辉内部中英单词表.txt`）：
进 ERP 的中文用词优先照它写。测试只钉**判据**，不钉全表内容 ——
词表会增补，钉死条目会让每次更新都红灯。
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import glossary as G  # noqa: E402


def test_词表能加载且有一批条目():
    table = G.load()
    assert len(table) > 500, "内部单词表至少几百条，加载不到说明路径/编码变了"
    assert all(zh for zhs in table.values() for zh in zhs)


def test_精确查不区分大小写且保留多义项():
    assert "无线胶装书" in G.lookup("perfect binding")
    assert G.lookup("Perfect Binding") == G.lookup("perfect binding")
    assert G.lookup("SQUARE BACK") == ["方脊/平脊"]


def test_未收录返回空():
    assert G.lookup("量子纠缠") == []
    assert G.lookup("") == []


def test_子串命中不等于收录():
    """`单粉卡` 只在 `单粉咭(C1S)` 上做子串命中 —— 不能当成合规写法。"""
    assert G.known_zh("单粉咭") == "exact"
    assert G.known_zh("单粉卡") == "fuzzy"
    assert G.known_zh("完全不存在的词") == ""


def test_正文里出现英文就给出词表中文():
    r = G.check("封面：230 art card\n装订：perfect binding, square back")
    en = dict(r["en_hits"])
    assert "perfect binding" in en and "无线胶装书" in en["perfect binding"]
    assert "square back" in en


def test_体检只看材料工艺值_跳过叙述段():
    """装箱/备注/概要行是叙述区，拿材料词表卡它们只会产生噪声。"""
    text = "\n".join([
        "书籍方向：【长书脊】产品规格：【160*210*0】共【5】款，每款【7】语言",
        "封面：230g单粉咭(C1S)，4C+UV/0C",
        "装订：无线胶装书，方脊",
        "装箱：待确认（客户附表只给总箱数，未给每箱本数）",
        "备注：交期按邮件 CRD（客户原文 August 7th, 2027）",
    ])
    assert G.check(text)["zh_unknown"] == []


def test_词表外写法会被点名():
    r = G.check("封面：230g单粉卡，4C+UV/0C")
    assert r["zh_unknown"] == ["单粉卡"], "自己另造的近义词必须被点名，别静默放行"


def test_check_json逐条体检产物并在有词表外写法时非零退出(tmp_path, capsys):
    """交付前一条命令：整份 ERP 产物过一遍词表。"""
    good = tmp_path / "good.json"
    good.write_text(json.dumps([{"CPMC": "甲", "CPMS": "封面：230g单粉咭(C1S)，4C+UV/0C"}],
                               ensure_ascii=False), encoding="utf-8")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([{"CPMC": "乙", "CPMS": "封面：230g单粉卡，4C+UV/0C"}],
                              ensure_ascii=False), encoding="utf-8")

    sys.argv = ["glossary.py", "--check-json", str(good)]
    assert G.main() == 0
    assert "✅" in capsys.readouterr().out

    sys.argv = ["glossary.py", "--check-json", str(bad)]
    assert G.main() == 1, "有词表外写法就要非零退出，否则闸门形同虚设"
    assert "词表外  单粉卡" in capsys.readouterr().out
