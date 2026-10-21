#!/usr/bin/env python3
"""compare_with_erp.py —— 拿客户附件跑一遍管道，与「权威 ERP 导出」逐字段对照。

**为什么需要它**：校准客户时最容易糊弄自己的一步就是「看起来跑通了」。
这个脚本把差异**按原因分类**打印，而不是笼统列一堆不同 —— 这样每次改口径后
一眼就能看清三件事：

  1. 业务已定的口径是否真的生效（该类差异应该「预期如此」而不是「意外」）
  2. 还有哪些差异是**附件里根本取不到**的（要邮件/人工补）
  3. 有没有冒出新差异（那才是真 bug）

用法：

    # 单份对照
    uv run ./scripts/compare_with_erp.py \\
        --customer ngv \\
        --attachment "customer-files/samples/ngv/香港单号 CU26072301 …xlsx" \\
        --erp "customer-files/samples/ngv/CU26072301.erp.json"

    # 整个语料库（按文件名里的单号自动配对，配不上的用 --map 指定）
    uv run ./scripts/compare_with_erp.py --customer ngv \\
        --samples-dir customer-files/samples/ngv

    # 口径白名单（把「业务已定的差异」写成一个 JSON: {"字段": "为什么该不同"}）
    uv run ./scripts/compare_with_erp.py … --accepted /path/accepted.json

输出分四类：`0 与空` / `口径已定（白名单命中）` / `⚠️ 需要核对（白名单外的差异）` /
`ERP 导出侧多出的键`。**只有第三类需要人看**。

退出码：0 = 没有白名单外的差异；1 = 有（CI 里可以拿它当门禁）。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / "assets" / "add_customer_quote_request.json"

# ERP 自动带出的字段：业务已确认「我们留空即正确」，不必当成差异
ERP_AUTO_FIELDS = ["BDBH", "YWY", "KHZL", "GDY", "Remark", "CPMX"]

# 永远属于「设计如此」的字段（与客户无关）
ALWAYS_ACCEPTED = {
    "CPMS": "★ agent 亲自撰写（脚本一律留空）",
    "BDBH": "ERP 自动带出", "YWY": "ERP 自动带出", "KHZL": "ERP 自动带出",
    "GDY": "ERP 自动带出", "Remark": "ERP 自动带出", "CPMX": "ERP 自动带出",
}


def _norm(value):
    return "" if value is None else value


def _load_template_keys() -> list:
    with open(TEMPLATE, encoding="utf-8") as f:
        return list(json.load(f).keys())


def _run_pipeline(customer: str, attachment: Path, workdir: Path) -> dict:
    """跑一遍 main.py（在临时目录里），返回产出的 ERP JSON。"""
    for sub in ("pending", "processed", "failed"):
        (workdir / sub).mkdir(parents=True, exist_ok=True)
    target = workdir / "pending" / attachment.name
    if attachment.is_dir():
        # 单元目录（一封邮件的多个参考文件 = 一份报价）也要能对照
        shutil.copytree(attachment, target)
    else:
        shutil.copy2(attachment, target)

    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "main.py"), str(target),
         "--customer", customer,
         "--output-dir", str(workdir / "processed"),
         "--processed-dir", str(workdir / "processed"),
         "--failed-dir", str(workdir / "failed")],
        cwd=REPO_ROOT, capture_output=True, text=True)

    found = sorted(workdir.glob("processed/*/ERP_*.json"), key=os.path.getmtime)
    if not found:
        print(f"!! 管道没产出 ERP JSON（exit={proc.returncode}）")
        print(proc.stdout[-1500:])
        print(proc.stderr[-800:], file=sys.stderr)
        raise SystemExit(2)

    with open(found[-1], encoding="utf-8") as f:
        result = json.load(f)

    reports = list(workdir.glob("processed/*/*.validation.json"))
    report = {}
    if reports:
        with open(reports[-1], encoding="utf-8") as f:
            report = json.load(f)
    return {"result": result, "validation": report, "json_path": found[-1],
            "log_tail": proc.stdout}


def _compare(ours: dict, gt: dict, keys: list, accepted: dict) -> dict:
    """把逐字段差异分成四类。"""
    buckets = {"zero_vs_blank": [], "accepted": [], "needs_review": [], "extra_keys": []}
    for key in keys:
        a, b = _norm(ours.get(key)), _norm(gt.get(key))
        if a == b:
            continue
        if {str(a), str(b)} <= {"0", ""}:
            # 未使用档位 / 客户目标价：业务 2026-09-24 确认 0 可接受（导出存空）
            buckets["zero_vs_blank"].append((key, a, b))
            continue
        reason = accepted.get(key)
        (buckets["accepted"] if reason else buckets["needs_review"]).append((key, a, b, reason))
    buckets["extra_keys"] = [k for k in gt if k not in keys]
    return buckets


def _report(order: str, ours: dict, gt: dict, validation: dict, accepted: dict,
            keys: list) -> int:
    errors = [i for i in validation.get("issues", []) if i.get("level") == "error"]
    warns = [i for i in validation.get("issues", []) if i.get("level") == "warn"]
    print(f"\n### {order}   校验 error={len(errors)} warn={len(warns)}")
    for i in errors + warns:
        print(f"    [{i['level']}] {i['field']}: {i['message']}")

    buckets = _compare(ours, gt, keys, accepted)
    if not any(buckets[k] for k in ("zero_vs_blank", "accepted", "needs_review", "extra_keys")):
        print("    与权威导出**逐字段完全一致** ✅")
        return 0

    for key, a, b, _ in buckets["accepted"]:
        print(f"    · {key:<10} 我们={str(a)[:34]!r:<38} ERP={str(b)[:34]!r:<38}"
              f" 口径已定: {accepted[key]}")
    for key, a, b in buckets["zero_vs_blank"]:
        print(f"    · {key:<10} 我们={str(a)[:34]!r:<38} ERP={str(b)[:34]!r:<38}"
              f" 0 与空（业务确认可接受）")
    for key, a, b, _ in buckets["needs_review"]:
        print(f"    ⚠️ {key:<10} 我们={str(a)[:34]!r:<38} ERP={str(b)[:34]!r:<38}"
              f" **需要核对**")
    if buckets["extra_keys"]:
        print(f"    （ERP 导出里多出的键，非我方字段: {buckets['extra_keys']}）")
    return len(buckets["needs_review"])


def main() -> int:
    ap = argparse.ArgumentParser(description="与权威 ERP 导出逐字段对照")
    ap.add_argument("--customer", required=True, help="客户档案 id（如 ngv）")
    ap.add_argument("--attachment", help="客户附件路径（单份模式）")
    ap.add_argument("--erp", help="对应的 ERP 导出 JSON（单份模式）")
    ap.add_argument("--samples-dir", help="语料库目录（批量模式：按文件名里的单号配对 *.erp.json）")
    ap.add_argument("--accepted", help='口径白名单 JSON：{"字段": "为什么该不同"}')
    ap.add_argument("--map", action="append", default=[],
                    help="批量模式的补充配对：单号=附件文件名片段（可重复）")
    args = ap.parse_args()

    keys = _load_template_keys()
    accepted = dict(ALWAYS_ACCEPTED)
    if args.accepted and Path(args.accepted).exists():
        with open(args.accepted, encoding="utf-8") as f:
            accepted.update(json.load(f))

    workdir_base = Path(tempfile.mkdtemp(prefix="cqr-compare-"))
    pairs: list = []

    if args.samples_dir:
        samples = Path(args.samples_dir)
        fallback = dict(
            (m.split("=", 1)[0], m.split("=", 1)[1]) for m in args.map if "=" in m)
        for erp in sorted(samples.glob("*.erp.json")):
            order = erp.name.replace(".erp.json", "")
            cands = [p for p in samples.glob("*.xlsx") if order in p.name]
            if not cands and order in fallback:
                cands = [p for p in samples.glob("*.xlsx") if fallback[order] in p.name]
            if not cands:
                print(f"!! {order}: 找不到对应附件（可用 --map {order}=文件名片段 指定）")
                continue
            pairs.append((order, cands[0], erp))
    elif args.attachment and args.erp:
        attachment = Path(args.attachment)
        pairs.append((attachment.stem, attachment, Path(args.erp)))
    else:
        ap.error("要么给 --samples-dir，要么同时给 --attachment 与 --erp")

    review_total = 0
    for idx, (order, attachment, erp_path) in enumerate(pairs):
        run = _run_pipeline(args.customer, attachment, workdir_base / f"run{idx}")
        with open(erp_path, encoding="utf-8") as f:
            gt = json.load(f)
        review_total += _report(order, run["result"], gt, run["validation"], accepted, keys)

    print(f"\n{'=' * 78}")
    if review_total:
        print(f"⚠️ 有 {review_total} 处白名单外的差异需要人工核对（逐条都在上面）")
    else:
        print("✅ 所有差异都在已定口径内 —— 业务已拍板的口径全部生效")
    print(f"（本次运行的工作目录: {workdir_base}）")
    return 1 if review_total else 0


if __name__ == "__main__":
    sys.exit(main())
