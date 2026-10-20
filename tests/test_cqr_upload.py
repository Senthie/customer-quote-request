"""上传落盘（`CQR_Web/upload.py`）。

上传是**唯一往 pending/ 写东西的入口**，所以这层的错误边界就是安全边界：
文件名净化、后缀白名单、同名不覆盖、单 .zip 必须平铺。用例都按「外面真能塞进来什么」写。
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from CQR_Web import upload as U  # noqa: E402


# ── 文件名净化 ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("210x280_Diamantsticker.xlsx", "210x280_Diamantsticker.xlsx"),
    ("../../etc/passwd.xlsx", "passwd.xlsx"),            # 路径穿越 → 只剩 basename
    ("a/b/c/PUZZLE COLOUR DISNEY.xlsx", "PUZZLE COLOUR DISNEY.xlsx"),
    ("C:\\temp\\报价单.xlsx", "报价单.xlsx"),              # Windows 反斜杠
    ("香港单号 CU26072301 A_Klappkarten.xlsx", "香港单号 CU26072301 A_Klappkarten.xlsx"),
    ("weird:name*?.xlsx", "weird_name__.xlsx"),          # 危险字符 → _
    ("  spaced  .xlsx", "spaced.xlsx"),                   # 去首尾空白
    (".hidden.xlsx", "hidden.xlsx"),                      # 点开头会被 input_unit 当垃圾
    ("tab\there.xlsx", "tab_here.xlsx"),                  # 控制字符
])
def test_safe_filename(raw, expected):
    assert U.safe_filename(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "...", "/", ".."])
def test_safe_filename_rejects_empty(raw):
    with pytest.raises(U.UploadError):
        U.safe_filename(raw)


def test_safe_filename_truncates_but_keeps_suffix():
    name = U.safe_filename("长" * 300 + ".xlsx")
    assert name.endswith(".xlsx")
    assert len(name) == U._STEM_MAX + len(".xlsx")


# ── 后缀白名单 ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["a.xlsx", "a.XLS", "a.csv", "a.pdf", "a.docx",
                                  "a.txt", "a.zip", "a.XLSX"])
def test_check_suffix_allows(name):
    U.check_suffix(name)          # 不抛就算过


def test_eml_is_rejected_with_actionable_hint():
    """`.eml` main.py 直接吃不了（要过 ingest_email）—— 上传口要给出下一步。"""
    with pytest.raises(U.UploadError) as excinfo:
        U.check_suffix("mail.eml")
    assert "ingest_email" in str(excinfo.value)


@pytest.mark.parametrize("name", ["a.exe", "a.png", "a", "a.json", "a.meta.json"])
def test_check_suffix_rejects_others(name):
    with pytest.raises(U.UploadError):
        U.check_suffix(name)


# ── 落盘布局 ───────────────────────────────────────────────────────────

def test_single_file_goes_flat(tmp_path):
    """单文件 = 一份报价 → 平铺在 pending/ 根下（不进子目录）。"""
    out = U.save_uploads([("报价.xlsx", b"data")], group="one", pending_dir=tmp_path)
    assert out["batch"] is None
    assert [s["relpath"] for s in out["saved"]] == ["报价.xlsx"]
    assert (tmp_path / "报价.xlsx").read_bytes() == b"data"


def test_single_zip_goes_flat_never_into_folder(tmp_path):
    """单个 .zip 必须平铺：放进目录里 input_unit 会判「文件夹里只有压缩包」而拒。"""
    out = U.save_uploads([("打包.zip", b"PK")], group="one", pending_dir=tmp_path)
    assert out["batch"] is None
    assert (tmp_path / "打包.zip").exists()


def test_multi_files_group_one_share_a_batch_dir(tmp_path):
    """N 个文件 + 「同一份报价」→ 一个目录 = 一份报价的全部参考文件。"""
    out = U.save_uploads([("a.xlsx", b"1"), ("b.pdf", b"2"), ("c.txt", b"3")],
                         group="one", pending_dir=tmp_path)
    assert out["batch"]
    batch_dir = tmp_path / out["batch"]
    assert batch_dir.is_dir()
    assert sorted(p.name for p in batch_dir.iterdir()) == ["a.xlsx", "b.pdf", "c.txt"]
    assert all(s["relpath"].startswith(out["batch"] + "/") for s in out["saved"])


def test_multi_files_group_each_go_flat(tmp_path):
    out = U.save_uploads([("a.xlsx", b"1"), ("b.xlsx", b"2")],
                         group="each", pending_dir=tmp_path)
    assert out["batch"] is None
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.xlsx", "b.xlsx"]


def test_same_name_never_overwrites(tmp_path):
    U.save_uploads([("报价.xlsx", b"first")], pending_dir=tmp_path)
    out = U.save_uploads([("报价.xlsx", b"second")], pending_dir=tmp_path)
    assert out["saved"][0]["name"] == "报价 (2).xlsx"
    assert (tmp_path / "报价.xlsx").read_bytes() == b"first"
    assert (tmp_path / "报价 (2).xlsx").read_bytes() == b"second"


def test_partial_rejection_keeps_the_good_ones(tmp_path):
    out = U.save_uploads([("ok.xlsx", b"1"), ("bad.exe", b"2"), ("empty.csv", b"")],
                         pending_dir=tmp_path)
    assert [s["name"] for s in out["saved"]] == ["ok.xlsx"]
    assert {r["name"] for r in out["rejected"]} == {"bad.exe", "empty.csv"}


def test_all_rejected_raises(tmp_path):
    with pytest.raises(U.UploadError):
        U.save_uploads([("bad.exe", b"1")], pending_dir=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_traversal_name_cannot_escape_pending(tmp_path):
    """最坏情况：名字里带一堆 ../，落盘后必须还在 pending 里。"""
    out = U.save_uploads([("../../../../tmp/pwn.xlsx", b"x")], pending_dir=tmp_path)
    dest = tmp_path / out["saved"][0]["relpath"]
    assert dest.parent == tmp_path
    assert not (Path("/tmp/pwn.xlsx")).exists()


def test_batch_name_is_not_hidden():
    """批次目录名不能以 . 开头（否则 input_unit 当隐藏文件、main.py 会「没东西可读」）。"""
    name = U.new_batch_name()
    assert not name.startswith(".")
    assert not name.startswith("_")
    assert len(name) >= 15


# ── 待抽取清单 ─────────────────────────────────────────────────────────

def test_scan_pending_lists_files_folders_and_skips_noise(tmp_path):
    (tmp_path / "a.xlsx").write_bytes(b"1")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.pdf").write_bytes(b"22")
    (tmp_path / "sub" / "c.txt").write_bytes(b"333")
    (tmp_path / "empty").mkdir()                        # 空目录（搬空的残留）
    (tmp_path / "_claimed").mkdir()                     # 扫描器认领中
    (tmp_path / "_claimed" / "x.xlsx").write_bytes(b"4")
    (tmp_path / ".hidden.xlsx").write_bytes(b"5")       # 隐藏文件
    (tmp_path / "a.xlsx.meta.json").write_bytes(b"6")   # 邮件副档
    (tmp_path / "notes.docx").write_bytes(b"7777")

    items = {it["name"]: it for it in U.scan_pending(tmp_path)}
    assert set(items) == {"a.xlsx", "sub", "notes.docx"}
    assert items["sub"]["kind"] == "folder"
    assert items["sub"]["n_files"] == 2
    assert items["sub"]["bytes"] == 5
    assert items["a.xlsx"]["kind"] == "file"


def test_scan_pending_reports_age(tmp_path):
    f = tmp_path / "old.xlsx"
    f.write_bytes(b"1")
    old = datetime.now() - timedelta(minutes=7)
    import os
    os.utime(f, (old.timestamp(), old.timestamp()))
    item = U.scan_pending(tmp_path)[0]
    assert 400 < item["age_seconds"] < 500


def test_scan_pending_missing_dir_is_empty(tmp_path):
    assert U.scan_pending(tmp_path / "nope") == []
