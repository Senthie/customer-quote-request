"""邮件里的 `.zip` 附件（`scripts/ingest_email.py` 的隔离解压）。

业务口径（2026-11-13，森西选的是第 3 条）：
**「解压落 pending，不自动分组」**。

为什么不能沿用「≥2 附件 = 一份报价」：NGV 发的是转发封皮 + 一个 zip，zip 里装着
5~10 个**互相独立**的询价（不同 `LG` 单号）。照旧规则并成一份是错的；反过来直接
摊平也会把「一份报价的 5 个参考文件」拆成 5 份。脚本猜不出来 → 只负责解开 + 留清单，
拆几份由人/agent 决定。

配套的两条硬约束：
* 隔离目录用**点开头**（`pending/.zip_quarantine/`），扫描器不认 → 不会被自动抽
* 有 zip 时**不再**把封皮正文落成任务（否则产出 `CPMC=【中文品名待定】` 的空单，
  2026-11-13 就是这么翻车的）
"""
import json
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

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


def make_zip(entries: dict[str, bytes]) -> bytes:
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


# ── extract_zip ──────────────────────────────────────────────────────

def test_解压保留目录结构(tmp_path):
    payload = make_zip({
        "Kevin/LG26070701-01/询价表.xlsx": b"a",
        "Kevin/LG26080302/询价表.xlsx": b"b",
    })
    written, note = E.extract_zip(payload, tmp_path)
    assert note == ""
    assert sorted(written) == ["Kevin/LG26070701-01/询价表.xlsx",
                               "Kevin/LG26080302/询价表.xlsx"]
    assert (tmp_path / "Kevin/LG26070701-01/询价表.xlsx").read_bytes() == b"a"


def test_拒绝目录穿越与绝对路径(tmp_path):
    payload = make_zip({
        "../../etc/passwd": b"x",
        "/abs.txt": b"y",
        "ok.txt": b"z",
    })
    written, _ = E.extract_zip(payload, tmp_path)
    assert written == ["ok.txt"], "穿越/绝对路径的成员必须丢掉"
    assert not (tmp_path.parent.parent / "etc").exists()


def test_跳过系统垃圾(tmp_path):
    payload = make_zip({
        "__MACOSX/._a.xlsx": b"x",
        ".DS_Store": b"y",
        "真正的询价.xlsx": b"z",
    })
    written, _ = E.extract_zip(payload, tmp_path)
    assert written == ["真正的询价.xlsx"]


def test_GBK编码的成员名不乱码():
    """Windows 打的 zip 不置 UTF-8 标志，成员名是 GBK 字节；照 cp437 解会成 `▒¿╝█`。

    真实样本：NGV 的 `Demo - 报价搜集 - 5个.zip`（2026-11-13 实测）。
    这里直接构造 ZipInfo —— 用 zipfile 写不出「无 UTF-8 标志的 GBK 名」。
    """
    name = "报价给客户邮件 Re_ Kassenplatzierung.xlsx"

    class FakeInfo:
        flag_bits = 0
        filename = name.encode("gbk").decode("cp437")   # 真实 zip 里就是这个样子

    assert E._zip_member_name(FakeInfo()) == name

    class Utf8Info:
        flag_bits = 0x800                               # 明确标了 UTF-8 → 原样用
        filename = name

    assert E._zip_member_name(Utf8Info()) == name


def test_单号前缀只是参考分组(tmp_path):
    payload = make_zip({
        "CU26082501 询价邮件.eml": b"a",
        "CU26082501 系统询价单.pdf": b"b",
        "LG26070701-01 报价.xlsx": b"c",
        "说明.txt": b"d",
    })
    pending = tmp_path / "pending"
    result = E.quarantine_archives([("包.zip", payload)], pending, "主题")
    dest = Path(result["entries"][0]["dir"])
    manifest = json.loads((dest / "_unzip_manifest.json").read_text(encoding="utf-8"))
    assert manifest["grouping"].startswith("未分组")
    assert len(manifest["prefix_hint"]["CU26082501"]) == 2
    assert len(manifest["prefix_hint"]["LG26070701-01"]) == 1
    assert len(manifest["prefix_hint"]["（无单号前缀）"]) == 1


def test_假zip给出说明而不是抛异常(tmp_path):
    written, note = E.extract_zip(b"this is not a zip", tmp_path)
    assert written == []
    assert "不是合法的 zip" in note


def test_成员数上限会截断并说明(tmp_path):
    payload = make_zip({f"f{i}.xlsx": b"x" for i in range(10)})
    written, note = E.extract_zip(payload, tmp_path, max_files=3)
    assert len(written) == 3
    assert "上限" in note


# ── quarantine_archives ──────────────────────────────────────────────

def test_隔离目录用点开头并留清单(tmp_path):
    pending = tmp_path / "pending"
    pending.mkdir()
    payload = make_zip({"Kevin/LG26070701-01/a.xlsx": b"x",
                        "Kevin/LG26080302/b.xlsx": b"y"})
    result = E.quarantine_archives([("Kevin-报价搜索5个.zip", payload)],
                                   pending, "NGV 客户 - 询价资料",
                                   eml_name="m.eml", logger=DummyLogger())

    entry = result["entries"][0]
    dest = Path(entry["dir"])
    assert dest.name.startswith("Kevin-报价搜索5个_")
    assert dest.parent.name == E.ZIP_QUARANTINE_DIRNAME
    assert result["root"] == str(pending / E.ZIP_QUARANTINE_DIRNAME)

    manifest = (dest / "_unzip_manifest.json").read_text(encoding="utf-8")
    assert "未分组" in manifest, "清单必须写明还没分组，别让下游以为是拆好的"
    assert "LG26070701-01" in manifest
    assert entry["members"] == 2


def test_隔离目录不会被扫描器当成任务(tmp_path):
    """点开头 = pending_items 不认，所以 zip 里的文件不会自动变成报价任务。"""
    pending = tmp_path / "pending"
    pending.mkdir()
    payload = make_zip({"a.xlsx": b"x"})
    E.quarantine_archives([("包.zip", payload)], pending, "主题")

    assert S.pending_items(pending) == [], "隔离目录（及其中文件）不该进队列"
    # 放到根下才入队 —— 这就是「人工确认后移出来」的用法
    (pending / "a.xlsx").write_bytes(b"x")
    assert [p.name for p in S.pending_items(pending)] == ["a.xlsx"]


# ── ingest() 集成 ────────────────────────────────────────────────────

def test_只有zip时不落正文空单(tmp_path):
    """封皮正文只有「附件是客户NGV的5个报价」—— 落成任务只会产出空单。"""
    pending = tmp_path / "pending"
    payload = make_zip({"Kevin/LG26070701-01/a.xlsx": b"x"})
    result = E.ingest(
        attachments=[("Kevin-报价搜索5个.zip", payload)],
        subject="NGV-报价搜索5个",
        body="附件是客户NGV的5个报价，请查收。",
        sender="kevinlai@ghouseproducts.com",
        customer="auto",
        pending_dir=pending,
        logger=DummyLogger(),
    )
    assert result["attachments"] == [], "不该产出正文任务"
    assert result["body_is_task"] is False
    assert len(result["quarantined_zips"]) == 1
    assert result["quarantined_zips"][0]["members"] == 1
    # pending 根下没有任何可抽的东西
    assert S.pending_items(pending) == []
    # 邮件记录仍然留着（正文/单号可追）
    assert Path(result["email_record"]).exists()


def test_zip和普通附件并存(tmp_path):
    pending = tmp_path / "pending"
    payload = make_zip({"内层.xlsx": b"x"})
    result = E.ingest(
        attachments=[("报价表.xlsx", b"fake"), ("补充资料.zip", payload)],
        subject="LG26090401 询价",
        body="见附件",
        sender="kevinlai@ghouseproducts.com",
        customer="auto",
        pending_dir=pending,
        logger=DummyLogger(),
    )
    # 普通附件照旧落盘 + 副档；zip 进隔离区
    assert [Path(a["file"]).name for a in result["attachments"]] == ["报价表.xlsx"]
    assert len(result["quarantined_zips"]) == 1
    assert (pending / "报价表.xlsx").exists()


def test_zip不算进多附件单元(tmp_path):
    """zip 不参与「≥2 附件 = 单元目录」的判断，否则内层文件会被并成一份报价。"""
    pending = tmp_path / "pending"
    payload = make_zip({"a.xlsx": b"x", "b.xlsx": b"y"})
    result = E.ingest(
        attachments=[("包.zip", payload), ("单文件.xlsx", b"fake")],
        subject="某单询价",
        body="",
        sender="x@y.com",
        customer="auto",
        pending_dir=pending,
        logger=DummyLogger(),
    )
    assert result["is_unit"] is False
    assert result["unit_dir"] == ""


def test_rar只报警不静默(tmp_path):
    pending = tmp_path / "pending"
    log = DummyLogger()
    result = E.ingest(
        attachments=[("资料.rar", b"Rar!\x1a\x07\x00")],
        subject="询价",
        body="见附件",
        sender="x@y.com",
        customer="auto",
        pending_dir=pending,
        logger=log,
    )
    assert result["unhandled_archives"] == ["资料.rar"]
    assert any("解不开" in m for _lvl, m in log.lines)


# ── 扫描器侧：隔离摘要要带回给人看 ────────────────────────────────────

def test_从ingest输出解析隔离清单():
    stdout = ('some noise\n'
              '{"attachments": [], "quarantined_zips": '
              '[{"archive": "包.zip", "members": 7, "note": ""}]}')
    out = S._parse_quarantined(stdout)
    assert out == [{"archive": "包.zip", "members": 7, "note": ""}]


def test_从无隔离的输出解析出空列表():
    assert S._parse_quarantined('{"attachments": []}') == []
    assert S._parse_quarantined("") == []
    assert S._parse_quarantined("纯文本，不是 JSON") == []
