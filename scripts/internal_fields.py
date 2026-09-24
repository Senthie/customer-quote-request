#!/usr/bin/env python3
"""
internal_fields.py - 内部导出字段（我方自用，不是 ERP 表单字段）

单一真源：`assets/internal_export_fields.yaml`

这些字段**追加在 59 个 ERP 键之后**，不参与 ERP 字典校验、不占 59 键的位置。
用途（业务 2026-09-24）：
  * `ID`      —— 标注本次执行的任务，方便「产物 ↔ 日志」对回去
  * `CPMC_ZH` —— 产品名称的中文表达（`CPMC` 只放英文表达）
  * `Reminder`—— 给业务的提醒（如报价截止日已过期）
  * `version` —— 当前配置的版本，方便技能迭代时辨认产物出自哪一代口径

用法：
    from internal_fields import load_internal_fields, internal_field_keys
    fields = load_internal_fields()          # [{key, label, source, notes}, …]
    keys = internal_field_keys()             # ["ID", "CPMC_ZH"]
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
INTERNAL_FIELDS_PATH = REPO_ROOT / "assets" / "internal_export_fields.yaml"


def load_internal_fields(path: Optional[Path] = None) -> list[dict[str, Any]]:
    """读内部字段声明（保持文件里的顺序 = 产物里的追加顺序）。"""
    src = Path(path) if path else INTERNAL_FIELDS_PATH
    if not src.exists():
        return []
    with open(src, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    fields = raw.get("fields") or []
    out: list[dict[str, Any]] = []
    for item in fields:
        if isinstance(item, dict) and item.get("key"):
            out.append(dict(item))
    return out


def internal_field_keys(path: Optional[Path] = None) -> list[str]:
    """内部字段的键名（顺序敏感）。"""
    return [str(f["key"]) for f in load_internal_fields(path)]


def is_internal_field(key: str, path: Optional[Path] = None) -> bool:
    return str(key) in internal_field_keys(path)


if __name__ == "__main__":
    import sys

    fields = load_internal_fields()
    if not fields:
        print(f"（没有内部字段声明：{INTERNAL_FIELDS_PATH}）")
        sys.exit(0)
    print(f"内部导出字段（{len(fields)} 个，追加在 59 个 ERP 键之后）：{INTERNAL_FIELDS_PATH}")
    for i, f in enumerate(fields, 1):
        print(f"  {i}. {f['key']:<10} {f.get('label', ''):<12} source={f.get('source', '')}")
