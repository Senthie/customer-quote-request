#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""erp_bundle.py —— ERP 导出文件的「容器格式」读写（数组）。

★ 2026-10-29 业务拍板（森西）：**一个报价文件里可以有多个产品**。
原来「一个产品 = 一份 ERP JSON」的写法，多产品询价要开很多单、很多文件，下游难用。
现在统一成**数组**：

    [
      { "BDBH": "", ... 59 个 ERP 键 ..., "ID": "...", "CPMC_ZH": "...", ... },
      { ... 第二个产品 ... }
    ]

规则：
  * **永远是数组**，哪怕只有 1 个产品（`[{...}]`）—— 下游不用先判类型。
  * 数组里每个对象 = 一条完整的产品记录（59 个 ERP 键 + 内部字段），键序与模板一致。
  * **向下兼容读取**：老的单对象文件（`{...}`）照样能读进来，按「1 条记录」处理。
    写盘只写数组（老格式不再产出）。

用法：

    from erp_bundle import load_bundle, load_bundle_text, wrap

    records = load_bundle(Path("processed/<task>/ERP_xxx.json"))   # → list[dict]
    for i, rec in enumerate(records, 1):
        print(i, rec["CPLB"], rec["CPMS"])

命令行自检：

    uv run ./scripts/erp_bundle.py <ERP_JSON>      # 打印记录数与每条的关键字段
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable


def wrap(record: dict) -> list:
    """单条记录 → 数组（写盘前用）。"""
    return [record]


def _coerce(data: Any, *, where: str = "") -> list:
    """把「数组 / 单对象 / 带 records 的对象」统一成 list[dict]。"""
    if isinstance(data, list):
        records = data
    elif isinstance(data, dict):
        # 兼容可能的历史包装层：{"records": [...]}
        inner = data.get("records")
        if isinstance(inner, list) and all(isinstance(r, dict) for r in inner):
            records = inner
        else:
            records = [data]
    else:
        raise ValueError(f"{where}: ERP 导出必须是数组（或单个对象），实际是 {type(data).__name__}")
    for i, rec in enumerate(records, 1):
        if not isinstance(rec, dict):
            raise ValueError(f"{where}: 第 {i} 条不是对象，实际是 {type(rec).__name__}")
    return records


def load_bundle_text(text: str, *, where: str = "") -> list:
    """从 JSON 文本读一组产品记录（容忍 BOM）。"""
    return _coerce(json.loads(text.lstrip("\ufeff")), where=where)


def load_bundle(path: str | Path) -> list:
    """从文件读一组产品记录（容忍 BOM）。"""
    p = Path(path)
    return load_bundle_text(p.read_text(encoding="utf-8-sig"), where=str(p))


def write_bundle(path: str | Path, records: Iterable[dict]) -> Path:
    """把一组产品记录写成数组文件。"""
    p = Path(path)
    p.write_text(json.dumps(list(records), ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def main(argv: list[str] | None = None) -> int:
    import sys

    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 2
    total = 0
    for arg in argv:
        records = load_bundle(arg)
        total += len(records)
        print(f"{arg}: {len(records)} 条产品记录")
        for i, rec in enumerate(records, 1):
            print(f"  [{i}] CPLB={rec.get('CPLB')!r} CPMC={rec.get('CPMC')!r} "
                  f"CPMS={'有' if str(rec.get('CPMS') or '').strip() else '空'} "
                  f"ID={rec.get('ID')!r}")
    return 0 if total else 1


if __name__ == "__main__":
    raise SystemExit(main())
