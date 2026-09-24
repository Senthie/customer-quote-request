"""客户档案层测试：加载 / 深合并 / 别名匹配 / 自动识别 / 校验。"""
import pytest

from customer_profile import (
    CANONICAL_KEYS,
    DEFAULTS_ID,
    ProfileError,
    _read_yaml,
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


# ── 档案重复键（2026-09-23 加：YAML 默认静默保留最后一个，实测踩过）──────
def test_重复键会被拦成硬错误(tmp_path):
    """同一层级写两遍同名键，PyYAML 会静默保留最后一个 —— 必须报错而不是放过。"""
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "id: x\nname: X\nrules:\n  unit:\n    default: 本\n  unit:\n    default: 套\n",
        encoding="utf-8",
    )
    with pytest.raises(ProfileError) as exc:
        _read_yaml(bad)
    assert "重复键" in str(exc.value)
    assert "unit" in str(exc.value)
    assert "第 6 行" in str(exc.value)


def test_列表里的同名键不算重复(tmp_path):
    """sections 的每个列表项都有自己的 entries —— 不能误报。"""
    ok = tmp_path / "ok.yaml"
    ok.write_text(
        "id: x\nname: X\nsections:\n"
        "  - names: [a]\n    entries:\n      - { section_value: notes }\n"
        "  - names: [b]\n    entries:\n      - { section_value: contact }\n",
        encoding="utf-8",
    )
    assert _read_yaml(ok)["sections"][0]["names"] == ["a"]


def test_纯标签与附加信息分开():
    """`ignored_labels:` = 栏目标题（不含值）；`extras:` = 真信息（要写进 CPMS）。

    多行块只有**每一行都是标题**才算纯标签 —— 否则「标签 + 同行值」会被误杀。
    """
    ngv = load_profile("ngv")
    assert ngv.is_ignored_label("TPS\nFormat") is True
    assert ngv.is_ignored_label("paper\nPapier\n230 gsm art card") is False, "含值行不算纯标签"
    # 没声明 ignored_labels 的档案不受影响（igl 一个都没声明）
    igl = load_profile("igl")
    assert igl.is_ignored_label("Carton Weight") is False
    assert igl.is_known_extra("Carton Weight") is True


# ── 客户偏好表（业务 2026-09-24 提出：「每个客户设一份喜爱表，方便快速填写习惯」）
def test_偏好表_每个已声明档案内部一致():
    """偏好表是给人看的、erp_fixed 才是落库值 —— 两者必须对得上，否则就是漂移。"""
    for cid in list_profiles():
        profile = load_profile(cid)
        if not profile.preferences:
            continue
        rows = profile.preference_table()
        assert rows, f"{cid}: preferences 段解析不出条目"
        for r in rows:
            assert r["ok"], (
                f"{cid}.{r['field']}: 偏好表写 {r['value']!r}，"
                f"erp_fixed 实际 {r['effective']!r} —— 想留空就必须在 erp_fixed 里写 ''"
            )
            assert r["reason"], f"{cid}.{r['field']} 缺少依据（偏好表必须能一眼看出为什么）"


def test_偏好表_拦住文档与落库值不一致(tmp_path, monkeypatch):
    """故意把偏好表改成「留空」而 erp_fixed 还填着值 → 校验必须报错。

    这个洞很隐蔽：偏好表写了「留空」，看着好像是空；实际产物里还留着模板默认值。
    """
    import customer_profile as cp

    profiles = tmp_path / "customers"
    profiles.mkdir()
    (profiles / "_defaults.yaml").write_text(
        "id: generic\nname: 兜底\npriority: 0\nstatus: draft\n", encoding="utf-8")
    (profiles / "x.yaml").write_text(
        "id: x\nname: X\nstatus: calibrated\n"
        "detect:\n  label_signatures: [hello]\n"
        "erp_fixed:\n  PXH: 1\n"
        "preferences:\n"
        "  - { field: PXH, value: \"\", reason: \"想留空但没清 erp_fixed\" }\n",
        encoding="utf-8")
    monkeypatch.setattr(cp, "profiles_dir", lambda: profiles)

    problems = cp.load_profile("x").validate()
    assert any("PXH" in p and "不一致" in p for p in problems), problems


def test_偏好表_NGV_显式留空的字段确实为空():
    """业务 2026-09-24 第二批口径：PXH / YSLX / SFYPJ / Remark1 / Remark3 / FSCMC / ZBLX
    一律留空（推不出不猜），BZ / CPDL 我们填。"""
    ngv = load_profile("ngv")
    prefs = {r["field"]: r["value"] for r in ngv.preference_table()}
    for field in ("PXH", "YSLX", "SFYPJ", "Remark1", "Remark3", "FSCMC", "ZBLX", "JHRQ", "WCRQ"):
        assert prefs[field] == "", f"{field} 应为显式留空"
    assert prefs["BZ"] == "美元" and prefs["CPDL"] == "书版"
    assert ngv.erp_fixed_values()["PXH"] == "", "PXH 必须在 erp_fixed 里显式清空"


def test_偏好表_命令行可打印():
    """`--prefs` 是业务侧「快速填写习惯」的入口，别让它悄悄失效。"""
    import customer_profile as cp

    text = cp.load_profile("ngv").describe_preferences()
    assert "客户偏好表" in text and "PXH" in text and "排序号" in text
