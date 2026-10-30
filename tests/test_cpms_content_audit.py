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


# ── 1.0.49：一份报价 = 多个文件（单元目录）时，语料要合起来审 ──────────────
def test_单元目录里的多个文件当一个语料(tmp_path):
    """LG26090401 实测：数量只在 `Titel+Mengen` 里、规格只在 enquiry 表单里。

    只喂其中一个文件 → 另一个文件的事实被误报「无出处」，逼人删掉真信息。
    """
    unit = tmp_path / "unit"
    unit.mkdir()
    (unit / "spec.txt").write_text("cover 310 gsm art card C1S\nrounded corners", encoding="utf-8")
    (unit / "titel.txt").write_text("QTY per title 410000\ncps/carton 20", encoding="utf-8")
    (unit / "spec.txt.meta.json").write_text("{}", encoding="utf-8")   # 副档不参与

    files = A.expand_sources([str(unit)])
    assert [f.name for f in files] == ["spec.txt", "titel.txt"], files

    source_text = "\n".join(A.read_source(f) for f in files)
    r = A.audit("内文：310g单粉，410000 本，20本/箱，圆角", source_text)
    assert r["summary"]["untraced"] == 0, r["untraced"]


def test_多个source可并列传(tmp_path):
    a = tmp_path / "a.txt"; a.write_text("410000", encoding="utf-8")
    b = tmp_path / "b.txt"; b.write_text("310 gsm", encoding="utf-8")
    files = A.expand_sources([str(a), str(b)])
    assert [f.name for f in files] == ["a.txt", "b.txt"]


def test_灰板mm换克重不算编数字():
    """CU26072401（2026-10-21）：附件写 `boards 2 mm`，规范要求灰板一律写 g → `1200g灰板`。

    审计曾把这个换算判成「无出处」，逼人把合规写法改回 mm（又踩 CPMS054）。
    """
    r = A.audit("皮壳：128g光粉(FSC)+1200g灰板(FSC)", "cover: 128 gsm glossy art paper\nboards 2 mm")
    assert r["summary"]["untraced"] == 0, r["untraced"]


def test_灰板换算是双向核对的_没写厚度仍然报():
    r = A.audit("皮壳：128g光粉(FSC)+1500g灰板(FSC)", "cover: 128 gsm glossy art paper\nboards 2 mm")
    assert any(t["token"].startswith("1500") for t in r["untraced"]), r["untraced"]


def test_数字要按独立数字匹配_料号里的子串不算出处():
    """CU26072401：CPMS 写 `140g光粉`，料号 `1240700` 里含 `140` → 旧逻辑直接放过。

    子串匹配等于让被审对象自己作证：业务口径（精装封面 140g）必须走白名单才许放行。
    """
    r = A.audit("封面：140g光粉(FSC)", "article no 18/20/1240700/FSM")
    assert [t["token"] for t in r["untraced"]] == ["140g"], r["untraced"]


def test_口径白名单要留痕_不是静默放行():
    """业务拍板要写、客户文件里却没有的值 → 走 --accepted，报告里必须看得见理由。"""
    accepted = {"140g": "精装封面面纸口径（2019 旧单 PO#13826 材质），业务 2026-10-21 拍板"}
    r = A.audit("封面：140g光粉(FSC)", "cover 128 gsm glossy art paper", accepted)
    assert r["summary"]["untraced"] == 0
    assert r["summary"]["accepted"] == 1
    assert "PO#13826" in r["accepted"][0]["reason"]


def test_我们自己的产物不许当溯源出处(tmp_path):
    """CU26072401 踩过：`CPMS_来源对照.md` 躺在产物目录里，审计拿它当客户原文 →
    业务口径的 140g 自己给自己作证。"""
    unit = tmp_path / "u"
    unit.mkdir()
    (unit / "enquiry.txt").write_text("cover 128 gsm glossy art paper", encoding="utf-8")
    (unit / "CPMS_来源对照.md").write_text("封面：140g光粉(FSC)", encoding="utf-8")
    (unit / "CU1.extracted.json").write_text('{"CPMS": "140g光粉"}', encoding="utf-8")
    files = A.expand_sources([str(unit)])
    assert [f.name for f in files] == ["enquiry.txt"], [f.name for f in files]


def test_邮件正文算客户原话_副档里的抽取结果不算(tmp_path):
    """邮件是客户材料（`printed in 2019` 只在邮件里）；但我们自己写进副档的
    `numbers`（抽错的号）绝不能当出处。"""
    unit = tmp_path / "u"
    unit.mkdir()
    (unit / "enquiry.txt").write_text("boards 2.0 mm", encoding="utf-8")
    (unit / "enquiry.txt.meta.json").write_text(
        '{"subject": "reprint", "body": "reprint of PO#13826, printed in 2019",'
        ' "numbers": {"KHDH": "13826"}}', encoding="utf-8")
    text = A.email_text([str(unit)]) + "\n" + "\n".join(
        A.read_source(f) for f in A.expand_sources([str(unit)]))
    r = A.audit("备注：2019 旧单再版；皮壳：1200g灰板", text)
    assert r["summary"]["untraced"] == 0, r["untraced"]


def test_日期单元格的日号可溯源_不许把日期与时间粘成数字(tmp_path):
    """2026-10-29 实测踩坑：`str(datetime)` = "2027-04-30 00:00:00"，
    `normalize()` 去掉空白后粘成 "2027-04-3000:00:00" —— 日号 `30` 前后都是数字，
    被判「无出处」，逼人把客户真实交期从 CPMS 里删掉。日期是客户原文事实，不许丢。"""
    import datetime as dt

    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws["C3"] = dt.datetime(2027, 4, 30)
    ws["C4"] = dt.datetime(2027, 9, 3)
    path = tmp_path / "enquiry.xlsx"
    wb.save(path)

    text = A.read_source(path)
    assert "2027-04-30" in text and "00:00:00" not in text, text
    r = A.audit("备注：文件到印厂 2027-04-30，交期 2027-09-03", text)
    assert r["summary"]["untraced"] == 0, r["untraced"]


def test_灰板数字与mm分处两格仍认灰板换算(tmp_path):
    """xlsx 里 `2` 与 `mm` 分处两格（C13/D13）—— 读取时用 `|` 分隔，
    灰板 mm→g 的换算必须照样认（否则规范要求的 `1200g灰板` 反被判无出处）。"""
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws["B13"] = "boards"
    ws["C13"] = 2
    ws["D13"] = "mm"
    path = tmp_path / "enquiry.xlsx"
    wb.save(path)

    r = A.audit("皮壳：1200g灰板(FSC)，4C/0C", A.read_source(path))
    assert r["summary"]["untraced"] == 0, r["untraced"]


def test_客户写cm_CPMS写mm算可溯源():
    """Lidl `Anlage`（2026-10-29）：客户表尺寸写 `20 x 20 x 2 cm`，
    CPMS 规范与 ERP 的 `CD/KD` 都是 mm（`200*200*0`）—— 机械换算不是编数字。
    """
    r = A.audit("书籍方向：【长书脊】产品规格：【200*200*0】", "20 x 20 x 2 cm, 25 x 25,3 x 2,3 cm")
    tokens = [t["token"] for t in r["untraced"]]
    assert "200" not in tokens, r["untraced"]


def test_cm换算带小数的也认():
    r = A.audit("产品规格：【250*253*0】", "25 x 25,3 x 2,3 cm")
    tokens = [t["token"] for t in r["untraced"]]
    assert "253" not in tokens, r["untraced"]


def test_cm换算是双向核对_编的毫米数仍然报():
    r = A.audit("产品规格：【300*300*0】", "20 x 20 x 2 cm")
    assert any(t["num"] == "300" for t in r["untraced"]), r["untraced"]


def test_原文没有cm时_毫米换算不生效():
    r = A.audit("产品规格：【200*200*0】", "20 x 20 x 2 mm")
    assert any(t["num"] == "200" for t in r["untraced"]), r["untraced"]
