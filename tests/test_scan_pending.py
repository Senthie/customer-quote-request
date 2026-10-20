"""定时扫描器（`scripts/scan_pending.py`）。

不跑真抽取：用**桩 main.py** 模拟「成功（把输入搬走）」和「失败（原地不动）」。
验的是调度器自己那几条容易出事的规矩：

* 空 pending **一个字都不打印**（cron 靠这个保持安静，否则每 5 分钟骚扰一次）
* 认领 → 跑 → 成功即清；失败退回 pending 并记次数；3 次进 `failed/_stuck_*`
* 上一轮没跑完时，这一轮安静退出（flock 非阻塞）
"""
import fcntl
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import scan_pending as S  # noqa: E402


# ── 桩 main.py ────────────────────────────────────────────────────────

STUB_OK = textwrap.dedent('''
    """假 main.py：把输入搬进 processed/ 假装抽取成功。"""
    import shutil, sys
    from pathlib import Path
    src = Path(sys.argv[1])
    dest = Path({processed!r}) / "task_stub"
    dest.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest / src.name))
    (dest / "ERP_stub.json").write_text("{{}}", encoding="utf-8")
    print("stub: ok")
''')

STUB_FAIL = textwrap.dedent('''
    """假 main.py：报错退出，**不动输入**（模拟抽取失败）。"""
    import sys
    print("!! 不支持的文件格式: .foo", file=sys.stderr)
    sys.exit(1)
''')

STUB_SLOW = textwrap.dedent('''
    """假 main.py：卡住不返回（测超时）。"""
    import time
    time.sleep(30)
''')


@pytest.fixture
def env(tmp_path):
    """一套隔离目录：pending / _claimed / failed / state / 桩 main。"""
    pending = tmp_path / "pending"
    pending.mkdir()
    (tmp_path / "failed").mkdir()
    processed = tmp_path / "processed"
    processed.mkdir()
    return {
        "root": tmp_path,
        "pending": pending,
        "claim": pending / "_claimed",
        "failed": tmp_path / "failed",
        "processed": processed,
        "state": tmp_path / "state.json",
    }


def make_stub(env, template, name="fake_main.py"):
    path = env["root"] / name
    path.write_text(template.format(processed=str(env["processed"])), encoding="utf-8")
    return path


def run(env, **kwargs):
    return S.scan(pending_dir=env["pending"], claim_dir=env["claim"],
                  failed_dir=env["failed"], state_path=env["state"], **kwargs)


# ── 安静契约 ──────────────────────────────────────────────────────────

def test_empty_pending_prints_nothing(env, capsys):
    """cron 每 5 分钟跑一次 —— 没事可做时必须闭嘴。"""
    assert run(env) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_verbose_breaks_silence_on_purpose(env, capsys):
    run(env, verbose=True)
    assert "pending 空" in capsys.readouterr().out


def test_non_supported_noise_is_ignored(env, capsys):
    """pending 里躺着的邮件副档 / 隐藏文件不该被当成待处理。"""
    (env["pending"] / "a.xlsx.meta.json").write_text("{}", encoding="utf-8")
    (env["pending"] / ".DS_Store").write_text("x", encoding="utf-8")
    assert run(env) == 0
    assert capsys.readouterr().out == ""


# ── 成功路径 ──────────────────────────────────────────────────────────

def test_success_moves_input_and_reports(env, capsys):
    (env["pending"] / "报价.xlsx").write_bytes(b"1")
    stub = make_stub(env, STUB_OK)

    assert run(env, main_py=stub) == 0
    out = capsys.readouterr().out
    assert "成功 1" in out and "✅ 报价.xlsx" in out
    assert not (env["pending"] / "报价.xlsx").exists(), "成功的那份必须离开 pending"
    assert not (env["claim"] / "报价.xlsx").exists(), "认领区也要清干净"
    assert (env["processed"] / "task_stub" / "报价.xlsx").exists()


def test_folder_is_processed_as_one_unit(env, capsys):
    folder = env["pending"] / "一批参考文件"
    folder.mkdir()
    (folder / "a.xlsx").write_bytes(b"1")
    (folder / "b.pdf").write_bytes(b"2")
    stub = make_stub(env, STUB_OK)

    run(env, main_py=stub)
    assert "✅ 一批参考文件" in capsys.readouterr().out
    assert not folder.exists()


def test_limit_only_processes_n(env, capsys):
    for name in ("a.xlsx", "b.xlsx", "c.xlsx"):
        (env["pending"] / name).write_bytes(b"1")
    stub = make_stub(env, STUB_OK)

    run(env, main_py=stub, limit=1)
    assert "成功 1" in capsys.readouterr().out
    assert len([p for p in env["pending"].iterdir() if p.is_file()]) == 2


# ── 失败与重试 ────────────────────────────────────────────────────────

def test_failure_returns_item_to_pending_and_counts(env, capsys):
    (env["pending"] / "坏文件.foo").with_suffix(".xlsx").write_bytes(b"1")
    stub = make_stub(env, STUB_FAIL)

    run(env, main_py=stub)
    out = capsys.readouterr().out
    assert "失败 1" in out
    assert "不支持的文件格式" in out, "失败原因要原样带出来，别只说「失败」"
    assert (env["pending"] / "坏文件.xlsx").exists(), "失败的要退回 pending 等下一轮"
    assert S._read_state(env["state"])["坏文件.xlsx"] == 1


def test_third_failure_goes_to_stuck_bucket(env, capsys):
    (env["pending"] / "顽固.xlsx").write_bytes(b"1")
    stub = make_stub(env, STUB_FAIL)

    for _ in range(3):
        if (env["claim"] / "顽固.xlsx").exists():      # 上一轮退回过，这里不会发生
            break
        run(env, main_py=stub)

    buckets = list(env["failed"].glob("_stuck_*"))
    assert len(buckets) == 1
    assert (buckets[0] / "顽固.xlsx").exists()
    assert not (env["pending"] / "顽固.xlsx").exists()
    assert "顽固.xlsx" not in S._read_state(env["state"]), "进了 stuck 就别再记次数"
    assert "_stuck_" in capsys.readouterr().out


def test_success_clears_retry_counter(env):
    (env["pending"] / "先坏后好.xlsx").write_bytes(b"1")
    fail = make_stub(env, STUB_FAIL, name="fail.py")
    ok = make_stub(env, STUB_OK, name="ok.py")

    run(env, main_py=fail)
    assert S._read_state(env["state"])["先坏后好.xlsx"] == 1
    run(env, main_py=ok)
    assert "先坏后好.xlsx" not in S._read_state(env["state"])


def test_timeout_is_a_failure(env, capsys):
    (env["pending"] / "卡住.xlsx").write_bytes(b"1")
    stub = make_stub(env, STUB_SLOW)

    run(env, main_py=stub, timeout=1)
    assert "超时" in capsys.readouterr().out
    assert (env["pending"] / "卡住.xlsx").exists()


def test_stale_retry_counter_skips_without_running(env, capsys):
    """计数已经到上限、文件却还在 pending（例如手工挪回来的）→ 直接进 stuck。"""
    (env["pending"] / "旧账.xlsx").write_bytes(b"1")
    S._write_state(env["state"], {"旧账.xlsx": 3})
    stub = make_stub(env, STUB_OK)

    run(env, main_py=stub)
    assert list(env["failed"].glob("_stuck_*"))
    assert "旧账.xlsx" in capsys.readouterr().out


# ── dry-run ───────────────────────────────────────────────────────────

def test_dry_run_touches_nothing(env, capsys):
    (env["pending"] / "a.xlsx").write_bytes(b"1")
    (env["pending"] / "b.xlsx").write_bytes(b"2")

    run(env, dry_run=True)
    out = capsys.readouterr().out
    assert "待处理 2 项" in out and "a.xlsx" in out and "b.xlsx" in out
    assert len(list(env["pending"].iterdir())) == 2
    assert not env["claim"].exists()


# ── 互斥锁 ────────────────────────────────────────────────────────────

def test_second_round_exits_silently_while_locked(tmp_path, monkeypatch, capsys):
    """上一轮还没跑完 → 本轮不能抢同一批文件，也不能打日志。"""
    lock_path = tmp_path / ".scan.lock"
    monkeypatch.setattr(S, "LOCK_PATH", lock_path)
    monkeypatch.setattr(S, "PENDING_DIR", tmp_path / "pending")
    monkeypatch.setattr(S, "CUSTOMER_FILES", tmp_path)
    (tmp_path / "pending").mkdir()

    with open(lock_path, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert S.main([]) == 0
    assert capsys.readouterr().out == ""


def test_lock_is_released_after_run(tmp_path, monkeypatch, capsys):
    lock_path = tmp_path / ".scan.lock"
    monkeypatch.setattr(S, "LOCK_PATH", lock_path)
    monkeypatch.setattr(S, "PENDING_DIR", tmp_path / "pending")
    monkeypatch.setattr(S, "FAILED_DIR", tmp_path / "failed")
    monkeypatch.setattr(S, "CUSTOMER_FILES", tmp_path)
    monkeypatch.setattr(S, "STATE_PATH", tmp_path / "state.json")
    (tmp_path / "pending").mkdir()

    assert S.main([]) == 0                 # 空跑
    # 锁已经松开，再拿一次不会阻塞
    with open(lock_path, "w") as again:
        fcntl.flock(again, fcntl.LOCK_EX | fcntl.LOCK_NB)
