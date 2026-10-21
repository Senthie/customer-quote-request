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


def html_tables_to_text(html: str) -> str:
    """把 HTML 表格压成 `标签: 值` 行 —— 报价需求常把规格写在表格里。

    只处理「一行两列」这种 label/value 形态（客户邮件最常见），其余行按 `|` 拼接，
    交给 text 解析器当自由文本扫。纯文本正文缺失时，这是唯一的规格来源。
    """
    out: list[str] = []
    for table in re.findall(r"(?is)<table.*?</table>", html):
        for row in re.findall(r"(?is)<tr.*?</tr>", table):
            cells = [
                re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", cell))).strip()
                for cell in re.findall(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>", row)
            ]
            cells = [c for c in cells if c]
            if not cells:
                continue
            if len(cells) == 2 and cells[0].rstrip().endswith((":", "：")):
                out.append(f"{cells[0].strip()} {cells[1]}")
            else:
                out.append(" | ".join(cells))
    return "\n".join(out).strip()


def parse_eml(path: Path) -> dict:
    """解析 .eml → {subject, sender, date, body, body_plain, body_html, attachments}

    body 取值顺序：text/plain 正文 → HTML 转文本 → HTML 表格压平结果。
    附件只认真正的 `Content-Disposition: attachment`；内嵌图片（logo/签名）不落盘。
    """
    with open(path, "rb") as f:
        msg = email.message_from_bytes(f.read(), policy=policy.default)

    plain_parts: list[str] = []
    html_parts: list[str] = []
    attachments = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        disposition = str(part.get("Content-Disposition") or "").lower()
        filename = part.get_filename()
        if "attachment" in disposition or (filename and not disposition):
            # inline 图片（logo、签名图）不是报价材料，跳过
            if "inline" in disposition and part.get_content_maintype() == "image":
                continue
            if not filename:
                continue
            payload = part.get_payload(decode=True) or b""
            attachments.append((filename, payload))
            continue
        if part.get_content_type() == "text/plain":
            plain_parts.append(part.get_content())
        elif part.get_content_type() == "text/html":
            html_parts.append(part.get_content())

    plain = "\n".join(p for p in plain_parts if p).strip()
    html_raw = "\n".join(html_parts)
    html_text = html_to_text(html_raw).strip() if html_raw else ""
    tables = html_tables_to_text(html_raw) if html_raw else ""
    body = plain or html_text or tables
    if tables and tables not in body:
        # 正文是纯文本、规格却在表格里时，追加（重复的会被 text 解析器后写覆盖）
        body = f"{body}\n\n{tables}".strip()

    return {
        "subject": str(msg.get("Subject") or "").strip(),
        "sender": str(msg.get("From") or "").strip(),
        "date": str(msg.get("Date") or "").strip(),
        "body": body,
        "body_plain": plain,
        "body_html": html_text,
        "body_tables": tables,
        "attachments": attachments,
    }


# ── 客户单号 KHDH ────────────────────────────────────────────────────────
def normalize_khdh(value: str) -> str:
    """归一化客户单号：压缩空白，统一分隔符两侧空格（23992/23993 → 23992 / 23993）。"""
    s = re.sub(r"\s+", " ", str(value or "")).strip()
    s = re.sub(r"\s*([/,&])\s*", r" \1 ", s)
    return re.sub(r"\s+", " ", s).strip()


# 引用/转发旧邮件的分界线：从第一个命中处往后都是**上一封**的内容。
#   Outlook 中文「发件人：」/ 英文「From:」、Gmail「-----Original Message-----」、
#   德文「Am 28.07.2026 um 17:02 schrieb …」、Outlook 的分隔横线。
_QUOTE_MARKERS = (
    r"(?mi)^\s*-{2,}\s*original message\s*-{2,}",
    r"(?mi)^\s*from:\s*.+$",
    r"(?mi)^\s*发件人\s*[:：]",
    r"(?mi)^\s*寄件者\s*[:：]",
    r"(?mi)^\s*am\s.+\sschrieb\s.+$",
    r"(?mi)^_{10,}\s*$",
)


# 「参考/同上」的上下文：出现在单号前 40 字里 → 那个号是**引用旧单**，不是本单。
#   实测原话：`Type B … spec same as PO#15900, type C`（LG26090401 的往来邮件里，
#   15900 是上一个项目的 PO）。取成本单 KHDH 就是错的。
_REFERENCE_BEFORE = re.compile(
    r"(?i)(?:same\s+as|as\s+(?:in|per)|refer(?:ence|ring)?\s+to|参考|參照|参见|参照|"
    r"wie\s+(?:in|bei)|siehe|laut|per\s+PO|according\s+to)\s*[:#]?\s*$")


def _is_reference(text: str, start: int) -> bool:
    """这个匹配前面是不是「参考/同上 …」的语气（是 → 引用旧单，别当本单事实）。"""
    return bool(_REFERENCE_BEFORE.search(text[max(0, start - 40):start]))


def newest_message(body: str) -> str:
    """只留**最新一封**邮件（切掉引用的旧邮件）。

    为什么必须切（2026-10-21 LG26090401 实测踩到）：业务给的 .eml 里带着整串往来，
    旧邮件写着上一个项目的 `6 titles 4 languages` 和 `spec same as PO#15900`。
    全文搜「单号 / 款数」就会把 **旧单的 PO 号** 落成 KHDH（15900），
    把本单的 10 款 10 语言盖掉。旧邮件不是本单事实。
    """
    text = body or ""
    cut = len(text)
    for pattern in _QUOTE_MARKERS:
        for m in re.finditer(pattern, text):
            if m.start() < cut:
                cut = m.start()
            break
    return text[:cut]


def extract_numbers(profile, subject: str, body: str, logger=None, extra_texts=None) -> tuple:
    """按档案 email 段声明的**每一组**单号规则，从标题/正文取号。

    只看**最新一封**正文（`newest_message`）：引用的旧邮件里常带着上一个项目的
    单号，拿它当本单事实是错的（LG26090401 实测把旧 PO#15900 落成了 KHDH）。

    返回 (numbers, sources)：
        numbers = {目标 ERP 字段: 值}   例 {'KHDDH': 'CU26072301'}
        sources = {目标 ERP 字段: 来源说明}

    ⚠️ 一个客户可能同时有两个单号（NGV：客户单号 KHDH + 香港单号 KHDDH），
    两组规则各自独立匹配，不能互相顶替（2026-09-24）。
    """
    numbers: dict = {}
    sources: dict = {}
    extra = [t for t in (extra_texts or []) if t]
    for rule_key, cfg, target in profile.number_rules():
        candidates = [
            ("subject_patterns", "邮件标题", subject),
            ("body_patterns", "邮件正文", newest_message(body)),
        ]
        # 额外文本（.eml 文件名、附件名）用同一套规则再扫一遍 ——
        # 实测 NGV 的香港单号常只出现在邮件文件名里（`CU26070802 - price enquiry….eml`）
        candidates += [("body_patterns", "邮件文件名", t) for t in extra]
        for section, label, text in candidates:
            if not text:
                continue
            for pattern in cfg.get(section) or []:
                try:
                    matches = list(re.finditer(pattern, text))
                except re.error:
                    continue
                for match in matches:
                    if _is_reference(text, match.start()):
                        continue        # 「参考 PO#…」不是本单单号
                    value = normalize_khdh(match.group(1))
                    if value:
                        numbers[target] = value
                        sources[target] = f"{label}（{profile.id} 规则 {rule_key}）"
                        if logger:
                            logger.info(f"{target} 来源: {label}（{profile.id} 规则 {rule_key}）")
                        break
                if target in numbers:
                    break
            if target in numbers:
                break
    return numbers, sources


def extract_khdh(profile, subject: str, body: str, logger=None) -> tuple:
    """兼容旧调用：只要客户单号 KHDH。返回 (值, 来源说明)。"""
    numbers, sources = extract_numbers(profile, subject, body, logger)
    return numbers.get("KHDH", ""), sources.get("KHDH", "")


# ── 投递 ─────────────────────────────────────────────────────────────────
def _unique_path(directory: Path, name: str) -> Path:
    target = directory / name
    counter = 1
    stem, suffix = Path(name).stem, Path(name).suffix
    while target.exists():
        target = directory / f"{stem}__{counter}{suffix}"
        counter += 1
    return target


def _unit_dir_name(subject: str, eml_name: str = "") -> str:
    """多附件邮件的单元目录名：标题（或 .eml 名）做人能认的名字。"""
    raw = (subject or Path(eml_name).stem or "email").strip()
    slug = re.sub(r"[^\w\u4e00-\u9fff.\- ]+", "", raw).strip().replace(" ", "_")
    slug = re.sub(r"_+", "_", slug)[:70].strip("_") or "email"
    return f"{slug}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def _unique_dir(parent: Path, name: str) -> Path:
    """建一个不重名的单元目录。"""
    target = parent / name
    counter = 1
    while target.exists():
        target = parent / f"{name}__{counter}"
        counter += 1
    target.mkdir(parents=True, exist_ok=False)
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
    eml_name: str = "",
    body_as_task: bool = True,
    body_extra: str = "",
) -> dict:
    """把附件落到 pending 并写 sidecar。attachments: [(文件名, bytes)]

    没有可用附件时（客户把规格写在邮件正文里），body_as_task=True 会把正文
    落成 `.txt` 任务，main.py 的文本通道可直接处理。
    """
    pending_dir.mkdir(parents=True, exist_ok=True)

    probe_texts = [subject, body] + [name for name, _ in attachments]
    profile, source = resolve_profile(
        customer, texts=probe_texts, filename=" ".join(name for name, _ in attachments),
        sender=sender, subject=subject, logger=logger,
    )

    # 先落盘，再用附件内容把客户身份判准（认不出时才有意义）
    usable = []
    skipped = []
    for name, payload in attachments:
        suffix = Path(name).suffix.lower()
        if suffix not in SUPPORTED_ATTACHMENTS:
            if logger:
                logger.warn(f"跳过不支持的附件类型: {name}")
            skipped.append(name)
            continue
        usable.append((name, payload))

    # ★ 一封邮件的 **≥2 个附件 = 同一份报价的全部参考文件**（业务 2026-10-20 口径）：
    #   不能拆成多个任务（那样会变成 N 份报价），要把它们投递成一个**单元目录**，
    #   main.py 把目录当一份报价处理（见 scripts/input_unit.py）。
    #   只有 1 个附件时保持老行为（单文件 + 副档），不制造多余的目录。
    unit_dir = None
    if len(usable) >= 2:
        unit_dir = _unique_dir(pending_dir, _unit_dir_name(subject, eml_name))
        delivery_dir = unit_dir
        if logger:
            logger.info(f"本次邮件 {len(usable)} 个附件 → 视为**同一份报价**，"
                        f"投递到单元目录: {unit_dir.name}/")
    else:
        delivery_dir = pending_dir

    saved_paths = []
    for name, payload in usable:
        target = _unique_path(delivery_dir, Path(name).name)
        with open(target, "wb") as f:
            f.write(payload)
        saved_paths.append(target)

    refined = _refine_by_attachment(profile, saved_paths, subject, logger)
    if refined.id != profile.id:
        profile = refined
        source = "自动识别（附件内容）"
        if not profile.is_calibrated and logger:
            logger.warn(f"档案 {profile.id} 状态为 {profile.status} —— 未经校准，字段值需人工核对")

    numbers, number_sources = extract_numbers(
        profile, subject, body, logger, extra_texts=[eml_name] if eml_name else None)
    subject_khdh = numbers.get("KHDH", "")
    subject_src = number_sources.get("KHDH", "")

    common_meta = {
        "source": "email",
        "ingested_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "subject": subject,
        "sender": sender,
        "eml_name": eml_name,
        "body": body,
        "customer_profile": profile.id,
        "customer_source": source,
        "numbers": numbers,
        "number_sources": number_sources,
        "khdh": numbers.get("KHDH", ""),
        "khdh_source": number_sources.get("KHDH", ""),
    }

    # 正文即任务：客户没附文件、规格全写在邮件里（NGV/KiK 这种 enquiry 邮件）
    body_task_path = None
    if not saved_paths and body.strip() and body_as_task:
        body_task_path, _ = write_body_task(pending_dir, body, common_meta)
        saved_paths.append(body_task_path)
        if logger:
            logger.info(f"邮件无可用附件 —— 正文已落为任务: {body_task_path.name}")

    saved = []
    unit_meta_path = None
    if unit_dir is not None:
        # 单元：一份邮件上下文（标题/正文/单号）+ 全部参考文件，放在同一个目录里；
        # main.py 读目录时用 `_email.meta.json`（见 input_unit.find_unit_meta）。
        unit_meta = dict(
            common_meta,
            body_extra=body_extra,
            attachments=[p.name for p in saved_paths],
            attachment_original_name=unit_dir.name,
            body_is_task=False,
            is_unit=True,
        )
        unit_meta_path = unit_dir / "_email.meta.json"
        with open(unit_meta_path, "w", encoding="utf-8") as f:
            json.dump(unit_meta, f, ensure_ascii=False, indent=2)
        if logger:
            logger.info(f"单元副档: {unit_meta_path.name}（{len(saved_paths)} 个附件共用）")

    for target in saved_paths:
        meta = {
            "source": "email",
            "ingested_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "subject": subject,
            "sender": sender,
            "eml_name": eml_name,
            "body": body,
            "body_extra": body_extra,
            "customer_profile": profile.id,
            "customer_source": source,
            # 单号：按目标 ERP 字段分别存（KHDH 客户单号 / KHDDH 香港单号 …）
            "numbers": numbers,
            "number_sources": number_sources,
            # 兼容旧字段（早期只认客户单号）
            "khdh": subject_khdh,
            "khdh_source": subject_src,
            "attachment_original_name": target.name,
            "body_is_task": target == body_task_path,
        }
        if unit_meta_path is not None:
            meta_path = unit_meta_path
        else:
            meta_path = write_meta(target, meta)
            if logger:
                logger.info(f"附件已投递: {target.name} -> 副档 {meta_path.name}")
        saved.append({
            "file": str(target), "meta": str(meta_path), "original_name": target.name,
            "body_is_task": bool(meta.get("body_is_task")),
        })

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
            "numbers": numbers,
            "number_sources": number_sources,
            "khdh": subject_khdh,
            "khdh_source": subject_src,
            "attachments": [s["original_name"] for s in saved],
            "skipped_attachments": skipped,
            "body_task": body_task_path.name if body_task_path else "",
            "body_is_task": bool(body_task_path),
        }, f, ensure_ascii=False, indent=2)

    return {
        "customer_profile": profile.id,
        "customer_source": source,
        "numbers": numbers,
        "number_sources": number_sources,
        "khdh": subject_khdh,
        "khdh_source": subject_src,
        "attachments": saved,
        "skipped_attachments": skipped,
        "body_task": body_task_path.name if body_task_path else "",
        "body_is_task": bool(body_task_path),
        "email_record": str(email_record),
        # 多附件 → 一个单元目录（一份报价的全部参考文件）
        "unit_dir": str(unit_dir) if unit_dir else "",
        "is_unit": bool(unit_dir),
    }


def body_task_name(subject: str, eml_name: str = "", numbers: dict | None = None) -> str:
    """给「正文即任务」的 .txt 起名：单号前缀 + 标题，便于认。"""
    number = ""
    for key in ("KHDDH", "KHDH"):
        if numbers and numbers.get(key):
            number = str(numbers[key]).split(" / ")[0].strip()
            break
    raw = " ".join(x for x in (number, subject or Path(eml_name).stem) if x).strip()
    slug = re.sub(r"[^\w\u4e00-\u9fff.\- ]+", "", raw).strip().replace(" ", "_")
    slug = re.sub(r"_+", "_", slug)[:80].strip("_") or "email_body"
    return f"{slug}.txt"


def write_body_task(pending_dir: Path, body: str, meta: dict) -> tuple:
    """没有可用附件时，把邮件正文落成 `.txt` 任务 —— main.py 的文本通道可直接吃。"""
    pending_dir.mkdir(parents=True, exist_ok=True)
    name = body_task_name(meta.get("subject", ""), meta.get("eml_name", ""), meta.get("numbers") or {})
    target = _unique_path(pending_dir, name)
    with open(target, "w", encoding="utf-8") as f:
        f.write(body.strip() + "\n")
    meta = dict(meta, body_is_task=True, attachment_original_name=target.name)
    meta_path = write_meta(target, meta)
    return target, meta_path


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
    parser.add_argument(
        "--body-as-task", dest="body_as_task", action="store_true", default=True,
        help="邮件没有可用附件时，把正文落成 .txt 任务（默认开）")
    parser.add_argument(
        "--no-body-as-task", dest="body_as_task", action="store_false",
        help="只留邮件记录，不把正文落成任务")
    parser.add_argument("--pending-dir", default=str(PENDING_DIR), help="投递目录")
    parser.add_argument("--json", action="store_true", help="只输出 JSON 汇总")
    args = parser.parse_args()

    logger = _Logger()
    attachments = []
    subject, body, sender = args.subject, args.body, args.sender
    eml_name = ""
    body_extra = ""

    if args.eml:
        eml_path = Path(args.eml)
        if not eml_path.exists():
            print(f"文件不存在: {eml_path}", file=sys.stderr)
            return 1
        parsed = parse_eml(eml_path)
        eml_name = eml_path.name
        subject = subject or parsed["subject"]
        body = body or parsed["body"]
        sender = sender or parsed["sender"]
        body_extra = parsed.get("body_tables", "")
        attachments.extend(parsed["attachments"])
        logger.info(
            f"解析 .eml: {eml_path.name}"
            f"（正文 {len(parsed['body'])} 字，附件 {len(parsed['attachments'])} 个）")

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
        eml_name=eml_name,
        body_as_task=args.body_as_task,
        body_extra=body_extra,
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print()
        print(f"客户档案: {result['customer_profile']}（{result['customer_source']}）")
        if result.get("numbers"):
            for target, value in result["numbers"].items():
                src = (result.get("number_sources") or {}).get(target, "")
                print(f"单号 {target}: {value}{'  来源: ' + src if src else ''}")
        else:
            print("单号: 未从标题/正文取到（将由附件列/全表扫描兜底）")
        if result.get("body_is_task"):
            print("正文即任务: 邮件没有可用附件，正文已落为 .txt（规格在邮件里）")
        print(f"投递附件: {len(result['attachments'])} 个")
        for item in result["attachments"]:
            print(f"  - {item['original_name']}  →  {item['file']}")
        print(f"邮件记录: {result['email_record']}")
        print()
        if result.get("is_unit"):
            print(f"这封邮件的 {len(result['attachments'])} 个附件 = 同一份报价的参考文件")
            print(f"→ 已放进单元目录: {result['unit_dir']}")
            print(f"下一步: uv run ./scripts/main.py \"{result['unit_dir']}\" --customer auto")
            print("        （把目录整体当作一份报价处理，只产出一个 ERP JSON）")
        else:
            print("下一步: uv run ./scripts/main.py <上面任一附件> --customer auto")
            print("        （main.py 会自动读副档里的邮件上下文与客户单号）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
