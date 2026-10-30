#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""migrate_erp_to_array.py —— 把历史产物（单对象）就地转成**数组**格式。

背景（2026-10-29 业务拍板）：导出统一为 `[{...}, {...}]`，一个报价文件可含多个产品。
2026-10-29 之前产出的 `ERP_*.json` 顶层是单个对象。读取侧已经向下兼容
（`erp_bundle.load_bundle`），但为了「导出格式一致、ERP 导入端只认一种」，
这里做一次性就地升级：

    uv run ./scripts/migrate_erp_to_array.py            # 预演（只打印，不写盘）
    uv run ./scripts/migrate_erp_to_array.py --apply    # 真正写盘

规则：
  * 顶层是对象 → 包成 `[obj]`；已经是数组 → 跳过（幂等，可反复跑）。
  * 默认跳过 `_superseded_*`（历史作废产物留证，不回炉）。
  * 改之前留一份 `<原文件>.prearray.bak`，出问题能退回。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ROOT = REPO_ROOT / "customer-files" / "processed"


def iter_erp_files(root: Path, include_superseded: bool = False):
    for p in sorted(root.rglob("ERP_*.json")):
        if not include_superseded and any(part.startswith("_superseded") for part in p.parts):
            continue
        yield p


def migrate_one(path: Path, apply: bool = False) -> str:
    """返回 'array'（已是数组）/ 'wrapped'（已转换）/ 'skip'（读不了）/ 'dry'（预演）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:                                     # noqa: BLE001
        return "skip"
    if isinstance(data, list):
        return "array"
    if not isinstance(data, dict):
        return "skip"
    if not apply:
        return "dry"
    shutil.copy2(path, path.with_suffix(path.suffix + ".prearray.bak"))
    path.write_text(json.dumps([data], ensure_ascii=False, indent=2), encoding="utf-8")
    return "wrapped"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="历史 ERP 产物 → 数组格式（一次性迁移）")
    ap.add_argument("--dir", default=str(DEFAULT_ROOT), help="扫描根目录（默认 customer-files/processed）")
    ap.add_argument("--apply", action="store_true", help="真正写盘（默认只预演）")
    ap.add_argument("--include-superseded", action="store_true", help="连 _superseded_* 一起转")
    args = ap.parse_args(argv)

    root = Path(args.dir)
    if not root.is_dir():
        print(f"目录不存在：{root}", file=sys.stderr)
        return 2

    counts = {"array": 0, "wrapped": 0, "dry": 0, "skip": 0}
    for path in iter_erp_files(root, args.include_superseded):
        state = migrate_one(path, apply=args.apply)
        counts[state] += 1
        if state in ("wrapped", "dry", "skip"):
            print(f"[{state}] {path.relative_to(root)}")

    print(f"\n已是数组 {counts['array']} 份 / "
          f"{'已转换' if args.apply else '待转换'} {counts['wrapped'] or counts['dry']} 份 / "
          f"读不了 {counts['skip']} 份")
    if not args.apply and counts["dry"]:
        print("（预演模式：加 --apply 才会写盘）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
