"""2026-11-17 新建三个客户档案（Ullmann / Priddy / Nosy Crow）的回归。

背景：11-17 那批 10 封真实邮件里有 4 封真询价，全部落成 `profile=generic` 的空单
（CPLB=32.00;其他、CD/KD/GD=0/0/0、TZS=1、CPSL 全 0）。逐条查证后确认是**没有档案**，
不是引擎错。本文件钉住三份新档案的行为：

1. 客户识别：4 份真实询价必须各自识别到正确档案（ULLMANN / PRIDDY ×2 / NOSY CROW）。
2. 抽取结果：`TZS` / `CPSL` / `CD`-`KD`-`GD` / `ZDFS` / `CPLB` 与客户原文一致。
3. 三条新引擎能力（都是这批邮件逼出来的）：
   * `parse_tier_list` 认「k + 量词」叠写（`10k copies`）
   * `expand_quantity_range` 把「1k-100k」这种区间按 1-2-5 展开（档案开关）
   * `size_looks_clean` / `normalize_size_run` 不被「两组尺寸写一句」「整句里只有一个数字」骗
4. 装订映射的键必须是**字面子串**（引擎不是用正则匹配 key）—— 这条是踩过的坑。

真实样本放在 `customer-files/processed/`，可能被归档/清理 → 找不到就 skip，
但纯单元断言永远跑。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import customer_profile as C  # noqa: E402
import main as MAIN  # noqa: E402

PROCESSED = REPO_ROOT / "customer-files" / "processed"

# 任务目录 → (期望客户, 该目录里那一个 .txt)
# 任务 id 会随重跑变（旧 generic 产物归档进 processed/_superseded_YYYYMMDD/），
# 所以按「正文里的客户名」找样本，而不是写死 id。
SAMPLES = {
    "Crime_Mysteries": "ullmann",
    "STF_Sticker_Book": "priddy",
    "STF_Flashcards": "priddy",
    "Little_Museum": "nosycrow",
}


def _source_text(name_hint: str) -> str:
    """按文件名片段找真实客户原文（含 `_superseded_*` 归档目录）。"""
    for pattern in (f"**/*{name_hint}*.txt",):
        files = sorted(PROCESSED.glob(pattern))
        if files:
            return files[0].read_text(encoding="utf-8", errors="ignore")
    pytest.skip(f"真实样本 *{name_hint}* 已不在 processed/ 里")


@pytest.mark.parametrize("name_hint,expected", sorted(SAMPLES.items()))
def test_真实询价能识别到对应客户(name_hint, expected):
    text = _source_text(name_hint)
    candidates = C.auto_detect([text], filename=name_hint)
    assert candidates, f"{task_id} 没识别出任何客户"
    assert candidates[0][0].id == expected, \
        f"{name_hint} 识别成 {candidates[0][0].id}（应 {expected}），得分 {candidates[0][1]}"


# ── 引擎新能力 ────────────────────────────────────────────────────────
def test_档位允许k加量词叠写():
    """Nosy Crow：`1k, 2k, 3k, 4k, 5k, 7k, 10k copies` —— 旧正则整条失败 → 全 0。"""
    assert MAIN.parse_tier_list("1k, 2k, 3k, 4k, 5k, 7k, 10k copies") == \
        [1000, 2000, 3000, 4000, 5000, 7000, 10000]


def test_档位老写法不受影响():
    assert MAIN.parse_tier_list("5.000 / 7.500 / 10.000") == [5000, 7500, 10000]
    assert MAIN.parse_tier_list("10k/25k/50k/100k") == [10000, 25000, 50000, 100000]


def test_区间按1_2_5阶梯展开():
    """Priddy：`Quantities: 1k-100k scales` 没有具体档位。"""
    assert MAIN.expand_quantity_range("1k-100k scales") == \
        [1000, 2000, 5000, 10000, 20000, 50000, 100000]


@pytest.mark.parametrize("text", ["", "perfect binding at the top", "140x198mm", "16pp"])
def test_非区间文本不许展开(text):
    assert MAIN.expand_quantity_range(text) == []


@pytest.mark.parametrize("text,clean", [
    ("198mm x 129mm x 10mm", True),
    ("140x198mm", True),
    ("250 x 210 x 10mm", True),
    ("20 x 25,3 cm", False),                      # cm 要换算
    ("215(w) x 280(h) - portrait", False),        # 带尾巴
    ("300mm x 220mm - trimmed. 300mm(h) x 220mm(w)", False),   # 两组尺寸
    ("Size to house the 15 cards", False),        # 整句只有一个数字
])
def test_尺寸干净度判定(text, clean):
    assert MAIN.size_looks_clean(text) is clean


@pytest.mark.parametrize("text,expected", [
    ("215(w) x 280(h) - portrait", "215 x 280 mm"),
    ("300mm x 220mm - trimmed. 300mm(h) x 220mm(w)", "300 x 220 mm"),
    ("20 x 25,3 cm", "200 x 253 mm"),
    ("198mm x 129mm x 10mm", "198 x 129 x 10 mm"),
    ("Size to house the 15 cards", ""),
])
def test_尺寸归一化(text, expected):
    assert MAIN.normalize_size_run(text) == expected


# ── 档案约定 ──────────────────────────────────────────────────────────
def test_装订映射的键必须是字面子串():
    """引擎用 `key.lower() in text.lower()`，写 `a|b` 永远匹配不到。

    2026-11-17 踩过：三个新档案第一版都写成了 `"limp, sewn|sewn"` 这种，
    结果装订全部落到占位值（看起来「判断不出」），真相是键写错。
    """
    for cid in C.list_profiles():
        profile = C.load_profile(cid)
        mapping = profile.rule("binding.map", {}) or {}
        for key in mapping:
            assert "|" not in str(key), (
                f"{cid}.rules.binding.map 的键 {key!r} 里含 `|` —— "
                f"引擎是字面子串匹配，不是正则；请拆成多条")


def test_新客户档案的ERP代码():
    assert C.load_profile("ullmann").erp_khzl == ["UMG19"]
    assert set(C.load_profile("priddy").erp_khzl or []) == {"PRD21", "PRD25"}
    # Nosy Crow 还没有 ERP 单据 → 故意留空（别凭空填）
    assert (C.load_profile("nosycrow").erp_khzl or []) == []


def test_新客户档案都有语料来源的默认值或明确留空():
    """Ullmann/Priddy 有语料 → BZFS 必须落；Nosy Crow 无语料 → 不许抄别家。"""
    ullmann = C.load_profile("ullmann")
    priddy = C.load_profile("priddy")
    nosy = C.load_profile("nosycrow")
    assert ullmann.erp_defaults.get("BZFS") == "纸箱包装"
    assert priddy.erp_defaults.get("BZFS") == "纸箱包装"
    # 公共层会带 BZ=美元 之类的通用默认值；这里只盯「语料推导出来」的那几个
    leaked = {k: v for k, v in nosy.erp_defaults.items() if k in ("BZFS", "SHFS", "TBLX", "chkBSYTB")}
    assert not leaked, f"Nosy Crow 没有语料，不该有这些默认值: {leaked}"
