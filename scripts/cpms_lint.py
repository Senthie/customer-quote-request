#!/usr/bin/env python3
"""
cpms_lint.py — CPMS（产品描述）写法校验 / 机械规范化

单一真源：`assets/cpms_spec.yaml`（骨架、词表、槽位语法、规则码）
人读版本：`docs/CPMS_写作规范.md`

为什么要它：2021-2025 年 18476 份 ERP 导出实测 —— 91.2% 的 CPMS 已自发使用同一套
「书籍方向：【X】产品规格：【a*b*c】共【N】款」开头，但行首标签有 3176 种写法、
四段齐全的只有 53.9%、49.6% 混进了内部讨论块。规范写在文档里没人执行，
所以规则必须能一键跑出来。

用法：
    uv run ./scripts/cpms_lint.py <ERP.json>              # 校验单份产物
    uv run ./scripts/cpms_lint.py <目录> -r               # 递归校验（跳过 _superseded_）
    uv run ./scripts/cpms_lint.py <ERP.json> --json       # 机器可读报告
    uv run ./scripts/cpms_lint.py <ERP.json> --fix        # 只做机械规范化（改前备份 .bak）
    uv run ./scripts/cpms_lint.py --text "书籍方向：【长书脊】…"   # 直接校验一段文本
    uv run ./scripts/cpms_lint.py --vocab                 # 打印闭集词表

退出码 = error 条数（可以当门禁）。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = REPO_ROOT / "assets" / "cpms_spec.yaml"


# ─────────────────────────────────────────────────────────────
# 规范加载
# ─────────────────────────────────────────────────────────────
@dataclass
class Spec:
    raw: dict[str, Any]

    def __post_init__(self) -> None:
        self.sections: list[dict] = list(self.raw.get("sections") or [])
        self.order: dict[str, int] = {s["key"]: int(s["order"]) for s in self.sections}
        self.alias: dict[str, str] = {}
        for s in self.sections:
            for a in s.get("aliases") or []:
                self.alias[str(a)] = s["key"]
        self.rules: dict[str, dict] = dict(self.raw.get("rules") or {})
        self.header_re = re.compile(self.raw["header"]["regex"])
        vocab = self.raw.get("vocabulary") or {}
        self.paper = set(map(str, vocab.get("paper") or []))
        self.paper_syn = dict(vocab.get("paper_synonyms") or {})
        self.finish = list(map(str, vocab.get("finish") or []))
        self.binding = set(map(str, vocab.get("binding") or []))
        self.finish_order = list(map(str, self.raw.get("finish_order") or []))

    @property
    def keys(self) -> set[str]:
        return set(self.order)


def load_spec(path: Optional[Path] = None) -> Spec:
    src = Path(path) if path else SPEC_PATH
    with open(src, "r", encoding="utf-8") as f:
        return Spec(yaml.safe_load(f) or {})


# ─────────────────────────────────────────────────────────────
# 校验
# ─────────────────────────────────────────────────────────────
@dataclass
class Finding:
    code: str
    level: str
    line: int
    msg: str

    def as_dict(self) -> dict:
        return {"code": self.code, "level": self.level, "line": self.line, "msg": self.msg}


LABEL_RE = re.compile(r"^(?P<indent>[ ]*)(?P<label>[^\s：:，。；]{1,12})[：:](?P<value>.*)$")
ITEM_RE = re.compile(r"^[ ]*(?P<num>\d{1,2})\s*[.．、)]\s*(?P<name>\S.*)$")


def lint_text(text: str, spec: Spec, record: Optional[dict] = None) -> list[Finding]:
    """校验一段 CPMS。record 给定时，顺带核对概要行与 59 字段是否一致。"""
    out: list[Finding] = []
    raw = text or ""

    def add(code: str, line: int, msg: str) -> None:
        level = spec.rules.get(code, {}).get("level", "warn")
        out.append(Finding(code, level, line, msg))

    # ── 版式（先按原始文本查，修完再看一遍）──
    if "\r" in raw:
        add("CPMS001", 0, f"存在 {raw.count(chr(13))} 个 CR/CRLF —— 只允许 LF")
    if "\t" in raw:
        add("CPMS002", 0, f"存在 {raw.count(chr(9))} 个制表符 —— 缩进用 2 个空格")
    if raw != raw.strip():
        add("CPMS005", 0, "CPMS 首尾有空白字符")
    if re.search(r"={5,}|-{5,}", raw):
        add("CPMS006", 0, "存在 ===== / ----- 分隔线")
    if re.search(r"【[\u4e00-\u9fff]{2,4}\s{2,}\d{4,8}】|产品补充描述", raw):
        add("CPMS007", 0, "存在【姓名   日期】产品补充描述式内部讨论块 —— 应折算成备注一行或丢弃")

    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")

    if re.search(r"\n[ \u3000]*\n[ \u3000]*\n", text):
        add("CPMS004", 0, "存在连续多个空行 —— 最多一个空行")
    for i, line in enumerate(lines, 1):
        if re.search(r"[ \u3000]+$", line):
            add("CPMS003", i, "行尾有空格")

    if not text.strip():
        add("CPMS040", 0, "CPMS 为空 —— 视为未完成，不得交付")
        return out

    stripped = [l for l in lines if l.strip()]
    if not stripped:
        add("CPMS040", 0, "CPMS 只有空白字符")
        return out

    # ── 概要行 ──
    head_idx = next((i for i, l in enumerate(lines) if l.strip().startswith(("书籍方向：", "产品规格："))), None)
    comp_pieces = _composition_pieces(text)
    if head_idx is None:
        add("CPMS010", 1, "缺少概要行「书籍方向：【X】产品规格：【a*b*c】共【N】款」")
    else:
        m = spec.header_re.match(lines[head_idx].strip())
        if not m:
            add("CPMS011", head_idx + 1, "概要行格式不合规范（应为 书籍方向：【长书脊】产品规格：【CD*KD*GD】共【N】款/件）")
        elif record:
            _check_header(m, record, spec, head_idx + 1, add, comp_pieces)
        dup = [i for i, l in enumerate(lines) if i != head_idx and l.strip().startswith(("书籍方向：", "产品规格：【"))]
        if dup:
            add("CPMS012", dup[0] + 1, f"概要行重复出现（共 {len(dup) + 1} 次）")

    # ── 段落：标签 / 顺序 / 空值 ──
    blocks: list[list[tuple[int, str, int]]] = [[]]          # [(行号, 规范标签, order)]
    label_values: dict[str, tuple[int, str]] = {}            # 标签 → (行号, 值)
    seen_item = False
    for i, line in enumerate(lines, 1):
        s = line.strip()
        if not s:
            continue
        if s.startswith("- "):                                # 三级子项，豁免词表
            continue
        if i - 1 == head_idx:
            continue
        item = ITEM_RE.match(line)
        if item:
            rest = item.group("name").strip()
            if not (rest and LABEL_RE.match(rest)):
                # 款 / 件标题行：开一个新块，块内重新检查段落顺序
                if seen_item:
                    blocks.append([])
                seen_item = True
                continue
            add("CPMS023", i, f"序号「{item.group('num')}.」与部件标签写在同一行 —— 序号单独一行，部件另起一行")
            line = "  " + rest                                # 折成款内缩进的标签行继续校验
        m = LABEL_RE.match(line)
        if not m:
            continue                                          # 无标签的自由文本行：允许
        label, value = m.group("label"), m.group("value").strip()
        pos = len(m.group("indent")) + len(label)
        if pos < len(line) and line[pos] == ":":
            add("CPMS008", i, f"标签「{label}」用了半角冒号")
        if not value:
            add("CPMS022", i, f"空标签「{label}」—— 推不出来就整行不写")
            continue
        if label in spec.alias:
            canon = spec.alias[label]
            add("CPMS021", i, f"标签「{label}」应改为规范写法「{canon}」")
            label = canon
        elif label not in spec.keys:
            hint = _suggest(label, spec)
            add("CPMS020", i, f"标签「{label}」不在词表内" + (f"（候选：{hint}）" if hint else "（自定义子项请用「- 子项：值」）"))
            continue
        blocks[-1].append((i, label, spec.order[label]))
        label_values.setdefault(label, (i, value))
        _check_value(label, value, i, spec, add)

    for b in blocks:
        for (l1, n1, o1), (l2, n2, o2) in zip(b, b[1:]):
            if o2 < o1:
                add("CPMS030", l2, f"段落顺序错误：「{n2}」不应出现在「{n1}」之后（顺序：部件 → 装订 → 包装 → 装箱 → 箱唛 → 备注）")

    # 装订段必须写**客户原文的装订方式**，不是 ERP「装订方式 ZDFS」的下拉值。
    # 真 bug（2026-10-20，CU26070802）：CPMS 写成「装订：胶装/PAD书…」——那是 ZDFS 枚举
    # （一个字面同时代表「胶装」和「PAD 书」两类），客户原文是「冲形状无线胶装书（冲鸡蛋形状）」。
    if "装订" in label_values:
        line_no, binding_text = label_values["装订"]
        if re.search(r"胶装\s*/\s*PAD|PAD\s*书", binding_text):
            add("CPMS055", line_no,
                "装订段写的是 ERP「装订方式 ZDFS」的下拉值（胶装/PAD书）—— "
                "要按客户原文写实际装订方式（如 冲形状无线胶装书 / 无线胶装书 / 骑马钉）")

    _check_required(lines, blocks, spec, record, add)

    if len(text.strip()) < 60:
        add("CPMS070", 0, f"CPMS 过短（{len(text.strip())} 字）—— 是否漏了部件或装箱要求")
    elif len(text.strip()) > 4000:
        add("CPMS070", 0, f"CPMS 过长（{len(text.strip())} 字，历史 p99 = 2149）—— 建议把变更过程移到备注")
    return out


COMP_ITEM_QTY_RE = re.compile(
    r"^\s*(?P<n>\d+(?:\.\d+)?)\s*(?:本|张|个|支|条|块|片|款|套|只|对|包|袋|卷|枚|组|种)?"
)


def _composition_pieces(text: str) -> float:
    """套装组成声明的**实物件数**：`1套=A+B+C` 里每项开头数量之和，没写数量算 1 件。

    例：`陈列盒+4款卡片+信封+自粘袋` → 1+4+1+1 = 7 件；`皮壳+24本骑钉书+24个口袋` → 1+24+24 = 49 件。
    没有组成声明返回 0（推不出来 -> 不强制写件数）。
    """
    m = re.search(r"^(?:[ \u3000]*)(?:1\s*套|一套|整套)\s*[=＝]\s*(.+)$", text, re.MULTILINE)
    if not m:
        return 0
    total = 0.0
    for item in re.split(r"[+＋]", m.group(1).strip()):
        if not item.strip():
            continue
        q = COMP_ITEM_QTY_RE.match(item)
        total += float(q.group("n")) if q else 1.0
    return total


def _is_set(record: dict) -> bool:
    """套装 / 盒类判定：CPLB 顶层 29~30，或 ZDFS = 套装。"""
    cplb = str(record.get("CPLB") or "")
    top = cplb.split(";")[0].split(".")[0]
    return top in {"29", "30"} or str(record.get("ZDFS") or "") == "套装"


def _check_header(m: re.Match, record: dict, spec: Spec, line: int, add, comp_pieces: float = 0) -> None:
    """概要行必须与 59 字段一致 —— 这一行是派生数据，不许手写漂移。"""
    def num(v: Any) -> Optional[float]:
        try:
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    g = m.groupdict()
    if not g.get("spine") and not _is_set(record):
        add("CPMS013", line,
            "书本类概要行缺「书籍方向：【长书脊/短书脊】」—— 装订边位置 + 版式能判就必须写"
            "（Drop_SJFX 为空只表示 ERP 侧没落库，不代表 CPMS 可以不写方向）")
    if g.get("spine") and record.get("Drop_SJFX") and g["spine"] != record["Drop_SJFX"]:
        add("CPMS011", line, f"书籍方向={g['spine']} 与 Drop_SJFX={record['Drop_SJFX']} 不一致")
    for key, fieldname in (("cd", "CD"), ("kd", "KD"), ("gd", "GD")):
        want, got = num(record.get(fieldname)), num(g.get(key))
        if want is not None and got is not None and abs(want - got) > 0.6:
            add("CPMS011", line, f"产品规格 {key.upper()}={got:g} 与 {fieldname}={want:g} 不一致（差 >0.5mm）")

    yys, got_yys = num(record.get("YYS")), num(g.get("yys"))
    if yys is not None and got_yys is not None and abs(yys - got_yys) > 0.01:
        add("CPMS011", line, f"概要行 YYS={got_yys:g} 与 YYS={yys:g} 不一致")
    if yys and yys > 1 and not g.get("yys"):
        add("CPMS011", line, f"YYS={yys:g} > 1，概要行必须写「，每款【{yys:g}】语言」")

    # ── 计数词：书本类 = 款（TZS），套装/多件 = 件（组成声明里的实物件数）──
    tzs, got_tzs = num(record.get("TZS")), num(g.get("tzs"))
    unit = g.get("unit")
    if _is_set(record):
        if unit == "款":
            add("CPMS011", line, "套装/盒类应写「共【N】件」，不写「款」（业务 2026-10-19 拍板）")
        elif comp_pieces >= 2:
            if unit != "件":
                add("CPMS011", line, f"套装实物共 {comp_pieces:g} 件，概要行必须写「共【{comp_pieces:g}】件」")
            elif got_tzs is not None and abs(got_tzs - comp_pieces) > 0.01:
                add("CPMS011", line, f"件数={got_tzs:g} 与组成声明实物件数={comp_pieces:g} 不一致")
    else:
        if unit == "件":
            add("CPMS011", line, "书本类应写「共【N】款」，不写「件」")
        if unit and tzs is not None and got_tzs is not None and abs(tzs - got_tzs) > 0.01:
            add("CPMS011", line, f"概要行 TZS={got_tzs:g} 与 TZS={tzs:g} 不一致")
        if tzs and tzs > 1 and not unit:
            add("CPMS011", line, f"TZS={tzs:g} > 1，概要行必须写「共【{tzs:g}】款」")


def _check_value(label: str, value: str, line: int, spec: Spec, add) -> None:
    """取值语法只做「窄而确定」的检查 —— 宁可漏判，不可误判。"""
    if re.search(r"\d+(?:\.\d+)?\s*(?:gsm|克)", value):
        add("CPMS050", line, "克重单位应为 g（如 300g单粉），不写 gsm / 克")
    if re.search(r"(?<=\d)\s*[xX×]\s*(?=\d)", value):
        add("CPMS051", line, "尺寸分隔符应为 *（如 228.6*254mm），不写 x / X / ×")
    # 灰板只认 g：mm 只在「不是尺寸串」时算违规（300*200mm 里的 200mm 是尺寸，不报）
    if "灰板" in label or "灰板" in value:
        bad_mm = False
        for seg in value.split("，"):
            if "灰板" not in seg and "灰板" not in label:
                continue
            for m in re.finditer(r"\d+(?:\.\d+)?\s*mm", seg):
                if seg[: m.start()].rstrip().endswith("*"):
                    continue
                bad_mm = True
        if bad_mm:
            add("CPMS054", line, "灰板厚度用 g 表示（600g=1mm / 900g=1.5mm / 1200g=2mm / 1500g=2.5mm / 1800g=3mm），不写 mm")

    # 印色：抓「数字[+工艺]/数字」并核对严格形式 {n}C(+工艺)*/{m}C
    for tok in re.finditer(r"(?<![\d.*])(\d{1,2})\s*([cC])?\s*((?:\+[^/，；*\n]{1,20})*)\s*[/／]\s*(\d{1,2})\s*([cC])?(?![\d.*])", value):
        whole = tok.group(0).strip()
        strict = re.match(r"^(\d{1,2})C((?:\+[^/]+)*)/(\d{1,2})C$", whole)
        if not strict:
            add("CPMS052", line, f"印色「{whole}」写法不规范 —— 应为「{tok.group(1)}C{'+工艺' if tok.group(3) else ''}/{tok.group(4)}C」")
            break
        # 表面处理顺序：只看正面（斜杠之前）的 + 段
        front = whole.split("/")[0]
        finishes = []
        for w in re.split(r"[+＋]", front)[1:]:
            base = re.sub(r"[（(].*?[)）]", "", w).strip()      # 局部UV(30%) → 局部UV
            if base in spec.finish:
                finishes.append(base)
        idx = [spec.finish_order.index(f) for f in finishes if f in spec.finish_order]
        if idx != sorted(idx):
            add("CPMS053", line, f"表面处理顺序应为 {' → '.join(spec.finish_order)}（当前：{' → '.join(finishes)}）")


def _check_required(lines: list[str], blocks, spec: Spec, record: Optional[dict], add) -> None:
    labels = [n for b in blocks for (_, n, _) in b]
    if "装箱" not in labels:
        add("CPMS040", 0, "缺少「装箱」段 —— 装箱数量与纸板要求必须写")
    # 书本类（CPLB 顶层 22~28）必须有正文部件与装订
    top = ""
    if record:
        cplb = str(record.get("CPLB") or "")
        top = cplb.split(";")[0].split(".")[0]
    book_like = top in {"22", "23", "24", "25", "26", "27", "28"} or (record and record.get("ZDFS") not in (None, "", "套装"))
    if book_like:
        body = {"皮壳", "封面", "前封面", "封面连内文"}
        if not (body & set(labels)):
            add("CPMS040", 0, "缺少封面/皮壳段 —— 书本类必须写封面材料与印刷")
        if "内文" not in labels and "封面连内文" not in labels and "内文连衬纸" not in labels:
            add("CPMS040", 0, "缺少「内文」段 —— 页数、纸张、印色必须写")
        if "装订" not in labels:
            add("CPMS040", 0, "缺少「装订」段")
    # 商务要求必须在装箱之后（按行位置，不是 order 值 —— 两条规则说的是两件事）
    rows = [(l, n) for b in blocks for (l, n, _) in b]
    pack_line = next((l for l, n in rows if n == "装箱"), None)
    if pack_line:
        for l, n in rows:
            if n in {"请拆分", "请选配", "客户目标价"} and l < pack_line:
                add("CPMS060", l, f"「{n}」属于报价备注，应放在装箱之后（当前第 {l} 行，装箱在第 {pack_line} 行）")


def _suggest(label: str, spec: Spec) -> str:
    for k in spec.keys:
        if label in k or k in label:
            return k
    return ""


# ─────────────────────────────────────────────────────────────
# 机械规范化（只改版式与已定同义词，不动语义）
# ─────────────────────────────────────────────────────────────
INK_SLOT_RE = re.compile(
    r"^(?P<a>\d{1,2})\s*(?:[cC])?\s*(?P<fin>(?:\+[^/，；*\n]{1,24})*)\s*[/／]\s*(?P<b>\d{1,2})\s*(?:[cC])?$"
)


def _fix_ink_slot(value: str) -> tuple[str, bool]:
    """部件行取值里的印色槽补 C：整槽形如 4/0c、4c+光油/0c、4C+哑胶/0 → 4C/0C、4C+光油/0C、4C+哑胶/0C。

    只在「整槽」上动手（值按中文逗号切成槽），避免把 1/2 这类比例改成 1C/2C。
    """
    changed = False
    out: list[str] = []
    for seg in value.split("，"):
        s = seg.strip()
        m = INK_SLOT_RE.match(s)
        if not m:
            out.append(seg)
            continue
        new = f"{int(m.group('a'))}C{m.group('fin') or ''}/{int(m.group('b'))}C"
        if new != s:
            changed = True
        out.append(new)
    return "，".join(out), changed


def normalize_text(text: str, spec: Spec) -> tuple[str, list[str]]:
    changes: list[str] = []
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if t != text:
        changes.append("CR/CRLF → LF")
    if "\t" in t:
        t = t.replace("\t", "  ")
        changes.append("制表符 → 2 空格")

    out: list[str] = []
    for line in t.split("\n"):
        orig = line
        if re.fullmatch(r"\s*[=\-]{5,}\s*", line):
            changes.append("删除分隔线")
            continue
        line = line.rstrip(" \u3000")
        if line != orig and orig.strip():
            changes.append("行尾空格")
        m = LABEL_RE.match(line)
        p = len(m.group("indent")) + len(m.group("label")) if m else 0
        if m and p < len(line) and line[p] == ":":
            line = line[: len(m.group("indent")) + len(m.group("label"))] + "：" + m.group("value")
            changes.append(f"半角冒号 → 全角（{m.group('label')}）")
        m = LABEL_RE.match(line)
        if m and m.group("label") in spec.alias:
            canon = spec.alias[m.group("label")]
            line = line.replace(m.group("label") + "：", canon + "：", 1)
            changes.append(f"标签 {m.group('label')} → {canon}")
        # 印色补 C：只在部件行（order 100~299）的整槽上动手
        m = LABEL_RE.match(line)
        if m:
            canon = spec.alias.get(m.group("label"), m.group("label"))
            order = spec.order.get(canon)
            if order is not None and 100 <= order <= 299:
                new_value, ink_changed = _fix_ink_slot(m.group("value"))
                if ink_changed:
                    line = line[: m.start("value")] + new_value
                    changes.append(f"印色补 C（{canon}）")
        # 窄替换：数字后面的 gsm/克 → g；数字之间的 x → *
        new = re.sub(r"(?<=\d)\s*(?:gsm|克)(?=\s*(?:[\u4e00-\u9fff(（]|$))", "g", line)
        new = re.sub(r"(?<=\d)\s*[xX×]\s*(?=\d)", "*", new)
        if new != line:
            changes.append("单位/尺寸分隔符规范化")
            line = new
        out.append(line)

    t = "\n".join(out)
    t2 = re.sub(r"\n{3,}", "\n\n", t)
    if t2 != t:
        changes.append("连续空行 → 一个")
        t = t2
    t = t.strip()
    # 去重输出（同一行里多次触发只报一次）
    uniq: list[str] = []
    for c in changes:
        if c not in uniq:
            uniq.append(c)
    return t, uniq


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────
def _iter_json(target: Path, recursive: bool) -> Iterable[Path]:
    if target.is_file():
        yield target
        return
    pattern = "**/*.json" if recursive else "*.json"
    for p in sorted(target.glob(pattern)):
        if "_superseded_" in p.parts:
            continue
        yield p


def _report(path: Optional[Path], findings: list[Finding], cpms_len: int, as_json: bool) -> dict:
    err = [f for f in findings if f.level == "error"]
    warn = [f for f in findings if f.level != "error"]
    if as_json:
        return {"path": str(path) if path else None, "cpms_len": cpms_len,
                "errors": len(err), "warnings": len(warn), "findings": [f.as_dict() for f in findings]}
    print(f"\n{'=' * 70}\n{path or '<text>'}   CPMS {cpms_len} 字")
    for f in findings:
        tag = "ERROR" if f.level == "error" else " WARN"
        print(f"  [{tag}] {f.code}  L{f.line or '-':<3} {f.msg}")
    if not findings:
        print("  ✅ 无违规")
    print(f"  小结：{len(err)} error / {len(warn)} warn")
    return {"path": str(path) if path else None, "errors": len(err), "warnings": len(warn)}


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="CPMS 产品描述写法校验（规范：assets/cpms_spec.yaml）")
    ap.add_argument("target", nargs="?", help="ERP JSON 文件或目录")
    ap.add_argument("-r", "--recursive", action="store_true", help="目录递归")
    ap.add_argument("--text", help="直接校验一段 CPMS 文本")
    ap.add_argument("--json", action="store_true", help="输出 JSON 报告")
    ap.add_argument("--fix", action="store_true", help="机械规范化后写回（先备份 .bak）")
    ap.add_argument("--vocab", action="store_true", help="打印闭集词表")
    args = ap.parse_args(argv)

    spec = load_spec()

    if args.vocab:
        print(f"规范：{SPEC_PATH}\n")
        print("段落（按输出顺序）：")
        for s in spec.sections:
            print(f"  {s['order']:>3}  {s['key']:<8} {s['group']:<6} 别名={s.get('aliases') or '—'}")
        print(f"\n纸张：{' / '.join(sorted(spec.paper))}")
        print(f"表面处理（按书写顺序）：{' → '.join(spec.finish)}")
        print(f"装订：{' / '.join(sorted(spec.binding))}")
        return 0

    if args.text is not None:
        findings = lint_text(args.text, spec)
        _report(None, findings, len(args.text.strip()), args.json)
        return sum(1 for f in findings if f.level == "error")

    if not args.target:
        ap.error("需要 ERP JSON 路径 / 目录，或 --text / --vocab")

    target = Path(args.target)
    if not target.exists():
        ap.error(f"找不到：{target}")

    total_err = 0
    results = []
    for path in _iter_json(target, args.recursive):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as e:                                    # noqa: BLE001
            print(f"跳过（不是合法 JSON）：{path} —— {e}")
            continue
        cpms = data.get("CPMS") or ""
        if args.fix and cpms:
            fixed, changes = normalize_text(cpms, spec)
            if changes:
                shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
                data["CPMS"] = fixed
                path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                print(f"  🔧 {path.name}：{', '.join(changes)}")
            cpms = fixed
        findings = lint_text(cpms, spec, data)
        results.append(_report(path, findings, len(cpms.strip()), args.json))
        total_err += sum(1 for f in findings if f.level == "error")

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    return total_err


if __name__ == "__main__":
    sys.exit(main())
