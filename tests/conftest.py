"""pytest 配置：把仓库根与 scripts/ 加进 import 路径。

- `scripts/`  → 让测试能直接 import 各脚本模块（main / erp_schema / customer_profile …）
- 仓库根      → 让测试能 import `erp_view` 包（查看器，1.0.26 起从 scripts/ 搬进包）
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

for path in (REPO_ROOT, SCRIPTS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
