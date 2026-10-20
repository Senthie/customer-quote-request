"""查表工具（1.0.42 内联）：`main.py` 里的三张表 + 任务号。

这四件事以前各是一个独立脚本（`select_page_thickness.py` / `product_categories.py` /
`string2timestamp.py` / `uuid_utils.py`），main.py 用 `subprocess + uv run` 去调。
1.0.42 把它们内联成函数，**行为必须逐行等价** —— 本文件用真实表逐行钉住：

- 纸张厚度表 72 行：每行精确命中都必须返回表里那个值
- 类别表：唯一有值的那一行（`paperback`）必须命中；大小写 / 空格宽容；其余未命中
- 日期：客户文件里出现过的写法（含 `May-27`、`12.06.2026` 这种解不出的）
- 任务号：32 位 hex

外加两条「别再长回去」的护栏：`main.py` 不许再 import subprocess、那四个脚本不许复活。
"""
import re
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import main as M  # noqa: E402

THICKNESS_XLSX = REPO_ROOT / "assets" / "page_thickness.xlsx"
CATEGORIES_XLSX = REPO_ROOT / "assets" / "product_categories.xlsx"


class QuietLogger:
    """只收 WARN，不打印（测试里不需要噪音）。"""

    def __init__(self):
        self.infos = []
        self.warns = []

    def info(self, msg, *args, **kwargs):
        self.infos.append(str(msg))

    def warn(self, msg, *args, **kwargs):
        self.warns.append(str(msg))

    def error(self, msg, *args, **kwargs):
        self.warns.append(str(msg))


@pytest.fixture(autouse=True)
def _clear_table_cache():
    """每个用例都从干净缓存开始（否则「表缺失」用例会被上一个用例污染）。"""
    M._table_cache.clear()
    yield
    M._table_cache.clear()


# ── 纸张厚度 ───────────────────────────────────────────────────────────


def _thickness_rows():
    df = pd.read_excel(THICKNESS_XLSX)
    return [(str(r["page_style"]).strip(), str(r["faw/g"]).strip(), float(r["thickness"]))
            for _, r in df.iterrows()]


@pytest.mark.parametrize("style,faw,expected", _thickness_rows(),
                        ids=lambda v: str(v))
def test_thickness_every_row_in_table(style, faw, expected):
    """全表 72 行：精确命中的值必须与表里完全一致（旧脚本也是这个值）。"""
    assert M.get_page_thickness(style, faw, QuietLogger()) == expected


def test_thickness_exact_match_wins_over_fuzzy():
    """`80` 精确命中 0.07，不应该被 `180` 之类的模糊匹配抢走。"""
    table = {(s, w): t for s, w, t in _thickness_rows()}
    assert M.get_page_thickness("书纸", "80", QuietLogger()) == table[("书纸", "80")]


def test_thickness_nearest_weight_when_missing():
    """90g 表里没有 → 同纸型里按克重就近（书纸最近的是 100g）。"""
    table = {(s, w): t for s, w, t in _thickness_rows()}
    logger = QuietLogger()
    assert M.get_page_thickness("书纸", "90", logger) == table[("书纸", "100")]
    assert any("最近克重" in w for w in logger.warns), "就近取值必须打 WARN 说明"


def test_thickness_tie_takes_the_thicker_one():
    """110g 与 100/120 等距 → 取较大克重（宁厚勿薄，书脊算窄会装不下）。"""
    table = {(s, w): t for s, w, t in _thickness_rows()}
    assert M.get_page_thickness("书纸", "110", QuietLogger()) == table[("书纸", "120")]


def test_thickness_with_unit_suffix_still_hits():
    """客户写 `80g` / `100 gsm` 时靠「包含」兜住（旧脚本第二级）。"""
    table = {(s, w): t for s, w, t in _thickness_rows()}
    assert M.get_page_thickness("书纸", "80g", QuietLogger()) == table[("书纸", "80")]


def test_thickness_unknown_paper_returns_zero_not_raise():
    logger = QuietLogger()
    assert M.get_page_thickness("不存在的纸", "80", QuietLogger()) == 0.0
    assert M.get_page_thickness("书纸", "", logger) == 0.0
    assert logger.warns, "查不到必须留下 WARN 痕迹"


def test_thickness_ambiguous_match_returns_zero():
    """模糊命中多行时**不猜**：旧脚本 `float()` 吃掉多行 → 0.0，这里保持一致。"""
    logger = QuietLogger()
    assert M.get_page_thickness("书", "80", logger) == 0.0
    assert any("命中" in w for w in logger.warns)


def test_thickness_missing_table_degrades_gracefully(monkeypatch, tmp_path):
    """表被删掉也不能炸整单（旧脚本 exit 1 → run_script 返回 '' → 0.0）。"""
    monkeypatch.setattr(M, "PAGE_THICKNESS_PATH", tmp_path / "nope.xlsx")
    assert M.get_page_thickness("书纸", "80", QuietLogger()) == 0.0


# ── 产品类别表 ─────────────────────────────────────────────────────────


def test_categories_paperback_hits_the_one_filled_row():
    """这张表 88 行里只有 1 行填了 `customer_product_categories`（paperback）。"""
    df = pd.read_excel(CATEGORIES_XLSX)
    filled = df[df["customer_product_categories"].notna()]
    assert len(filled) == 1, "类别表的口径变了 —— 兜底覆盖面也跟着变，先确认再改测试"
    row = filled.iloc[0]

    cplb, zlbm = M.parse_product_categories("paperback", QuietLogger())
    assert cplb == str(row["CPLB"]).strip()
    assert zlbm == M._cell(row["ZLBM"])


@pytest.mark.parametrize("written", ["paperback", "Paperback", "PAPERBACK", " paperback "])
def test_categories_is_case_and_space_tolerant(written):
    assert M.parse_product_categories(written, QuietLogger()) == \
        M.parse_product_categories("paperback", QuietLogger())


@pytest.mark.parametrize("miss", ["paper backs", "hardcover", "", "书纸"])
def test_categories_miss_returns_empty(miss):
    assert M.parse_product_categories(miss, QuietLogger()) == ("", "")


def test_categories_missing_table_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(M, "PRODUCT_CATEGORIES_PATH", tmp_path / "nope.xlsx")
    assert M.parse_product_categories("paperback", QuietLogger()) == ("", "")


# ── 日期 ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw,expected", [
    ("2027-05-01", "2027-05-01"),
    ("2027/05/01", "2027-05-01"),
    ("2027.05.01", "2027-05-01"),
    ("May-27", "2027-05-01"),
    ("may-27", "2027-05-01"),
    ("May-2027", "2027-05-01"),
    ("2027-05", "2027-05-01"),          # 只到月 → 取 1 日
    ("2027.05", "2027-05-01"),
    ("2027-05-01 08:30:00", "2027-05-01"),
    (" 2026-07-27 ", "2026-07-27"),
])
def test_date_formats_parse(raw, expected):
    parsed = M._parse_date_value(raw)
    assert parsed is not None, f"{raw!r} 应该能解析"
    assert parsed.strftime("%Y-%m-%d") == expected


@pytest.mark.parametrize("raw", ["12.06.2026", "Juni 2026", "garbage", ""])
def test_date_unparsable_returns_none(raw):
    """这些旧脚本也解不出 → 交给 parse_date_to_datestr 的正则回退。"""
    assert M._parse_date_value(raw) is None


def test_parse_date_to_datestr_does_not_shift_into_previous_day():
    """旧实现走 `ms → UTC` 往返，本地时区会把 00:30 挪到前一天；现在只取日期部分。"""
    assert M.parse_date_to_datestr("2027-05-01 00:30:00", QuietLogger()) == "2027-05-01"


def test_parse_date_to_datestr_gives_up_cleanly():
    assert M.parse_date_to_datestr("garbage", QuietLogger()) == ""


# ── 任务号 ─────────────────────────────────────────────────────────────


def test_task_id_is_32_hex():
    task_id = M.generate_task_id(QuietLogger())
    assert re.fullmatch(r"[0-9a-f]{32}", task_id), task_id


def test_task_id_is_unique_per_call():
    assert M.generate_task_id(QuietLogger()) != M.generate_task_id(QuietLogger())


# ── 护栏：不许再 spawn 子进程 / 脚本不许复活 ────────────────────────────


DELETED_SCRIPTS = ["select_page_thickness.py", "product_categories.py",
                   "string2timestamp.py", "uuid_utils.py"]


@pytest.mark.parametrize("name", DELETED_SCRIPTS)
def test_old_lookup_script_stays_deleted(name):
    """内联了就别把脚本留一份 —— 两份实现迟早漂。"""
    assert not (REPO_ROOT / "scripts" / name).exists(), \
        f"scripts/{name} 已内联进 main.py，不要再单独留脚本"


def test_main_does_not_spawn_subprocesses():
    """查表不该再起 `uv run` 子进程（那还要求 PATH 里有 uv）。"""
    src = (REPO_ROOT / "scripts" / "main.py").read_text(encoding="utf-8")
    code = "\n".join(line for line in src.splitlines()
                     if not line.lstrip().startswith("#"))
    assert "subprocess" not in code, "main.py 不该再 import subprocess"
    assert "run_script" not in code, "run_script 已随四个查表脚本一起下线"
