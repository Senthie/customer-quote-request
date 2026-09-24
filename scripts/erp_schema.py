#!/usr/bin/env python3
"""ERP 表单权威字典 —— 取值归一化 + 校验。

数据源：assets/erp_form_schema.yaml（从真实 ERP 报价需求单表单转录）。

为什么需要这个模块
──────────────────
ERP 的下拉框只认固定枚举。客户文件里的写法五花八门（"无线胶装"、"骑马订"、
"Paperback"…），脚本或 agent 填错的值导入时会被静默丢弃或直接报错 ——
历史单据 0e39e2e2（2026-05-25）就是例子：ZDFS 填了「无线胶装」，
而 ERP 的合法值是「胶装/PAD书」。

对外接口
────────
    load_schema()                       -> dict
    field_spec(field)                   -> dict         字段定义
    legal_values(field)                 -> list[str]    合法取值（label 形式）
    normalize_value(field, value)       -> (value, status)
                                           status: ok / normalized / unknown / untouched
    normalize_result(result, logger)    -> (result, changes)
    validate_result(result)             -> list[{"field","level","message"}]

    # CLI
    uv run ./scripts/erp_schema.py                        # 打印字典概览
    uv run ./scripts/erp_schema.py --field ZDFS           # 看单个字段
    uv run ./scripts/erp_schema.py --check <erp.json>     # 校验一份结果 JSON
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Optional

try:
    import yaml
except ImportError:  # pragma: no cover
    print("需要 pyyaml：uv add pyyaml", file=sys.stderr)
    raise

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "assets" / "erp_form_schema.yaml"

_SCHEMA_CACHE: Optional[dict] = None


# ── 加载 ─────────────────────────────────────────────────────────────────
def load_schema(path: Optional[Path] = None) -> dict:
    global _SCHEMA_CACHE
    if path is None and _SCHEMA_CACHE is not None:
        return _SCHEMA_CACHE
    with open(path or SCHEMA_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    data.setdefault("fields", {})
    if path is None:
        _SCHEMA_CACHE = data
    return data


def field_spec(field: str) -> dict:
    return load_schema()["fields"].get(field) or {}


def _option_entries(spec: dict) -> list[dict]:
    """把 options 统一成 [{code, label, store}] 形式。"""
    out = []
    for opt in spec.get("options") or []:
        if isinstance(opt, dict):
            label = str(opt.get("label", "")).strip()
            code = str(opt.get("code", "")).strip()
            out.append({"code": code, "label": label, "store": opt.get("store") or spec.get("store", "label")})
        else:
            out.append({"code": "", "label": str(opt).strip(), "store": spec.get("store", "label")})
    return out


def legal_values(field: str) -> list[str]:
    """该字段的合法取值（label 形式，已 strip）。含树形下拉的顶层节点。"""
    values = [o["label"] for o in _option_entries(field_spec(field)) if o["label"]]
    values += [t["label"] for t in _tree_entries(field_spec(field)) if t["label"]]
    return values


def _observed_values(spec: dict) -> list[dict]:
    """「实测值」：不在表单静态 option 列表里，但在真实 ERP 导出里出现过的取值。

    字典是从表单 HTML 转录的；如果某个下拉由服务端 postback 动态填充，
    转录出的枚举就不完整（ZBLX 的 `B3B普通芯` 就是这种情况）。
    这类值校验时降级为 WARN（不静默放过、也不误报成代码缺陷），
    并要求业务确认后把值移进 options 或从档案里去掉。
    """
    out = []
    for item in spec.get("observed_values") or []:
        if isinstance(item, dict):
            out.append(item)
        else:
            out.append({"value": str(item)})
    return out


def _tree_entries(spec: dict) -> list[dict]:
    """树形下拉（如 CPLB 产品类别）：节点 = 编号 + 名称。

    优先读 `spec.tree_source` 指向的权威树（assets/cplb_tree.yaml，88 节点，
    每个子类带业务定义）；树文件缺失/损坏时退回 `spec.options_tree`（顶层兜底）。
    """
    src = spec.get("tree_source")
    if src:
        try:
            import cplb_tree

            path = Path(src)
            if not path.is_absolute():
                path = REPO_ROOT / src
            tree_nodes = cplb_tree.nodes(path)
            if tree_nodes:
                return [{"code": code, "label": node.get("name", ""),
                         "depth": node.get("depth")}
                        for code, node in sorted(tree_nodes.items())]
        except Exception:  # 树文件缺失或损坏 → 退回兜底
            pass

    out = []
    for node in spec.get("options_tree") or []:
        if isinstance(node, dict):
            out.append({"code": str(node.get("code", "")).strip(),
                        "label": str(node.get("label", "")).strip()})
    return out


def _is_select(spec: dict) -> bool:
    return bool(_option_entries(spec) or _tree_entries(spec))


# ── 归一化 ───────────────────────────────────────────────────────────────
def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return value.strip()
    return value


def normalize_value(field: str, value: Any) -> tuple[Any, str]:
    """把一个取值对齐到 ERP 合法值。

    返回 (新值, 状态)：
      untouched  —— 空值 / 非枚举字段，原样返回
      ok         —— 已经是合法值（可能只做了 strip）
      normalized —— 通过 code 或别名映射到了合法值
      unknown    —— 填了值但不在枚举里，也不是已知别名 → 原样保留 + 上层报警
    """
    spec = field_spec(field)
    if value is None:
        return value, "untouched"

    if isinstance(value, str):
        value = value.strip()
        if value == "":
            return "", "untouched"

    # 非枚举字段：只做类型上的宽容处理
    if not _is_select(spec):
        ftype = spec.get("type")
        if ftype == "checkbox" and isinstance(value, str):
            low = value.lower()
            if low in ("y", "yes", "true", "1", "是", "✓"):
                return True, "normalized"
            if low in ("n", "no", "false", "0", "否"):
                return False, "normalized"
        return value, "untouched"

    options = _option_entries(spec)
    tree = _tree_entries(spec)
    raw = str(value).strip()
    low = raw.lower()

    # 0. 树形下拉（CPLB 产品类别）：支持「23.01;无线胶装书」「23.01」「23;胶装书」「胶装书」
    #    四种写法，一律规范化成**树里的**「编号;名称」（名称以树为准，防手工写错名字）。
    if tree:
        by_code = {t["code"]: t for t in tree if t["code"]}

        def _resolve(code: str) -> Optional[dict]:
            """编号 → 树节点。兼容「23」这种只写顶层的写法（树里顶层是 23.00）。"""
            code = code.strip()
            if code in by_code:
                return by_code[code]
            if re.fullmatch(r"\d+", code) and f"{code}.00" in by_code:
                return by_code[f"{code}.00"]
            return None

        def _canonical(node: dict) -> str:
            return f"{node['code']};{node['label']}"

        if ";" in raw:
            node = _resolve(raw.partition(";")[0])
            if node:
                canonical = _canonical(node)
                return canonical, ("ok" if canonical == value else "normalized")
        node = _resolve(raw)
        if node:
            return _canonical(node), "normalized"
        for t in tree:
            if t["label"] and t["label"].lower() == low:
                return _canonical(t), "normalized"

    # 1. 直接命中 label（忽略大小写/首尾空白）
    for o in options:
        if o["label"].lower() == low:
            return o["label"], ("ok" if o["label"] == value else "normalized")

    # 2. 命中 code
    for o in options:
        if o["code"] and o["code"].lower() == low and o["label"]:
            return o["label"], "normalized"

    # 3. 别名表（子串包含，长别名优先 —— 避免 "perfect bound" 被 "perfect" 抢走）
    aliases = spec.get("aliases") or {}
    for alias in sorted(aliases, key=len, reverse=True):
        if str(alias).lower() in low:
            target = str(aliases[alias]).strip()
            # 别名可以指向 code（如 CPLB 的 "26;线圈书"），也指向 label
            for o in options:
                if target.lower() in (o["label"].lower(), o["code"].lower()):
                    return o["label"] or target, "normalized"
            return target, "normalized"

    return value, "unknown"


def normalize_result(result: dict, logger=None) -> tuple[dict, list[dict]]:
    """就地归一化整份结果。返回 (结果, 变更列表)。"""
    changes = []
    for field in list(result.keys()):
        if field.startswith("_"):
            continue
        old = result[field]
        new, status = normalize_value(field, old)
        if status == "normalized":
            changes.append({"field": field, "old": old, "new": new, "status": status})
            result[field] = new
            if logger is not None:
                logger.info(f"  [归一化] {field}: {old!r} → {new!r}（对齐 ERP 枚举）")
        elif status == "unknown":
            changes.append({"field": field, "old": old, "new": old, "status": status})
    return result, changes


# ── 校验 ─────────────────────────────────────────────────────────────────
def _type_issue(field: str, value: Any, ftype: str) -> Optional[str]:
    if ftype == "int":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"应为整数，实际是 {type(value).__name__}（{value!r}）"
    elif ftype == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"应为数值，实际是 {type(value).__name__}（{value!r}）"
    elif ftype == "checkbox":
        if not isinstance(value, bool):
            return f"应为布尔，实际是 {type(value).__name__}（{value!r}）"
    elif ftype == "date":
        import re
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value)):
            return f"应为 YYYY-MM-DD，实际是 {value!r}"
    return None


def declared_exceptions(entries) -> dict:
    """把档案 `rules.enum_exceptions` 归一成 {字段: {取值: 原因}}。

    用途（2026-09-24 新增）：业务拍板的口径有时**不在 ERP 下拉枚举里**
    （NGV 的 `CPSX: 无` —— 枚举里只有 三岁以上/三岁以下/其他/两个「要寄第三方测试」）。
    按字典硬报 error 会让**每一单**都红一次，把「0 error」这道闸门变成噪声、
    真缺陷反而被淹没；静默放过又是拿业务口径当没看见。
    所以走「档案显式声明 + 降级为 WARN + 带上原因」——可见、可查、不算缺陷。
    """
    out: dict = {}
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        field, value = entry.get("field"), entry.get("value")
        if not field or value is None:
            continue
        out.setdefault(str(field), {})[str(value).strip()] = str(entry.get("reason", "")).strip()
    return out


def validate_result(result: dict, allow_empty=(), allow_values=None,
                    internal_fields=()) -> list[dict]:
    """校验一份结果。返回问题列表（level: error / warn）。

    allow_empty: 允许为空的字段集合（客户档案 rules.required_empty）。
    ⚠️ 为什么需要它（2026-09-24）：KHDH「客户单号」在字典里是业务必填，
    但 NGV 的报价阶段**客户还没给单号** —— 4 份权威 ERP 导出里 KHDH 全是空的；
    套装单同样没有书脊方向（Drop_SJFX）。对这类客户，按字典硬报 error 是误报，
    会淹没真正的取值错误。豁免是**按档案声明**的，不是全局放宽。

    allow_values: {字段: {取值: 原因}}，客户档案 rules.enum_exceptions（见 declared_exceptions）。
    这些取值**不在 ERP 枚举里但业务已拍板**用 → 报 WARN 并带上原因，不报 error。

    internal_fields: 内部导出字段的键名（assets/internal_export_fields.yaml）。
    它们**不是 ERP 字段**（`ID` / `CPMC_ZH` 字典里都没有），追加在 59 键之后，
    不参与校验 —— 否则每份产物都会报「字典里没有这个字段」。
    """
    issues: list[dict] = []
    allow_empty = set(allow_empty or ())
    internal = {str(k) for k in (internal_fields or ())}

    for field, value in result.items():
        if field.startswith("_") or field in internal:
            continue
        spec = field_spec(field)

        if not spec:
            issues.append({"field": field, "level": "warn",
                           "message": "字典里没有这个字段 —— 确认它是否属于 ERP 导入模板"})
            continue

        empty = value == "" or value is None

        # 合法值校验（只对非空值）
        if not empty and _is_select(spec):
            tree = _tree_entries(spec)
            values = legal_values(field)
            text = str(value).strip()
            if tree:
                # 树形下拉：值形如「23.01;无线胶装书」—— 校验「编号」确实是树节点
                by_code = {t["code"] for t in tree if t["code"]}
                code = text.split(";")[0].strip()
                by_name = {str(t["label"]).strip().lower(): t["code"]
                           for t in tree if t.get("label")}
                top_code = f"{code}.00" if re.fullmatch(r"\d+", code) else code
                if ";" not in text and text.lower() in by_name:
                    # ★ 实测：真实 ERP 导出里 CPLB 存的是**纯名称**（"其它套装"），
                    #   而我们输出的是「编号;名称」。两种都可能被导入端接受，
                    #   口径未定 → 判 WARN 保留可见性，不当错误。
                    issues.append({
                        "field": field, "level": "warn",
                        "message": f"取值 {value!r} 是**纯名称**（无编号）—— 真实 ERP 导出"
                                   f"就是这种写法，而我们输出「编号;名称」"
                                   f"（如 {by_name[text.lower()]};{value}）。"
                                   f"导入端认哪种需业务确认；两种都能定位到同一节点。",
                    })
                elif top_code not in by_code:
                    tops = sorted(c for c in by_code if c.endswith(".00"))
                    issues.append({
                        "field": field, "level": "error",
                        "message": f"取值 {value!r} 的类别编号 {code!r} 不在 ERP 产品类别树里；"
                                   f"顶层编号：{' / '.join(t.split('.')[0] for t in tops)}",
                    })
                elif top_code.endswith(".00"):
                    issues.append({
                        "field": field, "level": "warn",
                        "message": f"取值 {value!r} 只定位到顶层类别 {top_code}，"
                                   f"建议具体到子类（如 {code}.01）—— 顶层节点过粗，生产/计价会不准",
                    })
            elif values and text not in values:
                declared = (allow_values or {}).get(field, {})
                observed = _observed_values(spec)
                hit = next((o for o in observed
                            if str(o.get("value", "")).strip().lower() == text.lower()), None)
                if text in declared:
                    # 档案声明的口径例外（reason 来自业务拍板）→ WARN + 原因
                    issues.append({
                        "field": field, "level": "warn",
                        "message": f"取值 {value!r} 是客户档案声明的口径（rules.enum_exceptions），"
                                   f"不在 ERP 下拉枚举里（枚举：{' / '.join(values)}）"
                                   f"—— {declared[text] or '档案未注明原因'}",
                    })
                elif hit:
                    # 实测值：不在表单静态枚举里，但真实 ERP 导出里出现过。
                    # 不能当 error（会让每次运行都报红、被当成代码缺陷），
                    # 也不能静默放过 —— 打 WARN 让这条待确认项一直可见。
                    issues.append({
                        "field": field, "level": "warn",
                        "message": f"取值 {value!r} 不在表单枚举里，但实测于 "
                                   f"{hit.get('seen_in') or '真实 ERP 导出'} —— 待业务确认："
                                   f"补进字典 options，还是改用已有值。"
                                   f"（表单枚举：{' / '.join(values)}）",
                    })
                else:
                    issues.append({
                        "field": field, "level": "error",
                        "message": f"取值 {value!r} 不在 ERP 合法枚举里；"
                                   f"合法值：{' / '.join(values)}",
                    })

        # 类型校验
        ftype = spec.get("type")
        if not empty and ftype:
            problem = _type_issue(field, value, ftype)
            if problem:
                issues.append({"field": field, "level": "error",
                               "message": f"{spec.get('label', field)} {problem}"})

        # 必填校验（allow_empty 里的字段按客户档案豁免 —— 见下方说明）
        required = spec.get("required")
        if empty and required and field not in allow_empty:
            issues.append({
                "field": field, "level": "error" if required == "error" else "warn",
                "message": f"{spec.get('label', field)} 为空"
                           + ("（业务必填）" if required == "error" else "（建议填写）"),
            })

    return issues


# ── CLI ──────────────────────────────────────────────────────────────────
def _cmd_overview() -> int:
    schema = load_schema()
    print(f"ERP 字段字典 v{schema.get('schema_version')}（{schema.get('captured_at')}）")
    print(f"来源: {schema.get('source')}")
    print(f"字段数: {len(schema['fields'])}，仅表单字段: {len(schema.get('form_only') or [])}")
    print()
    for field, spec in schema["fields"].items():
        n = len(legal_values(field))
        mark = f"（{n} 个合法值）" if n else ""
        auto = " [ERP自动带出]" if spec.get("auto_filled") else ""
        agent = " [agent手写]" if spec.get("agent_written") else ""
        print(f"  {field:12s} {spec.get('label', ''):14s} {spec.get('type', ''):9s}{mark}{auto}{agent}")
    return 0


def _cmd_field(field: str) -> int:
    spec = field_spec(field)
    if not spec:
        print(f"字典里没有字段 {field}", file=sys.stderr)
        return 1
    print(yaml.safe_dump({field: spec}, allow_unicode=True, sort_keys=False))
    return 0


def _cmd_check(path: str, customer: Optional[str] = None) -> int:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    allow_empty: tuple = ()
    allow_values: dict = {}
    internal: tuple = ()
    try:
        # 内部导出字段（ID / CPMC_ZH …）：不是 ERP 字段，不参与字典校验。
        # 无条件加载（与是否传 --customer 无关）—— 否则校验自己产出的文件时会误报
        # 「字典里没有这个字段」。
        import sys as _sys
        from pathlib import Path as _Path
        _sys.path.insert(0, str(_Path(__file__).resolve().parent))
        from internal_fields import internal_field_keys
        internal = tuple(internal_field_keys())
        if internal:
            print(f"内部导出字段（非 ERP 字段，跳过校验）: {list(internal)}")
    except Exception as exc:                          # 声明文件缺失不阻塞校验
        print(f"（内部字段声明未能加载，按无内部字段校验：{exc}）")
    if customer:
        # 按客户档案豁免「本客户本来就该为空」的字段（rules.required_empty），
        # 以及「业务拍板用、但不在 ERP 枚举里」的口径例外（rules.enum_exceptions）。
        # 不传客户时按字典硬校验 —— NGV 的输出会因此报 KHDH 为空、CPSX=无（假 error）。
        try:
            import sys as _sys
            from pathlib import Path as _Path
            _sys.path.insert(0, str(_Path(__file__).resolve().parent))
            from customer_profile import load_profile
            prof = load_profile(customer)
            allow_empty = tuple(prof.rule("required_empty", []) or [])
            allow_values = declared_exceptions(prof.rule("enum_exceptions", []))
            print(f"按客户档案豁免必填校验的字段（{customer}）: {list(allow_empty)}")
            if allow_values:
                print(f"按客户档案声明的口径例外（不在枚举里 → WARN）: "
                      f"{ {f: list(v) for f, v in allow_values.items()} }")
        except Exception as exc:                      # 档案缺失/加载失败不阻塞校验
            print(f"（客户档案 {customer} 未能加载，按字典硬校验：{exc}）")
    issues = validate_result(data, allow_empty=allow_empty, allow_values=allow_values,
                             internal_fields=internal)
    if not issues:
        print(f"✓ {path}：{len(data)} 个字段全部通过（枚举 / 类型 / 必填）")
        return 0
    errors = [i for i in issues if i["level"] == "error"]
    warns = [i for i in issues if i["level"] == "warn"]
    for i in errors:
        print(f"✗ [ERROR] {i['field']}: {i['message']}")
    for i in warns:
        print(f"! [WARN ] {i['field']}: {i['message']}")
    print(f"\n合计 {len(issues)} 个问题（error {len(errors)} / warn {len(warns)}）")
    return 1 if errors else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="ERP 表单权威字典（归一化 / 校验）")
    parser.add_argument("--field", help="查看单个字段的定义与合法取值")
    parser.add_argument("--check", metavar="ERP_JSON", help="校验一份 ERP 结果 JSON")
    parser.add_argument("--customer", help="配合 --check：按该客户档案豁免必填（rules.required_empty）"
                                           "与口径例外（rules.enum_exceptions）")
    parser.add_argument("--values", metavar="FIELD", help="只打印该字段的合法取值")
    args = parser.parse_args()

    if args.field:
        return _cmd_field(args.field)
    if args.values:
        for v in legal_values(args.values):
            print(v)
        return 0
    if args.check:
        return _cmd_check(args.check, args.customer)
    return _cmd_overview()


if __name__ == "__main__":
    sys.exit(main())
