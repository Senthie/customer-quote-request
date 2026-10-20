"""钉住 `docs/CPMS_规范与样例集.md` 里的 7 份样例：**文档里的示例必须真能跑过 linter**。

理由（同 `test_cpms_lint.py` 开头）：规范文档里贴的示例如果自己不合规，规范就是废纸。
这份测试直接从 markdown 里抽代码块，按同一份 `assets/cpms_spec.yaml` 校验，
所以「文档改了一行、忘了同步字段」会当场红，而不是等 agent 照抄错样例。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import cpms_lint as L

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC = REPO_ROOT / "docs" / "CPMS_规范与样例集.md"
SPEC = L.load_spec()

FIELD_RE = re.compile(r"([A-Za-z_]+)=(\"[^\"]*\"|'[^']*'|[^\s`]+)")


def parse_samples(text: str) -> list[tuple[str, dict, str]]:
    """按「### 样例」分段：取段内的 **ERP 字段** 行 + 第一个代码块。"""
    lines = text.split("\n")
    out: list[tuple[str, dict, str]] = []
    i = 0
    while i < len(lines):
        if not lines[i].startswith("### 样例"):
            i += 1
            continue
        title = lines[i]
        fields: dict[str, str] = {}
        block: str | None = None
        j = i + 1
        while j < len(lines) and not lines[j].startswith(("### ", "## ")):
            line = lines[j]
            if line.startswith("**ERP 字段**"):
                fields = {k: v.strip("\"'") for k, v in FIELD_RE.findall(line)}
            elif line.startswith("```") and block is None:
                end = j + 1
                while end < len(lines) and not lines[end].startswith("```"):
                    end += 1
                block = "\n".join(lines[j + 1:end])
                j = end
            j += 1
        if block is not None:
            out.append((title, fields, block))
        i = j
    return out


SAMPLES = parse_samples(DOC.read_text(encoding="utf-8"))


def test_文档存在且有七份样例():
    assert DOC.exists(), f"缺少规范文档：{DOC}"
    assert len(SAMPLES) == 7, [t for t, _, _ in SAMPLES]


@pytest.mark.parametrize("title,fields,cpms", SAMPLES, ids=[s[0][:24] for s in SAMPLES])
def test_每份样例零违规(title, fields, cpms):
    findings = L.lint_text(cpms, SPEC, fields)
    errors = [f"[{f.code}] L{f.line} {f.msg}" for f in findings if f.level == "error"]
    warns = [f"[{f.code}] L{f.line} {f.msg}" for f in findings if f.level != "error"]
    assert not errors, f"{title} 有 {len(errors)} 个 error：\n" + "\n".join(errors)
    assert not warns, f"{title} 有 {len(warns)} 个 warn：\n" + "\n".join(warns)


@pytest.mark.parametrize("title,fields,cpms", SAMPLES, ids=[s[0][:24] for s in SAMPLES])
def test_样例都带齐概要行所需字段(title, fields, cpms):
    for key in ("Drop_SJFX", "CD", "KD", "GD", "TZS", "YYS"):
        assert key in fields, f"{title} 缺少字段 {key} —— 样例必须能核对概要行"
    assert "产品规格：" in cpms, f"{title} 概要不完整"
