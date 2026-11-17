"""auto_profile.py（新客户询价自动建档）的回归测试 —— 业务 2026-11-17 森西口径。

> 校准过的客户档案，未来遇到新的客户询价自动创建档案

要点：
* 自动建的档案只能是 `status: draft`（人工核过才能改 calibrated）
* 已有档案对得上发件人域名时**不许**再建一份（同一客户分叉成两份是最坏结果）
* `erp_khzl` 不猜（要去 ERP 客户资料页查）
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import auto_profile  # noqa: E402
from customer_profile import auto_detect, load_profile, profiles_dir  # noqa: E402


def test_id从发件人域名推():
    assert auto_profile.guess_id(Path("x.pdf"), sender="buyer@acmebooks.com") == "acmebooks"
    assert auto_profile.guess_id(Path("x.pdf"), sender="a@mail.acmebooks.com") == "acmebooks"
    assert auto_profile.guess_id(Path("x.pdf"), sender="", explicit="acme22") == "acme22"


def test_已有档案按域名兜住_不重复建档():
    """Fleurus = fes22（语料自动建档，未校准）。再建一份就分叉了。"""
    assert auto_profile.existing_profile_for("fleuruseditions.com") == "fes22"
    assert auto_profile.existing_profile_for("casterman.com") == "car12"


def test_已有档案按标签识别出的样本_不建档(tmp_path):
    """样本能被现有档案稳定识别（≥100 分）→ 不需要新档案。"""
    sample = tmp_path / "q.txt"
    sample.write_text("Size (tps) H x W x D mm: 210 x 148 x 10\n"
                      "Extent (pp + cover): 32pp\n"
                      "Binding instructions: saddle stitch\n", encoding="utf-8")
    texts = auto_profile._sample_texts(sample)
    cands = auto_detect(texts, filename=sample.name)
    assert cands and cands[0][1] >= 100, "样本本身要能被 igl 认出（测试前提）"
    assert auto_profile.build_profile(sample) is None


def test_新客户会落一份draft档案(tmp_path):
    sample = tmp_path / "quote.txt"
    sample.write_text(
        "Book Title: My New Book\n"
        "Trim size: 240 x 170 mm\n"
        "Page extent: 48pp\n"
        "Cover paper: 250gsm C1S\n"
        "Text paper: 128gsm gloss\n"
        "Binding: perfect bound\n"
        "Quantity: 3000, 5000\n",
        encoding="utf-8")
    pid = "zztestauto"
    target = profiles_dir() / f"{pid}.yaml"
    try:
        res = auto_profile.build_profile(sample, sender="buyer@zznewcustomer.com",
                                         name="ZZ New Customer", pid=pid)
        assert res and res["id"] == pid
        raw = yaml.safe_load(target.read_text(encoding="utf-8"))
        assert raw["status"] == "draft", "自动建档必须是 draft，不能自称 calibrated"
        assert raw["erp_khzl"] == [], "ERP 客户代码不许猜"
        assert "zznewcustomer.com" in raw["detect"]["sender_domains"]
        assert "自动建档" in raw["notes"]
    finally:
        for p in (target, profiles_dir() / f"_draft_{pid}.yaml"):
            if p.exists():
                p.unlink()


def test_重复跑不会把同一客户建两份(tmp_path):
    sample = tmp_path / "quote.txt"
    sample.write_text("Book Title: X\nTrim size: 200 x 200 mm\nPage extent: 24pp\n",
                      encoding="utf-8")
    pid = "zztestdup"
    target = profiles_dir() / f"{pid}.yaml"
    try:
        first = auto_profile.build_profile(sample, sender="buyer@zzdup.com", pid=pid)
        assert first is not None
        second = auto_profile.build_profile(sample, sender="buyer@zzdup.com", pid=pid)
        assert second is None, "第二次必须靠 sender_domains 认出已有档案并跳过"
    finally:
        for p in (target, profiles_dir() / f"_draft_{pid}.yaml"):
            if p.exists():
                p.unlink()


def test_扫generic产物时数量太小的不建档(tmp_path):
    """只有 1-2 个规范键的产物是「识别失败」，不是新客户 —— 不建档。"""
    assert auto_profile.MIN_VALUES_FOR_SIGNAL >= 3
