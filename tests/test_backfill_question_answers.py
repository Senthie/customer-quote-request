# -*- coding: utf-8 -*-
"""v2 → v3 回填（`scripts/backfill_question_answers.py`）的回归测试。

背景：already 跑过的 47 条 v2 产物里的 `CPMS` / `CPMC` 是 agent 手写的，重跑会丢，
所以在**原文件上**补 `question_answers` + `schema_version`。这个脚本碰的是已经
交付过的历史产物，所以边界要钉死：

  1. 前 59 个 ERP 键：**键集合、顺序、值**一个都不许动（下游按位置读）；
  2. 判断「做过了」看**键在不在**，不看内容空不空 —— `question_answers: []`
     也算 v3，不许每次跑都重写一遍；
  3. CPMS 正文写了「待确认」就必须有 `field=CPMS` 的承接条目（否则回填完的
     产物立刻踩自己的 `cpms_lint` CPMS080）；
  4. `--force` 重算是「重算问题清单」，不是「抹掉页面上的答案」；
  5. 结构不符的旧文件宁可**拒绝**（报错退出），不许猜着改。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO))

import backfill_question_answers as bf  # noqa: E402
import question_answers as qa  # noqa: E402

TEMPLATE = REPO / "assets" / "add_customer_quote_request.json"
ERP_KEYS = list(json.loads(TEMPLATE.read_text(encoding="utf-8")).keys())
INTERNAL_KEYS = ["ID", "CPMC_ZH", "Reminder", "version",
                 "schema_version", "question_answers"]


def make_record(**over) -> dict:
    """凑一条「结构正确」的 v2 产品记录（59 键 + 4 个内部字段）。"""
    rec = {k: "" for k in ERP_KEYS}
    rec.update({"CPMS": "书籍方向：【长书脊】产品规格：【200*200*0】共【1】款",
                "CPMC": "Test Book", "ECPMC": "Test Book",
                "ID": "abc123", "CPMC_ZH": "测试书", "Reminder": "", "version": 2})
    rec.update(over)
    return rec


REMINDERS = [{"kind": "offer_deadline_expired", "deadline": "2025-01-22",
              "days_over": 645,
              "message": "报价截止日期 2025-01-22 已过期 645 天（基准日 2026-10-29）"}]


def fill(record: dict, *, issues=(), reminders=(), force=False, position=1,
         erp_keys=None, internal_keys=None):
    return bf.backfill_record(
        record, position,
        erp_keys=erp_keys or ERP_KEYS,
        internal_keys=internal_keys or INTERNAL_KEYS,
        issues=list(issues), reminders=list(reminders),
        asked_at="2026-10-29", force=force)


# ── 1. 形状与键序 ────────────────────────────────────────────────────────
def test_erp_keys_untouched_and_new_keys_appended_in_order():
    rec = make_record()
    new, changed, _ = fill(rec, issues=[
        {"level": "warn", "field": "ZDFS", "message": "装订方式 为空（建议填写）"}])
    assert changed
    keys = list(new)
    # 前 59 键：同序同值
    assert keys[:len(ERP_KEYS)] == ERP_KEYS
    assert [new[k] for k in ERP_KEYS] == [rec[k] for k in ERP_KEYS]
    # 内部字段按声明顺序追加，None 个不许插队
    assert keys[len(ERP_KEYS):] == INTERNAL_KEYS
    assert new["schema_version"] == qa.SCHEMA_VERSION == 3


def test_original_record_not_mutated():
    rec = make_record()
    snapshot = json.dumps(rec, ensure_ascii=False)
    fill(rec, reminders=REMINDERS)
    assert json.dumps(rec, ensure_ascii=False) == snapshot, "回填不许就地改入参"


def test_empty_internal_key_set_still_works():
    """声明列表为空（旧 yaml / --strip-internal 时代）也要把两个新键写出来。"""
    rec = make_record()
    new, changed, _ = fill(rec, erp_keys=ERP_KEYS, internal_keys=[])
    assert changed
    assert list(new)[:len(ERP_KEYS)] == ERP_KEYS
    assert new["schema_version"] == 3 and "question_answers" in new
    # 原来就有的内部字段不许丢
    assert new["ID"] == rec["ID"] and new["version"] == 2


# ── 2. 幂等 ──────────────────────────────────────────────────────────────
def test_second_run_is_noop_for_nonempty_items():
    rec, _, _ = fill(make_record(), issues=[
        {"level": "warn", "field": "ZDFS", "message": "装订方式 为空"}])
    again, changed, note = fill(rec)
    assert not changed and again is rec
    assert "已是 v3" in note


def test_second_run_is_noop_for_empty_item_list():
    """零待确认项的记录（question_answers: []）也算做完 —— 不重写、不报假改动。"""
    rec, changed, _ = fill(make_record())
    assert changed and rec["question_answers"] == []
    again, changed2, note = fill(rec)
    assert not changed2 and "已是 v3" in note


# ── 3. CPMS 正文的「待确认」要有承接条目（CPMS080）─────────────────────
def test_cpms_pending_line_becomes_item():
    cpms = ("书籍方向：【长书脊】产品规格：【200*200*0】共【1】款\n\n"
            "装订：普通BB书\n装箱：待确认（原单未给每箱数量）")
    new, _, _ = fill(make_record(CPMS=cpms))
    items = new["question_answers"]
    cpms_items = [i for i in items if i["field"] == "CPMS"]
    assert len(cpms_items) == 1
    item = cpms_items[0]
    assert item["id"] == "Q_CPMS"
    assert "待确认（原单未给每箱数量）" in item["question"]
    # 业务补填、不猜 → 不卡交付；出处是 agent（CPMS 是它写的）
    assert item["blocking"] is False and item["source"] == "agent"
    assert item["status"] == "open" and item["answer"] == ""


def test_cpms_clean_text_adds_nothing():
    new, _, _ = fill(make_record())          # CPMS 里没有待办词
    assert new["question_answers"] == []


def test_multiple_pending_lines_get_distinct_ids():
    cpms = "装箱：待确认\n用料：TBD"
    new, _, _ = fill(make_record(CPMS=cpms))
    ids = [i["id"] for i in new["question_answers"]]
    assert ids == ["Q_CPMS", "Q_CPMS_2"]


def test_existing_cpms_carrier_not_duplicated():
    cpms = "装箱：待确认"
    old = qa.normalize([qa.make_item("Q_CPMS", "CPMS", "旧问题", source="agent")])
    # 判据：已有未关闭的 field=CPMS 就不再造（cpms_lint 只要求「至少一条」）
    assert bf.build_from_cpms(cpms, today="2026-10-29", existing=old) == []
    assert bf.build_from_cpms(cpms, today="2026-10-29") != []
    # 已确认的那条不算承接（答完了就该能再提新的）
    closed = qa.normalize([qa.make_item("Q_CPMS", "CPMS", "旧问题",
                                        answer="12 本", status="confirmed")])
    assert bf.build_from_cpms(cpms, today="2026-10-29", existing=closed) != []


def test_cpms_long_line_is_truncated():
    new, _, _ = fill(make_record(CPMS="备註：" + "很长的说明" * 40 + "待确认"))
    item = new["question_answers"][0]
    assert item["question"].endswith("…")
    assert len(item["question"]) <= len("CPMS 待确认：") + 118


# ── 4. 字段级 warning / 报价截止提醒 ────────────────────────────────────
def test_field_warnings_become_items_but_group_notes_do_not():
    issues = [{"level": "warn", "field": "ZDFS", "message": "装订方式 为空"},
              {"level": "warn", "field": "分组1", "message": "本组由 3 个条目合并"},
              {"level": "warn", "field": "不存在的字段", "message": "x"}]
    new, _, _ = fill(make_record(), issues=issues)
    ids = [i["id"] for i in new["question_answers"]]
    assert ids == ["Q_ZDFS"], "分组说明 / 非法字段不该进确认清单"


def test_error_level_blocks_delivery():
    new, _, _ = fill(make_record(), issues=[
        {"level": "error", "field": "CPSL1", "message": "数量缺失"}])
    item = new["question_answers"][0]
    assert item["blocking"] is True


def test_warning_for_other_record_is_skipped():
    issues = [{"level": "warn", "field": "ZDFS", "message": "装订方式 为空",
               "record": 2}]
    new, _, _ = fill(make_record(), issues=issues, position=1)
    assert new["question_answers"] == []
    new2, _, _ = fill(make_record(), issues=issues, position=2)
    assert [i["id"] for i in new2["question_answers"]] == ["Q_ZDFS"]


def test_deadline_reminder_becomes_blocking_item_and_reminder_snapshot():
    new, _, _ = fill(make_record(), reminders=REMINDERS)
    items = new["question_answers"]
    assert [i["id"] for i in items] == ["Q_OFFER_DEADLINE"]
    assert items[0]["blocking"] is True
    # Reminder 降级为「卡交付未确认项」的文本快照，和条目一字不差
    assert new["Reminder"] == items[0]["question"]


def test_reminder_is_cleared_when_nothing_blocks():
    rec = make_record(Reminder="旧的手写提醒")
    new, _, _ = fill(rec)                      # 没有任何 blocking 项
    assert new["Reminder"] == ""


def test_asked_at_comes_from_erp_filename():
    p = Path("/x/ERP_abc123_20261029_100816.json")
    assert bf.asked_at_for(p) == "2026-10-29"
    assert bf.asked_at_for(Path("/x/whatever.json"))  # 没时间戳 → 当天，不崩


# ── 5. 拒绝改结构不符的文件 ─────────────────────────────────────────────
def test_refuses_record_with_wrong_erp_keys():
    rec = make_record()
    rec.pop("BDBH")
    rec["SJFX"] = "x"
    with pytest.raises(ValueError, match="与 ERP 模板不一致"):
        fill(rec)


def test_unknown_internal_key_is_preserved_not_dropped():
    rec = make_record()
    rec["手写备注"] = "别丢"
    new, _, _ = fill(rec)
    assert new["手写备注"] == "别丢"
    assert list(new)[-1] == "手写备注"


def test_missing_internal_keys_are_filled_not_invented():
    """老产物没有 version / CPMC_ZH 时：不凭空造值，只补该补的两个键。"""
    rec = make_record()
    rec.pop("CPMC_ZH")
    rec.pop("version")
    new, _, _ = fill(rec)
    assert "CPMC_ZH" not in new and "version" not in new
    assert new["schema_version"] == 3 and "question_answers" in new


# ── 6. --force 保留答案 ─────────────────────────────────────────────────
def test_force_preserves_answers_by_id():
    answered = qa.make_item("Q_ZDFS", "ZDFS", "装订方式 为空",
                            answer="无线胶装书", status="confirmed",
                            answered_at="2026-10-30")
    answered["blocking"] = False
    rec = make_record(question_answers=[answered], schema_version=3)
    issues = [{"level": "warn", "field": "ZDFS", "message": "装订方式 为空"},
              {"level": "warn", "field": "CPSL1", "message": "数量待确认"}]
    new, changed, _ = fill(rec, issues=issues, force=True)
    assert changed
    by_id = {i["id"]: i for i in new["question_answers"]}
    assert by_id["Q_ZDFS"]["answer"] == "无线胶装书"
    assert by_id["Q_ZDFS"]["status"] == "confirmed"
    assert by_id["Q_ZDFS"]["answered_at"] == "2026-10-30"
    assert "Q_CPSL1" in by_id, "force 也要把新出现的问题补进来"


# ── 7. 文档级：形状 / 写盘 ──────────────────────────────────────────────
def test_document_keeps_list_shape_and_counts_changes():
    data = [make_record(CPMC="A"), make_record(CPMC="B")]
    new, notes, changed = bf.backfill_document(
        data, erp_keys=ERP_KEYS, internal_keys=INTERNAL_KEYS,
        issues=[], reminders=REMINDERS, asked_at="2026-10-29")
    assert isinstance(new, list) and len(new) == 2
    assert changed == 2 and len(notes) == 2
    assert all(r["schema_version"] == 3 for r in new)
    # 文件级提醒复制到每条记录（记录要自包含）
    assert all(r["question_answers"][0]["id"] == "Q_OFFER_DEADLINE" for r in new)


def test_document_keeps_dict_shape():
    single = make_record()
    new, _, changed = bf.backfill_document(
        single, erp_keys=ERP_KEYS, internal_keys=INTERNAL_KEYS,
        issues=[], reminders=[], asked_at="2026-10-29")
    assert isinstance(new, dict) and changed == 1


def test_write_is_atomic_and_keeps_trailing_newline(tmp_path):
    target = tmp_path / "ERP_abc_20261029_100816.json"
    target.write_text('[{"a": 1}]\n', encoding="utf-8")
    bf.write_atomic(target, bf.dump_like_original(target, [{"a": 1}]))
    text = target.read_text(encoding="utf-8")
    assert text.endswith("]\n")
    assert not (tmp_path / "ERP_abc_20261029_100816.json.tmp").exists()
    assert json.loads(text) == [{"a": 1}]


def test_write_keeps_no_trailing_newline_when_original_had_none(tmp_path):
    target = tmp_path / "x.json"
    target.write_text('{"a": 1}', encoding="utf-8")
    bf.write_atomic(target, bf.dump_like_original(target, {"a": 1}))
    assert not target.read_text(encoding="utf-8").endswith("\n")


def test_find_validation_by_stem(tmp_path):
    erp = tmp_path / "ERP_abc123_20261029_100816.json"
    erp.write_text("[]", encoding="utf-8")
    assert bf.validation_for(erp) is None
    val = tmp_path / "Some Title.validation.json"
    val.write_text("{}", encoding="utf-8")
    assert bf.validation_for(erp) == val


def test_erp_files_skips_single_object_backup(tmp_path):
    (tmp_path / "ERP_abc_20261029_100816.json").write_text("[]", encoding="utf-8")
    (tmp_path / "ERP_abc_20261029_100816.json.prearray.bak").write_text("{}")
    names = [p.name for p in bf.erp_files(tmp_path)]
    assert names == ["ERP_abc_20261029_100816.json"]
