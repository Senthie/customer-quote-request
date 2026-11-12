#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""erp_push.py —— 把 59 键产物**推进 ERP**（流水线的最后一步）。

> 协议依据：`docs/erp-quote-create.md`（2026-11-12，逐字段对照
> `src/erp_tools/tools/quote_create/service.py` 的 `prepare()` / `create()` /
> `SUB_FIELD_MAP` / `PRODUCT_ARG_FIELDS` 校对过）。**以那份文档为准，不以猜测为准。**

能写什么（59 键里 24 个）
-------------------------
表头 9 个：`customer` / `sales` / `contact` / `phone` / `mobile` / `fax` /
          `discount` / `remark` / `product_memo`
明细 14 个（`products[0]`）：`binding`→`ZDFS`、`packing`→`BZFS`、`pallet`→`TBLX`、
          `delivery`→`SHFS`、`delivery_date`→`JHRQ`、`expected_date`→`WCRQ`、
          `parts_desc`→`Remark1`、`modify_log`→`Remark3`、`inspection`→`Remark4`、
          `product_desc`→`CPMS`、`attribute`→`CPSX`、`product_type`→`CPLX`、
          `cases`→`TZS`、`languages`→`YYS`
+ `name`（`CPMC`）/ `spec`（`CD*KD*GD`，**只校验不落库**）/ `qty`（写 `txtCPSL`+`txtCPSL1`）

写不进去的（由 ERP 从**产品档案**反查回填，或页面没有对应控件）
--------------------------------------------------------------
`CPLB`（产品类别，树形下拉）、`CD`/`KD`/`GD`（尺寸——`spec` 传了也只校验）、
`DWMC`（单位）、`CPSL2…CPSL10`（只写汇总 `txtCPSL`，阶梯丢失）、
`FSCMC`、`KHDH`、`KHDDH`、`ECPMC`、`BZ`、`KHMBDJ`、`YSLX`/`SHDQ`/`KHDQ`、
`ZXLB`/`ZBLX`/`ZXFS`/`DKMC`（装箱细项）、`SFYPJ` 及各 `chk*` 布尔、`Drop_SJFX`、
`OLD_KHDDH`、`BGMS`。→ 脚本把它们列进 `not_writable`，进 ERP 后由录单同事补
（业务 2026-11-11：「未定选项可以去 ERP 后修改」）。

两条硬约束（踩过）
------------------
1. **产品名必须在【产品档案】里唯一匹配**（精确同名优先）——2026-11-11 实测：
   `产品档案里找不到 '冬季找找图画书'`。而且**尺寸/类别/单位都是从档案反查的**，
   所以产品档案条目本身得对。
2. **一次只支持 1 条明细**（`len(products)!=1` → `status=unsupported`）→ 一条产品记录
   一次 `create`，N 条就调 N 次。

用法
----
    # ① 干跑（默认；MCP 侧 confirm=false，不写库）
    uv run ./scripts/erp_push.py <产物>/ERP_xxx.erp-import.json --customer NGV10 --qty 410000

    # ② 真写（逐条建主表 + 明细，返回表单编号 BDBH）
    uv run ./scripts/erp_push.py ... --confirm

输出：stdout 一份 JSON，同时写 `<产物>.erp-push.json` + 追加 `customer-files/.erp_push_log.jsonl`。

幂等：同一个 task（产物里的 `ID`）已有 `status=created` 记录 → 默认拒推（`--force` 才再推）。
ERP 里重复建单是最麻烦的错误，宁可少推一次也不要多推。
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

REPO = Path(__file__).resolve().parent.parent
CUSTOMER_FILES = REPO / "customer-files"
PUSH_LOG = CUSTOMER_FILES / ".erp_push_log.jsonl"

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from erp_bundle import load_bundle  # noqa: E402

DEFAULT_MCP_URL = "https://14.12.0.173:8443/mcp"
CPMC_PLACEHOLDER = "【中文品名待定】"

# 59 键 → erp_quote_create 明细入参（协议见 docs/erp-quote-create.md §4）
PRODUCT_ARG_MAP: dict[str, str] = {
    "ZDFS": "binding",
    "BZFS": "packing",
    "TBLX": "pallet",
    "SHFS": "delivery",
    "JHRQ": "delivery_date",
    "WCRQ": "expected_date",
    "Remark1": "parts_desc",
    "Remark3": "modify_log",
    "Remark4": "inspection",
    "CPMS": "product_desc",
    "CPSX": "attribute",
    "CPLX": "product_type",
    "TZS": "cases",
    "YYS": "languages",
}

# 59 键 → erp_quote_create 表头入参
HEADER_ARG_MAP: dict[str, tuple[str, str]] = {
    "LXR": ("contact", "str"),
    "LXDH": ("phone", "str"),
    "SJ": ("mobile", "str"),
    "CZ": ("fax", "str"),
    "ZKL": ("discount", "str"),
    "CPMX": ("product_memo", "str"),
}

# 这些字段 create 一定写不进去（ERP 从产品档案反查，或页面无控件）
NOT_WRITABLE = (
    "CPLB", "CD", "KD", "GD", "DWMC", "FSCMC", "KHDH", "KHDDH", "ECPMC",
    "BZ", "KHMBDJ", "YSLX", "SHDQ", "KHDQ", "ZXLB", "ZBLX", "ZXFS", "DKMC",
    "SFYPJ", "chkXBJF", "chkZDBJF", "chkBSYTB", "chkBSYJZX", "Drop_SJFX",
    "OLD_KHDDH", "BGMS",
) + tuple(f"CPSL{i}" for i in range(2, 11))


# ─────────────────────────── MCP 客户端（stdlib，无新依赖） ───────────────────────────
class McpError(RuntimeError):
    pass


class McpHttpClient:
    """极简 MCP streamable-HTTP 客户端：initialize → tools/call。

    响应可能是 SSE（`data: {...}`）也可能是裸 JSON，两种都解 —— 服务端版本不同
    framed 类型不同。
    """

    def __init__(self, url: str = DEFAULT_MCP_URL, token: str = "",
                 timeout: float = 180.0, verify_ssl: bool = False):
        self.url = url
        self.token = token
        self.timeout = timeout
        self.session_id: Optional[str] = None
        self._last_headers: dict[str, str] = {}
        self._id = 0
        self._ctx = ssl.create_default_context()
        if not verify_ssl:  # 自签证书（SAN 里没有 14.12.0.173），同 Hermes MCP 配置
            self._ctx.check_hostname = False
            self._ctx.verify_mode = ssl.CERT_NONE

    def _post(self, payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json, text/event-stream")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        if self.session_id:
            req.add_header("Mcp-Session-Id", self.session_id)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as resp:
                raw = resp.read().decode("utf-8", "replace")
                headers = {k.lower(): v for k, v in resp.headers.items()}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:500]
            raise McpError(f"HTTP {exc.code} {self.url}: {body}") from exc
        except urllib.error.URLError as exc:
            raise McpError(f"连不上 MCP（{self.url}）：{exc.reason}") from exc
        self._last_headers = headers
        return _parse_body(raw), headers

    def _rpc(self, method: str, params: Optional[dict[str, Any]] = None,
             notify: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not notify:
            self._id += 1
            payload["id"] = self._id
        body, _ = self._post(payload)
        if notify:
            return {}
        if "error" in body:
            raise McpError(f"{method} 失败：{body['error']}")
        return body.get("result", body)

    def initialize(self) -> dict[str, Any]:
        result = self._rpc("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "cqr-erp-push", "version": "1.0"},
        })
        self.session_id = (self._last_headers.get("mcp-session-id")
                           or self._last_headers.get("x-session-id"))
        self._rpc("notifications/initialized", notify=True)
        return result

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        for item in result.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "text":
                try:
                    return json.loads(item["text"])
                except (KeyError, json.JSONDecodeError):
                    return {"raw": item.get("text")}
        return result


def _parse_body(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        return {}
    if text.startswith("{"):
        return json.loads(text)
    out: dict[str, Any] = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            chunk = line[5:].strip()
            if chunk and chunk != "[DONE]":
                try:
                    out = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
    return out


# ─────────────────────────── 产物 → 推送载荷 ───────────────────────────
def _profile_customer_code(profile_id: str) -> str:
    """客户档案 `erp_khzl` 的第一个 = ERP 主客户代号（NGV→NGV10、igl→IBL10）。"""
    if not profile_id:
        return ""
    try:
        from customer_profile import load_profile

        codes = load_profile(profile_id).rule("erp_khzl", []) or []
        return str(codes[0]).strip() if codes else ""
    except Exception:  # noqa: BLE001
        return ""


def _record_qty(record: dict[str, Any], override: Optional[int]) -> Optional[int]:
    if override:
        return int(override)
    total = sum(int(v) for v in (record.get(f"CPSL{i}") for i in range(1, 11))
                if isinstance(v, (int, float)))
    return total or None


def build_call(
    record: dict[str, Any],
    *,
    customer: str,
    qty: Optional[int],
) -> tuple[dict[str, Any], list[str]]:
    """单条产品记录 → 一次 `erp_quote_create` 的入参（ERP 一次只收 1 条明细）。"""
    problems: list[str] = []
    name = str(record.get("CPMC") or record.get("CPMC_ZH") or "").strip()
    if not name or name == CPMC_PLACEHOLDER:
        problems.append("CPMC 还是占位符/空 → 先写中文品名再推")

    cd, kd, gd = record.get("CD"), record.get("KD"), record.get("GD")
    spec = f"{cd}*{kd}*{gd or 0}" if cd not in (None, "") and kd not in (None, "") else ""

    qty = _record_qty(record, qty)
    if not qty:
        problems.append("数量未知（CPSL1-10 全空/为 0）→ 用 --qty 指定（ERP 明细的「产品数量」是实体数量）")

    product: dict[str, Any] = {"name": name, "spec": spec, "qty": qty or 0}
    for key, arg in PRODUCT_ARG_MAP.items():
        val = record.get(key)
        if val in (None, "", [], 0):
            continue  # 0/空不传：别把 `TZS=0` 这类噪音塞进 ERP
        product[arg] = val

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
    for key, (arg, _) in HEADER_ARG_MAP.items():
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
    profile_id: str = "",
) -> tuple[list[dict[str, Any]], list[str]]:
    """整份产物 → N 次 create 的入参（N = 产品记录数；ERP 一次只收 1 条明细）。"""
    if not records:
        return [], ["产物里没有记录"]
    problems: list[str] = []
    code = (customer or str(records[0].get("KHZL") or "").strip()
            or _profile_customer_code(profile_id))
    if not code:
        problems.append("客户代码未知：产物 KHZL 为空且档案里没有 erp_khzl → 请用 --customer 指定")

    calls: list[dict[str, Any]] = []
    for idx, rec in enumerate(records, 1):
        payload, probs = build_call(rec, customer=code, qty=qty)
        if len(records) > 1:
            probs = [f"第{idx}条：{p}" for p in probs]
        problems.extend(probs)
        calls.append(payload)
    return calls, problems


# 兼容旧调用（单条）：返回第一条的 payload
def build_payload(records: list[dict[str, Any]], *, customer: str = "",
                  qty_override: Optional[int] = None,
                  profile_id: str = "") -> tuple[dict[str, Any], list[str]]:
    calls, problems = build_calls(records, customer=customer, qty=qty_override,
                                  profile_id=profile_id)
    return (calls[0] if calls else {}), problems


def _is_emptyish(key: str, val: Any) -> bool:
    """判断「这个字段其实没有值」。

    `GD=0` 对业务是**有意义的 0**（客户没给厚度 = 0，不是没填），必须报出来；
    而 `CPSL2=0` / `chkXBJF=False` 这种默认值报出来只是噪音。
    """
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


# ─────────────────────────── 幂等日志 ───────────────────────────
def _log_records() -> list[dict[str, Any]]:
    if not PUSH_LOG.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in PUSH_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def already_pushed(task_id: str) -> Optional[dict[str, Any]]:
    for row in reversed(_log_records()):
        if row.get("task_id") == task_id and row.get("status") == "created":
            return row
    return None


def append_log(entry: dict[str, Any]) -> None:
    PUSH_LOG.parent.mkdir(parents=True, exist_ok=True)
    with PUSH_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def sidecar_path(artifact: Path) -> Path:
    """`ERP_x.erp-import.json` → `ERP_x.erp-push.json`（别叠成 `.erp-import.erp-push`）。"""
    name = artifact.name
    for suffix in (".erp-import.json", ".json"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return artifact.with_name(name + ".erp-push.json")


# ─────────────────────────── 主流程 ───────────────────────────
def _classify(result: dict[str, Any]) -> tuple[str, Optional[str], Optional[str]]:
    """ERP 返回 → (status, bdbh, error)。以 `status` 为准，不靠 alert 文案。"""
    if isinstance(result, dict) and "raw" in result:
        # MCP 把工具异常包成一条 text：「Error executing tool …: 产品档案里找不到 X」
        return "error", None, str(result.get("raw"))
    status = str(result.get("status") or "")
    bdbh = result.get("bdbh") or result.get("BDBH")
    if status == "created" or bdbh:
        return "created", bdbh, None
    if status == "needs_confirmation":
        return "dry_run", None, None
    if status in ("invalid", "unsupported", "rejected", "partial", "failed"):
        msg = result.get("message") or result.get("detail_error") or str(result.get("errors") or "")
        return status, None, msg
    return status or "unknown", None, None


def push(
    artifact: Path,
    *,
    client: Any,
    customer: str = "",
    qty: Optional[int] = None,
    confirm: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    records = load_bundle(artifact)
    task_id = str(records[0].get("ID") or artifact.parent.name) if records else ""

    if not force and task_id:
        prev = already_pushed(task_id)
        if prev:
            return {
                "status": "skipped",
                "reason": "该任务已经推过一次（ERP 里重复建单最麻烦）",
                "task_id": task_id,
                "bdbh": prev.get("bdbh"),
                "log": str(PUSH_LOG),
            }

    calls, problems = build_calls(records, customer=customer, qty=qty)
    report: dict[str, Any] = {
        "task_id": task_id,
        "artifact": str(artifact),
        "confirm": confirm,
        "record_count": len(records),
        "calls": calls,
        "problems": problems,
        "not_writable": unwritable_fields(records),
        "results": [],
    }
    if problems:
        report["status"] = "invalid"
        return report

    for idx, payload in enumerate(calls, 1):
        result = client.call_tool("erp_quote_create", {**payload, "confirm": bool(confirm)})
        status, bdbh, error = _classify(result)
        report["results"].append({
            "record": idx,
            "status": status,
            "bdbh": bdbh,
            "error": error,
            "erp_response": result,
        })
        if status == "created":
            append_log({"task_id": task_id, "record": idx, "bdbh": bdbh, "status": "created",
                        "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "artifact": str(artifact),
                        "customer": payload.get("customer"),
                        "product": payload["products"][0]["name"]})

    statuses = [r["status"] for r in report["results"]]
    if all(s == "created" for s in statuses):
        report["status"] = "created"
        report["bdbh"] = [r["bdbh"] for r in report["results"]]
    elif all(s == "dry_run" for s in statuses):
        report["status"] = "dry_run"
    elif any(s == "created" for s in statuses):
        report["status"] = "partial"  # 有的建了有的没建 —— 必须人看一眼
    else:
        report["status"] = statuses[0] if statuses else "unknown"
        report["error"] = next((r["error"] for r in report["results"] if r["error"]), None)
    return report


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="把 59 键产物推进 ERP（新增报价需求）")
    ap.add_argument("artifact", help="ERP_*.json 或 *.erp-import.json")
    ap.add_argument("--customer", default="", help="ERP 客户代号（NGV10 / NGVS16 / IBL10…）")
    ap.add_argument("--qty", type=int, default=None, help="本单数量（CPSL 全空时必填；多条记录时共用）")
    ap.add_argument("--confirm", action="store_true", help="真写 ERP；不加 = 干跑")
    ap.add_argument("--force", action="store_true", help="同一 task 已推过也再推一次")
    ap.add_argument("--token", default="", help="MCP token（默认 $ERP_MCP_TOKEN）")
    ap.add_argument("--url", default=DEFAULT_MCP_URL)
    args = ap.parse_args(argv)

    path = Path(args.artifact)
    if not path.is_file():
        print(f"找不到产物：{path}", file=sys.stderr)
        return 2
    token = args.token or os.environ.get("ERP_MCP_TOKEN", "")

    client = McpHttpClient(url=args.url, token=token)
    try:
        client.initialize()
    except McpError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 3

    report = push(path, client=client, customer=args.customer, qty=args.qty,
                  confirm=args.confirm, force=args.force)
    report["at"] = time.strftime("%Y-%m-%dT%H:%M:%S")

    sidecar = sidecar_path(path)
    sidecar.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if report.get("status") == "dry_run":
        append_log({"task_id": report.get("task_id"), "status": "dry_run", "at": report["at"],
                    "artifact": str(path)})
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") in ("created", "dry_run") else 1


if __name__ == "__main__":
    sys.exit(main())
