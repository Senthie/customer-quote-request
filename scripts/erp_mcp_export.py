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

from typing import Any, Optional

CPMC_PLACEHOLDER = "【中文品名待定】"

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
    default_date: str = "",
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

    # 数量档位 CPSL1..10 → qty_1..qty_10（每档都是**总量** = 单款 × 款数）
    titles = _record_titles(record)
    for key, arg in QTY_SLOT_MAP.items():
        val = record.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool) and val:
            fields[arg] = int(val) * titles
    if qty:
        fields["qty_1"] = int(qty)

    # ★ 兜底：缺日期用 default_date（JHRQ/WCRQ 同值），缺必填用 FIELD_DEFAULTS
    if default_date:
        for key in ("JHRQ", "WCRQ"):
            if not str(record.get(key) or "").strip():
                fields[PRODUCT_FIELD_MAP[key]] = default_date
    for arg, (val, _reason) in apply_defaults(record, fields).items():
        fields[arg] = val

    product["fields"] = fields

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
    default_date: str = "",
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
    default_date: str = "",
) -> tuple[list[dict[str, Any]], list[str]]:
    """导出用入口：59 键产物 → MCP 入参数组（= 一份 `<stem>.erp-import.json`）。"""
    return build_calls(records, customer=customer, qty=qty, default_date=default_date)
