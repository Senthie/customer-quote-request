"""技能文档一致性：总纲 + 每个客户一份子技能（2026-09-24 拆分，1.0.19）。

业务要求「把 igl 与 ngv 的客户报价提取分开为两个子 skill 方便维护」。拆分之后
最容易坏的三件事，这个文件逐条钉住：

  1. **加了客户却忘了写子技能** —— 档案 ↔ 子技能必须一一对应；
  2. **子技能指错客户 / 丢了必需章节** —— 附件形态、坑、交付检查清单一个不能少；
  3. **客户专属内容回流到总纲** —— 总纲只留引擎与机制，否则会重新长成第二个客户档案。

（技能加载路径是软链接、cron 自愈那件事属于运维，由
`cqr_skill_link_watchdog.sh` 负责，不在这里断言 —— 测试不应依赖 ~/.hermes 路径。）
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ROOT_SKILL = REPO_ROOT / "SKILL.md"
SKILLS_DIR = REPO_ROOT / "skills"
CUSTOMERS_DIR = REPO_ROOT / "customers"

sys_path_ok = True  # scripts/ 由 conftest.py 注入
from customer_profile import list_profiles, load_profile  # noqa: E402

FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def _sub_skill_path(customer_id: str) -> Path:
    return SKILLS_DIR / f"customer-quote-{customer_id}" / "SKILL.md"


def _split_frontmatter(text: str) -> tuple:
    m = FRONTMATTER.match(text)
    assert m, "SKILL.md 必须带 YAML frontmatter（--- 包裹）"
    body = text[m.end():]
    return m.group(1), body


def _frontmatter_value(front: str, key: str) -> str:
    m = re.search(rf"^{key}:\s*(.+)$", front, re.MULTILINE)
    return m.group(1).strip() if m else ""


def _preference_section(text: str) -> str | None:
    """取出子技能里「客户偏好表 / 偏好表」那一节（到下一个 `##` 标题为止）。

    必须限界：不限的话会一路扫到后面的「字段来源对照」表（那里面本来就会提到
    CPMS 之类的字段，不是偏好表条目），假阳性。
    """
    lines = text.splitlines()
    start = None
    for idx, line in enumerate(lines):
        if line.startswith("#") and "偏好表" in line:
            start = idx
            break
    if start is None:
        return None
    out = [lines[start]]
    for line in lines[start + 1:]:
        if line.startswith("## "):
            break
        out.append(line)
    return "\n".join(out)


# ── ① 档案 ↔ 子技能一一对应 ─────────────────────────────────────────────
def test_每个客户档案都有对应子技能():
    for cid in list_profiles():
        path = _sub_skill_path(cid)
        assert path.exists(), (
            f"客户档案 customers/{cid}.yaml 没有对应的子技能 {path.relative_to(REPO_ROOT)}。"
            f"新增客户后请照 skills/customer-quote-ngv/SKILL.md 的结构写一份"
        )


def test_子技能不会指向不存在的客户():
    dirs = sorted(p.name for p in SKILLS_DIR.glob("customer-quote-*") if p.is_dir())
    known = {f"customer-quote-{cid}" for cid in list_profiles()}
    assert dirs, "skills/ 下应至少有一个客户子技能"
    extra = [d for d in dirs if d not in known]
    assert extra == [], f"这些子技能没有对应的客户档案：{extra}（档案删了或写错名字？）"


def test_子技能名与_frontmatter_一致():
    for cid in list_profiles():
        text = _sub_skill_path(cid).read_text(encoding="utf-8")
        front, _ = _split_frontmatter(text)
        name = _frontmatter_value(front, "name")
        assert name == f"customer-quote-{cid}", (
            f"frontmatter name={name!r} 必须等于目录名 customer-quote-{cid}"
        )
        assert re.fullmatch(r"[a-z0-9-]+", name), f"{name!r} 只能是小写字母/数字/连字符"
        desc = _frontmatter_value(front, "description")
        assert desc and "customer-quote-request" in desc, (
            f"{name} 的 description 必须说明何时加载，并回指总纲 skill customer-quote-request"
        )


# ── ② 子技能内容完整、找得到唯一真源 ────────────────────────────────────
@pytest.mark.parametrize("cid", list_profiles())
def test_子技能必备章节齐全(cid):
    text = _sub_skill_path(cid).read_text(encoding="utf-8")
    for section in ("附件长什么样", "坑", "交付检查清单"):
        assert section in text, f"customer-quote-{cid} 缺少「{section}」章节"
    assert "customer-quote-request" in text, f"customer-quote-{cid} 必须回指总纲"
    assert f"customers/{cid}.yaml" in text, (
        f"customer-quote-{cid} 必须指明唯一真源 customers/{cid}.yaml（改行为改档案，不改文档）"
    )
    assert "uv run" in text, f"customer-quote-{cid} 里应给出可执行的回归/处理命令"


@pytest.mark.parametrize("cid", list_profiles())
def test_子技能不泄露别的客户的专属细节(cid):
    """子技能里只该谈自己那一家（提到别人只允许出现在「别混」这类提醒里）。"""
    text = _sub_skill_path(cid).read_text(encoding="utf-8")
    others = [o for o in list_profiles() if o != cid]
    for other in others:
        assert f"customers/{other}.yaml" not in text, (
            f"customer-quote-{cid} 里出现了 customers/{other}.yaml —— 客户细节别串台"
        )


def test_子技能里的偏好表字段名都对得上档案():
    """子技能里若列了「客户偏好表」，字段名必须是该档案 preferences 里真实存在的。

    漂移的典型形态（这次拆分最容易出的错）：
      * 子技能写了 `YSLX`／`PZH` 这种档案里没有的字段（拼错或抄了别家）；
      * 口径变了、子技能列了新字段但档案没加（文档先行、落库没跟上）。
    数值级别的强校验在 test_customer_profile.py::test_偏好表_每个已声明档案内部一致，
    这里只管「名字有没有对上」。
    """
    tpl_keys = set(json.loads(
        (REPO_ROOT / "assets" / "add_customer_quote_request.json").read_text(encoding="utf-8")))
    for cid in list_profiles():
        profile = load_profile(cid)
        text = _sub_skill_path(cid).read_text(encoding="utf-8")
        section = _preference_section(text)
        if section is None:
            continue          # 有些子技能只让读者去看 --prefs，不列字段
        known = {r["field"] for r in profile.preference_table()}
        for line in section.splitlines():
            if not line.strip().startswith("|"):
                continue
            cells = [c.strip().strip("*` ") for c in line.strip().strip("|").split("|")]
            if not cells:
                continue
            first = cells[0]
            if first in tpl_keys and first not in known:
                raise AssertionError(
                    f"customer-quote-{cid} 的偏好表里列了 `{first}`，"
                    f"但档案 preferences 里没有这个字段"
                    f"（新增/改口径请先写进 customers/{cid}.yaml 的 preferences）"
                )


# ── ③ 总纲：路由完整、客户细节不回流 ────────────────────────────────────
def test_总纲路由表覆盖所有子技能():
    text = ROOT_SKILL.read_text(encoding="utf-8")
    for cid in list_profiles():
        assert f"customer-quote-{cid}" in text, (
            f"总纲没有指向 skill customer-quote-{cid} —— 路由表漏了一家"
        )


def test_总纲不回流客户专属内容():
    """总纲里不许出现某个客户的表单结构/口径表 —— 那是子技能的活。

    刻意挑「只有那一家才有」的标记：NGV 的分区表单结构、igl 的基线值。
    """
    text = ROOT_SKILL.read_text(encoding="utf-8")
    # 标记挑「只有那一家才有」的**结构/基线**内容；路由表里一句话点名形态是允许的
    for marker in ("### 2.6 分区表单客户", "#### 2.6.1", "#### 2.6.2", "#### 2.6.3",
                   "enquiry/Anfrage", "section_value: product_name",
                   "五个必须知道的坑", "Ebers, Lea",
                   "CPSL1..7 = 3000/5000/7500"):
        assert marker not in text, (
            f"总纲里出现了客户专属标记 {marker!r} —— 请移进对应子技能（见 2.6 铁律）"
        )


def test_总纲提到_CPMS_必须手写与子技能加载顺序():
    """两件最容易忘的：CPMS 由 agent 手写、干活前先加载子技能。"""
    text = ROOT_SKILL.read_text(encoding="utf-8")
    assert "CPMS" in text and "agent" in text
    assert "加载对应子技能" in text or "加载" in text
    front, _ = _split_frontmatter(text)
    assert _frontmatter_value(front, "name") == "customer-quote-request"
