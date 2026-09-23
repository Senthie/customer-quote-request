"""端到端回归：真实样本 → ERP JSON（59 键 + 逐字段值）。

这是 1.0.13 多客户重构的**回归基线**：重构前后对同一份样本必须逐字段一致。
样本取自仓库里归档的真实任务；样本缺失时跳过（不阻塞其它测试）。
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
TEMPLATE = REPO_ROOT / "assets" / "add_customer_quote_request.json"

# 重构前（1.0.12）对这份样本的确定输出 —— 由旧代码逻辑 + SKILL.md 5.1 实测记录得出
EXPECTED_VALUES = {
    "CPDL": "书版", "YYS": 1, "BZ": "美元", "PXH": 1, "KHMBDJ": 0,
    "CPLB": "23.01;无线胶装书",
    "CPMC": "23.01;无线胶装书-Puzzle & Colour Disney",
    "TZS": 2,
    "CPSL1": 3000, "CPSL2": 5000, "CPSL3": 7500, "CPSL4": 10000,
    "CPSL5": 20000, "CPSL6": 25000, "CPSL7": 30000,
    "CPSL8": 0, "CPSL9": 0, "CPSL10": 0,
    "CD": 198, "KD": 129, "GD": 6.85,
    "Drop_SJFX": "长书脊",
    "ZDFS": "胶装/PAD书",             # 按 ERP 合法枚举（2026-09-23 定口径）
    "DWMC": "本",                    # 业务确认 2026-09-23
    "CPSX": "3岁以上（要寄第三方测试）",  # 样本：Target Age Group=7+ 且 Safety Checks=Y
    "JHRQ": "2027-05-01",
    "KHDH": "23992 / 23993",
    "CPMS": "",                      # ★ 必须为空：由 agent 亲自撰写
}
# 脚本不该碰的字段 → 应等于模板默认值
UNTOUCHED = [
    "BDBH", "YWY", "KHZL", "GDY", "Remark", "CPMX",
    "BZFS", "TBLX", "SHFS", "FSCMC", "OLD_KHDDH", "SFYPJ", "Remark1",
    "CPLX", "KHDDH", "ECPMC", "ZXLB", "ZBLX", "ZXFS", "DKMC",
    "YSLX", "SHDQ", "KHDQ", "BGMS", "Remark4", "Remark3",
]


def _find_sample():
    for candidate in sorted((REPO_ROOT / "customer-files" / "processed").glob(
            "*/PUZZLE COLOUR DISNEY.xlsx")):
        return candidate
    return None


@pytest.fixture(scope="module")
def pipeline_run(tmp_path_factory):
    sample = _find_sample()
    if sample is None:
        pytest.skip("找不到真实样本 PUZZLE COLOUR DISNEY.xlsx，跳过端到端回归")

    workdir = tmp_path_factory.mktemp("cqr-e2e")
    pending = workdir / "pending"
    pending.mkdir()
    target = pending / sample.name
    shutil.copy2(sample, target)

    result = subprocess.run(
        ["uv", "run", "./scripts/main.py", str(target),
         "--processed-dir", str(workdir / "processed")],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, f"main.py 失败:\n{result.stdout}\n{result.stderr}"

    erp_files = list((workdir / "processed").glob("*/*.json"))
    erp_files = [p for p in erp_files if p.name.startswith("ERP_")]
    assert len(erp_files) == 1, erp_files
    return {
        "workdir": workdir,
        "stdout": result.stdout,
        "erp": json.loads(erp_files[0].read_text(encoding="utf-8")),
        "task_dir": erp_files[0].parent,
    }


def test_键集合与顺序等于模板(pipeline_run):
    template = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    out = pipeline_run["erp"]
    assert len(out) == 59
    assert list(out) == list(template)


def test_逐字段值与重构前一致(pipeline_run):
    out = pipeline_run["erp"]
    for key, expected in EXPECTED_VALUES.items():
        assert out[key] == expected, f"{key}: 期望 {expected!r}，实际 {out[key]!r}"


def test_未触碰字段等于模板默认值(pipeline_run):
    template = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    out = pipeline_run["erp"]
    for key in UNTOUCHED:
        assert out[key] == template[key], f"{key}: 不应被脚本改写"


# ── 业务决策（2026-09-23）在端到端上的表现 ─────────────────────────────
def test_CPLB_来自产品类别树(pipeline_run):
    """CPLB 必须是权威树里的节点，且名称与树一致（编号对、名字错同样会流毒）。"""
    from cplb_tree import exists, label_of
    cplb = pipeline_run["erp"]["CPLB"]
    code = cplb.split(";")[0]
    assert exists(code), f"{cplb} 的编号不在 assets/cplb_tree.yaml 里"
    assert label_of(code) == cplb, f"{cplb} 的名称与树不一致"
    assert "cplb.map" in pipeline_run["stdout"], "应走档案 cplb.map 命中"


def test_CPSX_落库且由年龄段与安全检查推导(pipeline_run):
    """业务确认：年龄段 + 安全检查要落库到 CPSX（样本：7+ 且 Safety Checks=Y）。"""
    out = pipeline_run["erp"]
    assert out["CPSX"] == "3岁以上（要寄第三方测试）"
    assert "rules.cpsx" in pipeline_run["stdout"]
    assert "仍必须" in pipeline_run["stdout"] or "CPMS" in pipeline_run["stdout"]


def test_输出不含_KHCPMC(pipeline_run):
    """保留 KHDH（ERP 侧控件名 txtKHCPMC 由 ERP 侧改），输出里不得出现 KHCPMC。"""
    out = pipeline_run["erp"]
    assert "KHDH" in out
    assert not [k for k in out if "KHCPMC" in k]


def test_每个有值的枚举字段都是中文合法值(pipeline_run):
    """决策 ③ 统一中文：所有取过值的下拉字段，输出都应是 ERP 字典里的 label 文本。"""
    from erp_schema import field_spec, legal_values
    out = pipeline_run["erp"]
    checked = 0
    for field, value in out.items():
        if field.startswith("_") or value in ("", None):
            continue
        spec = field_spec(field)
        if not spec or spec.get("type") != "select":
            continue
        tree = spec.get("options_tree")
        if tree:
            continue          # CPLB 走树节点校验（编号;名称）
        values = legal_values(field)
        if not values:
            continue
        assert str(value) in values, f"{field}={value!r} 不是中文字典里的合法值"
        checked += 1
    assert checked >= 4, f"应至少校验到 4 个下拉字段，实际 {checked}"


def test_WCRQ_是今天加十天(pipeline_run):
    from datetime import datetime, timedelta
    out = pipeline_run["erp"]
    expected = (datetime.now() + timedelta(days=10)).strftime("%Y-%m-%d")
    assert out["WCRQ"] == expected


def test_开关字段保持_false(pipeline_run):
    out = pipeline_run["erp"]
    for key in ("chkXBJF", "chkZDBJF", "chkBSYTB", "chkBSYJZX"):
        assert out[key] is False


def test_CPMS_必须为空并提示_agent(pipeline_run):
    assert pipeline_run["erp"]["CPMS"] == ""
    assert "CPMS 为空" in pipeline_run["stdout"]
    assert "字段一致性断言: 通过" in pipeline_run["stdout"]


def test_自动识别到_igl(pipeline_run):
    assert "客户档案: igl" in pipeline_run["stdout"]


def test_产物齐全(pipeline_run):
    task_dir = pipeline_run["task_dir"]
    names = {p.name for p in task_dir.iterdir()}
    assert any(n.endswith(".extracted.json") for n in names), names
    assert any(n.endswith(".validation.json") for n in names), names
    assert any(n.endswith(".csv") for n in names), names
    assert "PUZZLE COLOUR DISNEY.xlsx" in names, names


def test_ERP取值校验通过(pipeline_run):
    """跑完必须留下校验报告，且样例不该有 error（枚举/类型/必填）。"""
    report_path = next(p for p in pipeline_run["task_dir"].iterdir()
                       if p.name.endswith(".validation.json"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["errors"] == [], report["errors"]
    assert report["schema"] == "assets/erp_form_schema.yaml"
    assert "ERP 取值校验" in pipeline_run["stdout"]


def test_取值全部合法枚举(pipeline_run):
    """输出里每个有值的枚举字段，都必须落在 ERP 表单权威字典里。

    树形下拉（CPLB）按「编号前缀」判，其余按下拉 label 精确比对。
    """
    from erp_schema import field_spec, legal_values
    data = pipeline_run["erp"]
    for field, value in data.items():
        if field.startswith("_") or value in ("", None):
            continue
        spec = field_spec(field)
        if not spec:
            continue
        tree = [t["code"] for t in (spec.get("options_tree") or [])]
        if tree:
            prefix = str(value).split(";")[0].split(".")[0]
            assert prefix in tree, f"{field}={value!r} 的类别编号不在 ERP 产品类别树里"
            continue
        values = legal_values(field)
        if values:
            assert str(value) in values, f"{field}={value!r} 不在 ERP 合法枚举里"


def test_抽取明细里没有未识别标签(pipeline_run):
    detail = next(p for p in pipeline_run["task_dir"].iterdir()
                  if p.name.endswith(".extracted.json"))
    data = json.loads(detail.read_text(encoding="utf-8"))
    assert data["profile_id"] == "igl"
    assert data["unmatched_labels"] == []
    assert data["quantity_tiers"] == [3000, 5000, 7500, 10000, 20000, 25000, 30000]


def test_抽取明细里列出_CPMS_素材(pipeline_run):
    """非 ERP 字段的附加信息（纸箱/报价有效期等）必须被列出来，否则写 CPMS 会漏。"""
    detail = next(p for p in pipeline_run["task_dir"].iterdir()
                  if p.name.endswith(".extracted.json"))
    data = json.loads(detail.read_text(encoding="utf-8"))
    extras = data["extras_found"]
    assert extras, "示例文件里有纸箱/最低最高数量等附加信息"
    assert any("Carton" in e for e in extras)
    assert any("Quotation Number" in e for e in extras)


def test_输入文件已从_pending_移出(pipeline_run):
    target = pipeline_run["workdir"] / "pending" / "PUZZLE COLOUR DISNEY.xlsx"
    assert not target.exists(), "处理完成后原始文件应从 pending 移出（防重复处理）"
