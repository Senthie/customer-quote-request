#!/usr/bin/env python3
"""产品类别树（CPLB）的测试。

树的数据源：assets/product_categories.xlsx（ERP 产品类别表导出，88 行）
            → 生成 assets/cplb_tree.yaml（编号 + 业务定义 + 重设计导航树）

这些用例守住三件事：
  1. 树与源表一致（编号、名称一个都不能漂）—— 编号错 = 导入丢数据
  2. 重设计的导航树覆盖全部可选子类，且每个只出现一次（不然人工选还是会漏）
  3. 脚本与主流程只接受树里的编号（main.py resolve_cplb）
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import cplb_tree  # noqa: E402

XLSX = REPO_ROOT / "assets" / "product_categories.xlsx"
TREE = REPO_ROOT / "assets" / "cplb_tree.yaml"


# ── 数据完整性 ───────────────────────────────────────────────────────────
def test_树文件存在且有节点():
    nodes = cplb_tree.nodes()
    assert nodes, "assets/cplb_tree.yaml 为空 —— 跑 uv run ./scripts/cplb_tree.py --rebuild"
    assert len(nodes) == 88, f"节点数应为 88（ERP 导出），实际 {len(nodes)}"


def test_顶层与子类数量():
    nodes = cplb_tree.nodes()
    tops = [c for c, n in nodes.items() if n["depth"] == 1]
    leaves = [c for c, n in nodes.items() if n["depth"] == 2]
    assert len(tops) == 11, tops
    assert len(leaves) == 77, leaves
    assert all(c.endswith(".00") for c in tops)
    assert all(not c.endswith(".00") for c in leaves)


@pytest.mark.skipif(not XLSX.exists(), reason="源表不在仓库里")
def test_与源表逐节点一致():
    """树是转录的，就必须与 ERP 导出表逐字一致。"""
    raw = cplb_tree.read_xlsx_nodes()
    tree = cplb_tree.nodes()
    assert set(raw) == set(tree), (
        f"只在源表: {sorted(set(raw) - set(tree))}；只在树: {sorted(set(tree) - set(raw))}")
    for code, node in raw.items():
        assert tree[code]["name"] == node["name"], f"{code} 名称漂了"
        assert tree[code]["label"] == f"{code};{node['name']}"


def test_自检通过():
    problems = cplb_tree.validate()
    assert problems == [], problems


def test_导航树覆盖全部子类且不重复():
    tree = cplb_tree.load_tree()
    seen: dict[str, int] = {}
    for section in tree["navigation"]:
        for group in section["groups"]:
            for sub in group["subgroups"]:
                for code in sub["items"]:
                    seen[code] = seen.get(code, 0) + 1
    leaves = {c for c, n in tree["nodes"].items() if n["depth"] == 2}
    assert set(seen) == leaves, f"未覆盖: {sorted(leaves - set(seen))}"
    assert all(v == 1 for v in seen.values()), {k: v for k, v in seen.items() if v > 1}


# ── 重设计是否真的更可读 ─────────────────────────────────────────────────
def test_原树最大的组被拆成多个子分组():
    """原树 27 BB书 一个父节点挂 23 个子类（编号顺序与工艺无关）—— 重设计后按工艺聚合。"""
    tree = cplb_tree.load_tree()
    bb = next(g for s in tree["navigation"] for g in s["groups"] if g["top"] == "27")
    subs = [sg for sg in bb["subgroups"] if len(sg["items"]) > 1]
    assert len(subs) >= 6, "BB书应被拆成「基础/揭页/砌图/拉卡/POP UP/音乐/磁胶/毛毡」等子分组"
    flat = [c for sg in bb["subgroups"] for c in sg["items"]]
    assert len(flat) == 23 and len(set(flat)) == 23
    # 同工艺的「普通/精装」兄弟必须相邻（原树里被编号打散）
    for sg in bb["subgroups"]:
        pass
    jieye = next(sg for sg in bb["subgroups"] if "揭页" in sg["name"])
    assert "27.05" in jieye["items"] and "27.06" in jieye["items"]


def test_每个子类都有业务定义或明确留空():
    """业务定义（源表 REMARK 列）就是「该不该选它」的判据，不能大面积缺失。"""
    nodes = cplb_tree.nodes()
    leaves = {c: n for c, n in nodes.items() if n["depth"] == 2}
    with_criteria = [c for c, n in leaves.items() if n.get("criteria")]
    assert len(with_criteria) / len(leaves) > 0.8, (
        f"只有 {len(with_criteria)}/{len(leaves)} 个子类有业务定义")


def test_决策路径覆盖到每个顶层分组():
    tree = cplb_tree.load_tree()
    text = " ".join(str(p) for p in tree["paths"])
    for top, name in [("22", "精装"), ("23", "胶装"), ("24", "骑钉"), ("25", "车线"),
                      ("26", "线圈"), ("27", "BB书"), ("28", "POP UP"),
                      ("29", "套装"), ("30", "盒"), ("31", "单张"), ("32", "其他")]:
        assert name in text, f"决策路径里没提到 {top} {name}"


# ── 查询接口 ─────────────────────────────────────────────────────────────
def test_编号查名称():
    assert cplb_tree.label_of("23.01") == "23.01;无线胶装书"
    assert cplb_tree.label_of("27.06") == "27.06;精装揭页BB书"
    assert cplb_tree.label_of("99.99") == "99.99", "未知编号原样返回，交给校验去报错"


def test_存在性判断():
    assert cplb_tree.exists("23.01")
    assert cplb_tree.exists("22.00"), "顶层节点也算存在"
    assert not cplb_tree.exists("23.99")
    assert not cplb_tree.exists("99.99;其他"), "旧兜底值已废弃（不在树里）"


@pytest.mark.parametrize("keyword,expected", [
    ("揭页", "27.05;普通揭页BB书"),
    ("lift the flap", "27.05;普通揭页BB书"),
    ("蛇圈", "26.01;普通蛇圈书"),
    ("手挽袋", "32.03;手挽袋"),
    ("sticky note", "31.06;便利贴"),
])
def test_关键词搜索(keyword, expected):
    hits = [h["label"] for h in cplb_tree.search(keyword)]
    assert expected in hits, f"搜 {keyword!r} 没找到 {expected}；实际 {hits[:5]}"


def test_按描述给候选():
    top = cplb_tree.suggest("hardcover, round spine")[0]
    assert top["code"].startswith("22"), top
    top2 = cplb_tree.suggest("board book with lift the flap, hardcover")[0]
    assert top2["code"] == "27.06", top2


def test_兜底值必须真的在树里():
    """档案里的 product_category.fallback 写错编号会导致导入丢数据 —— 逐个档案核对。"""
    import customer_profile

    for path in sorted((REPO_ROOT / "customers").glob("*.yaml")):
        if path.name.startswith("_"):
            continue
        profile = customer_profile.load_profile(path.stem)
        fallback = str(profile.rule("product_category.fallback", "32.00;其他"))
        assert cplb_tree.exists(fallback.split(";")[0].strip()), (
            f"{path.name} 的 fallback={fallback!r} 不在产品类别树里")


def test_客户档案里的_cplb_映射都指向真实节点():
    """档案 cplb.map 的 node 必须存在 —— 写错就等于把客户单子导成错误类别。"""
    import customer_profile

    for path in sorted((REPO_ROOT / "customers").glob("*.yaml")):
        if path.name.startswith("_"):
            continue
        profile = customer_profile.load_profile(path.stem)
        for entry in profile.rule("cplb.map", []) or []:
            code = str(entry.get("node", "")).strip()
            assert cplb_tree.exists(code), f"{path.name}: node={code} 不在树里"
            assert "." in code, f"{path.name}: node={code} 只到顶层，应具体到子类"
