#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""列出「抽取已完成但还没人写完」的产物 —— 给 cron 的 agent 环节当待办清单。

为什么需要它
------------
`main.py` 只做到 ERP 结构落盘，**CPMS（产品描述）和 CPMC_ZH（中文品名）天生留空**
—— 这两项必须由 agent 通读原文后手写，脚本不允许代笔（见 CPMS 写作规范 v2）。

所以「抽取完成」≠「交付就绪」。定时任务如果只跑 `scan_pending.py`，产物会一直
停在未完成状态而没人接手。本脚本就是那道交接棒：抽取跑完，把还欠着的产物点名列出来。

输出契约（cron 用，别随手改）
-----------------------------
- 有待办：先打一行 `CPMS_TODO: <N>`，每条一行 `- task=... | 缺=... | 文件=...`
- 无待办：打 `CPMS_TODO: 0`
- 调用方（shell 包装）负责把「0」吞掉，保持安静。

用法::

    uv run ./scripts/cpms_todo.py            # 人看的清单
    uv run ./scripts/cpms_todo.py --json     # 机器读
    uv run ./scripts/cpms_todo.py --dir ...  # 换一个 processed 目录（测试用）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable, Optional

REPO = Path(__file__).resolve().parent.parent
PROCESSED_DIR = REPO / "customer-files" / "processed"

# 中文品名的占位符：agent 没拟名时脚本会填这个
CPMC_PLACEHOLDER = "【中文品名待定】"

# 这些字段是「必须由 agent 亲手写」的，脚本永远留空。
# ⚠️ CPMC 也要查（2026-10-21 补）：NGV 的 CPMC 与 CPMC_ZH 是**两个独立字段**，
#    agent 只补 CPMC_ZH 是常见漏法 —— 那时清单照样显示 0，交付闸门也不拦，
#    但 ERP 带出去的产品名称还挂着占位符。判据用「占位符」而不是「空」，
#    这样 igl 那种由脚本拼好的英文/中文串不会被误报。
AGENT_FIELDS = ("CPMS", "CPMC", "CPMC_ZH")


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        text = value.strip()
        return not text or text == CPMC_PLACEHOLDER
    return False


def _newest_erp(task_dir: Path) -> Optional[Path]:
    """一个任务目录里可能有多份 ERP_*.json（重跑过），取最新那份为准。"""
    files = sorted(task_dir.glob("ERP_*.json"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def _source_label(task_dir: Path) -> str:
    """产物目录里的客户原始文件名（去掉 .txt/.csv 之类的中间件后缀更好读）。"""
    for pattern in ("*.extracted.json",):
        for p in sorted(task_dir.glob(pattern)):
            return p.name[: -len(".extracted.json")]
    for p in sorted(task_dir.iterdir()):
        if p.is_file() and p.suffix.lower() not in (".json", ".txt", ".csv"):
            return p.name
    return ""


def iter_incomplete(processed_dir: Path) -> list[dict[str, Any]]:
    """扫产物目录，返回还欠 agent 手写字段的任务清单（按修改时间倒序）。"""
    if not processed_dir.is_dir():
        return []

    out: list[dict[str, Any]] = []
    for task_dir in processed_dir.iterdir():
        if not task_dir.is_dir() or task_dir.name.startswith((".", "_")):
            continue
        erp = _newest_erp(task_dir)
        if erp is None:
            continue
        try:
            data = json.loads(erp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue

        missing = [f for f in AGENT_FIELDS if _is_blank(data.get(f))]
        if not missing:
            continue
        out.append({
            "task_id": task_dir.name,
            "erp_file": str(erp),
            "missing": missing,
            "source": _source_label(task_dir),
            "khdh": data.get("KHDH") or "",
            "cplb": data.get("CPLB") or "",
            "mtime": erp.stat().st_mtime,
        })

    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def format_report(items: Iterable[dict[str, Any]]) -> str:
    items = list(items)
    if not items:
        return "CPMS_TODO: 0"
    lines = [f"CPMS_TODO: {len(items)}"]
    for it in items:
        bits = [f"task={it['task_id']}", f"缺={'/'.join(it['missing'])}"]
        if it["khdh"]:
            bits.append(f"单号={it['khdh']}")
        if it["source"]:
            bits.append(f"文件={it['source']}")
        bits.append(f"产物={it['erp_file']}")
        lines.append("- " + " | ".join(bits))
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="列出还欠 agent 手写 CPMS/中文品名的产物")
    parser.add_argument("--dir", default=None, help="processed 目录（默认 customer-files/processed）")
    parser.add_argument("--json", action="store_true", help="输出 JSON 而不是人话清单")
    args = parser.parse_args(argv)

    processed = Path(args.dir) if args.dir else PROCESSED_DIR
    items = iter_incomplete(processed)

    if args.json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
    else:
        print(format_report(items))
    return 0


if __name__ == "__main__":
    sys.exit(main())
