"""抽取引擎测试：布局策略 / 数量档位 / 尺寸 / 材质映射 / 文本类文件。"""
import pytest

from customer_profile import load_profile
from extract_fields import (
    _extract_rows,
    extract_from_text,
    extract_quantity_tiers,
    load_customer_data,
)
from main import parse_extent, parse_material, parse_size, resolve_page_style

IGL = load_profile("igl")


class _SilentLogger:
    def info(self, *a, **k):
        pass

    def warn(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


LOGGER = _SilentLogger()


# ── 布局策略：横向键值对 ────────────────────────────────────────────────
def test_auto_pairs_横向键值对_值可在空列之后():
    rows = [
        ["", "Imprint", "Autumn", "Complexity", "Low", "Requested By", "", "Christine"],
    ]
    values, raw, layout, _ = _extract_rows(rows, IGL)
    assert values["imprint"] == "Autumn"
    assert values["complexity"] == "Low"
    # 右侧最近的非空且非标签单元格（跳过空格）
    assert values["requested_by"] == "Christine"
    assert "auto_pairs" in layout


def test_auto_pairs_密集标签行_取值在下方():
    rows = [
        ["", "Pub Month", "Series Name", "", "", "Number of titles"],
        ["", "2027-05-01", "Puzzle & Colour Disney", "", "", "2"],
    ]
    values, _, _, _ = _extract_rows(rows, IGL)
    assert values["pub_month"] == "2027-05-01"
    assert values["series_name"] == "Puzzle & Colour Disney"
    assert values["number_of_titles"] == "2"


def test_auto_pairs_密集行下方也是标签时改取右侧():
    """相邻两行都是标签行时，不能把下一行的标签当成值。"""
    rows = [
        ["", "Imprint", "Autumn", "Complexity", "Low"],
        ["", "Pub Month", "Series Name"],          # 又一行标签 → 不能被当成值
    ]
    values, _, _, _ = _extract_rows(rows, IGL)
    assert values["imprint"] == "Autumn"
    assert values["complexity"] == "Low"
    assert "pub_month" not in values               # 下面那行没数据，不该凭空取值


def test_auto_pairs_稀疏标签行_取右侧():
    rows = [["", "Size (tps) H x W x D mm", "198mm x 129mm"]]
    values, _, _, _ = _extract_rows(rows, IGL)
    assert values["size"] == "198mm x 129mm"


def test_先到先得_后面的策略不覆盖已有值():
    rows = [["", "Format", "Paperback"], ["", "Format", "Hardback"]]
    values, _, _, _ = _extract_rows(rows, IGL)
    assert values["format"] == "Paperback"


def test_quantity_tiers_特殊键不被当普通值():
    rows = [["", "Total Costs", "3k", "5k"]]
    values, _, _, _ = _extract_rows(rows, IGL)
    assert "quantity_tiers" not in values


# ── 数量档位 ────────────────────────────────────────────────────────────
def test_档位_带k后缀():
    rows = [["", "Total Costs", "3k", "5k", "7.5k", "10k"]]
    assert extract_quantity_tiers(rows, IGL) == [3000, 5000, 7500, 10000]


def test_档位_纯数字需标签提示():
    rows = [["", "Quantity", "3000", "5000", "7500"]]
    assert extract_quantity_tiers(rows, IGL) == [3000, 5000, 7500]


def test_档位_纯数字无提示标签则不算():
    rows = [["", "Size", "3000", "5000"]]
    assert extract_quantity_tiers(rows, IGL) == []


def test_档位_单独一个k不算():
    rows = [["", "Spine width", "3k"]]
    assert extract_quantity_tiers(rows, IGL) == []


def test_档位_不连续不算():
    rows = [["", "Total Costs", "3k", "封面", "5k"]]
    assert extract_quantity_tiers(rows, IGL) == []


def test_档位_去重并保持顺序():
    rows = [["", "Total Costs", "3k", "5k"], ["", "Total Costs", "3k", "5k"]]
    assert extract_quantity_tiers(rows, IGL) == [3000, 5000]


def test_档位_多组取最长():
    rows = [
        ["", "Total Costs", "3k", "5k"],
        ["", "Total Costs", "3k", "5k", "7.5k", "10k", "20k"],
    ]
    assert extract_quantity_tiers(rows, IGL) == [3000, 5000, 7500, 10000, 20000]


def test_档位_超过上限截断():
    rows = [["", "Total Costs"] + [f"{i}k" for i in range(1, 15)]]
    tiers = extract_quantity_tiers(rows, IGL)
    assert len(tiers) == 10


def test_档位_有标签才认():
    rows = [["3k", "5k", "7.5k"]]      # 前面没有标签单元格
    assert extract_quantity_tiers(rows, IGL) == []


# ── 尺寸 / 材质 / 页数 ──────────────────────────────────────────────────
def _fields(profile, text):
    """去掉 `_raw`（内部用：书名方向换算要客户原始两段，不进 ERP 字段）。"""
    return {k: v for k, v in parse_size(text, profile, LOGGER).items() if not k.startswith("_")}


def test_尺寸_两段只有长宽():
    assert _fields(IGL, "198mm x 129mm") == {"CD": 198, "KD": 129}


def test_尺寸_三段含厚度():
    assert _fields(IGL, "198mm x 129mm x 10mm") == {"CD": 198, "KD": 129, "GD": 10}


def test_尺寸_支持乘号与星号():
    assert _fields(IGL, "198 mm × 129 mm") == {"CD": 198, "KD": 129}


def test_尺寸_NGV按书写顺序落库_不比较长短():
    """业务 2026-10-21：默认顺序「长 x 宽 x 高」→ 148x210 落 CD=148 / KD=210。

    （2026-10-20 那条「长边进 CD」已被真实录入 LG26090401 240x330→240/330 否掉。）
    """
    ngv = load_profile("ngv")
    parsed = parse_size("148 x 210 mm portrait", ngv, LOGGER)
    assert (parsed["CD"], parsed["KD"]) == (148, 210)
    assert parsed["_raw"] == [148, 210], "原始两段要留着 —— 书脊方向靠它算横竖版"


def test_页数解析():
    assert parse_extent("120pp + cover") == (120, 1)
    assert parse_extent("96 pp") == (96, 0)


def test_材质解析克重():
    assert parse_material("90gsm woodfree")[0] == 90
    assert parse_material("250gsm C1S artboard")[0] == 250


def test_材质映射_woodfree_到书纸():
    assert resolve_page_style("90gsm woodfree", "text", IGL, LOGGER) == "书纸"


def test_材质映射_C1S_artboard_到光粉纸():
    assert resolve_page_style("250gsm C1S artboard", "cover", IGL, LOGGER) == "光粉纸"


def test_材质映射_未命中时兜底走_default():
    assert resolve_page_style("300gsm 某种没见过的纸", "text", IGL, LOGGER) == "书纸"


def test_材质映射_客户档案可覆盖规则(tmp_path):
    """换一份档案 → 同一材质串应映射到不同纸张类型（多客户可覆盖的证明）。"""
    from dataclasses import replace
    custom = replace(IGL, rules={
        **IGL.rules,
        "materials": {
            "text": {"default": "哑粉",
                     "map": [{"match": "woodfree", "page_style": "哑粉"}]},
            "cover": IGL.rules["materials"]["cover"],
        },
    })
    assert resolve_page_style("90gsm woodfree", "text", custom, LOGGER) == "哑粉"


# ── 文本类文件 ──────────────────────────────────────────────────────────
def test_文本类_标签冒号值(tmp_path):
    path = tmp_path / "quote.txt"
    path.write_text(
        "Series Name: Puzzle & Colour Disney\n"
        "Format: Paperback\n"
        "Size: 198mm x 129mm\n"
        "Total Costs: 3k 5k 7.5k\n",
        encoding="utf-8")
    data = extract_from_text(str(path), IGL, LOGGER)
    assert data.values["series_name"] == "Puzzle & Colour Disney"
    assert data.values["format"] == "Paperback"
    assert data.values["size"] == "198mm x 129mm"


def test_文本类_标签单独成行时取下一行(tmp_path):
    path = tmp_path / "quote.txt"
    path.write_text("Series Name\nPuzzle & Colour Disney\nFormat\nPaperback\n",
                    encoding="utf-8")
    data = extract_from_text(str(path), IGL, LOGGER)
    assert data.values["series_name"] == "Puzzle & Colour Disney"
    assert data.values["format"] == "Paperback"


def test_文本类_不认识的标签不会乱配(tmp_path):
    path = tmp_path / "quote.txt"
    path.write_text("Wibble: wobble\nFoo: bar\n", encoding="utf-8")
    data = extract_from_text(str(path), IGL, LOGGER)
    assert data.values == {}


def test_文本类_短别名不截胡长标签(tmp_path):
    """档案里 "Series" 与 "Series Name" 并存时，后者才是标签。"""
    path = tmp_path / "quote.txt"
    path.write_text("Series Name\nPuzzle & Colour Disney\n", encoding="utf-8")
    data = extract_from_text(str(path), IGL, LOGGER)
    assert data.values["series_name"] == "Puzzle & Colour Disney"
    assert "Name" not in data.values.values()


# ── 统一入口 ────────────────────────────────────────────────────────────
def test_不支持的后缀会报错(tmp_path):
    path = tmp_path / "a.rar"
    path.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        load_customer_data(str(path), IGL, LOGGER)
