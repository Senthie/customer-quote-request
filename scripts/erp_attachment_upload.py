#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""erp_attachment_upload.py —— 把询价原始件 zip 以**分片**方式挂到 ERP 附件上。

为什么需要它
------------
MCP 更新后 `erp_attachment` 支持分片：
    files=[{upload_key, chunk_index, total_chunks, chunk_base64, ...}]
未收齐 → status=chunk_pending，收齐自动拼装上传并回读。

但实测（2026-11-23）有一条隐藏脾气：**连续快速发大分片时，先发的片会被吞掉**
（14×3MB 跑完只剩最后一片 received=[13]）。所以本脚本不信任「发过就算」——
每片发完都读回 `chunks[].received`，缺哪片补哪片，直到 complete 或重试用尽；
最后再用 size + sha256 双向核对。

用法
----
    # 打包 + 分片上传 + 校验（--confirm 才真传）
    uv run ./scripts/erp_attachment_upload.py \\
        --unit customer-files/pending/5 --record-id <GUID> --bdbh BJXQ261123017 --confirm

    # 直接用已打好的原始 zip（跳过打包）
    uv run ./scripts/erp_attachment_upload.py --zip zips/quote_5.zip \\
        --record-id <GUID> --bdbh BJXQ261201001 --confirm

    # 批量：按 erp_mapping.md 的映射表跑整个目录
    uv run ./scripts/erp_attachment_upload.py --mapping <dir>/erp_mapping.md --zips <dir>/zips --confirm

退出码：0=全部 attach 成功；1=有失败；2=参数问题。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import sys
import time
import zipfile
from pathlib import Path
from typing import Any, Optional

REPO = Path(__file__).resolve().parent.parent
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
DROP_SUFFIXES = {".pyc", ".zip", ".bak"}
ATTACH_NAME = "询价原始件（{unit}）"
CHUNK = 1_500_000          # 1.5MB/片（base64 约 2MB，稳过网关）
MAX_ROUNDS = 6             # 缺片补传最多几轮

# 两级上限（2026-11-23 二分实测）：
#   ① MCP 网关请求体 ≈4MB → 大文件必须分片（1.5MB/片）
#   ② ERP 上传控件 ≈10MB（10.49MB 过、11.32MB 报「上传后没有出现在列表里」，
#      注意它不是报超限，而是静默不落地）→ zip 预算取 9.5MB
# 另：MCP 侧还有 ERP_ATTACH_MAX_MB=20MB 的显式报错（拼装阶段才触发）。
SERVER_MAX = 20 * 1024 * 1024
ERP_UPLOAD_MAX = 10 * 1024 * 1024
DEFAULT_BUDGET = 9_500_000
TIERS = [(3000, 90), (2700, 88), (2400, 86), (2100, 84), (1800, 82),
         (1600, 80), (1400, 78), (1200, 74)]


# ── 打包：原图原尺寸，不压 ────────────────────────────────────────────
def _unit_files(unit: Path) -> list[Path]:
    files: list[Path] = []
    for p in sorted(unit.iterdir()):
        if p.is_dir() or p.name.startswith(("_", ".")):
            continue
        if p.suffix.lower() in DROP_SUFFIXES:
            continue
        files.append(p)
    return files


def build_zip(unit: Path, out_dir: Path, label: str, budget: int = DEFAULT_BUDGET) -> tuple[Path, dict[str, Any]]:
    """按预算打包：逐档降采样直到 zip ≤ budget；原图本身够小就直接原样（STORE）打。"""
    files = _unit_files(unit)
    images = [f for f in files if f.suffix.lower() in IMAGE_SUFFIXES]
    others = [f for f in files if f.suffix.lower() not in IMAGE_SUFFIXES]
    out_dir.mkdir(parents=True, exist_ok=True)
    zp = out_dir / f"{label}.zip"
    tmp = out_dir / f".{label}.zip.tmp"      # 先写临时名，成功再改名，被打断不留半截 zip
    if tmp.exists():
        tmp.unlink()

    raw_total = sum(f.stat().st_size for f in files)
    # 原图总量已经够小 → 原样打包，零损失
    if raw_total <= budget:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
            for f in files:
                z.write(f, f"{unit.name}/{f.name}")
        tmp.replace(zp)
        return zp, {"unit": unit.name, "files": len(files), "images": len(images),
                    "others": len(others), "zip": str(zp), "size": zp.stat().st_size,
                    "max_edge": None, "quality": None, "lossless": True,
                    "raw_total": raw_total}

    from PIL import Image, ImageOps  # 延迟导入

    best: Optional[tuple[Path, dict[str, Any]]] = None
    for max_edge, quality in TIERS:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for f in images:
                try:
                    im = ImageOps.exif_transpose(Image.open(f)).convert("RGB")
                    im.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
                    buf = io.BytesIO()
                    im.save(buf, "JPEG", quality=quality, optimize=True)
                    z.writestr(f"{unit.name}/{f.name}", buf.getvalue())
                except Exception:
                    z.write(f, f"{unit.name}/{f.name}")      # 坏图不丢件
            for f in others:
                z.write(f, f"{unit.name}/{f.name}")
        tmp.replace(zp)
        stats = {"unit": unit.name, "files": len(files), "images": len(images),
                 "others": len(others), "zip": str(zp), "size": zp.stat().st_size,
                 "max_edge": max_edge, "quality": quality, "lossless": False,
                 "raw_total": raw_total, "budget": budget}
        best = (zp, stats)
        if zp.stat().st_size <= budget:
            break
    assert best is not None
    return best


# ── 分片上传 ─────────────────────────────────────────────────────────
def _send_chunk(client: Any, *, record_id: str, module: str, zp: Path, key: str,
                idx: int, total: int, part: bytes, name: str, sha: str,
                full_size: int, remark: str) -> dict[str, Any]:
    return client.call_tool("erp_attachment", {
        "module": module, "record_id": record_id,
        "files": [{
            "filename": zp.name, "name": name, "remark": remark,
            "upload_key": key, "chunk_index": idx, "total_chunks": total,
            "chunk_base64": base64.b64encode(part).decode(),
            "expected_size": full_size, "sha256": sha,
        }],
    })


def _chunk_state(res: dict[str, Any]) -> tuple[list[int], bool]:
    ch = (res.get("chunks") or [{}])[0] if res.get("chunks") else {}
    recv = [int(x) for x in (ch.get("received") or [])]
    return recv, bool(ch.get("complete") or res.get("verified"))


def upload_chunked(zp: Path, *, record_id: str, unit_label: str, client: Any,
                   module: str = "quote_request", chunk: int = CHUNK,
                   verbose: bool = True) -> dict[str, Any]:
    """分片上传 + 缺片补传 + size/sha 核对。"""
    raw = zp.read_bytes()
    total_bytes = len(raw)
    sha = hashlib.sha256(raw).hexdigest()
    n = (total_bytes + chunk - 1) // chunk
    name = ATTACH_NAME.format(unit=unit_label)
    key = f"q-{record_id[:8]}-{sha[:10]}"
    remark = f"{total_bytes / 1e6:.2f}MB 原始件（{n} 片）"

    if verbose:
        print(f"  {zp.name}  {total_bytes:,}B  sha256={sha[:12]}…  → {n} 片 × {chunk:,}B  key={key}")

    sent = 0
    last: dict[str, Any] = {}
    for rnd in range(1, MAX_ROUNDS + 1):
        have: set[int] = set()
        # 先探一次当前已收（避免无脑重传）
        probe = client.call_tool("erp_attachment", {"module": module, "record_id": record_id,
                                                    "files": [{"upload_key": key, "chunk_index": -1,
                                                               "total_chunks": n,
                                                               "chunk_base64": ""}]})
        recv, complete = _chunk_state(probe)
        have = set(recv)
        if verbose:
            print(f"  轮次 {rnd}: 已收 {len(have)}/{n}")
        if complete or len(have) == n:
            last = probe
            break
        for i in range(n):
            if i in have:
                continue
            res = _send_chunk(client, record_id=record_id, module=module, zp=zp, key=key,
                              idx=i, total=n, part=raw[i * chunk:(i + 1) * chunk],
                              name=name, sha=sha, full_size=total_bytes, remark=remark)
            sent += 1
            # 拼装阶段被服务端拒绝（如超单附件上限）→ 立刻停，别白传六轮
            err_txt = json.dumps(res, ensure_ascii=False)
            if "超过上限" in err_txt or "exceeds" in err_txt.lower():
                out = {"zip": zp.name, "size": total_bytes, "sha256": sha, "chunks": n,
                       "sent_requests": sent, "ok": False, "status": "server_rejected",
                       "error": err_txt[:400], "attach_name": name}
                if verbose:
                    print(f"  ❌ 服务端拒绝：{err_txt[:200]}")
                return out
            recv, complete = _chunk_state(res)
            if complete or res.get("verified"):
                last = res
                break
            last = res
            if verbose and (i + 1) % 5 == 0:
                print(f"    发到 {i + 1}/{n}，服务端已收 {len(recv)}")
        recv, complete = _chunk_state(last)
        if complete or len(recv) == n:
            break
        if verbose:
            print(f"  轮次 {rnd} 结束仍缺 {n - len(set(recv))} 片，重来")
        time.sleep(1.5)

    recv, complete = _chunk_state(last)
    out: dict[str, Any] = {
        "zip": zp.name, "size": total_bytes, "sha256": sha, "chunks": n,
        "sent_requests": sent, "received_chunks": len(set(recv)),
        "complete": complete, "attach_name": name,
        "ok": bool(last.get("ok")), "status": last.get("status"),
        "verified": bool(last.get("verified")), "size_ok": last.get("size_ok"),
        "count": last.get("count"), "errors": last.get("errors"),
        "file_id": None,
    }
    for f in (last.get("files") or []):
        if str(f.get("名称") or "").startswith("询价原始件"):
            out["file_id"] = f.get("文件ID")
    return out


# ── 校验：下载回读比 sha256 ───────────────────────────────────────────
def verify_download(client: Any, *, record_id: str, name: str, expect_sha: str,
                    module: str = "quote_request", via_proxy: bool = True) -> dict[str, Any]:
    """① MCP download 取 base64 比 sha256；② proxy_url 用 HTTP GET 再比一次。"""
    res: dict[str, Any] = {"name": name}
    dl = client.call_tool("erp_attachment", {"module": module, "record_id": record_id,
                                             "download": name})
    b64 = dl.get("content_base64") or ""
    if b64:
        got = base64.b64decode(b64)
        got_sha = hashlib.sha256(got).hexdigest()
        res.update(mcp_bytes=len(got), mcp_sha256=got_sha, mcp_match=got_sha == expect_sha)
    else:
        res.update(mcp_bytes=0, mcp_match=False, mcp_error=(dl.get("raw") or "no content_base64"))

    url = dl.get("proxy_url") or ""
    if via_proxy and url:
        try:
            import ssl
            import urllib.request
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=300, context=ctx) as r:
                body = r.read()
            p_sha = hashlib.sha256(body).hexdigest()
            res.update(proxy_bytes=len(body), proxy_sha256=p_sha, proxy_match=p_sha == expect_sha,
                       proxy_status=r.status)
        except Exception as exc:
            res.update(proxy_error=f"{type(exc).__name__}: {exc}")
    res["ok"] = bool(res.get("mcp_match") and (res.get("proxy_match") is not False))
    return res


def delete_attach(client: Any, record_id: str, name: str, module: str = "quote_request") -> dict[str, Any]:
    return client.call_tool("erp_attachment", {"module": module, "record_id": record_id,
                                               "delete": name, "confirm": True})


# ── 映射表解析 ───────────────────────────────────────────────────────
def parse_mapping(md: Path) -> list[list[str]]:
    """| 目录 | 表单编号 | … | 记录 ID | → [[目录, 表单编号, 记录ID], …]"""
    rows = []
    for line in md.read_text(encoding="utf-8").splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 4 or cells[0] in ("目录",) or set(cells[0]) <= {"-"}:
            continue
        rid = next((c for c in cells if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", c)), "")
        bdbh = next((c for c in cells if c.startswith("BJXQ")), "")
        if rid and bdbh:
            rows.append([cells[0], bdbh, rid])
    return rows


# ── CLI ──────────────────────────────────────────────────────────────
def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="询价原始件 → ERP 附件（按预算打 zip + 分片上传 + 下载校验）")
    ap.add_argument("--unit", default="", help="询价单元目录（现场按预算打包）")
    ap.add_argument("--units", default="", help="批量：单元目录的父目录（配 --mapping）")
    ap.add_argument("--zip", default="", help="已打好的 zip（必须 ≤20MB）")
    ap.add_argument("--mapping", default="", help="erp_mapping.md（批量）")
    ap.add_argument("--zips", default="", help="批量模式下现成 zip 目录（不给则用 --units 现场打包）")
    ap.add_argument("--bdbh", default="")
    ap.add_argument("--record-id", default="")
    ap.add_argument("--module", default="quote_request")
    ap.add_argument("--chunk-size", type=int, default=CHUNK)
    ap.add_argument("--max-bytes", type=int, default=DEFAULT_BUDGET,
                    help="zip 目标上限（默认 18MB，服务端硬上限 20MB）")
    ap.add_argument("--confirm", action="store_true", help="真上传")
    ap.add_argument("--force", action="store_true", help="已存在同名附件先删再传")
    ap.add_argument("--no-verify", action="store_true", help="跳过下载回读校验")
    ap.add_argument("--dry-run", action="store_true", help="只打包不连 ERP")
    ap.add_argument("--token", default="")
    ap.add_argument("--url", default="")
    args = ap.parse_args(argv)

    from erp_push import DEFAULT_MCP_URL, McpHttpClient
    from local_config import resolve

    # ── 1) 组任务：(label, zip, record_id) ──────────────────────────
    # src 三选一：unit 目录（现场打包）/ 现成 zip
    tasks: list[dict[str, Any]] = []
    if args.mapping:
        for unit_no, bdbh, rid in parse_mapping(Path(args.mapping)):
            t: dict[str, Any] = {"label": bdbh, "record_id": rid, "unit": None, "zip": None}
            if args.units:
                t["unit"] = Path(args.units) / unit_no
            elif args.zips:
                t["zip"] = Path(args.zips) / f"quote_{unit_no}.zip"
            if t["unit"] is None and (t["zip"] is None or not t["zip"].exists()):
                print(f"⚠ 跳过 {bdbh}：既没 --units 也没找到 zip", file=sys.stderr)
                continue
            tasks.append(t)
    elif args.zip or args.unit:
        if not args.record_id:
            print("--zip / --unit 需要配 --record-id", file=sys.stderr)
            return 2
        tasks.append({"label": args.bdbh or "unit", "record_id": args.record_id,
                      "unit": Path(args.unit) if args.unit else None,
                      "zip": Path(args.zip) if args.zip else None})
    else:
        print("要么 --mapping（+--units/--zips），要么 --zip，要么 --unit", file=sys.stderr)
        return 2

    # ── 2) 打包（单元目录才有这一步）────────────────────────────────
    for i, t in enumerate(tasks):
        if t["unit"] is None:
            zp = t["zip"]
            if zp.stat().st_size > SERVER_MAX:
                print(f"⚠ {zp.name} {zp.stat().st_size / 1e6:.1f}MB 超服务端 20MB 上限，改用 --unit 现场降采样",
                      file=sys.stderr)
            t["label"] = t["label"] if t["label"] != "unit" else zp.stem
            continue
        unit = Path(t["unit"])
        if not unit.is_dir():
            print(f"⚠ 单元不存在：{unit}", file=sys.stderr)
            tasks[i] = {}
            continue
        label = t["label"] if t["label"] != "unit" else unit.name
        zp, stats = build_zip(unit, unit / "_attachment", label, budget=args.max_bytes)
        how = "原样" if stats.get("lossless") else f"{stats.get('max_edge')}px/q{stats.get('quality')}"
        print(f"📦 {label}: 原图 {stats.get('raw_total', 0) / 1e6:.1f}MB → zip {stats['size'] / 1e6:.2f}MB（{how}）")
        t["zip"] = zp
        t["label"] = label
    tasks = [t for t in tasks if t]

    if args.dry_run:
        print(json.dumps([{"label": t["label"], "zip": str(t["zip"]),
                           "record_id": t["record_id"],
                           "size": t["zip"].stat().st_size if t["zip"].exists() else None}
                          for t in tasks], ensure_ascii=False, indent=1))
        return 0
    if not args.confirm:
        print("（未加 --confirm，仅打包，不上传）")
        return 0

    # ── 3) 上传 + 校验 ─────────────────────────────────────────────
    client = McpHttpClient(url=args.url or resolve("erp_endpoint", "url", "ERP_MCP_URL") or DEFAULT_MCP_URL,
                           token=args.token or resolve("erp_endpoint", "token", "ERP_MCP_TOKEN"),
                           timeout=600)
    client.initialize()

    results: list[dict[str, Any]] = []
    for t in tasks:
        label, zp, rid = t["label"], Path(t["zip"]), t["record_id"]
        t0 = time.time()
        print(f"\n=== {label} → {rid}  ({zp.name}, {zp.stat().st_size / 1e6:.2f}MB) ===")
        try:
            if args.force:
                lst = client.call_tool("erp_attachment", {"module": args.module, "record_id": rid})
                for f in (lst.get("files") or []):
                    nm = str(f.get("名称") or "")
                    if nm.startswith("询价原始件"):
                        print(f"  ↺ 删旧附件 {nm}")
                        delete_attach(client, rid, nm, args.module)
            up = upload_chunked(zp, record_id=rid, unit_label=label, client=client,
                                module=args.module, chunk=args.chunk_size)
            if up.get("ok") and not args.no_verify:
                up["verify"] = verify_download(client, record_id=rid, name=up["attach_name"],
                                               expect_sha=up["sha256"], module=args.module)
            up["elapsed_s"] = round(time.time() - t0, 1)
            results.append(up)
            print(f"  → ok={up.get('ok')} verified={up.get('verified')} size_ok={up.get('size_ok')} "
                  f"count={up.get('count')} 用时 {up['elapsed_s']}s")
            if up.get("verify"):
                v = up["verify"]
                print(f"  下载校验：MCP {v.get('mcp_bytes')}B match={v.get('mcp_match')} | "
                      f"proxy {v.get('proxy_bytes')}B match={v.get('proxy_match')}")
        except Exception as exc:
            print(f"  ❌ {type(exc).__name__}: {exc}")
            results.append({"label": label, "record_id": rid, "ok": False,
                            "error": f"{type(exc).__name__}: {exc}"})

    out = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "total": len(results),
           "ok": sum(1 for r in results if r.get("ok")), "results": results}
    outf = REPO / "logs" / f"attachment_upload_{time.strftime('%Y%m%d_%H%M%S')}.json"
    outf.parent.mkdir(exist_ok=True)
    outf.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n=== 汇总 {out['ok']}/{out['total']} 成功 → {outf} ===")
    for r in results:
        flag = "✅" if r.get("ok") else "❌"
        print(f"{flag} {r.get('label') or r.get('attach_name')} {r.get('error') or ''}")
    return 0 if out["ok"] == out["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
