# -*- coding: utf-8 -*-
"""业务口径 2026-11-16（森西三条决定）的回归：

1. **CPSL 存「单款本数」** —— 不再是印刷总量。导出层 × 款数 还原成 ERP 要的总量，
   所以不变式是 `CPSL1 × TZS == 客户总本数`。除不尽时**不许静默均分**。
2. **判断不出装订方式 → 落 ERP 第一个合法值「胶装/PAD书」占位**（套装类仍是「套装」），
   并留一条确认项、CPMS 里标明「需业务自行判断」。
   ★ 2026-11-17 森西改口径：此前是「没提到就默认 快线BB书」，已作废。
3. KHDH 可用 RFQ 号 —— 见 `tests/test_ingest_email.py`。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import customer_profile as C  # noqa: E402
import main as MAIN  # noqa: E402
from customer_profile import load_profile  # noqa: E402
from extract_fields import CustomerData  # noqa: E402
from main import default_binding_for, map_fields  # noqa: E402
from product_groups import build, to_customer_data  # noqa: E402

NGV = load_profile("ngv")
TEMPLATE = json.loads((REPO_ROOT / "assets" / "add_customer_quote_request.json")
                      .read_text(encoding="utf-8"))


class _Quiet:
    def info(self, *a, **k):
        pass

    warn = error = debug = info

    def section(self, *a, **k):
        pass

    def field_mapping(self, *a, **k):
        pass

    def elapsed_time(self):
        return 0.0


def _entry(label: str, quantity=None) -> dict:
    entry = {"label": label, "values": {}, "quantity": quantity}
    return entry


def _result(quantity: str, quantities: list[int]):
    data = CustomerData(
        values={"size": "210 x 280 mm", "binding": "softcover, perfect binding",
                "extent": "24 pp", "quantity": quantity},
        products=[_entry(f"款{i}", quantity=q) for i, q in enumerate(quantities, 1)],
    )
    groups = build(data, NGV, _Quiet())
    return map_fields(to_customer_data(groups[0], data), TEMPLATE, NGV, _Quiet())


# ── ① CPSL = 单款本数 ───────────────────────────────────────────────────
def test_CPSL存单款_总量由款数还原():
    r = _result("12000", [3000, 4000, 5000])      # 3 款，合计 12000
    assert r["TZS"] == 3
    assert r["CPSL1"] == 4000, "CPSL1 必须是单款 12000/3"
    assert r["CPSL1"] * r["TZS"] == 12000


def test_除不尽时不静默均分():
    """130000 本 ÷ 3 款除不尽 → 保留总数并标冲突，绝不四舍五入后当单款。"""
    r = _result("130000", [50000, 40000, 40000])
    assert r["TZS"] == 3
    assert r["CPSL1"] == 130000, "除不尽时保留总量交人工，不要私自均分"
    assert r.get("_qty_conflict"), "必须留下冲突标记，让人知道这单要确认"


def test_能整除时才均分():
    r = _result("130000", [50000, 40000, 40000])   # 上面那单的对照：款数写对就均分
    assert r["CPSL1"] == 130000
    r2 = _result("120000", [40000, 40000, 40000])
    assert r2["CPSL1"] == 40000 and r2["CPSL1"] * r2["TZS"] == 120000


# ── ② 装订缺省（2026-11-17 口径变更：快线BB书 → ERP 第一个合法值）────────
def test_装订缺省是ERP第一个合法值():
    """业务 2026-11-17：判断不出就落**第一个值**，再在 CPMS 里标明需业务自行判断。"""
    assert default_binding_for({"CPLB": "23.01;无线胶装书"}) == "胶装/PAD书"
    assert default_binding_for({}) == "胶装/PAD书"


def test_缺省值必须等于表单字典的第一个枚举():
    """ERP 下拉顺序一变，缺省值就跟着错 —— 钉住它，别让人默默改常量。"""
    import erp_schema

    schema = erp_schema.load_schema()
    first = schema["fields"]["ZDFS"]["options"][0]
    assert MAIN.DEFAULT_BINDING == first, \
        f"缺省装订 {MAIN.DEFAULT_BINDING!r} ≠ ERP 第一个合法值 {first!r}"


def test_套装类不套用缺省装订():
    assert default_binding_for({"CPLB": "32.00;其它套装"}) == "套装"
    assert default_binding_for({"CPMC": "礼盒套装"}) == "套装"


def test_判断不出装订时留确认项(monkeypatch):
    """产出必须带一条「装订方式无法判断…需业务自行判断」的确认项，agent 才能写进 CPMS。"""
    profile = C.load_profile("generic")
    record = {"CPLB": "23.01;无线胶装书",
              "CPMC": "犯罪悬疑与谜题宇宙解谜书",
              "ZDFS": default_binding_for({"CPLB": "23.01;无线胶装书"})}

    class _Data(dict):
        pass

    note = MAIN.binding_inferred_note(record, _Data({"binding": ""}), profile)
    assert "无法判断" in note
    assert "需业务自行判断" in note
    assert "胶装/PAD书" in note


def test_有装订证据时不留确认项():
    profile = C.load_profile("generic")
    # 档案 binding.map 命中（generic 默认关闭映射，用合成档案验证判据）
    fake = type("P", (), {"rule": lambda self, key, default=None: {
        "binding.match_keys": ["binding"],
        "binding.map": {"perfect binding": "胶装/PAD书"},
    }.get(key, default)})()
    record = {"ZDFS": "胶装/PAD书"}
    assert MAIN.binding_inferred_note(record, {"binding": "Softcover, perfect binding"}, fake) == ""
    # 没有证据时才报
    assert MAIN.binding_inferred_note(record, {"binding": "Softcover, sewn"}, fake)
