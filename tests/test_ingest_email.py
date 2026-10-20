"""邮件入口测试：标题/正文取客户单号、附件投递、副档读写。"""
import json
from email.message import EmailMessage
from pathlib import Path

import pytest

from customer_profile import load_profile
from ingest_email import (
    META_SUFFIX,
    extract_khdh,
    extract_numbers,
    html_tables_to_text,
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
    """同一封邮件标题，两份档案抓出各自认的单号（证明规则是按客户档案走的）。"""
    ngv = load_profile("ngv")
    subject = "NGV enquiry CU26072301 Postkarten in Box"
    # NGV：CU… 是**香港单号** → KHDDH（2026-09-24 拆开）
    numbers, sources = extract_numbers(ngv, subject, "")
    assert numbers.get("KHDDH") == "CU26072301"
    assert "KHDH" not in numbers
    assert "标题" in sources["KHDDH"]
    # igl 的规则不会认得 NGV 单号（互不干扰）
    igl_value, _ = extract_khdh(IGL, subject, "")
    assert igl_value != "CU26072301"


def test_NGV两组单号各归各位():
    """客户单号→KHDH、香港单号→KHDDH；两条规则独立匹配，互不顶替。"""
    ngv = load_profile("ngv")
    numbers, _ = extract_numbers(
        ngv, "NGV enquiry CU26072301 - PO no.: AB-12345", "香港单号: CU26072301")
    assert numbers.get("KHDDH") == "CU26072301"
    assert numbers.get("KHDH") == "AB-12345"


def test_客户单号正则不许被单词前缀误捕():
    """`Postkarten` 里的 `Po` 曾被当成 PO 号（抓出 `stkarten`）。"""
    ngv = load_profile("ngv")
    numbers, _ = extract_numbers(ngv, "Postkarten in Box mit Aufsteller", "")
    assert "KHDH" not in numbers


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


def test_投递_多个附件归成同一份报价的单元(tmp_path):
    """一封邮件 ≥2 个附件 = **同一份报价**的参考文件 → 一个单元目录，不是 N 份。

    业务口径（2026-10-20）：单个文件是独一份报价；文件夹/压缩包里的文件
    是一份报价的全部参考文件。邮件是客户的一次询价，多个附件自然是同一单的参考材料 ——
    拆成多个任务会变成多份报价，是错的。
    """
    pending = tmp_path / "pending"
    result = ingest(
        attachments=[("SPEC.xlsx", b"fake1"), ("CONFIRMATION.docx", b"fake2")],
        subject="PR-00099999 price enquiry",
        body="Please quote the attached.",
        sender="buyer@example.com",
        customer="auto",
        pending_dir=pending,
        logger=LOGGER,
    )
    assert result["is_unit"] is True
    unit_dir = Path(result["unit_dir"])
    assert unit_dir.is_dir()
    assert sorted(p.name for p in unit_dir.iterdir() if p.is_file()) == sorted([
        "_email.meta.json", "CONFIRMATION.docx", "SPEC.xlsx"])
    # 共享一份邮件副档（标题/正文/单号），不是每个附件一份
    meta = json.loads((unit_dir / "_email.meta.json").read_text(encoding="utf-8"))
    assert meta["is_unit"] is True
    assert meta["attachments"] == ["SPEC.xlsx", "CONFIRMATION.docx"]
    assert meta["subject"] == "PR-00099999 price enquiry"


def test_投递_单个附件仍然是单文件任务(tmp_path):
    """1 个附件 = 独一份报价：老行为不变（单文件 + 同名副档，不建目录）。"""
    pending = tmp_path / "pending"
    result = ingest(
        attachments=[("SAMPLE.xlsx", b"fake")],
        subject="PR-00099998", body="", sender="buyer@example.com",
        customer="auto", pending_dir=pending, logger=LOGGER,
    )
    assert result["is_unit"] is False and result["unit_dir"] == ""
    saved = Path(result["attachments"][0]["file"])
    assert saved.parent == pending
    assert saved.with_name(saved.name + ".meta.json").exists()


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


def test_投递_无附件时正文落为任务(tmp_path):
    """客户把规格写在邮件正文里（NGV/KiK 这种 enquiry）：正文必须变成可处理的 .txt。"""
    pending = tmp_path / "pending"
    result = ingest(attachments=[], subject="PO 23992", body="Title: Ostermalblock",
                    sender="", customer="igl", pending_dir=pending, logger=LOGGER)
    assert len(result["attachments"]) == 1, result["attachments"]
    item = result["attachments"][0]
    assert item["body_is_task"] is True
    task = Path(item["file"])
    assert task.suffix == ".txt"
    assert "Ostermalblock" in task.read_text(encoding="utf-8")
    assert Path(result["email_record"]).exists()
    assert result["body_is_task"] is True


def test_投递_可以关闭正文任务(tmp_path):
    pending = tmp_path / "pending"
    result = ingest(attachments=[], subject="PO 23992", body="ref 23992",
                    sender="", customer="igl", pending_dir=pending, logger=LOGGER,
                    body_as_task=False)
    assert result["attachments"] == []
    assert Path(result["email_record"]).exists()


def test_香港单号可以从_eml_文件名取(tmp_path):
    """实测 NGV 的 `CU26070802 - price enquiry….eml`：表内没有单号，只在文件名里。"""
    pending = tmp_path / "pending"
    result = ingest(attachments=[], subject="price enquiry. Ostermalblock",
                    body="Dear Alex, please quote.", sender="Faeller@vemag-medien.de",
                    customer="auto", pending_dir=pending, logger=LOGGER,
                    eml_name="CU26070802 - price enquiry. Ostermalblock.eml")
    assert result["numbers"].get("KHDDH") == "CU26070802", result["numbers"]
    assert "文件名" in result["number_sources"].get("KHDDH", "")


def test_内嵌图片不算附件(tmp_path):
    """签名 logo（Content-Disposition: inline）不该落成待处理任务。"""
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["Subject"] = "price enquiry"
    msg["From"] = "a@vemag-medien.de"
    msg.set_content("body text")
    msg.add_alternative('<html><body><img src="cid:x"></body></html>', subtype="html")
    msg.get_payload()[1].add_related(
        b"\xff\xd8\xff", maintype="image", subtype="jpeg", cid="x",
        filename="logo.jpg", disposition="inline")

    eml = tmp_path / "m.eml"
    eml.write_bytes(bytes(msg))
    parsed = parse_eml(eml)
    assert parsed["attachments"] == [], parsed["attachments"]
    assert "body text" in parsed["body"]


def test_HTML表格压成标签值行(tmp_path):
    """规格写在 HTML 表格里时（纯文本缺失），表格要变成 `标签: 值` 行。"""
    html = ("<table><tr><td>TPS:</td><td>148 x 210 mm portrait</td></tr>"
            "<tr><td>Extent:</td><td>64 pp + cover</td></tr></table>")
    text = html_tables_to_text(html)
    assert "TPS:" in text and "148 x 210 mm portrait" in text
    assert "Extent:" in text and "64 pp + cover" in text


def test_读副档_不存在时返回空(tmp_path):
    assert read_attachment_meta(tmp_path / "nope.xlsx") == {}


def test_读副档_坏JSON不炸(tmp_path):
    bad = tmp_path / "a.xlsx"
    bad.write_text("x", encoding="utf-8")
    bad.with_name("a.xlsx" + META_SUFFIX).write_text("{ not json", encoding="utf-8")
    assert read_attachment_meta(bad) == {}
