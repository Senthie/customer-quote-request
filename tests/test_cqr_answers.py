# -*- coding: utf-8 -*-
"""页面写答案（`POST /api/answer` → `CQR_Web/answers.py`）的回归测试。

业务 2026-10-30（森西）：「在展示 web 上加入 question_answers 字段，新增一个回答的
文本框，这样我们可以在页面上直接编写答案」。

钉住的四件事：
  1. **只改 question_answers**（answer/status/answered_at）+ 顺带重算 Reminder 快照，
     CPMS / 品名 / 59 个 ERP 字段一个都不许动；
  2. 写盘是原子的，键序/缩进/中文与 main.py 产出完全一致（下游看不出人改过）；
  3. 判据：有答案 → confirmed + 盖日期；清空 → open；作废 → waived 不卡交付；
  4. 拒绝的姿势要明确：v2 旧产物 409、未知 id 404、被别处改过 409、乱填 status 400。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO))

import question_answers as qa  # noqa: E402
from CQR_Web.answers import AnswerError, answer_question  # noqa: E402

TEMPLATE = REPO / "assets" / "add_customer_quote_request.json"


def _record(**over) -> dict:
    """一条最小但结构完整的 v3 记录（59 键 + 内部字段）。"""
    base = {k: "" for k in json.loads(TEMPLATE.read_text(encoding="utf-8"))}
    base.update({
        "CPMC": "滑动发声书", "CPMC_ZH": "滑动发声书", "CPMS": "书籍方向：【长书脊】…",
        "CPLB": "27.01;普通BB书", "TZS": 3, "CPSL1": 182670,
    })
    items = qa.normalize([
        {"id": "Q_ZDFS", "field": "ZDFS", "question": "装订方式为空（建议填写）",
         "blocking": False, "source": "pipeline", "asked_at": "2026-10-30"},
        {"id": "Q_PACK", "field": "CPMS", "question": "装箱几本一箱？",
         "blocking": True, "source": "agent", "asked_at": "2026-10-30"},
    ])
    base.update({"ID": "t1", "version": 2, "schema_version": 3,
                 "Reminder": qa.reminder_text(items), "question_answers": items})
    base.update(over)
    return base


@pytest.fixture()
def export(tmp_path: Path) -> Path:
    p = tmp_path / "ERP_t1_20261030_000000.json"
    p.write_text(json.dumps([_record()], ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _load(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


# ── 正常路径 ────────────────────────────────────────────────────────────
def test_写答案_标为已确认并盖日期(export: Path):
    res = answer_question(export, record_index=1, qid="Q_PACK",
                          answer="12 本/箱", today="2026-11-02")
    item = res["item"]
    assert item["answer"] == "12 本/箱"
    assert item["status"] == "confirmed"
    assert item["answered_at"] == "2026-11-02"
    # 卡交付的那条解决了 → 闸门空了；Q_ZDFS（blocking=false）本来就不拦
    assert res["gates"] == []
    assert res["summary"]["open"] == 1, "Q_ZDFS 仍处未确认"

    saved = _load(export)[0]
    byid = {i["id"]: i for i in saved["question_answers"]}
    assert byid["Q_PACK"]["answer"] == "12 本/箱"
    assert byid["Q_ZDFS"]["status"] == "open" and byid["Q_ZDFS"]["answer"] == ""


def test_写答案_只动清单与Reminder快照(export: Path):
    before = _load(export)[0]
    answer_question(export, record_index=1, qid="Q_PACK", answer="12 本/箱")
    after = _load(export)[0]

    # 59 个 ERP 键 + 内部字段的**顺序与取值**都不许变（除了 Reminder 快照）
    assert list(after) == list(before), "键序被改动 = 下游按位置读的解析器会错"
    changed = {k for k in after if after[k] != before[k]}
    assert changed == {"question_answers", "Reminder"}, changed
    assert after["CPMS"] == before["CPMS"] and after["CPMC"] == "滑动发声书"


def test_写答案_Reminder快照跟着消失(export: Path):
    """答完最后一条卡交付项 → Reminder 该空了（它是快照，不是另一本账）。"""
    first = _load(export)[0]
    assert "装箱几本一箱" in first["Reminder"], "fixture 前提：卡交付项在 Reminder 里"
    answer_question(export, record_index=1, qid="Q_PACK", answer="12 本/箱")
    assert _load(export)[0]["Reminder"] == ""


def test_清空答案回到未确认(export: Path):
    answer_question(export, record_index=1, qid="Q_PACK", answer="12 本/箱",
                    today="2026-11-02")
    res = answer_question(export, record_index=1, qid="Q_PACK", answer="   ")
    item = res["item"]
    assert item["answer"] == "" and item["status"] == "open"
    assert item["answered_at"] is None
    assert "Q_PACK" in " ".join(res["gates"]), "重新卡交付"


def test_作废不卡交付且保留原答案(export: Path):
    answer_question(export, record_index=1, qid="Q_PACK", answer="12 本/箱")
    res = answer_question(export, record_index=1, qid="Q_PACK", answer="12 本/箱",
                          status="waived")
    assert res["item"]["status"] == "waived"
    assert res["gates"] == [], "作废后不再卡交付"


def test_多记录只改指定那一条(tmp_path: Path):
    p = tmp_path / "ERP_t2_20261030_000000.json"
    p.write_text(json.dumps([_record(CPMC="第一条"), _record(CPMC="第二条")],
                            ensure_ascii=False, indent=2), encoding="utf-8")
    answer_question(p, record_index=2, qid="Q_PACK", answer="24 本/箱")
    recs = _load(p)
    assert recs[0]["question_answers"][1]["answer"] == ""
    assert recs[1]["question_answers"][1]["answer"] == "24 本/箱"


def test_原子写_文件始终是合法JSON且缩进不变(export: Path):
    answer_question(export, record_index=1, qid="Q_PACK", answer="12 本/箱")
    text = export.read_text(encoding="utf-8")
    assert text.startswith("[\n  {"), "顶层仍是数组 + 缩进 2"
    assert "中文品名" not in text.replace("滑动发声书", ""), "不该冒出新内容"
    json.loads(text)                                  # 能解析
    assert not list(export.parent.glob(".answer_*")), "临时文件必须被 replace 掉"


def test_单对象旧文件写回还是单对象(tmp_path: Path):
    """形状照原样还原 —— 不因为写过一次就把 `{...}` 悄悄变成 `[{...}]`。"""
    p = tmp_path / "ERP_old_20261030_000000.json"
    p.write_text(json.dumps(_record(), ensure_ascii=False, indent=2), encoding="utf-8")
    answer_question(p, record_index=1, qid="Q_PACK", answer="12 本/箱")
    data = json.loads(p.read_text(encoding="utf-8"))
    assert isinstance(data, dict)


def test_结尾换行跟随原文件_不改文件末尾(tmp_path: Path):
    """原文件末尾有 `\\n` 就保留、没有就不加 —— 人改一行答案不该连带改动文件末尾。"""
    body = json.dumps([_record()], ensure_ascii=False, indent=2)
    for keep, text in ((True, body + "\n"), (False, body)):
        p = tmp_path / f"ERP_tail_{int(keep)}_20261030_000000.json"
        p.write_text(text, encoding="utf-8")
        answer_question(p, record_index=1, qid="Q_PACK", answer="12 本/箱")
        after = p.read_text(encoding="utf-8")
        assert after.endswith("\n") is keep, f"原文件 keep_newline={keep} 时被改掉了"


# ── 拒绝的姿势 ──────────────────────────────────────────────────────────
def test_v2旧产物拒绝写入(tmp_path: Path):
    rec = _record()
    rec.pop("question_answers")
    rec.pop("schema_version")
    p = tmp_path / "ERP_t3_20261030_000000.json"
    p.write_text(json.dumps([rec], ensure_ascii=False), encoding="utf-8")
    with pytest.raises(AnswerError) as exc:
        answer_question(p, record_index=1, qid="Q_PACK", answer="12")
    assert exc.value.status == 409 and "v2" in str(exc.value)
    assert "question_answers" not in _load(p)[0], "没写进去"


def test_未知id和越界记录都拒绝(export: Path):
    with pytest.raises(AnswerError) as e1:
        answer_question(export, record_index=1, qid="Q_NOPE", answer="x")
    assert e1.value.status == 404 and "Q_PACK" in str(e1.value), "错误信息要列出现有 id"

    with pytest.raises(AnswerError) as e2:
        answer_question(export, record_index=9, qid="Q_PACK", answer="x")
    assert e2.value.status == 404


def test_乱填status拒绝(export: Path):
    with pytest.raises(AnswerError) as exc:
        answer_question(export, record_index=1, qid="Q_PACK", answer="x", status="糊了")
    assert exc.value.status == 400
    assert _load(export)[0]["question_answers"][1]["answer"] == "", "失败不该写盘"


def test_确认但答案为空拒绝(export: Path):
    with pytest.raises(AnswerError) as exc:
        answer_question(export, record_index=1, qid="Q_PACK", answer="", status="confirmed")
    assert exc.value.status == 400


def test_被别处改过就拒绝并保持原文件(export: Path):
    # 页面加载时看到的 mtime（故意比真实值旧 —— 等价于「页面开着，agent 后来写了 CPMS」）
    stale_mtime = export.stat().st_mtime - 10
    export.write_text(json.dumps([_record(CPMS="agent 刚写完的 CPMS")],
                                 ensure_ascii=False, indent=2), encoding="utf-8")
    with pytest.raises(AnswerError) as exc:
        answer_question(export, record_index=1, qid="Q_PACK", answer="12",
                        expected_mtime=stale_mtime)
    assert exc.value.status == 409 and "改过" in str(exc.value)
    assert _load(export)[0]["CPMS"] == "agent 刚写完的 CPMS"


def test_答案里的换行与中文原样保留(export: Path):
    text = "12 本/箱\n外箱：5 层瓦楞纸板\n客户回邮 2026-11-01 确认"
    answer_question(export, record_index=1, qid="Q_PACK", answer=text)
    assert _load(export)[0]["question_answers"][1]["answer"] == text


# ── HTTP 层 ────────────────────────────────────────────────────────────
@pytest.fixture()
def client(export: Path):
    from CQR_Web.app import create_app

    app = create_app(export.parent)
    app.testing = True
    return app.test_client()


def test_接口_保存答案(client, export: Path):
    res = client.post("/api/answer", json={
        "path": export.name, "record": 1, "id": "Q_PACK",
        "answer": "12 本/箱", "mtime": export.stat().st_mtime,
    })
    assert res.status_code == 200, res.get_json()
    body = res.get_json()
    assert body["item"]["status"] == "confirmed"
    assert body["summary"]["open_blocking"] == 0
    assert body["gates"] == []
    assert _load(export)[0]["question_answers"][1]["answer"] == "12 本/箱"


def test_接口_路径穿越与未知文件(client):
    assert client.post("/api/answer", json={
        "path": "../../etc/passwd", "id": "Q_PACK", "answer": "x"}).status_code == 404
    assert client.post("/api/answer", json={
        "path": "没有这个文件.json", "id": "Q_PACK", "answer": "x"}).status_code == 404


def test_接口_缺id就400(client, export: Path):
    res = client.post("/api/answer", json={"path": export.name, "answer": "x"})
    assert res.status_code == 400 and "id" in res.get_json()["error"]
