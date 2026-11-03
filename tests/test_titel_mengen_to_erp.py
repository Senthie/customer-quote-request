"""titel_mengen_to_erp：项目级多国多款表 → ERP 记录。

重点钉住两件事：
1. 表尾「语言图例」的识别 —— 正文 R/S 列同样有 `ja` / `4` / `A` 这些杂值，
   不挡掉就会把 YYS 算成 34 种语言（2026-11-03 A00130 实测踩过）。
2. 规格块 / 尺寸顺序 / 装订映射 —— 客户文件里写着的东西照抄，没写的（GD）不推算。
"""
import json
import sys
from pathlib import Path

import openpyxl
import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import titel_mengen_to_erp as t2e  # noqa: E402

SPEC_ROWS = {
    "A": ["A", "160 x 210 mm", "128 pp + cover",
          "text: 120 gsm offset paper, 4/4 c",
          "cover: 230 gsm art card C1S, 4/0 + UV-varnish",
          "softcover, perfect binding, square back"],
    "B": ["B", "190 x 245 mm", "144 pp + cover",
          "text: 100 gsm woodfree, 4/4 c",
          "cover: 300 gsm art card C1S, 4/0 + UV-varnish",
          "softcover, perfect binding, square back"],
}
LEGEND = [("German", 20, 24), ("English", 21, 12), ("French", 22, 0),
          ("Plish", 30, 4), ("Italian", 26, 0)]
TITLES = [("Killdoku", "Killdoku", "A", 1000),
          ("Killdle I", "Killdle I", "A", 2000),
          ("Landkarten-Rätsel", "Puzzle Book of Maps", "B", 500)]


def build(tmp_path: Path) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Expl"
    ws["A1"], ws["A2"], ws["B2"], ws["C2"] = "VJ", "LJ", "A00123", "Testaktion"
    headers = {2: "Kunde", 3: "Land", 14: "specs", 16: "title name German",
               17: "title name English", 19: "QTY per title"}
    for col, label in headers.items():
        ws.cell(5, col, label)
    # 正文 3 行产品；R/S 列故意塞 `ja`/`4` 这种杂值（真实表就是这样）
    for i, (de, en, group, qty) in enumerate(TITLES):
        r = 6 + i
        ws.cell(r, 2, "ALDI Süd")
        ws.cell(r, 3, "DE")
        ws.cell(r, 14, "160 x 210, 128pp, 4/4")
        ws.cell(r, 16, de)
        ws.cell(r, 17, en)
        ws.cell(r, 19, qty)
        ws.cell(r, 18, "ja")
        ws.cell(r, 19 + 0, qty)
        ws.cell(r, 20, 15)
    # 表尾规格块（B 列）+ 语言图例（R/S/T）+ 逐款汇总（P/R/S）
    row = 20
    for spec in SPEC_ROWS.values():
        for line in spec:
            ws.cell(row, 2, line)
            row += 1
    row += 1
    for name, code, n in LEGEND:
        ws.cell(row, 18, name)
        ws.cell(row, 19, code)
        ws.cell(row, 20, n)
        row += 1
    row += 1
    ws.cell(row, 16, "Titelbezeichnung DE")
    ws.cell(row, 17, "title name English")
    row += 1
    for de, en, group, qty in TITLES:
        ws.cell(row, 16, de)
        ws.cell(row, 17, en)
        ws.cell(row, 18, group)
        ws.cell(row, 19, qty)
        row += 1
    out = tmp_path / "Anfrage_Titel+Mengen_A00123_Test.xlsx"
    wb.save(out)
    return out


def template_record() -> dict:
    """最小骨架：只要键存在就行，脚本是「读模板再覆盖」。"""
    keys = ["BDBH", "YWY", "KHZL", "CPDL", "CPLB", "Drop_SJFX", "ZDFS", "CPMC",
            "TZS", "YYS", "DWMC", "BZ", "KHMBDJ", "CD", "KD", "GD", "BZFS",
            "SHFS", "JHRQ", "CPMS", "BGMS", "ECPMC", "ID", "CPMC_ZH",
            "Reminder", "version", "schema_version", "question_answers"]
    keys += [f"CPSL{i}" for i in range(1, 11)]
    rec = {k: "" for k in keys}
    rec.update({"KHZL": "", "CPMS": "", "CPSL1": 0, "version": 2, "schema_version": 3,
                "question_answers": []})
    return rec


def test_语言图例只认语言名不认正文杂值(tmp_path):
    path = build(tmp_path)
    ws = openpyxl.load_workbook(path, data_only=True).worksheets[0]
    legend = t2e.read_language_legend(ws)
    assert [x["name"] for x in legend] == ["German", "English", "French", "Plish", "Italian"], \
        "正文 R/S 列的 'ja'/'4'/'A' 不能算进语言图例，否则 YYS 会翻几倍"
    assert [x["code"] for x in legend] == [20, 21, 22, 30, 26]


def test_规格块按组号切段(tmp_path):
    path = build(tmp_path)
    ws = openpyxl.load_workbook(path, data_only=True).worksheets[0]
    specs = t2e.read_spec_block(ws)
    assert set(specs) == {"A", "B"}
    assert specs["A"]["pages"] == "128 pp + cover"
    assert "120 gsm offset" in specs["A"]["text"]
    assert "perfect binding" in specs["A"]["binding"]
    assert "100 gsm woodfree" in specs["B"]["text"]


def test_尺寸按书写顺序落CD_KD():
    assert t2e.size_to_cd_kd("160 x 210 mm") == (160, 210)
    assert t2e.size_to_cd_kd("240*330 mm") == (240, 330)
    assert t2e.size_to_cd_kd("没有尺寸") is None


def test_装订认不出就报错不猜():
    assert t2e.binding_to_erp("softcover, perfect binding, square back") == \
        ("无线胶装书", "胶装/PAD书")
    with pytest.raises(SystemExit):
        t2e.binding_to_erp("hardcover, sewn")


def test_端到端出记录_不填CPSL_不填CPMS(tmp_path, monkeypatch):
    xlsx = build(tmp_path)
    tpl = tmp_path / "tpl.json"
    tpl.write_text(json.dumps([template_record()], ensure_ascii=False), encoding="utf-8")
    names = tmp_path / "names.json"
    names.write_text(json.dumps({"A": "测试书（2款）", "B": "地图篇"}, ensure_ascii=False),
                     encoding="utf-8")
    out = tmp_path / "ERP.json"
    monkeypatch.setattr(sys, "argv", [
        "titel_mengen_to_erp.py", str(xlsx), "--template", str(tpl),
        "--names", str(names), "--out", str(out), "--asked-at", "2026-11-03"])
    assert t2e.main() == 0

    doc = json.loads(out.read_text(encoding="utf-8"))
    assert [r["TZS"] for r in doc] == [2, 1], "TZS = 按规格组归并后的款数"
    assert all(r["YYS"] == 3 for r in doc), "YYS = 图例里 T>0 的语言种数（German/English/Plish）"
    assert all(r["CPMS"] == "" for r in doc), "默认不写 CPMS（业务 2026-11-03 口径）"
    assert all(r[f"CPSL{i}"] == 0 for r in doc for i in range(1, 11)), "CD/KD/CPSL 不能瞎填"
    assert doc[0]["CD"] == 160 and doc[0]["KD"] == 210 and doc[0]["GD"] == 0
    assert doc[0]["CPLB"] == "无线胶装书" and doc[0]["ZDFS"] == "胶装/PAD书"
    assert doc[0]["ECPMC"] == "Killdoku / Killdle I", "ECPMC 照抄客户款名，不译"
    assert doc[0]["CPMC"] == "测试书（2款）"
    qa = {q["id"]: q for q in doc[0]["question_answers"]}
    assert qa["Q_CPMS"]["status"] == "confirmed"
    assert qa["Q_CPSL_QTY"]["status"] == "confirmed"


def test_缺中文品名要报错而不是留占位符(tmp_path, monkeypatch):
    xlsx = build(tmp_path)
    tpl = tmp_path / "tpl.json"
    tpl.write_text(json.dumps([template_record()]), encoding="utf-8")
    names = tmp_path / "names.json"
    names.write_text(json.dumps({"A": "只有A组"}), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [
        "titel_mengen_to_erp.py", str(xlsx), "--template", str(tpl),
        "--names", str(names), "--out", str(tmp_path / "o.json")])
    with pytest.raises(SystemExit):
        t2e.main()
