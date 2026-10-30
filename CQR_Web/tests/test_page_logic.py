"""页面**逻辑**测试（不需要浏览器）：`page_harness.js` 的 pytest 壳。

为什么单独一个文件：`test_viewer.py` 里的页面测试只读 HTML 文本、断言「某个函数名
出现了」。那证明不了「点 Tab 真的会换内容」。这份把页面脚本真跑起来（最小 DOM shim），
按 `/api/exports` / `/api/export` 的真实返回形状渲染，逐项核对：

    * 列表是**任务列表**（一个任务一行、标出「N 条产品」、把每条产品列出来）；
    * 详情是**多个产品记录 Tab**（条数、默认激活、切换后标题/闸门/校验错误都跟着换）。

没有 node 就 skip —— 这条不是每次必跑的门，但有 node 的环境能白捡一层保护。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

PKG_DIR = Path(__file__).resolve().parent.parent
PAGE_PATH = PKG_DIR / "templates" / "index.html"
HARNESS = Path(__file__).resolve().parent / "page_harness.js"


def test_页面逻辑_任务列表与产品Tab(tmp_path):  # noqa: ARG001
    node = shutil.which("node")
    if not node:
        pytest.skip("没有 node（可选依赖）—— 页面逻辑测试跳过")

    proc = subprocess.run([node, str(HARNESS), str(PAGE_PATH)],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, (
        "页面逻辑测试失败：\n" + (proc.stdout or "") + (proc.stderr or ""))


def test_页面逻辑_脚本没有语法错误(tmp_path):
    """把内联 script 抽出来交给 node --check —— 一个中文引号打错就整页白屏。"""
    node = shutil.which("node")
    if not node:
        pytest.skip("没有 node（可选依赖）—— 语法检查跳过")

    import re

    html = PAGE_PATH.read_text(encoding="utf-8")
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert scripts, "页面里没有内联 <script>"
    target = tmp_path / "page.js"
    target.write_text("\n".join(scripts), encoding="utf-8")

    proc = subprocess.run([node, "--check", str(target)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
