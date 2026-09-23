#!/usr/bin/env python3
"""
ingest_email.py - 邮件入口：把「客户邮件」变成「待处理任务」

负责三件事：
  1. 把邮件附件落到 `customer-files/pending/`
  2. 解析邮件**标题 / 正文**里的**客户单号**（ERP 字段 `KHDH`）
  3. 给每个附件写一个 sidecar（`<附件名>.meta.json`），把标题/正文/单号/识别到的客户
     一起存下来 —— main.py 处理该附件时会自动读取，不用人工重新输入

两种投递方式（都不依赖具体收件方式，谁都能调）：

  # A. 有 .eml 文件（浏览器导出 / 邮件客户端保存 / 任何 IMAP 脚本落盘）
  uv run ./scripts/ingest_email.py --eml mail.eml

  # B. 只有纯文本（Hermes 邮件工具、剪贴板、其它系统推送）
  uv run ./scripts/ingest_email.py \
      --subject "PO 23992 / 23993 - PUZZLE COLOUR DISNEY" \
      --body "Hi, please quote the attached. Works References: 23992 / 23993" \
      --attach "/tmp/PUZZLE COLOUR DISNEY.xlsx"

  # 也可以指向一个已有目录，把所有文件当成附件投递
  uv run ./scripts/ingest_email.py --subject "..." --body "..." --attach-dir /path/to/dir

`KHDH` 的取值优先级（业务确认 2026-09-23：来源 = 客户邮件内容或附件）：
    档案 email.khdh.subject_patterns（标题）→ email.khdh.body_patterns（正文）
    → 附件里的列（email.khdh.attachment_keys，由 main.py 处理附件时兜底）

输出：stdout 一份 JSON 汇总（供 agent/上层程序读取）。

⚠️ 尚未实现：IMAP 直连收信。需要先确认公司的收信方式（IMAP 服务器 / 转发到本地 /
   邮箱 API），再加一个「取信 → 落 .eml → 调本脚本」的适配层。本脚本刻意做成
   传输无关，就是为了这一步不返工。
"""
from __future__ import annotations

import argparse
import base64
import email
import json
import re
import shutil
import sys
from datetime import datetime
from email import policy
from html import unescape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from customer_profile import resolve_profile  # noqa: E402
from extract_fields import load_customer_data  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
PENDING_DIR = REPO_ROOT / "customer-files" / "pending"
META_SUFFIX = ".meta.json"
SUPPORTED_ATTACHMENTS = (".xlsx", ".xls", ".csv", ".pdf", ".docx", ".txt")


# ── 邮件解析 ─────────────────────────────────────────────────────────────
def html_to_text(html: str) -> str:
    """极简 HTML → 文本（够用为止：去 script/style、去标签、还原实体）。"""
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", html)
    html = re.sub(r"<[^>]+>", " ", html)
    return unescape(re.sub(r"[ \t]+", " ", html))


def parse_eml(path: Path) -> dict:
    """解析 .eml → {subject, sender, date, body, attachments:[(name, bytes)]}"""
    with open(path, "rb") as f:
        msg = email.message_from_bytes(f.read(), policy=policy.default)

    body_parts = []
    attachments = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        disposition = str(part.get("Content-Disposition") or "")
        filename = part.get_filename()
        if "attachment" in disposition.lower() or (filename and not disposition):
            if not filename:
                continue
            payload = part.get_payload(decode=True) or b""
            attachments.append((filename, payload))
            continue
        if part.get_content_type() == "text/plain":
            body_parts.append(part.get_content())
        elif part.get_content_type() == "text/html":
            body_parts.append(html_to_text(part.get_content()))

    return {
        "subject": str(msg.get("Subject") or "").strip(),
        "sender": str(msg.get("From") or "").strip(),
        "date": str(msg.get("Date") or "").strip(),
        "body": "\n".join(p for p in body_parts if p).strip(),
        "attachments": attachments,
    }


# ── 客户单号 KHDH ────────────────────────────────────────────────────────
def normalize_khdh(value: str) -> str:
    """归一化客户单号：压缩空白，统一分隔符两侧空格（23992/23993 → 23992 / 23993）。"""
    s = re.sub(r"\s+", " ", str(value or "")).strip()
    s = re.sub(r"\s*([/,&])\s*", r" \1 ", s)
    return re.sub(r"\s+", " ", s).strip()


def extract_khdh(profile, subject: str, body: str, logger=None) -> tuple:
    """按档案的 email.khdh 规则从标题/正文取客户单号。返回 (值, 来源说明)。"""
    khdh_cfg = (profile.email.get("khdh") or {})
    for section, label, text in (
        ("subject_patterns", "邮件标题", subject),
        ("body_patterns", "邮件正文", body),
    ):
        if not text:
            continue
        for pattern in khdh_cfg.get(section) or []:
            try:
                match = re.search(pattern, text)
            except re.error:
                continue
            if match:
                value = normalize_khdh(match.group(1))
                if value:
                    if logger:
                        logger.info(f"客户单号来源: {label}（{profile.id} 规则）")
                    return value, f"{label}（{profile.id} 规则）"
    return "", ""


# ── 投递 ─────────────────────────────────────────────────────────────────
def _unique_path(directory: Path, name: str) -> Path:
    target = directory / name
    counter = 1
    stem, suffix = Path(name).stem, Path(name).suffix
    while target.exists():
        target = directory / f"{stem}__{counter}{suffix}"
        counter += 1
    return target


def write_meta(attachment_path: Path, meta: dict) -> Path:
    """把邮件上下文写到附件旁边，main.py 会读它。"""
    meta_path = attachment_path.with_name(attachment_path.name + META_SUFFIX)
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    return meta_path


def _refine_by_attachment(profile, saved_paths: list, subject: str, logger=None):
    """只靠标题/正文/文件名认不出客户时，读**附件内容**再判一次。

    报价单的文件内容里通常有客户的招牌栏目（如 igl 的
    `Works References For all Titles`），比文件名可靠得多。
    """
    from customer_profile import DEFAULTS_ID, auto_detect

    if profile.id != DEFAULTS_ID:
        return profile
    for path in saved_paths:
        try:
            data = load_customer_data(str(path), profile, logger=None)
        except Exception:
            continue
        candidates = auto_detect(data.text_blocks, filename=Path(path).name, subject=subject)
        if candidates:
            if logger:
                logger.info(f"按附件内容识别客户: {candidates[0][0].id}"
                            f"（得分 {candidates[0][1]}）")
            return candidates[0][0]
    return profile


def ingest(
    attachments: list,
    subject: str,
    body: str,
    sender: str,
    customer: str,
    pending_dir: Path,
    logger=None,
) -> dict:
    """把附件落到 pending 并写 sidecar。attachments: [(文件名, bytes)]"""
    pending_dir.mkdir(parents=True, exist_ok=True)

    probe_texts = [subject, body] + [name for name, _ in attachments]
    profile, source = resolve_profile(
        customer, texts=probe_texts, filename=" ".join(name for name, _ in attachments),
        sender=sender, subject=subject, logger=logger,
    )

    # 先落盘，再用附件内容把客户身份判准（认不出时才有意义）
    saved_paths = []
    skipped = []
    for name, payload in attachments:
        suffix = Path(name).suffix.lower()
        if suffix not in SUPPORTED_ATTACHMENTS:
            if logger:
                logger.warn(f"跳过不支持的附件类型: {name}")
            skipped.append(name)
            continue
        target = _unique_path(pending_dir, Path(name).name)
        with open(target, "wb") as f:
            f.write(payload)
        saved_paths.append(target)

    refined = _refine_by_attachment(profile, saved_paths, subject, logger)
    if refined.id != profile.id:
        profile = refined
        source = "自动识别（附件内容）"
        if not profile.is_calibrated and logger:
            logger.warn(f"档案 {profile.id} 状态为 {profile.status} —— 未经校准，字段值需人工核对")

    subject_khdh, subject_src = extract_khdh(profile, subject, body, logger)

    saved = []
    for target in saved_paths:
        meta = {
            "source": "email",
            "ingested_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "subject": subject,
            "sender": sender,
            "body": body,
            "customer_profile": profile.id,
            "customer_source": source,
            "khdh": subject_khdh,
            "khdh_source": subject_src,
            "attachment_original_name": target.name,
        }
        meta_path = write_meta(target, meta)
        if logger:
            logger.info(f"附件已投递: {target.name} -> 副档 {meta_path.name}")
        saved.append({"file": str(target), "meta": str(meta_path), "original_name": target.name})

    # 即使没有附件也留一份邮件记录（正文/单号不能丢）
    email_record = pending_dir / f"_email_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(email_record, "w", encoding="utf-8") as f:
        json.dump({
            "source": "email",
            "ingested_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "subject": subject,
            "sender": sender,
            "body": body,
            "customer_profile": profile.id,
            "customer_source": source,
            "khdh": subject_khdh,
            "khdh_source": subject_src,
            "attachments": [s["original_name"] for s in saved],
            "skipped_attachments": skipped,
        }, f, ensure_ascii=False, indent=2)

    return {
        "customer_profile": profile.id,
        "customer_source": source,
        "khdh": subject_khdh,
        "khdh_source": subject_src,
        "attachments": saved,
        "skipped_attachments": skipped,
        "email_record": str(email_record),
    }


def read_attachment_meta(attachment_path: Path) -> dict:
    """main.py 用：读取某个附件的邮件副档（不存在返回 {}）。"""
    meta_path = Path(attachment_path).with_name(Path(attachment_path).name + META_SUFFIX)
    if not meta_path.exists():
        return {}
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


# ── CLI ──────────────────────────────────────────────────────────────────
class _Logger:
    def info(self, message, *a):
        print(f"[INFO] {message}")

    def warn(self, message, *a):
        print(f"[WARN] {message}", file=sys.stderr)

    def error(self, message, *a):
        print(f"[ERROR] {message}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="邮件 → 待处理任务（附件 + 客户单号）")
    parser.add_argument("--eml", help=".eml 文件路径")
    parser.add_argument("--subject", default="", help="邮件标题（无 .eml 时用）")
    parser.add_argument("--body", default="", help="邮件正文（无 .eml 时用）")
    parser.add_argument("--sender", default="", help="发件人（可选，用于识别客户）")
    parser.add_argument("--attach", action="append", default=[], help="附件路径，可重复")
    parser.add_argument("--attach-dir", help="把该目录下所有支持的文件当附件投递")
    parser.add_argument("--customer", "-c", default="auto", help="客户 id（默认自动识别）")
    parser.add_argument("--pending-dir", default=str(PENDING_DIR), help="投递目录")
    parser.add_argument("--json", action="store_true", help="只输出 JSON 汇总")
    args = parser.parse_args()

    logger = _Logger()
    attachments = []
    subject, body, sender = args.subject, args.body, args.sender

    if args.eml:
        eml_path = Path(args.eml)
        if not eml_path.exists():
            print(f"文件不存在: {eml_path}", file=sys.stderr)
            return 1
        parsed = parse_eml(eml_path)
        subject = subject or parsed["subject"]
        body = body or parsed["body"]
        sender = sender or parsed["sender"]
        attachments.extend(parsed["attachments"])
        logger.info(f"解析 .eml: {eml_path.name}（附件 {len(parsed['attachments'])} 个）")

    for raw in args.attach:
        path = Path(raw)
        if not path.exists():
            logger.warn(f"附件不存在，跳过: {path}")
            continue
        if path.suffix.lower() not in SUPPORTED_ATTACHMENTS:
            logger.warn(f"不支持的附件类型，跳过: {path.name}")
            continue
        attachments.append((path.name, path.read_bytes()))

    if args.attach_dir:
        directory = Path(args.attach_dir)
        if not directory.is_dir():
            print(f"目录不存在: {directory}", file=sys.stderr)
            return 1
        for path in sorted(directory.iterdir()):
            if path.is_file() and path.suffix.lower() in SUPPORTED_ATTACHMENTS:
                attachments.append((path.name, path.read_bytes()))

    if not attachments and not subject and not body:
        print("没有可投递的内容：请给 --eml，或 --subject/--body/--attach", file=sys.stderr)
        return 1

    result = ingest(
        attachments=attachments,
        subject=subject,
        body=body,
        sender=sender,
        customer=args.customer,
        pending_dir=Path(args.pending_dir),
        logger=logger,
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print()
        print(f"客户档案: {result['customer_profile']}（{result['customer_source']}）")
        print(f"客户单号 KHDH: {result['khdh'] or '(未从标题/正文取到 —— 将由附件列兜底)'}"
              f"{'  来源: ' + result['khdh_source'] if result['khdh_source'] else ''}")
        print(f"投递附件: {len(result['attachments'])} 个")
        for item in result["attachments"]:
            print(f"  - {item['original_name']}  →  {item['file']}")
        print(f"邮件记录: {result['email_record']}")
        print()
        print("下一步: uv run ./scripts/main.py <上面任一附件> --customer auto")
        print("        （main.py 会自动读副档里的邮件上下文与客户单号）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
