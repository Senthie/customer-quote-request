"""上传落盘 —— **纯逻辑层（不 import Flask）**，与 data.py 同级。

业务（2026-10-20 森西）：「只上传文件，然后 Hermes 每 5 分钟扫一次 pending，
有任务就执行」。所以这一层只做一件事：把浏览器给的字节安全地落到
`customer-files/pending/`，**不抽取、不起进程、不记账单状态**。

落盘口径直接复用 `main.py` 的输入单元语义（`scripts/input_unit.py`），不发明新规则：

| 上传动作 | 落盘位置 | main.py 的理解 |
|---|---|---|
| 1 个文件（含 .zip） | `pending/<文件名>` | 一份报价 |
| N 个文件 + group=one | `pending/<批次号>/` | **一份**报价的 N 份参考文件 |
| N 个文件 + group=each | `pending/<文件名>` × N | N 份报价，各算一份 |

⚠️ 单个 .zip **必须平铺**到 `pending/` 根下。放进目录里会被 input_unit 判成
「文件夹里只有压缩包，没有可直接读的文件」而拒掉（踩过一次）。

⚠️ 文件名不能以 `.` 开头：input_unit 的 JUNK_PREFIXES 含 `"."`，点开头会被当垃圾
静默跳过 —— 用户会以为「传上去了怎么没反应」。
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PENDING_DIR = REPO_ROOT / "customer-files" / "pending"

# 扫描器认领中的目录（要跟 scripts/scan_pending.py 保持一致）
CLAIM_DIR_NAME = "_claimed"

# main.py 能直接吃的后缀（`input_unit.SUPPORTED_SUFFIXES` + 压缩包）。
# **不含 .eml**：`.eml` 得先过 ingest_email.py 才会变成 pending 里的附件，
# 直接喂给 main.py 会报「不支持的文件格式」——所以上传口先拦下来，给明确指引。
ALLOWED_SUFFIXES = (".xlsx", ".xls", ".csv", ".pdf", ".docx", ".txt", ".zip")

# 单个上传请求的字节上限（Flask MAX_CONTENT_LENGTH 用同一个数）
MAX_UPLOAD_BYTES = 64 * 1024 * 1024

# 文件名里不许出现的字符（Windows 更严，按最严的来）+ 控制字符
_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
_STEM_MAX = 80          # 主名最长（不含扩展名）；太长的中文文件名会让路径超限


class UploadError(ValueError):
    """上传被拒（消息直接给用户看）。"""


def safe_filename(raw: str) -> str:
    """浏览器给的文件名 → 可落盘的安全文件名（保留中文，去掉路径与危险字符）。

    做的事，按顺序：
      1. 统一分隔符后只取 basename —— 挡掉 `../../etc/passwd` 这类路径穿越
      2. 危险字符（`\\ / : * ? " < > |`）与控制字符 → `_`
      3. 去掉首尾空白与点（点开头会被 input_unit 当隐藏文件跳过）
      4. 主名截到 80 字符，扩展名保留
    """
    name = str(raw or "")
    name = name.replace("\\", "/").split("/")[-1]          # 1
    name = _BAD_CHARS.sub("_", name)                        # 2
    name = name.strip().strip(".")                          # 3
    if not name:
        raise UploadError("文件名是空的（或全是点/空白）")

    path = Path(name)
    suffix = path.suffix
    stem = path.name[: -len(suffix)] if suffix else path.name
    stem = stem.strip().strip(".")
    if not stem:
        raise UploadError(f"文件名没有主名: {raw!r}")
    return f"{stem[:_STEM_MAX]}{suffix}"


def check_suffix(filename: str) -> None:
    """后缀白名单。不合格抛 UploadError（消息里给出下一步怎么办）。"""
    suffix = Path(filename).suffix.lower()
    if suffix in ALLOWED_SUFFIXES:
        return
    if suffix == ".eml":
        raise UploadError(
            "邮件（.eml）请走邮件入口：先把附件存成文件再上传，"
            "或用 `uv run ./scripts/ingest_email.py --eml <文件>` 投递"
            "（.eml 直接喂 main.py 会报不支持）")
    raise UploadError(
        f"不支持的文件类型 {suffix or '（无扩展名）'}；"
        f"只收 {' / '.join(ALLOWED_SUFFIXES)}")


def _unique_path(target: Path) -> Path:
    """同名不覆盖：`a.xlsx` 已存在 → `a (2).xlsx` → `a (3).xlsx` …"""
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    for i in range(2, 1000):
        candidate = target.with_name(f"{stem} ({i}){suffix}")
        if not candidate.exists():
            return candidate
    raise UploadError(f"同名文件太多了，换个名字: {target.name}")


def new_batch_name() -> str:
    """批次号（时间戳 + 随机尾巴）。**不以 . 或 _ 开头**，避免被当隐藏/记账文件。"""
    from uuid import uuid4

    return f"{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:6]}"


def save_uploads(items: Iterable[tuple[str, bytes]], group: str = "one",
                 pending_dir: Optional[Path] = None) -> dict[str, Any]:
    """把上传的文件落到 pending/。

    参数
    ----
    items       : [(原始文件名, 内容字节)] —— 由路由从 `request.files` 抽出来，
                  纯逻辑层不认 FileStorage（那玩意绑着 Flask）
    group       : "one" = 这一批是同一份报价的参考文件
                  "each" = 每个文件各算一份报价
    pending_dir : 默认 customer-files/pending（测试传临时目录）

    返回
    ----
    {"batch": 批次号|None, "saved": [{name, relpath, bytes}], "rejected": [{name, reason}],
     "group": group}

    一个都不成功 → 抛 UploadError；部分成功 → rejected 里说明原因。
    """
    if group not in ("one", "each"):
        raise UploadError(f"group 只能是 one / each，收到 {group!r}")

    pending = Path(pending_dir) if pending_dir is not None else DEFAULT_PENDING_DIR
    pending.mkdir(parents=True, exist_ok=True)

    checked: list[tuple[str, bytes]] = []
    rejected: list[dict[str, str]] = []
    for raw_name, payload in items:
        try:
            name = safe_filename(raw_name)
            check_suffix(name)
        except UploadError as exc:
            rejected.append({"name": str(raw_name), "reason": str(exc)})
            continue
        if not payload:
            rejected.append({"name": name, "reason": "文件是空的（0 字节）"})
            continue
        checked.append((name, payload))

    if not checked:
        reasons = "；".join(f"{r['name']}: {r['reason']}" for r in rejected[:3])
        raise UploadError(f"没有可上传的文件（{reasons or '一个文件都没收到'}）")

    # 单文件（含单 .zip）一律平铺：main.py 对「文件夹里只有 zip」会拒
    if len(checked) == 1 or group == "each":
        batch = None
        target_dir = pending
    else:
        batch = new_batch_name()
        target_dir = pending / batch
    target_dir.mkdir(parents=True, exist_ok=True)

    saved: list[dict[str, Any]] = []
    for name, payload in checked:
        dest = _unique_path(target_dir / name)
        dest.write_bytes(payload)
        saved.append({
            "name": dest.name,
            "relpath": dest.relative_to(pending).as_posix(),
            "bytes": len(payload),
        })

    return {"batch": batch, "group": group, "saved": saved, "rejected": rejected}


def scan_pending(pending_dir: Optional[Path] = None,
                 now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """列出「还在等抽取」的东西（页面顶部的排队条用它）。

    条目 = pending/ 根下的单个文件，或一个目录（= 一份报价的参考文件）。
    `_claimed/`（扫描器认领中）、点开头（隐藏/垃圾）一律不算。
    """
    pending = Path(pending_dir) if pending_dir is not None else DEFAULT_PENDING_DIR
    if not pending.exists():
        return []

    now = now or datetime.now()
    out: list[dict[str, Any]] = []
    for item in sorted(pending.iterdir()):
        name = item.name
        if name.startswith(".") or name == CLAIM_DIR_NAME:
            continue
        if item.is_dir():
            files = [p for p in item.rglob("*") if p.is_file() and not p.name.startswith(".")]
            if not files:
                continue                      # 空目录（可能是刚搬空的残留）
            kind = "folder"
            n_files = len(files)
            size = sum(p.stat().st_size for p in files)
        elif item.is_file():
            if item.suffix.lower() not in ALLOWED_SUFFIXES:
                continue                      # 例如 *.meta.json 副档
            kind = "file"
            n_files = 1
            size = item.stat().st_size
        else:
            continue

        mtime = datetime.fromtimestamp(item.stat().st_mtime)
        out.append({
            "name": name,
            "kind": kind,
            "n_files": n_files,
            "bytes": size,
            "mtime_iso": mtime.strftime("%Y-%m-%d %H:%M:%S"),
            "age_seconds": max(0, int((now - mtime).total_seconds())),
        })
    out.sort(key=lambda it: it["age_seconds"])     # 最新的在前
    return out
