"""pytest 配置（CQR_Web 自己的测试）。

CQR_Web 是**独立项目**，但它的数据与权威字典仍来自技能仓库：

- `../scripts/`   → `erp_schema`（ERP 字段字典/校验）、`internal_fields`、`main`（TEMPLATE_PATH）
- `..`（仓库根）  → 让 `import CQR_Web` 成立（没做成 pip 包，靠路径）

这样「校验与判定只有一份实现」不会被复制成两份 —— 复制一份就等着漂。
"""
import sys
from pathlib import Path

PROJ_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = PROJ_ROOT.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

for path in (REPO_ROOT, SCRIPTS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
