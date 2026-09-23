"""邮件入口测试：标题/正文取客户单号、附件投递、副档读写。"""
import json
from email.message import EmailMessage
from pathlib import Path

import pytest

from customer_profile import load_profile
from ingest_email import (
    META_SUFFIX,
    extract_khdh,
    ingest,
    normalize_khdh,
    parse_eml,
    read_attachment_meta,
)

IGL = load_profile("igl")


class _SilentLogger:
    def info(self, *a, **k):
        pass

    def warn(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


LOGGER = _SilentLogger()


# ── 客户单号 ────────────────────────────────────────────────────────────
def test_单号归一化():
    assert normalize_khdh("23992/23993") == "23992 / 23993"
    assert normalize_khdh("  23992  /  23993  ") == "23992 / 23993"
    assert normalize_khdh("23992") == "23992"


def test_标题里取单号_PO前缀():
    value, source = extract_khdh(IGL, "PO 23992 / 23993 - Puzzle & Colour Disney", "")
    assert value == "23992 / 23993"
    assert "标题" in source


def test_标题里取单号_裸数字():
    value, _ = extract_khdh(IGL, "Quote request 23992", "")
    assert value == "23992"


def test_正文里取单号():
    value, source = extract_khdh(IGL, "", "Hi,\nWorks References: 23992 / 23993\nThanks")
    assert value == "23992 / 23993"
    assert "正文" in source


def test_标题优先于正文():
    value, source = extract_khdh(IGL, "Ref: 11111", "Works References: 22222")
    assert value == "11111"
    assert "标题" in source


def test_取不到时返回空():
    value, source = extract_khdh(IGL, "Hello there", "No reference inside")
    assert value == ""
    assert source == ""


def test_不同档案的规则互不干扰():
    """把标题换成 NGV 风格单号，igl 规则抓不到（证明规则是按客户档案走的）。"""
    ngv = load_profile("ngv")
    value, _ = extract_khdh(ngv, "PO NGV-2026-0451 artwork attached", "")
    assert value == "NGV-2026-0451"


# ── .eml 解析 ───────────────────────────────────────────────────────────
def _build_eml(path, subject, body, attachment_name=None, payload=b"fake-xlsx"):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = "Autumn <autumn@example.com>"
    msg["To"] = "quote@example.com"
    msg.set_content(body)
    if attachment_name:
        msg.add_attachment(payload, maintype="application",
                           subtype="octet-stream", filename=attachment_name)
    path.write_bytes(bytes(msg))
    return path


def test_解析_eml_标题正文附件(tmp_path):
    eml = _build_eml(tmp_path / "m.eml", "PO 23992 / 23993", "Please quote.\n",
                     attachment_name="SAMPLE.xlsx")
    parsed = parse_eml(eml)
    assert parsed["subject"] == "PO 23992 / 23993"
    assert "Please quote." in parsed["body"]
    assert parsed["attachments"][0][0] == "SAMPLE.xlsx"
    assert "autumn@example.com" in parsed["sender"]


def test_解析_eml_HTML正文转文本(tmp_path):
    msg = EmailMessage()
    msg["Subject"] = "x"
    msg.set_content("<html><body><p>Hello</p><script>bad()</script></body></html>",
                    subtype="html")
    eml = tmp_path / "h.eml"
    eml.write_bytes(bytes(msg))
    parsed = parse_eml(eml)
    assert "Hello" in parsed["body"]
    assert "bad()" not in parsed["body"]


# ── 投递 ────────────────────────────────────────────────────────────────
def test_投递_附件落盘并写副档(tmp_path):
    pending = tmp_path / "pending"
    result = ingest(
        attachments=[("SAMPLE.xlsx", b"fake")],
        subject="PO 23992 / 23993 - quote",
        body="Works References: 23992 / 23993\nWorks References For all Titles",
        sender="autumn@example.com",
        customer="auto",
        pending_dir=pending,
        logger=LOGGER,
    )
    assert result["khdh"] == "23992 / 23993"
    assert result["customer_profile"] == "igl"      # 正文里有 igl 的特征标签
    saved = Path(result["attachments"][0]["file"])
    assert saved.read_bytes() == b"fake"
    # 副档内容
    meta = read_attachment_meta(saved)
    assert meta["khdh"] == "23992 / 23993"
    assert meta["subject"].startswith("PO 23992")
    assert meta["customer_profile"] == "igl"
    # 邮件记录也留着（即使附件丢了正文/单号也能追）
    assert Path(result["email_record"]).exists()


def test_投递_正文无线索时按附件内容识别客户(tmp_path):
    """文件名和标题都没线索 → 读附件内容里的招牌栏目来认客户。"""
    import pandas as pd

    sample = tmp_path / "SAMPLE.xlsx"
    pd.DataFrame([
        ["", "Imprint", "Autumn"],
        ["", "Works References For all Titles", "23992 / 23993"],
        ["", "Extent (pp + cover)", "120pp + cover"],
    ]).to_excel(sample, index=False, header=False)

    pending = tmp_path / "pending"
    result = ingest(
        attachments=[("SAMPLE.xlsx", sample.read_bytes())],
        subject="Hello, please quote",
        body="See attached.",
        sender="",
        customer="auto",
        pending_dir=pending,
        logger=LOGGER,
    )
    assert result["customer_profile"] == "igl"
    assert "附件内容" in result["customer_source"]


def test_投递_同名附件不覆盖(tmp_path):
    pending = tmp_path / "pending"
    for _ in range(2):
        ingest(attachments=[("SAMPLE.xlsx", b"fake")], subject="", body="",
               sender="", customer="igl", pending_dir=pending, logger=LOGGER)
    names = sorted(p.name for p in pending.glob("SAMPLE*.xlsx"))
    assert len(names) == 2, names


def test_投递_不支持的附件类型被跳过(tmp_path):
    pending = tmp_path / "pending"
    result = ingest(attachments=[("virus.exe", b"x"), ("ok.csv", b"a,b\n1,2\n")],
                    subject="", body="", sender="", customer="igl",
                    pending_dir=pending, logger=LOGGER)
    assert [Path(a["file"]).name for a in result["attachments"]] == ["ok.csv"]


def test_投递_无附件也留邮件记录(tmp_path):
    pending = tmp_path / "pending"
    result = ingest(attachments=[], subject="PO 23992", body="ref 23992",
                    sender="", customer="igl", pending_dir=pending, logger=LOGGER)
    assert result["attachments"] == []
    assert Path(result["email_record"]).exists()


def test_读副档_不存在时返回空(tmp_path):
    assert read_attachment_meta(tmp_path / "nope.xlsx") == {}


def test_读副档_坏JSON不炸(tmp_path):
    bad = tmp_path / "a.xlsx"
    bad.write_text("x", encoding="utf-8")
    bad.with_name("a.xlsx" + META_SUFFIX).write_text("{ not json", encoding="utf-8")
    assert read_attachment_meta(bad) == {}
