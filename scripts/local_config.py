"""内网端点/密钥类配置的解析：**环境变量 > 机器本地 yaml（不入库）> 仓库内置默认值**。

为什么单独一个文件：`assets/vlm_prompts.yaml`（提示词）和 `scripts/erp_push.py`
（ERP MCP 地址）原来都把内网 IP 写死在仓库里，而这是一个**公开** GitHub 仓库
（2026-11-20 核实：未认证访问 api.github.com 返回 200）。逻辑只写一份，两个调用方共用。

约定：
- 机器本地文件：`assets/<名字>.local.yaml`，形如
  ```yaml
  endpoint: http://<内网IP>:<端口>/v1/chat/completions
  model: Qwen3-VL-4B-Instruct
  url: https://<内网IP>:8443/mcp
  ```
  已进 .gitignore；新机器照 `assets/*.local.example.yaml` 复制一份即可。
- 环境变量：同名大写，`CQR_VLM_ENDPOINT` / `CQR_VLM_MODEL` / `ERP_MCP_URL`。
- 仓库里的默认值一律留空 —— 宁可启动时报「没配端点」，也不要把内网地址推到公网。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

REPO = Path(__file__).resolve().parent.parent
ASSETS = REPO / "assets"


def local_config_path(name: str) -> Path:
    """`erp_endpoint` → assets/erp_endpoint.local.yaml"""
    return ASSETS / f"{name}.local.yaml"


def load_local(name: str) -> dict[str, Any]:
    p = local_config_path(name)
    if not p.is_file():
        return {}
    try:
        import yaml
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:  # 本地文件写坏了不该让流水线崩，交给上层用默认值/报错
        return {}


def resolve(name: str, key: str, env: str, default: str = "") -> str:
    """按优先级取一个端点类配置项。取不到就返回 default（通常是空串）。"""
    val = os.environ.get(env, "").strip()
    if val:
        return val
    local = load_local(name).get(key)
    if isinstance(local, str) and local.strip():
        return local.strip()
    return default


def resolve_many(name: str, mapping: dict[str, str], defaults: Optional[dict[str, str]] = None) -> dict[str, str]:
    """一次解析一组：`mapping` = {yaml 键: 环境变量名}。"""
    defaults = defaults or {}
    return {key: resolve(name, key, env, defaults.get(key, "")) for key, env in mapping.items()}
