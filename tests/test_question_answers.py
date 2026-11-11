"""question_answers（schema v3 · 内部确认清单）的回归测试。

业务口径：森西 2026-10-30 的 5 条拍板
  ① 放每条记录内 ② 内部确认语义 ③ ERP 自己过滤未知字段
  ④ 格式版本另用 schema_version（B 方案） ⑤ Reminder / validation warn /
  CPMS 里的「待确认」合并进这一处

这些测试钉住的是「口径」，改测试前先想清楚是不是业务又改主意了。
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import question_answers as qa  # noqa: E402

TEMPLATE = REPO / "assets" / "add_customer_quote_request.json"


# ── 条目契约 ────────────────────────────────────────────────────────────
def test_条目是固定9键且顺序敏感():
    item = qa.make_item("Q_ZDFS", "ZDFS", "线路没定")
    assert list(item) == list(qa.ITEM_KEYS)
    assert item["status"] == "open" and item["blocking"] is False
    assert item["answer"] == "" and item["answered_at"] is None


def test_不允许拿问题文本当key():
    """`{question1: ans1}` 这种写法：key 会重复、客户改措辞就成新问题。

    normalize 仍然把这种老/野写法读进来（不丢数据），但**输出**一律固定键。
    """
    raw = [{"装箱几本一箱？": "12"}]
    items = qa.normalize(raw)
    assert len(items) == 1
    assert list(items[0])[:len(qa.ITEM_KEYS)] == list(qa.ITEM_KEYS)
    # 问题文本落到 question（或额外键里），不会消失
    joined = json.dumps(items[0], ensure_ascii=False)
    assert "装箱几本一箱" in joined


def test_旧产物没有这个键时视为空():
    """v2 产物没有 question_answers → 不批量迁移，读的时候当 []。"""
    assert qa.normalize(None) == []
    assert qa.normalize("") == []
    assert qa.gates(None) == []
    assert qa.summary(None)["total"] == 0


def test_状态别名与只填answer的手写方式():
    items = qa.normalize([
        {"id": "A", "question": "q1", "answer": "确认了 140g", "status": "open"},
        {"id": "B", "question": "q2", "answer": "", "status": "done"},
        {"id": "C", "question": "q3", "status": "n/a"},
    ])
    byid = {i["id"]: i for i in items}
    # 只填 answer 忘了翻 status → 按已确认算（否则闸门永远关不上）
    assert qa.effective_status(byid["A"]) == "confirmed"
    assert qa.effective_status(byid["B"]) == "confirmed"
    assert qa.effective_status(byid["C"]) == "waived"
    assert qa.gates(items) == []


# ── 闸门 ────────────────────────────────────────────────────────────────
def test_只有blocking且未确认的项卡交付():
    items = qa.normalize([
        {"id": "Q_ZDFS", "field": "ZDFS", "question": "线路?", "blocking": False},
        {"id": "Q_PACK", "field": "CPMS", "question": "装箱?", "blocking": True},
    ])
    gates = qa.gates(items)
    assert len(gates) == 1 and "Q_PACK" in gates[0]
    # 业务口径「ZDFS 由业务补填、不猜」→ 不卡路（2026-10-21）
    assert qa.summary(items)["open_blocking"] == 1


def test_问题答完闸门自动打开():
    items = qa.normalize([{"id": "Q_PACK", "field": "CPMS", "question": "装箱?",
                           "blocking": True, "answer": "12 本/箱"}])
    assert qa.gates(items) == []


# ── 从管道输入生成 ──────────────────────────────────────────────────────
def _issue(field, level="warn", text="没填", record=None):
    out = {"field": field, "level": level, "message": text}
    if record is not None:
        out["record"] = record
    return out


def test_合并_字段级校验项进清单():
    erp_fields = {"ZDFS", "CPLB", "TZS"}
    items = qa.build_from_issues(
        [_issue("ZDFS", "warn", "ZDFS 为空 —— 由业务补填"),
         _issue("CPLB", "error", "取值不在合法枚举")],
        record_fields=erp_fields, today="2026-10-30")
    ids = [i["id"] for i in items]
    assert ids == ["Q_ZDFS", "Q_CPLB"]
    assert items[0]["blocking"] is False      # warn 不堵路
    assert items[1]["blocking"] is True       # error 现在就不能交付
    assert all(i["source"] == "pipeline" and i["asked_at"] == "2026-10-30"
               for i in items)


def test_合并_解释性说明不进清单():
    """分组说明 / unit 说明是技术备注，不是待确认项 —— 留在 validation.json。"""
    items = qa.build_from_issues(
        [_issue("分组1", "warn", "本组 3 款结构相同，合成一条"),
         _issue("unit", "warn", "父目录散文件单独成单")],
        record_fields={"ZDFS"}, today="2026-10-30")
    assert items == []


def test_合并_只收本条记录的issue():
    items = qa.build_from_issues(
        [_issue("ZDFS", "warn", "第2条的问题", record=2)],
        record_fields={"ZDFS"}, record_index=1, today="2026-10-30")
    assert items == []
    items2 = qa.build_from_issues(
        [_issue("ZDFS", "warn", "第2条的问题", record=2)],
        record_fields={"ZDFS"}, record_index=2, today="2026-10-30")
    assert [i["id"] for i in items2] == ["Q_ZDFS"]


def test_合并_同一字段多条issue不互相覆盖():
    items = qa.build_from_issues(
        [_issue("ZDFS", "warn", "问题一"), _issue("ZDFS", "warn", "问题二")],
        record_fields={"ZDFS"}, today="2026-10-30")
    assert [i["id"] for i in items] == ["Q_ZDFS", "Q_ZDFS_2"]
    assert len({i["id"] for i in items}) == 2, "同题追问不能静默覆盖"


def test_合并_报价截止提醒复制到每条记录():
    reminders = [{"kind": "offer_deadline_expired", "deadline": "2026-06-26",
                  "days_over": 90, "message": "已过期 90 天 —— 请与业务确认是否还要报价"}]
    r1 = qa.build_for_record(1, issues=[], reminders=reminders,
                             record_fields={"ZDFS"}, today="2026-10-30")
    r2 = qa.build_for_record(2, issues=[], reminders=reminders,
                             record_fields={"ZDFS"}, today="2026-10-30")
    assert r1 == r2, "文件级提醒对整条询价都成立 → 每条记录各带一条"
    assert r1[0]["id"] == "Q_OFFER_DEADLINE" and r1[0]["blocking"] is True
    # Reminder 字段降级为文本快照（权威来源是 question_answers）
    assert "还要报价" in qa.reminder_text(r1)
    assert qa.reminder_text(qa.normalize([{"id": "x", "question": "q"}])) == ""


# ── 真实产物：端到端 ────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def pipeline_run(tmp_path_factory):
    """跑一次真实管道（ngv 样本），拿到产物。

    用的是仓库里的客户样本（customer-files/samples/ngv）—— 这条链路上
    「question_answers 真的写进产物 + 查看器真的读得到」只有跑一遍才算数。
    """
    import shutil
    import subprocess

    out = tmp_path_factory.mktemp("qa_run")
    sample = REPO / "customer-files" / "samples" / "ngv"
    src = None
    for cand in sorted(sample.glob("*.xlsx")):
        src = cand
        break
    if src is None:
        pytest.skip("没有 ngv 样本文件（customer-files/samples/ngv/*.xlsx）")
    work = out / "pending"
    work.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, work / src.name)
    proc = subprocess.run(
        ["uv", "run", "./scripts/main.py", str(work),
         "--processed-dir", str(out / "processed"), "--customer", "ngv"],
        cwd=str(REPO), capture_output=True, text=True)
    assert proc.returncode == 0, f"管道没跑通：\n{proc.stdout[-800:]}\n{proc.stderr[-800:]}"
    erp_files = sorted(p for p in (out / "processed").rglob("ERP_*.json")
                       if not p.name.endswith(".erp-import.json"))
    assert erp_files, "没产出 ERP JSON"
    raw = json.loads(erp_files[0].read_text(encoding="utf-8"))
    return {"records": raw, "path": erp_files[0]}


def test_端到端_每条记录都有清单和格式号(pipeline_run):
    tpl_keys = list(json.loads(TEMPLATE.read_text(encoding="utf-8")))
    for rec in pipeline_run["records"]:
        assert rec["schema_version"] == qa.SCHEMA_VERSION
        assert isinstance(rec["question_answers"], list)
        assert list(rec)[:59] == tpl_keys, "59 键位置一个都不许动"
        assert list(rec)[59:] == ["ID", "CPMC_ZH", "Reminder", "version",
                                  "schema_version", "question_answers"]
        for item in rec["question_answers"]:
            assert list(item)[:len(qa.ITEM_KEYS)] == list(qa.ITEM_KEYS)


def test_端到端_查看器闸门认清单(pipeline_run):
    sys.path.insert(0, str(REPO / "CQR_Web"))
    sys.path.insert(0, str(REPO / "scripts"))
    from CQR_Web import data as web_data

    rec = dict(pipeline_run["records"][0])
    gates = web_data.delivery_gates(rec)
    # CPMS 未写 + CPMC 占位符 + 清单里的卡交付项（若有）
    assert any("CPMS" in g for g in gates)
    # 加一条 blocking 的 open 项 → 闸门必须多出一条（说明清单真的被读了）
    rec2 = dict(rec)
    rec2["question_answers"] = qa.normalize(
        [{"id": "Q_PACK", "field": "CPMS", "question": "装箱几本一箱？",
          "blocking": True}])
    assert any("Q_PACK" in g for g in web_data.delivery_gates(rec2))
    # 答完就不再卡
    rec3 = dict(rec)
    rec3["question_answers"] = qa.normalize(
        [{"id": "Q_PACK", "field": "CPMS", "question": "装箱几本一箱？",
          "blocking": True, "answer": "12"}])
    assert not any("Q_PACK" in g for g in web_data.delivery_gates(rec3))


# ── 交接棒：cpms_todo 也要认清单 ────────────────────────────────────────
def _task_with_questions(root, task_id, questions):
    d = root / task_id
    d.mkdir(parents=True, exist_ok=True)
    payload = {"KHDH": "CU001", "CPMS": "完整描述", "CPMC": "产品名",
               "CPMC_ZH": "图画书", "CPLB": "23.01;无线胶装书",
               "schema_version": 3, "question_answers": questions}
    (d / f"ERP_{task_id}_20260101_000000.json").write_text(
        json.dumps([payload], ensure_ascii=False), encoding="utf-8")
    (d / "x.extracted.json").write_text("{}", encoding="utf-8")
    return d


def test_交接棒_卡交付的开放项算欠账(tmp_path):
    """agent 的待办不只补 CPMS，还要把卡住的事实确认掉。"""
    import cpms_todo

    root = tmp_path / "processed"
    root.mkdir()
    _task_with_questions(root, "task_a", qa.normalize(
        [{"id": "Q_PACK", "field": "CPMS", "question": "装箱几本一箱？",
          "blocking": True}]))
    items = cpms_todo.iter_incomplete(root)
    assert len(items) == 1
    assert any("确认清单" in m and "Q_PACK" in m for m in items[0]["missing"]), items


def test_交接棒_不卡交付的项不制造噪音(tmp_path):
    """ZDFS 那种「由业务补填、不猜」的项不该让任务天天挂在待办里。"""
    import cpms_todo

    root = tmp_path / "processed"
    root.mkdir()
    _task_with_questions(root, "task_b", qa.normalize(
        [{"id": "Q_ZDFS", "field": "ZDFS", "question": "线路?", "blocking": False}]))
    assert cpms_todo.iter_incomplete(root) == []


# ── CPMS080：正文写「待确认」必须有条目承接 ─────────────────────────────
def _cpms_text(extra: str = "") -> str:
    return ("书籍方向：【长书脊】产品规格：【200*200*20】共【1】款\n"
            "封面：200g铜版纸 4C0C\n内文：300g单粉 4C4C\n装订：无线胶装书\n"
            "装箱：12 本/箱" + extra)


def test_CPMS正文写待确认且没有条目_报警():
    import cpms_lint

    spec = cpms_lint.load_spec()
    findings = cpms_lint.lint_text(
        _cpms_text("（装箱方式待确认）"), spec,
        {"question_answers": [], "CD": 200, "KD": 200, "GD": 20, "TZS": 1})
    assert any(f.code == "CPMS080" for f in findings), [f.code for f in findings]


def test_CPMS正文写待确认且有条目承接_不报警():
    import cpms_lint

    spec = cpms_lint.load_spec()
    findings = cpms_lint.lint_text(
        _cpms_text("（装箱方式待确认）"), spec,
        {"question_answers": qa.normalize(
            [{"id": "Q_PACK", "field": "CPMS", "question": "装箱方式?", "blocking": True}]),
         "CD": 200, "KD": 200, "GD": 20, "TZS": 1})
    assert not any(f.code == "CPMS080" for f in findings), [f.code for f in findings]


def test_CPMS正文写待确认_v2旧产物不判():
    """没有 question_answers 键的旧产物不回头改（不制造历史噪音）。"""
    import cpms_lint

    spec = cpms_lint.load_spec()
    findings = cpms_lint.lint_text(
        _cpms_text("（装箱方式待确认）"), spec,
        {"CD": 200, "KD": 200, "GD": 20, "TZS": 1})
    assert not any(f.code == "CPMS080" for f in findings)
