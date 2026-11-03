"""parse_titel_mengen：Titel+Mengen（多国多款数量表）解析。

用合成工作簿验证结构判定 —— 不依赖客户真实附件（那些在 customer-files/ 里，不入库）。
"""
import sys
from pathlib import Path

import openpyxl
import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import parse_titel_mengen as ptm  # noqa: E402


def build(tmp_path: Path, blocks, extra_cols=True) -> Path:
    """blocks = [(land, [(title_de, specs, qty), …]), …]；同一 land 内数量档由 qty 给。

    刻意复刻真实表的两个坑：① 表头在第 5 行、前 3 行是项目头；
    ② `Land` 是合并单元格（只有首行为值），解析必须向下继承。
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Expl"
    ws["A1"], ws["B1"], ws["C1"], ws["H1"] = "VJ", "A000xx", "Name der Aktion", "ALDI Süd"
    ws["A2"], ws["B2"], ws["C2"], ws["H2"] = "LJ", "A00123", "Testaktion", "HOFER"
    ws["A3"] = "Version: 1.0"
    headers = {2: "Kunde", 3: "Land", 5: "delivery date", 14: "specs",
               15: "assortment", 16: "title name German", 17: "title name English",
               19: "QTY per title", 20: "total QTY"}
    for col, label in headers.items():
        ws.cell(ptm.HEADER_ROW_HINT, col, label)
    row = ptm.HEADER_ROW_HINT + 1
    for land, titles in blocks:
        first = row
        for title_de, specs, qty in titles:
            ws.cell(row, 3, land if row == first else None)     # 合并单元格：只有首行有值
            ws.cell(row, 16, title_de)
            ws.cell(row, 17, title_de + " EN")
            ws.cell(row, 14, specs)
            ws.cell(row, 15, "A")
            ws.cell(row, 19, qty)
            ws.cell(row, 20, sum(t[2] for t in titles) if row == first else None)
            if extra_cols:                                       # 无表头列（J/L/M）
                ws.cell(row, 10, 20)
                ws.cell(row, 12, 35)
                ws.cell(row, 13, "FSM")
            row += 1
    out = tmp_path / "Anfrage_Titel+Mengen_A00123_Test.xlsx"
    wb.save(out)
    return out


def test_合并单元格的国家要向下继承(tmp_path):
    path = build(tmp_path, [("DE", [("T1", "160 x 210, 128pp, 4/4", 1000),
                                    ("T2", "160 x 210, 128pp, 4/4", 2000)]),
                            ("AT", [("T1", "160 x 210, 128pp, 4/4", 500),
                                    ("T2", "160 x 210, 128pp, 4/4", 0)])])
    data = ptm.parse(path)
    assert [b["land"] for b in data["blocks"]] == ["DE", "AT"], "Land 合并单元格必须继承，否则区块会碎"
    assert data["blocks"][0]["qty_sum"] == 3000
    assert data["blocks"][1]["qty_sum"] == 500


def test_款级汇总去重升序且去掉零量级(tmp_path):
    path = build(tmp_path, [("DE", [("T1", "s", 1000), ("T2", "s", 2000)]),
                            ("AT", [("T1", "s", 500), ("T2", "s", 0)])])
    data = ptm.parse(path)
    t1 = next(t for t in data["titles"] if t["title_de"] == "T1")
    assert t1["quantities"] == [500, 1000], "量级要升序、去重、剔除 0"
    assert t1["total_qty"] == 1500
    assert t1["countries"] == 2


def test_超过十个量级要报警告(tmp_path):
    """ERP 只有 CPSL1..10 —— 一款出现 11 个不同量级时必须提示，不能默默截断。"""
    titles = [("T1", "s", q) for q in range(100, 1200, 100)]      # 11 个不同量级
    path = build(tmp_path, [(f"L{i}", [t]) for i, t in enumerate(titles)])
    data = ptm.parse(path)
    assert any("CPSL1..10" in w for w in data["warnings"])


def test_无表头列只记录原值不猜含义(tmp_path):
    path = build(tmp_path, [("DE", [("T1", "s", 1000)])])
    data = ptm.parse(path)
    assert any("没有表头" in w for w in data["warnings"])
    blk = data["blocks"][0]
    assert blk["unlabeled_cols"] == {"col10": 20, "col12": 35, "col13": "FSM"}


def test_项目头与重复表头行被跳过(tmp_path):
    """第 1-3 行是项目头、中间可能再插一行表头 —— 都不能当成产品行。"""
    path = build(tmp_path, [("DE", [("T1", "s", 1000), ("T2", "s", 1000)])])
    wb = openpyxl.load_workbook(path)
    ws = wb.active
    ws.cell(9, 16, "Titelbezeichnung DE")        # 重复表头行
    wb.save(path)
    data = ptm.parse(path)
    assert data["n_rows"] == 2
    assert data["header"]["project_no"] == "A00123"
    assert data["header"]["channels"] == ["ALDI Süd", "HOFER"]
