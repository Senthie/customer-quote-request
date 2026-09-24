#!/usr/bin/env python3
"""转发壳 —— 查看器已经搬到 `erp_view/` 包（Flask 版，1.0.26）。

**这里没有任何逻辑**（避免出现第二份实现去跟 `erp_view/` 漂）：

    erp_view/data.py   ← 扫描 / 路径安全 / ERP 字典 / 交付闸门
    erp_view/app.py    ← Flask 应用（create_app / main）

留这个文件的唯一目的是让既有命令与文档继续可用：

    uv run ./scripts/erp_viewer.py                 # 本文件
    uv run python -m erp_view                      # 等价，更直白
    uv run python -m erp_view --port 9000 --no-open
"""
from __future__ import annotations

import sys
from pathlib import Path

# 仓库根进 sys.path，才 import 得到 erp_view 包（本项目没做成 pip 包）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from erp_view.app import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
