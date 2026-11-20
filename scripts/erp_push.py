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

# ⚠ ERP MCP 地址**不写死**（公开仓库，2026-11-20 森西要求外置）。
# 解析顺序：env ERP_MCP_URL > assets/erp_endpoint.local.yaml > 空（空则启动时报错提示怎么配）。
# 见 scripts/local_config.py。
def _default_mcp_url() -> str:
    try:
        from local_config import resolve
        return resolve("erp_endpoint", "url", "ERP_MCP_URL")
    except ImportError:  # 单独拷脚本时退回环境变量
        return os.environ.get("ERP_MCP_URL", "").strip()


DEFAULT_MCP_URL = _default_mcp_url()
# ★ 59 键 → MCP 入参 的映射**只有一处**：`erp_mcp_export.py`（导出那一步就用它）。
#   这里只做 re-export，方便老代码/测试继续 `erp_push.PRODUCT_FIELD_MAP`，
#   绝不再落地第二份字段名表 —— 2026-11-12 森西要求「字段名、格式一致，一份文件」。
from erp_mcp_export import (  # noqa: E402
    BINDING_BY_CATEGORY,
    CPMC_PLACEHOLDER,
    FIELD_DEFAULTS,
    HEADER_ARG_MAP,
    MANDATORY_FIELDS,
    NOT_WRITABLE,
    PRODUCT_FIELD_MAP,
    QTY_SLOT_MAP,
    _norm_value,
    _record_qty,
    _record_titles,
    _record_total,
    apply_defaults,
    build_call,
    build_calls,
    defaulted_fields,
    is_mcp_payload,
    unwritable_fields,
    validate_call,
)


class McpError(RuntimeError):
    pass


class McpHttpClient:
    """极简 MCP streamable-HTTP 客户端：initialize → tools/call。

    响应可能是 SSE（`data: {...}`）也可能是裸 JSON，两种都解 —— 服务端版本不同
    framed 类型不同。
    """

    def __init__(self, url: str = DEFAULT_MCP_URL, token: str = "",
                 timeout: float = 180.0, verify_ssl: bool = False):
        if not url:
            raise McpError(
                "没配 ERP MCP 地址 —— 三选一："
                "① 设环境变量 ERP_MCP_URL；"
                "② 写 assets/erp_endpoint.local.yaml（照 .local.example.yaml 复制，机器本地不入库）；"
                "③ 调用时传 --url")
        self.url = url
        self.token = token
        self.timeout = timeout
        self.session_id: Optional[str] = None
        self._last_headers: dict[str, str] = {}
        self._id = 0
        self._ctx = ssl.create_default_context()
        if not verify_ssl:  # 自签证书（SAN 里没有内网地址），同 Hermes MCP 配置
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


def build_payload(records: list[dict[str, Any]], *, customer: str = "",
                  qty_override: Optional[int] = None,
                  profile_id: str = "",
                  default_date: str = "") -> tuple[dict[str, Any], list[str]]:
    """兼容旧调用（单条）：返回第一次 create 的入参。"""
    code = customer or _profile_customer_code(profile_id)
    calls, problems = build_calls(records, customer=code, qty=qty_override,
                                  default_date=default_date)
    return (calls[0] if calls else {}), problems


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


# ── 「不推 ERP」白名单（业务 2026-11-17 森西：早期产物不需要推送到 ERP）──────
# 2026-10-30 / 11-10 那批是流水线搭起来时的历史产物：报价期早过、
# 有些客户当时还没建档。业务口径是**留档不推**。
# 用一份显式清单（带原因与日期）而不是「按时间一刀切」——
# 时间会漂移，清单不会：以后哪份要补推，删掉那一行即可。
SKIP_PATH = CUSTOMER_FILES / ".erp_push_skip.json"


def skip_reason(task_id: str) -> Optional[dict[str, Any]]:
    """这个 task 是否被业务标了「不推 ERP」。返回该条记录（含 reason）或 None。"""
    if not task_id or not SKIP_PATH.exists():
        return None
    try:
        data = json.loads(SKIP_PATH.read_text(encoding="utf-8"))
    except Exception:                       # 清单写坏了不能拦住正常推送
        return None
    entry = (data.get("skip") or {}).get(task_id)
    return entry if isinstance(entry, dict) else None


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
        raw = str(result.get("raw"))
        if "产品档案" in raw:
            # ★ 最麻烦的一种：主表已经建好、明细被档案挡住 → 留下一条**空单**，
            #   必须先补档案 / 给料号，再 `erp_quote_delete` 清掉空单才能重推。
            return "archive_missing", None, raw
        return "error", None, raw
    status = str(result.get("status") or "")
    bdbh = result.get("bdbh") or result.get("BDBH")
    # ★ status 优先于 bdbh：`failed` / `partial` 也带 bdbh（主表已建、明细没落），
    #   先判 bdbh 会把空单报成 created（2026-11-12 BJXQ261112007 踩过）。
    if status in ("failed", "partial", "rejected", "invalid", "unsupported"):
        msg = (result.get("detail_error") or result.get("message")
               or str(result.get("errors") or ""))
        if status == "partial" or (bdbh and status == "failed"):
            msg = f"{msg}（主表已建：{bdbh}，明细未落库 → 是空单，要删）"
        return status, None, msg
    if status == "created" or (bdbh and not status):
        return "created", bdbh, None
    if status == "needs_confirmation":
        return "dry_run", None, None
    return status or "unknown", None, None


def _sibling_records(artifact: Path) -> Optional[list[dict[str, Any]]]:
    """同目录那份 59 键主产物（用于补 `defaulted` / `not_writable` 等诊断信息）。

    MCP 入参文件里**只有** ERP 参数，诊断信息放不进去（放进去 ERP 会拒），
    所以能读到主产物就从主产物算，读不到就算了 —— 不影响推送本身。
    """
    for candidate in (artifact.with_name(artifact.name.replace(".erp-import.json", ".json")),
                      artifact.with_name(artifact.name.replace(".json", ".json"))):
        if candidate == artifact or not candidate.is_file():
            continue
        if candidate.name.endswith(".erp-import.json") or candidate.name.endswith(".erp-push.json"):
            continue
        try:
            return load_bundle(candidate)
        except Exception:  # noqa: BLE001
            continue
    return None


def _mcp_calls_with_overrides(calls: list[dict[str, Any]], qty: Optional[int]
                              ) -> list[dict[str, Any]]:
    """`--qty`（单款本数）覆盖已导出文件里的数量：qty 与 qty_1 一起改。"""
    if not qty:
        return calls
    out = json.loads(json.dumps(calls, ensure_ascii=False))  # 深拷贝
    for call in out:
        for product in call.get("products") or []:
            fields = product.get("fields") or {}
            fields["qty_1"] = int(qty)
            product["qty"] = int(qty)
            product["fields"] = fields
    return out


def _mcp_quantity(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """MCP 入参文件里的数量（已乘过款数，直接读，不再推算）。"""
    out: list[dict[str, Any]] = []
    for idx, call in enumerate(calls, 1):
        for product in call.get("products") or []:
            fields = product.get("fields") or {}
            out.append({"record": idx,
                        "titles": fields.get("cases"),
                        "qty_landed": product.get("qty"),
                        "qty_1": fields.get("qty_1")})
    return out


def push(
    artifact: Path,
    *,
    client: Any,
    customer: str = "",
    qty: Optional[int] = None,
    confirm: bool = False,
    force: bool = False,
    default_date: str = "",
) -> dict[str, Any]:
    """推一份产物。

    两种输入都吃：
      * **MCP 入参**（`*.erp-import.json`，新格式）→ 原样发出，不再二次映射；
      * **59 键主产物**（旧格式）→ 现场用 `erp_mcp_export` 翻一次，兼容老文件。
    """
    raw = json.loads(artifact.read_text(encoding="utf-8"))
    is_mcp = is_mcp_payload(raw)
    # MCP 入参里没有内部字段 → 诊断信息去同目录主产物找；旧格式就直接读自己
    records = _sibling_records(artifact) if is_mcp else load_bundle(artifact)
    records = records or []
    task_id = str((records[0].get("ID") if records else "") or artifact.parent.name)

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
        # 业务标了「早期产物不推 ERP」→ 直接跳过（`--force` 仍可强推）
        skipped = skip_reason(task_id)
        if skipped:
            return {
                "status": "skipped",
                "reason": f"业务标记「不推 ERP」：{skipped.get('reason') or '历史产物留档'}",
                "task_id": task_id,
                "skip_list": str(SKIP_PATH),
            }

    if is_mcp:
        calls = _mcp_calls_with_overrides(raw, qty)
        problems: list[str] = []
        for idx, call in enumerate(calls, 1):
            prefix = f"第{idx}条：" if len(calls) > 1 else ""
            problems.extend(validate_call(call))
            if not call.get("customer"):
                problems.append(prefix + "customer 为空（导出时客户代号没解析出来）")
            for product in call.get("products") or []:
                fields = product.get("fields") or {}
                missing = [f"{arg}({src})" for arg, src in MANDATORY_FIELDS.items()
                           if arg not in fields]
                if missing:
                    problems.append(prefix + "ERP 必填缺失（会留下空主表）：" + "、".join(missing))
    else:
        calls, problems = build_calls(records, customer=customer, qty=qty,
                                      default_date=default_date)

    report: dict[str, Any] = {
        "task_id": task_id,
        "artifact": str(artifact),
        "artifact_format": "mcp" if is_mcp else "59键",
        "confirm": confirm,
        "record_count": len(calls),
        "calls": calls,
        "problems": problems,
        "quantity": (_mcp_quantity(calls) if is_mcp else [
            {"record": idx,
             "titles": _record_titles(rec),
             "per_title": _record_qty(rec, qty),
             "total_landed": _record_total(rec, qty)}
            for idx, rec in enumerate(records, 1)
        ]),
        "defaulted": defaulted_fields(records),
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

    if "archive_missing" in statuses:
        report["lost_header"] = True
        report["hint"] = ("产品档案不命中：ERP 已建好主表、明细被挡 → 列表里会多一条空单"
                          "（产品名称/报价状态均为空）。补档案或给料号后，"
                          "先用 erp_quote_delete 删掉空单再重推。")
    return report


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="把 59 键产物推进 ERP（新增报价需求）")
    ap.add_argument("artifact", help="ERP_*.json 或 *.erp-import.json")
    ap.add_argument("--customer", default="", help="ERP 客户代号（NGV10 / NGVS16 / IBL10…）")
    ap.add_argument("--qty", type=int, default=None, help="本单数量（CPSL 全空时必填；多条记录时共用）")
    ap.add_argument("--confirm", action="store_true", help="真写 ERP；不加 = 干跑")
    ap.add_argument("--date", dest="default_date", default="",
                    help="JHRQ/WCRQ 缺值时的兜底日期（ERP 必填），形如 2027-03-15")
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
                  confirm=args.confirm, force=args.force,
                  default_date=args.default_date)
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
