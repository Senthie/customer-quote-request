"""客户档案层测试：加载 / 深合并 / 别名匹配 / 自动识别 / 校验。"""
import pytest

from customer_profile import (
    CANONICAL_KEYS,
    DEFAULTS_ID,
    auto_detect,
    list_profiles,
    load_profile,
    norm_label,
    resolve_profile,
)


def test_所有档案都能加载且校验通过():
    for cid in list_profiles() + [DEFAULTS_ID]:
        profile = load_profile(cid)
        assert profile.validate() == [], f"{cid} 校验未通过"


def test_列表里包含_igl_与兜底档案():
    ids = list_profiles()
    assert "igl" in ids
    assert DEFAULTS_ID not in ids          # 兜底档案不以客户身份出现在列表里


def test_深合并_客户档案继承公共默认值():
    igl = load_profile("igl")
    # BZ / YYS 只在 _defaults.yaml 里定义，igl.yaml 没有重复声明
    assert igl.erp_fixed_values()["BZ"] == "美元"
    assert igl.erp_fixed_values()["YYS"] == 1
    # igl 自己声明的值也在
    assert igl.erp_fixed_values()["CPDL"] == "书版"


def test_归一化_压缩空白与大小写():
    assert norm_label("  Series \u00a0 Name  ") == "series name"
    assert norm_label(None) == ""
    assert norm_label("Pub\tMonth") == "pub month"


def test_别名匹配_最长优先_Group_不抢_Group_Series_Number():
    igl = load_profile("igl")
    assert igl.match_label("Group") == "group"
    assert igl.match_label("Group/Series Number") == "group_series_number"


def test_别名匹配_整词边界_不误吞():
    igl = load_profile("igl")
    # "Works References For all Titles" 归一化后含换行 → 应命中 works_references
    assert igl.match_label("Works References\nFor all Titles") == "works_references"
    # 值不该被当标签
    assert igl.match_label("Paperback") is None
    assert igl.match_label("Autumn") is None


def test_别名匹配_超长单元格不当标签():
    igl = load_profile("igl")
    long_text = "Series Name " + "x" * 80
    assert igl.match_label(long_text) is None


def test_别名匹配_支持复数():
    igl = load_profile("igl")
    assert igl.match_label("Series Names") == "series_name"


def test_规范键白名单外会报错(tmp_path, monkeypatch):
    import customer_profile
    bad = tmp_path / "customers"
    bad.mkdir()
    (bad / "_defaults.yaml").write_text(
        (customer_profile.profiles_dir() / "_defaults.yaml").read_text(encoding="utf-8"),
        encoding="utf-8")
    (bad / "broken.yaml").write_text(
        "id: broken\nname: x\nstatus: draft\ndetect:\n  label_signatures: [x]\n"
        "fields:\n  不存在的键: [A]\n", encoding="utf-8")
    monkeypatch.setattr(customer_profile, "profiles_dir", lambda: bad)
    problems = customer_profile.load_profile("broken").validate()
    assert any("未知规范键" in p for p in problems)


def test_自动识别_真实特征标签命中_igl():
    texts = ["New Product Development Cost Request Form",
             "Works References For all Titles", "23992 / 23993",
             "Safety Checks Req? (Y/N)", "Extent (pp + cover)", "120pp + cover"]
    results = auto_detect(texts, filename="PUZZLE COLOUR DISNEY.xlsx")
    assert results, "应至少命中一个客户的档案"
    assert results[0][0].id == "igl"
    assert results[0][1] >= 300          # 3 个特征标签 × 100


def test_自动识别_无命中时走兜底():
    profile, source = resolve_profile("auto", texts=["完全无关的一段话"])
    assert profile.id == DEFAULTS_ID
    assert source == "兜底"


def test_显式指定客户():
    profile, source = resolve_profile("igl", texts=[])
    assert profile.id == "igl"
    assert source == "显式指定"


def test_不存在的客户档案会报错():
    from customer_profile import ProfileError
    with pytest.raises(ProfileError):
        load_profile("不存在的客户")


def test_规范键集合非空且含关键键():
    for key in ("format", "series_name", "size", "quantity_tiers", "pub_month"):
        assert key in CANONICAL_KEYS


def test_rule_点号路径读取():
    igl = load_profile("igl")
    assert igl.rule("size.unit") == "mm"
    assert igl.rule("size.order") == ["CD", "KD", "GD"]
    assert igl.rule("不存在的路径", "兜底") == "兜底"


def test_已知附加信息登记():
    """extras: 里的「非 ERP 字段」要能被认出来（它们必须写进 CPMS）。"""
    igl = load_profile("igl")
    assert igl.extras, "igl 档案应登记附加信息"
    assert igl.is_known_extra("Quotation Number")
    assert igl.is_known_extra("Carton Weight")
    assert igl.is_known_extra("Add spot UV")
    # 普通字段/值不该被误判成附加信息
    assert not igl.is_known_extra("Series Name")
    assert not igl.is_known_extra("Autumn")
