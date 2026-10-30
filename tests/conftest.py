"""pytest 配置：把仓库根与 scripts/ 加进 import 路径。

- `scripts/`  → 让测试能直接 import 各脚本模块（main / erp_schema / customer_profile …）
- 仓库根      → 让测试能 import `CQR_Web` 包（查看器，1.0.26 起从 scripts/ 搬进包）

★ 2026-10-29：ERP 导出文件改成**数组**（一个文件可含多个产品），
读产物一律走 `load_erp(path)`（返回 list）；只关心单条的测试用 `load_one(path)`。
"""
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

for path in (REPO_ROOT, SCRIPTS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def load_erp(path) -> list:
    """读 ERP 导出文件 → 记录列表（数组；老的单对象文件读成 1 条）。"""
    from erp_bundle import load_bundle

    return load_bundle(path)


def load_one(path) -> dict:
    """读 ERP 导出文件的第一条记录（绝大多数单产品测试用它）。"""
    records = load_erp(path)
    assert records, "导出文件里没有任何产品记录"
    return records[0]


@pytest.fixture
def erp_records():
    """给测试用的读取函数（避免每个测试文件各写一份 json.loads）。"""
    return load_erp
