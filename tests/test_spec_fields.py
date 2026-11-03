#!/usr/bin/env python3
"""`spec_fields`（规格 / 物流 11 字段推导）的测试。

业务 2026-10-30（森西）：「BZFS TBLX SHFS SHDQ ZXLB ZBLX ZXFS DKMC YSLX chkBSYTB BZ：
没有一处推导代码。我认为在你填写 CPMS 的时候，这些值由你推导出，并且填到对应的值，
这样也可以减轻 CPMS 的负担。」

这份测试守四件事：
  ① 规则表覆盖全部 11 个字段，且**每个推导值都是 ERP 合法枚举**（写错一个字等于没填）；
  ② 客户原文有证据就推得出来（含三件套 SHFS/YSLX/SHDQ 要互相对得上）；
  ③ **没证据绝不猜**（只有 `FOB` 不写口岸 / `free Harbour` → 一律不推）；
  ④ 推不出来的字段进内部确认清单（`gap_items`），不卡交付。
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import erp_schema  # noqa: E402
import spec_fields  # noqa: E402
from question_answers import ITEM_KEYS  # noqa: E402

SPEC_FIELDS = ["BZFS", "TBLX", "SHFS", "SHDQ", "ZXLB", "ZBLX",
               "ZXFS", "DKMC", "YSLX", "chkBSYTB", "BZ"]
CHECKS = {"chkBSYTB"}


# ── ① 规则表本身 ─────────────────────────────────────────────────────────
def test_规则表覆盖业务点名的11个字段():
    rules = spec_fields.load_rules()
    assert set(rules["fields"]) == set(SPEC_FIELDS), (
        "业务点名的 11 个字段一个都不能少（多出来的字段要说明理由）")
    for field, spec in rules["fields"].items():
        assert spec.get("label"), f"{field} 要有中文标签（日志/清单里要用）"
        if spec.get("derivable") is False:
            # 「语料证明推不出来」也是结论，但必须写明理由（不能默默没规则）
            assert spec.get("reason"), f"{field} 声明不推导就必须写 reason"
            continue
        assert spec.get("evidence"), f"{field} 至少要有一条推导规则"


def test_每个推导值都是ERP合法枚举():
    """规则表里写的值必须是 ERP 下拉里的原文 —— 写错一个字符 ERP 就静默丢弃。

    这是这份测试最要紧的一条：它让「加一条规则」在 CI 里就能验证，
    而不是等导入 ERP 时才发现值是空的。
    """
    rules = spec_fields.load_rules()
    problems = []
    for field, spec in rules["fields"].items():
        legal = erp_schema.legal_values(field)
        for rule in spec.get("evidence") or []:
            value = rule.get("value")
            if field in CHECKS:
                if not isinstance(value, bool):
                    problems.append(f"{field}: 勾选框的值必须是 true/false，实际 {value!r}")
                continue
            if not legal:
                problems.append(f"{field}: 字典里没有枚举（不该进推导表）")
                continue
            if str(value) not in legal:
                problems.append(f"{field}: {value!r} 不在 ERP 枚举里（{legal}）")
        follow = spec.get("derive_from")
        if isinstance(follow, dict) and field not in CHECKS:
            problems.append(f"{field}: derive_from 只用于勾选框")
    assert not problems, "规则表有非法值：\n" + "\n".join(problems)


def test_派生出来的SHDQ与YSLX同组():
    """联动关系（SHDQ.linked_to，业务 2026-10-30）不能被推导表破坏。

    规则表允许「蛇口 → SHDQ=FOB蛇口 / YSLX=FOB蛇口」这种三件套。这条用例把两个
    特性钉在一起：推导出的组合必须满足 `linked_options(SHDQ, YSLX)`。
    """
    text = "Delivery: FOB 蛇口, packing: 14 books per export carton, price in USD"
    items = {i["field"]: i["value"] for i in spec_fields.derive(text)}
    assert items.get("SHFS") == "FOB蛇口"
    assert items.get("YSLX") == "FOB蛇口"
    assert items.get("SHDQ") == "FOB蛇口"
    allowed = erp_schema.linked_options("SHDQ", items["YSLX"])
    assert allowed and items["SHDQ"] in allowed, (
        f"SHDQ={items['SHDQ']} 不在 YSLX={items['YSLX']} 的可选值里：{allowed}")
    assert erp_schema.validate_result(items) == [], erp_schema.validate_result(items)


# ── ② 有证据就推 ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,field,expected", [
    ("客户目标价 1.20 €/本", "BZ", "欧元"),
    ("Target price: USD 0.85 per book", "BZ", "美元"),
    ("目标价 6.5 元人民币", "BZ", "人民币"),
    ("14 books per export carton", "BZFS", "纸箱包装"),
    ("包装：牛皮纸", "BZFS", "牛皮纸包装"),
    ("Delivery: FOB 盐田", "SHFS", "FOB 盐田"),
    ("Delivery: FOB 盐田", "YSLX", "FOB盐田(普通)"),
    ("Delivery: CIF Hamburg", "YSLX", "CIF"),
    ("发货到东莞凤岗工厂", "SHDQ", "东莞凤岗"),
    ("flat packed in carton", "ZXFS", "平装"),
    ("无需底卡", "DKMC", "无需"),
])
def test_客户原文有证据就推得出来(text, field, expected):
    items = {i["field"]: i["value"] for i in spec_fields.derive(text)}
    assert items.get(field) == expected, items
    # 每条都要能说出「凭什么」——证据原文非空
    hit = next(i for i in spec_fields.derive(text) if i["field"] == field)
    assert hit["evidence"], "推导结果必须带证据原文（业务要能核对）"


def test_推不出纸板类型就不瞎认材料里的坑号():
    """`A=B` 出现在**内文用纸**里不等于纸箱纸板等级 —— 不能当 ZBLX。

    客户写「350g粉灰裱W9坑纸板」是内文/封面材料；外箱等级要在「纸箱/carton」
    语境里出现才算。误判会直接把外箱成本算错。
    """
    items = {i["field"]: i["value"] for i in spec_fields.derive("内文 350g粉灰裱W9坑纸板")}
    assert "ZBLX" not in items


# ── ③ 没证据绝不猜 ───────────────────────────────────────────────────────
def test_只说FOB不说口岸就不推口岸类字段():
    """`FOB (Incoterms 2020)` / `free Harbour (FOB)` 都没写口岸。

    实测语料里这两种写法都有（NGV 的 Ostermalblock / Rossmann 单）。
    猜成香港或盐田 = 报价基数错，比留空危险得多 → 一律不推。
    """
    for text in ("Delivery: FOB (Incoterms 2020)",
                 "delivery free on board Delivery: free Harbour (FOB)"):
        items = {i["field"] for i in spec_fields.derive(text)}
        assert not ({"SHFS", "YSLX", "SHDQ"} & items), (text, items)


def test_空白原文什么都不推():
    assert spec_fields.derive("") == []
    assert spec_fields.derive("   \n\t ") == []


def test_规则表坏一条正则不影响其它字段(capsys):
    rules = {"fields": {
        "BZ": {"label": "币种", "evidence": [
            {"match": "([", "value": "美元"},           # 坏正则
            {"match": "USD", "value": "美元"},          # 好规则
        ]},
    }}
    items = spec_fields.derive("price in USD", rules)
    assert [i["value"] for i in items] == ["美元"]
    assert "正则无效" in capsys.readouterr().err


# ── ④ 联动与缺口 ─────────────────────────────────────────────────────────
def test_不打卡板不再连带勾上不使用台板():
    """★ 2026-11-03 语料证伪（18476 份真实 ERP 报价单）：

    曾以为 `TBLX=不打卡板` 与 `chkBSYTB=true` 是同一件事的两种写法，
    但语料里 TBLX=不打卡板 的 7236 份记录 chkBSYTB **全是 false**，
    全语料 true 出现 0 次 —— 两者在 ERP 里不联动，所以只推 TBLX、不推勾选框。
    """
    items = {i["field"]: i["value"] for i in spec_fields.derive("no pallets required")}
    assert items.get("TBLX") == "不打卡板"
    assert "chkBSYTB" not in items, "勾选框由客户档案给，不从正文/联动推"


def test_缺口清单只报空字段且不卡交付():
    record = {f: "" for f in SPEC_FIELDS}
    record["BZ"] = "美元"                 # 有值的不要报
    record["chkBSYTB"] = False            # 勾选框的 false 是明确答案，不算缺
    gaps = spec_fields.missing(record)
    fields = {g["field"] for g in gaps}
    assert "BZ" not in fields and "chkBSYTB" not in fields
    assert "SHDQ" in fields

    items = spec_fields.gap_items(record, today="2026-10-30")
    assert items and all(list(i) == list(ITEM_KEYS) for i in items), "必须是固定 9 键"
    assert all(i["blocking"] is False for i in items), "装箱类待确认不拦交付"
    assert {i["field"] for i in items} == fields
    assert all(i["asked_at"] == "2026-10-30" for i in items)


def test_证据文本含规范键与文本块():
    class Fake:
        values = {"primary_packaging": "14 books per export carton"}
        scanned = {"currency": "USD"}
        text_blocks = ["Delivery: FOB 蛇口"]

    blob = spec_fields.evidence_text(Fake(), extra="补充：目标价 1.2 €")
    items = {i["field"]: i["value"] for i in spec_fields.derive(blob)}
    assert items["BZFS"] == "纸箱包装", "规范键的值也是证据"
    assert items["YSLX"] == "FOB蛇口", "文本块里的口岸要推出来"
    assert items["BZ"] == "欧元", "extra（邮件正文）也要算证据"
