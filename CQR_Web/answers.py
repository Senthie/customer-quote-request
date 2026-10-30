"""页面里直接写答案 —— 本服务**唯一**会改产物内容的入口。

业务 2026-10-30（森西）：「在展示 web 上加入 question_answers 字段，新增一个回答的
文本框，这样我们可以在页面上直接编写答案」。

边界（刻意收窄，别扩）
----------------------
* **只改 `question_answers`**：`answer` / `status` / `answered_at`（顺带把 `Reminder`
  这个文本快照重算一遍）。CPMS、品名、59 个 ERP 字段页面**一律不许写** ——
  那些是 agent 通读原文后手写的，页面上没有编辑部。
* **只改产物文件本身**，不起进程、不调脚本（`tests/test_cqr_web_boundary.py`
  那条「Web 侧不许自己跑抽取」仍然有效）。
* `path` 仍走 `data.resolve_export()`（resolve 后前缀校验，出不了扫描根）。
* 写入是**原子**的（同目录临时文件 + `os.replace`），缩进/中文/键序与 `main.py`
  产出的完全一致，下游读到的东西和「脚本跑出来的」没有区别。
* 带 `expected_mtime` 乐观锁：页面加载后文件被别人（agent / 另一个人）改过 →
  拒绝写入并让人刷新，避免把别人刚写的 CPMS 覆盖掉。
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# 复用 scripts/ 下的权威实现（与 data.py 同一份 sys.path 注入）
from .data import REPO_ROOT  # noqa: F401  （导入即完成 scripts/ 的 sys.path 注入）

import question_answers as qa  # noqa: E402  —— 在 scripts/ 里


class AnswerError(Exception):
    """写答案失败。`status` = 建议的 HTTP 状态码。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _split(raw: Any) -> tuple[list[dict[str, Any]], str]:
    """把文件内容拆成「记录列表 + 容器形状」，写回时照原样还原。

    形状只有三种（与 `erp_bundle` 一致）：数组 / `{records: [...]}` / 单个对象。
    """
    if isinstance(raw, list):
        return raw, "list"
    if isinstance(raw, dict) and isinstance(raw.get("records"), list):
        return raw["records"], "records"
    if isinstance(raw, dict):
        return [raw], "single"
    raise AnswerError(f"这不像一份 ERP 产物（顶层是 {type(raw).__name__}）", 422)


def _dump(path: Path, payload: Any) -> None:
    """原子写回：同目录临时文件 → os.replace，缩进 2、中文不转义（与 main.py 一致）。"""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".answer_", suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def answer_question(path: Path | str, *,
                    record_index: int,
                    qid: str,
                    answer: str = "",
                    status: Optional[str] = None,
                    expected_mtime: Optional[float] = None,
                    today: Optional[str] = None) -> dict[str, Any]:
    """写一条确认清单的答案。返回 `{item, summary, gates, reminder, mtime}`。

    `status` 不给就按内容推：有 `answer` → `confirmed`（并盖 `answered_at`），
    空 `answer` → 回到 `open`（`answered_at` 清空）。
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise AnswerError(f"文件不存在: {file_path}", 404)

    if expected_mtime is not None:
        try:
            current = file_path.stat().st_mtime
        except OSError as exc:
            raise AnswerError(f"读不到文件状态: {exc}", 500) from exc
        if abs(float(expected_mtime) - current) > 0.001:
            raise AnswerError("这份产物刚被别处改过（可能 agent 刚补了 CPMS 或别人保存了答案）"
                              "—— 刷新页面再看一眼，别覆盖别人的改动", 409)

    try:
        raw = json.loads(file_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AnswerError(f"读不了这份产物: {type(exc).__name__}: {exc}", 422) from exc

    records, shape = _split(raw)
    if not 1 <= int(record_index) <= len(records):
        raise AnswerError(f"第 {record_index} 条记录不存在（这份产物共 {len(records)} 条）", 404)
    record = records[int(record_index) - 1]

    if record.get(qa.QUESTIONS_KEY) is None:
        raise AnswerError("这份产物是 v2 格式（没有 question_answers 字段）—— "
                          "旧产物不回头改；需要清单请重跑这条询价生成 v3 产物", 409)

    items = qa.normalize(record.get(qa.QUESTIONS_KEY))
    target = next((i for i in items if str(i.get("id")) == str(qid)), None)
    if target is None:
        known = ", ".join(str(i.get("id")) for i in items) or "（空清单）"
        raise AnswerError(f"没有 id={qid!r} 的确认项（这份记录现有: {known}）", 404)

    text = str(answer or "").strip()
    stamp = today or _today()

    wanted = (status or "").strip().lower()
    if wanted and wanted not in qa.STATUSES:
        # 允许别名（done / n/a 这类），但明确乱写就报错，别静默改成 open
        alias = qa._STATUS_ALIASES.get(wanted)          # noqa: SLF001 —— 同一模块族
        if alias is None:
            raise AnswerError(f"status 只能是 {'/'.join(qa.STATUSES)}，收到 {status!r}", 400)
        wanted = alias
    if not wanted:
        wanted = qa.CONFIRMED if text else qa.OPEN

    target["answer"] = text
    target["status"] = wanted
    if wanted == qa.CONFIRMED:
        if not text:
            raise AnswerError("标记已确认但答案为空 —— 要么写答案，要么把它标成作废（waived）", 400)
        # 首次确认盖日期；改答案也更新（业务看的是「最后一次确认」）
        target["answered_at"] = stamp
    elif wanted == qa.WAIVED:
        target["answered_at"] = target.get("answered_at")
    else:
        target["answered_at"] = None

    # 写回固定 9 键顺序（normalize 已保证）+ 重算 Reminder 快照
    record[qa.QUESTIONS_KEY] = items
    if "Reminder" in record:
        record["Reminder"] = qa.reminder_text(items)

    _dump(file_path, raw)

    return {
        "path": str(file_path),
        "record": int(record_index),
        "item": target,
        "summary": qa.summary(items),
        "gates": qa.gates(items),
        "reminder": record.get("Reminder"),
        "mtime": file_path.stat().st_mtime,
        "shape": shape,
    }
