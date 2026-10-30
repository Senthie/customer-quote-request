#!/usr/bin/env python3
"""question_answers.py —— 产物 schema v3 的「内部确认清单」

业务口径（森西 2026-10-30 拍板，5 条问题的答案）
------------------------------------------------
1. 位置：**每条产品记录内**（不是文件级）—— 问题基本都绑定在某个产品上
   （这条 ZDFS 没定、这条装箱没给、这条 CPMS 措辞待确认），放文件级又要
   加 `record_index` 把层级塞回去。
2. 语义：**内部确认清单**（我方 ↔ 业务/客户确认过什么、还欠什么），不是
   「发给客户的问卷」。
3. ERP 导入端**自己会过滤未知字段** → 本字段照常跟着产物走，不需要
   `--strip-internal` 才安全（但 `--strip-internal` 仍会剥掉它，见下）。
4. `version` 继续表示**客户档案口径版本**（`customers/_defaults.yaml`），
   格式另用 `schema_version: 3` 标（B 方案）。
5. 以前散在四处的「还没确定的事」**合并到这一处**：
   * `Reminder`（报价截止过期）→ 一条 `Q_OFFER_DEADLINE`（`Reminder` 字段保留，
     降级为**运行时的文本快照**，权威来源是 question_answers）
   * validation `warnings` 里**属于某个 ERP 字段**的项（ZDFS 空、枚举待确认…）
     → 一条 open 项（field 指向该字段）
   * CPMS 里写的「待确认 / 待定 / TBD」→ 由 `cpms_lint` 的 CPMS080 强制要求
     有对应条目，不能只在正文里说一句就算完
   * agent 自己发现、需要业务/客户确认的事实 → `source` 标真出处

条目契约（固定 9 键，顺序敏感；不允许拿问题文本当 key）
---------------------------------------------------------
    id           稳定短码，重跑不变（`Q_ZDFS` / `Q_OFFER_DEADLINE` / `Q_GROUP_2`）
    field        答完要回填的字段（`ZDFS` / `Drop_SJFX` / `CPMS` …）
    question     要确认什么（一句话，含客户原文关键词）
    answer       确认结果；空 = 还没确认
    status       open / confirmed / waived
    blocking     未确认时是否**卡交付**（`delivery_gates` 只看 blocking 的 open 项）
    source       pipeline / agent / enquiry / customer / business
    asked_at     提出日期 YYYY-MM-DD
    answered_at  确认日期 YYYY-MM-DD；未确认 = null

判据
----
* `effective_status()`：`status=open` 但 `answer` 非空 → 视作 `confirmed`
  （手写的人常只填 answer，忘了翻 status，不能因此一直卡着）
* `gates()`：只拦 `blocking=True` 且仍 open 的项 —— 业务明确「由业务补填、
  不猜」的字段（如 2026-10-21 拍板的 `ZDFS` 线路）用 `blocking=False`，
  看得见但不堵路
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

SCHEMA_VERSION = 3
QUESTIONS_KEY = "question_answers"

# 条目固定键（顺序 = 产物里的顺序，测试钉着）
ITEM_KEYS = ("id", "field", "question", "answer", "status",
             "blocking", "source", "asked_at", "answered_at")

STATUSES = ("open", "confirmed", "waived")
OPEN, CONFIRMED, WAIVED = STATUSES
SOURCES = ("pipeline", "agent", "enquiry", "customer", "business")

# 手写时常见的状态别名 —— 静默纠正，不要因为写了个 "done" 就判成 open
_STATUS_ALIASES = {
    "done": CONFIRMED, "ok": CONFIRMED, "resolved": CONFIRMED,
    "closed": CONFIRMED, "confirm": CONFIRMED, "answered": CONFIRMED,
    "skip": WAIVED, "ignored": WAIVED, "defer": WAIVED,
    "deferred": WAIVED, "na": WAIVED, "n/a": WAIVED, "作废": WAIVED,
}

_GROUP_FIELD_RE = re.compile(r"^分组(\d+)$")
_SLUG_RE = re.compile(r"[^A-Za-z0-9]+")

# 这些 issue 不是「某个字段待确认」，是解释性说明 → 不进确认清单
#   （分组说明、纯名称写法提示之类的技术备注仍留在 validation.json 里）
_NON_QUESTION_FIELDS = {"分组", "unit", "conflicts"}


def _slug(text: Any) -> str:
    s = _SLUG_RE.sub("_", str(text or "")).strip("_").upper()
    return s or "ITEM"


def _field_slug(field: Any) -> str:
    raw = str(field or "").strip()
    m = _GROUP_FIELD_RE.match(raw)
    if m:
        return f"GROUP_{m.group(1)}"
    return _slug(raw)


def _flag(value: Any, default: bool = False) -> bool:
    """容忍 true/"true"/1/"yes"/"是" —— 手改 JSON 的人不会都写对。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("true", "yes", "y", "1", "是", "必填", "blocking"):
        return True
    if text in ("false", "no", "n", "0", "否", "不卡", ""):
        return False
    return default


def make_item(qid: str, field: str, question: str, *,
              answer: str = "", status: str = OPEN, blocking: bool = False,
              source: str = "pipeline", asked_at: Optional[str] = None,
              answered_at: Optional[str] = None) -> dict[str, Any]:
    return {
        "id": qid,
        "field": str(field or ""),
        "question": str(question or "").strip(),
        "answer": str(answer or ""),
        "status": status if status in STATUSES else OPEN,
        "blocking": bool(blocking),
        "source": source if source in SOURCES else "agent",
        "asked_at": asked_at,
        "answered_at": answered_at,
    }


# ── 从管道输入生成 ──────────────────────────────────────────────────────
def build_from_issues(issues: Iterable[dict], *,
                      record_fields: Optional[set[str]] = None,
                      record_index: int = 1,
                      today: Optional[str] = None) -> list[dict[str, Any]]:
    """把校验报告里**属于某个字段**的项折成确认条目。

    只收 `field` 是真实 ERP 字段（或 agent 必填字段）的 issue：
      * 分组说明、unit 说明这类解释性文字**不进清单**（它们不是待确认项，
        留在 validation.json 里给技术看）
      * `record` 标号不属于本条记录的跳过（多条记录时 main.py 会打标号）
    """
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for issue in issues or []:
        if not isinstance(issue, dict):
            continue
        field = str(issue.get("field") or "").strip()
        if not field or field in _NON_QUESTION_FIELDS:
            continue
        if record_fields is not None and field not in record_fields:
            continue                      # 不是字段级问题（分组说明等）
        rec = issue.get("record")
        if rec not in (None, "", record_index, str(record_index)):
            continue
        message = str(issue.get("message") or "").strip()
        key = (field, message)
        if key in seen:
            continue
        seen.add(key)
        qid = f"Q_{_field_slug(field)}"
        taken = {i["id"] for i in out}
        if qid in taken:                  # 同一字段多条 issue → 加序号，保持稳定
            n = 2
            while f"{qid}_{n}" in taken:
                n += 1
            qid = f"{qid}_{n}"
        # error 级 = 现在就不能交付；warn 级 = 看得见但要业务判断，先不堵路
        blocking = str(issue.get("level") or "warn").lower() == "error"
        out.append(make_item(qid, field, message, blocking=blocking,
                             source="pipeline", asked_at=today))
    return out


def build_from_reminders(reminders: Iterable[dict], *,
                         today: Optional[str] = None) -> list[dict[str, Any]]:
    """`Reminder`（报价截止过期等）折成 `Q_OFFER_DEADLINE` —— 卡交付。"""
    out: list[dict[str, Any]] = []
    for r in reminders or []:
        if not isinstance(r, dict):
            continue
        message = str(r.get("message") or "").strip()
        if not message:
            continue
        kind = str(r.get("kind") or "reminder")
        qid = "Q_OFFER_DEADLINE" if kind.startswith("offer_deadline") else f"Q_{_slug(kind)}"
        out.append(make_item(qid, "Reminder", message, blocking=True,
                             source="pipeline", asked_at=today))
    return out


def build_for_record(record_index: int, *,
                     issues: Iterable[dict],
                     reminders: Iterable[dict] = (),
                     record_fields: Optional[set[str]] = None,
                     today: Optional[str] = None) -> list[dict[str, Any]]:
    """一条产品记录的确认清单 = 字段级校验项 + 文件级提醒（复制到每条）。

    文件级提醒（报价截止）对整条询价都成立，所以每条记录都带一条同样的
    `Q_OFFER_DEADLINE`（决策是「放在记录内」—— 记录要自包含，
    第 3 条的 Tab 里不该看不到「这单已过期」）。
    """
    items = build_from_reminders(reminders, today=today)
    items += build_from_issues(issues, record_fields=record_fields,
                               record_index=record_index, today=today)
    return normalize(items)


# ── 读入 / 归一化 ───────────────────────────────────────────────────────
def normalize(raw: Any) -> list[dict[str, Any]]:
    """把产物里的 `question_answers` 归一成固定键（兼容手写/旧产物/脏数据）。

    * `None` / 缺失 → `[]`（v2 旧产物不批量迁移）
    * 允许 `{q: a}` 这种"问题当 key"的写法被读进来（历史上想这么写），
      转成 `id` 由问题文本 hash 化的条目 —— 但**写出时一律用固定键**
    * 未知的额外键保留在末尾，不静默丢数据
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        return [make_item("Q_NOTE", "", text, source="agent")] if text else []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []

    out: list[dict[str, Any]] = []
    used: set[str] = set()
    for entry in raw:
        if isinstance(entry, str):
            entry = {"question": entry}
        if not isinstance(entry, dict):
            continue
        question = str(entry.get("question") or entry.get("q") or "").strip()
        answer = str(entry.get("answer") if entry.get("answer") is not None
                     else (entry.get("a") or ""))
        field = str(entry.get("field") or "")
        qid = str(entry.get("id") or "").strip()
        if not qid:                        # 没有 id：按 field+问题文本生成稳定短码
            qid = f"Q_{_field_slug(field)}" if field else f"Q_{_slug(question)[:24]}"
            base, n = qid, 2
            while qid in used:
                qid = f"{base}_{n}"
                n += 1

        status = str(entry.get("status") or "").strip().lower()
        status = _STATUS_ALIASES.get(status, status)
        if status not in STATUSES:
            status = OPEN
        blocking = _flag(entry.get("blocking"), default=False)
        source = str(entry.get("source") or "agent").strip().lower()
        if source not in SOURCES:
            source = "agent"

        item = make_item(qid, field, question, answer=answer, status=status,
                         blocking=blocking, source=source,
                         asked_at=entry.get("asked_at"),
                         answered_at=entry.get("answered_at"))
        # 未声明的额外键接在后面（不丢信息）
        for k, v in entry.items():
            if k not in ITEM_KEYS and k not in ("q", "a"):
                item[k] = v
        used.add(qid)
        out.append(item)
    return out


def effective_status(item: dict) -> str:
    """实际状态：`status=open` 但已经填了 answer → 当作 confirmed。

    手写的人常只填答案忘了翻状态；继续按 open 卡下去只会逼人改 status 来骗闸门。
    """
    status = str(item.get("status") or OPEN).strip().lower()
    status = _STATUS_ALIASES.get(status, status)
    if status not in STATUSES:
        status = OPEN
    if status == OPEN and str(item.get("answer") or "").strip():
        return CONFIRMED
    return status


def open_items(items: Iterable[dict], *, blocking_only: bool = False) -> list[dict]:
    out = []
    for item in normalize(items):
        if effective_status(item) != OPEN:
            continue
        if blocking_only and not _flag(item.get("blocking")):
            continue
        out.append(item)
    return out


def gates(items: Any) -> list[str]:
    """交付闸门：blocking 且仍 open 的项 → 未完成，不得交付。"""
    return [f"{i['id']} 待确认：{i['question'] or i['field']}"
            for i in open_items(items, blocking_only=True)]


def reminder_text(items: Any) -> str:
    """`Reminder` 字段的取值：卡交付的未确认项，一行一条。

    与旧行为对齐：只有报价截止过期时才非空（那时唯一 blocking 项就是它）。
    """
    return "\n".join(i["question"] for i in open_items(items, blocking_only=True))


def summary(items: Any) -> dict[str, int]:
    norm = normalize(items)
    counts = {"total": len(norm), "open": 0, "open_blocking": 0,
              "confirmed": 0, "waived": 0}
    for item in norm:
        status = effective_status(item)
        if status == OPEN:
            counts["open"] += 1
            if _flag(item.get("blocking")):
                counts["open_blocking"] += 1
        elif status == CONFIRMED:
            counts["confirmed"] += 1
        else:
            counts["waived"] += 1
    return counts


if __name__ == "__main__":
    import json
    import sys

    raw = json.load(open(sys.argv[1], encoding="utf-8")) if len(sys.argv) > 1 else []
    if isinstance(raw, list) and raw and isinstance(raw[0], dict) and "CPMS" in raw[0]:
        records = raw
    else:
        records = [{"question_answers": raw}]
    for idx, rec in enumerate(records, 1):
        items = normalize(rec.get(QUESTIONS_KEY))
        s = summary(items)
        print(f"[{idx}] 共 {s['total']} 项 / 未确认 {s['open']}"
              f"（其中卡交付 {s['open_blocking']}）/ 已确认 {s['confirmed']}"
              f"/ 作废 {s['waived']}")
        for item in items:
            print(f"    {effective_status(item):<9} {item['id']:<24} "
                  f"field={item['field']:<12} {item['question'][:60]}")
