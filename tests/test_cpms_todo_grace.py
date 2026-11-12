# -*- coding: utf-8 -*-
"""`cpms_todo` 的并发宽限（2026-11-11 现场事故的回归）。

事故：agent 手工处理一封 `.eml`（10:05 抽完、10:06 起手写 CPMS），同一时刻
`*/5` 的 cron 看到「CPMS 空」→ 把同一条待办交给另一个 agent 会话，
两边在 10:10 前后互相覆盖产物（字段、`CPMS_来源对照.md`、`.erp-import.json`）。

修法：任务目录里**任何文件**最近 N 分钟被写过 → 本轮不进待办。
这里钉住五个判据：
  1. 库层默认不启用（`grace_minutes=0`）—— 人工查询所见即所报；
  2. 宽限窗口内 → 不进欠账清单，进「跳过」清单，并带 `idle_seconds`；
  3. 宽限窗口外 → 照常报；
  4. 判断只看任务目录里最新的那个文件（`.md` 刚写、JSON 还旧，也算有人在写）；
  5. 已完成的任务两边都不进（跳过清单只解释「欠账却没报」，不是第二份欠账）。

另外两条命令层面的：`--grace-minutes` 生效、`# 跳过…` 只走 stderr（stdout 是 cron 契约）。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import cpms_todo  # noqa: E402


def _task(root: Path, task_id: str, *, cpms: str = "", cpmc: str = "占位",
          cpmc_en: str = "产品名") -> Path:
    d = root / task_id
    d.mkdir(parents=True)
    payload = {"KHDH": "CU001", "CPMS": cpms, "CPMC": cpmc_en, "CPMC_ZH": cpmc,
               "CPLB": "27.01;普通BB书"}
    (d / f"ERP_{task_id}_20260101_000000.json").write_text(
        json.dumps([payload], ensure_ascii=False), encoding="utf-8"
    )
    (d / "some_customer_file.extracted.json").write_text("{}", encoding="utf-8")
    return d


def _age(directory: Path, minutes: float) -> None:
    """把目录里所有文件（含子目录）的时间往前推 N 分钟。"""
    stamp = time.time() - minutes * 60
    for p in sorted(directory.rglob("*"), reverse=True):
        os.utime(p, (stamp, stamp))


def test_库层默认不启用宽限(tmp_path: Path):
    processed = tmp_path / "p"
    processed.mkdir()
    _task(processed, "fresh")
    # 文件是刚写的，但 grace 默认 0 → 照样报（人工查账不被时间窗骗）
    assert [it["task_id"] for it in cpms_todo.iter_incomplete(processed)] == ["fresh"]
    items, skipped = cpms_todo.scan(processed)
    assert len(items) == 1 and skipped == []


def test_宽限窗口内跳过并进跳过清单(tmp_path: Path):
    processed = tmp_path / "p"
    processed.mkdir()
    _task(processed, "being_written")
    items, skipped = cpms_todo.scan(processed, grace_minutes=15)
    assert items == []
    assert [it["task_id"] for it in skipped] == ["being_written"]
    assert skipped[0]["skipped"] == "recent-write"
    assert skipped[0]["idle_seconds"] < 60
    assert "CPMS" in skipped[0]["missing"]


def test_超过宽限照常报(tmp_path: Path):
    processed = tmp_path / "p"
    processed.mkdir()
    d = _task(processed, "stale")
    _age(d, 30)
    items, skipped = cpms_todo.scan(processed, grace_minutes=15)
    assert [it["task_id"] for it in items] == ["stale"]
    assert skipped == []


def test_目录里任一文件刚写也算有人在写(tmp_path: Path):
    """产物 JSON 半小时没动，但 `CPMS_来源对照.md` 刚写 → 仍然跳过。

    手工处理时先写 md 再写 JSON 是常态，只盯 ERP 文件会漏判。
    """
    processed = tmp_path / "p"
    processed.mkdir()
    d = _task(processed, "md_only")
    erp = next(d.glob("ERP_*.json"))
    old = time.time() - 30 * 60
    os.utime(erp, (old, old))
    os.utime(d / "some_customer_file.extracted.json", (old, old))
    # md 是刚写的（write_text 的默认 mtime = now）
    (d / "CPMS_来源对照.md").write_text("# 对照\n", encoding="utf-8")

    items, skipped = cpms_todo.scan(processed, grace_minutes=15)
    assert items == []
    assert [it["task_id"] for it in skipped] == ["md_only"]


def test_已完成的任务两边都不进(tmp_path: Path):
    processed = tmp_path / "p"
    processed.mkdir()
    d = _task(processed, "done", cpms="内文：10PP，300g单粉咭(C1S)，4C/4C", cpmc="图画书",
              cpmc_en="图画书")
    os.utime(d, None)  # 目录刚动过
    items, skipped = cpms_todo.scan(processed, grace_minutes=15)
    assert items == [] and skipped == []


def test_新的跳过_旧的照报_一起出现(tmp_path: Path):
    processed = tmp_path / "p"
    processed.mkdir()
    old = _task(processed, "old")
    _age(old, 120)
    _task(processed, "new")
    items, skipped = cpms_todo.scan(processed, grace_minutes=15)
    assert [it["task_id"] for it in items] == ["old"]
    assert [it["task_id"] for it in skipped] == ["new"]
    assert cpms_todo.format_report(items).splitlines()[0] == "CPMS_TODO: 1"


def test_命令行_grace_minutes_生效_跳过信息只走_stderr(tmp_path: Path, capsys):
    processed = tmp_path / "p"
    processed.mkdir()
    _task(processed, "busy")

    assert cpms_todo.main(["--dir", str(processed), "--grace-minutes", "15"]) == 0
    captured = capsys.readouterr()
    # stdout 仍是契约：只有 CPMS_TODO 行（cron 的 shell 壳按这行决定要不要出声）
    assert captured.out.strip() == "CPMS_TODO: 0"
    assert "busy" not in captured.out
    # 跳过原因进 stderr（被 cron 壳重定向到 /tmp/cqr_scan.stderr，可回查）
    assert "# 跳过 1 条欠账" in captured.err
    assert "busy" in captured.err


def test_命令行_show_skipped_给人看(tmp_path: Path, capsys):
    processed = tmp_path / "p"
    processed.mkdir()
    _task(processed, "busy")
    assert cpms_todo.main(["--dir", str(processed), "--grace-minutes", "15",
                           "--show-skipped"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "CPMS_TODO: 0"
    assert "· 跳过 1 条（宽限 15 分钟）" in out
    assert "task=busy" in out


def test_extracted_被写成数组也不炸(tmp_path: Path):
    """`.extracted.json` 不是对象时，只跳过档案识别，不能让整轮清单挂掉。

    2026-11-11 手工验证时踩到：把产物数组拷成 `*.extracted.json` →
    `_profile_id` 里 `data.get` 直接 AttributeError，**整轮 cron 都会死**。
    """
    processed = tmp_path / "p"
    processed.mkdir()
    d = _task(processed, "arr")
    (d / "some_customer_file.extracted.json").write_text(
        json.dumps([{"profile_id": "ngv"}], ensure_ascii=False), encoding="utf-8"
    )
    items = cpms_todo.iter_incomplete(processed)
    assert [it["task_id"] for it in items] == ["arr"]

