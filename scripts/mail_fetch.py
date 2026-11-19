#!/usr/bin/env python3
"""mail_fetch.py - 收信入口：把邮箱里的**新询价信**变成 `pending/*.eml`

定位（业务 2026-11-13 拍板：「收信接入流水线」）：

    IMAP ──himalaya──▶ pending/<日期>_<主题>.eml ──scan_pending──▶ ingest_email
                                                                    │
                                                     附件/正文 → 任务 → main.py

**刻意只做一件事**：落 `.eml` 到 `pending/`。展开邮件、抽字段、建任务全部交给
现有链路（`scan_pending.py` 见到 `.eml` 就会调 `ingest_email.py`）。这样收信方式
换掉（IMAP→API/转发）也不影响下游。

设计取舍：
* **不改邮箱状态**（不标已读、不移动）—— 人还要在手机上看信。去重靠本地台账
  `customer-files/.mail_fetch_state.json`（Message-ID 为主键，指纹做前置过滤）。
* **安静**是硬要求：没有新信时**一个字都不打印**（cron 每 5 分钟跑一次）。
* **失败不阻断扫描**：异常只写 stderr，退出码非 0 由调用方 `|| true` 吞掉。

用法：
    uv run ./scripts/mail_fetch.py --dry-run --verbose   # 看它想拉什么
    uv run ./scripts/mail_fetch.py                       # 真拉（写 pending/）
"""
from __future__ import annotations

import argparse
import fcntl
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
from pathlib import Path
from typing import Any, Callable, Optional

try:  # PyYAML 是仓库既有依赖；缺失也不至于让收信停摆
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

REPO_ROOT = Path(__file__).resolve().parent.parent
PENDING_DIR = REPO_ROOT / "customer-files" / "pending"
STATE_PATH = REPO_ROOT / "customer-files" / ".mail_fetch_state.json"
LOCK_PATH = REPO_ROOT / "customer-files" / ".mail_fetch.lock"
CONFIG_PATH = REPO_ROOT / "assets" / "mail.yaml"

STATE_VERSION = 1

# 兜底配置：配置文件缺失/缺键时用这套。
DEFAULT_CONFIG: dict[str, Any] = {
    "account": "",                       # himalaya 账户名；空 = 默认账户
    "folder": "INBOX",
    "page_size": 50,                     # 每轮看多少封信封（不是拉多少封）
    "max_per_run": 5,                    # 每轮最多**落盘**几封，防止一次爆量
    "himalaya": "himalaya",              # 二进制路径（PATH 里找不到时给绝对路径）
    "timeout": 90,
    "skip_senders": [
        "10000@exmail.weixin.qq.com",    # 企业微信登录提醒
        "mailer-daemon@",
        "postmaster@",
    ],
    "skip_subjects": [
        "企业微信邮箱登录提醒",
        "已设置将邮件转发",          # 腾讯企业邮「转发设置」系统通知（无产品字段）
        "已设置自动转发",
    ],
    # 自家域名：发件人在这些域名、而 To/Cc 里有外部收件人 = 我们外发的回信，不是询价。
    # 留空 = 不启用方向判断（保持旧行为）。
    "self_domains": [],
}


class HimalayaError(RuntimeError):
    """himalaya 调不通 / 返回非 0。"""


@dataclass
class Envelope:
    id: str
    subject: str = ""
    sender_addr: str = ""
    sender_name: str = ""
    date: str = ""
    flags: list[str] = field(default_factory=list)
    has_attachment: bool = False

    @property
    def sender(self) -> str:
        if self.sender_name and self.sender_addr:
            return f"{self.sender_name} <{self.sender_addr}>"
        return self.sender_addr or self.sender_name

    @property
    def date_compact(self) -> str:
        """`2026-11-04 15:34+08:00` → `20261104`；解析不了就用今天。"""
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", self.date or "")
        return f"{m.group(1)}{m.group(2)}{m.group(3)}" if m else f"{datetime.now():%Y%m%d}"


class HimalayaClient:
    """himalaya CLI 的薄封装（stdlib subprocess，不加依赖）。"""

    def __init__(self, binary: str = "himalaya", account: str = "",
                 folder: str = "INBOX", timeout: int = 90):
        self.binary = shutil.which(binary) or binary
        self.account = account
        self.folder = folder
        self.timeout = timeout

    # ── 内部 ────────────────────────────────────────────────────────
    def _cmd(self, args: list[str]) -> list[str]:
        """拼命令行。himalaya 1.2 的 `--account` 是**子命令级**选项（不是全局），
        必须跟在子命令后面：`himalaya envelope list --account sora …`，
        放最前面会直接 `unexpected argument '--account'`。"""
        cmd = [self.binary, *args]
        if self.account:
            cmd += ["--account", self.account]
        return cmd

    def _run(self, args: list[str]) -> str:
        cmd = self._cmd(args)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=self.timeout)
        except FileNotFoundError as exc:
            raise HimalayaError(f"找不到 himalaya 可执行文件: {self.binary}") from exc
        except subprocess.TimeoutExpired as exc:
            raise HimalayaError(f"himalaya 超时（{self.timeout}s）: {' '.join(cmd)}") from exc
        if proc.returncode != 0:
            err = (proc.stderr or "").strip() or (proc.stdout or "").strip()
            raise HimalayaError(f"himalaya 返回 {proc.returncode}: {err[:500]}")
        return proc.stdout

    # ── 对外 ────────────────────────────────────────────────────────
    def list_envelopes(self, page_size: int = 50) -> list[Envelope]:
        out = self._run(["envelope", "list", "--folder", self.folder,
                         "--page-size", str(page_size), "--output", "json"])
        try:
            raw = json.loads(out or "[]")
        except json.JSONDecodeError as exc:
            raise HimalayaError(f"envelope list 不是合法 JSON: {out[:200]}") from exc
        envelopes: list[Envelope] = []
        for item in raw:
            sender = item.get("from") or {}
            envelopes.append(Envelope(
                id=str(item.get("id", "")),
                subject=str(item.get("subject") or ""),
                sender_addr=str(sender.get("addr") or ""),
                sender_name=str(sender.get("name") or ""),
                date=str(item.get("date") or ""),
                flags=list(item.get("flags") or []),
                has_attachment=bool(item.get("has_attachment")),
            ))
        return envelopes

    def export_full(self, envelope: Envelope, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            dest.unlink()
        self._run(["message", "export", envelope.id, "--folder", self.folder,
                   "--full", "--destination", str(dest)])
        if not dest.exists():
            raise HimalayaError(f"导出失败，文件没落地: {dest}")
        return dest


# ── 配置 / 状态 ────────────────────────────────────────────────────────

def load_config(path: Optional[Path] = None) -> dict[str, Any]:
    """配置 = DEFAULT_CONFIG ← assets/mail.yaml 覆盖（只覆盖出现过的键）。"""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    cfg_path = Path(path) if path else CONFIG_PATH
    if cfg_path and cfg_path.exists() and yaml is not None:
        try:
            loaded = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except Exception as exc:  # 配置写坏了不能把收信整个打死
            print(f"!! 读不了 {cfg_path}: {exc}（用默认配置继续）", file=sys.stderr)
            loaded = {}
        if isinstance(loaded, dict):
            cfg.update({k: v for k, v in loaded.items() if v is not None})
    return cfg


def read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": STATE_VERSION, "seen": {}, "fingerprints": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": STATE_VERSION, "seen": {}, "fingerprints": {}}
    data.setdefault("version", STATE_VERSION)
    data.setdefault("seen", {})
    data.setdefault("fingerprints", {})
    return data


def write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# ── 过滤 / 命名 ────────────────────────────────────────────────────────

def fingerprint(env: Envelope) -> str:
    """前置指纹：不下载正文就能判「这封看过没」。"""
    return f"{env.sender_addr.lower()}|{env.date}|{env.subject}"


def skip_reason(env: Envelope, cfg: dict[str, Any]) -> str:
    """纯信封阶段的跳过判定（不下载正文，只看 From / Subject）。

    三类接口（都在 `assets/mail.yaml` 里配，改配置不需要动代码）：

    | 键 | 匹配对象 | 方式 |
    |---|---|---|
    | `skip_senders` | 发件人地址 | 子串，**不分大小写** |
    | `skip_subjects` | 标题 | 子串，**不分大小写**（2026-11-18 起；之前区分大小写） |
    | `skip_subject_regex` | 标题 | 正则，`re.search` + `IGNORECASE` |

    ★ 为什么加正则这一层：子串拦不住同一类通知的**变体**。腾讯企业邮/Outlook 的
      自动回复在德文客户端里是 `Automatische Antwort:`／`Abwesenheitsnotiz:`，
      在中文客户端里是 `自动回复:`，再叠上 `AW:` / `Re:` / `回复:` 前缀与全角冒号，
      逐个堆子串会一直漏。正则一条就覆盖。
    """
    addr = (env.sender_addr or "").lower()
    for pat in cfg.get("skip_senders") or []:
        if pat and str(pat).lower() in addr:
            return f"发件人命中跳过名单（{pat}）"
    subject = env.subject or ""
    for pat in cfg.get("skip_subjects") or []:
        if pat and str(pat).lower() in subject.lower():
            return f"主题命中跳过名单（{pat}）"
    for pat in cfg.get("skip_subject_regex") or []:
        if not pat:
            continue
        try:
            hit = re.search(str(pat), subject, re.IGNORECASE)
        except re.error as exc:      # 配置写错不能让整轮收信挂掉
            print(f"  ! 跳过规则正则无效，已忽略：{pat}（{exc}）")
            continue
        if hit:
            return f"主题命中跳过正则（{pat}）"
    return ""


_SLUG_BAD = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def safe_eml_name(env: Envelope, max_len: int = 60) -> str:
    stem = _SLUG_BAD.sub(" ", env.subject or "").strip()
    stem = re.sub(r"\s+", " ", stem)[:max_len].strip() or "email"
    return f"{env.date_compact}_{stem}.eml"


def unique_path(directory: Path, name: str) -> Path:
    target = directory / name
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    n = 2
    while True:
        candidate = directory / f"{stem}_{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def message_id_of(eml_path: Path) -> str:
    try:
        with open(eml_path, "rb") as fh:
            msg = BytesParser(policy=policy.default).parse(fh)
        return (msg.get("Message-ID") or "").strip()
    except Exception:
        return ""


# ── 外发邮件识别（收信侧的「方向」判断）────────────────────────────────

def _domain_of(addr: str) -> str:
    return (addr or "").rsplit("@", 1)[-1].strip().strip(">").strip().lower()


def _is_self_domain(domain: str, self_domains: list[str]) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in self_domains if d)


def outbound_reason(eml_path: Path, cfg: dict[str, Any]) -> str:
    """判断这封是不是**我们自己外发的回信**（不是客户询价）。

    ★ 2026-11-17 实测：邮箱把本公司发出的回信也收进来了 —— `Re: PO16528 …`、
      `Re: AW: Lidl 10-2026 …`、`Re: KHDD26100021 …`、`Ndeues Möbel …` 四封
      被当成询价抽成空单（正文全是进度往来 / ozalid 通知，没有任何询价字段）。

    **不能简单按「发件人是我方域名」全砍**：同事把客户询价转发给业务
    （`Fw: WG: Anfrage …` → `marklin@ghouseproducts.com`）发件人也是我方域名，
    那是**真询价**，必须保留。

    判据（方向）：From 全在自家域名 + `To`/`Cc` 里**存在外部收件人** → 外发。
    转发件的收件人全在自家域内，因此不会误砍。
    """
    self_domains = [str(d).lower().strip().lstrip("@")
                    for d in (cfg.get("self_domains") or []) if str(d).strip()]
    if not self_domains:
        return ""
    try:
        with open(eml_path, "rb") as fh:
            msg = BytesParser(policy=policy.default).parse(fh)
    except Exception:
        return ""

    senders = [a for _n, a in getaddresses(msg.get_all("From") or []) if a]
    if not senders:
        return ""
    if not all(_is_self_domain(_domain_of(a), self_domains) for a in senders):
        return ""          # 发件人在外部 → 正常收信，放行

    rcpts = []
    for header in ("To", "Cc"):
        rcpts += [a for _n, a in getaddresses(msg.get_all(header) or []) if a]
    external = [r for r in rcpts if not _is_self_domain(_domain_of(r), self_domains)]
    if not external:
        return ""          # 收件人全在自家域内 = 内部转发，保留
    return f"外发邮件（我方 → {external[0]}），不是客户询价"


# ── 主流程 ────────────────────────────────────────────────────────────

def fetch(client: Any, pending_dir: Path, state_path: Path, cfg: dict[str, Any],
          limit: Optional[int] = None, dry_run: bool = False,
          logger: Callable[[str], None] = print) -> dict[str, Any]:
    """扫一遍邮箱，把没见过的信落成 `pending/*.eml`。

    返回 `{"ingested": [...], "skipped": [...], "errors": [...], "eml": [...]}`
    """
    pending = Path(pending_dir)
    max_per_run = int(limit or cfg.get("max_per_run") or 5)
    page_size = int(cfg.get("page_size") or 50)

    envelopes = client.list_envelopes(page_size=page_size)
    state = read_state(Path(state_path))
    seen: dict[str, Any] = state["seen"]
    prints: dict[str, str] = state["fingerprints"]

    result: dict[str, Any] = {"ingested": [], "skipped": [], "errors": [], "eml": []}
    ingested_now = 0

    for env in envelopes:
        if ingested_now >= max_per_run:
            result["skipped"].append({"id": env.id, "subject": env.subject,
                                      "reason": f"本轮已达上限 {max_per_run} 封"})
            continue

        reason = skip_reason(env, cfg)
        if reason:
            result["skipped"].append({"id": env.id, "subject": env.subject, "reason": reason})
            if not dry_run:
                # 记指纹，下次连过滤都不用跑
                prints[fingerprint(env)] = ""
            logger(f"  · 跳过 [{env.id}] {env.subject[:40]} — {reason}")
            continue

        fp = fingerprint(env)
        if fp in prints:
            result["skipped"].append({"id": env.id, "subject": env.subject,
                                      "reason": "已处理过（指纹命中）"})
            continue

        if dry_run:
            result["ingested"].append({"id": env.id, "subject": env.subject,
                                       "sender": env.sender, "dry_run": True})
            logger(f"  + 会拉 [{env.id}] {env.sender} / {env.subject[:50]}")
            ingested_now += 1
            continue

        pending.mkdir(parents=True, exist_ok=True)
        target = unique_path(pending, safe_eml_name(env))
        try:
            client.export_full(env, target)
        except HimalayaError as exc:
            result["errors"].append({"id": env.id, "subject": env.subject,
                                     "error": str(exc)})
            logger(f"  ! 导出失败 [{env.id}] {env.subject[:40]}: {exc}")
            continue

        msgid = message_id_of(target)
        dedupe_key = msgid or fp

        # ★ 2026-11-17：方向判断 —— 我们自己外发的回信不进流水线。
        #   必须放在**下载之后**（信封里没有 To/Cc），也放在 Message-ID 去重之前，
        #   这样外发邮件同样记进台账，不会每轮重复下载。
        out_reason = outbound_reason(target, cfg)
        if out_reason:
            target.unlink(missing_ok=True)
            prints[fp] = msgid
            if dedupe_key:
                seen[dedupe_key] = {
                    "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "eml": "",
                    "from": env.sender,
                    "subject": env.subject,
                    "date": env.date,
                    "outbound": True,
                }
            result["skipped"].append({"id": env.id, "subject": env.subject,
                                      "reason": out_reason})
            logger(f"  · 跳过 [{env.id}] {env.subject[:40]} — {out_reason}")
            continue

        if msgid and msgid in seen:
            target.unlink(missing_ok=True)
            prints[fp] = msgid
            result["skipped"].append({"id": env.id, "subject": env.subject,
                                      "reason": f"已处理过（Message-ID {msgid[:24]}…）"})
            logger(f"  · 跳过 [{env.id}] 已入过队（Message-ID 命中）")
            continue

        seen[dedupe_key] = {
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "eml": target.name,
            "from": env.sender,
            "subject": env.subject,
            "date": env.date,
        }
        prints[fp] = msgid
        ingested_now += 1
        result["ingested"].append({"id": env.id, "subject": env.subject,
                                   "sender": env.sender, "eml": target.name})
        result["eml"].append(str(target))
        logger(f"  + 已落盘 {target.name}（{env.sender} / {env.subject[:40]}）")

    if not dry_run and (result["ingested"] or result["skipped"]):
        write_state(Path(state_path), state)
    return result


def mark_baseline(client: Any, state_path: Path, cfg: dict[str, Any],
                  logger: Callable[[str], None] = print) -> int:
    """把邮箱里**现有的**信全部记为已处理，但不入队。

    上线第一天用：邮箱里躺着历史邮件（已经人工处理过的），直接开跑会把它们
    再灌一遍队列、重复建单。跑一次 `--mark-baseline`，之后只有**新到的信**进流水线。
    """
    state = read_state(Path(state_path))
    envelopes = client.list_envelopes(page_size=int(cfg.get("page_size") or 50))
    for env in envelopes:
        state["fingerprints"][fingerprint(env)] = ""
        state["seen"][fingerprint(env)] = {
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "eml": "",
            "from": env.sender,
            "subject": env.subject,
            "date": env.date,
            "baseline": True,
        }
        logger(f"  = 基线 [{env.id}] {env.sender} / {env.subject[:50]}")
    write_state(Path(state_path), state)
    return len(envelopes)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="从邮箱拉新询价信 → pending/*.eml")
    ap.add_argument("--dry-run", action="store_true", help="只看会拉什么，不写文件")
    ap.add_argument("--verbose", action="store_true", help="打印每封的处理决定")
    ap.add_argument("--mark-baseline", action="store_true",
                    help="把邮箱现有信件记为已处理（不入队），之后只拉新到的")
    ap.add_argument("--limit", type=int, default=0, help="本轮最多落几封（覆盖配置）")
    ap.add_argument("--account", default="", help="himalaya 账户名（默认取配置）")
    ap.add_argument("--folder", default="", help="文件夹（默认取配置 INBOX）")
    ap.add_argument("--config", default="", help=f"配置文件（默认 {CONFIG_PATH}）")
    ap.add_argument("--state", default="", help="去重台账路径")
    ap.add_argument("--pending", default="", help="落盘目录（默认 customer-files/pending）")
    ap.add_argument("--json", action="store_true", help="输出 JSON 汇总")
    args = ap.parse_args(argv)

    cfg = load_config(Path(args.config) if args.config else None)
    if args.account:
        cfg["account"] = args.account
    if args.folder:
        cfg["folder"] = args.folder

    state_path = Path(args.state) if args.state else STATE_PATH
    pending_dir = Path(args.pending) if args.pending else PENDING_DIR

    # 与上一轮互斥：拿不到锁就安静退出（scan 那边有同样的规矩）
    lock_path = LOCK_PATH if not args.state else state_path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fh = open(lock_path, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        if args.verbose:
            print("上一轮收信还在跑，跳过本轮")
        return 0

    client = HimalayaClient(
        binary=str(cfg.get("himalaya") or "himalaya"),
        account=str(cfg.get("account") or ""),
        folder=str(cfg.get("folder") or "INBOX"),
        timeout=int(cfg.get("timeout") or 90),
    )
    logger = print if (args.verbose or args.dry_run or args.json or args.mark_baseline) \
        else (lambda _m: None)

    if args.mark_baseline:
        try:
            n = mark_baseline(client, state_path, cfg, logger=logger)
        except HimalayaError as exc:
            print(f"!! 收信失败: {exc}", file=sys.stderr)
            return 2
        print(f"✅ 已把现有 {n} 封信记为基线（不入队）；之后只有新到的信进流水线")
        return 0

    try:
        result = fetch(client, pending_dir, state_path, cfg,
                       limit=args.limit or None, dry_run=args.dry_run, logger=logger)
    except HimalayaError as exc:
        print(f"!! 收信失败: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.dry_run and result["ingested"]:
        print(f"（dry-run）会拉 {len(result['ingested'])} 封，未落盘")
    elif result["ingested"]:
        n = len(result["ingested"])
        names = "、".join(Path(p).name for p in result["eml"][:3])
        more = "" if n <= 3 else f" 等 {n} 封"
        print(f"📥 收到 {n} 封新询价邮件 → pending/（{names}{more}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
