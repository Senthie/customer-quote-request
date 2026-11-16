#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""erp_mcp_export.py —— 把内部 59 键产物翻成 **`erp_quote_create` 的入参**。

2026-11-12 森西拍板：「导出的 ERP 格式就改成 MCP input 格式，字段名、格式一致，
导出只要一份文件」。所以导出产物本身就是一次 MCP 工具调用的 `arguments`：

```json
[
  {
    "customer": "NGV10",
    "remark": "LG26090401 / Wimmelbücher (Winter)",
    "products": [
      {
        "name": "冬季找找图画书",
        "spec": "240*330*0",
        "qty": 410000,
        "fields": { "binding": "快线BB书", "cases": 10, "...": "..." }
      }
    ]
  }
]
```

* 一个元素 = 一次 `erp_quote_create`（ERP 一次只收 1 条明细，N 条产品就 N 个元素）。
* 键名/枚举一律用协议里的 **MCP 字段名**（`binding` / `packing` / `qty_1` …），
  与 `erp_quote_product_form` 的 `upload.json_schema` 一一对应；
  不再出现 `ZDFS` / `BZFS` 这类 ERP 控件名，也不用再维护第二份映射。
* 里面**只有** MCP 参数，没有任何内部字段（ID/version/question_answers/Reminder…），
  所以可以直接拿去调工具，或喂给 `scripts/erp_push.py`。

协议依据：`docs/erp-quote-create.md`；口径与坑：`docs/erp-push-字段口径.md`。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

CPMC_PLACEHOLDER = "【中文品名待定】"

# CPMS 概要行：ERP 页面自己会用「书籍方向 / 产品规格 / 款数」生成这一行。
CPMS_HEADER_PREFIXES = ("书籍方向：", "产品规格：")

# ★ 入参类型以服务端 pydantic 模型为准（`erp_quote_create.inputSchema` 就是它生成的）。
#   存一份在 assets 里当"单一事实源"，改协议时重抓一次即可，
#   **不要把类型手抄成第二份会漂移的常量**。
SCHEMA_PATH = Path(__file__).resolve().parent.parent / "assets" / "erp_quote_create_schema.json"


def _load_schema() -> dict[str, Any]:
    try:
        return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))["inputSchema"]
    except Exception:  # noqa: BLE001 - 缺文件时退化成"不校验类型"，不该拖垮导出
        return {}


_SCHEMA = _load_schema()


def _type_of(node: dict[str, Any]) -> str:
    """`anyOf: [{type: number}, {type: null}]` → `number`。"""
    if "type" in node:
        return str(node["type"])
    for branch in node.get("anyOf") or []:
        if branch.get("type") and branch["type"] != "null":
            return str(branch["type"])
    return "string"


# 明细 51 字段 → 期望类型（number / boolean / string）
FIELD_TYPES: dict[str, str] = {
    name: _type_of(node)
    for name, node in ((_SCHEMA.get("$defs", {}).get("QuoteProductFields", {})
                        .get("properties") or {}).items())
}
# 表头参数类型
HEADER_TYPES: dict[str, str] = {
    name: _type_of(node)
    for name, node in ((_SCHEMA.get("properties") or {}).items())
}
# `products[]` 除 `fields` 外的三个必填 + 可选 product_category
_PRODUCT_INPUT_PROPS = ((_SCHEMA.get("$defs", {}).get("QuoteProductInput", {})
                         .get("properties") or {}))
PRODUCT_INPUT_TYPES: dict[str, str] = {
    name: _type_of(node)
    for name, node in _PRODUCT_INPUT_PROPS.items()
    if name != "fields"
}
NUMERIC_FIELDS = {k for k, v in FIELD_TYPES.items() if v == "number"}
BOOLEAN_FIELDS = {k for k, v in FIELD_TYPES.items() if v == "boolean"}
STRING_FIELDS = {k for k, v in FIELD_TYPES.items() if v == "string"}

# 59 键 → erp_quote_create 明细 `fields`（协议 §3.3：51 个「首屏可见且可写」字段）
PRODUCT_FIELD_MAP: dict[str, str] = {
    "CPMC": "product_name",
    "DWMC": "unit",
    "KHDH": "customer_order_no",      # ★ 客户单号是**明细级**（txtKHCPMC），不在表头
    "ECPMC": "product_name_en",
    "CPDL": "product_group",
    "CD": "length_mm",
    "KD": "width_mm",
    "GD": "height_mm",
    "Drop_SJFX": "spine_direction",
    "PXH": "sort_no",
    "ZDFS": "binding",
    "BZFS": "packing",
    "TBLX": "pallet",
    "SHFS": "delivery",
    "YSLX": "transport_type",
    "SHDQ": "delivery_area",          # ⚠ 实测不落库（级联下拉，服务端空）
    "ZXLB": "carton_type",
    "ZBLX": "board_type",
    "ZXFS": "packing_orientation",
    "DKMC": "bottom_card",
    "chkBSYTB": "no_pallet",
    "chkBSYJZX": "no_container",
    "JHRQ": "delivery_date",
    "WCRQ": "expected_date",
    "chkZDBJF": "has_schedule",
    "CPSX": "attribute",
    "CPLX": "product_type",
    "FSCMC": "fsc_category",
    "SFYPJ": "has_parts",
    "chkXBJF": "no_quote",
    "CPMS": "product_desc",
    "BGMS": "extra_desc",
    "Remark1": "parts_desc",
    "Remark3": "modify_log",
    "Remark4": "inspection",
    "OLD_KHDDH": "old_hk_order_no",
    "BZ": "target_price_currency",
    "KHMBDJ": "target_price",
    "TZS": "cases",
    "YYS": "languages",
}

# `CPSL1..10` → `qty_1..qty_10`（报价数量档位；产物里存**单款**，落库要 × 款数）
QTY_SLOT_MAP: dict[str, str] = {f"CPSL{i}": f"qty_{i}" for i in range(1, 11)}

# 59 键 → 表头入参
HEADER_ARG_MAP: dict[str, str] = {
    "LXR": "contact",
    "LXDH": "phone",
    "SJ": "mobile",
    "CZ": "fax",
    "ZKL": "discount",
    "CPMX": "product_memo",
}

# ★ ERP 服务端 14 个必填（协议 §3.4）。缺了 → 主表建了、明细被拒 = 留一条空单。
MANDATORY_FIELDS: dict[str, str] = {
    "binding": "ZDFS", "cases": "TZS", "languages": "YYS", "packing": "BZFS",
    "pallet": "TBLX", "delivery": "SHFS", "delivery_date": "JHRQ",
    "parts_desc": "Remark1", "attribute": "CPSX", "product_type": "CPLX",
    "expected_date": "WCRQ", "product_desc": "CPMS", "modify_log": "Remark3",
    "inspection": "Remark4",
}

# ★ 业务默认值（2026-11-12 森西拍板）：ERP 必填、客户没给 → 兜底。
FIELD_DEFAULTS: dict[str, tuple[Any, str]] = {
    "CPSX": ("三岁以下", "客户未指定产品属性 → 业务口径默认三岁以下"),
    "CPLX": ("新产品", "新询价默认按新产品报"),
    "Remark1": ("无", "无配件"),
    "Remark3": ("无", "无修改记录"),
}

# 装订方式按产品类别推导（产物 CPLB 形如 `27.01;普通BB书`）
BINDING_BY_CATEGORY: tuple[tuple[str, str], ...] = (("BB", "快线BB书"),)

# 59 键里**确实**写不进的 4 个（2026-11-12 按 51 字段协议核对）
NOT_WRITABLE = ("BDBH", "CPLB", "KHDDH", "KHDQ")


# ─────────────────────────── 取值 / 归一 ───────────────────────────
def _norm_value(key: str, val: Any) -> Any:
    """产物内部表达 → MCP 能接受的样子（下拉一律传显示文字）。

    `SFYPJ`（是否有配件）产物里两种写法都出现过：布尔 `False`，或已经是 `"是"/"否"`。
    **不能只判真假**——`"否"` 是非空字符串，会被判成 `True` 直接写反。
    """
    if key == "SFYPJ":
        if isinstance(val, bool):
            return "是" if val else "否"
        text = str(val).strip()
        if text in ("是", "否"):
            return text
        return "是" if text else "否"
    return val


def strip_cpms_header(text: Any) -> Any:
    """把 CPMS 开头的**概要行**剥掉 —— 这行 ERP 会自己生成，我们不能再写。

    2026-11-16 实测（BJXQ261113003 / BJXQ261113004）：我们只发了一份概要行，
    ERP 的需求录入页却按 `书籍方向 / 产品规格 / 款数` 再生成一行，存下来是两份。
    历史语料 18395 份单里 **18348 份（99.74%）概要行只出现一次** —— 说明这行
    本来就不该由录入方提供文本，靠字段渲染即可。

    只剥**开头连续**的概要行（含紧跟的空行）；正文中间出现同样的字不复原不动。
    剥完若什么都不剩（整段 CPMS 只有概要行），原样返回 —— `product_desc` 是
    ERP 必填，宁可重复一行也不要把字段清空。
    """
    if not isinstance(text, str) or not text.strip():
        return text
    lines = text.split("\n")
    idx = 0
    removed = False
    while idx < len(lines):
        current = lines[idx].strip()
        if not current:
            break
        is_header = (current.startswith("书籍方向：")
                     or (current.startswith("产品规格：") and "【" in current))
        if not is_header:
            break
        removed = True
        idx += 1
        while idx < len(lines) and not lines[idx].strip():   # 吃掉概要行下面的空行
            idx += 1
    if not removed:
        return text
    stripped = "\n".join(lines[idx:])
    return stripped if stripped.strip() else text


def _coerce_fields(fields: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """按服务端 pydantic 类型把值掰正 —— `number` 不能是 "240"，`boolean` 不能是 "否"。

    返回 (掰过的 fields, 问题列表)。掰不动的不塞进去，宁可少一个字段也不要送错类型
    （pydantic 会直接 422，整单连主表都进不去）。
    """
    out: dict[str, Any] = {}
    problems: list[str] = []
    for key, val in fields.items():
        want = FIELD_TYPES.get(key, "string")
        if want == "number":
            if isinstance(val, bool):
                problems.append(f"{key} 应为数字，拿到布尔 {val!r}")
                continue
            if isinstance(val, (int, float)):
                out[key] = int(val) if float(val).is_integer() else float(val)
                continue
            text = str(val).strip().replace(",", "").replace("，", "")
            try:
                num = float(text)
            except (TypeError, ValueError):
                problems.append(f"{key} 应为数字，拿到 {val!r}")
                continue
            out[key] = int(num) if num.is_integer() else num
        elif want == "boolean":
            if isinstance(val, bool):
                out[key] = val
            elif str(val).strip().lower() in ("true", "1", "是", "y", "yes", "on"):
                out[key] = True
            elif str(val).strip().lower() in ("false", "0", "否", "n", "no", "off", ""):
                out[key] = False
            else:
                problems.append(f"{key} 应为布尔，拿到 {val!r}")
        else:
            out[key] = str(val)
    return out, problems


def validate_call(call: dict[str, Any]) -> list[str]:
    """离线按 `erp_quote_create` 的 pydantic schema 校验一份入参。"""
    problems: list[str] = []
    if not isinstance(call.get("products"), list) or not call["products"]:
        return ["products 缺失或为空（schema 里它必填，minItems=1）"]
    if len(call["products"]) > 1:
        problems.append(f"products 有 {len(call['products'])} 条，ERP 一次只收 1 条")
    unknown_header = set(call) - {"products", "confirm"} - set(HEADER_TYPES)
    if HEADER_TYPES and unknown_header:
        problems.append(f"表头出现 schema 里没有的参数：{sorted(unknown_header)}")
    for idx, product in enumerate(call["products"], 1):
        prefix = f"第{idx}条：" if len(call["products"]) > 1 else ""
        for required in ("name", "spec", "qty"):
            if not product.get(required):
                problems.append(prefix + f"{required} 必填（schema required）")
        qty = product.get("qty")
        if qty is not None and (isinstance(qty, bool) or not isinstance(qty, (int, float))
                                or qty <= 0):
            problems.append(prefix + f"qty 必须 > 0 的数字，实际 {qty!r}")
        fields = product.get("fields") or {}
        unknown = set(fields) - set(FIELD_TYPES) if FIELD_TYPES else set()
        if unknown:
            problems.append(prefix + f"fields 里有 schema 不认识的键：{sorted(unknown)}")
        for key, val in fields.items():
            want = FIELD_TYPES.get(key, "string")
            if want == "number" and (isinstance(val, bool) or not isinstance(val, (int, float))):
                problems.append(prefix + f"{key} 类型应为 number，实际 {type(val).__name__}")
            if want == "boolean" and not isinstance(val, bool):
                problems.append(prefix + f"{key} 类型应为 boolean，实际 {type(val).__name__}")
    return problems


def _record_qty(record: dict[str, Any], override: Optional[int]) -> Optional[int]:
    """**单款本数**（业务口径）。`CPSL1..10` 每档存总量，这里取单款，落库前乘款数。"""
    if override:
        return int(override)
    for i in range(1, 11):
        val = record.get(f"CPSL{i}")
        if isinstance(val, (int, float)) and not isinstance(val, bool) and val:
            return int(val)
    return None


def _record_titles(record: dict[str, Any]) -> int:
    """款数（`TZS`）。单款 × 款数 = 报价档位总量，ERP 不替我们乘。"""
    try:
        n = int(record.get("TZS") or 1)
    except (TypeError, ValueError):
        return 1
    return n if n > 0 else 1


def _record_total(record: dict[str, Any], override: Optional[int]) -> Optional[int]:
    per = _record_qty(record, override)
    return per * _record_titles(record) if per else None


def _date_fallback(default_date: Any, key: str) -> str:
    """兜底日期解析：给字符串 → JHRQ/WCRQ 同值；给 dict → 按字段各取各的。

    为什么要支持 dict：NGV 这类客户**交货日期和预计下单日期不是同一天**
    （客户只给 `Lieferung Q4 27`，预计下单根本没给），同值兜底会把一个日期
    硬塞进另一个字段。产物里这两个字段按客户档案「报价阶段不落库」保持空，
    只在**推送那一刻**补 ERP 强制必填的值 —— 所以兜底值只能从外部传进来，
    不能写回产物。
    """
    if isinstance(default_date, dict):
        return str(default_date.get(key) or "").strip()
    return str(default_date or "").strip()


def apply_defaults(record: dict[str, Any], fields: dict[str, Any]
                   ) -> dict[str, tuple[Any, str]]:
    """客户没给、按业务口径兜底的必填字段 → `{mcp_arg: (value, reason)}`。"""
    out: dict[str, tuple[Any, str]] = {}
    if "binding" not in fields:
        cplb = str(record.get("CPLB") or "")
        for token, val in BINDING_BY_CATEGORY:
            if token in cplb:
                out["binding"] = (val, f"产品类别含「{token}」→ 装订方式取 {val}")
                break
    for key, (val, reason) in FIELD_DEFAULTS.items():
        arg = PRODUCT_FIELD_MAP.get(key)
        if arg and arg not in fields:
            out[arg] = (val, reason)
    return out


# ─────────────────────────── 59 键 → MCP 入参 ───────────────────────────
def build_call(
    record: dict[str, Any],
    *,
    customer: str,
    qty: Optional[int],
    default_date: Any = "",
) -> tuple[dict[str, Any], list[str]]:
    """一条产品记录 → 一次 `erp_quote_create` 的入参。"""
    problems: list[str] = []
    name = str(record.get("CPMC") or record.get("CPMC_ZH") or "").strip()
    if not name or name == CPMC_PLACEHOLDER:
        problems.append("CPMC 还是占位符/空 → 先写中文品名再推")

    cd, kd, gd = record.get("CD"), record.get("KD"), record.get("GD")
    spec = f"{cd}*{kd}*{gd or 0}" if cd not in (None, "") and kd not in (None, "") else ""

    qty = _record_total(record, qty)
    if not qty:
        problems.append("数量未知（CPSL1-10 全空/为 0）→ 用 qty 指定单款本数（会乘款数）")

    product: dict[str, Any] = {"name": name, "spec": spec, "qty": qty or 0}

    fields: dict[str, Any] = {}
    for key, arg in PRODUCT_FIELD_MAP.items():
        val = record.get(key)
        if val in (None, "", []):
            continue
        fields[arg] = _norm_value(key, val)

    # ★ 概要行剥掉：ERP 会按 书籍方向/产品规格/款数 自己生成一行，我们写了就重复
    if "product_desc" in fields:
        fields["product_desc"] = strip_cpms_header(fields["product_desc"])

    # 数量档位：**只有第 2 档起**才显式传 qty_n（每档都是总量 = 单款 × 款数）。
    # ★ `qty_1` 绝不能显式传：`create()` 只在没给 qty_1 时才补隐藏的 `txtCPSL`，
    #   给了就只写展示框、汇总为空 →「请输入【产品数量】」→ 明细行存不进去
    #   （2026-11-12 实测，BJXQ261112007 就是这样废掉的）。
    titles = _record_titles(record)
    for key, arg in QTY_SLOT_MAP.items():
        if arg == "qty_1":
            continue
        val = record.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool) and val:
            fields[arg] = int(val) * titles

    # ★ 兜底：缺日期用 default_date（字符串 → JHRQ/WCRQ 同值；dict → 各取各的），
    #   缺必填用 FIELD_DEFAULTS
    for key in ("JHRQ", "WCRQ"):
        if str(record.get(key) or "").strip():
            continue
        fallback = _date_fallback(default_date, key)
        if fallback:
            fields[PRODUCT_FIELD_MAP[key]] = fallback
    for arg, (val, _reason) in apply_defaults(record, fields).items():
        fields[arg] = val

    # ★ 类型按服务端 pydantic 掰正（`number` 不接受 "240"、`boolean` 不接受 "否"）
    fields, type_problems = _coerce_fields(fields)
    problems.extend(type_problems)

    product["fields"] = fields

    # ★ 产品档案搜不到时的**唯一出路**（2026-11-12 协议新增）：
    #   `product_category` 给产品类别即可当新产品建明细，不要求档案存在。
    #   我们的 `CPLB` 形如 `27.01;普通BB书`，正是它接受的写法。
    category = str(record.get("CPLB") or "").strip()
    if category:
        product["product_category"] = category

    missing = [f"{arg}({src})" for arg, src in MANDATORY_FIELDS.items()
               if arg not in fields]
    if missing:
        problems.append("ERP 必填缺失（create 会留下空主表）：" + "、".join(missing))

    khddh = str(record.get("KHDDH") or "").strip()
    ecpmc = str(record.get("ECPMC") or "").strip()
    remark = str(record.get("Remark") or "").strip()
    if not remark:
        # 「香港单号 / 原始品名」当追溯串：ERP 里一眼认出是哪条询价
        remark = " / ".join(x for x in (khddh, ecpmc) if x)

    payload: dict[str, Any] = {
        "customer": customer,
        "products": [product],
        "remark": remark[:500],
    }
    for key, arg in HEADER_ARG_MAP.items():
        val = record.get(key)
        if val not in (None, ""):
            payload[arg] = str(val)
    # ★ YWY/GDY 故意不传：产物里是生成标记 `KM`（不是 ERP 真人），传了被拒；
    #   ERP 会按客户资料自动带出业务员/跟单员（实测 NGV10 → 陈纯 / 肖也）。
    return payload, problems


def build_calls(
    records: list[dict[str, Any]],
    *,
    customer: str = "",
    qty: Optional[int] = None,
    default_date: Any = "",
) -> tuple[list[dict[str, Any]], list[str]]:
    """整份产物 → N 次 create 的入参（N = 产品记录数）。"""
    if not records:
        return [], ["产物里没有记录"]
    problems: list[str] = []
    code = (customer or str(records[0].get("KHZL") or "").strip())
    if not code:
        problems.append("客户代码未知：产物 KHZL 为空 → 调用方需用客户档案的 erp_khzl 补上")

    calls: list[dict[str, Any]] = []
    for idx, rec in enumerate(records, 1):
        payload, probs = build_call(rec, customer=code, qty=qty,
                                    default_date=default_date)
        if len(records) > 1:
            probs = [f"第{idx}条：{p}" for p in probs]
        problems.extend(probs)
        calls.append(payload)
    return calls, problems


def defaulted_fields(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """这单有哪些字段是**按业务口径兜的**（不是客户给的）——给业务签字看。"""
    out: list[dict[str, Any]] = []
    for idx, rec in enumerate(records, 1):
        fields: dict[str, Any] = {}
        for key, arg in PRODUCT_FIELD_MAP.items():
            val = rec.get(key)
            if val not in (None, "", []):
                fields[arg] = _norm_value(key, val)
        for arg, (val, reason) in apply_defaults(rec, fields).items():
            out.append({"record": idx, "erp_field": arg, "value": val, "reason": reason})
    return out


def _is_emptyish(key: str, val: Any) -> bool:
    """`GD=0` 是有意义的 0 要报；`CPSL2=0` / `chkXBJF=False` 这种默认值只是噪音。"""
    if val is None or val == "" or val == []:
        return True
    if isinstance(val, bool):
        return not val
    if isinstance(val, (int, float)) and not val:
        return key.startswith("CPSL") or key in ("KHMBDJ", "BZ")
    return False


def unwritable_fields(records: list[dict[str, Any]]) -> dict[str, str]:
    """这单有哪些字段**create 写不进去**（要给业务签字确认的清单）。"""
    head = records[0] if records else {}
    out: dict[str, str] = {}
    for key in NOT_WRITABLE:
        val = head.get(key)
        if _is_emptyish(key, val):
            continue
        out[key] = str(val)
    return out


# ─────────────────────────── 格式判定 ───────────────────────────
def is_mcp_payload(data: Any) -> bool:
    """这份 JSON 是不是**已经是** MCP 入参（数组，每个元素带 products）。"""
    if not isinstance(data, list) or not data:
        return False
    return all(isinstance(x, dict) and isinstance(x.get("products"), list) for x in data)


def export_calls(
    records: list[dict[str, Any]],
    *,
    customer: str,
    qty: Optional[int] = None,
    default_date: Any = "",
) -> tuple[list[dict[str, Any]], list[str]]:
    """导出用入口：59 键产物 → MCP 入参数组（= 一份 `<stem>.erp-import.json`）。"""
    return build_calls(records, customer=customer, qty=qty, default_date=default_date)
