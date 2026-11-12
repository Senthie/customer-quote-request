#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""erp_push.py —— 把 59 键产物**推进 ERP**（流水线的最后一步）。

为什么需要它
------------
`main.py` 只做到「把产物落盘」+「多写一份 59 键 `<...>.erp-import.json`」。
文件躺在 `processed/` 里不会自己进 ERP —— 2026-11-11 业务问「为什么没上传 ERP」，
答案就是**流水线原本没有这一步**。本脚本补上这一步。

⚠️ 只能推 10 个字段（2026-11-11 实测，不是权限问题，是表单没有控件）
--------------------------------------------------------------------
ERP「新增客户报价需求」页 `BJ_KHBJXQ/Add.aspx` 只有 **15 个表头控件** +
产品明细子页（名称/规格/数量）。`erp_quote_new_form` 的实测清单：

    txtBDBH(只读) txtKHID txtKHMC(只读) txtKHBM txtYGID txtYGXM txtGDYXM
    txtDJTX txtLXR txtLXDH txtSJ txtCZ txtZKL txtRemark txtCPMX
    + 产品明细：名称 / 规格 / 数量

59 键里能对上的是 **10 个**（`KHZL`→txtKHID、`YWY`→txtYGXM、`GDY`→txtGDYXM、
`LXR`→txtLXR、电话/手机/传真、`ZKL`→txtZKL、`Remark`、`CPMX`、`CPMC`+尺寸+数量）。
其余 49 个（`CPLB`/`CPMS`/`TZS`/`YYS`/`CD`/`KD`/`GD`/`FSCMC`/`BZFS`/`TBLX`/`SHFS`/
`YSLX`/`SHDQ`/`ZXLB`/`ZBLX`/`ZXFS`/`DKMC`/`SFYPJ`/`KHDH`/`KHDDH`/`ECPMC`/`JHRQ`/
`WCRQ`/`Remark1-4`/`CPSL1-10`/`CPSX`…）**在这张页面上根本没有输入框** ——
MCP 是驱动浏览器填表，页面上不存在的控件填不进去。这些字段要么由 ERP 侧的
**导入接口**一次性写入，要么由录单同事在建单后的明细/编辑页补齐（业务 2026-11-11
口径：「未定选项可以去 ERP 后修改」）。

所以本脚本的定位是：**先把单建出来（客户 + 产品 + 数量 + 一个能追溯的 Remark），
剩下的字段进 ERP 后补**。不要在脚本里假装能写全 59 键。

用法
----
    # ① 干跑（默认；MCP 侧 confirm=false，不写库）
    uv run ./scripts/erp_push.py customer-files/processed/<task>/ERP_xxx.erp-import.json

    # ② 真写（建主表 + 产品明细，返回表单编号 BDBH）
    uv run ./scripts/erp_push.py ... --confirm --qty 410000

    # 客户代号不确定时显式给（NGV→NGV10、igl→IBL10；默认取客户档案 erp_khzl[0]）
    uv run ./scripts/erp_push.py ... --customer NGVS16

输出：stdout 一份 JSON（给 agent/上层读），同时把结果写到
`<产物>.erp-push.json`、追加一行到 `customer-files/.erp_push_log.jsonl`。

幂等：同一个 task（= 产物里的 `ID`）已有 `status=created` 记录 → 拒绝再推，
除非 `--force`（重复推会在 ERP 里生成第二张单，业务最怕这个）。
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
# ERP 里「新增报价需求」页只能写这些 —— 其余字段见模块 docstring
WRITABLE_HEADER = (
    "KHZL", "YWY", "GDY", "LXR", "LXDH", "SJ", "CZ", "ZKL", "Remark", "CPMX",
)
CPMC_PLACEHOLDER = "【中文品名待定】"


# ─────────────────────────── MCP 客户端（stdlib，无新依赖） ───────────────────────────
class McpError(RuntimeError):
    pass


class McpHttpClient:
    """极简 MCP streamable-HTTP 客户端：initialize → tools/call。

    只实现本脚本需要的两个动作。响应可能是 SSE（`data: {...}`）也可能是裸 JSON，
    两种都解 —— 服务端版本不同返回不同 framed 类型。
    """

    def __init__(self, url: str = DEFAULT_MCP_URL, token: str = "",
                 timeout: float = 120.0, verify_ssl: bool = False):
        self.url = url
        self.token = token
        self.timeout = timeout
        self.session_id: Optional[str] = None
        self._last_headers: dict[str, str] = {}
        self._id = 0
        self._ctx = ssl.create_default_context()
        if not verify_ssl:  # 自签证书（14.12.0.173 的 SAN 里没有这个 IP），同 MCP 配置
            self._ctx.check_hostname = False
            self._ctx.verify_mode = ssl.CERT_NONE

    # -- 传输层 ------------------------------------------------------------
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
        except urllib.error.HTTPError as exc:  # 带上响应体，别只报状态码
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
        body, headers = self._post(payload)
        if notify:
            return {}
        if "error" in body:
            raise McpError(f"{method} 失败：{body['error']}")
        return body.get("result", body)

    # -- 协议层 ------------------------------------------------------------
    def initialize(self) -> dict[str, Any]:
        result = self._rpc("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "cqr-erp-push", "version": "1.0"},
        })
        # 服务端在 initialize 的响应头里给 session id；后续请求必须带上
        self.session_id = (self._last_headers.get("mcp-session-id")
                           or self._last_headers.get("x-session-id"))
        self._rpc("notifications/initialized", notify=True)
        return result

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        # MCP 把工具返回值放在 content[].text（JSON 字符串）
        content = result.get("content") or []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                try:
                    return json.loads(item["text"])
                except (KeyError, json.JSONDecodeError):
                    return {"raw": item.get("text")}
        return result


def _parse_body(raw: str) -> dict[str, Any]:
    """兼容两种响应：裸 JSON / SSE（`event: message` + `data: {...}`）。"""
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
    except Exception:  # noqa: BLE001 —— 读不到档案就让调用方显式给 --customer
        return ""


def build_payload(
    records: list[dict[str, Any]],
    *,
    customer: str = "",
    qty_override: Optional[int] = None,
    profile_id: str = "",
) -> tuple[dict[str, Any], list[str]]:
    """把产物记录折成 `erp_quote_create` 的入参 + 一组「推不进去的字段」说明。"""
    problems: list[str] = []
    if not records:
        return {}, ["产物里没有记录"]
    head = records[0]

    code = (customer or str(head.get("KHZL") or "").strip()
            or _profile_customer_code(profile_id))
    if not code:
        problems.append("客户代码未知：产物 KHZL 为空且档案里没有 erp_khzl → 请用 --customer 指定")

    products: list[dict[str, Any]] = []
    for idx, rec in enumerate(records, 1):
        name = str(rec.get("CPMC") or rec.get("CPMC_ZH") or "").strip()
        if not name or name == CPMC_PLACEHOLDER:
            problems.append(f"第{idx}条 CPMC 还是占位符/空 → 先写中文品名再推")
        cd, kd, gd = rec.get("CD"), rec.get("KD"), rec.get("GD")
        spec = ""
        if cd not in (None, "") and kd not in (None, ""):
            spec = f"{cd}*{kd}*{gd or 0}"
        qty = qty_override
        if qty is None:
            slots = [rec.get(f"CPSL{i}") for i in range(1, 11)]
            total = sum(int(v) for v in slots if isinstance(v, (int, float)))
            qty = total or None
        if not qty:
            problems.append(
                f"第{idx}条数量未知（CPSL1-10 全空/为 0）→ 用 --qty 指定本单数量"
                "（ERP 明细的「产品数量」是必填的实体数量，不是阶梯）")
        products.append({"name": name, "spec": spec, "qty": qty or 0})

    khddh = str(head.get("KHDDH") or "").strip()
    ecpmc = str(head.get("ECPMC") or "").strip()
    remark = str(head.get("Remark") or "").strip()
    if not remark:
        # 用「香港单号 / 原始品名」当追溯串：ERP 里一眼能认出是这条询价
        remark = " / ".join(x for x in (khddh, ecpmc) if x)

    payload: dict[str, Any] = {
        "customer": code,
        "products": products,
        "remark": remark[:500],
    }
    for key in ("LXR", "LXDH", "SJ", "CZ", "ZKL", "CPMX"):
        val = head.get(key)
        if val not in (None, ""):
            payload[key.lower()] = str(val)
    # ★ YWY/GDY 故意不传：产物里是生成标记 `KM`（不是 ERP 真人），传了会被拒；
    #   ERP 会按客户资料自动带出业务员/跟单员（实测 NGV10 → 陈纯 / 肖也）。
    return payload, problems


def unwritable_fields(records: list[dict[str, Any]]) -> dict[str, str]:
    """这单有哪些字段**推不进 ERP**（要给业务签字确认的清单）。"""
    head = records[0] if records else {}
    out: dict[str, str] = {}
    for key, rec_key in (("CPLB", "CPLB"), ("CPMS", "CPMS"), ("TZS", "TZS"), ("YYS", "YYS"),
                         ("KHDH", "KHDH"), ("KHDDH", "KHDDH"), ("ECPMC", "ECPMC"),
                         ("ZDFS", "ZDFS"), ("FSCMC", "FSCMC"), ("BZFS", "BZFS"),
                         ("TBLX", "TBLX"), ("SHFS", "SHFS"), ("JHRQ", "JHRQ")):
        val = head.get(rec_key)
        if val not in (None, "", 0, []):
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

    payload, problems = build_payload(records, customer=customer, qty_override=qty)
    report: dict[str, Any] = {
        "task_id": task_id,
        "artifact": str(artifact),
        "confirm": confirm,
        "payload": payload,
        "problems": problems,
        "not_writable": unwritable_fields(records),
    }
    if problems:
        report["status"] = "invalid"
        return report

    result = client.call_tool("erp_quote_create", {**payload, "confirm": bool(confirm)})
    report["erp_response"] = result
    status = str(result.get("status") or "")
    if status == "created" or result.get("bdbh") or result.get("BDBH"):
        report["status"] = "created"
        report["bdbh"] = result.get("bdbh") or result.get("BDBH")
    elif status == "needs_confirmation":
        report["status"] = "dry_run"
    elif isinstance(result, dict) and "raw" in result:
        # MCP 把工具异常包成一条 text：「Error executing tool …: 产品档案里找不到 X」
        report["status"] = "error"
        report["error"] = str(result.get("raw"))
    else:
        report["status"] = status or "unknown"
    return report


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="把 59 键产物推进 ERP（新增报价需求）")
    ap.add_argument("artifact", help="ERP_*.json 或 *.erp-import.json")
    ap.add_argument("--customer", default="", help="ERP 客户代号（NGV10 / NGVS16 / IBL10…）")
    ap.add_argument("--qty", type=int, default=None, help="本单数量（CPSL 全空时必填）")
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
    except McpError as exc:  # 连不上就别装成功
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 3

    report = push(path, client=client, customer=args.customer, qty=args.qty,
                  confirm=args.confirm, force=args.force)
    report["at"] = time.strftime("%Y-%m-%dT%H:%M:%S")

    sidecar = sidecar_path(path)
    sidecar.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if report.get("status") in ("created", "dry_run"):
        append_log({
            "task_id": report.get("task_id"), "bdbh": report.get("bdbh"),
            "status": "created" if report["status"] == "created" else "dry_run",
            "at": report["at"], "artifact": str(path),
            "customer": report["payload"].get("customer"),
        })
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") in ("created", "dry_run") else 1


if __name__ == "__main__":
    sys.exit(main())
