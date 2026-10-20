"""Flask 应用（路由层）—— ERP 导出 JSON 浏览器。

    data.py   ← 唯一实现（扫描 / 路径安全 / 字典 / 交付闸门）
    app.py    ← 只管 HTTP（本文件：路由、状态码、缓存头、CLI）
    templates/index.html ← 只管显示（零外部依赖的原生 JS）

为什么这里要有服务端：浏览器**不允许** `file://` 页面列出本地目录（同源限制），
所以「自动扫描 customer-files/processed」只能靠一个本地 HTTP 服务把目录列表喂给页面。

用法：
    uv run ./scripts/erp_viewer.py                # 转发壳（老命令照旧）
    uv run python -m CQR_Web                     # 等价，更直白
    uv run python -m CQR_Web --port 9000         # 换端口
    uv run python -m CQR_Web --no-open           # 无头/远程：不开浏览器
    uv run python -m CQR_Web --dir <其它目录>     # 改扫描根目录

接口（页面 templates/index.html 消费）：
    GET /                    → 页面本体
    GET /api/exports         → 扫描结果（每份产物的摘要，最新在前）
    GET /api/export?path=…   → 某一份产物的完整 JSON（`path` 是相对扫描根的路径）
    GET /api/schema          → ERP 字段字典（标签/类型/合法取值）+ 内部字段 + 分组

安全：所有 `path` 参数都在 data.resolve_export() 里做 resolve 后前缀校验，出不了扫描根。
"""
from __future__ import annotations

import argparse
import errno
import json
import logging
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any, Optional

from flask import Flask, jsonify, render_template, request
from flask.json.provider import DefaultJSONProvider
from werkzeug.exceptions import HTTPException, NotFound

from .data import (
    DEFAULT_PROCESSED_DIR,
    build_schema_payload,
    build_validation_payload,
    delivery_gates,
    read_config_version,
    resolve_export,
    scan_exports,
)

APP_NAME = "ERP 报价需求单导出浏览器"


class PrettyJSONProvider(DefaultJSONProvider):
    """JSON 输出口径：不转义中文、保持我们构造的键序、缩进 2 格。

    为什么显式定这三个：
      * 默认 `ensure_ascii=True` 会把中文转成 `\\uXXXX`，`curl` 直读没法看；
      * 默认 `sort_keys=True` 会把按业务顺序构造的键打乱（诊断时读起来对不上）；
      * `compact=False` → Flask 自己就按 `indent=2` 输出（不必自己覆写 `dumps`；
        默认的 `compact=None` 只在 debug 下才缩进，非 debug 是 `(",", ":")` 紧凑格式）。
    """

    ensure_ascii = False
    sort_keys = False
    compact = False


def create_app(processed_dir: Optional[Path | str] = None) -> Flask:
    """应用工厂。

    `processed_dir` 显式传入（测试、`--dir`）→ 存进 `app.config`，路由从那里读。
    为什么不学以前那样改模块级全局（`global PROCESSED_DIR`）：全局既脏又不可测，
    而且 `--dir` 与测试会互相污染。
    """
    root = Path(processed_dir).expanduser().resolve() if processed_dir else DEFAULT_PROCESSED_DIR

    app = Flask(__name__)          # template_folder 默认 = CQR_Web/templates
    app.config["PROCESSED_DIR"] = root
    app.config["APP_NAME"] = APP_NAME

    # 中文别转义（页面直接显示）、键序保持我们构造的顺序、缩进 2 格方便 `curl` 直读。
    # Flask 官方推荐的改法就是子类化 provider（实例属性 `indent` 在 Flask 2.3 起没有了）
    app.json = PrettyJSONProvider(app)

    # 内部工具：不要让浏览器缓存住产物（每跑一单目录就变）
    @app.after_request
    def _no_store(resp):
        resp.headers["Cache-Control"] = "no-store"
        return resp

    # 只留 API 调用一行日志（静态请求太吵），跟旧版 http.server 行为一致
    @app.before_request
    def _log_api():
        if request.path.startswith("/api/"):
            app.logger.info("%s %s", request.method, request.full_path.rstrip("?"))

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/exports")
    def api_exports():
        root_now = current_root()
        items = scan_exports(root_now)
        return jsonify(root=str(root_now), count=len(items), exports=items)

    @app.get("/api/export")
    def api_export():
        relpath = request.args.get("path", "")
        try:
            target = resolve_export(relpath, current_root())
        except ValueError as exc:
            return jsonify(error=str(exc)), 404
        try:
            data: Any = json.loads(target.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            return jsonify(error=f"解析失败: {type(exc).__name__}: {exc}"), 422
        if not isinstance(data, dict):
            return jsonify(error=f"顶层不是对象（是 {type(data).__name__}），看不了字段"), 422
        return jsonify(
            relpath=relpath,
            data=data,
            # 取值校验用权威实现（erp_schema.validate_result），页面不自己比枚举
            validation=build_validation_payload(target, data),
            # 交付闸门同样只在服务端算一份（页面只显示）
            incomplete=delivery_gates(data),
        )

    @app.get("/api/schema")
    def api_schema():
        return jsonify(build_schema_payload())

    # 未知路径也回 JSON（页面是按 JSON 读错的）；文案保持中文，跟旧版一致
    @app.errorhandler(HTTPException)
    def _http_error(exc: HTTPException):
        if isinstance(exc, NotFound):
            msg = f"未知路径: {request.path}"
        else:
            msg = exc.description or exc.name
        return jsonify(error=msg, code=exc.code), exc.code or 500

    def current_root() -> Path:
        return Path(app.config["PROCESSED_DIR"])

    app.current_root = current_root   # type: ignore[attr-defined]  # 给测试/CLI 用
    return app


def _port_in_use(host: str, port: int) -> bool:
    """能不能在 (host, port) 上 bind —— 起服务前先问一次。

    为什么不信 `app.run()` 抛的 OSError：werkzeug 在 `make_server` 里**自己**把
    EADDRINUSE 处理掉了（打印英文 "Address already in use" 然后 `sys.exit(1)`），
    异常根本传不到我们的 `except OSError` —— 中文提示会变成死代码（实测踩到）。
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return True
    return False


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=f"{APP_NAME}（Flask 版）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8765, help="端口（默认 8765）")
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--dir", help="改扫描的根目录（默认 customer-files/processed）")
    args = parser.parse_args(argv)

    root = Path(args.dir).expanduser().resolve() if args.dir else DEFAULT_PROCESSED_DIR
    if not root.exists():
        print(f"⚠️  扫描目录不存在: {root}", file=sys.stderr)

    # 端口先探一次：给中文提示 + 退出码 1（别让 werkzeug 的英文报错先出去）
    if _port_in_use(args.host, args.port):
        print(f"❌ 起不了服务（{args.host}:{args.port}）: 端口已被占用", file=sys.stderr)
        print("   换个端口试试：--port 8766", file=sys.stderr)
        return 1

    app = create_app(root)

    # werkzeug 的每请求日志降噪（本应用自己只打 API 行）
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    url = f"http://{args.host}:{args.port}/"
    print(f"ERP 导出浏览器: {url}")
    print(f"  扫描目录: {root}")
    print(f"  找到产物: {len(scan_exports(root))} 份")
    cfg = read_config_version()
    print(f"  当前配置版本: {cfg if cfg is not None else '(读不到)'}")
    print("  Ctrl-C 退出")

    if not args.no_open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        app.run(host=args.host, port=args.port, debug=False, use_reloader=False, threaded=True)
    except OSError as exc:
        if exc.errno in (errno.EADDRINUSE, errno.EADDRNOTAVAIL):
            print(f"❌ 起不了服务（{args.host}:{args.port}）: {exc}", file=sys.stderr)
            print("   换个端口试试：--port 8766", file=sys.stderr)
        else:
            print(f"❌ 起不了服务: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已停止。")
    return 0
