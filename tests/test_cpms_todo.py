# -*- coding: utf-8 -*-
"""scripts/cpms_todo.py 的回归测试。

背景（2026-10-21）：定时任务原来只跑抽取（no_agent 纯脚本），抽取完 CPMS 永远空着，
没人接手，产物停在未完成状态。补了 cpms_todo.py 当交接棒 + 把 cron 改成 agent 模式后，
这里钉住三件事：
  1. 什么算「欠账」（CPMS 空 / 中文品名是占位符）
  2. 什么算「已完成」（不该再出现在清单里，否则每 5 分钟重复劳动）
  3. 输出契约（cron 的 shell 壳靠 `CPMS_TODO: N` 这一行判断要不要出声）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import cpms_todo  # noqa: E402


def _task(root: Path, task_id: str, *, cpms: str, cpmc: str, khdh: str = "CU001",
          cpmc_en: str = "产品名") -> Path:
    d = root / task_id
    d.mkdir(parents=True)
    payload = {"KHDH": khdh, "CPMS": cpms, "CPMC": cpmc_en, "CPMC_ZH": cpmc,
               "CPLB": "23.01;无线胶装书"}
    (d / f"ERP_{task_id}_20260101_000000.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    (d / "some_customer_file.extracted.json").write_text("{}", encoding="utf-8")
    return d


@pytest.fixture()
def processed(tmp_path: Path) -> Path:
    root = tmp_path / "processed"
    root.mkdir()
    return root


def test_空目录返回空清单(processed: Path):
    assert cpms_todo.iter_incomplete(processed) == []
    assert cpms_todo.format_report([]) == "CPMS_TODO: 0"


def test_目录不存在不炸(tmp_path: Path):
    assert cpms_todo.iter_incomplete(tmp_path / "nope") == []


def test_CPMS为空算欠账(processed: Path):
    _task(processed, "aaa", cpms="", cpmc="图画书")
    items = cpms_todo.iter_incomplete(processed)
    assert len(items) == 1
    assert items[0]["missing"] == ["CPMS"]


def test_中文品名占位符算欠账(processed: Path):
    _task(processed, "bbb", cpms="这是完整描述", cpmc=cpms_todo.CPMC_PLACEHOLDER)
    items = cpms_todo.iter_incomplete(processed)
    assert items[0]["missing"] == ["CPMC_ZH"]


def test_CPMC占位符也算欠账():
    """2026-10-21 补：CPMC 与 CPMC_ZH 是两个字段，只补一个会漏（清单显示 0 但 ERP 带占位符出去）。"""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _task(root, "only_zh", cpms="完整描述", cpmc="儿童涂色本",
              cpmc_en=cpms_todo.CPMC_PLACEHOLDER)
        items = cpms_todo.iter_incomplete(root)
        assert items[0]["missing"] == ["CPMC"]


def _with_profile(root: Path, task_id: str, profile_id: str) -> None:
    """给任务目录的 extracted.json 写上客户档案 id（cpms_todo 靠它判 ECPMC 要不要查）。"""
    p = next((root / task_id).glob("*.extracted.json"))
    p.write_text(json.dumps({"profile_id": profile_id}), encoding="utf-8")


def test_NGV的ECPMC空着算欠账(processed: Path):
    """NGV 档案声明了 rules.ecpmc.source_keys → ECPMC 空着就是缺陷（业务 2026-10-20 指出过）。"""
    _task(processed, "ngv1", cpms="完整描述", cpmc="儿童涂色书")
    _with_profile(processed, "ngv1", "ngv")
    items = cpms_todo.iter_incomplete(processed)
    assert items and items[0]["missing"] == ["ECPMC"]


def test_igl的ECPMC空着不算欠账(processed: Path):
    """igl 业务明确「先不做」（2026-09-24），档案没声明 ecpmc → 空着是正确的，不能报警。"""
    _task(processed, "igl1", cpms="完整描述", cpmc="23.01;无线胶装书-系列")
    _with_profile(processed, "igl1", "igl")
    assert cpms_todo.iter_incomplete(processed) == []


def test_没有档案信息时不猜ECPMC要不要(processed: Path):
    """extracted.json 缺失/没 profile_id → 判不出就不报（宁可漏，不制造噪音）。"""
    _task(processed, "noprof", cpms="完整描述", cpmc="某品名")
    assert cpms_todo.iter_incomplete(processed) == []


def test_ECPMC一填上就消失(processed: Path):
    d = _task(processed, "ngv2", cpms="完整描述", cpmc="儿童涂色书")
    _with_profile(processed, "ngv2", "ngv")
    erp = next(d.glob("ERP_*.json"))
    payload = json.loads(erp.read_text(encoding="utf-8"))
    payload["ECPMC"] = "Malbücher"      # 原始产品名，德文照抄
    erp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert cpms_todo.iter_incomplete(processed) == []


def test_两项都缺时都列出来(processed: Path):
    _task(processed, "ccc", cpms="  ", cpmc="")
    assert cpms_todo.iter_incomplete(processed)[0]["missing"] == ["CPMS", "CPMC_ZH"]


def test_都写完就不该再出现(processed: Path):
    _task(processed, "ddd", cpms="完整的中文产品描述正文", cpmc="精装绘本")
    assert cpms_todo.iter_incomplete(processed) == []


def test_多个任务按产物新鲜度倒序(processed: Path):
    _task(processed, "old", cpms="", cpmc="x")
    new = _task(processed, "new", cpms="", cpmc="y")
    # 把 new 的 ERP 文件时间推后
    erp = next(new.glob("ERP_*.json"))
    erp.touch()
    import os, time  # noqa: E401

    os.utime(erp, (time.time() + 10, time.time() + 10))
    ids = [it["task_id"] for it in cpms_todo.iter_incomplete(processed)]
    assert ids[0] == "new"


def test_重跑过的目录只认最新那份ERP(processed: Path):
    d = _task(processed, "eee", cpms="", cpmc="占位")
    older = d / "ERP_eee_20250101_000000.json"
    older.write_text(json.dumps({"CPMS": "", "CPMC_ZH": ""}), encoding="utf-8")
    import os, time  # noqa: E401

    old_t = time.time() - 1000
    os.utime(older, (old_t, old_t))
    items = cpms_todo.iter_incomplete(processed)
    assert items[0]["erp_file"].endswith("ERP_eee_20260101_000000.json")


def test_坏JSON跳过而不是让整轮挂掉(processed: Path):
    d = processed / "fff"
    d.mkdir()
    (d / "ERP_fff_1.json").write_text("{not json", encoding="utf-8")
    _task(processed, "ggg", cpms="", cpmc="占位")
    items = cpms_todo.iter_incomplete(processed)
    assert [it["task_id"] for it in items] == ["ggg"]


def test_下划线和点开头的目录不算产物(processed: Path):
    _task(processed, "_claimed", cpms="", cpmc="占位")
    _task(processed, ".hidden", cpms="", cpmc="占位")
    assert cpms_todo.iter_incomplete(processed) == []


def test_输出契约第一行固定(processed: Path):
    _task(processed, "hhh", cpms="", cpmc=cpms_todo.CPMC_PLACEHOLDER)
    items = cpms_todo.iter_incomplete(processed)
    report = cpms_todo.format_report(items)
    assert report.splitlines()[0] == "CPMS_TODO: 1"
    assert "task=hhh" in report
    assert "缺=CPMS/CPMC_ZH" in report


def test_清单里带单号方便人工核对(processed: Path):
    _task(processed, "iii", cpms="", cpmc="占位", khdh="CU26070901")
    report = cpms_todo.format_report(cpms_todo.iter_incomplete(processed))
    assert "单号=CU26070901" in report


def test_命令行_JSON_模式可被脚本消费(processed: Path, capsys):
    _task(processed, "jjj", cpms="", cpmc="占位")
    assert cpms_todo.main(["--dir", str(processed), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["task_id"] == "jjj"
