#!/usr/bin/env python3
"""scan_pending.py - 扫 `customer-files/pending/`，有就逐份跑 main.py。

给 **Hermes 定时任务**用（业务 2026-10-20：「每 5 分钟扫一次，有任务就执行」）。
CQR_Web 只管上传，抽取一概由本脚本负责 —— 于是 Web 那边不用管进程、队列、超时。

输出契约（配合 cron 的 no_agent 模式）
--------------------------------------
* **什么都没做 → stdout 一个字都不打**，cron 保持安静（不会每 5 分钟骚扰你）
* 有文件 → 打一段人话汇总（每份 ✓/✗ + 产物路径 + CPMS 待办），cron 原样发出来
* 退出码一律 0：单份失败不算「定时任务坏了」，别让它每 5 分钟报错一次

并发
----
用 `customer-files/.scan.lock` 上的 flock 做非阻塞互斥：上一轮还没跑完
（比如一个文件夹里 20 份），这一轮直接安静退出，不会两份进程抢同一批文件。

邮件（.eml，1.0.46）
-------------------
上传口也收 `.eml`，但 `main.py` 吃不了它，所以本脚本多一道展开：认领后调
`ingest_email.py --eml …`，把邮件变成 pending 里的正文 `.txt` / 附件 /（≥2 个附件时的）
单元目录，**同一轮**接着抽掉；原邮件归档到 `customer-files/inbox/<YYYYMMDD>/`。
展开出来的副档（`*.meta.json` / `_email_<ts>.json`）不进队列 —— 它们不是报价。

认领
----
处理前把条目 `os.replace` 到 `pending/_claimed/<name>`（同盘原子操作）：
* main.py 成功 → 它自己把输入搬进 `processed/<task_id>/`，`_claimed/` 自动空
* main.py 崩了 → 条目还在 `_claimed/`，记一次尝试，退回 pending/ 重试
* 失败 3 次 → 搬进 `customer-files/failed/_stuck_<时间戳>/`，不再无限重试

用法
----
    uv run ./scripts/scan_pending.py                  # 给 cron 用（安静模式）
    uv run ./scripts/scan_pending.py --dry-run        # 只看会处理什么，不动文件
    uv run ./scripts/scan_pending.py --verbose        # 空跑也打一行（手工调试）
    uv run ./scripts/scan_pending.py --limit 1        # 只处理一份
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
CUSTOMER_FILES = REPO_ROOT / "customer-files"
PENDING_DIR = CUSTOMER_FILES / "pending"
CLAIM_DIR = PENDING_DIR / "_claimed"
FAILED_DIR = CUSTOMER_FILES / "failed"
LOCK_PATH = CUSTOMER_FILES / ".scan.lock"
STATE_PATH = CUSTOMER_FILES / ".scan_state.json"

MAIN_PY = SCRIPTS_DIR / "main.py"
INGEST_EMAIL_PY = SCRIPTS_DIR / "ingest_email.py"

# main.py 直接能吃的后缀（表格 / 文本 / 压缩包）
DIRECT_SUFFIXES = (".xlsx", ".xls", ".csv", ".pdf", ".docx", ".txt", ".zip")
# 邮件：main.py 吃不了，得先调 ingest_email.py 展开成「正文 .txt / 附件」再抽
EML_SUFFIX = ".eml"
SUPPORTED_SUFFIXES = DIRECT_SUFFIXES + (EML_SUFFIX,)

# 展开成功的 .eml 归档在这（业务上还要能翻回去看原邮件）
INBOX_DIR = CUSTOMER_FILES / "inbox"

MAX_ATTEMPTS = 3
TIMEOUT_SECONDS = 600
INGEST_TIMEOUT_SECONDS = 120


# ── 状态（只记「重试次数」，不记业务数据）─────────────────────────────────
def _read_state(path: Path) -> dict[str, int]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(k): int(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(path: Path, state: dict[str, int]) -> None:
    try:
        path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass          # 状态存不下不该影响抽取


# ── 待处理条目 ────────────────────────────────────────────────────────
def _rel(path: Path) -> str:
    """给人看的相对路径（临时目录里跑的测试不该因为 relative_to 炸掉）。"""
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path)


def pending_items(pending_dir: Optional[Path] = None) -> list[Path]:
    """pending/ 根下要处理的条目（文件或目录），按修改时间从旧到新。"""
    pending = Path(pending_dir) if pending_dir is not None else PENDING_DIR
    if not pending.exists():
        return []
    out: list[Path] = []
    for item in pending.iterdir():
        name = item.name
        if name.startswith(".") or name == "_claimed":
            continue
        if name == LOCK_PATH.name:
            continue
        if item.is_dir():
            if any(p.is_file() for p in item.rglob("*")):
                out.append(item)
        elif item.suffix.lower() in SUPPORTED_SUFFIXES:
            out.append(item)
    out.sort(key=lambda p: p.stat().st_mtime)
    return out


def _claim(item: Path, claim_dir: Path) -> Path:
    """把条目挪进 _claimed/（原子）。同名已存在 → 加后缀，绝不覆盖。"""
    claim_dir.mkdir(parents=True, exist_ok=True)
    target = claim_dir / item.name
    if target.exists():
        target = claim_dir / f"{item.stem}_{datetime.now():%H%M%S%f}{item.suffix}"
    os.replace(item, target)
    return target


def _release(claimed: Path, back_to: Path) -> None:
    """认领失败 → 挪回 pending/（尽力而为，挪不动就留在 _claimed 里，下一轮能看到）。"""
    target = back_to / claimed.name
    if target.exists():
        target = back_to / f"{claimed.stem}_{datetime.now():%H%M%S%f}{claimed.suffix}"
    try:
        os.replace(claimed, target)
    except OSError:
        pass


def _stuck(claimed: Path, failed_dir: Path) -> Path:
    """试太多次了 → 丢进 failed/_stuck_<ts>/，等人来看。"""
    bucket = failed_dir / f"_stuck_{datetime.now():%Y%m%d_%H%M%S}"
    bucket.mkdir(parents=True, exist_ok=True)
    target = bucket / claimed.name
    try:
        os.replace(claimed, target)
    except OSError:
        pass
    return bucket


# ── 跑一份 ────────────────────────────────────────────────────────────
def _run_capture(cmd: list[str], timeout: int, fail_prefix: str) -> tuple[int, str]:
    """跑子进程，返回 (退出码, 失败原因)。产物一律不接管道 —— 只留最后一行当原因。"""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              cwd=str(REPO_ROOT))
    except subprocess.TimeoutExpired:
        return -1, f"{fail_prefix}超时（>{timeout}s）"
    except OSError as exc:
        return -1, f"{fail_prefix}起不来: {exc}"
    tail = [line.strip() for line in (proc.stderr or proc.stdout or "").splitlines()
            if line.strip() and not line.startswith("warning:")]
    return proc.returncode, tail[-1] if tail else f"退出码 {proc.returncode}"


def run_one(claimed: Path, main_py: Path, timeout: int = TIMEOUT_SECONDS,
            extra_args: Optional[list[str]] = None) -> tuple[bool, str]:
    """跑 main.py 抽一份。返回 (是否成功, 人话结果)。

    「成功」判据：进程退出码 0 **且** 认领的条目已被 main.py 搬走。
    只看退出码不够 —— main.py 对「批次里部分失败」也返回 1。
    """
    cmd = [sys.executable, str(main_py), str(claimed), "-c", "auto"]
    if extra_args:
        cmd.extend(extra_args)
    code, reason = _run_capture(cmd, timeout, "")
    if code == -1:
        return False, reason

    moved = not claimed.exists()
    if code == 0 and moved:
        return True, ""
    if code == 0 and not moved:
        reason = "退出码 0 但输入没被搬走（main.py 逻辑异常）"
    return False, reason


def _archive_eml(eml: Path, inbox: Path) -> Path:
    """展开成功的 .eml → `customer-files/inbox/<YYYYMMDD>/`（同名加后缀，不覆盖）。"""
    bucket = inbox / f"{datetime.now():%Y%m%d}"
    bucket.mkdir(parents=True, exist_ok=True)
    target = bucket / eml.name
    if target.exists():
        target = bucket / f"{eml.stem}_{datetime.now():%H%M%S%f}{eml.suffix}"
    try:
        os.replace(eml, target)
    except OSError:
        pass                      # 归档失败不该让已经展开的任务白做
    return target


def ingest_one(claimed: Path, pending: Path, inbox: Optional[Path] = None,
               ingest_py: Optional[Path] = None,
               timeout: int = INGEST_TIMEOUT_SECONDS) -> tuple[bool, str, list[Path]]:
    """把 `.eml` 展开成 pending 里的任务（正文 .txt / 附件 / 单元目录）。

    业务（1.0.46 森西）：「上传也得能传 .eml」——邮件导出的 .eml 里正文和附件都在，
    让人先手工拆附件是把活儿推回给人。所以扫描器多这一步：
    认领 → `ingest_email.py --eml …` → 新出现的 pending 条目走正常 main.py 抽取。

    返回 (是否成功, 失败原因, 本次新投递的条目)。新条目**按扫描器自己的规矩过滤**——
    邮件展开时会顺手写 `items.csv.meta.json` / `_email_<ts>.json` 这类副档，
    那些不是报价，绝不能进队列（踩过：副档被当独立报价喂给 main.py）。
    """
    inbox_dir = Path(inbox) if inbox is not None else INBOX_DIR
    script = Path(ingest_py) if ingest_py is not None else INGEST_EMAIL_PY

    before = {p.name for p in pending.iterdir()} if pending.exists() else set()
    code, reason = _run_capture(
        [sys.executable, str(script), "--eml", str(claimed),
         "--pending-dir", str(pending), "--customer", "auto"],
        timeout, "邮件展开")
    if code == -1:
        return False, reason, []
    if code != 0:
        return False, reason, []

    delivered = [p for p in pending_items(pending) if p.name not in before]
    _archive_eml(claimed, inbox_dir)
    return True, "", delivered


# ── 主流程 ────────────────────────────────────────────────────────────
def scan(limit: Optional[int] = None, dry_run: bool = False, verbose: bool = False,
         main_py: Optional[Path] = None, pending_dir: Optional[Path] = None,
         claim_dir: Optional[Path] = None, failed_dir: Optional[Path] = None,
         state_path: Optional[Path] = None, timeout: int = TIMEOUT_SECONDS,
         inbox_dir: Optional[Path] = None, ingest_py: Optional[Path] = None) -> int:
    pending = Path(pending_dir) if pending_dir else PENDING_DIR
    claim = Path(claim_dir) if claim_dir else (pending / "_claimed")
    failed = Path(failed_dir) if failed_dir else FAILED_DIR
    main_path = Path(main_py) if main_py else MAIN_PY
    inbox = Path(inbox_dir) if inbox_dir else (failed.parent / "inbox")
    ingest_script = Path(ingest_py) if ingest_py else None

    items = pending_items(pending)
    if limit:
        items = items[:limit]

    if not items:
        if verbose:
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] pending 空，跳过")
        return 0

    if dry_run:
        print(f"待处理 {len(items)} 项（--dry-run，不动文件）：")
        for item in items:
            kind = "目录" if item.is_dir() else "文件"
            mtime = datetime.fromtimestamp(item.stat().st_mtime)
            print(f"  · [{kind}] {item.name}  （{mtime:%Y-%m-%d %H:%M}）")
        return 0

    # 重试次数：只认「已经失败过的名字」，成功即清
    state_file = Path(state_path) if state_path else STATE_PATH
    retry_state = _read_state(state_file)
    results: list[dict[str, Any]] = []

    # 队列而不是 for：`.eml` 展开后会在 pending 里**长出新的条目**，
    # 那一轮就得顺手抽掉（否则用户传一封邮件要等两个 5 分钟周期）。
    queue: list[Path] = list(items)
    seen: set[str] = {p.name for p in items}
    idx = 0
    while idx < len(queue):
        item = queue[idx]
        idx += 1
        name = item.name
        attempts = int(retry_state.get(name, 0))
        if attempts >= MAX_ATTEMPTS:
            bucket = _stuck(item, failed)
            results.append({"name": name, "ok": False, "stuck": True,
                            "reason": f"已失败 {attempts} 次，移入 {_rel(bucket)}"})
            retry_state.pop(name, None)
            continue

        claimed = _claim(item, claim)
        note = ""
        if item.suffix.lower() == EML_SUFFIX:
            ok, reason, delivered = ingest_one(claimed, pending, inbox, ingest_script)
            if ok:
                note = (f"📧 邮件已展开 → {len(delivered)} 项" if delivered
                        else "📧 邮件已展开（没有可抽的内容）")
                for new_item in delivered:
                    if new_item.name in seen:
                        continue
                    seen.add(new_item.name)
                    queue.append(new_item)
        else:
            ok, reason = run_one(claimed, main_path, timeout)

        if ok:
            retry_state.pop(name, None)
            results.append({"name": name, "ok": True, "reason": "", "note": note})
            continue

        retry_state[name] = attempts + 1
        if retry_state[name] >= MAX_ATTEMPTS:
            bucket = _stuck(claimed, failed)
            retry_state.pop(name, None)
            results.append({"name": name, "ok": False, "stuck": True,
                            "reason": f"{reason} → 已失败 {MAX_ATTEMPTS} 次，移入 "
                                      f"{_rel(bucket)}"})
        else:
            _release(claimed, pending)
            results.append({"name": name, "ok": False,
                            "reason": f"{reason}（第 {retry_state[name]} 次，稍后重试）"})

    _write_state(state_file, retry_state)

    print_summary(results)
    return 0


def print_summary(results: list[dict[str, Any]]) -> None:
    """人话汇总（cron 会把这段原样发给人看，所以别打日志腔）。"""
    ok = [r for r in results if r["ok"]]
    bad = [r for r in results if not r["ok"]]
    lines = [f"📥 自动抽取完成：{len(results)} 份，成功 {len(ok)}，失败 {len(bad)}"]
    for r in ok:
        note = r.get("note") or ""
        lines.append(f"  ✅ {r['name']}" + (f"：{note}" if note else ""))
    for r in bad:
        lines.append(f"  ❌ {r['name']}：{r['reason']}")
    if ok:
        lines.append("")
        lines.append("产物在 customer-files/processed/ 下；CPMS 还得你亲手写（脚本不生成）。")
    print("\n".join(lines))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="扫 pending/ 并逐份抽取（给定时任务用）")
    parser.add_argument("--dry-run", action="store_true", help="只列出会处理什么，不动文件")
    parser.add_argument("--verbose", action="store_true", help="空跑也打一行（手工调试用）")
    parser.add_argument("--limit", type=int, default=None, help="最多处理几份")
    parser.add_argument("--timeout", type=int, default=TIMEOUT_SECONDS, help="单份超时秒数")
    parser.add_argument("--main", default=None, help="换一个 main.py（测试用）")
    args = parser.parse_args(argv)

    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    CUSTOMER_FILES.mkdir(parents=True, exist_ok=True)

    # 非阻塞互斥：上一轮还在跑就安静退出（cron 会每 5 分钟来一次，不能叠）
    with open(LOCK_PATH, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if args.verbose:
                print("上一轮还在跑，跳过本轮")
            return 0
        return scan(limit=args.limit, dry_run=args.dry_run, verbose=args.verbose,
                    main_py=Path(args.main) if args.main else None,
                    timeout=args.timeout)


if __name__ == "__main__":
    sys.exit(main())
