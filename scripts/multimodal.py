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
import difflib
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
MAX_TOKENS = 3200


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


def _repair_truncated_json(chunk: str) -> Optional[dict[str, Any]]:
    """把被 max_tokens 截断的 JSON 尽量救回来。

    2026-11-23 实测：components 加进来后，单张封面就能把 1200 tokens 用尽，
    回复停在 `"count": nul`。策略是从后往前砍掉不完整的部分，同时闭合括号：
    先按逗号切、再按行切，每砍一刀就补齐 ```
    ``` 试着 json.loads，成功即返回。
    """
    if not chunk.strip():
        return None
    # 去掉最后一个不完整的 token（如 `nul`、`"abc`）
    attempts = [chunk]
    for cut in range(len(chunk), 0, -1):
        if chunk[cut - 1] in ",{[":
            attempts.append(chunk[:cut])
            break
    # 从末尾逐步退，找到最后一个能闭合的位置
    for base in list(attempts):
        t = base.rstrip().rstrip(",")
        # 统计还差几个括号
        stack: list[str] = []
        in_str = esc = False
        for ch in t:
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch in "{[":
                stack.append("}" if ch == "{" else "]")
            elif ch in "}]":
                if stack:
                    stack.pop()
        if in_str:
            t += '"'
        fixed = t + "".join(reversed(stack))
        try:
            data = json.loads(fixed)
            if isinstance(data, dict):
                data.setdefault("_meta", {})
                data["_meta"]["repaired"] = True
                return data
        except json.JSONDecodeError:
            continue
    # 最后一招：按逗号边界逐段回退
    t = chunk
    for _ in range(60):
        t = t[: t.rfind(",")] if "," in t else t
        cand = t.rstrip().rstrip(",")
        stack2: list[str] = []
        in_str = esc = False
        for ch in cand:
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch in "{[":
                stack2.append("}" if ch == "{" else "]")
            elif ch in "}]":
                if stack2:
                    stack2.pop()
        if in_str:
            cand += '"'
        try:
            data = json.loads(cand + "".join(reversed(stack2)))
            if isinstance(data, dict):
                data.setdefault("_meta", {})
                data["_meta"]["repaired"] = True
                return data
        except json.JSONDecodeError:
            if not cand:
                break
    return None


def parse_json_reply(text: str) -> Optional[dict[str, Any]]:
    """剥掉代码栅栏 → 取第一个 { 到最后一个 } → json.loads。

    解析失败时先试截断修复（模型常被 max_tokens 切断），救不回来才返回 None。
    """
    if not text:
        return None
    t = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", t, re.S)
    if fence:
        t = fence.group(1)
    i = t.find("{")
    if i < 0:
        return None
    j = t.rfind("}")
    if j > i:
        try:
            data = json.loads(t[i:j + 1])
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    # 走到这里 = 没有闭合的 } 或整体不合法 → 截断修复
    return _repair_truncated_json(t[i:])


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


def _dhash(path: Path, hash_size: int = 8) -> int:
    """64 位差分感知哈希（dHash）。同一页的两帧哈希距离很小，换页会变大。"""
    from PIL import Image       # 延迟导入：只有视频路径用得到
    with Image.open(path) as im:
        img = im.convert("L").resize((hash_size + 1, hash_size), Image.LANCZOS)
    px = list(img.tobytes())   # 'L' 模式：每像素 1 字节
    bits = 0
    width = hash_size + 1
    for row in range(hash_size):
        for col in range(hash_size):
            left = px[row * width + col]
            right = px[row * width + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return bits


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _group_by_page(items: list[tuple[float, int]], threshold: int) -> list[list[int]]:
    """把 (时刻, 哈希) 序列按画面相似度分组（只用于**去掉重复帧**，不用于切页）。

    ⚠ 实测结论（2026-11-24，`20220317_EMCPU.mp4`）：**别指望自动切页**。
      · ffmpeg `select='gt(scene,0.10)'` → 84.5s 翻书视频 0 个切点（书本/桌面/灯光不变，
        只有中间一块纸翻过去，全局 scene 分数上不去）；阈值降到 0.06 也只有 1 个。
      · dHash 相邻帧汉明距离平均 21/64，60 帧里 57 帧互不相同 —— 手持拍摄整帧一直在动，
        同一页的两帧也不像。
      所以页级覆盖**只能靠采样密度**（见 `video_frame_interval`），哈希只用来丢「视频卡住/
      重复帧」这种真·重复。要自动切页得做局部特征匹配，代价远超收益。
    """
    groups: list[list[int]] = []
    for i, (_t, h) in enumerate(items):
        if groups:
            rep = items[groups[-1][0]][1]
            if _hamming(h, rep) <= threshold:
                groups[-1].append(i)
                continue
        groups.append([i])
    return groups


def _sample_times(dur: float, cfg: dict[str, Any]) -> list[float]:
    """均匀采样时刻：间隔至少 `video_frame_interval` 秒，总帧数不超过 `max_video_frames`。

    84.5s 的视频 → 3.0s 一帧 = 28 帧；一页（约 8s）能落到 2-3 帧，模型自己归页。
    视频越长间隔越大，此时在 unknowns 里提示可能漏页（见 process_file）。
    """
    max_frames = max(2, int(cfg.get("max_video_frames") or 28))
    min_interval = float(cfg.get("video_frame_interval") or 3.0)
    step = max(min_interval, dur / max_frames)
    times = []
    t = 0.0
    while t < dur - 0.05 and len(times) < max_frames:
        times.append(round(t, 2))
        t += step
    last = round(dur - 0.6, 2)
    if last > 0 and (not times or last - times[-1] > 0.5):
        if len(times) >= max_frames:
            times[-1] = last        # 帧数满了也要保住片尾（封底/版权页）
        else:
            times.append(last)
    return times[:max_frames]


def _grab(src: Path, ffmpeg: str, t: float, path: Path, short: int) -> bool:
    cmd = [ffmpeg, "-v", "error", "-ss", f"{t:.2f}", "-i", str(src), "-frames:v", "1",
           "-vf", f"scale='if(gt(iw,ih),{short * 16 // 9},-2)':'if(gt(iw,ih),-2,{short})'",
           "-y", str(path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return proc.returncode == 0 and path.exists() and path.stat().st_size > 0


def _probe_duration(src: Path) -> float:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return 0.0
    try:
        proc = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                               "-of", "default=nw=1:nk=1", str(src)],
                              capture_output=True, text=True, timeout=60)
        return float((proc.stdout or "0").strip() or 0)
    except (subprocess.SubprocessError, ValueError):
        return 0.0


def _video_frames(src: Path, tmp: Path, cfg: dict[str, Any]) -> list[Path]:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise MMError("ffmpeg / ffprobe 未安装 —— 视频识别跑不了（装法见 docs/multimodal-intake.md）")
    short = int(cfg.get("video_short_side") or 480)
    dup_th = int(cfg.get("video_hash_threshold") or 2)

    dur = _probe_duration(src)
    if dur <= 0:
        raise MMError(f"读不出视频时长：{src.name}")

    kept: list[tuple[float, Path, int]] = []
    for i, t in enumerate(_sample_times(dur, cfg)):
        p = tmp / f"frame{i + 1:02d}.jpg"
        if not _grab(src, ffmpeg, t, p, short):
            continue
        h = _dhash(p)
        if kept and _hamming(h, kept[-1][2]) <= dup_th:   # 视频卡住/重复帧
            continue
        kept.append((t, p, h))
    if not kept:
        raise MMError(f"抽帧全失败：{src.name}")
    return [p for _t, p, _h in kept]


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


def _craft_rows(craft: Any) -> list[str]:
    """把 craft（五类互动工艺）渲染成可抄进 CPMS 的行。"""
    if not isinstance(craft, dict):
        return []
    rows = []
    for key, zh in _CRAFT_ZH.items():
        item = craft.get(key)
        if not isinstance(item, dict):
            continue
        state = str(item.get("state") or "").strip()
        bits = [f"{_DETAIL_ZH.get(f, f)}={item.get(f)}"
                for f in _CRAFT_DETAIL.get(key, ()) if item.get(f)]
        ev = str(item.get("evidence") or "").strip()
        if not state and not bits:      # 既没判断也没细节 → 本图无此工艺，别刷行
            continue
        line = f"  {zh}: state={state or 'null'}"
        if bits:
            line += "｜" + "、".join(bits)
        if ev:
            line += f" ← 「{ev}」"
        rows.append(line)
    return rows


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
        # ── 逐页清单（2026-11-24）：客户只发视频时，报价靠这段逐页记录；
        #    「各种图案」这种概括等于没看，所以页级内容单独成型、按出现顺序排。
        pages = payload.get("pages")
        if isinstance(pages, list) and pages:
            lines.append("──── 逐页清单（视频实际翻到的页，按出现顺序；左右页分开记）────")
            for pg in pages:
                if not isinstance(pg, dict):
                    continue
                kind_zh = {"cover": "封面", "back_cover": "封底", "spread": "跨页",
                           "page": "单页", "detail": "特写", "unknown": "看不清"}.get(
                    str(pg.get("kind") or "").strip(), str(pg.get("kind") or "").strip())
                fr = pg.get("frames")
                fr_s = ("帧" + "/".join(str(x) for x in fr)) if isinstance(fr, list) and fr else ""
                row = f"  P{pg.get('no', '?')}"
                if kind_zh:
                    row += f" [{kind_zh}]"
                if fr_s:
                    row += f" {fr_s}"
                lines.append(row)
                left = str(pg.get("left") or "").strip()
                right = str(pg.get("right") or "").strip()
                if left or right:
                    lines.append(f"    左页: {left or '—'}｜右页: {right or '—'}")
                pop = pg.get("popup")
                if isinstance(pop, dict):
                    bits = [f"{_PG_POPUP_ZH[k]}={pop.get(k)}"
                            for k in _PG_POPUP_ZH if pop.get(k) and str(pop.get(k)).strip().lower() not in ("null", "none")]
                    if bits:
                        lines.append("    立体件: " + "｜".join(bits))
                ptext = pg.get("text")
                if isinstance(ptext, list):
                    for t in ptext:
                        if str(t).strip():
                            lines.append(f"    文字: {str(t).strip()}")
                note = str(pg.get("note") or "").strip()
                if note:
                    lines.append(f"    ⚠ {note}")
            single = payload.get("pages_single_run")
            if isinstance(single, list) and single:
                lines.append("  ── 只出现一遍（未采信，需人工对照视频）──")
                for pg in single:
                    if not isinstance(pg, dict):
                        continue
                    desc = "｜".join(x for x in (str(pg.get("left") or "").strip(),
                                                 str(pg.get("right") or "").strip()) if x)
                    fr = pg.get("frames")
                    fr_s = ("（帧" + "/".join(str(x) for x in fr) + "）") if isinstance(fr, list) and fr else ""
                    lines.append(f"    P? {fr_s}{desc or '（描述为空）'}")
        for step in payload.get("sequence") or []:
            if isinstance(step, dict):
                lines.append(f"帧{step.get('frame', '?')}: {step.get('what_changed', '')}")
        bind = payload.get("binding") or {}
        if isinstance(bind, dict) and bind.get("value"):
            lines.append(f"装订: {bind['value']}" + (f"（帧{bind['frame']}: {bind.get('why','')}）" if bind.get("frame") else ""))
        craft_payload = payload.get("craft")
        if isinstance(craft_payload, list) and any(str(x).strip() for x in craft_payload):
            lines.append("工艺: " + "、".join(str(x) for x in craft_payload))
        elif isinstance(craft_payload, dict):
            rows = _craft_rows(craft_payload)
            if rows:
                lines.append("──── 互动工艺（可见；术语按本厂口径）────")
                lines.append("  state: real=看到动作发生 / printed=只是印刷图案 / unclear=分不清 / null=本段无信息")
                lines += rows
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
        # ── 结构部件（2026-11-23）：玩具/发声器/灯光/电池，抽取引擎靠这段填「配件描述」
        comp = payload.get("components") or {}
        if isinstance(comp, dict) and any(comp.values()):
            rows = []
            for key, zh, extra in (
                ("sound_module", "发声模块", (("keys", "键数"), ("key_shape", "按键形状"),
                                              ("speaker_holes", "出音孔"))),
                ("lights", "灯光", (("type", "类型"), ("count", "数量"), ("trigger", "触发"))),
                ("battery", "电池", (("cell_type", "型号"), ("cell_count", "节数"),
                                     ("compartment", "仓位"))),
            ):
                item = comp.get(key)
                if not isinstance(item, dict) or not any(item.values()):
                    continue
                state = str(item.get("state") or "").strip()
                bits = [f"{label}={item.get(k)}" for k, label in extra if item.get(k)]
                ev = str(item.get("evidence") or "").strip()
                if not state and not bits:      # 既没判断也没细节 → 这张图就是没信息，别刷行
                    continue
                line = f"  {zh}: state={state or 'null'}"
                if bits:
                    line += "｜" + "、".join(bits)
                if ev:
                    line += f" ← 「{ev}」"
                rows.append(line)
            for key, zh in (("interactive", "互动结构"), ("toys_included", "随书玩具"),
                            ("box_contents", "盒内物")):
                val = comp.get(key)
                if isinstance(val, list) and any(str(x).strip() for x in val):
                    rows.append(f"  {zh}: " + "、".join(str(x) for x in val))
            if rows:
                lines.append("──── 结构部件（可见）────")
                lines.append("  state: real=看得见实物结构 / printed=只是印刷图案 / unclear=分不清 / null=本图无信息")
                lines += rows
        # ── 互动工艺（2026-11-24）：客户询价文字里通常不写，报价成本却全靠它，
        #    所以单独成段，术语按本厂口径（揭页/拉卡/机关/毛毡布/冲形状），可直接抄进 CPMS。
        craft = payload.get("craft") or {}
        rows = _craft_rows(craft)
        if rows:
            lines.append("──── 互动工艺（可见；术语按本厂口径）────")
            lines.append("  state: real=实物结构 / printed=只是印刷图案 / unclear=分不清 / null=本图无信息")
            lines += rows
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
    # 部件扫查（2026-11-23）：只看得到耳朵/电池仓/按键背面的特写图，也是有效询价信息
    comp = payload.get("components") or {}
    if isinstance(comp, dict):
        for v in comp.values():
            if isinstance(v, dict):
                if v.get("state") or v.get("keys") or v.get("type") or v.get("cell_type"):
                    return True
            elif isinstance(v, list) and any(str(x).strip() for x in v):
                return True
    if payload.get("quantities") or payload.get("languages") or payload.get("titles"):
        return True
    # 互动工艺（2026-11-24）：只拍出「有揭页/拉卡/机关/毛毡/冲形」的样书内页，
    # 客户正文一个字不提也是有效询价信息 —— 报价成本全在这些工艺上
    craft = payload.get("craft") or {}
    if isinstance(craft, dict):
        for key, item in craft.items():
            if not isinstance(item, dict):
                continue
            if item.get("state") or any(item.get(f) for f in _CRAFT_DETAIL.get(key, ())):
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
        # 逐页清单（2026-11-24）：客户只发视频时，页级内容就是唯一可引用的原文，
        # 有页就是有效信息 —— 不能再按「texts ≥3 行」卡掉。
        pages = payload.get("pages")
        if isinstance(pages, list) and any(
                isinstance(p, dict) and (p.get("text") or p.get("left") or p.get("right"))
                for p in pages):
            return True
        return len([t for t in (payload.get("texts") or []) if str(t).strip()]) >= 3
    raw = [str(r) for r in (payload.get("raw_text") or []) if str(r).strip()]
    # doc_type 已经排除了 logo_banner；其余类型只要抄到了字就值得进转录。
    # 2026-11-24 实测：封面照只有「LLenas / Un libro pop-up」两行，按旧规则（≥6 行含数字）
    # 被判无价值丢掉 —— 而"pop-up"正是判断产品形态的唯一证据。
    if raw and str(payload.get("doc_type") or "") in (
            "spec_table", "cover_art", "interior_spread", "die_line",
            "packing_photo", "component_closeup"):
        return True
    return len(raw) >= 6 and any(ch.isdigit() for ch in "".join(raw))



# ── 部件字段的确定性护栏（2026-11-23）────────────────────────────────
_COMP_ZH = {"sound_module": "发声模块", "lights": "灯光", "battery": "电池"}
_COMP_DETAIL = ("keys", "key_shape", "speaker_holes", "type", "count",
                "trigger", "cell_type", "cell_count", "compartment")
# 只有 evidence 里出现这些词，才承认「印刷图案」这个判断
_PRINT_WORDS = ("印刷", "图案", "插画", "插图", "印着", "写着", "字样", "装饰",
                "printed", "illustration", "logo")


def normalize_components(payload: dict[str, Any]) -> dict[str, Any]:
    """把模型给的 components 收敛成可用的三态，并保证审计留痕。

    4B 模型在细粒度上不稳（实测把 "Made in China" 当电池证据、把出音孔说成印刷），
    所以这里做确定性兜底：
      1. state 只留 real / printed / unclear / null，true→real、false→null；
      2. 说 "printed" 但 evidence 里没有任何「印刷类」字样 → 降级 unclear；
      3. 非 real 的，细节字段（键数/型号/数量…）一律清空 —— 图案上的数字不是真部件数字；
      4. 非 real 的，自动往 unknowns 补一条「需客户确认」（模型常忘写）。
    """
    comp = payload.get("components")
    if not isinstance(comp, dict):
        return payload
    unk = payload.get("unknowns")
    if not isinstance(unk, list):
        unk = []
    for key, zh in _COMP_ZH.items():
        item = comp.get(key)
        if not isinstance(item, dict):
            continue
        st = item.get("state")
        if isinstance(st, bool):
            st = "real" if st else None
        st = str(st).strip().lower() if st else None
        if st in ("true", "yes"):
            st = "real"
        elif st in ("false", "no", ""):
            st = None
        if st not in ("real", "printed", "unclear"):
            st = None
        ev = str(item.get("evidence") or "").strip()
        if not ev and not any(item.get(f) for f in _COMP_DETAIL):
            st = None            # 既没证据也没细节 = 这张图就是没信息，别刷 noise 行
        if st == "printed" and not any(w in ev.lower() for w in _PRINT_WORDS):
            st = "unclear"       # 说不出是印刷还是实物 → 不敢断言
        if st == "unclear" and not ev:
            st = None
        if st in ("printed", "unclear"):
            for f in _COMP_DETAIL:
                if item.get(f):
                    item[f] = None
            if not any(zh in str(u) and "客户确认" in str(u) for u in unk):
                unk.append(f"{zh} state={st}，真实部件需客户确认")
        item["state"] = st
    payload["unknowns"] = unk
    return payload


# ── 互动工艺字段的确定性护栏（2026-11-24）──────────────────────────
# 客户询价文字里通常不写这些工艺，只能从样书照片/刀线图看出来；
# 但它们是报价成本的大头（模切次数、手工装配工时），所以单独立段并要求结构化工序要素。
_CRAFT_ZH = {
    "flap": "揭页",
    "pull_tab": "拉卡",
    "mechanism": "机关",
    "felt": "毛毡布",
    "die_cut_shape": "冲形状",
}
# 逐页清单（pages[].popup）的中文标签
_PG_POPUP_ZH = {
    "state": "state",
    "subject": "主体",
    "type": "类型",
    "spans": "跨页",
    "motion": "动作",
}
_CRAFT_DETAIL = {
    "flap": ("where", "per_page", "total", "origin", "finger_notch",
             "waste_removal", "staple_allowance", "size_mm"),
    "pull_tab": ("where", "total", "layers", "size_mm", "print_face", "print_back", "track"),
    "mechanism": ("kind", "where", "sets", "parts_per_set", "size_mm", "motion"),
    "felt": ("count", "source", "thickness_mm", "size_mm", "color_softness", "mount"),
    "die_cut_shape": ("where", "shape", "size_mm", "purpose", "waste_removal"),
}
_DETAIL_ZH = {
    "where": "在哪", "per_page": "每页", "total": "共", "origin": "来源",
    "finger_notch": "手指位", "waste_removal": "除废", "staple_allowance": "钉位",
    "size_mm": "尺寸", "layers": "用纸", "print_face": "正面印色",
    "print_back": "背面印色", "track": "轨道槽", "kind": "类型", "sets": "套数",
    "parts_per_set": "部件数", "motion": "动作", "count": "张数", "source": "来源",
    "thickness_mm": "厚度", "color_softness": "颜色硬度", "mount": "固定方式",
    "shape": "形状", "purpose": "用途",
}


def normalize_crafts(payload: dict[str, Any]) -> dict[str, Any]:
    """把模型给的 craft（互动工艺五类）收敛成可用的三态，并保证审计留痕。

    与 normalize_components 同一套护栏，因为 4B 在工艺上更爱把「插画里画出来的门」
    当成揭页、把「印上去的孔」当成冲孔：
      1. state 只留 real / printed / unclear / null，true→real、false→null；
      2. 说 printed 但 evidence 里没有印刷类字样 → 降级 unclear；
      3. 非 real 的，工序要素（几个/在哪/尺寸…）一律清空 —— 图案上的东西算不了成本；
      4. 非 real 的，自动往 unknowns 补一条「需客户确认」。
    """
    craft = payload.get("craft")
    if not isinstance(craft, dict):
        return payload
    unk = payload.get("unknowns")
    if not isinstance(unk, list):
        unk = []
    for key, zh in _CRAFT_ZH.items():
        item = craft.get(key)
        if not isinstance(item, dict):
            continue
        details = _CRAFT_DETAIL.get(key, ())
        st = item.get("state")
        if isinstance(st, bool):
            st = "real" if st else None
        st = str(st).strip().lower() if st else None
        if st in ("true", "yes"):
            st = "real"
        elif st in ("false", "no", ""):
            st = None
        if st not in ("real", "printed", "unclear"):
            st = None
        ev = str(item.get("evidence") or "").strip()
        has_detail = any(item.get(f) for f in details)
        if not ev and not has_detail:
            st = None
        if st == "printed" and not any(w in ev.lower() for w in _PRINT_WORDS):
            st = "unclear"
        if st == "unclear" and not ev:
            st = None
        if st in ("printed", "unclear"):
            for f in details:
                if item.get(f):
                    item[f] = None
            if not any(zh in str(u) and "确认" in str(u) for u in unk):
                unk.append(f"{zh} state={st}，是否真有该工艺需客户确认")
        item["state"] = st
    payload["unknowns"] = unk
    return payload


# ── 逐页清单「两遍取交集」（2026-11-24）──────────────────────────────
# 同一段视频、同一组帧跑两遍，4B 给出的页数会大幅波动（实测同配置 15 页 vs 6 页，±60%）。
# 两遍都描述到的页才算「采信」；只出现一遍的单独列出来交人工核 —— 不静默丢，也不静默采。
def _page_blob(pg: dict[str, Any]) -> str:
    parts = [str(pg.get("left") or ""), str(pg.get("right") or ""),
             " ".join(str(t) for t in (pg.get("text") or []))]
    pop = pg.get("popup")
    if isinstance(pop, dict):
        parts += [str(pop.get("subject") or ""), str(pop.get("type") or "")]
    return " ".join(x.strip() for x in parts if str(x).strip())


def _page_similarity(a: str, b: str) -> float:
    """序列相似度（difflib 的字符级 LCS 比例）。

    试过二元组 Jaccard：中文换句说法时掉到 0.41（「一个立体的玻璃瓶和彩色山丘结构」
    vs「立体的玻璃瓶与彩色山丘」），把同一页判成两页 —— 太严。LCS 比例对增删字更宽容，
    同一段话换说法约 0.6-0.7，不相关页 <0.1，界线清楚。
    """
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _combine_pages(pa: dict[str, Any], pb: dict[str, Any], score: float) -> dict[str, Any]:
    """两遍都认到的同一页：字段取更完整的那一份，popup 的 real 优先。"""
    def _longer(x: Any, y: Any) -> Any:
        return x if len(str(x or "")) >= len(str(y or "")) else y

    frames = sorted(set(list(pa.get("frames") or []) + list(pb.get("frames") or [])))
    ta = [str(t) for t in (pa.get("text") or []) if str(t).strip()]
    tb = [str(t) for t in (pb.get("text") or []) if str(t).strip()]
    pop_a, pop_b = pa.get("popup"), pb.get("popup")
    pop = pop_a if isinstance(pop_a, dict) else {}
    if isinstance(pop_b, dict):
        if str(pop_b.get("state") or "") == "real" and str(pop.get("state") or "") != "real":
            pop = pop_b
        elif not any(pop.get(k) for k in ("subject", "type")) and any(pop_b.get(k) for k in ("subject", "type")):
            pop = pop_b
    notes = [n for n in {str(pa.get("note") or "").strip(), str(pb.get("note") or "").strip()} if n]
    return {
        "no": 0,
        "kind": _longer(pa.get("kind"), pb.get("kind")),
        "frames": frames,
        "left": _longer(pa.get("left"), pb.get("left")),
        "right": _longer(pa.get("right"), pb.get("right")),
        "text": ta if len("".join(ta)) >= len("".join(tb)) else tb,
        "popup": pop or None,
        "note": "；".join(notes) if notes else None,
        "consensus": round(score, 2),
    }


def merge_page_runs(run_a: Any, run_b: Any, threshold: float = 0.55) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """两遍的 pages 取交集。返回 (采信页, 只出现一遍的页)。"""
    a = [p for p in (run_a or []) if isinstance(p, dict) and _page_blob(p)]
    b = [p for p in (run_b or []) if isinstance(p, dict) and _page_blob(p)]
    used: set[int] = set()
    merged: list[dict[str, Any]] = []
    single: list[dict[str, Any]] = []
    for pa in a:
        blob = _page_blob(pa)
        best_j, best_s = None, 0.0
        for j, pb in enumerate(b):
            if j in used:
                continue
            sc = _page_similarity(blob, _page_blob(pb))
            if sc > best_s:
                best_s, best_j = sc, j
        if best_j is not None and best_s >= threshold:
            used.add(best_j)
            merged.append(_combine_pages(pa, b[best_j], best_s))
        else:
            single.append(pa)
    for j, pb in enumerate(b):
        if j not in used:
            single.append(pb)
    for i, pg in enumerate(merged, start=1):
        pg["no"] = i
    return merged, single


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
        prompt = str(prompt).replace("{FRAME_COUNT}", str(len(images)))
        reply = client.chat(images, prompt)
        payload = parse_json_reply(reply["text"]) or {}
        # 逐页清单跑两遍取交集（2026-11-24）：4B 页数波动 ±60%，两遍都认到的页才采信
        if kind == "video" and cfg.get("video_page_consensus", True):
            reply2 = client.chat(images, prompt)
            payload2 = parse_json_reply(reply2["text"]) or {}
            th = float(cfg.get("video_page_consensus_threshold") or 0.55)
            merged, single = merge_page_runs(payload.get("pages"), payload2.get("pages"), th)
            n_a = len([x for x in (payload.get("pages") or []) if isinstance(x, dict)])
            n_b = len([x for x in (payload2.get("pages") or []) if isinstance(x, dict)])
            if merged:
                payload["pages"] = merged
                if single:
                    payload["pages_single_run"] = single
                payload.setdefault("unknowns", []).append(
                    f"逐页清单跑了两遍取交集（第一遍 {n_a} 页 / 第二遍 {n_b} 页）："
                    f"两遍都描述到的 {len(merged)} 页已采信，"
                    f"只在其中一遍出现的 {len(single)} 页未采信（见「只出现一遍」段），需人工核")
            elif single:
                payload["pages"] = payload.get("pages") or []
                payload.setdefault("unknowns", []).append(
                    f"两遍逐页清单没有共同页（{n_a} 页 vs {n_b} 页），交集为空 —— 退回单遍结果，必须人工对照视频")
            reply = {"text": reply["text"], "latency_s": reply["latency_s"] + reply2["latency_s"],
                     "usage": reply.get("usage")}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if kind != "video":
        payload = normalize_components(payload)
    payload = normalize_crafts(payload)

    # ── 复读护栏（2026-11-24，实测教训）──
    # Qwen3-VL-4B 在帧数偏多（28 帧）时会退化成复读：P8–P20 二十条全写「彩色圆点图案/卡通怪兽」。
    # 这里把「连续且 left/right/text 完全相同」的页合并，并在 unknowns 里挑明 —— 宁少不多。
    if kind == "video" and isinstance(payload.get("pages"), list):
        merged: list[dict[str, Any]] = []
        dropped = 0
        for pg in payload["pages"]:
            if not isinstance(pg, dict):
                continue
            sig = (str(pg.get("left") or ""), str(pg.get("right") or ""),
                   "|".join(str(t) for t in (pg.get("text") or [])))
            if merged:
                prev = merged[-1]
                psig = (str(prev.get("left") or ""), str(prev.get("right") or ""),
                        "|".join(str(t) for t in (prev.get("text") or [])))
                if sig == psig:
                    frames = list(dict.fromkeys((prev.get("frames") or []) + (pg.get("frames") or [])))
                    prev["frames"] = frames
                    dropped += 1
                    continue
            merged.append(pg)
        if dropped:
            for i, pg in enumerate(merged, start=1):
                pg["no"] = i
            payload["pages"] = merged
            payload.setdefault("unknowns", []).append(
                f"模型输出里有 {dropped} 条页描述与上一条完全相同（复读），已合并；"
                f"逐页清单实际只剩 {len(merged)} 页，需人工对照视频核一遍")

    # ── 页级文字兜底（2026-11-24）──
    # 4B 有时把整页文字写进 pages[].text 却把顶层 texts 留空；抽取引擎读的是 texts，
    # 这里按页序汇总补上（页序 = 书页顺序，比拼在一起的 texts 更好核对）。
    if kind == "video" and isinstance(payload.get("pages"), list):
        page_texts: list[str] = []
        clean_pages: list[dict[str, Any]] = []
        for pg in payload["pages"]:
            if not isinstance(pg, dict):
                continue
            try:
                pg["no"] = int(str(pg.get("no")).strip())
            except (TypeError, ValueError):
                pass
            clean_pages.append(pg)
            if isinstance(pg.get("text"), list):
                page_texts += [str(t).strip() for t in pg["text"] if str(t).strip()]
        payload["pages"] = clean_pages
        seen_pages = {str(p.get("no")) for p in clean_pages if p.get("no") is not None}
        if clean_pages and len(seen_pages) != len(clean_pages):
            payload.setdefault("unknowns", []).append("pages 里有重复的页号，页序需人工核对")
        if page_texts and not payload.get("texts"):
            payload["texts"] = page_texts
            payload.setdefault("unknowns", []).append("顶层 texts 为空，已从 pages[].text 按页序汇总")

    # ── 文本去重（2026-11-24）──
    # 4B 在整段视频上会把同一句画面文字循环输出几十次（实测 227 行 → 29 行唯一），
    # 也会在 raw_text 里重复抄同一行。转录里留着会污染抽取引擎，这里按顺序去重。
    for key in ("texts", "raw_text"):
        vals = payload.get(key)
        if not isinstance(vals, list) or not vals:
            continue
        seen, uniq = set(), []
        for v in vals:
            s = str(v).strip()
            if not s or s in seen:
                continue
            seen.add(s)
            uniq.append(s)
        if len(uniq) != len(vals):
            payload.setdefault("unknowns", []).append(
                f"{key} 原文重复输出 {len(vals)} 行，已按顺序去重为 {len(uniq)} 行")
        payload[key] = uniq

    # raw_text 硬上限（2026-11-23 森西拍板）：提示词里已要求 ≤40 行，
    # 但 4B 模型不总听话，这里兜底截断，超出的写进 unknowns，避免长文本撑爆 JSON。
    _lim = cfg.get("max_raw_text_lines")
    if kind != "video" and _lim:
        raw_lines = payload.get("raw_text") or []
        if isinstance(raw_lines, list) and len(raw_lines) > int(_lim):
            extra = len(raw_lines) - int(_lim)
            payload["raw_text"] = raw_lines[: int(_lim)]
            unk = payload.get("unknowns")
            if not isinstance(unk, list):
                unk = []
            unk.append(f"原文还有约 {extra} 行未转录（超出 {int(_lim)} 行上限）")
            payload["unknowns"] = unk

    payload.setdefault("_meta", {})
    payload["_meta"].update({"file": path.name, "kind": kind, "model": client.model,
                             "latency_s": reply["latency_s"], "frames": len(images),
                             "usage": reply.get("usage"), "raw_reply": reply["text"][:2000]})
    # 逐页清单条数 ≈ 帧数 → 多半是按帧记而不是按页归并，必须说出来
    if kind == "video" and isinstance(payload.get("pages"), list) and images:
        n_pages = len([p for p in payload["pages"] if isinstance(p, dict)])
        if n_pages >= max(3, int(len(images) * 0.9)):
            payload.setdefault("unknowns", []).append(
                f"逐页清单 {n_pages} 条 ≈ 帧数 {len(images)}，可能按帧分页而非按页归并"
                f"（一本 10 跨页的书通常少于帧数），页数需人工归并")

    # 视频太长、帧数打了上限 → 未必每页都有帧，必须说出来
    if kind == "video" and len(images) >= int(cfg.get("max_video_frames") or 28):
        dur = _probe_duration(path)
        gap = dur / max(len(images), 1)
        if gap > float(cfg.get("video_frame_interval") or 3.0) + 0.5:
            payload.setdefault("unknowns", []).append(
                f"视频 {dur:.0f}s，只抽到 {len(images)} 帧（约 {gap:.1f}s 一帧），可能有页没拍到")

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
