"""CQR_Web（原 `erp_view/`）抽离成**独立项目**之后的边界测试。

业务 2026-10-20：「我们的 ERP view 只有看，没有上传，把 erp_view 改为 CQR_Web，
然后在里面开始新的编码任务」。

所以这里钉住三件事，防「抽离到一半又长回去」：
  1. `CQR_Web/` 是个能自己跑的项目（pyproject + README + 自己的 tests）；
  2. 技能侧**只留转发壳**，不再有第二份展示逻辑；
  3. 旧包名 `erp_view` 不许在任何地方复活（文档/代码/命令都统一叫 CQR_Web）。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROJ = REPO_ROOT / "CQR_Web"


def test_项目自带pyproject和README和测试():
    assert (PROJ / "pyproject.toml").is_file(), "CQR_Web 要是独立项目：得有 pyproject.toml"
    assert (PROJ / "README.md").is_file(), "得有一份能给别人看的 README"
    assert (PROJ / "tests").is_dir(), "项目自己的回归测试放在 CQR_Web/tests/"
    assert (PROJ / "tests" / "conftest.py").is_file(), "测试要靠 conftest 找到 ../scripts"


def test_包还是三层且data层不依赖flask():
    for name in ("__init__.py", "__main__.py", "data.py", "app.py",
                 "templates/index.html"):
        assert (PROJ / name).is_file(), f"缺 {name}"
    data_src = (PROJ / "data.py").read_text(encoding="utf-8")
    assert "import flask" not in data_src and "from flask" not in data_src


def test_技能侧只剩转发壳():
    shim = (REPO_ROOT / "scripts" / "erp_viewer.py").read_text(encoding="utf-8")
    assert "from CQR_Web.app import main" in shim
    assert len(shim.splitlines()) < 40
    assert not (REPO_ROOT / "erp_view").exists(), "旧的 erp_view 目录不许留着（会变两份实现）"


def test_旧包名不许复活():
    """`python -m erp_view` / `from erp_view` 一律不该再出现（`erp_viewer.py` 除外 —— 老命令）。"""
    hits = subprocess.run(
        ["grep", "-rn", "--include=*.py", "--include=*.md", "--include=*.html",
         "--exclude=test_cqr_web_boundary.py",      # 本文件自己就写着这些串
         "-e", "python -m erp_view", "-e", "import erp_view", "-e", "erp_view.app",
         "-e", "erp_view/data.py", str(REPO_ROOT)],
        capture_output=True, text=True).stdout.strip()
    assert hits == "", f"旧包名还在这些地方：\n{hits}"


def test_模块入口可跑():
    r = subprocess.run([sys.executable, "-m", "CQR_Web", "--help"],
                       cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert "--port" in r.stdout and "--dir" in r.stdout
