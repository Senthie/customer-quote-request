#!/usr/bin/env python3
"""
input_unit.py - 「报价的输入单元」：一份报价到底由几个文件组成

业务口径（2026-10-20 森西拍板）
--------------------------------------------------------------------
    * **单个文件 = 独一份报价**：一个 xlsx / csv / txt / eml 正文 → 一份报价、一个 ERP JSON。
    * **文件夹 / 压缩包里的文件 = 这一份报价的全部参考文件**：
      规格表 + 确认单 + 邮件截图 + 补充说明 …… 合起来才是**一份**报价，
      产出**一个** ERP JSON，而不是每个文件一份。

这也是为什么同一封邮件里带多个附件时，它们必须归到一个单元里
（`ingest_email.py` 会把它们投递到 `pending/<单元名>/`）—— 邮件是客户的一次询价，
附件只是这次询价的参考材料。

三种单元
--------------------------------------------------------------------
    file     : 路径本身就是一个支持的文件
    folder   : 目录 → 目录下（含子目录）所有受支持的文件
    archive  : .zip → 安全解压到临时目录后，同 folder

分裂：一个文件夹里装了**好几份**报价怎么办（1.0.38，森西拍板「子目录各算一份」）
--------------------------------------------------------------------
    * 文件夹 / 压缩包下**有子目录**（子目录里确实有受支持的文件）→ **子目录各算一份报价**，
      子目录内部的文件仍然合并（子目录里再分子目录也不拆，一路合并）。
    * 子目录之外、直接躺在根下的文件 → 单独算一份（`role="root_files"`），
      不会被偷偷并进某个子目录的报价里。
    * **没有子目录** → 老口径不变：整个文件夹 / 压缩包 = 一份报价。
    * 压缩包拆成多份时，压缩包本身只随**第 1 份**报价归档（`archive_owner`），
      其余份在 validation 里记 `source_archive` 指回去 —— 不复制 N 份压缩包。

⚠️ 这里只做「这一份报价包含哪些文件」的判断，不做字段合并。
   字段合并口径见 `extract_fields.merge_customer_data()`。
"""
from __future__ import annotations

import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# 与 extract_fields 保持一致：表格类 / 文本类
TABLE_SUFFIXES = (".xlsx", ".xls", ".csv")
TEXT_SUFFIXES = (".pdf", ".docx", ".txt")
SUPPORTED_SUFFIXES = TABLE_SUFFIXES + TEXT_SUFFIXES

# 压缩包里常见的垃圾/元数据，不要当成参考文件
JUNK_NAMES = {"__MACOSX", ".DS_Store", "Thumbs.db", "desktop.ini"}
JUNK_PREFIXES = ("~$", ".")          # Office 临时文件 / 隐藏文件

# 防炸：一个单元最多多少个文件、解压后最多多大
MAX_MEMBERS = 200
MAX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024


@dataclass
class InputUnit:
    """一份报价的输入单元。"""

    path: Path                     # 用户给的原始路径（文件 / 目录 / zip）；拆分子单元时为该子单元自己的路径
    kind: str                      # file | folder | archive
    members: list[Path] = field(default_factory=list)   # 真正要读的文件
    temp_dir: Optional[Path] = None                    # zip 解压的临时目录（用完要删）
    skipped: list[str] = field(default_factory=list)   # 被跳过的文件名（不支持/垃圾文件）
    # ── 批次拆分（1.0.38：文件夹里多个子目录 = 多份报价）──────────────────
    origin: Optional[Path] = None   # 拆分来源：原父文件夹 或 原压缩包
    role: str = "whole"             # whole（整个单元）| subdir（某个子目录）| root_files（根下散文件）
    from_archive: bool = False      # 子单元来自压缩包（path 在临时目录里）
    archive_owner: bool = False     # 同一批里由谁来归档原压缩包（只归档一次）

    # ── 展示 ──────────────────────────────────────────────────────────────
    @property
    def is_single_file(self) -> bool:
        return self.kind == "file"

    @property
    def is_batch_member(self) -> bool:
        """是不是「一个文件夹拆出来的其中一份」。"""
        return self.role != "whole"

    @property
    def stem(self) -> str:
        """产物文件名用的主干（单文件 = 文件名去后缀；单元 = 目录/压缩包名）。"""
        if self.kind == "file":
            return self.path.stem
        if self.kind == "archive":
            return self.path.stem
        if self.role == "root_files":
            base = self.origin.name if self.origin else self.path.name
            return f"{base}_根目录文件"
        return self.path.name or "unit"

    @property
    def label(self) -> str:
        if self.kind == "file":
            return "单文件（独一份报价）"
        if self.role == "subdir":
            where = "压缩包内" if self.from_archive else "文件夹内"
            return f"{where}子目录（{len(self.members)} 个参考文件）"
        if self.role == "root_files":
            return f"父目录根下的文件（{len(self.members)} 个参考文件）"
        if self.kind == "archive":
            return f"压缩包（{len(self.members)} 个参考文件）"
        return f"文件夹（{len(self.members)} 个参考文件）"

    @property
    def kind_cn(self) -> str:
        if self.role == "subdir":
            return "子目录"
        if self.role == "root_files":
            return "父目录根文件"
        return {"file": "单文件", "folder": "文件夹", "archive": "压缩包"}[self.kind]

    # ── 清理 ──────────────────────────────────────────────────────────────
    def cleanup(self) -> None:
        """删除 zip 解压的临时目录（幂等；同批多个单元共享同一个临时目录）。"""
        if self.temp_dir and self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)
        self.temp_dir = None

    def summary(self) -> dict:
        return {
            "kind": self.kind,
            "kind_cn": self.kind_cn,
            "path": str(self.path),
            "label": self.label,
            "role": self.role,
            "origin": str(self.origin) if self.origin else None,
            "from_archive": self.from_archive,
            "member_count": len(self.members),
            "members": [m.name for m in self.members],
            "skipped": list(self.skipped),
        }


def _is_junk(name: str) -> bool:
    if name in JUNK_NAMES:
        return True
    return any(name.startswith(p) for p in JUNK_PREFIXES)


def _is_bookkeeping(name: str) -> bool:
    """我们自己的副档（邮件上下文），不是客户的参考文件，也不算「被跳过」。"""
    return name.lower().endswith(".meta.json")


def _collect_files(root: Path, skipped: list[str]) -> list[Path]:
    """递归收受支持的文件（跳过垃圾/隐藏/临时文件）。"""
    out: list[Path] = []
    for item in sorted(root.rglob("*")):
        if item.is_dir():
            continue
        if _is_bookkeeping(item.name):
            continue
        if any(part in JUNK_NAMES for part in item.parts):
            skipped.append(str(item.relative_to(root)))   # 记账，别静默：
            continue                                       # 「为什么少了个文件」要能答
        if _is_junk(item.name):
            skipped.append(str(item.relative_to(root)))
            continue
        if item.suffix.lower() not in SUPPORTED_SUFFIXES:
            skipped.append(str(item.relative_to(root)))
            continue
        out.append(item)
    return out


def _safe_extract_zip(zip_path: Path, dest: Path) -> None:
    """解压 zip —— 拒绝路径穿越、限制体积与数量（不要 trust 客户给的压缩包）。"""
    with zipfile.ZipFile(zip_path) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > MAX_MEMBERS:
            raise ValueError(
                f"压缩包里有 {len(infos)} 个文件，超过上限 {MAX_MEMBERS} —— "
                f"请确认是不是把整个资料库打进来了")
        total = sum(i.file_size for i in infos)
        if total > MAX_UNCOMPRESSED_BYTES:
            raise ValueError(
                f"压缩包解压后 {total / 1024 / 1024:.1f} MB，超过上限 "
                f"{MAX_UNCOMPRESSED_BYTES / 1024 / 1024:.0f} MB")
        dest_resolved = dest.resolve()
        for info in infos:
            name = info.filename
            if name.startswith("/") or ".." in Path(name).parts or ":" in name:
                raise ValueError(f"压缩包里出现可疑路径，拒绝解压: {name!r}")
            target = (dest / name).resolve()
            if dest_resolved not in target.parents and target != dest_resolved:
                raise ValueError(f"压缩包路径越界，拒绝解压: {name!r}")
        zf.extractall(dest)


def resolve_input_unit(path: str | Path) -> InputUnit:
    """把用户给的路径解析成「一份报价」的输入单元。

    单文件 → 就一个成员；目录 / zip → 里面所有受支持的文件都是这份报价的参考文件。
    """
    p = Path(path)
    if not p.exists():
        raise ValueError(f"路径不存在: {p}")

    if p.is_file():
        if p.suffix.lower() == ".zip":
            tmp = Path(tempfile.mkdtemp(prefix="quote_unit_"))
            try:
                _safe_extract_zip(p, tmp)
            except Exception:
                shutil.rmtree(tmp, ignore_errors=True)
                raise
            skipped: list[str] = []
            members = _collect_files(tmp, skipped)
            if not members:
                shutil.rmtree(tmp, ignore_errors=True)
                raise ValueError(f"压缩包里没有可处理的文件（支持 {SUPPORTED_SUFFIXES}）: {p.name}")
            return InputUnit(path=p, kind="archive", members=members,
                             temp_dir=tmp, skipped=skipped)
        if p.suffix.lower() not in SUPPORTED_SUFFIXES:
            raise ValueError(
                f"不支持的文件格式: {p.suffix}（支持 {SUPPORTED_SUFFIXES}；"
                f"多个文件请放进文件夹或打成 .zip）")
        return InputUnit(path=p, kind="file", members=[p])

    # 目录
    skipped = []
    members = _collect_files(p, skipped)
    if not members:
        nested_zips = sorted(x.name for x in p.rglob("*.zip"))
        if nested_zips:
            raise ValueError(
                f"文件夹里只有压缩包，没有可直接读的文件: {nested_zips[:5]}"
                f"（{p}）—— 压缩包要逐个跑，或解压后把文件放进同一个文件夹")
        raise ValueError(f"文件夹里没有可处理的文件（支持 {SUPPORTED_SUFFIXES}）: {p}")
    # 子目录里只有一个文件时也别降级成「单文件」—— 用户既然给了文件夹，
    # 语义就是「这个单元里的东西都是参考文件」，产物仍按单元出（1 个 ERP JSON）。
    return InputUnit(path=p, kind="folder", members=members, skipped=skipped)


def find_unit_meta(unit: InputUnit) -> Optional[Path]:
    """找单元里的邮件副档（`*.meta.json`）。

    单文件：`<文件>.meta.json`（ingest_email 的老位置）。
    文件夹/压缩包：`<目录>/_email.meta.json` 或目录里任意 `*.meta.json`。
    拆出来的子单元：自己的目录 → 上一个父目录（一封邮件的多个文件夹附件共用一份副档）
    → 压缩包旁边的 `<zip>.meta.json`。
    """
    if unit.kind == "file":
        candidate = unit.path.with_name(unit.path.name + ".meta.json")
        return candidate if candidate.exists() else None

    search_dirs = [unit.path]
    if unit.is_batch_member:
        search_dirs.append(unit.path.parent)          # 只往上找一层：批次的根
        if unit.from_archive and unit.origin:
            search_dirs.append(unit.origin.parent)    # 压缩包所在的邮件投递目录
    for base in search_dirs:
        if not base.is_dir():
            continue
        for name in ("_email.meta.json", "meta.json"):
            candidate = base / name
            if candidate.exists():
                return candidate
    hits = sorted(unit.path.glob("*.meta.json"))
    if hits:
        return hits[0]
    if unit.from_archive and unit.origin:
        candidate = unit.origin.with_name(unit.origin.name + ".meta.json")
        if candidate.exists():
            return candidate
    return None


# ── 批次拆分：一个文件夹里装了好几份报价（1.0.38）──────────────────────────
def _root_files(root: Path, skipped: list[str]) -> list[Path]:
    """只看根目录下**直接**躺着的受支持文件（不递归）。"""
    out: list[Path] = []
    for item in sorted(root.iterdir()):
        if item.is_dir():
            continue
        if _is_bookkeeping(item.name):
            continue
        if _is_junk(item.name):
            skipped.append(item.name)
            continue
        if item.suffix.lower() not in SUPPORTED_SUFFIXES:
            skipped.append(item.name)
            continue
        out.append(item)
    return out


def _child_dirs_with_files(root: Path) -> list[tuple[Path, list[Path], list[str]]]:
    """根目录下（递归）含受支持文件的子目录 —— 每个这样的子目录 = 一份报价。"""
    out: list[tuple[Path, list[Path], list[str]]] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if child.name in JUNK_NAMES or _is_junk(child.name):
            continue
        skipped: list[str] = []
        files = _collect_files(child, skipped)   # 子目录内部继续合并，不再往下拆
        if files:
            out.append((child, files, skipped))
    return out


def resolve_input_units(path: str | Path) -> list[InputUnit]:
    """把用户给的路径解析成**一份或多份**报价。

    口径（1.0.38）：
      * 单文件 → 1 份；
      * 文件夹 / 压缩包**没有子目录** → 1 份（里面所有文件都是参考文件，合并）；
      * 文件夹 / 压缩包**有子目录** → 子目录各算一份，根下的散文件另算一份。
    """
    base = resolve_input_unit(path)
    if base.kind == "file":
        return [base]

    root = base.temp_dir if base.kind == "archive" else base.path
    assert root is not None          # 压缩包解析成功时一定有临时目录
    children = _child_dirs_with_files(root)
    if not children:
        return [base]                 # 没有子目录 —— 老口径，整包 = 一份报价

    units: list[InputUnit] = []
    for child, files, skipped in children:
        units.append(InputUnit(
            path=child, kind="folder", members=files, skipped=skipped,
            temp_dir=base.temp_dir, origin=base.path,
            role="subdir", from_archive=base.kind == "archive"))

    root_skipped: list[str] = []
    root_files = _root_files(root, root_skipped)
    if root_files:
        units.append(InputUnit(
            path=root, kind="folder", members=root_files, skipped=root_skipped,
            temp_dir=base.temp_dir, origin=base.path,
            role="root_files", from_archive=base.kind == "archive"))
    elif root_skipped:
        # 根下只有垃圾/不支持文件：挂到第一个单元的 skipped 上，别静默
        units[0].skipped.extend(f"(根目录) {s}" for s in root_skipped)

    if base.kind == "archive":
        units[0].archive_owner = True      # 压缩包本身只归档一次
    return units


def finalize_batch(units: list[InputUnit], logger=None,
                   remove_shared_meta: bool = True) -> Optional[str]:
    """同批处理完后的收尾：清掉共享邮件副档 + 删掉被搬空的父目录。

    返回一句给人看的说明（没什么可收尾就返回 None）。
    """
    if len(units) <= 1 or units[0].origin is None:
        return None
    origin = units[0].origin
    if origin.suffix.lower() == ".zip":
        return None                        # 压缩包由 archive_owner 归档，临时目录由 cleanup 删
    if not origin.exists():
        return None

    shared_meta = origin / "_email.meta.json"
    if remove_shared_meta and shared_meta.exists():
        shared_meta.unlink()               # 已经复制进各任务目录了
    leftovers = sorted(p.name for p in origin.iterdir())
    if not leftovers:
        origin.rmdir()
        return f"批次父目录已清空并删除: {origin}"
    return f"批次父目录仍有 {len(leftovers)} 项没被任何一份报价用上: {leftovers[:8]}"

