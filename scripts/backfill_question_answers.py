#!/usr/bin/env python3
"""backfill_question_answers.py —— 把既有 v2 产物**就地**升到 schema v3

为什么是「回填」而不是「重跑」
------------------------------
v2 产物里的 `CPMS` / `CPMC` / `ECPMC` 是 agent 通读客户原文后**手写**的，
重跑一遍引擎只会给出邮件级品类名，手写正文全丢。回填只**补**一样东西：
每条产品记录的 `question_answers`（+ `schema_version`），其余键一个字节不动。

数据从哪来（不重新抽取，不联网）
--------------------------------
同目录的 `<stem>.validation.json`（跑管道时已经落盘）：
  * `warnings` + `errors` → 字段级待确认项（走 `question_answers.build_from_issues`）
  * `reminders`           → `Q_OFFER_DEADLINE`（走 `build_for_record`）
判据不在这里重写 —— 全部复用 `scripts/question_answers.py`，与 main.py 同源。

`asked_at` 取**产物文件名里的运行时刻**（`ERP_<task>_YYYYMMDD_HHMMSS.json`），
不是回填当天：问题当初就是那次跑出来的，把今天写成 ask 日期会伪造时间线。

安全边界
--------
1. 已有**非空** `question_answers` 的记录跳过 —— 页面上写过的答案不能被脚本冲掉
   （要覆盖得显式 `--force`）。
2. 前 59 个 ERP 键：**键集合 + 顺序**逐个核对模板，不一致直接中止（下游按位置读）。
3. 内部字段按键序重建：`ID / CPMC_ZH / Reminder / version / schema_version /
   question_answers`（唯一真源 `assets/internal_export_fields.yaml`），
   缺的补、已有的留。
4. `Reminder` 按 v3 口径重算 = 卡交付未确认项的文本快照（权威来源是 question_answers）。
5. 原子写（同目录 `.tmp` + `os.replace`），保留原文件的缩进 / 中文 / 结尾换行 / 顶层形状
   （数组仍是数组，单对象仍是对象 —— 不趁机改结构）。

用法
----
    uv run ./scripts/backfill_question_answers.py <task_id>          # 按目录名
    uv run ./scripts/backfill_question_answers.py <ERP.json> ...     # 按文件
    uv run ./scripts/backfill_question_answers.py --all              # 全部在用任务
    uv run ./scripts/backfill_question_answers.py <task_id> --dry-run
    uv run ./scripts/backfill_question_answers.py <task_id> --force
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from internal_fields import internal_field_keys  # noqa: E402
from question_answers import (QUESTIONS_KEY, SCHEMA_VERSION,  # noqa: E402
                              build_for_record, effective_status,
                              make_item, normalize, open_items,
                              reminder_text, summary)

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = REPO_ROOT / "assets" / "add_customer_quote_request.json"
PROCESSED_DIR = REPO_ROOT / "customer-files" / "processed"
VALIDATION_SUFFIX = ".validation.json"

# 与 cpms_lint 的 CPMS080 用同一组待办词：正文里出现就得有条目承接
_PENDING_RE = re.compile(r"待确认|待定|待补|TBD|待核实")

# ERP_<task_id>_YYYYmmdd_HHMMSS.json —— 运行时刻就是问题的提出时刻
_STAMP_RE = re.compile(r"_(\d{8})_(\d{6})\.json$")


# ── 找文件 ──────────────────────────────────────────────────────────────
def erp_files(target: Path) -> list[Path]:
    """把「目录 / 任务号 / 文件」统一成 ERP_*.json 列表。"""
    if target.is_file():
        return [target]
    out = [p for p in sorted(target.glob("ERP_*.json"))
           # 单对象时代留下的备份（.prearray.bak）不是产物
           if not p.name.endswith(".bak")]
    return out


def resolve_targets(args: Iterable[str], use_all: bool) -> list[Path]:
    paths: list[Path] = []
    if use_all:
        for task in sorted(p for p in PROCESSED_DIR.iterdir() if p.is_dir()):
            if task.name.startswith("_"):        # _superseded_* / _dedupe_* 不动
                continue
            paths.extend(erp_files(task))
        return paths
    for arg in args:
        p = Path(arg)
        if not p.exists():
            p2 = PROCESSED_DIR / arg               # 允许只给 task_id
            if p2.exists():
                p = p2
            else:
                raise SystemExit(f"找不到目标: {arg}")
        paths.extend(erp_files(p))
    return paths


def validation_for(erp_path: Path) -> Optional[Path]:
    """同目录、同 stem 的 validation.json（stem 去掉 ERP_<task>_<stamp>）。"""
    m = re.match(r"^(ERP_[0-9a-f]+)_\d{8}_\d{6}$", erp_path.stem)
    stem = m.group(1) if m else erp_path.stem
    for cand in sorted(erp_path.parent.glob("*" + VALIDATION_SUFFIX)):
        if cand.name.startswith(stem):
            return cand
    found = sorted(erp_path.parent.glob("*" + VALIDATION_SUFFIX))
    return found[0] if len(found) == 1 else None


def load_validation(erp_path: Path) -> dict[str, Any]:
    path = validation_for(erp_path)
    if not path:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def asked_at_for(erp_path: Path) -> str:
    """产物文件名里的运行日期；没有时间戳就用当天。"""
    m = _STAMP_RE.search(erp_path.name)
    if m:
        return datetime.strptime(m.group(1), "%Y%m%d").strftime("%Y-%m-%d")
    return datetime.now().strftime("%Y-%m-%d")


# ── CPMS 正文里的「待确认」→ 条目 ────────────────────────────────────────
def build_from_cpms(cpms: str, *, today: str,
                    existing: Iterable[dict] = ()) -> list[dict[str, Any]]:
    """CPMS 正文写了「待确认/待定/TBD」的，各折成一条 field=CPMS 的条目。

    为什么回填也要做这一步：`cpms_lint` 的 **CPMS080** 规定「正文出现待办词就必须
    有 field=CPMS 的未关闭项」。v2 产物里这些待办只写在正文里；只补校验项的话，
    回填完的产物会自己踩自己的 lint。

    语气与闸门：`blocking=False`（业务补填、不猜 —— 与 ZDFS 一致）。
    `source="agent"`：这些话是 agent 写 CPMS 时留下的，不是管道判出来的。
    已有 field=CPMS 的未关闭项时不再重复造（`cpms_lint` 只要求「至少一条」）。
    """
    if not cpms:
        return []
    has_carrier = any(str(q.get("field") or "").strip().upper() == "CPMS"
                      and effective_status(q) == "open"
                      for q in existing)
    if has_carrier:
        return []

    out: list[dict[str, Any]] = []
    for line in str(cpms).splitlines():
        text = line.strip()
        if not text or not _PENDING_RE.search(text):
            continue
        if len(text) > 120:                     # 正文一行可能很长，截断保可读
            text = text[:117].rstrip() + "…"
        qid = "Q_CPMS" if not out else f"Q_CPMS_{len(out) + 1}"
        out.append(make_item(qid, "CPMS", f"CPMS 待确认：{text}",
                             blocking=False, source="agent", asked_at=today))
    return out


# ── 回填一条记录 ────────────────────────────────────────────────────────
def backfill_record(record: dict, position: int, *, erp_keys: list[str],
                    internal_keys: list[str], issues: list[dict],
                    reminders: list[dict], asked_at: str,
                    force: bool = False) -> tuple[dict, bool, str]:
    """返回 (新记录, 是否有改动, 说明)。不改正文，只补 question_answers。"""
    keys = list(record)
    erp_part = keys[:len(erp_keys)]
    if erp_part != erp_keys:
        missing = [k for k in erp_keys if k not in erp_part]
        extra = [k for k in erp_part if k not in erp_keys]
        raise ValueError(f"第 {position} 条记录与 ERP 模板不一致："
                         f"缺失={missing} 多余={extra}（拒绝改动）")

    # 判断「是否已回填」看**键在不在**，不看内容空不空：
    #   已经升到 v3 但恰好零待确认项的记录（question_answers: []）也算完成，
    #   否则每次跑都会把它重写一遍、报一条假改动。
    existing = normalize(record.get(QUESTIONS_KEY))
    if QUESTIONS_KEY in record and not force:
        return record, False, (f"已是 v3（{len(existing)} 项，跳过；"
                              f"--force 才重算）")

    items = build_for_record(
        position,
        issues=issues,
        reminders=reminders,
        record_fields=set(erp_keys),
        today=asked_at,
    )
    # CPMS 正文里遗留的「待确认」也要有承接条目（否则回填完就踩 CPMS080）
    items += build_from_cpms(str(record.get("CPMS") or ""),
                             today=asked_at, existing=items)

    # --force 重算时，把**同 id 的旧答案**搬过来 —— force 的用途是重算问题清单
    # （补上新判据、清掉笔误），不是「顺手把页面上的答案抹掉」。
    if force and existing:
        by_id = {q["id"]: q for q in existing}
        for it in items:
            old = by_id.get(it["id"])
            if not old:
                continue
            for key in ("answer", "status", "answered_at"):
                if old.get(key):
                    it[key] = old[key]

    computed: dict[str, Any] = {
        QUESTIONS_KEY: items,
        "schema_version": SCHEMA_VERSION,
        # v3 口径：Reminder 是「卡交付未确认项」的文本快照
        "Reminder": reminder_text(items),
    }
    new: dict[str, Any] = dict.fromkeys(erp_keys)
    for k in erp_keys:
        new[k] = record[k]
    for k in internal_keys:
        if k in computed:
            new[k] = computed[k]
        elif k in record:
            new[k] = record[k]
    # 声明里没有、记录里却有的内部键：原样拖到末尾（不静默丢数据）
    for k in keys[len(erp_part):]:
        if k not in new:
            new[k] = record[k]
    # 兜底：声明的 internal_keys 万一漏了 computed 里的键（旧版 yaml / 调用方传空），
    # 补在最后 —— 回填的**唯一目的**就是写出这两样，不能因为声明不全就白跑一次。
    for k in (QUESTIONS_KEY, "schema_version"):
        if k in computed and k not in new:
            new[k] = computed[k]

    s = summary(items)
    note = (f"补 {s['total']} 项（未确认 {s['open']}"
            f"，卡交付 {s['open_blocking']}）")
    return new, True, note


def backfill_document(data: Any, *, erp_keys: list[str],
                      internal_keys: list[str], issues: list[dict],
                      reminders: list[dict], asked_at: str,
                      force: bool = False) -> tuple[Any, list[str], int]:
    """保持顶层形状（list 还是 dict），逐条回填。返回 (新文档, 说明, 改动条数)。"""
    is_list = isinstance(data, list)
    records = data if is_list else [data]
    notes: list[str] = []
    changed = 0
    out: list[dict] = []
    for idx, rec in enumerate(records, start=1):
        if not isinstance(rec, dict):
            out.append(rec)
            continue
        new, did, note = backfill_record(
            rec, idx, erp_keys=erp_keys, internal_keys=internal_keys,
            issues=issues, reminders=reminders, asked_at=asked_at, force=force)
        out.append(new)
        changed += int(did)
        notes.append(f"[{idx}] {note}")
    return (out if is_list else out[0]), notes, changed


def write_atomic(path: Path, text: str) -> None:
    """同目录临时文件 + os.replace：断电/并发下不会留下半个 JSON。"""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def dump_like_original(path: Path, data: Any) -> str:
    """缩进 2 / 中文不转义；结尾换行跟随原文件（不制造无意义的 diff）。"""
    body = json.dumps(data, ensure_ascii=False, indent=2)
    try:
        original = path.read_text(encoding="utf-8")
    except OSError:
        original = ""
    return body + ("\n" if original.endswith("\n") else "")


# ── CLI ─────────────────────────────────────────────────────────────────
def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="回填 question_answers（v2 → schema v3），正文不动")
    ap.add_argument("targets", nargs="*", help="ERP_*.json / 任务目录 / task_id")
    ap.add_argument("--all", action="store_true",
                    help="扫 customer-files/processed 下所有在用任务（跳过 _superseded_*）")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写盘")
    ap.add_argument("--force", action="store_true",
                    help="覆盖已有非空 question_answers（默认跳过，保护人工答案）")
    args = ap.parse_args(argv)

    if not args.targets and not args.all:
        ap.error("给路径或 --all")

    erp_keys = list(json.loads(TEMPLATE_PATH.read_text(encoding="utf-8")).keys())
    internal_keys = internal_field_keys()
    targets = resolve_targets(args.targets, args.all)

    total_files = total_records = 0
    for erp in targets:
        total_files += 1
        try:
            raw = json.loads(erp.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"✗ 读不了 {erp}: {exc}")
            continue
        val = load_validation(erp)
        issues = [*(val.get("errors") or []), *(val.get("warnings") or [])]
        reminders = list(val.get("reminders") or [])
        asked_at = asked_at_for(erp)
        try:
            new_data, notes, changed = backfill_document(
                raw, erp_keys=erp_keys, internal_keys=internal_keys,
                issues=issues, reminders=reminders, asked_at=asked_at,
                force=args.force)
        except ValueError as exc:
            print(f"✗ {erp.relative_to(REPO_ROOT)}: {exc}")
            continue
        rel = erp.relative_to(REPO_ROOT)
        if not changed:
            print(f"· {rel}")
            for n in notes:
                print(f"    {n}")
            continue
        total_records += changed
        if not args.dry_run:
            write_atomic(erp, dump_like_original(erp, new_data))
        print(f"{'~' if args.dry_run else '✓'} {rel}  (asked_at={asked_at})")
        for n in notes:
            print(f"    {n}")

    verb = "将改动" if args.dry_run else "已改动"
    print(f"\n{verb} {total_files} 个文件 / {total_records} 条记录")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
