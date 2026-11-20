#!/usr/bin/env python3
"""把「本轮提取 + 录入 ERP」的结果汇总成一封邮件，发回收信邮箱。

业务口径 2026-11-20（森西）：
  1. 一轮多封询价 → **合成一封**汇总邮件（不要一封询价一封）；
  2. 表格三列「邮件标题 | ERP表单号 | 是否录入ERP」，没录入成功的在**备注**里写原因；
  3. 本轮没有新询价 → **不发**；
  4. 发送失败 → **不重发**（SMTP 已投递但存已发送失败时 himalaya 也会非零退出，
     重试会变成重复邮件。见 skill `himalaya` 的 folder.aliases 坑）。

数据来源是**产物本身**，不是 agent 的自述：
  `customer-files/processed/<task>/**/*.erp-push.json`（scripts/erp_push.py 每次推送都写）
  + 同目录的 `_email.meta.json` 取邮件标题。

去重：已汇报过的回执记在 `customer-files/logs/round_report_sent.jsonl`，
键 = 回执相对路径 + 回执里的时间戳。同一个回执只会被汇报一次。

用法：
  uv run ./scripts/round_report_mail.py            # 有新的就发，没有就静默退出
  uv run ./scripts/round_report_mail.py --dry-run   # 只打印将要发的邮件，不发
  uv run ./scripts/round_report_mail.py --init      # 把现有回执全部登记为「已汇报」，不发送

退出码：0 = 正常（含「没东西可发」/「已登记但发送失败」）；2 = 参数或环境问题。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

REPO = Path(__file__).resolve().parent.parent
PROCESSED = REPO / "customer-files" / "processed"
LEDGER = REPO / "customer-files" / "logs" / "round_report_sent.jsonl"
MAIL_CFG = REPO / "assets" / "mail.yaml"

# 邮戳用北京时间：机器是 UTC，给业务看的邮件不能显示 UTC（曾差 8 小时）。
try:  # 有 tzdata 就走真时区；没有就退化成固定 +08:00
    from zoneinfo import ZoneInfo
    BJT = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover - 只在极简环境里触发
    BJT = timezone(timedelta(hours=8))


def bjt_now() -> datetime:
    return datetime.now(BJT).replace(tzinfo=None)


# 这些状态代表「没真写进 ERP」；dry_run 是干跑，压根不算一条结果，不进报表。
NOT_WRITTEN = {"failed", "partial", "invalid", "rejected", "unsupported", "archive_missing", "error"}
SKIP_STATUS = {"dry_run"}

STATUS_ZH = {
    "created": "已录入",
    "partial": "部分录入（主表建了、明细没落）",
    "failed": "录入失败",
    "invalid": "推送前校验不通过",
    "rejected": "ERP 拒绝",
    "unsupported": "ERP 不支持该写法",
    "archive_missing": "产品档案不命中（主表已建、明细空白）",
}


# ── 回执读取 ───────────────────────────────────────────────────────────────
def find_receipts(root: Optional[Path] = None) -> list[Path]:
    root = root or PROCESSED
    if not root.is_dir():
        return []
    return sorted(root.rglob("*.erp-push.json"))


def receipt_stamp(receipt: dict[str, Any], path: Path) -> str:
    """回执的时间指纹：优先用它自己写的时间，取不到就用文件 mtime。"""
    for key in ("at", "pushed_at"):
        val = receipt.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%dT%H:%M:%S")
    except OSError:
        return "unknown"


def ledger_key(path: Path, stamp: str) -> str:
    try:
        rel = path.relative_to(REPO).as_posix()
    except ValueError:
        rel = str(path)
    return f"{rel}@{stamp}"


def read_ledger() -> set[str]:
    if not LEDGER.is_file():
        return set()
    keys: set[str] = set()
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("key"):
            keys.add(rec["key"])
        for k in rec.get("keys") or []:
            if isinstance(k, str):
                keys.add(k)
    return keys


def append_ledger(keys: list[str], status: str, detail: str = "") -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "keys": keys,
        "status": status,
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "count": len(keys),
    }
    if detail:
        rec["detail"] = detail[:2000]
    with LEDGER.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ── 标题 / 备注 ────────────────────────────────────────────────────────────
def email_subject_for(receipt_path: Path) -> str:
    """从任务目录里的 _email.meta.json 取客户来信标题；没有就用产物名兜底。"""
    task_dir = receipt_path.parent
    metas = sorted(task_dir.glob("*/_email.meta.json")) or sorted(task_dir.glob("_email.meta.json"))
    for meta in metas:
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        subject = (data.get("subject") or "").strip()
        if subject:
            return subject
    return receipt_path.name.replace(".erp-push.json", "")


def status_of(receipt: dict[str, Any]) -> str:
    status = (receipt.get("status") or "unknown").strip()
    return status if status else "unknown"


def bdbh_of(receipt: dict[str, Any]) -> str:
    bdbh = receipt.get("bdbh")
    if isinstance(bdbh, list):
        return " / ".join(str(x) for x in bdbh if x) or "—"
    if isinstance(bdbh, str) and bdbh.strip():
        return bdbh.strip()
    # 主表建好但明细没落时，结果里也可能带单号
    for res in receipt.get("results") or []:
        if isinstance(res, dict) and (res.get("bdbh") or "").strip():
            return res["bdbh"].strip()
    return "—"


def reason_of(receipt: dict[str, Any]) -> str:
    """没录入成功时的原因：优先用推送环节自己的话，别自己编。"""
    bits: list[str] = []
    status = status_of(receipt)

    problems = receipt.get("problems")
    if isinstance(problems, list):
        bits.extend(str(p) for p in problems if p)
    elif isinstance(problems, str) and problems.strip():
        bits.append(problems.strip())

    if receipt.get("error"):
        bits.append(str(receipt["error"]).strip())
    for res in receipt.get("results") or []:
        if isinstance(res, dict) and res.get("error"):
            bits.append(str(res["error"]).strip())
    if receipt.get("hint"):
        bits.append(str(receipt["hint"]).strip())

    head = STATUS_ZH.get(status)
    if head and head != "已录入":
        bits.insert(0, head)
    elif not head:
        bits.insert(0, f"状态 {status}")

    seen: list[str] = []
    for bit in bits:
        bit = " ".join(str(bit).split())
        if bit and bit not in seen:
            seen.append(bit)
    return "；".join(seen) if seen else "原因未记录（看该目录 .erp-push.json）"


# ── 汇总 ───────────────────────────────────────────────────────────────────
def collect(root: Optional[Path] = None) -> tuple[list[dict[str, Any]], list[str]]:
    """返回（本轮要汇报的行，去重键列表）。只保留每个回执的最新一份。"""
    known = read_ledger()
    rows: list[dict[str, Any]] = []
    keys: list[str] = []
    for path in find_receipts(root):
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[warn] 读不了回执 {path}: {exc}", file=sys.stderr)
            continue
        status = status_of(receipt)
        if status in SKIP_STATUS:
            continue
        stamp = receipt_stamp(receipt, path)
        key = ledger_key(path, stamp)
        if key in known:
            continue
        ok = status == "created"
        rows.append({
            "key": key,
            "subject": email_subject_for(path),
            "bdbh": bdbh_of(receipt),
            "ok": ok,
            "reason": "" if ok else reason_of(receipt),
            "status": status,
            "path": str(path),
        })
        keys.append(key)
    return rows, keys


def compose(rows: list[dict[str, Any]], to_addr: str, from_addr: str) -> str:
    now = bjt_now()
    stamp = now.strftime("%Y-%m-%d %H:%M")
    ok_n = sum(1 for r in rows if r["ok"])
    bad_n = len(rows) - ok_n
    subject = f"【报价提取回执】{now.strftime('%Y-%m-%d')} 本轮 {len(rows)} 条"
    if bad_n:
        subject += f"（{ok_n} 成功 / {bad_n} 未录入）"

    lines = [
        f"From: {from_addr}",
        f"To: {to_addr}",
        f"Subject: {subject}",
        "MIME-Version: 1.0",
        'Content-Type: text/plain; charset="utf-8"',
        "Content-Transfer-Encoding: 8bit",
        "",
        "Hi Demo：",
        "这是最新提取到的客户询价：",
        "",
        "| 邮件标题 | ERP表单号 | 是否录入ERP|",
        "| --- | --- | --- |",
    ]
    for row in rows:
        title = row["subject"].replace("|", "\\|")
        lines.append(f"| {title} | {row['bdbh']} | {'是' if row['ok'] else '否'} |")

    lines.append("")
    lines.append("备注：")
    failures = [r for r in rows if not r["ok"]]
    if failures:
        for row in failures:
            lines.append(f"- {row['subject']}：未录入成功 —— {row['reason']}")
    else:
        lines.append("- 无")

    lines.append("")
    lines.append(f"（{stamp} 自动发送，共 {len(rows)} 条：成功 {ok_n} / 未录入 {bad_n}）")
    lines.append("")
    return "\n".join(lines)


# ── 发送 ───────────────────────────────────────────────────────────────────
def mail_account_settings() -> tuple[str, str, str]:
    """(himalaya 账户名, 发件地址, 收件地址)。收件地址默认 = 收信邮箱自己。"""
    account = "demochen"
    from_addr = "demochen@ghouseproducts.com"
    to_addr = from_addr
    try:
        import yaml  # 仓库依赖里已有
        cfg = yaml.safe_load(MAIL_CFG.read_text(encoding="utf-8")) or {}
        account = (cfg.get("account") or account).strip() or account
        to_addr = (cfg.get("report_to") or to_addr).strip() or to_addr
    except Exception as exc:  # 配置读不了就用默认值，别因为报表把流水线搞停
        print(f"[warn] 读 {MAIL_CFG} 失败，用默认账户 demochen：{exc}", file=sys.stderr)
    if account == "demochen":
        from_addr = "demochen@ghouseproducts.com"
    elif account == "sora":
        from_addr = "sora@handscript.net"
    return account, from_addr, to_addr


# 腾讯企业邮 + himalaya v1.2.0 实测坑（2026-11-20）：SMTP 已投递、APPEND 存「已发送」
# 也成功，但随后按 IMAP 回包找 UID 时失败 → 命令非零退出。这**不是**发送失败，
# 若当成失败去重试，Demo 会收到重复邮件（业务口径：不重发）。
BENIGN_ERR = "cannot find UID of appended IMAP message"


def send_raw(account: str, raw: str, timeout: int = 90) -> tuple[bool, str]:
    """himalaya message send 吃 stdin 的原始信。返回（是否成功，输出尾巴）。"""
    try:
        proc = subprocess.run(
            ["himalaya", "message", "send", "-a", account],
            input=raw, text=True, capture_output=True, timeout=timeout,
        )
    except FileNotFoundError:
        return False, "找不到 himalaya（PATH 里没有 ~/.local/bin？）"
    except subprocess.TimeoutExpired:
        return False, f"himalaya 超时 {timeout}s"
    output = (proc.stdout or "") + (proc.stderr or "")
    tail = "\n".join(output.strip().splitlines()[-6:])
    if proc.returncode == 0:
        return True, tail
    if BENIGN_ERR in output:
        return True, "已投递（himalaya 非零退出是 IMAP 取 UID 的老毛病，邮件其实发出了）"
    return False, tail


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="本轮 ERP 录入结果汇总邮件（一轮一封）")
    ap.add_argument("--dry-run", action="store_true", help="只打印邮件内容，不发送、不记台账")
    ap.add_argument("--init", action="store_true",
                    help="把现有回执全部登记为「已汇报」（首次上线用，避免把历史单全发一遍）")
    ap.add_argument("--to", default="", help="收件地址；默认 = assets/mail.yaml 的收信邮箱")
    ap.add_argument("--quiet", action="store_true", help="没东西可发时连一行提示都不打")
    ap.add_argument("--root", default="", help="回执根目录（默认 customer-files/processed，测试用）")
    ap.add_argument("--selftest", action="store_true",
                    help="只发一封通道自测信（不动台账），确认 SMTP 能不能出去")
    args = ap.parse_args(argv)

    account, from_addr, to_addr = mail_account_settings()
    if args.to:
        to_addr = args.to

    root = Path(args.root) if args.root else None

    if args.selftest:
        now = bjt_now()
        raw = "\n".join([
            f"From: {from_addr}",
            f"To: {to_addr}",
            "Subject: 【自测】报价提取回执 发送通道",
            "MIME-Version: 1.0",
            'Content-Type: text/plain; charset="utf-8"',
            "Content-Transfer-Encoding: 8bit",
            "",
            f"这是 scripts/round_report_mail.py 的通道自测信（{now.strftime('%Y-%m-%d %H:%M')} 北京时间）。",
            "收到即说明定时任务的「汇总回执」能正常发到这个邮箱。",
            "",
        ])
        ok, tail = send_raw(account, raw)
        print(("自测信已发送 → " if ok else "自测信发送失败 → ") + to_addr)
        if tail:
            print(tail)
        return 0 if ok else 1

    if args.init:
        rows, keys = collect(root)
        if keys:
            append_ledger(keys, "init_skip", "首次上线：历史回执登记为已汇报，不发送")
        print(f"[init] 登记 {len(keys)} 份历史回执为「已汇报」，未发送任何邮件")
        return 0

    rows, keys = collect(root)
    if not rows:
        if not args.quiet:
            print("本轮没有新的 ERP 录入结果 → 不发邮件")
        return 0

    raw = compose(rows, to_addr=to_addr, from_addr=from_addr)
    if args.dry_run:
        print(raw)
        print(f"[dry-run] 将发往 {to_addr}（账户 {account}），未发送、未记台账", file=sys.stderr)
        return 0

    ok, tail = send_raw(account, raw)
    # ★ 不管成败都登记：业务口径「不重发」。SMTP 已投递但存已发送失败时
    #   himalaya 也会非零退出，重试就是给 Demo 发第二封。
    append_ledger(keys, "sent" if ok else "send_failed", tail)
    if ok:
        print(f"已发送：{len(rows)} 条（发往 {to_addr}，账户 {account}）")
        return 0
    print(f"[warn] 发送失败，不重发（已在台账登记）：{tail}", file=sys.stderr)
    print(f"[warn] 本轮 {len(rows)} 条结果未送达邮箱，明细见 {LEDGER}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
