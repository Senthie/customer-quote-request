#!/usr/bin/env python3
"""mail_agent_route.py - 邮件分拣 agent 的「投递/归档」工具。

分工（2026-11-19 业务口径）：**分类权在邮件分拣 agent，报价侧只吃询价**。
本脚本是分类完成后唯一允许的两种去向，别用裸 `mv`：

    询价      → customer-files/pending/          （报价流水线会立刻认领）
    非询价    → customer-files/inbox/<YYYYMMDD>/_non_enquiry/
                                                 （原件保留，不删，便于复核）

每次投递都往 `customer-files/logs/mail_agent_ledger.jsonl` 追加一行，
报价侧的 `mail_agent_status.py` 靠它报「这封信判到哪儿去了」。

用法
----
    uv run ./scripts/mail_agent_route.py --list
    uv run ./scripts/mail_agent_route.py --quote "customer-files/staging/a.eml" ...
    uv run ./scripts/mail_agent_route.py --non-quote "customer-files/staging/b.eml" \\
        --reason "CMA 检测结果通知，无产品规格"

`--list` 只读；其它两个会移动文件。文件名请从 `--list` 原样复制
（主题里常有 NBSP \\xa0，手敲空格会 no such file）。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
CUSTOMER_FILES = REPO_ROOT / "customer-files"
STAGING_DIR = CUSTOMER_FILES / "staging"
PENDING_DIR = CUSTOMER_FILES / "pending"
INBOX_DIR = CUSTOMER_FILES / "inbox"
LOG_DIR = CUSTOMER_FILES / "logs"
LEDGER = LOG_DIR / "mail_agent_ledger.jsonl"


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for i in range(2, 1000):
        cand = path.with_name(f"{stem}_{i}{suffix}")
        if not cand.exists():
            return cand
    raise RuntimeError(f"同名文件过多：{path}")


def _move(src: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = _unique(dest_dir / src.name)
    shutil.move(str(src), str(dest))
    return dest


def _log(entry: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    entry.setdefault("ts", datetime.now().isoformat(timespec="seconds"))
    with open(LEDGER, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _resolve(name: str) -> Path:
    p = Path(name)
    if not p.is_absolute():
        p = (REPO_ROOT / p) if (REPO_ROOT / p).exists() else (STAGING_DIR / name)
    if p.exists():
        return p
    # NBSP / 全角空格兜底：按去掉空白的归一化名再找一遍
    norm = lambda s: "".join(s.split()).replace("\u00a0", "").replace("\u3000", "")
    for cand in STAGING_DIR.iterdir() if STAGING_DIR.exists() else []:
        if norm(cand.name) == norm(Path(name).name):
            return cand
    raise SystemExit(f"!! 找不到：{name}")


def cmd_list() -> int:
    if not STAGING_DIR.exists():
        print("staging/ 不存在（还没收过信）")
        return 0
    files = sorted(p for p in STAGING_DIR.iterdir() if p.is_file() and p.suffix.lower() == ".eml")
    if not files:
        print("staging/ 空（没有待分类的信）")
        return 0
    print(f"staging/ 待分类 {len(files)} 封：")
    for p in files:
        print(f"  {p.name}  ({p.stat().st_size / 1024:.0f} KB)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="邮件分拣：把 staging/ 里的信投到 pending/ 或非询价归档区")
    ap.add_argument("--list", action="store_true", help="列出 staging/ 里待分类的信")
    ap.add_argument("--quote", nargs="+", default=[], help="判定为询价的信（移动到 pending/）")
    ap.add_argument("--non-quote", nargs="+", default=[], help="判定为非询价的信（归档到 inbox/<日期>/_non_enquiry/）")
    ap.add_argument("--reason", default="", help="非询价的归因一句话（写进台账，必填 c 类时强烈建议）")
    args = ap.parse_args(argv)

    if args.list or (not args.quote and not args.non_quote):
        return cmd_list()

    today = datetime.now().strftime("%Y%m%d")
    moved = 0

    for name in args.quote:
        src = _resolve(name)
        dest = _move(src, PENDING_DIR)
        _log({"file": src.name, "status": "quoted", "dest": str(dest.relative_to(REPO_ROOT))})
        print(f"→ 询价  {src.name}  ⇒ pending/{dest.name}")
        moved += 1

    nonq_dir = INBOX_DIR / today / "_non_enquiry"
    for name in args.non_quote:
        src = _resolve(name)
        dest = _move(src, nonq_dir)
        _log({"file": src.name, "status": "non_enquiry", "reason": args.reason,
              "dest": str(dest.relative_to(REPO_ROOT))})
        print(f"→ 非询价 {src.name}  ⇒ {dest.relative_to(REPO_ROOT)}")
        moved += 1

    if moved:
        print(f"本轮投递 {moved} 封（台账 customer-files/logs/mail_agent_ledger.jsonl）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
