#!/usr/bin/env python3
"""
erp_import_adapter.py - 把我们产出的 59 键 JSON 翻成 ERP 导入端的口径

业务背景（2026-11-11 森西拍板）：
  产品里存的是**内部表达**（布尔、刻意留空的字段），ERP 导入端要的是它自己能存的
  样子（文本 `是`/`否`、整数、null）。两边差三处类型，**不在产物里改**（那会毁掉已
  拍板的口径、让历史产物不可比），统一在「导出给 ERP」这一层翻译掉。

  翻译表：assets/erp_import_translations.yaml（每条规则都带证据与理由）
  字段顺序与类型：assets/add_customer_quote_request.json + assets/erp_form_schema.yaml
  内部字段：assets/internal_export_fields.yaml（导出时一律剥掉）

用法
    # 生成导入文件（默认写在同目录 <stem>.erp-import.json）
    uv run ./scripts/erp_import_adapter.py customer-files/processed/<task>/ERP_xxx.json

    # 只看看会改什么，不落盘（退出码 0 = 结构没问题）
    uv run ./scripts/erp_import_adapter.py <in.json> --check

    # 指定输出路径
    uv run ./scripts/erp_import_adapter.py <in.json> -o /tmp/out.json

退出码：0 成功 / 2 结构问题（键集或顺序不符、未知键、规则表读不动）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

TEMPLATE_PATH = REPO_ROOT / "assets" / "add_customer_quote_request.json"
TRANSLATIONS_PATH = REPO_ROOT / "assets" / "erp_import_translations.yaml"

HERE = Path(__file__).resolve().parent


# ── 读资源 ────────────────────────────────────────────────────────────────
def load_template() -> dict[str, Any]:
    """ERP 导入模板（59 键，键序 = 导入顺序）。"""
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_rules(path: Optional[Path] = None) -> dict[str, Any]:
    src = Path(path) if path else TRANSLATIONS_PATH
    with open(src, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def internal_keys() -> list[str]:
    from internal_fields import internal_field_keys

    return internal_field_keys()


def _schema_type(field: str) -> Optional[str]:
    """查字典里的字段类型（查不到 → None，不做类型兜底）。"""
    try:
        from erp_schema import field_spec
    except Exception:  # pragma: no cover - 字典缺失时降级
        return None
    try:
        return (field_spec(field) or {}).get("type")
    except Exception:  # pragma: no cover
        return None


# ── 翻译 ──────────────────────────────────────────────────────────────────
def _is_empty(value: Any) -> bool:
    return value is None or value == ""


def translate_value(field: str, value: Any, rules: dict[str, Any]) -> tuple[Any, str]:
    """翻译单个字段的值。返回 (新值, 变更说明)；说明为空表示没改。"""
    spec = (rules.get("fields") or {}).get(field)
    if spec:
        kind = spec.get("type")
        if kind == "bool_to_label":
            mapping = spec.get("mapping") or {}
            if isinstance(value, bool):
                return mapping.get(bool(value), value), f"{field}: {value} → {mapping.get(bool(value), value)!r}"
            if _is_empty(value):
                return spec.get("empty", ""), f"{field}: {value!r} → 空"
            # 已经是文本（历史产物 / 人工改过）→ 原样放行
            return value, ""
        if kind == "empty_to":
            if value in (spec.get("values") or []):
                target = spec.get("to")
                if target == value:
                    return value, ""      # 已经是目标值 → 不算变更（幂等）
                return target, f"{field}: {value!r} → {target!r}"
            return value, ""
        if kind == "label_to_bool":
            mapping = spec.get("mapping") or {}
            if isinstance(value, str) and value.strip() in mapping:
                return mapping[value.strip()], f"{field}: {value!r} → {mapping[value.strip()]!r}"
            return value, ""

    # 兜底：字典里声明为 int/number 的字段，空串 → null（ERP 未填就是 null）
    # 注意只翻 ""，已经是 None 的不要再报一次变更（否则不幂等，--check 会刷重复行）
    if value == "" and _schema_type(field) in ("int", "number"):
        return None, f"{field}: {value!r} → None（整数字段空值）"
    return value, ""


def translate_record(record: dict[str, Any], template: dict[str, Any],
                     rules: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """把一条记录翻成导入口径。返回 (新记录, 变更列表)；键序与模板一致。"""
    out: dict[str, Any] = {}
    changes: list[str] = []
    for key in template:
        if key not in record:
            raise KeyError(f"记录缺字段 {key}（不是 59 键产物？）")
        new_value, note = translate_value(key, record[key], rules)
        out[key] = new_value
        if note:
            changes.append(note)
    unknown = [k for k in record if k not in template and k not in set(internal_keys())]
    if unknown:
        raise KeyError(f"记录里有模板之外、也不是内部字段的键：{unknown}")
    return out, changes


def translate_bundle(records: list[dict[str, Any]], template: Optional[dict[str, Any]] = None,
                     rules: Optional[dict[str, Any]] = None) -> tuple[list[dict[str, Any]], list[str]]:
    tpl = template if template is not None else load_template()
    rl = rules if rules is not None else load_rules()
    out: list[dict[str, Any]] = []
    changes: list[str] = []
    for i, rec in enumerate(records, 1):
        new, ch = translate_record(rec, tpl, rl)
        out.append(new)
        changes += [f"[{i}] {c}" for c in ch]
    return out, changes


def load_bundle(path: str | Path) -> list[dict[str, Any]]:
    """读产物：数组（v2 起）或者老的单个对象。"""
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return raw if isinstance(raw, list) else [raw]


# ── CLI ───────────────────────────────────────────────────────────────────
def default_output(src: Path) -> Path:
    return src.with_name(src.stem + ".erp-import.json")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="59 键产物 → ERP 导入口径（剥内部字段 + 类型翻译）")
    ap.add_argument("src", help="产物 JSON（数组或单对象）")
    ap.add_argument("-o", "--out", help="输出路径（默认 <stem>.erp-import.json）")
    ap.add_argument("--check", action="store_true", help="只报会改什么，不落盘")
    ap.add_argument("--json", action="store_true", help="变更以 JSON 输出（给脚本消费）")
    args = ap.parse_args(argv)

    src = Path(args.src)
    if not src.exists():
        print(f"❌ 找不到文件：{src}", file=sys.stderr)
        return 2
    try:
        records = load_bundle(src)
        tpl = load_template()
        rules = load_rules()
        out, changes = translate_bundle(records, tpl, rules)
    except KeyError as e:
        print(f"❌ 结构问题：{e}", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001
        print(f"❌ {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({"records": len(records), "changes": changes}, ensure_ascii=False, indent=2))
    else:
        print(f"源文件：{src}（{len(records)} 条记录）")
        print(f"模板：{TEMPLATE_PATH.name}（{len(tpl)} 键，顺序已对齐）")
        if changes:
            print(f"翻译 {len(changes)} 处：")
            for c in changes:
                print(f"  · {c}")
        else:
            print("无需翻译（已经是导入口径）")

    if args.check:
        print("（--check：没有写文件）")
        return 0

    dest = Path(args.out) if args.out else default_output(src)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ 已写出：{dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
