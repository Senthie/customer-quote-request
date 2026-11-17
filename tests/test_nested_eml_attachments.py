"""邮件里套邮件（`.eml` 当附件转发进来）。

现场（2026-11-16 07:20，森西转 Nancy 的测试邮件）：
封皮正文只有一句「Hi 张爽，附件5个询价邮件共你参考。」，真内容在 **5 个嵌套 `.eml`**
里（Fleurus / Casterman 的法文询价）。旧代码把 `.eml` 当「不支持的附件类型」WARN 跳过
→ `usable` 为空 → 走 `body_as_task` → 抽出一张空单（`values={}`、`profile=generic`、
`CPSL1..10` 全 0），并且这 5 封询价**一个字段都没进来**。

落定的口径：
* 每封嵌套 `.eml` **就是一份独立询价**（分组不用猜，这点和 zip 不同）；
* 但仍然**隔离、不自动入队**（`pending/.eml_quarantine/`，点开头 → 扫描器不认）——
  这些客户往往还没有档案，入队只会产出 `generic` 空单，让 `cpms_todo` 每 5 分钟催一次
  不存在的欠账；
* **有嵌套 `.eml` 时不再落封皮正文任务**（同 zip 那条闸）。
"""
import email.message
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import ingest_email as E  # noqa: E402
import scan_pending as S  # noqa: E402


class DummyLogger:
    def __init__(self):
        self.lines = []

    def info(self, msg):
        self.lines.append(("info", msg))

    def warn(self, msg):
        self.lines.append(("warn", msg))

    def error(self, msg):
        self.lines.append(("error", msg))

    def text(self):
        return "\n".join(m for _, m in self.lines)


def make_nested_eml(subject: str, body: str, sender: str = "buyer@example.com",
                    attachments: dict[str, tuple[str, bytes]] | None = None) -> bytes:
    """造一封能被我方 `parse_eml` 读懂的邮件（可带附件）。"""
    msg = email.message.EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = "quote@example.com"
    msg.set_content(body)
    for name, (ctype, payload) in (attachments or {}).items():
        maintype, subtype = ctype.split("/", 1)
        msg.add_attachment(payload, maintype=maintype, subtype=subtype, filename=name)
    return msg.as_bytes()


def email_with_nested(nested: list[tuple[str, bytes]], body: str = "Hi 张爽，附件5个询价邮件共你参考。") -> list:
    """把嵌套邮件当成「外层邮件的附件」交给 ingest（等价于 parse_eml 的产物）。"""
    return list(nested)


# ── 隔离（不丢件） ────────────────────────────────────────────────────

def test_嵌套eml被隔离而不是静默跳过(tmp_path):
    """核心回归：5 封嵌套询价不许凭空消失。"""
    nested = [
        (f"询价{i}- Request for Quotation #{1000 + i}.eml",
         make_nested_eml(f"询价{i}", f"正文{i}", attachments={"spec.pdf": ("application/pdf", b"%PDF-1.4 x")}))
        for i in range(1, 6)
    ]
    log = DummyLogger()
    result = E.ingest(
        attachments=email_with_nested(nested), subject="测试 AI 的邮件分类",
        body="Hi 张爽，附件5个询价邮件共你参考。", sender="nancy@example.com",
        customer="auto", pending_dir=tmp_path, logger=log, eml_name="outer.eml",
    )
    entries = result["quarantined_emls"]
    assert len(entries) == 1, "5 封要合成一条隔离记录"
    assert entries[0]["members"] == 5
    dest = Path(entries[0]["dir"])
    assert dest.is_dir()
    files = sorted(p.name for p in dest.iterdir() if p.suffix == ".eml")
    assert len(files) == 5, f"5 封都要落盘，实际 {files}"
    # 原来的 WARN「跳过不支持的附件类型」不该再出现
    assert "跳过不支持的附件类型" not in log.text()


def test_隔离清单带标题发件人与子附件(tmp_path):
    """清单要能直接拿去判断「该先给谁建档案」。"""
    nested = [("询价1.eml", make_nested_eml(
        "Request for Quotation #105819 Bienvenue à Zinzinville",
        "Bonjour, merci de nous chiffrer…",
        sender="t.dubus@fleuruseditions.com",
        attachments={"demande_prix.pdf": ("application/pdf", b"%PDF-1.4 x")},
    ))]
    result = E.ingest(attachments=nested, subject="测试", body="见附件", sender="n@example.com",
                      customer="auto", pending_dir=tmp_path, logger=DummyLogger(), eml_name="o.eml")
    dest = Path(result["quarantined_emls"][0]["dir"])
    manifest = json.loads((dest / "_nested_eml_manifest.json").read_text(encoding="utf-8"))
    assert manifest["kind"] == "nested_eml_quarantine"
    assert "独立询价" in manifest["grouping"]
    member = manifest["members"][0]
    assert "105819" in member["subject"]
    assert "fleuruseditions" in member["sender"]
    assert member["attachments"] == ["demande_prix.pdf"]


def test_嵌套eml不进队列(tmp_path):
    """点开头的隔离目录，扫描器不许认（否则下一轮 cron 就把它抽成 generic 空单）。"""
    nested = [("询价1.eml", make_nested_eml("Re: enquiry", "spec 210x300"))]
    E.ingest(attachments=nested, subject="测试", body="见附件", sender="n@example.com",
             customer="auto", pending_dir=tmp_path, logger=DummyLogger(), eml_name="o.eml")
    assert (tmp_path / E.EML_QUARANTINE_DIRNAME).is_dir()
    queue = S.pending_items(tmp_path)
    assert all(Path(p).suffix != ".eml" or E.EML_QUARANTINE_DIRNAME in str(p) for p in queue)
    for p in queue:
        assert E.EML_QUARANTINE_DIRNAME not in str(p), f"隔离目录里的文件不该入队: {p}"


# ── 不落空单 ──────────────────────────────────────────────────────────

def test_有嵌套eml时不落封皮正文空单(tmp_path):
    """封皮正文只有「附件 5 个询价邮件」，落成任务只会产出空单。"""
    nested = [("询价1.eml", make_nested_eml("RFQ #1", "specs"))]
    result = E.ingest(attachments=nested, subject="测试 AI 的邮件分类",
                      body="Hi 张爽，附件5个询价邮件共你参考。", sender="n@example.com",
                      customer="auto", pending_dir=tmp_path, logger=DummyLogger(), eml_name="o.eml")
    assert result["body_task"] == ""
    assert result["body_is_task"] is False
    assert result["attachments"] == []
    assert not list(tmp_path.glob("*.txt")), "不许落下封皮 .txt 任务"


def test_只有图片附件时正文仍然落成任务(tmp_path):
    """★ 别把闸门开太大：签名图/logo 不是「内容」，正文规格还得能进管道。"""
    result = E.ingest(attachments=[("logo.png", b"\x89PNG\r\n")],
                      subject="price enquiry Ostermalblock",
                      body="TPS: 148 x 210 mm portrait\nQuantity: 30.100cps",
                      sender="n@example.com", customer="auto", pending_dir=tmp_path,
                      logger=DummyLogger(), eml_name="o.eml")
    assert result["body_task"].endswith(".txt")
    assert result["quarantined_emls"] == []


def test_嵌套eml与普通附件并存时各自归位(tmp_path):
    """一封邮件既带 xlsx 又套了一封 .eml：xlsx 照常入队，嵌套邮件进隔离。"""
    nested = [("转发询价.eml", make_nested_eml("Fwd: RFQ", "specs"))]
    result = E.ingest(
        attachments=nested + [("报价表.xlsx", b"fake-xlsx-bytes")],
        subject="两个东西", body="见附件", sender="n@example.com", customer="auto",
        pending_dir=tmp_path, logger=DummyLogger(), eml_name="o.eml",
    )
    assert len(result["attachments"]) == 1
    assert result["attachments"][0]["original_name"] == "报价表.xlsx"
    assert result["quarantined_emls"][0]["members"] == 1


# ── CLI 汇总 ──────────────────────────────────────────────────────────

def test_cli_json汇总带隔离字段(tmp_path, monkeypatch, capsys):
    inner = make_nested_eml("RFQ #1", "specs")
    outer = tmp_path / "outer.eml"
    outer.write_bytes(make_nested_eml(
        "测试", "Hi 张爽，附件 1 封询价", attachments={
            "询价1.eml": ("application/octet-stream", inner)}))
    monkeypatch.setattr(sys, "argv", [
        "ingest_email.py", "--eml", str(outer), "--pending-dir", str(tmp_path / "pending"), "--json"])
    assert E.main() == 0
    raw = capsys.readouterr().out
    out = json.loads(raw[raw.index("{"):])          # 前面还有一行 [INFO] 解析 .eml
    assert out["quarantined_emls"][0]["members"] == 1
    assert out["body_task"] == ""
