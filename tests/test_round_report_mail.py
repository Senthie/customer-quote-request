"""round_report_mail.py：一轮一封汇总回执 —— 组信 / 去重 / 不重发 的回归。

不打真 SMTP：把 send_raw 换掉，只看「该不该发、发什么、台账怎么记」。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import round_report_mail as rrm  # noqa: E402


def _receipt(root: Path, task: str, status: str, *, subject: str | None = None,
             bdbh: str = "", stamp: str = "2026-11-20T03:43:20",
             extra: dict | None = None) -> Path:
    task_dir = root / task
    unit = task_dir / "unit_x"
    unit.mkdir(parents=True, exist_ok=True)
    if subject is not None:
        (unit / "_email.meta.json").write_text(
            json.dumps({"subject": subject}, ensure_ascii=False), encoding="utf-8")
    payload = {"status": status, "at": stamp}
    if bdbh:
        payload["bdbh"] = bdbh
    payload.update(extra or {})
    path = task_dir / f"{task}.erp-push.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    ledger = tmp_path / "logs" / "round_report_sent.jsonl"
    monkeypatch.setattr(rrm, "LEDGER", ledger)
    sent: list[str] = []
    monkeypatch.setattr(rrm, "send_raw", lambda account, raw, timeout=90: (sent.append(raw), (True, ""))[1])
    root = tmp_path / "processed"
    root.mkdir()
    return root, ledger, sent


def test_nothing_to_send_stays_silent(env, capsys):
    root, ledger, sent = env
    rc = rrm.main(["--root", str(root)])
    assert rc == 0
    assert sent == []
    assert not ledger.exists() or ledger.read_text().strip() == ""
    assert "不发邮件" in capsys.readouterr().out


def test_one_mail_for_whole_round_with_failure_note(env):
    root, ledger, sent = env
    _receipt(root, "taskA", "created", subject="Fwd: Sleuth and Clue", bdbh="BJXQ261120009")
    _receipt(root, "taskB", "invalid", subject="Neue Titel 2027", bdbh="",
             extra={"problems": ["客户名称没命中：SGX"]})
    assert rrm.main(["--root", str(root)]) == 0
    assert len(sent) == 1, "一轮多封也只能发一封"
    raw = sent[0]
    assert "| 邮件标题 | ERP表单号 | 是否录入ERP|" in raw
    assert "| Fwd: Sleuth and Clue | BJXQ261120009 | 是 |" in raw
    assert "| Neue Titel 2027 | — | 否 |" in raw
    assert "备注：" in raw and "客户名称没命中：SGX" in raw
    assert "本轮 2 条" in raw
    # 台账一次记全
    rec = json.loads(ledger.read_text(encoding="utf-8").splitlines()[-1])
    assert rec["status"] == "sent" and rec["count"] == 2


def test_dry_run_sends_nothing_and_records_nothing(env, capsys):
    root, ledger, sent = env
    _receipt(root, "taskA", "created", subject="X", bdbh="BJXQ1")
    assert rrm.main(["--root", str(root), "--dry-run"]) == 0
    assert sent == []
    assert not ledger.exists()
    assert "BJXQ1" in capsys.readouterr().out


def test_second_run_does_not_resend(env, capsys):
    root, ledger, sent = env
    _receipt(root, "taskA", "created", subject="X", bdbh="BJXQ1")
    assert rrm.main(["--root", str(root)]) == 0
    assert len(sent) == 1
    # 同一份回执再跑一次：去重 → 不发
    assert rrm.main(["--root", str(root)]) == 0
    assert len(sent) == 1
    assert "不发邮件" in capsys.readouterr().out


def test_failed_send_is_not_retried(env, monkeypatch):
    root, ledger, sent = env
    _receipt(root, "taskA", "created", subject="X", bdbh="BJXQ1")
    monkeypatch.setattr(rrm, "send_raw", lambda account, raw, timeout=90: (False, "smtp boom"))
    assert rrm.main(["--root", str(root)]) == 0  # 不因发信失败让整轮失败
    rec = json.loads(ledger.read_text(encoding="utf-8").splitlines()[-1])
    assert rec["status"] == "send_failed"
    # 第二趟不该再试
    calls: list[str] = []
    monkeypatch.setattr(rrm, "send_raw", lambda account, raw, timeout=90: (calls.append(raw), (True, ""))[1])
    assert rrm.main(["--root", str(root)]) == 0
    assert calls == []


def test_uid_quirk_counts_as_sent(monkeypatch):
    """腾讯企业邮老毛病：SMTP 已投递但 himalaya 非零退出，不能当失败。"""
    class Proc:
        returncode = 1
        stdout = ""
        stderr = "Error: cannot find UID of appended IMAP message"

    monkeypatch.setattr(rrm.subprocess, "run", lambda *a, **k: Proc())
    ok, tail = rrm.send_raw("demochen", "raw")
    assert ok is True
    assert "已投递" in tail


def test_dry_run_skipped(env):
    root, ledger, sent = env
    _receipt(root, "taskA", "dry_run", subject="X", bdbh="BJXQ1")
    assert rrm.main(["--root", str(root)]) == 0
    assert sent == []


def test_init_marks_history_without_sending(env, capsys):
    root, ledger, sent = env
    _receipt(root, "taskA", "created", subject="X", bdbh="BJXQ1")
    assert rrm.main(["--root", str(root), "--init"]) == 0
    assert sent == []
    assert json.loads(ledger.read_text(encoding="utf-8").splitlines()[-1])["status"] == "init_skip"
    # 登记过之后不再发
    assert rrm.main(["--root", str(root)]) == 0
    assert sent == []
    assert "不发邮件" in capsys.readouterr().out


def test_subject_falls_back_to_artifact_name(env):
    root, ledger, sent = env
    _receipt(root, "taskA", "created", subject=None, bdbh="BJXQ1")
    assert rrm.main(["--root", str(root)]) == 0
    assert "taskA" in sent[0]
