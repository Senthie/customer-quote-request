"""derive_customer_defaults：从语料反推客户常用值的判定逻辑。

语料本身（/home/claw/train/custom_cpms_requie）是 152MB 的外部数据，不在仓库里，
所以这里只用**合成记录**验证判定逻辑：阈值怎么切、布尔怎么还原、档案怎么归组。
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import derive_customer_defaults as dcd  # noqa: E402


def rec(year: int, **kw) -> dict:
    """造一条最小真实形状的记录：KHDDH 承载年份（CM + YY + …）。"""
    base = {"KHDDH": f"CM{str(year)[2:]}0101-01", "KHZL": "TEST10"}
    base.update(kw)
    return base


# ── ① 类型还原：布尔不能写成字符串 ──────────────────────────────────────
@pytest.mark.parametrize("field", sorted(dcd.BOOL_FIELDS))
def test_勾选框还原成真布尔(field):
    """语料里是 JSON false，统计时被 str() 成 'False' —— 写回档案必须还原，
    否则 ERP 收到字符串 'False' 会当非法值静默丢弃。"""
    assert dcd.coerce(field, "False") is False
    assert dcd.coerce(field, "True") is True


def test_普通字段原样保留():
    assert dcd.coerce("BZFS", "纸箱包装") == "纸箱包装"


# ── ② 阈值判定 ────────────────────────────────────────────────────────
def test_双率高才采纳():
    recs = [rec(2025, BZFS="纸箱包装", TBLX="不打卡板") for _ in range(37)]
    recs += [rec(2025, BZFS="纸箱包装", TBLX="欧标松木卡板") for _ in range(20)]
    res = dcd.analyse(recs, since=2023, min_rate=0.9, min_cov=0.9)
    assert res["rows"]["BZFS"]["tier"] == "采纳", "100% 一致 + 100% 覆盖"
    assert res["rows"]["TBLX"]["tier"] == "需业务定(不稳定)", "覆盖满但只 65% 一致"


def test_覆盖不足只算候选():
    """出现即一致、但多数单据没填 —— 不能当默认值（填了就是凭空造值）。"""
    recs = [rec(2025, SHFS="FOB 盐田") for _ in range(10)]
    recs += [rec(2025) for _ in range(90)]
    res = dcd.analyse(recs, since=2023, min_rate=0.9, min_cov=0.9)
    row = res["rows"]["SHFS"]
    assert row["tier"] == "候选(覆盖低)"
    assert row["rate"] == 1.0 and row["coverage"] == pytest.approx(0.1)


def test_全空字段标从不填():
    res = dcd.analyse([rec(2025) for _ in range(40)], since=2023, min_rate=0.9, min_cov=0.9)
    assert res["rows"]["SHDQ"]["tier"] == "从不填"
    assert res["rows"]["SHDQ"]["mode"] is None


def test_since过滤掉枚举规范化之前的老数据():
    """2022 及更早的下拉值已被 ERP 规范化（如「不用打卡板」），不能参与众数统计。"""
    recs = [rec(2022, TBLX="不用打卡板") for _ in range(50)]
    recs += [rec(2024, TBLX="不打卡板") for _ in range(50)]
    res = dcd.analyse(recs, since=2023, min_rate=0.9, min_cov=0.9)
    assert res["total"] == 50
    assert res["rows"]["TBLX"]["mode"] == "不打卡板"


# ── ③ 档案归组：ERP 客户代码 ↔ 档案 id ────────────────────────────────
def test_档案声明的erp_khzl能解析():
    profiles = dcd.build_customer_map()
    assert dcd.resolve("NGV10", profiles) == "ngv"
    assert dcd.resolve("NGVS16", profiles) == "ngv"
    assert dcd.resolve("IBL10", profiles) == "igl"


def test_没有档案的客户解析为空():
    assert dcd.resolve("ZZZ99", dcd.build_customer_map()) is None


# ── ④ 草稿文件 ────────────────────────────────────────────────────────
def test_草稿里布尔是真的_false(tmp_path, monkeypatch):
    monkeypatch.setattr(dcd, "CUSTOMERS", tmp_path)
    recs = [rec(2025, BZFS="纸箱包装", chkBSYTB=False, chkZDBJF=False, chkBSYJZX=False)
            for _ in range(40)]
    res = dcd.analyse(recs, since=2023, min_rate=0.9, min_cov=0.9)
    out = dcd.write_draft("TEST10", "test", res, 0.9, 0.9, Path("/tmp/corpus"), 2023)
    text = out.read_text(encoding="utf-8")
    assert "chkBSYTB: false" in text
    assert '"False"' not in text, "字符串 False 会被 ERP 静默丢弃"
    assert "BZFS" in text


def test_只看不写的字段不进草稿(tmp_path, monkeypatch):
    """SFYPJ 由 accessories 规则推、CPLB 逐单变 —— 即使 100% 一致也不写进默认值。"""
    monkeypatch.setattr(dcd, "CUSTOMERS", tmp_path)
    recs = [rec(2025, SFYPJ="否", CPLB="无线胶装书") for _ in range(40)]
    res = dcd.analyse(recs, since=2023, min_rate=0.9, min_cov=0.9)
    out = dcd.write_draft("TEST10", "test", res, 0.9, 0.9, Path("/tmp/corpus"), 2023)
    body = out.read_text(encoding="utf-8")
    defaults_block = body.split("erp_defaults:")[1].split("# ──")[0]
    assert "SFYPJ" not in defaults_block and "CPLB" not in defaults_block


# ── ⑤ 批量建档的产物必须守规矩 ────────────────────────────────────────
def test_批量建档的档案都声明了erp_khzl且是draft():
    """`--bootstrap` 生成的档案：没被真实询价文件验证过，只能是 draft；
    且必须声明 erp_khzl，否则下次统计无法按代码归组。"""
    from customer_profile import list_profiles, load_profile

    drafts = [cid for cid in list_profiles() if load_profile(cid).status == "draft"]
    assert drafts, "至少应有一批语料铺底的 draft 档案"
    for cid in drafts:
        profile = load_profile(cid)
        assert profile.validate() == [], f"{cid} 校验未通过"
        assert profile.erp_khzl, f"{cid} 缺 erp_khzl 声明（统计归组要用）"
