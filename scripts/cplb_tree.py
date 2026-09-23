#!/usr/bin/env python3
"""cplb_tree.py —— ERP「产品类别」（CPLB）权威树 + 重设计导航树。

背景
────
ERP 的产品类别是个树形下拉（`txtCPLB`，ComponentArt TreeView），节点文本是
`<编号>;<名称>`，例如 `23.01;无线胶装书` —— 这个字符串就是最终写进 ERP JSON 的
CPLB 值。树上共 11 个顶层节点（22 精装书 … 32 其他）与若干子节点。

原始清单来自 `assets/product_categories.xlsx`（ERP 产品类别表导出，88 行），
含每个子类的**业务定义**（REMARK 列）—— 那就是「什么时候该选它」的判据。

为什么要有这个模块
──────────────────
1. 原始树是「按编号平铺」的：27 BB书 一个父节点下挂了 23 个子类，26 线圈书挂 14 个，
   而编号顺序与工艺属性无关（27.01 普通BB书、27.02 精装BB书、27.03 普通砌图BB书…），
   人工按编号扫一遍很慢，且容易选到「看起来像、实际不是」的类别。
2. 子类名大量使用内部行话（蛇圈 / YO / BB书 / 揭页 / 砌图 / 带机封），
   换成「普通 / 精装 × 内文特征 × 工艺」的结构后能按决策路径逐层收敛。
3. 我们这边的脚本要能**校验**产物的 CPLB 是否真是树上节点（写错编号导入就丢）。

本模块做两件事
──────────────
① 生成 `assets/cplb_tree.yaml`：
   - `nodes`      —— 88 个节点原样转录（编号 / 名称 / 父节点 / 业务定义 / 结构化属性）
   - `navigation` —— 重设计的导航树（分组、子分组、常用速选）
   - `paths`      —— 决策路径（两个问题定档）
   生成脚本 = 本文件，设计内容以数据形式内嵌，`--rebuild` 可确定性重放。
② 查询 / 校验 / 出 HTML 选取器。

CLI
───
    uv run ./scripts/cplb_tree.py --rebuild              # 从 xlsx 重新生成 yaml
    uv run ./scripts/cplb_tree.py --list                 # 按导航树列出全部类别
    uv run ./scripts/cplb_tree.py --group 精装书          # 看某一组
    uv run ./scripts/cplb_tree.py --node 23.01           # 看单个节点
    uv run ./scripts/cplb_tree.py --search 揭页           # 关键词搜索（中文/英文/行话）
    uv run ./scripts/cplb_tree.py --suggest "pp+gloss+perfect bound"
    uv run ./scripts/cplb_tree.py --validate             # 与 xlsx 交叉校验
    uv run ./scripts/cplb_tree.py --html assets/cplb_tree.html
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

try:
    import yaml
except ImportError:  # pragma: no cover
    print("需要 pyyaml：uv add pyyaml", file=sys.stderr)
    raise

REPO_ROOT = Path(__file__).resolve().parent.parent
XLSX_PATH = REPO_ROOT / "assets" / "product_categories.xlsx"
TREE_PATH = REPO_ROOT / "assets" / "cplb_tree.yaml"

TREE_VERSION = 1
FORMAT_HINT = "ERP 存储格式：`<编号>;<名称>`，例：`23.01;无线胶装书`（历史单据实例）"


# ═══════════════════════════════════════════════════════════════════════════
# 一、重设计：导航树（分组结构）
#
# 设计原则
#   1. 编号一个都不动 —— 只加「导航层」，ERP 兼容性零风险。
#   2. 分组按**决策顺序**排，不按编号：成品形态 → 装订 → 封面 → 内文工艺。
#   3. 每个分组给出「怎么判」的一句话，组内按「普通 → 精装 → 特殊工艺」排列，
#      让同属性的兄弟节点相邻（原树里它们被编号打散）。
#   4. 行话配英文别名，方便按客户文件里的英文词反查。
# ═══════════════════════════════════════════════════════════════════════════

QUICK_PICK = {
    "name": "常用速选（童书/纸品类订单高频）",
    "note": "⚠️ 排序依据是业务经验，不是 ERP 真实频次数据 —— 拿到导出后可据实重排。",
    "items": [
        {"code": "23.01", "why": "平装首选：软封面、无线胶装、不锁线"},
        {"code": "22.02", "why": "硬封面 + 方脊（最常见精装）"},
        {"code": "22.01", "why": "硬封面 + 圆脊"},
        {"code": "24.01", "why": "薄本、成本最低（骑马钉）"},
        {"code": "27.02", "why": "纸板书 + 硬封面（BB书主力）"},
        {"code": "27.01", "why": "纸板书 + 软封面"},
        {"code": "25.01", "why": "车线（缝线）装订、无灰板"},
        {"code": "26.01", "why": "单线圈（蛇圈）、无灰板"},
        {"code": "23.05", "why": "PAD 书（封面大于内文、露 PAD 头）"},
        {"code": "31.05", "why": "非成册：单张 / 卡牌"},
    ],
}

NAVIGATION: list[dict] = [
    {
        "name": "A. 书籍类（可翻阅成册）",
        "hint": "先定装订方式，再看封面有没有灰板（有=精装变体，无=普通变体），最后看内文工艺。",
        "groups": [
            {
                "file": "22 精装书",
                "top": "22",
                "hint": "封面有灰板包皮壳（硬封面）。再分：脊型（圆/方）、是否带机封（封面需折叠）、内文是否含线路。",
                "subgroups": [
                    {"name": "圆脊", "items": ["22.01", "22.04"]},
                    {"name": "方脊", "items": ["22.02", "22.05"]},
                    {"name": "假精装（软封套硬壳，成本低于精装）", "items": ["22.03"]},
                    {"name": "内文含线路（音乐书）", "items": ["22.06"]},
                ],
            },
            {
                "file": "23 胶装书",
                "top": "23",
                "hint": "软封面、书脊上胶。先判要不要锁线（不锁=无线胶装，锁=锁线），再判是否 PAD 书（封面与内文尺寸不等）、封面是否需折叠、是否整书冲形状。",
                "subgroups": [
                    {"name": "常规胶装", "items": ["23.01", "23.02"]},
                    {"name": "PAD 书（封面大于内文，露 PAD 头）", "items": ["23.05", "23.06"]},
                    {"name": "封面需折叠（带机封）", "items": ["23.07", "23.08"]},
                    {"name": "整书冲形状", "items": ["23.03", "23.04"]},
                ],
            },
            {
                "file": "24 骑钉书",
                "top": "24",
                "hint": "骑马钉（staple / saddle stitch），薄本。只有「封面需折叠」「整书冲形状」「内文是贴纸」三种变体。",
                "subgroups": [
                    {"name": "常规", "items": ["24.01"]},
                    {"name": "封面需折叠（带机封）", "items": ["24.04"]},
                    {"name": "整书冲形状", "items": ["24.02"]},
                    {"name": "内文全部是贴纸", "items": ["24.03"]},
                ],
            },
            {
                "file": "25 车线书",
                "top": "25",
                "hint": "车线（thread sewn）装订。按封面有没有灰板分普通/精装；内文含线路的另立一类。",
                "subgroups": [
                    {"name": "无灰板（普通）", "items": ["25.01"]},
                    {"name": "有灰板（精装）", "items": ["25.02"]},
                    {"name": "含线路（音乐书）", "items": ["25.03"]},
                ],
            },
            {
                "file": "26 线圈书",
                "top": "26",
                "hint": "线圈装订。两个变量：圈型（蛇圈=单线圈 / YO=双线圈）× 封面（普通=无灰板 / 精装=有灰板），圈的可视度再分「全外露 / 半内藏 / 内藏」。日历是 YO + 三角座的变体。",
                "subgroups": [
                    {"name": "蛇圈（单线圈）—— 普通封面的 全外露/半内藏/内藏", "items": ["26.01", "26.02", "26.03"]},
                    {"name": "蛇圈（单线圈）—— 精装（有灰板）的 全外露/半内藏/内藏", "items": ["26.04", "26.05", "26.06"]},
                    {"name": "YO（双线圈）—— 普通封面的 全外露/半内藏/内藏", "items": ["26.07", "26.08", "26.09"]},
                    {"name": "YO（双线圈）—— 精装（有灰板）的 全外露/半内藏/内藏", "items": ["26.10", "26.11", "26.12"]},
                    {"name": "日历（YO + 三角座）", "items": ["26.13", "26.14"]},
                ],
            },
            {
                "file": "27 BB书（纸板书）",
                "top": "27",
                "hint": "★ 原树最大的组（23 个子类）。判法：先定封面「普通（无灰板）/ 精装（有灰板）」，再看内文或工艺特征（揭页 / 砌图 / 拉卡机关 / POP UP / 音乐 / 磁胶 / 毛毡布偶 / 整书冲形状）。组内已按特征重新聚合，不再按编号散排。",
                "subgroups": [
                    {"name": "基础（无特殊工艺）", "items": ["27.01", "27.02"]},
                    {"name": "揭页（lift-the-flap）", "items": ["27.05", "27.06", "27.07", "27.20", "27.21"]},
                    {"name": "砌图（puzzle）", "items": ["27.03", "27.04"]},
                    {"name": "拉卡机关（pull-tab）", "items": ["27.12", "27.13"]},
                    {"name": "内文 POP UP", "items": ["27.14", "27.15"]},
                    {"name": "音乐 / 发声（外露音乐器 vs 内藏软线路）", "items": ["27.16", "27.17", "27.18", "27.19"]},
                    {"name": "磁胶 / 铁粉胶", "items": ["27.08", "27.09"]},
                    {"name": "毛毡布偶 / 整本书粘配件", "items": ["27.22", "27.23"]},
                    {"name": "整书冲形状", "items": ["27.10", "27.11"]},
                ],
            },
            {
                "file": "28 POP UP书",
                "top": "28",
                "hint": "整本以 POP UP（立体页）为主体的书 —— 区别于 27 里的「内文含 POP UP 的 BB书」。按封面有无灰板分。",
                "subgroups": [
                    {"name": "无灰板", "items": ["28.01"]},
                    {"name": "有灰板", "items": ["28.02"]},
                ],
            },
        ],
    },
    {
        "name": "B. 组合 / 包装类",
        "hint": "不是单本成册的书：多件组合（套装）或盒形容器。",
        "groups": [
            {
                "file": "29 套装",
                "top": "29",
                "hint": "多个产品/物料组合成一套。按组合件的容器/玩法分。",
                "subgroups": [
                    {"name": "按容器 / 玩法", "items": ["29.01", "29.02", "29.03", "29.04"]},
                ],
            },
            {
                "file": "30 盒类",
                "top": "30",
                "hint": "单独的彩盒（不含书）。按盒身材料分：灰板（糊盒）/ 坑纸（坑盒）/ 卡纸（咭盒）。",
                "subgroups": [
                    {"name": "按盒身材料", "items": ["30.01", "30.02", "30.03"]},
                ],
            },
        ],
    },
    {
        "name": "C. 单张 / 卡牌类",
        "hint": "不成册的平面印品。",
        "groups": [
            {
                "file": "31 单张类",
                "top": "31",
                "hint": "单张、卡牌、贴纸、便利贴。按「表面工艺 / 纸张结构」分：过啤牌油 / 过吸塑油 / 对裱 / 坑纸。",
                "subgroups": [
                    {"name": "基础单张", "items": ["31.05"]},
                    {"name": "表面工艺（过油）", "items": ["31.01", "31.07"]},
                    {"name": "纸张结构（对裱 / 坑纸）", "items": ["31.04", "31.03"]},
                    {"name": "文具类", "items": ["31.06"]},
                ],
            },
        ],
    },
    {
        "name": "D. 其他（非纸类 / 杂项）",
        "hint": "主材不是纸的印品。",
        "groups": [
            {
                "file": "32 其他",
                "top": "32",
                "hint": "塑胶 / 铝片 / 手挽袋 / 文件夹。",
                "subgroups": [
                    {"name": "非纸主材", "items": ["32.01", "32.04"]},
                    {"name": "袋 / 夹", "items": ["32.03", "32.02"]},
                ],
            },
        ],
    },
]

# 决策路径：业务按这个顺序自问，两步就能收敛到具体子类
DECISION_PATHS: list[dict] = [
    {
        "step": "第 1 问：成品是不是「成册可翻阅的书」？",
        "yes": "进入 A 类，问装订方式",
        "no": "不是书 → 29 套装（多件组合）/ 30 盒类（容器）/ 31 单张类（不成册印品）/ 32 其他（非纸主材）",
    },
    {
        "step": "第 2 问：装订方式是什么？",
        "options": [
            "硬封面、书脊有灰板包皮壳 → 22 精装书",
            "软封面、书脊上胶 → 23 胶装书",
            "骑马钉（铁丝钉）→ 24 骑钉书",
            "车线（缝线）→ 25 车线书",
            "线圈（蛇圈=单线圈 / YO=双线圈）→ 26 线圈书",
            "内文是厚纸板（整本硬纸板）→ 27 BB书",
            "整本立体页 → 28 POP UP书",
        ],
    },
    {
        "step": "第 3 问（书类）：封面有没有灰板？",
        "options": [
            "有灰板 → 精装 / 精装变体（名称带「精装」，如 27.02 精装BB书、26.04 精装蛇圈书）",
            "无灰板 → 普通变体（名称带「普通」，如 27.01 普通BB书、26.01 普通蛇圈书）",
        ],
    },
    {
        "step": "第 4 问：内文 / 工艺有什么特征？",
        "options": [
            "有揭页（lift-the-flap）→ 27.05/27.06/27.07/27.20/27.21",
            "有砌图（puzzle）→ 27.03/27.04（砌图套装是 29.02）",
            "有拉卡机关（pull-tab）→ 27.12/27.13",
            "内文有 POP UP → 27.14/27.15；整本 POP UP → 28.01/28.02",
            "有发声器（外露）→ 27.16/27.17；内藏软线路 → 27.18/27.19",
            "有磁胶 / 铁粉胶 → 27.08/27.09",
            "有毛毡布 / 玩偶 / 粘 2 片以上配件 → 27.22/27.23",
            "内文全部是贴纸 → 24.03（骑钉）",
            "无特殊工艺 → 取该装订的基础子类",
        ],
    },
    {
        "step": "第 5 问：整书要不要冲形状（die-cut）？封面要不要折叠（带机封）？",
        "options": [
            "整书冲形状 → 取同装订的「冲形状 XXX」，如 23.03 冲形状无线胶装书、27.10 冲形状普通BB书",
            "封面需折叠（机封）→ 取「带机封 XXX」，如 23.07、22.04、24.04",
        ],
    },
    {
        "step": "第 6 问（胶装特例）：是不是 PAD 书？",
        "options": [
            "封面大于内文、露 PAD 头（内文无封面，或有封面但封底尺寸≠封面）→ 23.05 PAD头书",
            "有封面的 PAD 头书、需二次上胶装机 → 23.06 PAD头书(全封面)",
        ],
    },
]

# 结构化属性抽取（用于 --suggest 打分与检索）
FEATURE_PATTERNS: list[tuple[str, str]] = [
    ("揭页", r"揭页"),
    ("砌图", r"砌图"),
    ("拉卡机关", r"拉卡机关"),
    ("POP UP", r"pop\s*up"),
    ("发声器（外露）", r"发声"),
    ("软线路（内藏）", r"线路"),
    ("磁胶/铁粉胶", r"磁胶|铁粉胶"),
    ("毛毡布偶/配件", r"毛毡|布偶|配件"),
    ("冲形状", r"冲形状"),
    ("对裱", r"对裱"),
    ("风琴折", r"风琴"),
    ("贴纸", r"贴纸"),
    ("Y O 双线圈", r"yo|双线"),
    ("蛇圈单线圈", r"蛇圈|单线"),
    ("机封（封面折叠）", r"机封"),
    ("PAD 头", r"pad"),
    ("日历/三角座", r"日历"),
    ("过啤牌油", r"啤牌"),
    ("过吸塑油", r"吸塑"),
]

# 行话 / 英文别名（搜索用）
_STOPWORDS = {"the", "and", "with", "for", "book", "cover", "spine", "版", "书"}  # 太泛的词不计分

EXTRA_ALIASES: dict[str, list[str]] = {
    "22": ["hardcover", "case bound", "硬皮", "硬封面"],
    "23": ["paperback", "perfect bound", "softcover", "平装"],
    "24": ["saddle stitch", "staple", "骑马钉"],
    "25": ["thread sewn", "sewn", "缝线"],
    "26": ["wire-o", "wiro", "coil", "spiral", "线圈", "铁圈"],
    "27": ["board book", "纸板书", "厚纸板"],
    "28": ["pop-up book"],
    "29": ["box set", "gift set", "套装"],
    "30": ["box", "彩盒", "包装盒"],
    "31": ["sheet", "card", "cardboard", "单张", "卡牌"],
    "32": ["plastic", "aluminum", "非纸类"],
    "23.05": ["pad book", "PAD头"],
    "23.06": ["pad head full cover"],
    "22.01": ["round back", "round spine", "圆脊"],
    "22.02": ["square back", "flat spine", "方脊"],
    "22.03": ["quarter bound", "假精装", "半精装"],
    "27.01": ["soft cover board book", "普通纸板书"],
    "27.02": ["hardcover board book", "精装纸板书", "硬皮书"],
    "27.05": ["lift the flap", "翻翻书"],
    "27.06": ["hardcover lift the flap"],
    "27.12": ["pull tab"],
    "27.16": ["sound book", "musical"],
    "26.13": ["calendar", "月历"],
    "31.06": ["sticky note", "n次贴"],
    "32.03": ["tote bag", "纸袋"],
}


# ═══════════════════════════════════════════════════════════════════════════
# 二、从 xlsx 生成节点（权威转录，不做任何解读）
# ═══════════════════════════════════════════════════════════════════════════
def _clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in ("nan", "none") else text


def _fmt_code(value: Any) -> str:
    """子类编号格式化为两位小数（xlsx 里是数值列，22.00 会被读成 22.0）。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        try:
            if float(value) != float(value):      # NaN
                return ""
        except (TypeError, ValueError):
            return ""
        return f"{float(value):.2f}"
    text = str(value).strip()
    if text.lower() in ("nan", "none", ""):
        return ""
    m = re.fullmatch(r"(\d+)(?:\.(\d+))?", text)
    if m:
        return f"{int(m.group(1))}.{int(m.group(2) or 0):02d}"
    return text


def _extract_features(text: str) -> list[str]:
    out = []
    for name, pattern in FEATURE_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            out.append(name)
    return out


def _extract_board(text: str) -> str:
    """封面灰板：有 / 无 / 不涉及。"""
    if re.search(r"【?不用】?灰板|没有灰板|无灰板", text):
        return "无"
    if re.search(r"【?有用】?灰板|有灰板|有用灰板", text):
        return "有"
    return "不涉及"


def read_xlsx_nodes(verbose: bool = False) -> dict[str, dict]:
    import pandas as pd

    if not XLSX_PATH.exists():
        raise FileNotFoundError(f"找不到产品类别表：{XLSX_PATH}")
    df = pd.read_excel(XLSX_PATH)
    for col in ("ZLBM", "CPLB", "CPDL", "REMARK"):
        if col not in df.columns:
            raise ValueError(f"产品类别表缺少列 {col}")

    # ParentID → 子类编号（用于判断父子关系；X.00 为顶层）
    by_id = {}
    for _, row in df.iterrows():
        code = _fmt_code(row["ZLBM"])
        if code:
            by_id[_clean(row["ID"])] = code

    nodes: dict[str, dict] = {}
    for _, row in df.iterrows():
        code = _fmt_code(row["ZLBM"])
        if not code:
            continue
        name = _clean(row["CPLB"])
        remark = _clean(row["REMARK"])
        parent_id = _clean(row["ParentID"])
        parent_code = by_id.get(parent_id, "") or None
        top = code.split(".")[0]
        depth = 1 if code.endswith(".00") else 2
        blob = f"{name} {remark}"
        node = {
            "code": code,
            "name": name,
            "label": f"{code};{name}",
            "depth": depth,
            "top": top,
            "top_name": "",
            "parent": parent_code if depth > 1 else None,
            "category_group": _clean(row["CPDL"]) or "书版",
            "criteria": remark,
            "attrs": {
                "cover_board": _extract_board(blob) if depth > 1 else "不涉及",
                "features": _extract_features(blob) if depth > 1 else [],
                "finance_class": _clean(row.get("KWCPLB")),
                "report_class": _clean(row.get("ECPLB")),
                "board_class": _clean(row.get("BBCPLB")),
            },
        }
        aliases = EXTRA_ALIASES.get(code, [])
        if aliases:
            node["aliases"] = aliases
        nodes[code] = node

    # 顶层名称回填（子节点用得上）
    for code, node in nodes.items():
        top_node = nodes.get(f"{node['top']}.00")
        if top_node:
            node["top_name"] = top_node["name"]
    for code, node in nodes.items():
        if code in EXTRA_ALIASES and node["depth"] == 1:
            node.setdefault("aliases", [])
    # 去掉空 attrs 键，输出更干净
    for node in nodes.values():
        node["attrs"] = {k: v for k, v in node["attrs"].items() if v not in ("", [], None)}
    if verbose:
        print(f"读取节点 {len(nodes)} 个（顶层 {sum(1 for n in nodes.values() if n['depth'] == 1)}）")
    return nodes


def build_tree() -> dict:
    nodes = read_xlsx_nodes()
    return {
        "tree_version": TREE_VERSION,
        "generated_at": datetime.now().strftime("%Y-%m-%d"),
        "source": "assets/product_categories.xlsx（ERP 产品类别表导出）",
        "generator": "scripts/cplb_tree.py --rebuild",
        "field": "CPLB",
        "form_name": "txtCPLB",
        "label": "产品类别",
        "format_hint": FORMAT_HINT,
        "design_note": (
            "编号全部沿用 ERP 原值（一个都没改），只在其上叠加「导航层」："
            "分组顺序按业务决策顺序（成品形态 → 装订 → 封面 → 内文工艺），"
            "组内把同属性的兄弟节点排到一起。"
        ),
        "legend": {
            "depth": "1=顶层大组（编号 X.00），2=可选的具体子类（写进 JSON 的值取这一层）",
            "criteria": "ERP 产品类别表里的业务定义（REMARK 列）—— 判断「该不该选它」的依据",
            "attrs.cover_board": "封面灰板：有 / 无 / 不涉及",
            "attrs.features": "从名称+定义里抽出的工艺特征（供搜索与建议打分）",
        },
        "nodes": nodes,
        "quick_pick": QUICK_PICK,
        "navigation": NAVIGATION,
        "paths": DECISION_PATHS,
    }


def write_tree(tree: Optional[dict] = None, path: Path = TREE_PATH) -> Path:
    tree = tree or build_tree()
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# ═══════════════════════════════════════════════════════════════════\n"
        "# ERP 产品类别（CPLB）权威树 + 重设计导航树\n"
        "#\n"
        "# 本文件由 scripts/cplb_tree.py --rebuild 生成，请勿手改 ——\n"
        "# 要改设计（分组 / 决策路径 / 常用速选），改脚本里的 NAVIGATION 等常量再重放。\n"
        "#\n"
        "# 用法：uv run ./scripts/cplb_tree.py --list | --group 精装书 | --node 23.01\n"
        "# ═══════════════════════════════════════════════════════════════════\n"
    )
    body = yaml.safe_dump(tree, allow_unicode=True, sort_keys=False, width=200)
    path.write_text(header + body, encoding="utf-8")
    return path


# ═══════════════════════════════════════════════════════════════════════════
# 三、加载与查询
# ═══════════════════════════════════════════════════════════════════════════
_CACHE: Optional[dict] = None


def load_tree(path: Optional[Path] = None) -> dict:
    global _CACHE
    if path is None and _CACHE is not None:
        return _CACHE
    with open(path or TREE_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if path is None:
        _CACHE = data
    return data


def nodes(path: Optional[Path] = None) -> dict[str, dict]:
    return load_tree(path).get("nodes") or {}


def leaf_codes() -> list[str]:
    """可写进 JSON 的具体子类编号（depth=2）。"""
    return sorted(c for c, n in nodes().items() if n.get("depth") == 2)


def label_of(code: str) -> str:
    """编号 → ERP 节点文本（`23.01;无线胶装书`）。未知编号返回原值。"""
    node = nodes().get(code)
    return node["label"] if node else code


def exists(code: str) -> bool:
    return code in nodes()


def search(keyword: str, limit: int = 40) -> list[dict]:
    """关键词搜索：编号 / 名称 / 业务定义 / 别名 / 特征 / 顶层名。"""
    kw = keyword.strip().lower()
    if not kw:
        return []
    hits = []
    for code, node in nodes().items():
        haystack = " ".join([
            code, node.get("name", ""), node.get("criteria", ""),
            node.get("top_name", ""), " ".join(node.get("aliases") or []),
            " ".join((node.get("attrs") or {}).get("features") or []),
        ]).lower()
        if kw in haystack:
            score = 3 if kw in code.lower() or kw in node.get("name", "").lower() else 1
            hits.append((score, code, node))
    hits.sort(key=lambda h: (-h[0], h[1]))
    return [{"code": c, "name": n["name"], "label": n["label"],
             "criteria": n.get("criteria", ""), "aliases": n.get("aliases") or []}
            for _, c, n in hits[:limit]]


def suggest(text: str, top: int = 5) -> list[dict]:
    """按自由文本（客户文件里的写法）给候选类别打分。"""
    blob = str(text).lower()
    scored = []
    for code, node in nodes().items():
        if node.get("depth") != 2:
            continue
        score = 0
        reasons = []
        for alias in node.get("aliases") or []:
            low_alias = alias.lower()
            if low_alias in blob:
                score += 3
                reasons.append(f"别名 {alias}")
                continue
            # 多词别名允许「词序不同 / 中间夹词」：按词覆盖率给部分加分
            words = [w for w in re.split(r"[\s\-]+", low_alias)
                     if len(w) >= 3 and w not in _STOPWORDS]
            if len(words) >= 2:
                hit = sum(1 for w in words if w in blob)
                coverage = hit / len(words)
                if coverage >= 0.6:
                    # 按命中词数计分：别名越长（越具体）命中越多分越高，
                    # 这样「hardcover lift the flap」会压过笼统的「hardcover board book」
                    score += hit
                    reasons.append(f"别名近似 {alias}（{hit}/{len(words)} 词命中）")
        # 顶层分组的别名给子树候选同等小幅加分（如 board book → 27 组）
        top_node = nodes().get(f"{node['top']}.00") or {}
        for alias in top_node.get("aliases") or []:
            if alias.lower() in blob:
                score += 2
                reasons.append(f"顶层别名 {alias}")
                break
        name = node.get("name", "")
        for token in re.findall(r"[\u4e00-\u9fff]{2,}", name):
            if token in blob:
                score += 2
                reasons.append(f"名称含 {token}")
        board = (node.get("attrs") or {}).get("cover_board")
        if board == "有" and re.search(r"hard|灰板|精装|硬", blob):
            score += 2
            reasons.append("提示有灰板")
        if board == "无" and re.search(r"soft|普通|无灰板|软", blob):
            score += 1
            reasons.append("提示无灰板")
        for feature in (node.get("attrs") or {}).get("features") or []:
            if feature.split("（")[0].lower() in blob:
                score += 2
                reasons.append(f"特征 {feature}")
        if score:
            scored.append({"code": code, "label": node["label"], "score": score,
                           "why": reasons[:4], "criteria": node.get("criteria", "")})
    scored.sort(key=lambda s: (-s["score"], s["code"]))
    return scored[:top]


def validate() -> list[str]:
    """自检：节点完整性 + 导航树覆盖 + 与 xlsx 一致性。返回问题列表。"""
    problems: list[str] = []
    tree = load_tree()
    node_map = tree.get("nodes") or {}

    if not node_map:
        return ["树是空的 —— 先跑 --rebuild"]

    leaves = {c for c, n in node_map.items() if n.get("depth") == 2}
    tops = {c for c, n in node_map.items() if n.get("depth") == 1}

    for code, node in node_map.items():
        if node.get("depth") == 2:
            parent = f"{node['top']}.00"
            if parent not in node_map:
                problems.append(f"{code} 的顶层节点 {parent} 不存在")
            elif node.get("parent") and node["parent"] != parent:
                problems.append(f"{code} 的 parent={node['parent']} 与编号推断的 {parent} 不一致")

    # 导航树覆盖：每个叶子必须被引用且只引用一次
    seen: dict[str, int] = {}
    for section in tree.get("navigation") or []:
        for group in section.get("groups") or []:
            top = group.get("top")
            if top and f"{top}.00" not in node_map:
                problems.append(f"导航分组「{group.get('file')}」的 top={top} 不在树里")
            for sub in group.get("subgroups") or []:
                for code in sub.get("items") or []:
                    if code not in node_map:
                        problems.append(f"导航树引用了不存在的编号 {code}")
                    seen[code] = seen.get(code, 0) + 1
    for code in sorted(leaves):
        if code not in seen:
            problems.append(f"叶子 {code}（{node_map[code]['name']}）没有被导航树覆盖")
        elif seen[code] > 1:
            problems.append(f"叶子 {code} 在导航树里出现 {seen[code]} 次（应唯一）")

    for item in (tree.get("quick_pick") or {}).get("items") or []:
        if item.get("code") not in node_map:
            problems.append(f"常用速选引用了不存在的编号 {item.get('code')}")

    # 与 xlsx 交叉校验（xlsx 在才查）
    if XLSX_PATH.exists():
        try:
            raw = read_xlsx_nodes()
        except Exception as exc:  # pragma: no cover
            problems.append(f"读取 xlsx 失败：{exc}")
        else:
            for code, node in raw.items():
                if code not in node_map:
                    problems.append(f"xlsx 里的 {code}（{node['name']}）没进树")
                elif node_map[code]["name"] != node["name"]:
                    problems.append(
                        f"{code} 名称不一致：树={node_map[code]['name']!r} xlsx={node['name']!r}")
            for code in node_map:
                if code not in raw:
                    problems.append(f"树里的 {code} 在 xlsx 里找不到")
    else:
        problems.append(f"⚠️ 找不到 {XLSX_PATH.name}，跳过与源表的一致性校验")

    return problems


# ═══════════════════════════════════════════════════════════════════════════
# 四、HTML 选取器
# ═══════════════════════════════════════════════════════════════════════════
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>ERP 产品类别选取器（CPLB）</title>
<style>
  :root { --bg:#0f1115; --panel:#171a21; --line:#262b36; --fg:#e6e9ef; --dim:#8b93a4;
          --accent:#4f9cf9; --code:#7ee2b8; --warn:#f0b46a; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.7
         -apple-system,"PingFang SC","Microsoft YaHei",system-ui,sans-serif; }
  header { padding:18px 24px; border-bottom:1px solid var(--line); position:sticky; top:0;
           background:rgba(15,17,21,.96); backdrop-filter:blur(6px); z-index:5; }
  h1 { margin:0 0 4px; font-size:17px; font-weight:600; }
  .sub { color:var(--dim); font-size:12px; }
  .sub code { color:var(--code); }
  main { display:grid; grid-template-columns:minmax(0,1.6fr) minmax(0,1fr); gap:20px; padding:20px 24px 60px; }
  @media (max-width:1000px) { main { grid-template-columns:1fr; } }
  .panel { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:14px 16px; }
  .panel h2 { margin:0 0 10px; font-size:13px; color:var(--dim); font-weight:600; letter-spacing:.04em; }
  input[type=search] { width:100%; padding:9px 11px; border-radius:8px; border:1px solid var(--line);
                       background:#0c0e12; color:var(--fg); font-size:14px; margin-bottom:12px; }
  .sect { margin:16px 0 6px; font-weight:600; color:var(--warn); font-size:13px; }
  .grp { margin:10px 0 0 8px; }
  .grp > .gt { font-weight:600; }
  .grp .hint { color:var(--dim); font-size:12px; margin:2px 0 6px 0; }
  .sub2 { margin:8px 0 0 10px; color:var(--dim); font-size:12px; }
  .items { display:flex; flex-wrap:wrap; gap:6px; margin:6px 0 2px 10px; }
  .node { display:inline-flex; align-items:baseline; gap:7px; padding:5px 9px; border:1px solid var(--line);
          border-radius:7px; background:#0c0e12; cursor:pointer; }
  .node:hover { border-color:var(--accent); }
  .node .c { color:var(--code); font-family:ui-monospace,Menlo,monospace; font-size:12px; }
  .node .n { font-size:13px; }
  .node .b { font-size:11px; color:var(--dim); }
  table { width:100%; border-collapse:collapse; font-size:12.5px; }
  td, th { text-align:left; padding:5px 6px; border-bottom:1px solid var(--line); vertical-align:top; }
  th { color:var(--dim); font-weight:600; }
  ol { margin:6px 0 0; padding-left:20px; }
  li { margin-bottom:4px; }
  .crit { color:var(--dim); font-size:12px; }
  .toast { position:fixed; bottom:22px; left:50%; transform:translateX(-50%) translateY(20px);
           background:var(--accent); color:#06101f; padding:8px 16px; border-radius:8px; font-weight:600;
           opacity:0; transition:.2s; pointer-events:none; }
  .toast.on { opacity:1; transform:translateX(-50%) translateY(0); }
  .hidden { display:none; }
</style>
</head>
<body>
<header>
  <h1>ERP 产品类别选取器 · CPLB</h1>
  <div class="sub">点任意类别即复制编号;名称到剪贴板 ｜ 数据源 <code>assets/cplb_tree.yaml</code>
     生成于 __GENERATED_AT__ ｜ 共 __NODE_COUNT__ 个节点（__LEAF_COUNT__ 个可选子类）</div>
</header>
<main>
  <section class="panel">
    <input type="search" id="q" placeholder="搜索：32.03 / 揭页 / lift the flap / 线圈 / 手挽袋 …">
    <div id="tree"></div>
  </section>
  <aside>
    <section class="panel" id="quick">
      <h2>常用速选</h2>
      <div id="quickBody"></div>
    </section>
    <section class="panel" style="margin-top:16px">
      <h2>决策路径（两个问题定档）</h2>
      <div id="paths"></div>
    </section>
  </aside>
</main>
<div class="toast" id="toast"></div>
<script>
const TREE = __TREE_JSON__;
const nodeEl = (code, extra) => {
  const n = TREE.nodes[code] || {name:'?', attrs:{}};
  const board = (n.attrs && n.attrs.cover_board && n.attrs.cover_board !== '不涉及')
        ? `<span class="b">灰板${n.attrs.cover_board}</span>` : '';
  return `<span class="node" data-code="${code}" title="${(n.criteria||'').replace(/"/g,'')}">
      <span class="c">${code}</span><span class="n">${n.name}</span>${board}${extra||''}</span>`;
};
const esc = s => String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

function render(filter) {
  const kw = (filter||'').trim().toLowerCase();
  const out = [];
  if (!kw) {
    out.push(`<div class="sect">${esc(TREE.quick_pick.name)}</div>
      <div class="grp"><div class="hint">${esc(TREE.quick_pick.note)}</div>
      <div class="items">${TREE.quick_pick.items.map(i =>
        nodeEl(i.code, `<span class="b">${esc(i.why)}</span>`)).join('')}</div></div>`);
  }
  TREE.navigation.forEach(sect => {
    const groupsHtml = [];
    sect.groups.forEach(g => {
      const subs = (g.subgroups||[]).map(sg => {
        const items = sg.items.filter(c => !kw || (c+' '+TREE.nodes[c].name+' '+
              ((TREE.nodes[c].criteria)||'')+' '+((TREE.nodes[c].aliases||[]).join(' '))).toLowerCase().includes(kw));
        if (!items.length) return '';
        return `<div class="sub2">${esc(sg.name)}</div>
                <div class="items">${items.map(c => nodeEl(c)).join('')}</div>`;
      }).join('');
      if (!subs) return;
      groupsHtml.push(`<div class="grp"><div class="gt">${esc(g.file)}</div>
        <div class="hint">${esc(g.hint)}</div>${subs}</div>`);
    });
    if (groupsHtml.length) out.push(`<div class="sect">${esc(sect.name)}</div>
        <div class="grp"><div class="hint">${esc(sect.hint)}</div>${groupsHtml.join('')}</div>`);
  });
  if (!out.length) out.push('<div class="hint">没有匹配的类别。试试更短的关键词。</div>');
  document.getElementById('tree').innerHTML = out.join('');
  document.querySelectorAll('.node').forEach(el => el.onclick = () => {
    const code = el.dataset.code, n = TREE.nodes[code];
    navigator.clipboard && navigator.clipboard.writeText(n.label);
    toast(`已复制 ${n.label}`);
  });
}

document.getElementById('quickBody').innerHTML =
  '<table><tr><th>编号</th><th>类别</th><th>判据</th></tr>' +
  TREE.quick_pick.items.map(i => `<tr><td class="c" style="color:var(--code)">${i.code}</td>
    <td>${esc(TREE.nodes[i.code].name)}</td><td class="crit">${esc(i.why)}</td></tr>`).join('') +
  '</table>';

document.getElementById('paths').innerHTML = '<ol>' + TREE.paths.map(p => {
  const opts = p.options ? `<ul>${p.options.map(o => `<li>${esc(o)}</li>`).join('')}</ul>` : '';
  const yn = (p.yes||p.no) ? `<div class="crit">是 → ${esc(p.yes||'')}<br>否 → ${esc(p.no||'')}</div>` : '';
  return `<li><b>${esc(p.step)}</b>${yn}${opts}</li>`;
}).join('') + '</ol>';

function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.add('on');
  setTimeout(() => t.classList.remove('on'), 1300);
}
document.getElementById('q').addEventListener('input', e => render(e.target.value));
render('');
</script>
</body>
</html>
"""


def write_html(path: Path) -> Path:
    tree = load_tree()
    html = (HTML_TEMPLATE
            .replace("__TREE_JSON__", json.dumps(tree, ensure_ascii=False))
            .replace("__GENERATED_AT__", str(tree.get("generated_at", "")))
            .replace("__NODE_COUNT__", str(len(tree.get("nodes") or {})))
            .replace("__LEAF_COUNT__", str(sum(1 for n in (tree.get("nodes") or {}).values()
                                                if n.get("depth") == 2))))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return path


# ═══════════════════════════════════════════════════════════════════════════
# 五、CLI
# ═══════════════════════════════════════════════════════════════════════════
def _cmd_list() -> int:
    tree = load_tree()
    for section in tree.get("navigation") or []:
        print(f"\n{section['name']}")
        print(f"  {section['hint']}")
        for group in section.get("groups") or []:
            print(f"\n  {group['file']}")
            print(f"    {group['hint']}")
            for sub in group.get("subgroups") or []:
                print(f"    · {sub['name']}")
                for code in sub.get("items") or []:
                    node = nodes()[code]
                    print(f"        {node['label']:24s} {node.get('criteria','')}")
    return 0


def _cmd_group(name: str) -> int:
    tree = load_tree()
    hit = False
    for section in tree.get("navigation") or []:
        for group in section.get("groups") or []:
            if name in group["file"] or name == group.get("top"):
                hit = True
                print(f"{group['file']}\n  {group['hint']}")
                for sub in group.get("subgroups") or []:
                    print(f"\n  · {sub['name']}")
                    for code in sub.get("items") or []:
                        node = nodes()[code]
                        print(f"      {node['label']:24s} 灰板={node['attrs'].get('cover_board','-')}"
                              f"  特征={','.join(node['attrs'].get('features') or []) or '-'}")
                        if node.get("criteria"):
                            print(f"        {node['criteria']}")
    if not hit:
        print(f"没有分组匹配 {name!r}（用 --list 看全部）", file=sys.stderr)
        return 1
    return 0


def _cmd_node(code: str) -> int:
    node = nodes().get(code)
    if not node:
        print(f"树里没有编号 {code}", file=sys.stderr)
        return 1
    print(yaml.safe_dump({code: node}, allow_unicode=True, sort_keys=False))
    return 0


def _cmd_search(keyword: str) -> int:
    for hit in search(keyword):
        print(f"{hit['label']:26s} {hit['criteria']}")
    return 0


def _cmd_suggest(text: str) -> int:
    results = suggest(text)
    if not results:
        print("没有候选 —— 补充更多描述（装订 / 封面 / 内文特征）")
        return 1
    for r in results:
        print(f"[{r['score']:2d}] {r['label']}")
        if r["why"]:
            print(f"     命中: {', '.join(r['why'])}")
        if r["criteria"]:
            print(f"     定义: {r['criteria']}")
    return 0


def _cmd_validate() -> int:
    problems = validate()
    if not problems:
        print(f"✓ 产品类别树自检通过（{len(nodes())} 节点 / "
              f"{len(leaf_codes())} 可选子类 / 导航树全覆盖）")
        return 0
    for p in problems:
        print(f"✗ {p}")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="ERP 产品类别树（CPLB）：生成 / 查询 / 校验 / 出 HTML")
    parser.add_argument("--rebuild", action="store_true", help="从 assets/product_categories.xlsx 重新生成 yaml")
    parser.add_argument("--list", action="store_true", help="按导航树列出全部类别")
    parser.add_argument("--group", help="查看某个分组（编号如 27 或名称如 BB书）")
    parser.add_argument("--node", help="查看单个节点")
    parser.add_argument("--search", help="关键词搜索")
    parser.add_argument("--suggest", help="按自由文本给候选类别")
    parser.add_argument("--validate", action="store_true", help="自检 + 与 xlsx 交叉校验")
    parser.add_argument("--html", metavar="OUT", help="生成 HTML 选取器")
    args = parser.parse_args()

    if args.rebuild:
        path = write_tree()
        print(f"✓ 已生成 {path}（{len(nodes())} 节点）")
        problems = validate()
        for p in problems:
            print(f"✗ {p}")
        return 1 if problems else 0
    if args.list:
        return _cmd_list()
    if args.group:
        return _cmd_group(args.group)
    if args.node:
        return _cmd_node(args.node)
    if args.search:
        return _cmd_search(args.search)
    if args.suggest:
        return _cmd_suggest(args.suggest)
    if args.validate:
        return _cmd_validate()
    if args.html:
        out = write_html(Path(args.html))
        print(f"✓ 已生成 {out}")
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
