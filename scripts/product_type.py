#!/usr/bin/env python3
"""判 ERP 字段 `CPLX`（产品类型）：新产品 / 返单产品 / 半返单旧产品 / 半返单新产品 / 特殊新产品。

口径来源：森西 2026-11-20 上传的《客户订单/客户报价需求单的产品类别方式》，
原文 + 操作口径见 `docs/产品类型判定.md`。核心三句：

1. 第一次做货 → `新产品`；第一次做且有特殊工艺/特殊纸面处理/新工艺 → `特殊新产品`。
2. 做过的货 → 只看「这次和上次差什么」：
   只差文件内容 / 装箱方式 / 贴纸 → `半返单旧产品`；
   差刀线、形状、窗口、小部件、用料、做法、工艺优化 → `半返单新产品`；
   规格材料文件一模一样 → `返单产品`。
3. NGV 系客户没明说「文件跟上次订单一模一样」→ `半返单旧产品`（客户极少全返单）。
   多款组合取最高档：特殊新产品 > 新产品 > 半返单新产品 > 半返单旧产品 > 返单产品。

查历史单（原文「通过客户订单号去 ERP 列表-客户订单查，先按规格、再按产品名称，查不到 = 新产品」）：
`--lookup` 时调 MCP `erp_sales_order_list`。实测三个坑写在 docs 里：
中文「产品名称」关键字检索不生效（用 `客户单号` / `产品英文名`）、必须显式给日期区间、
一行 = 一个订单产品行（取最近制单日期）。

用法：
  uv run ./scripts/product_type.py --text customer-files/processed/<task> --customer NGV10
  uv run ./scripts/product_type.py --text <task> --customer NGV10 --lookup      # 含 ERP 证据
  uv run ./scripts/product_type.py --text <task> --lookup --json                # 机器读

退出码：0 = 有结论（confidence 无论高低）；2 = 读不到原文/参数错。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

# ERP 下拉的五个合法值
NEW = "新产品"
NEW_SPECIAL = "特殊新产品"
REPEAT_SAME = "返单产品"
REPEAT_OLD = "半返单旧产品"
REPEAT_NEW = "半返单新产品"
ALL_TYPES = [NEW, REPEAT_SAME, REPEAT_OLD, REPEAT_NEW, NEW_SPECIAL]

# 多款组合取最高档（原文顺序：特殊新产品 > 新产品 > 半返单新款 > 半返单旧款 > 全返单）
PRIORITY = {NEW_SPECIAL: 5, NEW: 4, REPEAT_NEW: 3, REPEAT_OLD: 2, REPEAT_SAME: 1}


def pick_highest(types: list[str]) -> str:
    types = [t for t in types if t in PRIORITY]
    return max(types, key=lambda t: PRIORITY[t]) if types else NEW


# ── 文本信号（中 / 英 / 德）────────────────────────────────────────────
RE_REPRINT = re.compile(
    r"reprint|re-print|nachdruck|wiederauflage|re-?order|repeat order|"
    r"返单|翻单|再版|重印", re.I)
# ★ 只认「整体一样」的说法。裸的 `unchanged` 不能算 —— 实测「new cover file, packing unchanged」
#   说的是装箱没变、文件变了（= 半返单旧产品），被裸 unchanged 判成「返单产品」就错了。
RE_SAME = re.compile(
    r"exactly the same|same as (the )?(last|previous)( order)?|identical (to|with)|"
    r"(files?|artwork|print(ing)? files?|specs?|specification)s?\s+(are\s+|is\s+)?"
    r"(all\s+)?(unchanged|not changed|the same)|"
    r"wie beim letzten|unverändert|unveraendert|"
    r"跟上次(订单)?(完全)?一样|与上次(订单)?(完全)?一样|一模一样|完全相同", re.I)
RE_FILE_ONLY = re.compile(
    r"new cover|cover (file|change)|updated file|revised file|new file|"
    r"neue datei|neues cover|"
    r"封面文件|文件内容|改文件|文件修改|装箱方式|装箱数量|贴纸|sticker|aufkleber|packing", re.I)
RE_DIE_OR_SHAPE = re.compile(
    r"die[- ]?line|new die|stanzform|tooling|"
    r"window|shape change|cut ?out|"
    r"窗口|形状|刀线|刀模|开版|模切|小部件|部件|转轮|机构|mechanism|wheel", re.I)
# ★ 裸 `gsm` 不能算「用料变更」（2026-11-20 实测 TEV21 Kleines Stickerbuch）：
#   任何规格都会写 `250 gsm C1S`，把单位当变更信号 → 同一系列同规格的续报被判成
#   「半返单新产品」。只认「克重真的变了」的说法。
RE_MATERIAL = re.compile(
    r"material change|change (the )?material|reduce cost|cost down|cheaper|lighter|"
    r"(?:higher|lower|increase[d]?|reduce[d]?)\s*(?:gsm|paper weight)|"
    r"gsm (?:change|changed|increase|reduction)|克重(?:变更|增加|调整|改变)|"
    r"用料变更|换料|减成本|降本|改纸|纸质变更", re.I)
RE_PROCESS_OPT = re.compile(
    r"optimi[sz]|improve(ment)?|optimier|verbessert|工艺优化|优化|改善|做法更改", re.I)
RE_NEW_PROJECT = re.compile(
    r"new project|neues projekt|new title|neuer titel|first order|new customer|"
    r"新项目|新客户|第一次做|新品|新书", re.I)
RE_SPECIAL_PROCESS = re.compile(
    r"烫金|foil|spot ?uv|局部 ?uv|击凸|emboss|植绒|flock|闪粉|glitter|"
    r"发声|sound|电子|electronic|灯光|led|投影|projection|手电筒|taschenlampe|"
    r"pop[- ]?up|立体|拼插|磁|magnet|异形|特殊纸|触感|touch|sensory|香味|scent|变色", re.I)


def hits(pattern: re.Pattern, text: str, limit: int = 4) -> list[str]:
    found: list[str] = []
    for m in pattern.finditer(text):
        token = m.group(0).strip()
        if token and token not in found:
            found.append(token)
        if len(found) >= limit:
            break
    return found


# ── 原文读取 ────────────────────────────────────────────────────────────
def read_text(target: Path) -> str:
    """任务目录 → 拼所有 .txt（优先 *_email.meta.json 里的正文）；单个文件 → 直读。"""
    if target.is_file():
        return target.read_text(encoding="utf-8", errors="replace")
    if not target.is_dir():
        raise FileNotFoundError(f"读不到 {target}")

    parts: list[str] = []
    for meta in sorted(target.rglob("_email.meta.json")):
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
            if data.get("subject"):
                parts.append(str(data["subject"]))
            if data.get("body"):
                parts.append(str(data["body"]))
        except (OSError, json.JSONDecodeError):
            pass
    for txt in sorted(target.rglob("*.txt")):
        if txt.name.endswith(".meta.json"):
            continue
        try:
            parts.append(txt.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    if not parts:  # 退到 csv / json 里的字段
        for other in sorted(target.rglob("*.csv")):
            parts.append(other.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


def extract_po(text: str) -> list[str]:
    """客户单号 / PO 号候选（PO#16258-01 / POR460115608 / 16258-01 …）。"""
    out: list[str] = []
    for m in re.finditer(r"\b(?:PO|PO#|P\.O\.|POR|CU|KHDD)\s*#?\s*([0-9][0-9\-/]{3,})", text, re.I):
        val = m.group(1).strip(" -/")
        if val and val not in out:
            out.append(val)
    return out


# ── ERP 历史单查询 ──────────────────────────────────────────────────────
def make_client(url: str = "", token: str = ""):
    from erp_push import McpHttpClient  # 复用现成的 streamable-HTTP 客户端
    client = McpHttpClient(url=url, token=token) if url else McpHttpClient(token=token)
    client.initialize()
    return client


def lookup_history(client: Any, *, po_list: list[str], spec: str = "",
                   english_name: str = "", from_date: str = "", to_date: str = "") -> list[dict[str, Any]]:
    """按 客户单号 → 产品英文名 → 规格 依次查客户订单，返回命中的产品行（最近在前）。"""
    import time
    to_date = to_date or time.strftime("%Y-%m-%d")
    from_date = from_date or f"{int(to_date[:4]) - 2}-01-01"

    queries: list[dict[str, str]] = []
    for po in po_list[:3]:
        queries.append({"key": po, "field": "客户单号"})
    if english_name:
        queries.append({"key": english_name, "field": "产品英文名"})
    if spec:
        queries.append({"key": spec, "field": "规格"})

    seen: dict[str, dict[str, Any]] = {}
    tried: list[str] = []
    for q in queries:
        tried.append(f"{q['field']}={q['key']}")
        try:
            res = client.call_tool("erp_sales_order_list",
                                   {**q, "from_date": from_date, "to_date": to_date, "limit": 50})
        except Exception as exc:  # 查询失败不该让判定崩，记下来降级
            tried.append(f"(失败: {exc})")
            continue
        rows = (res or {}).get("records") or []
        for row in rows:
            key = f"{row.get('表单编号')}|{row.get('产品名称')}|{row.get('规格')}"
            if key in seen:
                continue
            seen[key] = {
                "表单编号": row.get("表单编号"),
                "客户名称": row.get("客户名称"),
                "产品名称": row.get("产品名称"),
                "产品英文名": row.get("产品英文名"),
                "规格": row.get("规格"),
                "产品类型": row.get("产品类型"),
                "订单数量": row.get("订单数量"),
                "制单日期": row.get("制单日期"),
                "客户单号": row.get("客户单号"),
                "matched_by": f"{q['field']}={q['key']}",
            }
    # 排序：**客户单号命中排最前**（原文口径：有 PO 号就照那单抄），其次产品英文名，最后规格；
    # 同一档内制单日期新→旧。别只按日期排 —— 同日系不同客户的老单会跑到最前面（实测 MOP17
    # 的 2026-08 单压过了真正对应的 PO#16258-01 单）。
    rank = {"客户单号": 0, "产品英文名": 1, "规格": 2}

    def _batch(row: dict[str, Any]) -> int:
        return rank.get(str(row.get("matched_by", "")).split("=")[0], 9)

    rows = sorted(seen.values(), key=lambda r: str(r.get("制单日期") or ""), reverse=True)
    rows.sort(key=_batch)  # 稳定排序：先按日期倒序，再按命中档位分组
    if rows:
        rows[0]["_queries"] = tried
    return rows


# ── 客户档案里的默认档 ──────────────────────────────────────────────────
def profile_defaults(customer: str) -> dict[str, str]:
    """读 customers/<档案>.rules.product_type.{default,new_when_no_history}。

    这样 `customers/ngv.yaml` 里那条「NGV 返单默认半返单旧产品」是**活的配置**，
    不是注释里的死知识。档案读不到就用内置默认（新产品 / 半返单旧产品）。
    """
    out = {"default": REPEAT_OLD, "new_when_no_history": NEW}
    code = (customer or "").strip().upper()
    if not code:
        return out
    key = re.sub(r"[0-9]+$", "", code).lower()  # NGV10 / NGVS16 → ngv
    try:
        import yaml
        path = REPO / "customers" / f"{key}.yaml"
        if not path.is_file():
            return out
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        block = ((cfg.get("rules") or {}).get("product_type") or {})
        for k in ("default", "new_when_no_history"):
            val = block.get(k)
            if isinstance(val, str) and val in ALL_TYPES:
                out[k] = val
    except Exception:  # 档案坏了不要影响判定
        pass
    return out


# ── 判定 ────────────────────────────────────────────────────────────────
def classify(text: str, customer: str = "", history: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    history = history or []
    customer = (customer or "").upper()
    is_ngv = customer.startswith("NGV")

    ev = {
        "reprint": hits(RE_REPRINT, text),
        "same_as_before": hits(RE_SAME, text),
        "file_only": hits(RE_FILE_ONLY, text),
        "die_or_shape": hits(RE_DIE_OR_SHAPE, text),
        "material": hits(RE_MATERIAL, text),
        "process_opt": hits(RE_PROCESS_OPT, text),
        "new_project": hits(RE_NEW_PROJECT, text),
        "special_process": hits(RE_SPECIAL_PROCESS, text),
        "po_list": extract_po(text),
    }
    structure_change = bool(ev["die_or_shape"] or ev["material"] or ev["process_opt"])
    same = bool(ev["same_as_before"])
    is_repeat = bool(ev["reprint"]) or bool(history)

    if history:
        prev = history[0]
        prev_type = prev.get("产品类型") or ""
        prev_ref = f"{prev.get('表单编号')}（{prev.get('制单日期')}，{prev.get('客户名称')}，{prev.get('产品类型')}）"
        if structure_change:
            cplx, why = REPEAT_NEW, f"有刀线/形状/部件/用料/工艺变更 → 半返单新产品（历史单 {prev_ref}）"
        elif same:
            cplx, why = REPEAT_SAME, f"客户明说与上次一模一样 → 返单产品（历史单 {prev_ref}）"
        else:
            cplx, why = REPEAT_OLD, (
                f"做过（历史单 {prev_ref}），只有文件内容/装箱/贴纸这类变动或未提及结构变更 → 半返单旧产品")
        if prev_type and prev_type in ALL_TYPES and not structure_change:
            why += f"；ERP 该单本身也是「{prev_type}」"
        return {"product_type": cplx, "confidence": "high" if (history and not structure_change) else "medium",
                "reason": why, "evidence": ev, "history": history[:5]}

    # 没有历史单
    defaults = profile_defaults(customer)
    if is_ngv and is_repeat:
        return {"product_type": defaults["default"], "confidence": "medium",
                "reason": (f"NGV 返单但没查到历史单：按 NGV 默认档落{defaults['default']}"
                           "（客户极少全返单，每单都会改封面文件），需业务确认"),
                "evidence": ev, "history": []}
    if is_repeat:
        return {"product_type": defaults["default"], "confidence": "low",
                "reason": f"客户称返单但 ERP 没查到历史单：先按{defaults['default']}落，需业务确认",
                "evidence": ev, "history": []}
    if ev["special_process"] and not same:
        return {"product_type": NEW_SPECIAL, "confidence": "medium",
                "reason": "查不到历史单 + 命中特殊工艺/特殊纸面处理 → 特殊新产品",
                "evidence": ev, "history": []}
    new_type = defaults["new_when_no_history"]
    return {"product_type": new_type, "confidence": "medium" if not history else "low",
            "reason": f"查不到历史订单（客户单号/产品英文名/规格都未命中）→ {new_type}",
            "evidence": ev, "history": []}


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="判 ERP 产品类型 CPLX（见 docs/产品类型判定.md）")
    ap.add_argument("--text", required=True, help="任务目录或原文文件")
    ap.add_argument("--customer", default="", help="客户代号（NGV10 / NGVS16 / MAC24…），NGV 系走默认档")
    ap.add_argument("--spec", default="", help="书本规格（查历史单用；--lookup 时有效）")
    ap.add_argument("--english-name", default="", help="产品英文名/系列名（查历史单用）")
    ap.add_argument("--lookup", action="store_true", help="调 MCP erp_sales_order_list 查历史订单")
    ap.add_argument("--token", default="", help="MCP token（默认 ERP_MCP_TOKEN 或本地配置）")
    ap.add_argument("--url", default="", help="MCP 地址（默认本地配置里的 erp_endpoint）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--from-date", default="", help="查历史单的起始日期（默认往前 2 年）")
    ap.add_argument("--to-date", default="", help="查历史单的结束日期（默认今天）")
    args = ap.parse_args(argv)

    try:
        text = read_text(Path(args.text))
    except FileNotFoundError as exc:
        print(f"读不到原文：{exc}", file=sys.stderr)
        return 2

    history: list[dict[str, Any]] = []
    lookup_error = ""
    if args.lookup:
        try:
            from local_config import resolve
            token = args.token or os.environ.get("ERP_MCP_TOKEN", "") or resolve("erp_endpoint", "token", "ERP_MCP_TOKEN")
            url = args.url or resolve("erp_endpoint", "url", "ERP_MCP_URL")
            if not token:
                lookup_error = "没配 MCP token（ERP_MCP_TOKEN 或 assets/erp_endpoint.local.yaml 的 token）"
            else:
                client = make_client(url, token)
                history = lookup_history(client, po_list=extract_po(text), spec=args.spec,
                                         english_name=args.english_name,
                                         from_date=args.from_date, to_date=args.to_date)
        except Exception as exc:
            lookup_error = f"MCP 查询失败：{exc}"

    result = classify(text, customer=args.customer, history=history)
    result["lookup_error"] = lookup_error

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    print(f"CPLX（产品类型）建议：{result['product_type']}   置信度：{result['confidence']}")
    print(f"依据：{result['reason']}")
    ev = result["evidence"]
    for label, key in (("返单词", "reprint"), ("一模一样", "same_as_before"), ("仅文件/装箱/贴纸", "file_only"),
                       ("结构变更", "die_or_shape"), ("用料变更", "material"), ("工艺变更", "process_opt"),
                       ("新项目", "new_project"), ("特殊工艺", "special_process"), ("客户单号", "po_list")):
        if ev.get(key):
            print(f"  · {label}: {' / '.join(ev[key])}")
    if result.get("history"):
        print("ERP 历史单（最近在前）：")
        for row in result["history"]:
            print(f"  - {row.get('表单编号')} | {row.get('制单日期')} | {row.get('客户名称')} | "
                  f"{row.get('规格')} | 产品类型={row.get('产品类型')} | 命中 {row.get('matched_by')}")
    if lookup_error:
        print(f"⚠ {lookup_error}（结论仅基于原文）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
