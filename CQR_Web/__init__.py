"""CQR_Web —— ERP 报价需求单导出浏览器的 Flask 应用包。

     CQR_Web/
     ├── data.py              ← 唯一实现：扫描 / 路径安全 / ERP 字典 / 交付闸门（不依赖 Flask）
     ├── app.py               ← Flask 工厂 + 路由 + CLI（create_app / main）
     ├── templates/index.html ← 页面本体（零外部依赖的原生 JS）
     └── __main__.py          ← `uv run python -m CQR_Web`

分层的意思（SKILL §4.10「判定只有一份实现」）：页面**不许**自己算取值合法性、
也不许自己算交付闸门 —— 都由 `data.py` 算好、`app.py` 传出去，页面只显示。

起服务：
    uv run ./scripts/erp_viewer.py         # 转发壳（老命令照旧）
    uv run python -m CQR_Web              # 等价
"""
from __future__ import annotations

from .app import APP_NAME, PrettyJSONProvider, create_app, main
from .data import (
    CPMC_ZH_PLACEHOLDER,
    DEFAULT_PROCESSED_DIR,
    EXPORT_NAME_RE,
    FIELD_GROUPS,
    INTERNAL_GROUP_HINT,
    INTERNAL_GROUP_NAME,
    REPO_ROOT,
    SUPERSEDED_PREFIX,
    build_schema_payload,
    build_validation_payload,
    delivery_gates,
    read_config_version,
    resolve_export,
    scan_exports,
)

__all__ = [
    "APP_NAME",
    "CPMC_ZH_PLACEHOLDER",
    "DEFAULT_PROCESSED_DIR",
    "EXPORT_NAME_RE",
    "FIELD_GROUPS",
    "INTERNAL_GROUP_HINT",
    "INTERNAL_GROUP_NAME",
    "PrettyJSONProvider",
    "REPO_ROOT",
    "SUPERSEDED_PREFIX",
    "build_schema_payload",
    "build_validation_payload",
    "create_app",
    "delivery_gates",
    "main",
    "read_config_version",
    "resolve_export",
    "scan_exports",
]
