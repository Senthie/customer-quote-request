#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""reprint_gate.py —— 重印/翻单件的产物闸门：把「这是重印，原单是哪张、工艺跟不跟」写进 question_answers。

为什么单独一层
    重印件（reprint / Nachdruck / 翻单 / 重印）的 CPMS 工艺天然继承原单。不写明来源
    就变成「抄历史单」；写错来源（例如「半翻订单」其实全量重印）会直接误导报价。
    2026-11-25 Tedi Schultüten 实测：要么漏了原文的 `3 sides trimmed`、要么把原单的
    检测条款换成通用 EN71 —— 都是「没人对着原单核」造成的。

它做什么（main.py 写完产物前调用，与 spec_fields.gap_items 同级）
    1. 原文命中重印关键词 → 抽出 PO 号；
    2. 有 ERP token 时按产品名找同产品历史报价单（找不到不阻塞，只提示）；
    3. 产出一条 `question_answers` 条目（`Q_REPRINT`，field=CPMS，blocking=False）：
       写清「原文声明重印自 PO#x / 找到的原单号 / 差异部位 / 本次新增项」，
       让业务在 CQR_Web 上一眼看到「这单的工艺是继承来的，核过没有」。

设计约束
    * **不联网也必须能跑**：没有 token / ERP 不可达 → 只产出「需人工核对原单」的条目，
      绝不抛异常拖垮抽取（main.py 里还有第二层 try/except）。
    * `blocking=False`：重印溯源缺一行不该拦住交付；它要的是可见，不是堵路。
"""
from __future__ import annotations

import os
import re
from typing import Any, Optional

from question_answers import make_item

# 「reprint from PO 16186」/「Nachdruck」/「翻单」/「重新印刷」
REPRINT_RE = re.compile(
    r"\bre-?prints?\b|\breprint(?:ed|ing)?\b|nachdruck|neuauflage|"
    r"翻单|重印|重新印刷|再印|加印",
    re.I,
)
PO_RE = re.compile(r"\bPO\s*(?:#|№|no\.?)?\s*[-–]?\s*(\d{4,6})\b", re.I)
LATIN_RUN_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ0-9\-'&./ ]{5,60}")
WORD_RE = re.compile(r"[A-ZÄÖÜ][A-Za-zÄÖÜäöüß]{4,}")
# 检索词里排除的邮件/规格高频词（拿它们去查产品名只会命中一堆无关单）
STOPWORDS = {
    "preisanfrage", "reprint", "nachdruck", "angebot", "offer", "quote", "quotation",
    "enquiry", "inquiry", "technical", "specification", "packaging", "delivery",
    "cover", "content", "extent", "binding", "title", "titles", "pages", "paper",
    "carton", "shipment", "sample", "samples", "production", "supplier",
}


def detect_reprint(text: str) -> Optional[str]:
    """命中重印语义 → 返回命中的关键词（用于条目文案）；否则 None。"""
    m = REPRINT_RE.search(text or "")
    return m.group(0) if m else None


def extract_pos(text: str) -> list[str]:
    out: list[str] = []
    for m in PO_RE.finditer(text or ""):
        po = m.group(1)
        if po not in out:
            out.append(po)
    return out


def product_keywords(record: dict, source_text: str = "") -> list[str]:
    """可用于 ERP 产品名检索的关键字：品名里的拉丁片段 + 原文里最高频的产品词。

    品名（ECPMC/CPMC）在抽取阶段常常还是空的 —— 那时只能从原文捞：
    邮件里的 `Schultüten` / `Wimmelbücher` 这类词命中率最高，规格词（Cover/Content/gsm）要排掉。
    """
    out: list[str] = []

    def push(val: str) -> None:
        val = val.strip(" .,;:")
        if len(val) >= 5 and val not in out:
            out.append(val)

    for raw in (record.get("ECPMC"), record.get("CPMC"), record.get("产品英文名"), record.get("书名")):
        txt = str(raw or "").replace("测试", "").strip()
        if not txt:
            continue
        tail = txt.split("/")[-1].strip() or txt
        runs = [r.strip() for r in LATIN_RUN_RE.findall(tail) if len(r.strip()) >= 6]
        if runs:
            push(max(runs, key=len)[:40])

    counts: dict[str, int] = {}
    for word in WORD_RE.findall(source_text or ""):
        low = word.lower()
        if low in STOPWORDS or len(word) < 6:
            continue
        counts[word] = counts.get(word, 0) + 1
    for word, _ in sorted(counts.items(), key=lambda kv: (-kv[1], -len(kv[0])))[:3]:
        push(word)
    return out


def product_keyword(record: dict, source_text: str = "") -> str:
    """兼容旧调用：返回首个（可能为空串）。"""
    kws = product_keywords(record, source_text)
    return kws[0] if kws else ""


def _client():
    """能连就返回 McpHttpClient，连不上返回 None（绝不抛）。"""
    try:
        from erp_push import McpHttpClient
        from local_config import resolve

        token = resolve("erp_endpoint", "token", "ERP_MCP_TOKEN")
        if not token and not os.environ.get("ERP_MCP_URL"):
            return None
        return McpHttpClient(token=token)
    except Exception:  # noqa: BLE001
        return None


def find_base_quotes(client: Any, keywords: Any, customer: str = "") -> list[dict]:
    """按产品名找同产品历史报价需求单（最近 3 年），只回关键列。

    `keywords` 可传一个关键字或列表；多个关键字逐个查、按表单编号去重。
    过滤掉产品名带「测试」的单 —— 否则会把自己的测试单当成原单。
    """
    if not client:
        return []
    if isinstance(keywords, str):
        keywords = [keywords]
    seen: dict[str, dict] = {}
    for kw in [k for k in (keywords or []) if k][:3]:
        for use_customer in ([customer, ""] if customer else [""]):
            args: dict[str, Any] = {
                "field": "产品名称", "key": kw,
                "from_date": "2023-01-01", "limit": 30,
                "columns": "记录ID,表单编号,客户名称,产品名称,制单日期,报价状态",
            }
            if use_customer:
                args["customer"] = use_customer
            try:
                recs = (client.call_tool("erp_quote_list", args) or {}).get("records") or []
            except Exception:  # noqa: BLE001
                recs = []
            for r in recs:
                bdbh = str(r.get("表单编号") or "")
                if not bdbh or bdbh in seen:
                    continue
                if "测试" in str(r.get("产品名称") or ""):
                    continue
                seen[bdbh] = r
            if recs:
                break  # 带客户查到了就别再用放宽条件查一遍（每次调用 2-8 秒）
    return list(seen.values())


def match_strength(product_name: str, source_text: str) -> int:
    """候选单与本封询价的匹配强度 = 产品名里**同时出现在原文中**的最长词长度。

    「Ulrich」这种业务员名会把一堆无关单带进来（实测：声音书 Furzipups 被当成同一个产品），
    而 `Schultüten` 这种书名词只会在同产品出现 —— 用词长当权重最省事。
    """
    best = 0
    for word in WORD_RE.findall(product_name or ""):
        if len(word) >= 5 and word.lower() not in STOPWORDS and word in (source_text or ""):
            best = max(best, len(word))
    return best


def gap_items(record: dict, source_text: str, *, customer: str = "",
              today: Optional[str] = None, client: Any = None,
              online: Optional[bool] = None) -> list[dict]:
    """重印件 → `question_answers` 条目。非重印件返回 []。"""
    hit = detect_reprint(source_text)
    if not hit:
        return []

    pos = extract_pos(source_text)
    keywords = product_keywords(record, source_text)
    if online is None:
        online = client is not None or _client() is not None
    if client is None and online:
        client = _client()

    bases = find_base_quotes(client, keywords, customer) if client else []
    bases.sort(key=lambda r: (match_strength(str(r.get("产品名称") or ""), source_text),
                              str(r.get("制单日期") or "")), reverse=True)

    po_txt = f"PO#{pos[0]}" if pos else "原订单（原文未给 PO 号）"
    if bases:
        b = bases[0]
        best = match_strength(str(b.get("产品名称") or ""), source_text)
        peers = [x for x in bases
                 if match_strength(str(x.get("产品名称") or ""), source_text) == best]
        detail = (f"已找到同产品历史报价单 {b['表单编号']}"
                  f"（{str(b.get('制单日期') or '')[:10]}，{b.get('客户名称') or ''}）")
        if len(peers) > 1:
            detail += f"，同产品候选共 {len(peers)} 张"
        act = ("请业务核对：① CPMS 工艺是否沿用该单；② 本次新增项（数量/包装/交期）是否单独写清；"
               "③ 运行 scripts/reprint_crosscheck.py 出逐槽对照后再定稿")
    else:
        kw_txt = "/".join(keywords[:3]) if keywords else ""
        detail = ("未能自动找到同产品历史报价单"
                  + (f"（检索词：{kw_txt}）" if kw_txt else "（原文里没有可检索的产品词）"))
        act = "请业务提供原订单号（KHDD…）或原报价单号，再核 CPMS 是否沿用原单工艺"

    return [make_item(
        "Q_REPRINT", "CPMS",
        f"原文声明为「{hit}」重印 {po_txt}。{detail}。{act}",
        source="enquiry", asked_at=today,
    )]
