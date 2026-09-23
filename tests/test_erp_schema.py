#!/usr/bin/env python3
"""ERP 表单权威字典的测试。

字典来源：客户真实 ERP 表单（assets/erp_form_schema.yaml，2026-09-23 转录）。
这些用例把「ERP 只认固定枚举」这件事钉死，防止以后有人填了 ERP 不认的值
（历史教训：0e39e2e2 那张单的 ZDFS 填了「无线胶装」）。
"""
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from erp_schema import (  # noqa: E402
    field_spec, legal_values, load_schema, normalize_result, normalize_value,
    validate_result,
)

TEMPLATE = REPO_ROOT / "assets" / "add_customer_quote_request.json"
HISTORICAL = (REPO_ROOT / "customer-files" / "processed" /
              "0e39e2e27dc34eb381d2e4360d776da8" /
              "ERP_0e39e2e27dc34eb381d2e4360d776da8_20260525_171144.json")


# ── 字典本身 ─────────────────────────────────────────────────────────────
def test_字典覆盖模板全部字段():
    """模板 59 个键，字典必须都能查到定义 —— 否则校验会漏字段。"""
    template = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    missing = [k for k in template if not field_spec(k)]
    assert missing == [], f"字典缺这些字段: {missing}"


def test_ERP自动带出的字段就是表单上找不到的那六个():
    """业务 2026-09-23：BDBH/YWY/KHZL/GDY/CPM X/Remark 由 ERP 自动带出，默认留空。

    交叉验证：表单 HTML 里确实找不到这 6 个控件。
    """
    auto = {f for f, spec in load_schema()["fields"].items() if spec.get("auto_filled")}
    assert auto == {"BDBH", "YWY", "KHZL", "GDY", "CPMX", "Remark"}


def test_书脊方向合法值来自表单():
    assert legal_values("Drop_SJFX") == ["短书脊", "长书脊", "四边裁"]


def test_装订方式合法值来自表单():
    values = legal_values("ZDFS")
    assert "胶装/PAD书" in values
    assert "骑马钉" in values
    assert "慢线BB书" in values
    # 表单原值「快线BB书   」带尾随空格 → 必须已 strip
    assert "快线BB书" in values
    assert all(v == v.strip() for v in values)


def test_单位枚举含本():
    assert "本" in legal_values("DWMC")


def test_产品属性含年龄段选项():
    """CPSX 是「目标年龄段 + 安全检查」的落库位置（业务确认 2026-09-23 要落库）。

    推导规则在 main.py derive_cpsx()；这里钉住合法枚举与「中文 label」口径。
    """
    values = legal_values("CPSX")
    assert "三岁以上" in values
    assert "三岁以下" in values
    assert any("第三方测试" in v for v in values)
    # 归一化输出的必须是中文 label（不是 CPSX001 这种代码）—— 决策 2026-09-23
    assert normalize_value("CPSX", "CPSX001")[0] == "三岁以上"
    assert normalize_value("CPSX", "CPSX005")[0] == "3岁以上（要寄第三方测试）"
    assert validate_result({"CPSX": "三岁以上"}) == []


# ── 业务决策钉死（2026-09-23 森西确认）────────────────────────────────────
def test_决策_所有下拉统一存中文_label():
    """③ 统一使用中文：字典里不许再出现 store: code（否则等于埋回代码值口径）。"""
    offenders = [f for f, spec in load_schema()["fields"].items()
                 if spec.get("store") not in (None, "label")]
    assert offenders == [], f"这些字段仍声明非 label 存储：{offenders}"


def test_决策_客户单号用_KHDH_且不得出现_KHCPMC():
    """① 保留 KHDH：ERP 侧控件名是 txtKHCPMC，但输出侧不跟着错。"""
    template = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    assert "KHDH" in template, "模板必须有 KHDH"
    assert "KHCPMC" not in template, "模板不得出现 KHCPMC（ERP 侧命名错误，会造成误导）"
    assert any(k.startswith("KHCPMC") for k in load_schema()["fields"]) is False
    # 表单控件名只作为溯源信息保留
    assert field_spec("KHDH")["form_name"] == "txtKHCPMC"
    assert field_spec("KHDH")["label"] == "客户单号"


# ── 归一化 ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("field,raw,expected", [
    # 历史单据错值 → 合法值
    ("ZDFS", "无线胶装", "胶装/PAD书"),
    ("ZDFS", "Perfect Bound", "胶装/PAD书"),
    ("ZDFS", "Paperback", "胶装/PAD书"),
    ("ZDFS", "骑马订", "骑马钉"),          # 「订」→「钉」
    ("ZDFS", "Wire-O", "线圈书"),
    ("ZDFS", "快线BB书   ", "快线BB书"),    # 尾随空格
    ("ZDFS", "胶装/PAD书", "胶装/PAD书"),   # 已合法
    ("Drop_SJFX", "1", "长书脊"),           # 表单存的是数字代码
    ("Drop_SJFX", "0", "短书脊"),
    ("Drop_SJFX", "四边裁", "四边裁"),
    ("Drop_SJFX", "长书脊", "长书脊"),
    ("BZ", "02", "美元"),                   # code → label
    ("DWMC", "册", "本"),
    ("CPSX", "CPSX001", "三岁以上"),
    ("SFYPJ", "1", "是"),
    ("P", "", ""),                          # 空值不动
])
def test_归一化把客户写法对齐到_ERP_合法值(field, raw, expected):
    value, status = normalize_value(field, raw)
    assert value == expected, f"{field}: {raw!r} → {value!r}（期望 {expected!r}）"
    assert status in ("ok", "normalized", "untouched")


def test_认不出的取值不改写但会被校验抓到():
    value, status = normalize_value("ZDFS", "螺旋装订")
    assert value == "螺旋装订"
    assert status == "unknown"
    issues = validate_result({"ZDFS": "螺旋装订"})
    assert [i["level"] for i in issues] == ["error"]


def test_空值不算错():
    """空值 = 未填，不是非法值；只有标记 required 的字段才会因空值产生告警。"""
    issues = validate_result({"CPMS": "", "ECPMC": "", "SHFS": "", "BZFS": ""})
    assert issues == []
    empty_required = validate_result({"ZDFS": ""})
    assert all(i["level"] != "error" for i in empty_required), empty_required


def test_字典外的字段会被提醒():
    issues = validate_result({"ZZZ_NOT_A_FIELD": "x"})
    assert issues and issues[0]["level"] == "warn"


def test_归一化结果是_ok_而不是_unknown_的情况不产生告警():
    _, status = normalize_value("Drop_SJFX", "长书脊")
    assert status == "ok"


def test_复选框字符串写法归一化():
    assert normalize_value("chkXBJF", "Y")[0] is True
    assert normalize_value("chkXBJF", "否")[0] is False


def test_树形下拉_CPLB_校验编号是否为树节点():
    """现实值形如「23.01;无线胶装书」—— 编号必须是 assets/cplb_tree.yaml 里的节点。"""
    assert normalize_value("CPLB", "23.01;无线胶装书")[1] in ("ok", "normalized")
    assert validate_result({"CPLB": "23.01;无线胶装书"}) == []
    issues = validate_result({"CPLB": "77.01;不存在的类别"})
    assert issues and issues[0]["level"] == "error"
    # 只定位到顶层 → WARN（顶层过粗，生产/计价会不准）
    issues2 = validate_result({"CPLB": "23;胶装书"})
    assert [i["level"] for i in issues2] == ["warn"]


def test_树形下拉_CPLB_名称以权威树为准():
    """手工把名称写错 → 归一化时用树里的名字覆盖，防止「编号对、名字错」流入 ERP。"""
    value, status = normalize_value("CPLB", "23.01;胶状书")
    assert value == "23.01;无线胶装书"
    assert status == "normalized"
    # 客户写英文/行话也能认（字典 aliases）
    assert normalize_value("CPLB", "perfect bound")[0] == "23.01;无线胶装书"
    assert normalize_value("CPLB", "board book")[0] == "27.02;精装BB书"


def test_normalize_result_返回变更清单():
    result = {"ZDFS": "无线胶装", "Drop_SJFX": "1", "CPMS": ""}
    out, changes = normalize_result(result)
    assert out["ZDFS"] == "胶装/PAD书"
    assert out["Drop_SJFX"] == "长书脊"
    assert {c["field"] for c in changes} == {"ZDFS", "Drop_SJFX"}


# ── 校验 ─────────────────────────────────────────────────────────────────
def test_类型校验_整数与日期与布尔():
    issues = validate_result({
        "TZS": "两款",                      # 应为整数
        "JHRQ": 1809129600000,              # 应为 YYYY-MM-DD（v1 是毫秒时间戳）
        "chkXBJF": "Y",                     # 应为布尔
    })
    fields = {i["field"] for i in issues if i["level"] == "error"}
    assert fields == {"TZS", "JHRQ", "chkXBJF"}


def test_必填字段缺失分级():
    issues = validate_result({"KHDH": ""})          # 客户单号：业务必填 → error
    assert issues[0]["level"] == "error"
    issues2 = validate_result({"CPDL": ""})         # 产品大类：建议填写 → warn
    assert issues2[0]["level"] == "warn"


@pytest.mark.skipif(not HISTORICAL.exists(), reason="历史单据不在仓库里")
def test_历史单据的三个真错误能被抓出来():
    """0e39e2e2（2026-05-25 手填）的实测缺陷 —— 这就是加字典的原因。

    1. ZDFS「无线胶装」不是 ERP 合法值（合法是「胶装/PAD书」）
    2. JHRQ/WCRQ 存的是毫秒时间戳，业务口径是 YYYY-MM-DD
    3. SFYPJ 存了布尔 false，ERP 是字符串枚举「是/否」
    """
    data = json.loads(HISTORICAL.read_text(encoding="utf-8"))
    errors = {i["field"] for i in validate_result(data) if i["level"] == "error"}
    assert {"ZDFS", "JHRQ", "WCRQ", "SFYPJ"} <= errors


@pytest.mark.skipif(not HISTORICAL.exists(), reason="历史单据不在仓库里")
def test_历史单据的错误值归一化后大部分可救():
    data = json.loads(HISTORICAL.read_text(encoding="utf-8"))
    out, _ = normalize_result({"ZDFS": data["ZDFS"], "SFYPJ": data["SFYPJ"]})
    assert out["ZDFS"] == "胶装/PAD书"
