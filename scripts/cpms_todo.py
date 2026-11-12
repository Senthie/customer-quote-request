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

并发宽限（2026-11-11 现场事故后加）
----------------------------------
背景：森西让 agent 手工处理一封 `.eml`，agent 10:05 抽完、10:06 起手工写 CPMS；
同一时刻 cron（`*/5`）看到这份产物「CPMS 空」→ 把同一条待办交给**另一个** agent 会话，
两边在 10:10 前后互相覆盖产物（字段、`CPMS_来源对照.md`、`.erp-import.json` 全被写了两遍）。
`pending/` 有 flock，`processed/` 里的产物**没有任何互斥**。

所以给「判断谁是欠账」加一道时间宽限：**任务目录里任何文件在最近 N 分钟内被写过**
→ 当作「可能有人在手工写」，本轮不进待办（继续下一单），并在 stderr 记一行，方便回查。

- 默认 `--grace-minutes 0` = **不启用**（库层保持「所见即所报」，测试与人工查询不受影响）；
- cron 的壳 `cqr_scan_and_cpms.sh` 显式传 `--grace-minutes 15`（覆盖 3 个 5 分钟调度周期）；
- 已完成的任务不进「跳过」清单 —— 跳过清单只解释「为什么这单明明欠账却没报」。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Optional

REPO = Path(__file__).resolve().parent.parent
PROCESSED_DIR = REPO / "customer-files" / "processed"

# 同目录模块（脚本直接跑时脚本目录在 sys.path 上；测试里也插了这一层）
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from erp_bundle import iter_bundles, load_bundle  # noqa: E402

# 中文品名的占位符：agent 没拟名时脚本会填这个
CPMC_PLACEHOLDER = "【中文品名待定】"

# 这些字段是「必须由 agent 亲手写」的，脚本永远留空。
# ⚠️ CPMC 也要查（2026-10-21 补）：NGV 的 CPMC 与 CPMC_ZH 是**两个独立字段**，
#    agent 只补 CPMC_ZH 是常见漏法 —— 那时清单照样显示 0，交付闸门也不拦，
#    但 ERP 带出去的产品名称还挂着占位符。判据用「占位符」而不是「空」，
#    这样 igl 那种由脚本拼好的英文/中文串不会被误报。
AGENT_FIELDS = ("CPMS", "CPMC", "CPMC_ZH")

# ECPMC（原始产品名）单独判：只有**客户档案声明了 rules.ecpmc.source_keys** 的客户才要求它。
#   NGV 声明了（取原文 product_name / series_name）→ 空着就是缺陷；
#   igl 没声明（业务 2026-09-24：「先不做」）→ 空着是正确的，不能报警。
# 业务 2026-10-21 澄清：ECPMC 是**报价的原始产品名**，不限于英文 ——
#   德文 `Malbücher`、日文、法文一律照抄，不许因为「不是英文」就留空。
ECPMC_FIELD = "ECPMC"

# 并发宽限（秒）：任务目录里任何文件的 mtime 落在这个窗口内 → 本轮跳过。
DEFAULT_GRACE_MINUTES = 0


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        text = value.strip()
        return not text or text == CPMC_PLACEHOLDER
    return False


def _newest_erp(task_dir: Path) -> Optional[Path]:
    """一个任务目录里可能有多份 ERP_*.json（重跑过），取最新那份为准。

    必须排除 `*.erp-import.json`（1.0.65 的导出口径副本）：它只有 59 个键、
    没有 `CPMC_ZH`/`version`/`ID`，拿它当产物会**每一单都误报「缺 CPMC_ZH」**
    （实测 31 单全是假待办，主产物其实都写好了）。
    """
    files = sorted(iter_bundles(task_dir), key=lambda p: p.stat().st_mtime)
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


def _profile_id(task_dir: Path) -> str:
    """产物目录里 `.extracted.json` 记着这次用的客户档案 id（没有就空）。"""
    for p in sorted(task_dir.glob("*.extracted.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        # ★ 2026-11-11：`.extracted.json` 正常是对象；被手工拷贝成数组/字符串时
        #   `data.get` 会 AttributeError —— 那会让**整轮 cron 挂掉**（不是只漏这一单）。
        if not isinstance(data, dict):
            continue
        pid = str(data.get("profile_id") or "").strip()
        if pid:
            return pid
    return ""


_PROFILE_CACHE: dict[str, Any] = {}


def _requires_ecpmc(profile_id: str) -> bool:
    """该客户档案是否要求 ECPMC（原始产品名）。

    只有档案里声明了 `rules.ecpmc.source_keys` 的客户才要求 —— igl 明确不产出
    （业务 2026-09-24「先不做」），空着是对的；不按档案判会天天误报。
    判不出来（没档案 / 读不动）一律当作**不要求**，宁可漏报也不制造噪音。
    """
    if not profile_id:
        return False
    if profile_id in _PROFILE_CACHE:
        return bool(_PROFILE_CACHE[profile_id])
    try:
        from customer_profile import load_profile  # 同目录，脚本运行时在 sys.path 里

        keys = load_profile(profile_id).rule("ecpmc.source_keys", []) or []
        needed = bool(keys)
    except Exception:  # noqa: BLE001 —— 读档案失败不该让整轮清单挂掉
        needed = False
    _PROFILE_CACHE[profile_id] = needed
    return needed


def _newest_mtime(task_dir: Path, erp: Path) -> float:
    """任务目录里**最新**的文件时间（含子目录），用来判断「有没有人在写」。

    只盯产物 JSON 不够：手工处理时先写 `CPMS_来源对照.md`、再写 JSON、再重生成
    `.erp-import.json`，任何一步都可能落在两次 cron 之间。
    """
    try:
        newest = erp.stat().st_mtime
    except OSError:
        newest = 0.0
    for p in task_dir.iterdir():
        try:
            newest = max(newest, p.stat().st_mtime)
        except OSError:  # 刚被移走 / 权限问题：不影响判断
            continue
    return newest


def scan(
    processed_dir: Path,
    *,
    grace_minutes: float = DEFAULT_GRACE_MINUTES,
    now: Optional[float] = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """扫产物目录 → (欠账清单, 因「刚被写过」跳过的清单)。

    `grace_minutes > 0` 时，任务目录里最新 mtime 落在窗口内的**欠账**任务不进清单，
    改为进第二份（含 `idle_seconds`，方便回查到底是谁在写）。已完成的任务两边都不进。
    """
    if not processed_dir.is_dir():
        return [], []

    now = time.time() if now is None else now
    grace_seconds = max(float(grace_minutes), 0.0) * 60.0

    out: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for task_dir in processed_dir.iterdir():
        if not task_dir.is_dir() or task_dir.name.startswith((".", "_")):
            continue
        erp = _newest_erp(task_dir)
        if erp is None:
            continue
        try:
            # ★ 2026-10-29：ERP 导出是**数组**（一个文件可含多个产品）→ 逐条记录检查；
            #   老的单对象文件照样能读。同一条产品记录进一条待办，`record` 标明第几条。
            from erp_bundle import load_bundle
            records = load_bundle(erp)
        except (OSError, ValueError):
            continue

        newest_mtime = _newest_mtime(task_dir, erp)
        idle_seconds = now - newest_mtime
        recently_touched = grace_seconds > 0 and idle_seconds < grace_seconds

        profile_id = _profile_id(task_dir)
        for ridx, data in enumerate(records, 1):
            missing = [f for f in AGENT_FIELDS if _is_blank(data.get(f))]
            # ECPMC 只对「档案声明了」的客户查（NGV 要求、igl 不产出）
            if _is_blank(data.get(ECPMC_FIELD)) and _requires_ecpmc(profile_id):
                missing.append(ECPMC_FIELD)
            # ★ v3（2026-10-30）：内部确认清单里**卡交付**且未确认的项也是待办 ——
            #   agent 的交接棒不只补 CPMS/品名，还要把卡住的事实确认掉。
            #   blocking=False 的项（如 ZDFS 由业务补填）不进待办，否则天天报。
            #   v2 旧产物没有这个键 → gates() 返回 [] → 行为不变。
            from question_answers import gates as _qa_gates
            qa_blocking = _qa_gates(data.get("question_answers"))
            if qa_blocking:
                ids = [g.split(" ")[0] for g in qa_blocking[:3]]
                more = "" if len(qa_blocking) <= 3 else f"等{len(qa_blocking)}项"
                missing.append(f"确认清单({','.join(ids)}{more})")
            if not missing:
                continue
            item = {
                "task_id": task_dir.name,
                "erp_file": str(erp),
                "record": ridx,
                "record_count": len(records),
                "missing": missing,
                "source": _source_label(task_dir),
                "khdh": data.get("KHDH") or "",
                "cplb": data.get("CPLB") or "",
                "mtime": newest_mtime,
            }
            if recently_touched:
                item["idle_seconds"] = round(idle_seconds, 1)
                item["skipped"] = "recent-write"
                skipped.append(item)
                continue
            out.append(item)

    out.sort(key=lambda x: x["mtime"], reverse=True)
    skipped.sort(key=lambda x: x["mtime"], reverse=True)
    return out, skipped


def iter_incomplete(
    processed_dir: Path,
    *,
    grace_minutes: float = DEFAULT_GRACE_MINUTES,
    now: Optional[float] = None,
) -> list[dict[str, Any]]:
    """扫产物目录，返回还欠 agent 手写字段的任务清单（按修改时间倒序）。"""
    return scan(processed_dir, grace_minutes=grace_minutes, now=now)[0]


def format_report(items: Iterable[dict[str, Any]]) -> str:
    items = list(items)
    if not items:
        return "CPMS_TODO: 0"
    lines = [f"CPMS_TODO: {len(items)}"]
    for it in items:
        bits = [f"task={it['task_id']}"]
        if it.get("record_count", 1) > 1:
            bits.append(f"第{it.get('record', 1)}/{it['record_count']}条")
        bits.append(f"缺={'/'.join(it['missing'])}")
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
    parser.add_argument("--grace-minutes", type=float, default=DEFAULT_GRACE_MINUTES,
                        help="并发宽限：任务目录最近 N 分钟内被写过就本轮跳过（默认 0 = 不启用；"
                             "cron 的壳传 15）")
    parser.add_argument("--show-skipped", action="store_true",
                        help="把「因刚被写过而跳过」的条目也打出来（人工排查用，不进 cron 契约）")
    args = parser.parse_args(argv)

    processed = Path(args.dir) if args.dir else PROCESSED_DIR
    items, skipped = scan(processed, grace_minutes=args.grace_minutes)

    if skipped:
        # ★ 只进 stderr：stdout 是 cron 的输出契约（`CPMS_TODO: N` + `- task=…`），
        #   多打一行会让 shell 壳的解析和 agent 的待办清单都变脏。
        detail = "; ".join(
            f"{it['task_id']}({int(it.get('idle_seconds', 0))}s前写过)" for it in skipped[:5]
        )
        more = "" if len(skipped) <= 5 else f" 等{len(skipped)}条"
        print(f"# 跳过 {len(skipped)} 条欠账：最近 {args.grace_minutes:g} 分钟内有写入 "
              f"（可能有人在手工处理）—— {detail}{more}", file=sys.stderr)

    if args.json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
    else:
        print(format_report(items))
        if args.show_skipped and skipped:
            print(f"· 跳过 {len(skipped)} 条（宽限 {args.grace_minutes:g} 分钟）：")
            for it in skipped:
                print(f"  - task={it['task_id']} | 缺={'/'.join(it['missing'])}"
                      f" | {int(it.get('idle_seconds', 0))} 秒前写过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
