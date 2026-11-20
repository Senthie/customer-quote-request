#!/usr/bin/env python3
"""multimodal.py - 把「图片 / 扫描 PDF / 视频」变成抽取引擎能读的文本。

为什么要有这一层
----------------
客户询价除了 xlsx / pdf / docx / txt，还会发**图片**（规格表截图、封面照、刀线图）
和**视频**（翻样书给人看装订与工艺）。`main.py` 只吃文本类输入，所以媒体文件在
抽取前必须转成可引用的文本 —— 转出来的 `<名>.vlm.txt` 就是「客户原文」的替身：
  * 人能读、`cpms_content_audit --source` 能引用；
  * `extract_fields` 能按原文标签抽字段（所以转录要保留德文/英文原文标签）。

模型口径（2026-11-19 森西）：vLLM 上的 `Qwen3-VL-4B-Instruct`（见 assets/vlm_prompts.yaml）。
**它只做转录员**：只写看得见的字与事实，看不到写 null，绝不推断克重/装订/页数。
数字类拿不准就进 `unknowns`，由 CPMS 写「待确认」，不许凭印象落库。

三种输入
--------
    image  : jpg / png / webp / bmp / tif / gif / heic → EXIF 转正 + 长边压到 1600px
    pdf    : **只有扫描版**（无文字层）才转图；有文字层的 PDF 走原路，不烧算力
    video  : ffmpeg 场景切分 + 均匀采样 ≤12 帧 → 一次多图请求做时序理解

用法
----
    # 单份（调试用；会在同目录写 <名>.vlm.txt / .vlm.json）
    uv run ./scripts/multimodal.py --path "customer-files/pending/xxx.jpg"

    # 前置批处理（cron 用；scan_pending.sh 已挂）
    uv run ./scripts/multimodal.py --prepass --limit 10

    # 只看会做什么，不动文件
    uv run ./scripts/multimodal.py --prepass --dry-run

坑（实测）
----------
* 模型偶尔把 JSON 包在 ```json 里 → 解析前先剥栅栏，再退到「第一个 { 到最后一个 }」。
* `max_tokens` 给太小会截断 JSON（症状：`json_ok=false`），默认 1200。
* 图片长边 1600px 是性价比点：规格表小字仍可读，token 只有 4K 的 1/4。
* 视频抽帧在**没有 ffmpeg 的机器**上直接报错，不要静默跳过（否则看起来像「视频识别成功了」）。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
CUSTOMER_FILES = REPO_ROOT / "customer-files"
PENDING_DIR = CUSTOMER_FILES / "pending"
LOG_DIR = CUSTOMER_FILES / "logs"
CACHE_DIR = CUSTOMER_FILES / ".vlm_cache"
LEDGER = LOG_DIR / "vlm_ledger.jsonl"
CONFIG_PATH = REPO_ROOT / "assets" / "vlm_prompts.yaml"

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif", ".heic", ".heif"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".webm"}
MEDIA_SUFFIXES = IMAGE_SUFFIXES | VIDEO_SUFFIXES
SIDECAR_TXT = ".vlm.txt"
SIDECAR_JSON = ".vlm.json"
MAX_TOKENS = 1200


class MMError(RuntimeError):
    """多模态环节的可读错误（不吞异常，直接进汇报）。"""


# ── 配置 ──────────────────────────────────────────────────────────────
def load_config(path: Optional[Path] = None, with_endpoint: bool = True) -> dict[str, Any]:
    """提示词/参数来自仓库里的 vlm_prompts.yaml；**端点与模型名不入库**。

    解析顺序（见 scripts/local_config.py）：
      env `CQR_VLM_ENDPOINT` / `CQR_VLM_MODEL`
      > `assets/vlm_endpoint.local.yaml`（机器本地，.gitignore）
      > vlm_prompts.yaml 里的同名键（通常为空）

    公开仓库不能写内网 IP（2026-11-20 森西要求）。
    """
    import yaml
    p = Path(path) if path else CONFIG_PATH
    with open(p, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    if with_endpoint:
        try:
            from local_config import resolve
            cfg["endpoint"] = resolve("vlm_endpoint", "endpoint", "CQR_VLM_ENDPOINT",
                                      str(cfg.get("endpoint") or ""))
            cfg["model"] = resolve("vlm_endpoint", "model", "CQR_VLM_MODEL",
                                   str(cfg.get("model") or ""))
        except ImportError:  # 极端情况下（单独拷脚本）退回内置值
            pass
    return cfg


# ── 模型客户端 ────────────────────────────────────────────────────────
class VLMClient:
    def __init__(self, cfg: dict[str, Any]):
        self.endpoint = str(cfg.get("endpoint") or "")
        self.model = str(cfg.get("model") or "")
        self.timeout = int(cfg.get("timeout") or 90)
        self.retries = int(cfg.get("retries") or 2)
        self.system = str(cfg.get("system") or "").strip()
        self.max_tokens = int(cfg.get("max_tokens") or MAX_TOKENS)
        if not self.endpoint or not self.model:
            raise MMError(
                "没配 VLM 端点/模型名 —— 二选一："
                "① 写 assets/vlm_endpoint.local.yaml（照 .local.example.yaml 复制，机器本地不入库）；"
                "② 设环境变量 CQR_VLM_ENDPOINT / CQR_VLM_MODEL")

    def chat(self, image_paths: list[Path], user_prompt: str) -> dict[str, Any]:
        content: list[dict[str, Any]] = [{"type": "text", "text": user_prompt}]
        for p in image_paths:
            mime = "image/jpeg" if p.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
            data = base64.b64encode(p.read_bytes()).decode()
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{data}"}})
        body = {
            "model": self.model,
            "messages": ([{"role": "system", "content": self.system}] if self.system else [])
                        + [{"role": "user", "content": content}],
            "max_tokens": self.max_tokens,
            "temperature": 0,
        }
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            t0 = time.time()
            try:
                req = urllib.request.Request(
                    self.endpoint, data=json.dumps(body).encode(),
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = json.load(resp)
                text = payload["choices"][0]["message"]["content"]
                return {"text": text, "latency_s": round(time.time() - t0, 1),
                        "usage": payload.get("usage") or {}}
            except (urllib.error.URLError, TimeoutError, KeyError, ValueError, OSError) as exc:
                last = exc
                if attempt < self.retries:
                    time.sleep(1.5 * (attempt + 1))
        raise MMError(f"VLM 调用失败（{self.endpoint}）：{last}")


def parse_json_reply(text: str) -> Optional[dict[str, Any]]:
    """剥掉代码栅栏 → 取第一个 { 到最后一个 } → json.loads。失败返回 None。"""
    if not text:
        return None
    t = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", t, re.S)
    if fence:
        t = fence.group(1)
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        data = json.loads(t[i:j + 1])
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


# ── 预处理 ────────────────────────────────────────────────────────────
def pdf_needs_vision(path: Path, min_chars: int = 20) -> bool:
    """PDF 有没有文字层。没有 = 扫描件，需要走视觉。"""
    try:
        import fitz  # pymupdf
    except ImportError:  # pragma: no cover
        return False
    try:
        with fitz.open(path) as doc:
            chars = sum(len((page.get_text() or "").strip()) for page in doc)
        return chars < min_chars
    except Exception:
        return False


def _normalize_image(src: Path, out: Path, max_edge: int) -> Path:
    from PIL import Image, ImageOps
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        w, h = im.size
        scale = min(1.0, max_edge / max(w, h))
        if scale < 1.0:
            im = im.resize((int(w * scale), int(h * scale)))
        im.save(out, "JPEG", quality=85)
    return out


def _pdf_pages(src: Path, tmp: Path, max_pages: int) -> list[Path]:
    import fitz
    out: list[Path] = []
    with fitz.open(src) as doc:
        for idx in range(min(doc.page_count, max_pages)):
            page = doc[idx]
            pix = page.get_pixmap(dpi=200)
            p = tmp / f"page{idx + 1:02d}.jpg"
            pix.save(str(p))
            out.append(p)
    return out


def _video_frames(src: Path, tmp: Path, cfg: dict[str, Any]) -> list[Path]:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise MMError("ffmpeg / ffprobe 未安装 —— 视频识别跑不了（装法见 docs/multimodal-intake.md）")
    max_frames = int(cfg.get("max_video_frames") or 12)
    short = int(cfg.get("video_short_side") or 480)

    dur = 0.0
    try:
        probe = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                                "-of", "default=nw=1:nk=1", str(src)],
                               capture_output=True, text=True, timeout=60)
        dur = float((probe.stdout or "0").strip() or 0)
    except (subprocess.SubprocessError, ValueError):
        dur = 0.0
    if dur <= 0:
        raise MMError(f"读不出视频时长：{src.name}")

    # 均匀采样：一段 40s 的翻书视频，12 帧已能看出装订与工艺
    interval = max(dur / max_frames, 0.5)
    out: list[Path] = []
    for i in range(max_frames):
        t = min(dur - 0.1, i * interval)
        p = tmp / f"frame{i + 1:02d}.jpg"
        cmd = [ffmpeg, "-v", "error", "-ss", f"{t:.2f}", "-i", str(src), "-frames:v", "1",
               "-vf", f"scale='if(gt(iw,ih),{short * 16 // 9},-2)':'if(gt(iw,ih),-2,{short})'",
               "-y", str(p)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode == 0 and p.exists() and p.stat().st_size > 0:
            out.append(p)
    if not out:
        raise MMError(f"抽帧全失败：{src.name}")
    return out


def prepare_images(path: Path, cfg: dict[str, Any], tmp: Path) -> tuple[str, list[Path]]:
    """返回 (kind, 待送模型的图片列表)。kind: image | pdf_scan | video。"""
    suffix = path.suffix.lower()
    max_edge = int(cfg.get("max_edge_px") or 1600)
    if suffix in IMAGE_SUFFIXES:
        return "image", [_normalize_image(path, tmp / "img01.jpg", max_edge)]
    if suffix == ".pdf":
        return "pdf_scan", _pdf_pages(path, tmp, int(cfg.get("max_pdf_pages") or 6))
    if suffix in VIDEO_SUFFIXES:
        return "video", _video_frames(path, tmp, cfg)
    raise MMError(f"不支持的多模态格式：{path.suffix}")


# ── 转录文本 ──────────────────────────────────────────────────────────
def _quote(item: dict[str, Any]) -> str:
    q = str(item.get("quote") or "").strip()
    where = str(item.get("where") or "").strip()
    return f" ← 「{q}」" + (f"（{where}）" if where else "")


def build_transcript(payload: dict[str, Any], src: Path, kind: str,
                     model: str, latency: float) -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    head = (f"【{'视频抽帧识别' if kind == 'video' else '图片识别'}】来源：{src.name}｜"
            f"{model}｜{ts}｜耗时 {latency}s\n"
            f"⚠ 本段由视觉模型转录，只可作为「客户原文的替身」引用；"
            f"数字类以 unknowns 为准，未确定的一律写「待确认」。\n")

    if kind == "video":
        lines = ["──── 视频内容（时序）────"]
        if payload.get("summary"):
            lines.append(f"概述: {payload['summary']}")
        for step in payload.get("sequence") or []:
            if isinstance(step, dict):
                lines.append(f"帧{step.get('frame', '?')}: {step.get('what_changed', '')}")
        bind = payload.get("binding") or {}
        if isinstance(bind, dict) and bind.get("value"):
            lines.append(f"装订: {bind['value']}" + (f"（帧{bind['frame']}: {bind.get('why','')}）" if bind.get("frame") else ""))
        if payload.get("craft"):
            lines.append("工艺: " + "、".join(str(x) for x in payload["craft"]))
        pg = payload.get("page_guess") or {}
        if isinstance(pg, dict) and pg.get("value"):
            lines.append(f"页数线索: {pg['value']}" + (f"（依据: {pg.get('basis','')}）" if pg.get("basis") else ""))
        sz = payload.get("size_guess") or {}
        if isinstance(sz, dict) and sz.get("value"):
            lines.append(f"尺寸线索: {sz['value']}" + (f"（依据: {sz.get('basis','')}）" if sz.get("basis") else ""))
        if payload.get("texts"):
            lines += ["──── 画面文字（照抄）────"] + [f"  {t}" for t in payload["texts"]]
    else:
        lines = []
        specs = payload.get("specs") or {}
        ev = {str(e.get("field")): e for e in (payload.get("evidence") or []) if isinstance(e, dict)}
        if payload.get("titles"):
            lines.append("品名 / 系列: " + "、".join(str(t) for t in payload["titles"]))
        lines.append("──── 原文逐行（照抄，供抽取引擎读标签）────")
        raw = payload.get("raw_text") or []
        lines += [f"  {r}" for r in raw] if raw else ["  （模型未给出可读文字）"]
        lines.append("──── 结构化字段（均须有原文引文）────")
        labels = [("finished_size_mm", "成品尺寸"), ("extent_pp", "页数"),
                  ("paper_gsm", "用纸/克重"), ("print_colors", "印色"),
                  ("binding", "装订"), ("finish", "表面工艺"), ("special", "配件/特殊工艺")]
        for key, zh in labels:
            val = specs.get(key)
            if not val:
                continue
            shown = "、".join(str(v) for v in val) if isinstance(val, list) else str(val)
            lines.append(f"  {zh}: {shown}" + (_quote(ev[key]) if key in ev else "（无引文，需复核）"))
        for key, zh in (("quantities", "印数/档位"), ("languages", "语言")):
            val = payload.get(key)
            if val:
                shown = "、".join(str(v) for v in val) if isinstance(val, list) else str(val)
                lines.append(f"  {zh}: {shown}")

    for title, key in (("未确定（必须人工核）", "unknowns"), ("图中自相矛盾", "conflicts")):
        vals = payload.get(key) or []
        if vals:
            lines.append(f"──── {title} ────")
            lines += [f"  {v}" for v in vals]
    return head + "\n".join(lines) + "\n"


# ── 单份处理 ──────────────────────────────────────────────────────────
def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _append_ledger(entry: dict[str, Any]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    entry.setdefault("ts", datetime.now().isoformat(timespec="seconds"))
    with open(LEDGER, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def has_quote_info(payload: dict[str, Any]) -> bool:
    """这张图/这段视频里到底有没有值得进抽取流程的报价信息。

    实测（2026-11-19，真值库 3 张真实附件）：客户邮件里大量图片是**签名 logo / 活动横幅**
    （VEMAG 徽标、`Retail Innovation Days` banner）—— 模型照样能读字，但把它写进 `.vlm.txt`
    会让抽取引擎把 `vermag` 当成品名。所以这类图**只留 json、不产 txt**。
    """
    if str(payload.get("doc_type") or "") == "logo_banner":
        return False
    specs = payload.get("specs") or {}
    if any(v for v in specs.values() if v):
        return True
    if payload.get("quantities") or payload.get("languages") or payload.get("titles"):
        return True
    # 视频的形状不一样：没有 specs，判据是「有没有装订/页数/尺寸结论」
    if str(payload.get("_meta", {}).get("kind") or "") == "video":
        bind = payload.get("binding") or {}
        pg = payload.get("page_guess") or {}
        sz = payload.get("size_guess") or {}
        if (isinstance(bind, dict) and bind.get("value")) or payload.get("craft"):
            return True
        if (isinstance(pg, dict) and pg.get("value")) or (isinstance(sz, dict) and sz.get("value")):
            return True
        return len([t for t in (payload.get("texts") or []) if str(t).strip()]) >= 3
    raw = [str(r) for r in (payload.get("raw_text") or []) if str(r).strip()]
    return len(raw) >= 6 and any(ch.isdigit() for ch in "".join(raw))


def process_file(path: Path, cfg: dict[str, Any], client: Any,
                 force: bool = False, dry_run: bool = False) -> dict[str, Any]:
    """把一份媒体文件转成 `<名>.vlm.txt` + `.vlm.json`。返回结果摘要。"""
    txt_path = path.with_suffix(path.suffix + SIDECAR_TXT)
    json_path = path.with_suffix(path.suffix + SIDECAR_JSON)
    if json_path.exists() and not force:
        return {"file": path.name, "status": "skipped", "reason": "已有转录"}
    if dry_run:
        return {"file": path.name, "status": "dry-run",
                "reason": "扫描版 PDF" if path.suffix.lower() == ".pdf" else "媒体文件"}

    kind, images = "", []
    tmp = Path(tempfile.mkdtemp(prefix="vlm_"))
    try:
        kind, images = prepare_images(path, cfg, tmp)
        prompt = cfg.get("video_user") if kind == "video" else cfg.get("image_user")
        reply = client.chat(images, str(prompt))
        payload = parse_json_reply(reply["text"]) or {}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    payload.setdefault("_meta", {})
    payload["_meta"].update({"file": path.name, "kind": kind, "model": client.model,
                             "latency_s": reply["latency_s"], "frames": len(images),
                             "usage": reply.get("usage"), "raw_reply": reply["text"][:2000]})
    usable = has_quote_info(payload)
    transcript = build_transcript(payload, path, kind, client.model, reply["latency_s"]) if usable else ""

    if not dry_run:
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (CACHE_DIR / f"{_sha256(path)}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        if usable:
            txt_path.write_text(transcript, encoding="utf-8")
        else:
            txt_path.unlink(missing_ok=True)   # 旧版本留下的脏转录要清掉
    try:
        txt_rel = str(txt_path.relative_to(REPO_ROOT))
    except ValueError:            # 调试时跑在仓库外（/tmp 等）
        txt_rel = str(txt_path)
    result = {"file": path.name, "kind": kind,
              "status": "ok" if usable else "no_quote_info",
              "json_ok": bool(payload and payload != {"_meta": payload.get("_meta", {})}),
              "frames": len(images), "latency_s": reply["latency_s"],
              "unknowns": len(payload.get("unknowns") or []),
              "txt": txt_rel if usable else ""}
    _append_ledger(result)
    return result


# ── 前置批处理 ────────────────────────────────────────────────────────
def _iter_media(pending: Path, min_image_bytes: int = 0) -> list[Path]:
    """pending 根下的媒体 + 单元目录里嵌着的媒体（.eml 展开后的附件就在单元目录里）。

    `min_image_bytes`：太小的图片基本是邮件签名图标 / 分隔线（实测一封客户邮件带 30 个
    0–3 KB 的 inline png），送模型纯属浪费 —— 默认 12 KB 以下跳过。
    """
    out: list[Path] = []
    if not pending.exists():
        return out

    def _small(p: Path) -> bool:
        try:
            return p.stat().st_size < min_image_bytes
        except OSError:
            return False

    for item in sorted(pending.iterdir()):
        if item.name.startswith(".") or item.name == "_claimed":
            continue
        if item.is_file():
            if item.suffix.lower() in MEDIA_SUFFIXES:
                if not (item.suffix.lower() in IMAGE_SUFFIXES and _small(item)):
                    out.append(item)
            elif item.suffix.lower() == ".pdf" and pdf_needs_vision(item):
                out.append(item)
        elif item.is_dir():
            for sub in sorted(item.rglob("*")):
                if not sub.is_file() or sub.name.startswith("."):
                    continue
                if sub.suffix.lower() in MEDIA_SUFFIXES:
                    if not (sub.suffix.lower() in IMAGE_SUFFIXES and _small(sub)):
                        out.append(sub)
                elif sub.suffix.lower() == ".pdf" and pdf_needs_vision(sub):
                    out.append(sub)
    return out


def prepass(pending: Path, cfg: dict[str, Any], client: Any,
            limit: int = 0, force: bool = False, dry_run: bool = False,
            wrap: bool = True) -> list[dict[str, Any]]:
    """扫描 pending/，把媒体文件转成文本。

    pending 根下的媒体会先被包成「单元目录」（`pending/<名>/`），
    这样 `scan_pending` 会把它当一份报价处理，而不是永远躺在 pending 里。
    `wrap=False`：就地处理（给「邮件展开出来的单元目录」用 —— 那里再套子目录会被
    input_unit 当成多份报价拆开）。
    """
    # ⚠ 不能用 `cfg.get(...) or 12000`：显式写 0（关掉阈值）会被 `or` 吃掉变回 12000。
    _min = cfg.get("min_image_bytes")
    min_bytes = 12000 if _min is None else int(_min)
    targets = _iter_media(pending, min_image_bytes=min_bytes)
    if limit:
        targets = targets[:limit]
    results: list[dict[str, Any]] = []
    for path in targets:
        if wrap and path.parent == pending:   # 根下散文件 → 包成单元目录
            folder = pending / path.stem
            folder.mkdir(exist_ok=True)
            moved = folder / path.name
            if not dry_run and not moved.exists():
                shutil.move(str(path), str(moved))
            path = moved if moved.exists() else path
        try:
            results.append(process_file(path, cfg, client, force=force, dry_run=dry_run))
        except MMError as exc:
            entry = {"file": path.name, "status": "failed", "reason": str(exc)}
            results.append(entry)
            _append_ledger(entry)
    return results


def summarize(results: list[dict[str, Any]]) -> str:
    if not results:
        return ""
    ok = [r for r in results if r.get("status") == "ok"]
    noinfo = [r for r in results if r.get("status") == "no_quote_info"]
    failed = [r for r in results if r.get("status") == "failed"]
    skipped = [r for r in results if r.get("status") == "skipped"]
    parts = []
    if ok:
        parts.append(f"🖼 多模态识别 {len(ok)} 份 → .vlm.txt")
    if noinfo:
        parts.append(f"无报价信息 {len(noinfo)} 份（签名 logo/横幅，已跳过）")
    if failed:
        parts.append(f"❌ 失败 {len(failed)} 份")
    if skipped:
        parts.append(f"跳过 {len(skipped)} 份（已有转录）")
    lines = ["｜".join(parts)]
    for r in ok:
        lines.append(f"  ✓ {r['file']}（{r.get('kind')}，{r.get('frames')} 帧图，"
                     f"{r.get('latency_s')}s，未确定 {r.get('unknowns')} 项）")
    for r in failed:
        lines.append(f"  ✗ {r['file']}：{r.get('reason')}")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="图片 / 扫描 PDF / 视频 → 可引用的转录文本")
    ap.add_argument("--path", default="", help="单个媒体文件（调试）")
    ap.add_argument("--prepass", action="store_true", help="批处理 pending/（cron 用）")
    ap.add_argument("--pending", default="", help="覆盖 pending 目录")
    ap.add_argument("--limit", type=int, default=0, help="本轮最多处理几份")
    ap.add_argument("--force", action="store_true", help="已有 .vlm.txt 也重做")
    ap.add_argument("--no-wrap", action="store_true",
                    help="就地处理，不把根下媒体包成单元目录（单元目录内部用）")
    ap.add_argument("--dry-run", action="store_true", help="只看会处理什么")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--config", default="", help="覆盖 vlm_prompts.yaml")
    args = ap.parse_args(argv)

    try:
        cfg = load_config(Path(args.config) if args.config else None)
        client = VLMClient(cfg)
    except (MMError, FileNotFoundError) as exc:
        print(f"!! 多模态配置错误：{exc}", file=sys.stderr)
        return 2

    if args.path:
        results = [process_file(Path(args.path), cfg, client,
                                force=args.force, dry_run=args.dry_run)]
    elif args.prepass:
        results = prepass(Path(args.pending) if args.pending else PENDING_DIR, cfg, client,
                          limit=args.limit, force=args.force, dry_run=args.dry_run,
                          wrap=not args.no_wrap)
    else:
        ap.print_help()
        return 1

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=1))
    else:
        out = summarize(results)
        if out:
            print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
