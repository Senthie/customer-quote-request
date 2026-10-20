"""「一份报价 = 单文件 | 文件夹 | 压缩包」的回归测试。

业务口径（2026-10-20 森西拍板）：
  * **单个文件 = 独一份报价**（1 个文件 → 1 个 ERP JSON，行为与以前完全一致）
  * **文件夹 / 压缩包里的文件 = 这一份报价的全部参考文件**
    → 多个文件**合并成一份**，仍然只出 1 个 ERP JSON
  * **文件夹 / 压缩包里还有子目录 → 子目录各算一份报价**（1.0.38 补充拍板）
    —— 一个文件夹里躺着两张独立单，绝不许合并成一份

这层测试锁四件事：
  1. 单元识别（file / folder / archive）+ 垃圾文件过滤 + zip 安全
  2. 合并口径（主文件优先、冲突记账、扫描类在整个单元上重跑）
  3. 端到端：一个文件夹跑出来只有一个 ERP JSON，且 validation 里记着成员清单
  4. 批次拆分：子目录各出一份、根下散文件另算一份、压缩包只归档一次、父目录清空
"""
from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from extract_fields import CustomerData, merge_customer_data
from input_unit import finalize_batch, resolve_input_unit, resolve_input_units

REPO_ROOT = Path(__file__).resolve().parent.parent
NGV_YAML = REPO_ROOT / "customers" / "ngv.yaml"


def _profile():
    from customer_profile import load_profile
    return load_profile("ngv")


# ── 1. 单元识别 ──────────────────────────────────────────────────────────
def test_单文件就是一个单元一份报价(tmp_path):
    f = tmp_path / "quote.xlsx"
    f.write_bytes(b"x")
    unit = resolve_input_unit(f)
    assert unit.kind == "file"
    assert unit.is_single_file
    assert unit.members == [f]
    assert "单文件" in unit.label


def test_文件夹里的文件都是同一份报价的参考文件(tmp_path):
    folder = tmp_path / "询价包"
    folder.mkdir()
    (folder / "规格表.xlsx").write_bytes(b"x")
    (folder / "确认单.docx").write_bytes(b"x")
    (folder / "说明.txt").write_text("补充：FSC 认证", encoding="utf-8")
    # 干扰项：Office 临时文件 / 系统垃圾 / 不支持的类型
    (folder / "~$规格表.xlsx").write_bytes(b"x")
    (folder / ".DS_Store").write_bytes(b"x")
    (folder / "参考图.jpg").write_bytes(b"x")

    unit = resolve_input_unit(folder)
    assert unit.kind == "folder"
    assert not unit.is_single_file
    names = sorted(m.name for m in unit.members)
    assert names == sorted(["规格表.xlsx", "确认单.docx", "说明.txt"])
    assert any("~$" in s for s in unit.skipped)
    assert any(s.endswith(".jpg") for s in unit.skipped)


def test_压缩包解出来的文件也属于同一个单元(tmp_path):
    zip_path = tmp_path / "报价.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("spec.xlsx", b"x")
        zf.writestr("sub/notes.txt", "备注：14 本/箱")
        zf.writestr("__MACOSX/._spec.xlsx", b"junk")

    unit = resolve_input_unit(zip_path)
    try:
        assert unit.kind == "archive"
        assert sorted(m.name for m in unit.members) == ["notes.txt", "spec.xlsx"]
        assert unit.temp_dir and unit.temp_dir.exists()
        assert any("__MACOSX" in s for s in unit.skipped)
    finally:
        temp = unit.temp_dir
        unit.cleanup()
        assert not temp.exists(), "zip 临时目录用完必须删掉"


def test_压缩包拒绝路径穿越(tmp_path):
    zip_path = tmp_path / "evil.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("../../evil.txt", "x")
    with pytest.raises(ValueError):
        resolve_input_unit(zip_path)


def test_空文件夹和空压缩包要说清楚(tmp_path):
    empty = tmp_path / "空目录"
    empty.mkdir()
    with pytest.raises(ValueError):
        resolve_input_unit(empty)


# ── 2. 合并口径 ──────────────────────────────────────────────────────────
def _data(values=None, scanned=None, blocks=None, layout="text_lines"):
    return CustomerData(values=values or {}, scanned=scanned or {},
                        text_blocks=blocks or [], layout=layout, profile_id="ngv")


def test_合并_主文件优先_不同值记账():
    spec = _data({"size": "148 x 210 mm portrait", "quantity": "30.100cps",
                  "text_material": "100 gsm offset paper"},
                 blocks=["Title: Ostermalblock", "TPS: 148 x 210 mm portrait"])
    note = _data({"quantity": "28.000cps"}, blocks=["补充：数量改为 28.000cps"])

    merged = merge_customer_data([("规格.txt", spec), ("补充.txt", note)], _profile())
    assert merged.values["size"] == "148 x 210 mm portrait"
    assert merged.values["quantity"] == "30.100cps", "主文件（规范键多的）胜出"
    assert merged.sources["quantity"] == "规格.txt"
    # 冲突必须留痕，不许静默丢
    assert any(c["key"] == "quantity" and c["other"] == "28.000cps" for c in merged.conflicts)
    # 文本块按文件分段，CPMS/溯源要看得到出处
    assert "# 文件：规格.txt" in merged.text_blocks
    assert "# 文件：补充.txt" in merged.text_blocks


def test_合并_扫描类在整个单元上重跑():
    """FSC 这类 unique 键必须跨文件判：A 写 Mix、B 写 Recycled = 真混用 → 留空。"""
    a = _data({"size": "148 x 210"}, blocks=["cover (FSC-Mix)"])
    b = _data({}, blocks=["notes: FSC Recycled accepted"])
    merged = merge_customer_data([("a.txt", a), ("b.txt", b)], _profile())
    assert "fsc" not in merged.scanned
    assert any("不唯一就留空" in n for n in merged.scan_notes)


def test_合并_单一口径的扫描值照常落库():
    a = _data({"size": "148 x 210"}, blocks=["FSC Mix"])
    b = _data({}, blocks=["FSC mix（同一值，写法不同）"])
    merged = merge_customer_data([("a.txt", a), ("b.txt", b)], _profile())
    assert merged.scanned.get("fsc", "").lower().replace("-", " ") == "fsc mix"


# ── 3. 端到端：一个文件夹 = 一份报价 = 一个 ERP JSON ─────────────────────
def test_端到端_文件夹只产出一份报价(tmp_path):
    folder = tmp_path / "CU26079999_询价包"
    folder.mkdir()
    (folder / "01_规格.txt").write_text(
        "Title: Ostertestbook\n"
        "Quantity: 10.000cps (one title, one language)\n"
        "TPS: 148 x 210 mm portrait\n"
        "Extent: 32 pp + cover\n"
        "Cover: 230 gsm artcard C1S (FSC Mix)\n"
        "Content: 100 gsm offset paper (FSC Mix)\n"
        "Finished binding: softcover, perfect binding\n",
        encoding="utf-8")
    (folder / "02_补充.txt").write_text(
        "Notes: 14 books per export carton\n", encoding="utf-8")

    processed = tmp_path / "processed"
    failed = tmp_path / "failed"
    processed.mkdir()
    failed.mkdir()

    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "main.py"), str(folder),
         "--customer", "ngv", "--processed-dir", str(processed),
         "--failed-dir", str(failed), "--today", "2026-10-20"],
        capture_output=True, text=True, cwd=REPO_ROOT)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    erp_files = list(processed.glob("*/ERP_*.json"))
    assert len(erp_files) == 1, f"一个文件夹只能出一份报价，实际 {len(erp_files)} 份"
    task_dir = erp_files[0].parent

    # 原始文件夹整包归档（参考文件必须待在一起）
    assert (task_dir / folder.name).is_dir()

    # validation 里记着「这份报价由哪几个文件组成」
    validation = json.loads(next(task_dir.glob("*.validation.json")).read_text(encoding="utf-8"))
    assert validation["unit"]["kind"] == "folder"
    assert sorted(validation["unit"]["members"]) == ["01_规格.txt", "02_补充.txt"]

    # 合并后的字段确实来自两个文件（装箱那句只写在 02）
    data = json.loads(erp_files[0].read_text(encoding="utf-8"))
    assert data["CPLB"].startswith("23.06"), data["CPLB"]
    assert data["CPSL1"] == 10000
    assert (data["CD"], data["KD"]) == (148, 210)


# ── 4. 批次拆分：文件夹里有子目录 = 多份报价（1.0.38）──────────────────────
def _spec(title: str, quantity: str, size: str) -> str:
    return (f"Title: {title}\n"
            f"Quantity: {quantity}\n"
            f"TPS: {size}\n"
            "Extent: 32 pp + cover\n"
            "Cover: 230 gsm artcard C1S (FSC Mix)\n"
            "Content: 100 gsm offset paper (FSC Mix)\n"
            "Finished binding: softcover, perfect binding\n")


def test_有子目录_子目录各算一份_根下散文件另算一份(tmp_path):
    folder = tmp_path / "两个单"
    (folder / "CU26070001").mkdir(parents=True)
    (folder / "CU26070001" / "01_规格.txt").write_text("x", encoding="utf-8")
    (folder / "CU26070001" / "02_补充.txt").write_text("x", encoding="utf-8")
    (folder / "CU26070002").mkdir()
    (folder / "CU26070002" / "inner").mkdir()          # 子目录里再分子目录：不拆，继续合并
    (folder / "CU26070002" / "inner" / "规格.txt").write_text("x", encoding="utf-8")
    (folder / "说明.txt").write_text("x", encoding="utf-8")   # 根下的散文件 = 第 3 份
    (folder / "_email.meta.json").write_text("{}", encoding="utf-8")

    units = resolve_input_units(folder)
    assert [u.stem for u in units] == ["CU26070001", "CU26070002", "两个单_根目录文件"]
    assert [u.role for u in units] == ["subdir", "subdir", "root_files"]
    # 子目录内部不再拆：两个文件都归第一份
    assert sorted(m.name for m in units[0].members) == ["01_规格.txt", "02_补充.txt"]
    assert [m.name for m in units[1].members] == ["规格.txt"]
    # 副档是我们自己的记账，不该被报成「跳过的客户文件」
    assert units[0].skipped == []


def test_没有子目录时老口径不变_一份(tmp_path):
    folder = tmp_path / "一个单"
    folder.mkdir()
    (folder / "a.txt").write_text("x", encoding="utf-8")
    units = resolve_input_units(folder)
    assert len(units) == 1 and units[0].role == "whole"


def test_压缩包按子目录拆_原包只归档一次(tmp_path):
    zip_path = tmp_path / "两张单.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("单A/规格.txt", "x")
        zf.writestr("单B/规格.txt", "x")
    units = resolve_input_units(zip_path)
    try:
        assert [u.stem for u in units] == ["单A", "单B"]
        assert all(u.from_archive for u in units)
        # 压缩包本身只由第 1 份归档，不复制 N 份
        assert [u.archive_owner for u in units] == [True, False]
        assert units[0].origin == zip_path
        assert units[0].temp_dir and units[0].temp_dir == units[1].temp_dir
    finally:
        temp = units[0].temp_dir
        for unit in units:
            unit.cleanup()
        assert not temp.exists(), "zip 临时目录用完必须删掉"


def test_批次收尾_搬空的父目录会被删掉(tmp_path):
    folder = tmp_path / "两个单"
    (folder / "A").mkdir(parents=True)
    (folder / "B").mkdir()
    (folder / "A" / "规格.txt").write_text("x", encoding="utf-8")
    (folder / "B" / "规格.txt").write_text("x", encoding="utf-8")
    (folder / "_email.meta.json").write_text("{}", encoding="utf-8")
    units = resolve_input_units(folder)
    # 模拟两份都已经归档搬走
    for unit in units:
        for member in unit.members:
            member.unlink()
        unit.path.rmdir()

    note = finalize_batch(units)
    assert note and "已清空" in note
    assert not folder.exists()


def test_批次收尾_父目录还有没用的东西就留着不删(tmp_path):
    folder = tmp_path / "两个单"
    (folder / "A").mkdir(parents=True)
    (folder / "B").mkdir()
    (folder / "A" / "规格.txt").write_text("x", encoding="utf-8")
    (folder / "B" / "规格.txt").write_text("x", encoding="utf-8")
    (folder / "参考图.jpg").write_bytes(b"x")     # 不支持的类型，谁也拿不走
    units = resolve_input_units(folder)
    for unit in units:                      # 模拟两份都已归档搬走
        for member in unit.members:
            member.unlink()
        unit.path.rmdir()
    note = finalize_batch(units)
    assert note and "没被任何一份报价用上" in note
    assert folder.exists(), "还有东西时不许删父目录"
    assert (folder / "参考图.jpg").exists()


def test_端到端_两个子目录出两份报价(tmp_path):
    folder = tmp_path / "CU26079999_两单"
    (folder / "CU26070001").mkdir(parents=True)
    (folder / "CU26070001" / "01_规格.txt").write_text(
        _spec("Ostertestbook A", "10.000cps", "148 x 210 mm portrait"), encoding="utf-8")
    (folder / "CU26070002").mkdir()
    (folder / "CU26070002" / "01_规格.txt").write_text(
        _spec("Ostertestbook B", "20.000cps", "210 x 280 mm portrait"), encoding="utf-8")

    processed = tmp_path / "processed"
    failed = tmp_path / "failed"
    processed.mkdir()
    failed.mkdir()

    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "main.py"), str(folder),
         "--customer", "ngv", "--processed-dir", str(processed),
         "--failed-dir", str(failed), "--today", "2026-10-20"],
        capture_output=True, text=True, cwd=REPO_ROOT)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    erp_files = sorted(processed.glob("*/ERP_*.json"))
    assert len(erp_files) == 2, f"两个子目录应该是两份报价，实际 {len(erp_files)} 份"

    quantities = {}
    for erp in erp_files:
        task_dir = erp.parent
        data = json.loads(erp.read_text(encoding="utf-8"))
        validation = json.loads(
            next(task_dir.glob("*.validation.json")).read_text(encoding="utf-8"))
        # 每份都归档了自己那个子目录，且 validation 记着角色
        subdir = validation["unit"]["path"]
        assert (task_dir / Path(subdir).name).is_dir(), "各自的参考文件要跟着自己走"
        assert validation["unit"]["role"] == "subdir"
        quantities[data["CPSL1"]] = data["CD"]
    assert quantities == {10000: 148, 20000: 210}, quantities

    # 父目录被搬空后删掉；pending 里不许留残骸
    assert not folder.exists()
    assert list(failed.iterdir()) == []


def test_口径1_根下散文件单独成单_不并进任何子目录(tmp_path):
    """森西 2026-10-20 拍板的口径 1：根下散文件自成一份 + validation 里留提醒。

    锁这条是为了防「以后有人图省事把根下文件塞进每个子目录」—— 那会让
    子单凭空多出不属于它的规格（量、材质都可能被带偏）。
    """
    folder = tmp_path / "两单加总则"
    (folder / "CU26070001").mkdir(parents=True)
    (folder / "CU26070001" / "01_规格.txt").write_text(
        _spec("Rootcase A", "10.000cps", "148 x 210 mm portrait"), encoding="utf-8")
    (folder / "CU26070002").mkdir()
    (folder / "CU26070002" / "01_规格.txt").write_text(
        _spec("Rootcase B", "20.000cps", "210 x 280 mm portrait"), encoding="utf-8")
    (folder / "总则.txt").write_text("Notes: FSC Mix accepted\n", encoding="utf-8")

    processed = tmp_path / "processed"
    failed = tmp_path / "failed"
    processed.mkdir()
    failed.mkdir()

    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "main.py"), str(folder),
         "--customer", "ngv", "--processed-dir", str(processed),
         "--failed-dir", str(failed), "--today", "2026-10-20"],
        capture_output=True, text=True, cwd=REPO_ROOT)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    validations = [json.loads(p.read_text(encoding="utf-8"))
                   for p in processed.glob("*/*.validation.json")]
    assert len(validations) == 3, "两个子目录 + 根下总则 = 3 份"

    by_role = {v["unit"]["role"]: v["unit"] for v in validations}
    assert set(by_role) == {"subdir", "root_files"}
    # 子单里绝不能出现「总则.txt」
    for v in validations:
        if v["unit"]["role"] == "subdir":
            assert "总则.txt" not in v["unit"]["members"], "根下文件不许并进子单"
            assert v["unit"]["note"] is None
    # 根下那份要被明确提醒「可能是共用说明」
    assert "共用的说明" in (by_role["root_files"]["note"] or "")
    assert "单独成一份" in proc.stdout + proc.stderr
