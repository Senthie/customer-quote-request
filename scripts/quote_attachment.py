#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""quote_attachment.py —— 询价单元 → 压图打 zip → 挂到 ERP 报价需求单附件。

> 业务规则（森西 2026-11-23）：**所有询价单录入报价需求时，询价文件必须打包成 zip 上传附件。**

为什么单独一层
--------------
`erp_attachment` 工具能上传，但有两条硬限制（2026-11-23 实测）：
  1. 单次 payload 约 4 MB（base64 后），手机原图 zip 27–72 MB 直接 HTTP 413；
  2. 删除要传**附件名称**而不是文件 ID。
所以这里负责「把单元目录压到能传的大小」并做幂等：同名附件已存在就跳过。

三种用法
--------
    # ① 建单后直接挂（erp_push.py 会自己调，也可单独跑）
    uv run ./scripts/quote_attachment.py --unit customer-files/pending/5 \\
        --record-id <GUID> --bdbh BJXQ261201001 --confirm

    # ② 从推送回执自取 bdbh / record_id / 单元目录
    uv run ./scripts/quote_attachment.py --artifact <目录>/ERP_xxx.erp-push.json --confirm

    # ③ 兜底补挂（scan_pending.sh 每轮跑一次；幂等，已挂的跳过）
    uv run ./scripts/quote_attachment.py --backfill --confirm

    # 只打包不上传
    uv run ./scripts/quote_attachment.py --unit <目录> --dry-run --show-paths

退出码：0 = 全部已挂或跳过；1 = 有失败；2 = 参数/文件问题。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import sys
import time
import zipfile
from pathlib import Path
from typing import Any, Optional

REPO = Path(__file__).resolve().parent.parent
CUSTOMER_FILES = REPO / "customer-files"

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

# 附件显示名（幂等键）：询价原始件（<单元名>）
ATTACH_NAME = "询价原始件（{unit}）"

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
DROP_SUFFIXES = {".pyc", ".zip", ".bak"}

# 2026-11-23 MCP 更新后：网关允许分片（1.5MB/片），实际瓶颈变成 ERP 上传控件 ≈10MB。
# 打包/上传/校验统一委托给 erp_attachment_upload，避免两套实现漂移。
import erp_attachment_upload as eu  # noqa: E402

TIERS = eu.TIERS                      # 见 erp_attachment_upload.TIERS（按 9.5MB 预算降采样）
BUDGET = eu.DEFAULT_BUDGET


# ── 收集文件 ─────────────────────────────────────────────────────────
def collect_files(unit: Path) -> list[Path]:
    """单元目录里的原始询价件 + 转录产物（转录是图片数值的出处，必须一起留档）。"""
    out: list[Path] = []
    if unit.is_file():
        return [unit]
    for p in sorted(unit.iterdir()):
        if p.is_dir() or p.name.startswith("_") or p.name.startswith("."):
            continue
        if p.suffix.lower() in DROP_SUFFIXES:
            continue
        if p.suffix.lower() == ".json" and ".vlm" not in p.name and "erp-" not in p.name:
            # 其它 json（如 _email.meta.json）留着，它是来源证据
            out.append(p)
            continue
        out.append(p)
    return out


def build_zip(unit: Path, out_dir: Path, unit_label: str, budget: int = BUDGET) -> tuple[Path, dict[str, Any]]:
    """委托 erp_attachment_upload.build_zip（按预算降采样），补齐旧统计键。"""
    zp, stats = eu.build_zip(unit, out_dir, unit_label, budget=budget)
    stats["sha256"] = hashlib.sha256(zp.read_bytes()).hexdigest()
    stats["base64_size"] = int(stats["size"] * 4 / 3)
    stats["over_budget"] = stats["size"] > eu.ERP_UPLOAD_MAX
    return zp, stats


# ── ERP 侧 ───────────────────────────────────────────────────────────
def attach_name_for(unit_label: str) -> str:
    return ATTACH_NAME.format(unit=unit_label)


def list_attachments(client: Any, record_id: str, module: str = "quote_request") -> list[dict[str, Any]]:
    res = client.call_tool("erp_attachment", {"module": module, "record_id": record_id})
    if isinstance(res, dict) and res.get("raw"):
        raise RuntimeError(f"列附件失败：{str(res.get('raw'))[:200]}")
    return list(res.get("files") or [])


def attach_unit(unit: Path, *, record_id: str, bdbh: str = "", client: Any,
                confirm: bool = True, force: bool = False, module: str = "quote_request",
                out_dir: Optional[Path] = None, skip_verify: bool = False) -> dict[str, Any]:
    """打包并上传一个单元的 zip；同名附件已存在则跳过（force 才重传）。"""
    unit_label = (bdbh or unit.name).strip() or unit.name
    name = attach_name_for(unit_label)
    zp, stats = build_zip(unit, out_dir or (unit if unit.is_dir() else unit.parent) / "_attachment", unit_label)
    report: dict[str, Any] = {"unit": str(unit), "record_id": record_id, "bdbh": bdbh,
                              "attach_name": name, **stats}

    existing = list_attachments(client, record_id, module)
    # 幂等键取「名称前缀」：一个单只需要一份询价原始件，历史名/改名都算已挂
    have = [f for f in existing if str(f.get("名称") or "").strip().startswith(ATTACH_NAME.split("（")[0])]
    report["existing"] = len(existing)
    if have and not force:
        report.update(status="skipped", reason="同名附件已存在（force 才重传）")
        return report
    if not confirm:
        report.update(status="dry_run", reason="未传 --confirm / confirm=False")
        return report

    if have and force:   # 按真实名称删（历史名也算），不能只删新名字
        for f in have:
            old_name = str(f.get("名称") or "")
            if old_name:
                client.call_tool("erp_attachment", {"module": module, "record_id": record_id,
                                                    "delete": old_name, "confirm": True})

    # 分片上传（网关请求体 ~4MB，单次直传只在小文件时可行）+ 下载回读校验
    up = eu.upload_chunked(zp, record_id=record_id, unit_label=unit_label, client=client,
                           module=module, verbose=False)
    ok = bool(up.get("ok"))
    verified = bool(up.get("verified"))
    report.update(status="attached" if (ok and verified) else "failed",
                  erp_ok=ok, verified=verified, count=up.get("count"),
                  chunks=up.get("chunks"), sent_requests=up.get("sent_requests"),
                  errors=up.get("errors") or ([] if ok else [str(up.get("error"))[:300]]))
    if ok and verified and not skip_verify:
        report["verify"] = eu.verify_download(client, record_id=record_id, name=name,
                                              expect_sha=stats["sha256"], module=module)
    return report


# ── 从推送回执 / 扫描补挂 ─────────────────────────────────────────────
def _iter_push_receipts() -> list[Path]:
    out: list[Path] = []
    for base in (CUSTOMER_FILES / "pending", CUSTOMER_FILES / "processed"):
        if not base.is_dir():
            continue
        out.extend(sorted(base.rglob("*.erp-push.json")))
    return out


def _units_from_receipt(path: Path) -> list[tuple[Path, str, str]]:
    """回执 → [(单元目录, bdbh, record_id)]。一个回执可能有多条记录。"""
    try:
        rep = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    results = rep.get("results") or []
    pairs: list[tuple[str, str]] = []
    for r in results:
        if r.get("status") != "created":
            continue
        erp = r.get("erp_response") or {}
        rid = str(erp.get("record_id") or "").strip()
        bdbh = str(r.get("bdbh") or erp.get("bdbh") or "").strip()
        if rid:
            pairs.append((bdbh, rid))
    if not pairs:
        return []
    # 单元目录：优先「回执所在目录下唯一子目录」，其次回执所在目录本身
    parent = path.parent
    subs = [p for p in parent.iterdir() if p.is_dir() and not p.name.startswith("_")
            and not p.name.startswith(".")] if parent.is_dir() else []
    unit = subs[0] if len(subs) == 1 else parent
    return [(unit, bdbh, rid) for bdbh, rid in pairs]


def backfill(client: Any, *, confirm: bool, force: bool = False) -> dict[str, Any]:
    items = []
    for rp in _iter_push_receipts():
        for unit, bdbh, rid in _units_from_receipt(rp):
            if not unit.is_dir():
                continue
            items.append((rp, unit, bdbh, rid))
    results = []
    for rp, unit, bdbh, rid in items:
        try:
            r = attach_unit(unit, record_id=rid, bdbh=bdbh, client=client, confirm=confirm, force=force)
        except Exception as exc:  # 单个失败不影响其它
            r = {"unit": str(unit), "bdbh": bdbh, "record_id": rid,
                 "status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        r["receipt"] = str(rp)
        results.append(r)
    return {"scanned": len(items), "results": results,
            "attached": sum(1 for r in results if r.get("status") == "attached"),
            "skipped": sum(1 for r in results if r.get("status") == "skipped"),
            "failed": sum(1 for r in results if r.get("status") == "failed")}


def attach_for_report(artifact: Path, report: dict[str, Any], *, client: Any,
                      confirm: bool = True) -> dict[str, Any]:
    """erp_push.py 建单成功后的挂钩：拿 report 里的 bdbh/record_id 直接挂附件。"""
    results = report.get("results") or []
    done = []
    unit_hint = artifact.parent
    for r in results:
        if r.get("status") != "created":
            continue
        erp = r.get("erp_response") or {}
        rid = str(erp.get("record_id") or "").strip()
        bdbh = str(r.get("bdbh") or erp.get("bdbh") or "").strip()
        if not rid:
            done.append({"bdbh": bdbh, "status": "failed", "error": "回执里没有 record_id"})
            continue
        # 单元目录：产物目录下唯一子目录 = 单元（processed 归档后的形态）
        subs = [p for p in unit_hint.iterdir() if p.is_dir() and not p.name.startswith("_")
                and not p.name.startswith(".")] if unit_hint.is_dir() else []
        unit = subs[0] if len(subs) == 1 else unit_hint
        try:
            done.append(attach_unit(unit, record_id=rid, bdbh=bdbh, client=client, confirm=confirm))
        except Exception as exc:
            done.append({"bdbh": bdbh, "record_id": rid,
                         "status": "failed", "error": f"{type(exc).__name__}: {exc}"})
    return {"status": "attached" if done and all(d.get("status") in ("attached", "skipped") for d in done) else "failed",
            "items": done}


# ── CLI ──────────────────────────────────────────────────────────────
def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="询价单元 → zip → ERP 报价需求附件")
    ap.add_argument("--unit", default="", help="询价单元目录（含原始件）")
    ap.add_argument("--artifact", default="", help="*_*.erp-push.json 回执（自取 bdbh/record_id/单元）")
    ap.add_argument("--bdbh", default="", help="ERP 表单编号（仅用于附件显示名）")
    ap.add_argument("--record-id", default="", help="ERP 记录 ID（GUID）")
    ap.add_argument("--module", default="quote_request", help="附件模块（默认 quote_request）")
    ap.add_argument("--backfill", action="store_true", help="扫 pending/processed 的推送回执补挂")
    ap.add_argument("--confirm", action="store_true", help="真上传；不加 = 只打包 + 干跑")
    ap.add_argument("--force", action="store_true", help="同名附件已存在也重传")
    ap.add_argument("--dry-run", action="store_true", help="只打包，不连 ERP")
    ap.add_argument("--show-paths", action="store_true", help="输出里带上文件明细")
    ap.add_argument("--quiet", action="store_true", help="只打一行摘要（给 scan_pending.sh 用，没事时完全安静）")
    ap.add_argument("--token", default="")
    ap.add_argument("--url", default="")
    args = ap.parse_args(argv)

    client = None
    if not args.dry_run:
        from erp_push import DEFAULT_MCP_URL, McpHttpClient
        token = args.token or os.environ.get("ERP_MCP_TOKEN", "")
        if not token:
            try:
                from local_config import resolve
                token = resolve("erp_endpoint", "token", "ERP_MCP_TOKEN") or ""
            except ImportError:
                token = ""
        client = McpHttpClient(url=args.url or DEFAULT_MCP_URL, token=token)
        try:
            client.initialize()
        except Exception as exc:
            print(json.dumps({"status": "error", "error": f"MCP 连不上：{exc}"}, ensure_ascii=False, indent=2))
            return 3

    confirm = bool(args.confirm) and not args.dry_run

    out: Any
    if args.backfill:
        out = backfill(client, confirm=confirm, force=args.force) if client else {"status": "dry_run"}
    elif args.artifact:
        rep = json.loads(Path(args.artifact).read_text(encoding="utf-8"))
        if client is None:
            out = {"status": "dry_run", "note": "dry-run 下不解析回执"}
        else:
            out = attach_for_report(Path(args.artifact), rep, client=client, confirm=confirm)
    else:
        if not args.unit or (not args.record_id and not args.dry_run):
            print("要么给 --backfill，要么给 --artifact，要么同时给 --unit 与 --record-id（--dry-run 只打包时 record-id 可省）", file=sys.stderr)
            return 2
        unit = Path(args.unit)
        if not unit.exists():
            print(f"单元不存在：{unit}", file=sys.stderr)
            return 2
        if client is None:  # dry-run：只打包
            zp, stats = build_zip(unit, unit.parent / "_attachment", (args.bdbh or unit.name))
            out = {"status": "packed", **stats}
        else:
            out = attach_unit(unit, record_id=args.record_id, bdbh=args.bdbh, client=client,
                              confirm=confirm, force=args.force, module=args.module)

    if not args.show_paths:
        def strip(o):
            if isinstance(o, dict):
                return {k: strip(v) for k, v in o.items() if k != "files_detail"}
            if isinstance(o, list):
                return [strip(i) for i in o]
            return o
        out = strip(out)
    out["at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    if args.quiet:
        if args.backfill:
            n_att, n_skip, n_bad = out.get("attached", 0), out.get("skipped", 0), out.get("failed", 0)
            if n_att or n_bad:
                print(f"📎 附件补挂：新增 {n_att} 份、跳过 {n_skip} 份、失败 {n_bad} 份")
                for r in out.get("results", []):
                    if r.get("status") == "failed":
                        print(f"  ❌ {r.get('bdbh') or r.get('unit')}：{r.get('error') or r.get('errors')}")
        elif out.get("status") not in ("attached", "skipped", "packed"):
            print(f"📎 附件失败：{out.get('bdbh') or out.get('unit')} {out.get('error') or out.get('errors')}")
    else:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    bad = out.get("failed") or (out.get("status") in ("failed", "error"))
    if isinstance(out.get("items"), list):
        bad = bad or any(i.get("status") == "failed" for i in out["items"])
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
