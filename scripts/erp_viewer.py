#!/usr/bin/env python3
"""
erp_viewer.py - ERP 导出 JSON 浏览器（自动扫描 customer-files/processed）

业务 2026-09-24：
  * 「有一个旧的展示 ERP JSON 格式的 html，但是我们的导出结构发生了变化，需要修改它」
  * 「让它可以自动扫描 customer-quote-request/customer-files/processed 的导出 ERP 文件」

为什么需要一个服务端：
    浏览器**不允许** `file://` 页面列出本地目录（同源/CORS 限制）。
    所以「自动扫描」只能靠一个本地 HTTP 服务把目录列表喂给页面。
    这个脚本只用标准库（http.server），不引入任何依赖。

用法：
    uv run ./scripts/erp_viewer.py              # 起服务 + 自动打开浏览器
    uv run ./scripts/erp_viewer.py --port 9000  # 换端口
    uv run ./scripts/erp_viewer.py --no-open    # 不自动开浏览器（远程/无头）

接口（前端 scripts/ERP_data_show.html 消费）：
    GET /                    → 页面本体
    GET /api/exports         → 扫描结果（每份产物的摘要，最新在前）
    GET /api/export?path=…   → 某一份产物的完整 JSON（`path` 是相对 processed/ 的路径）
    GET /api/schema          → ERP 字段字典（标签/类型/合法取值）+ 内部字段 + 分组

安全：所有 `path` 参数都做 resolve 后前缀校验，出不了 processed 目录。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import webbrowser
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

REPO_ROOT = SCRIPT_DIR.parent
PROCESSED_DIR = REPO_ROOT / "customer-files" / "processed"
PAGE_PATH = SCRIPT_DIR / "ERP_data_show.html"
SUPERSEDED_PREFIX = "_superseded"

# `main.py` 给 CPMC_ZH 里的中文品名留的可见占位符（档案 cpmc.needs_agent）
CPMC_ZH_PLACEHOLDER = "【中文品名待定】"

# 产物文件名：ERP_<task_id>_<YYYYmmdd>_<HHMMSS>.json
EXPORT_NAME_RE = re.compile(r"^ERP_(?P<task_id>.+)_(?P<date>\d{8})_(?P<time>\d{6})\.json$")


# ═══════════════════════════════════════════════════════════════════════
# 字段分组（纯展示用）
#
# 为什么分组放在这里而不是 HTML 里：加了新 ERP 字段后，如果分组只写在前端，
# 新字段会**悄悄不显示**。放这里 + 有一条测试钉住「59 个字段全部分到组、且只分一次」，
# 漏掉的话测试直接红。没归组的键会落到「其他」组，仍然可见。
# ═══════════════════════════════════════════════════════════════════════
FIELD_GROUPS: list[dict[str, Any]] = [
    {
        "name": "ERP 自动带出",
        "hint": "表单上不显示，导入接口自带 —— 默认留空",
        "keys": ["BDBH", "YWY", "KHZL", "GDY", "CPMX", "Remark"],
    },
    {
        "name": "产品信息",
        "hint": "产品大类/类别、书脊方向、装订方式、产品名称",
        "keys": ["CPDL", "CPLB", "Drop_SJFX", "ZDFS", "CPMC", "ECPMC"],
    },
    {
        "name": "款数与数量",
        "hint": "TZS 共有款数；CPSL1–CPSL10 逐款数量",
        "keys": ["TZS", "CPSL1", "CPSL2", "CPSL3", "CPSL4", "CPSL5",
                 "CPSL6", "CPSL7", "CPSL8", "CPSL9", "CPSL10"],
    },
    {
        "name": "语言文字",
        "hint": "每款语言种数",
        "keys": ["YYS"],
    },
    {
        "name": "规格尺寸",
        "hint": "单位与长/宽/高（mm）",
        "keys": ["DWMC", "CD", "KD", "GD"],
    },
    {
        "name": "计价与币种",
        "hint": "客户目标价与币种",
        "keys": ["BZ", "KHMBDJ"],
    },
    {
        "name": "包装与运输",
        "hint": "包装方式/台板/送货方式/纸箱相关/运输类型",
        "keys": ["BZFS", "TBLX", "SHFS", "ZXLB", "ZBLX", "ZXFS", "DKMC", "YSLX"],
    },
    {
        "name": "单号与日期",
        "hint": "客户单号/香港单号/返单号/交货与下单日期/地区",
        "keys": ["KHDH", "KHDDH", "OLD_KHDDH", "JHRQ", "WCRQ", "KHDQ", "SHDQ"],
    },
    {
        "name": "材质与合规",
        "hint": "FSC 类别、产品属性、产品类型",
        "keys": ["FSCMC", "CPSX", "CPLX"],
    },
    {
        "name": "配件与排序",
        "hint": "是否有配件 / 配件描述 / 排序号",
        "keys": ["SFYPJ", "Remark1", "PXH"],
    },
    {
        "name": "勾选项",
        "hint": "布尔字段（chk 前缀）",
        "keys": ["chkXBJF", "chkZDBJF", "chkBSYTB", "chkBSYJZX"],
    },
    {
        "name": "描述与备注",
        "hint": "产品描述（CPMS，agent 手写）/ 补充描述 / 修改记录 / 检测项目",
        "keys": ["CPMS", "BGMS", "Remark3", "Remark4"],
    },
]

# 内部导出字段在页面上单独成组（不是 ERP 表单字段）
INTERNAL_GROUP_NAME = "内部导出字段（我方自用）"
INTERNAL_GROUP_HINT = "ERP 字典里没有这些字段；--strip-internal 可去掉"


def _load_module(name: str):
    """按脚本目录内的名字加载模块（复用同一份权威数据，不抄第二份）。"""
    import importlib

    return importlib.import_module(name)


def read_config_version() -> Optional[int]:
    """当前配置版本 —— 读 customers/_defaults.yaml 的顶层 version（单一真源）。"""
    try:
        import yaml

        with open(REPO_ROOT / "customers" / "_defaults.yaml", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        v = raw.get("version")
        return v if isinstance(v, int) else None
    except Exception:  # noqa: BLE001 —— 只用于展示，读不到就不显示
        return None


# ── 扫描 ────────────────────────────────────────────────────────────────
def _sibling_validation(export_path: Path) -> Optional[dict[str, Any]]:
    """找产物同目录下的 `*.validation.json`（当次运行的权威校验报告）。

    注意：报告的文件名跟着**输入文件**（`<附件名>.validation.json`），
    不跟产物名（`ERP_<task_id>_<时间戳>.json`）—— 所以只能按目录找，不能按 stem 配。
    一个任务目录正常情况下恰好一份；多份取最新的。
    """
    try:
        cands = [p for p in export_path.parent.glob("*.validation.json") if p.is_file()]
    except OSError:
        return None
    if not cands:
        return None
    newest = max(cands, key=lambda p: p.stat().st_mtime)
    try:
        with open(newest, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 —— 报告坏了不该影响看产物
        return None


def build_validation_payload(export_path: Path, data: dict[str, Any]) -> dict[str, Any]:
    """这份产物的取值校验结果。

    ★ 为什么不让页面自己比枚举（真踩过的坑，2026-09-24）：
      `CPLB` 我们的口径是「编号;名称」（`26.01;普通蛇圈书`），而 `legal_values("CPLB")`
      返回的是树里 88 个**裸名称**。页面拿字符串直接比 → **每一份产物的 CPLB 都会被
      误标成「不在字典枚举内」**。真正认可这种写法的是 `erp_schema.validate_result()`
      （它按 `;` 切出编号再查树）。所以：校验只有一个权威实现，页面只负责显示。

    取值优先级：
      1. 产物同目录的 `*.validation.json` —— **当次运行**的权威报告（含客户档案豁免）
      2. 没有报告（旧产物 / 手动导入）→ 现算一次，但要标明「未按客户档案豁免必填」
    """
    issues_module = _load_module("erp_schema")
    internal_keys = [f["key"] for f in _load_module("internal_fields").load_internal_fields()]

    report = _sibling_validation(export_path)
    if report is not None:
        return {
            "source": "run_report",
            "customer": report.get("customer"),
            "checked_fields": report.get("checked_fields"),
            "errors": report.get("errors") or [],
            "warnings": report.get("warnings") or [],
            "reminders": report.get("reminders") or [],
            "note": None,
        }

    try:
        issues = issues_module.validate_result(data, internal_fields=internal_keys)
    except Exception as exc:  # noqa: BLE001 —— 校验器出错不该让产物看不了
        return {"source": "unavailable", "customer": None, "checked_fields": None,
                "errors": [], "warnings": [], "reminders": [],
                "note": f"现算校验失败: {type(exc).__name__}: {exc}"}
    return {
        "source": "recomputed",
        "customer": None,
        "checked_fields": len([k for k in data if k not in internal_keys]),
        "errors": [i for i in issues if i.get("level") == "error"],
        "warnings": [i for i in issues if i.get("level") != "error"],
        "reminders": [],
        "note": "无当次运行报告（旧产物或手动导入）→ 现场重算，**未按客户档案豁免必填**"
                "（如 NGV 的 KHDH 本来就该空）；有 error 先看是不是这个原因。",
    }


def delivery_gates(data: dict[str, Any]) -> list[str]:
    """交付闸门：产物里「必须由 agent 亲自填」的地方还是空的 → 未完成，不得交付。

    两处刻意不写（都不是缺陷，是分工）：

    1. `CPMS` —— 1.0.11 起脚本一律不生成（业务确认无法用 Python 统一处理，
       `build_cpms_draft` / `CPMS_SECTIONS` 已删除）。
    2. `CPMC_ZH` 里的中文品名 —— 1.0.22 起由 agent 每单拟，脚本只留可见占位符。

    所以**管道刚跑完的产物天然处在「未完成」状态**。「CPMS 为空的 JSON 视为未完成，
    不得交付」此前只活在文档里 —— 结果 2026-09-24 一份样本试跑的未完成产物在
    `processed/` 里躺了半天，是业务问出来的。现在把它做成看得见的闸门。
    """
    gates: list[str] = []
    cpms = data.get("CPMS")
    if not isinstance(cpms, str) or not cpms.strip():
        gates.append("CPMS 未写 —— 脚本不生成，必须 agent 亲自补写（SKILL 第 5 节）")
    cpmc_zh = data.get("CPMC_ZH")
    if isinstance(cpmc_zh, str) and CPMC_ZH_PLACEHOLDER in cpmc_zh:
        gates.append(f"CPMC_ZH 仍是占位符 {CPMC_ZH_PLACEHOLDER} —— 中文品名必须 agent 拟")
    return gates


def _summarize(path: Path, root: Path) -> dict[str, Any]:
    """读一份产物，抽出列表页要用的摘要。文件坏了也要能列出来（标 broken）。"""
    rel = path.relative_to(root)
    m = EXPORT_NAME_RE.match(path.name)
    stat = path.stat()
    item: dict[str, Any] = {
        "relpath": rel.as_posix(),
        "filename": path.name,
        "task_id": m.group("task_id") if m else path.parent.name,
        "dir": rel.parent.as_posix(),
        "mtime": stat.st_mtime,
        "mtime_iso": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        "size": stat.st_size,
        "superseded": any(p.startswith(SUPERSEDED_PREFIX) for p in rel.parts),
        "broken": None,
    }

    # 当次运行的校验报告（有就带上，列表页能一眼看出哪份有 error）
    report = _sibling_validation(path)
    if report is not None:
        summary = report.get("summary") or {}
        item["customer"] = report.get("customer")
        item["validation"] = {
            "source": "run_report",
            "error": int(summary.get("error") or 0),
            "warn": int(summary.get("warn") or 0),
        }
    else:
        item["customer"] = None
        item["validation"] = None

    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError(f"顶层不是对象，是 {type(data).__name__}")
    except Exception as exc:  # noqa: BLE001
        item["broken"] = f"{type(exc).__name__}: {exc}"
        item["key_count"] = 0
        item["cpms_len"] = 0
        item["incomplete"] = []   # 读不出来就没法判交付状态（列表上另有「文件损坏」徽章）
        return item

    item.update({
        "key_count": len(data),
        # 内部字段（老产物可能没有 → None，页面上显示「缺」）
        "version": data.get("version"),
        "ID": data.get("ID"),
        "CPMC_ZH": data.get("CPMC_ZH"),
        "Reminder": data.get("Reminder"),
        # 一眼能认出来的业务字段
        "CPMC": data.get("CPMC"),
        "CPLB": data.get("CPLB"),
        "CPDL": data.get("CPDL"),
        "KHDH": data.get("KHDH"),
        "CPSL1": data.get("CPSL1"),
        "TZS": data.get("TZS"),
        # ★ 交付闸门（CPMS 未写 / 中文品名还是占位符）—— 空列表 = 可以交付
        "cpms_len": len(data["CPMS"]) if isinstance(data.get("CPMS"), str) else 0,
        "incomplete": delivery_gates(data),
    })
    return item


def scan_exports(root: Optional[Path] = None) -> list[dict[str, Any]]:
    """递归扫描 processed/ 下所有 ERP_*.json，最新在前。

    注意：root 默认值必须写 None、在里面回落到 PROCESSED_DIR。
    写成 `root: Path = PROCESSED_DIR` 会在**函数定义时**就把路径固定住，
    之后 --dir 或测试里的 monkeypatch 都改不动（已经踩过一次）。
    """
    root = Path(root) if root is not None else PROCESSED_DIR
    if not root.exists():
        return []
    items = [_summarize(p, root) for p in sorted(root.rglob("ERP_*.json")) if p.is_file()]
    items.sort(key=lambda it: (it["mtime"], it["relpath"]), reverse=True)
    return items


def resolve_export(relpath: str, root: Optional[Path] = None) -> Path:
    """把前端给的相对路径解析成真实文件；越界/不存在都抛异常。"""
    root = Path(root) if root is not None else PROCESSED_DIR
    if not relpath:
        raise ValueError("缺少 path 参数")
    candidate = (root / relpath).resolve()
    root_resolved = root.resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise ValueError(f"路径越界（只允许 processed/ 内的文件）: {relpath}")
    if not candidate.is_file():
        raise ValueError(f"文件不存在: {relpath}")
    if candidate.suffix.lower() != ".json":
        raise ValueError(f"只允许读 .json: {relpath}")
    return candidate


# ── 字典 ────────────────────────────────────────────────────────────────
def build_schema_payload() -> dict[str, Any]:
    """ERP 字段字典 + 内部字段 + 分组 —— 前端据此渲染下拉与标签。"""
    erp_schema = _load_module("erp_schema")
    internal_fields = _load_module("internal_fields")

    spec_all: dict[str, Any] = erp_schema.load_schema().get("fields") or {}
    fields: list[dict[str, Any]] = []
    for key, spec in spec_all.items():
        spec = spec or {}
        ftype = str(spec.get("type", "text"))
        legal: list[str] = []
        if ftype == "select":
            try:
                legal = [str(v) for v in erp_schema.legal_values(key)]
            except Exception:  # noqa: BLE001
                legal = []
        fields.append({
            "key": key,
            "label": str(spec.get("label", "")),
            "type": ftype,
            "required": spec.get("required"),
            "auto_filled": bool(spec.get("auto_filled")),
            "legal": legal,
        })

    return {
        "config_version": read_config_version(),
        "erp_fields": fields,
        "internal_fields": [
            {"key": str(f["key"]), "label": str(f.get("label", "")),
             "source": str(f.get("source", ""))}
            for f in internal_fields.load_internal_fields()
        ],
        "groups": FIELD_GROUPS,
        "internal_group": {"name": INTERNAL_GROUP_NAME, "hint": INTERNAL_GROUP_HINT},
    }


# ═══════════════════════════════════════════════════════════════════════
# HTTP
# ═══════════════════════════════════════════════════════════════════════
class Handler(SimpleHTTPRequestHandler):
    server_version = "ERPViewer/2"

    # 必须与 BaseHTTPRequestHandler.log_message 签名一致（参数名 format）
    def log_message(self, format: str, *args) -> None:  # noqa: A002
        # 默认实现太吵（每个静态请求一行）；只留 API 调用
        if "/api/" in (self.path or ""):
            sys.stderr.write(f"  {self.command} {self.path}\n")

    # ---- 工具 ----
    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, message: str, status: int = 400) -> None:
        self._send_json({"error": message}, status=status)

    # ---- 路由 ----
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        if route == "/":
            return self._serve_page()
        if route == "/api/exports":
            items = scan_exports()
            return self._send_json({
                "root": str(PROCESSED_DIR),
                "count": len(items),
                "exports": items,
            })
        if route == "/api/export":
            relpath = (query.get("path") or [""])[0]
            try:
                target = resolve_export(relpath)
            except ValueError as exc:
                return self._send_error_json(str(exc), 404)
            try:
                with open(target, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as exc:  # noqa: BLE001
                return self._send_error_json(f"解析失败: {type(exc).__name__}: {exc}", 422)
            if not isinstance(data, dict):
                return self._send_error_json(
                    f"顶层不是对象（是 {type(data).__name__}），看不了字段", 422)
            return self._send_json({
                "relpath": relpath,
                "data": data,
                # 取值校验用权威实现（erp_schema.validate_result），页面不自己比枚举
                "validation": build_validation_payload(target, data),
                # 交付闸门同样只在服务端算一份（页面只显示）
                "incomplete": delivery_gates(data),
            })
        if route == "/api/schema":
            return self._send_json(build_schema_payload())

        return self._send_error_json(f"未知路径: {route}", 404)

    def _serve_page(self) -> None:
        if not PAGE_PATH.exists():
            return self._send_error_json(f"找不到页面: {PAGE_PATH}", 500)
        body = PAGE_PATH.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    parser = argparse.ArgumentParser(description="ERP 导出 JSON 浏览器（自动扫描 processed/）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8765, help="端口（默认 8765）")
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--dir", help="改扫描的根目录（默认 customer-files/processed）")
    args = parser.parse_args()

    global PROCESSED_DIR  # noqa: PLW0603 —— 覆盖默认扫描目录
    if args.dir:
        PROCESSED_DIR = Path(args.dir).expanduser().resolve()

    if not PROCESSED_DIR.exists():
        print(f"⚠️  扫描目录不存在: {PROCESSED_DIR}", file=sys.stderr)

    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        print(f"❌ 起不了服务（{args.host}:{args.port}）: {exc}", file=sys.stderr)
        print("   换个端口试试：--port 8766", file=sys.stderr)
        return 1

    url = f"http://{args.host}:{args.port}/"
    found = len(scan_exports())
    print(f"ERP 导出浏览器: {url}")
    print(f"  扫描目录: {PROCESSED_DIR}")
    print(f"  找到产物: {found} 份")
    cfg = read_config_version()
    print(f"  当前配置版本: {cfg if cfg is not None else '(读不到)'}")
    print("  Ctrl-C 退出")

    if not args.no_open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
