"""erp_import_adapter：产物 → ERP 导入口径的三处类型翻译。

业务口径（2026-11-11 森西）：「三处类型偏差做翻译，对接 ERP 的接口」——
产物里的内部表达（布尔 / 刻意留空）不改，差异在导出这一步翻掉。

判据全部来自实物：
  * 模板 `assets/add_customer_quote_request.json`（默认值与类型）
  * 真实单 `BJXQ261111005` / `BJXQ260706007`（erp_quote_detail 读回：
    SFYPJ="否" 文本、PXH=1、YYS=null）
"""
import copy
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import erp_import_adapter as ADP  # noqa: E402

TEMPLATE = ADP.load_template()
RULES = ADP.load_rules()


def _record(**over):
    """以模板为底造一条记录，再覆盖若干字段。"""
    rec = copy.deepcopy(TEMPLATE)
    rec.update(over)
    return rec


# ── SFYPJ：布尔 → 是 / 否 ────────────────────────────────────────────────
def test_有配件翻成是():
    out, changes = ADP.translate_record(_record(SFYPJ=True), TEMPLATE, RULES)
    assert out["SFYPJ"] == "是"
    assert any("SFYPJ" in c for c in changes)


def test_无配件翻成否():
    out, _ = ADP.translate_record(_record(SFYPJ=False), TEMPLATE, RULES)
    assert out["SFYPJ"] == "否"


def test_已经是文本的原样放行_幂等():
    rec = _record(SFYPJ="否")
    out, changes = ADP.translate_record(rec, TEMPLATE, RULES)
    assert out["SFYPJ"] == "否"
    assert not [c for c in changes if "SFYPJ" in c], "文本不该再被翻译一次"


def test_空配件落空串():
    out, _ = ADP.translate_record(_record(SFYPJ=None), TEMPLATE, RULES)
    assert out["SFYPJ"] == ""


# ── PXH：空串 → 1 ───────────────────────────────────────────────────────
def test_排序号空串补回1():
    out, changes = ADP.translate_record(_record(PXH=""), TEMPLATE, RULES)
    assert out["PXH"] == 1
    assert any("PXH" in c for c in changes)


def test_排序号已有值不动():
    out, changes = ADP.translate_record(_record(PXH=3), TEMPLATE, RULES)
    assert out["PXH"] == 3
    assert not [c for c in changes if "PXH" in c]


# ── YYS：空串 → null（不是 1）────────────────────────────────────────────
def test_语言数空串落null而不是1():
    out, changes = ADP.translate_record(_record(YYS=""), TEMPLATE, RULES)
    assert out["YYS"] is None, "写 1 会把「不适用」变成「一种语言」"
    assert any("YYS" in c for c in changes)


def test_语言数有值不动():
    out, _ = ADP.translate_record(_record(YYS=7), TEMPLATE, RULES)
    assert out["YYS"] == 7


# ── 兜底：整数字段空串 → null，但 0 和布尔都不许被动 ────────────────────
def test_整数字段空串落null():
    out, changes = ADP.translate_record(_record(TZS=""), TEMPLATE, RULES)
    assert out["TZS"] is None
    assert any("TZS" in c for c in changes)


def test_数量0不许变成null():
    out, _ = ADP.translate_record(_record(CPSL1=0, CPSL2=0), TEMPLATE, RULES)
    assert out["CPSL1"] == 0 and out["CPSL2"] == 0


def test_复选框布尔不许被当成整数0():
    rec = _record(chkXBJF=False, chkZDBJF=False)
    out, _ = ADP.translate_record(rec, TEMPLATE, RULES)
    assert out["chkXBJF"] is False and out["chkZDBJF"] is False


# ── 结构：剥内部字段 + 键序 = 模板序 ────────────────────────────────────
def test_内部字段被剥掉且键序与模板一致():
    rec = _record(ID="abc", version=2, schema_version=3, CPMC_ZH="中文名",
                  Reminder="", question_answers=[])
    out, _ = ADP.translate_record(rec, TEMPLATE, RULES)
    assert list(out) == list(TEMPLATE), "键序必须与 ERP 模板逐位一致"
    for k in ("ID", "version", "schema_version", "CPMC_ZH", "Reminder", "question_answers"):
        assert k not in out


def test_未知键直接报错():
    rec = _record()
    rec["KHCPMC"] = "x"
    with pytest.raises(KeyError):
        ADP.translate_record(rec, TEMPLATE, RULES)


def test_缺字段直接报错():
    rec = _record()
    del rec["CPMS"]
    with pytest.raises(KeyError):
        ADP.translate_record(rec, TEMPLATE, RULES)


def test_整份文件幂等():
    records = [
        _record(SFYPJ=True, PXH="", YYS="", TZS="", CPSL1=12000),
        _record(SFYPJ=False, PXH="", YYS=3, CPSL1=0, ID="t"),
    ]
    once, _ = ADP.translate_bundle(records, TEMPLATE, RULES)
    twice, changes2 = ADP.translate_bundle(once, TEMPLATE, RULES)
    assert once == twice
    assert changes2 == [], "翻过的记录再翻一次不该有任何变更"


# ── 规则表本身 ──────────────────────────────────────────────────────────
def test_三处规则都在且带理由():
    fields = RULES["fields"]
    assert set(fields) == {"SFYPJ", "PXH", "YYS"}
    for name, spec in fields.items():
        assert spec.get("reason"), f"{name} 规则缺理由"


def test_cli_check不写文件(tmp_path, capsys):
    src = tmp_path / "ERP_x.json"
    src.write_text(json.dumps([_record(SFYPJ=True, PXH="", YYS="")], ensure_ascii=False),
                   encoding="utf-8")
    assert ADP.main([str(src), "--check"]) == 0
    assert not list(tmp_path.glob("*.erp-import.json")), "--check 不该落盘"
    assert "SFYPJ" in capsys.readouterr().out


def test_cli默认输出文件名(tmp_path):
    src = tmp_path / "ERP_abc_20261111.json"
    src.write_text(json.dumps([_record(SFYPJ=True, PXH="", YYS="")], ensure_ascii=False),
                   encoding="utf-8")
    assert ADP.main([str(src)]) == 0
    dest = tmp_path / "ERP_abc_20261111.erp-import.json"
    assert dest.exists()
    out = json.loads(dest.read_text(encoding="utf-8"))
    assert out[0]["SFYPJ"] == "是" and out[0]["PXH"] == 1 and out[0]["YYS"] is None
    assert list(out[0]) == list(TEMPLATE)
