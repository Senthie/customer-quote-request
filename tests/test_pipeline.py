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
    """前 59 键必须与 ERP 模板完全一致（键集合 + 顺序），其后只追加声明的内部字段。

    业务 2026-09-24：产物追加内部字段 `ID`（本次任务）/ `CPMC_ZH`（产品名称中文表达）。
    ERP 键一个都不许动位置 —— 下游解析器按位置读。
    """
    from internal_fields import load_internal_fields

    template = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    out = pipeline_run["erp"]
    internal_keys = [str(f["key"]) for f in load_internal_fields()]
    assert internal_keys == ["ID", "CPMC_ZH", "Reminder"], "内部字段的键与顺序由声明文件决定"

    assert list(out) == list(template) + internal_keys
    assert list(out)[:59] == list(template), "59 个 ERP 键的位置不许动"
    assert len(out) == 59 + len(internal_keys)


def test_内部字段_ID_标注本次任务(pipeline_run):
    """`ID` 必须等于本次任务号（= 产物目录名 = 日志里的「任务ID」）—— 方便日志维护。"""
    out = pipeline_run["erp"]
    task_id = pipeline_run["task_dir"].name
    assert out["ID"] == task_id, f"ID={out['ID']!r} 与任务目录名 {task_id!r} 不一致"
    assert task_id and task_id != "unknown"
    # 同一份产物里，validation.json 也带上同一个 ID
    vfile = next(pipeline_run["task_dir"].glob("*.validation.json"))
    assert json.loads(vfile.read_text(encoding="utf-8"))["task_id"] == task_id


def test_内部字段_CPMC_ZH_与_CPMC分工(pipeline_run):
    """`CPMC` 存英文表达、中文表达落 `CPMC_ZH`（业务 2026-09-24）。

    这份样本是 **igl** —— 它**还没迁移**（未声明 `rules.cpmc.zh_template`）：
    igl 的 CPMC 历史口径是 `编号;名称-系列`（含中文类别名），英文版怎么写
    **等业务补充真实 ERP 信息后再处理**（2026-09-24 拍板）→ 见 SKILL.md 第 12 节。
    所以这里只钉住「字段存在 + 未迁移时为空」，
    NGV 那种已迁移的严格断言在 tests/test_ngv_profile.py。
    """
    out = pipeline_run["erp"]
    assert out["ID"], "内部字段 ID 必须有值"
    assert "CPMC_ZH" in out, "内部字段 CPMC_ZH 必须在产物里（哪怕为空）"
    assert out["CPMC_ZH"] == "", "igl 尚未声明 zh_template → CPMC_ZH 为空（待补真实 ERP 信息）"
    assert out["CPMC"] == EXPECTED_VALUES["CPMC"], "未迁移的客户 CPMC 保持历史写法不变"


def test_内部字段_Reminder_把提醒固化进产物(pipeline_run):
    """业务 2026-09-24：「这提醒现在每次跑都会自动出来」→ 加字段固化。

    `Reminder` 存在、类型是字符串；这份 igl 样本没有报价截止日期 → 空字符串
    （「没有待办」= 空；「值为无」= null —— 两件事，别混）。
    """
    out = pipeline_run["erp"]
    assert "Reminder" in out, "内部字段 Reminder 必须在产物里"
    assert isinstance(out["Reminder"], str)
    assert out["Reminder"] == "", "没取到截止日期 → 没有提醒 → 空字符串"


def test_决策_无落JSON_null(pipeline_run):
    """业务 2026-09-24：「CPSX 或者其他值的『无』使用 json 里的 null 填充」。

    ERP 自身导出里空值就是 null，所以「无」落 null 既合业务语言、又不触发下拉枚举校验。
    """
    template = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    out = pipeline_run["erp"]
    assert "无" not in {v for v in out.values() if isinstance(v, str)}, (
        "产物里不该再出现字符串「无」—— 业务口径是落 JSON null"
    )
    assert template["CPSX"] == "", "模板默认是空字符串（脚本再按口径转换）"


def test_逐字段值与重构前一致(pipeline_run):
    out = pipeline_run["erp"]
    for key, expected in EXPECTED_VALUES.items():
        assert out[key] == expected, f"{key}: 期望 {expected!r}，实际 {out[key]!r}"


def test_未触碰字段等于模板默认值(pipeline_run):
    template = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    out = pipeline_run["erp"]
    for key in UNTOUCHED:
        assert out[key] == template[key], f"{key}: 不应被脚本改写"


# ── 内部导出字段：ID / CPMC_ZH（业务 2026-09-24）──────────────────────
def test_step6_内部字段追加与_strip_internal(tmp_path):
    """两种输出模式都要对：

    * 默认       → 59 个 ERP 键 + 追加内部字段（`ID` / `CPMC_ZH`）
    * --strip    → 只有 59 个 ERP 键（给「ERP 导入端不收未知字段」兜底）

    直接调 step6（不跑子进程），逐键核对顺序与取值。
    """
    from extract_fields import CustomerData
    from main import Logger, step6_output_results
    from customer_profile import load_profile

    template = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    tpl_keys = list(template)
    internal_keys = ["ID", "CPMC_ZH", "Reminder"]
    profile = load_profile("ngv")

    def _run(tag: str, strip: bool, cpmc_zh: str, deadline: dict | None = None) -> dict:
        src = tmp_path / tag / "input.csv"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("enquiry\nAnfrage,Test Book\n", encoding="utf-8")
        result = dict(template)
        result["_task_id"] = tag
        if cpmc_zh:
            result["_cpmc_zh"] = cpmc_zh
        if deadline:
            result["_deadline"] = deadline
        info = step6_output_results(
            src, result, tmp_path / tag / "processed", Logger(),
            CustomerData(values={}), profile, None, strip_internal=strip)
        return json.loads(Path(info["erp_json"]).read_text(encoding="utf-8"))

    # ① 默认：追加内部字段，且不改动 59 键
    erp = _run("with_internal", False, "KM/Aldi 中文品名 普通蛇圈书/Test Book(FSC Mix)",
               {"deadline": "2026-06-26", "expired": True, "days": 90,
                "message": "报价截止日期 2026-06-26 已过期 90 天（基准日 2026-09-24）"})
    assert list(erp) == tpl_keys + internal_keys, list(erp)[-4:]
    assert erp["ID"] == "with_internal", "ID 必须是本次任务号"
    assert erp["CPMC_ZH"] == "KM/Aldi 中文品名 普通蛇圈书/Test Book(FSC Mix)"
    # Reminder：把「每次跑都会自动出来」的提醒固化进产物（业务 2026-09-24）
    assert "已过期 90 天" in erp["Reminder"], erp["Reminder"]
    assert "还要报价" in erp["Reminder"], erp["Reminder"]
    for key in tpl_keys:
        assert key in erp, key

    # ② --strip-internal：只有 59 键，一个不多一个不少
    erp2 = _run("stripped", True, "中文表达不应该出现")
    assert list(erp2) == tpl_keys and len(erp2) == 59
    for k in internal_keys:
        assert k not in erp2

    # ③ 没有提醒时 Reminder 是空字符串（不是 null —— null 只留给「值为无」）
    erp3 = _run("no_reminder", False, "")
    assert erp3["Reminder"] == "" and erp3["CPMC_ZH"] == ""


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
