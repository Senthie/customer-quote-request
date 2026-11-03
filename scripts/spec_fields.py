#!/usr/bin/env python3
"""「规格 / 物流」11 字段的推导引擎（业务 2026-10-30 要求）。

业务原话（森西）：
    「BZFS TBLX SHFS SHDQ ZXLB ZBLX ZXFS DKMC YSLX chkBSYTB BZ：没有一处推导代码。
     我认为在你填写 CPMS 的时候，这些值由你推导出，并且填到对应的值，
     这样也可以减轻 CPMS 的负担。」

也就是说：这些字段以前要么靠客户档案写死（`erp_fixed`）、要么靠默认值补空
（`erp_defaults`），没有一处从**客户原文**推。于是包装/物流这些信息全被写进
`CPMS` 正文，字段却空着 —— 业务还得反着从描述里抄回字段。

本模块做三件事（规则表在 `assets/spec_fields.yaml`）：

  `evidence_text()` 把客户文件里的可读文本拼成一个 blob（规范键的值也算证据）；
  `derive()`        按规则表匹配 → [{field, value, evidence}]，**每条都带证据原文**；
  `missing()`       跑完默认值后仍为空的字段 → 内部确认清单要问的那些。

设计原则（别破坏）：

  * **不许猜**：匹配不到就是匹配不到，交给客户档案默认值 / 问客户。
    ERP 对枚举外的值是**静默丢弃** —— 猜错不报错，代价比留空高得多。
  * **证据必须可引用**：每个推导结果带 `evidence`（命中处的上下文原文），
    日志里原样打出来，业务核对时能一眼看出「凭什么填这个」。
  * **不进 `CPMS`**：这些内容落字段后，CPMS 正文不再展开（见 `cpms_keywords`
    与 `cpms_lint` 的 CPMS090/CPMS100）。

CLI（排查单个任务用）：

    uv run python scripts/spec_fields.py --text customer-files/x.txt
    uv run python scripts/spec_fields.py --task <task_dir>
    uv run python scripts/spec_fields.py --list          # 打印规则表
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RULES = REPO_ROOT / "assets" / "spec_fields.yaml"

# 证据片段前后各留多少字符（够看清语境，又不至于把整段原文塞进日志）
_SNIPPET_PAD = 48


@lru_cache(maxsize=4)
def load_rules(path: Optional[str] = None) -> dict:
    """读规则表（带缓存）。文件缺失 → 空规则（不影响主流程）。"""
    import yaml

    target = Path(path) if path else DEFAULT_RULES
    if not target.exists():
        return {"fields": {}}
    with open(target, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    data.setdefault("fields", {})
    return data


def field_keys(rules: Optional[dict] = None) -> list[str]:
    return list((rules or load_rules()).get("fields") or {})


# ── 证据文本 ─────────────────────────────────────────────────────────────
def evidence_text(customer_data: Any = None, extra: str = "") -> str:
    """把客户文件里的可读文本拼成一个 blob。

    来源（有就拼，没有就跳）：
      * `CustomerData.text_blocks` —— 整份文件的文本块（CPMS 素材，最全）
      * `CustomerData.values` / `.scanned` —— 规范键的值（分区表抽出来的短值，
        如 `primary_packaging="14 books per export carton"` 就在这里面）
      * `extra` —— 调用方另外给的正文（邮件标题/正文、规范键拼串）
    """
    parts: list[str] = []
    if customer_data is not None:
        for block in getattr(customer_data, "text_blocks", None) or []:
            if block:
                parts.append(str(block))
        for mapping in (getattr(customer_data, "values", None) or {},
                        getattr(customer_data, "scanned", None) or {}):
            for key, value in mapping.items():
                if value in (None, ""):
                    continue
                parts.append(f"{key}: {value}")
    if extra:
        parts.append(str(extra))
    return "\n".join(parts)


def _snippet(text: str, match: re.Match) -> str:
    start = max(0, match.start() - _SNIPPET_PAD)
    end = min(len(text), match.end() + _SNIPPET_PAD)
    return " ".join(text[start:end].split())


# ── 推导 ─────────────────────────────────────────────────────────────────
def derive(text: str, rules: Optional[dict] = None) -> list[dict]:
    """按规则表从客户原文推字段。返回 [{field, label, value, evidence, pattern}]。

    规则顺序即优先级（先到先得）—— 具体的口岸要排在笼统的 `FOB` 前面，
    否则笼统规则会把具体的吃掉（`assets/spec_fields.yaml` 顶部有说明）。
    同一字段只出一个结果（第一个命中）。
    """
    rules = rules or load_rules()
    out: list[dict] = []
    if not text:
        return out
    for field, spec in (rules.get("fields") or {}).items():
        for rule in spec.get("evidence") or []:
            pattern = str(rule.get("match") or "")
            if not pattern:
                continue
            try:
                found = re.search(pattern, text)
            except re.error as exc:      # 规则表写坏了一条正则 → 跳过并提示，不炸整个管道
                print(f"[spec_fields] 规则 {field} 的正则无效，已跳过: {exc}", file=sys.stderr)
                continue
            if not found:
                continue
            out.append({
                "field": field,
                "label": str(spec.get("label") or field),
                "value": rule.get("value"),
                "evidence": _snippet(text, found),
                "pattern": pattern,
            })
            break
    return _apply_derives(out, rules)


def _apply_derives(items: list[dict], rules: dict) -> list[dict]:
    """处理 `derive_from`：某字段有值 → 连带推出另一个字段。

    当前只有一对：`TBLX=不打卡板 → chkBSYTB=true`（业务口径：勾选框和下拉是
    同一件事，说「不打卡板」就等于勾上「不使用台板」）。
    """
    by_field = {item["field"]: item for item in items}
    for field, spec in (rules.get("fields") or {}).items():
        follow = spec.get("derive_from")
        if not isinstance(follow, dict):
            continue
        parent = str(follow.get("field") or "")
        when = follow.get("when_value")
        parent_item = by_field.get(parent)
        if not parent_item or parent_item.get("value") != when:
            continue
        value = follow.get("then")
        if field in by_field and by_field[field].get("value") == value:
            continue
        item = {
            "field": field,
            "label": str(spec.get("label") or field),
            "value": value,
            "evidence": f"{parent}={when} → 连带（{parent_item.get('evidence', '')}）",
            "pattern": f"derive_from:{parent}",
        }
        if field in by_field:
            items[items.index(by_field[field])] = item
        else:
            items.append(item)
        by_field[field] = item
    return items


# ── 缺口 ─────────────────────────────────────────────────────────────────
def missing(result: dict, rules: Optional[dict] = None) -> list[dict]:
    """跑完（含客户档案默认值）仍为空的规格字段 → 该问客户的那些。

    只报**规则表里登记过**的字段；`chk*` 勾选框的 false 算「有值」
    （false 是明确答案，不是缺失）。
    """
    rules = rules or load_rules()
    out: list[dict] = []
    for field, spec in (rules.get("fields") or {}).items():
        if field not in result:
            continue
        value = result.get(field)
        if value not in (None, "") :
            continue
        out.append({
            "field": field,
            "label": str(spec.get("label") or field),
            "keywords": list(spec.get("cpms_keywords") or []),
        })
    return out


def gap_items(record: dict, rules: Optional[dict] = None,
              today: Optional[str] = None) -> list[dict]:
    """仍为空的规格字段 → 内部确认清单条目（`question_answers`）。

    业务 2026-10-30 的口径：这些字段**该由我们推**，不该让业务手填。既然推不出来
    （客户原文没证据、客户档案也没默认值），就得明确问客户 —— 而不是留个空格子
    让人猜。用 `question_answers.make_item` 造，键与管道自产条目完全一致。

    `blocking=False`：装箱/纸箱没定不拦交付（业务 2026-10-19 拍板「装箱待确认
    可以交付」）。
    """
    from question_answers import make_item

    items: list[dict] = []
    for gap in missing(record, rules):
        field = gap["field"]
        label = gap["label"]
        items.append(make_item(
            f"Q_{field}", field,
            f"{label}（{field}）为空：客户原文没写、客户档案也没默认值 —— "
            f"请与客户确认{label}，否则 ERP 这个下拉会空着",
            source="pipeline", asked_at=today,
        ))
    return items


# ── CLI ──────────────────────────────────────────────────────────────────
def _cmd_list() -> int:
    rules = load_rules()
    print(f"规格字段推导表 v{rules.get('version')}（{rules.get('captured_at')}）")
    print(f"来源: {rules.get('source')}\n")
    for field, spec in (rules.get("fields") or {}).items():
        rules_n = len(spec.get("evidence") or [])
        follow = spec.get("derive_from")
        tail = f"，derive_from={follow}" if follow else ""
        print(f"  {field:<10} {spec.get('label')}　规则 {rules_n} 条{tail}")
    return 0


def _cmd_text(path: str) -> int:
    text = Path(path).read_text(encoding="utf-8", errors="ignore")
    items = derive(text)
    if not items:
        print("没推出任何字段（客户原文里没有这些证据 → 走客户档案默认值/问客户）")
        return 0
    for item in items:
        print(f"  {item['field']:<10} = {item['value']!r}")
        print(f"      证据: …{item['evidence']}…")
    return 0


def _cmd_task(task_dir: str) -> int:
    """对一个已跑完的任务目录做推导，并用产物里的记录算缺口。"""
    directory = Path(task_dir)
    extracted = next(iter(sorted(directory.glob("*.extracted.json"))), None)
    records: list = []
    for path in sorted(directory.glob("ERP_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        records = data if isinstance(data, list) else [data]
        break
    text_parts = []
    if extracted:
        data = json.loads(extracted.read_text(encoding="utf-8"))
        text_parts.append(json.dumps(data.get("values") or {}, ensure_ascii=False))
        text_parts.append(json.dumps(data.get("products") or [], ensure_ascii=False))
    for path in sorted(directory.iterdir()):
        if path.suffix.lower() in (".txt", ".eml") and path.is_file():
            text_parts.append(path.read_text(encoding="utf-8", errors="ignore"))
    items = derive("\n".join(text_parts))
    print(f"任务: {directory.name}")
    for item in items:
        print(f"  推导 {item['field']:<10} = {item['value']!r}  证据: …{item['evidence']}…")
    if not items:
        print("  没推出任何字段")
    if records:
        gaps = missing(records[0])
        print(f"\n仍为空（该问客户或有档案默认值兜）: "
              f"{' / '.join(g['field'] for g in gaps) if gaps else '无'}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="规格/物流字段推导（客户原文 → ERP 枚举）")
    parser.add_argument("--list", action="store_true", help="打印规则表")
    parser.add_argument("--text", help="直接给一个文本文件做推导")
    parser.add_argument("--task", help="给一个任务目录（含 *.extracted.json / ERP_*.json）")
    args = parser.parse_args(argv)
    if args.list:
        return _cmd_list()
    if args.text:
        return _cmd_text(args.text)
    if args.task:
        return _cmd_task(args.task)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
