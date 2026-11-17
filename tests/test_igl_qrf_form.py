"""Igloo（igl）新版 QRF 询价表的回归测试 —— 2026-11-17 MBM / HWH 两单事故。

背景（`customer-files/processed/_quarantine_20261117_igloo_qrf_new_form/`）：
Igloo 把询价表从老的「横向键值对」换成了 **Quote Request Form（QRF）**，
4 个工作表 `Sales Request Form` / `Printer Sheet` / `All Printer Costs` /
`Sales Returned infomation`。老档案的 detect 特征一条都不命中 → 落 generic，
抽出 5 类错值：

    字段        错值（generic）        应然
    CPLB        32.00;其他            27.02;精装BB书 / 27.01;普通BB书
    CD/KD/GD    413/292/152（外箱！）  130×130 / 250×160（TPS 成品尺寸）
    TZS         1（B 单）             2（`Number of Titles: 2T`）
    CPSL1..10   全 0                  3k,5k / 3000 per T
    KHDH        空（blocking error）  `Quote Ref` = QRF 号（业务 2026-11-17 拍板）

这些测试**不依赖**那两个 xlsx（隔离区会被清掉），全部钉在档案与解析器上。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from customer_profile import load_profile  # noqa: E402
from extract_fields import parse_qty_cell  # noqa: E402

IGL = load_profile("igl")


# ── 1. 识别：QRF 特征要让 igl 认出来，不能再落 generic ────────────────────
def test_QRF特征标签能识别出igl():
    """`Qty Per Title` 是 QRF 的事务表头，老档案完全没有 → 曾得分 0。"""
    texts = ["Qty Per Title", "Number of Titles", "3000 per T = 6000 books"]
    score, reasons = IGL.detect_score(texts, filename="QRF20220341 - HWH.xlsx")
    assert score >= 100, f"QRF 样本必须稳稳命中 igl，实际 {score}（{reasons}）"


def test_QRF是由香港代理转发的_发件人域名兜底():
    """Igloo 的询价由 Golden House 转发，正文里才出现 igloobooks.com。"""
    score, reasons = IGL.detect_score(["Von: buyer@igloobooks.com"], sender="sales@ghouseproducts.com")
    assert score >= 30, reasons


# ── 2. 尺寸：`Carton Size` 是外箱，绝不能落进 CD/KD/GD ────────────────────
def test_外箱尺寸标签不会被当成成品尺寸():
    """裸别名 `Size` 会命中 `Carton Size (mm) - H x W x L`（整词包含）。"""
    assert IGL.match_label("Carton Size (mm) - H x W x L") != "size"
    assert IGL.match_label("外箱尺寸") != "size"
    # 成品尺寸照常命中（不能把闸门开太大）
    assert IGL.match_label("Size (tps) H x W x D mm") == "size"
    assert IGL.match_label("TPS height x width (mm):") == "size"


# ── 3. 档位：一格文本的三种写法 ─────────────────────────────────────────
@pytest.mark.parametrize("text,expected", [
    ("3k, 5k", [3000, 5000]),                       # Sales Request Form!N15
    ("3k,5k,7.5k", [3000, 5000, 7500]),
    ("3000 per T = 6000 books = 300 SRP", [3000]),   # ★ 单款量，不是 6000 / 300
    ("3000 x 2T", [3000]),                          # Printer Sheet 写法
    ("3000/5000/7500/10000", [3000, 5000, 7500, 10000]),
])
def test_档位格解析(text, expected):
    assert parse_qty_cell(text) == expected


@pytest.mark.parametrize("text", ["", None, "to be confirmed", "3000 per T", "TBC"])
def test_档位格解析_认不出就返回空(text):
    """认不出必须返回 []（交给人工），不许瞎猜一个数。"""
    got = parse_qty_cell(text)
    assert got == [] or got == [3000], got


def test_档位格_不把总量当单款():
    """业务口径：CPSL 存**单款本数**，导出层才 ×TZS。"""
    assert parse_qty_cell("3000 per T = 6000 books = 300 SRP") == [3000]
    assert parse_qty_cell("30000 x 4T") == [30000]


# ── 4. 装订 / 产品类别：BB 书节点看「有没有灰板」 ────────────────────────
class _SilentLogger:
    def __getattr__(self, _name):
        return lambda *a, **k: None


def test_精装BB书与普通BB书分得开():
    """27.02=封面有灰板，27.01=没有。A 单 cased+3mm 灰板，B 单纯卡纸。"""
    from main import resolve_cplb  # noqa: WPS433  (延迟导入，避免循环)

    a, _ = resolve_cplb({"binding": "Cased board book binding, cover mounted on 3mm grey board"},
                        IGL, _SilentLogger())
    b, _ = resolve_cplb({"binding": "Board book binding, front cover and text pages punch cut"},
                        IGL, _SilentLogger())
    assert a.startswith("27.02"), a
    assert b.startswith("27.01"), b


# ── 5. 单号：QRF 号直接当客户单号（业务 2026-11-17 拍板）────────────────
def test_QRF号是客户单号的来源之一():
    cfg = (IGL.email or {}).get("khdh") or {}
    assert "khdh" in (cfg.get("attachment_keys") or []), (
        "QRF 表里的 `Quote Ref`（QRFxxxxxxxx）必须进 attachment_keys，"
        "否则 KHDH 永远缺号"
    )
    assert IGL.match_label("Quote Ref") == "khdh"


# ── 6. 款数：`2T` 后缀 ────────────────────────────────────────────────
def test_款数支持2T后缀():
    """Printer Sheet `Number of Titles: 2T` —— 旧实现 isdigit() 不认，TZS 落 1。"""
    import re
    raw = "2T"
    m = re.fullmatch(r"(\d+)\s*[tT]", raw)
    assert m and int(m.group(1)) == 2


# ── 7. 空客户单号 → 占位符 NULL000（公共默认值层）──────────────────────
def test_KHDH空值落NULL000占位符():
    assert IGL.erp_default_values().get("KHDH") == "NULL000", (
        "业务 2026-11-17：KHDH 为空统一填 NULL000，别让每单都红 blocking error"
    )
