#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""修掉 11 单 CPMS 里重复的概要行。

根因：ERP 保存 txtCPMS 时会**自动补一行概要**（书籍方向/产品规格），
而我提交的正文若自带概要行，就会越存越多（实测存 2 次 = 3 行）。
做法：提交时剥掉所有概要行 / 空行 / 尾部「====【需求内容】====」分隔线，
只送部件正文，让 ERP 自己补那一行。
"""
import re
import sys

sys.path.insert(0, "/home/claw/customer-quote-request/scripts")
from erp_push import McpHttpClient  # noqa: E402
from local_config import resolve  # noqa: E402
from refresh_parts_desc import NEW  # noqa: E402

SUMMARY = re.compile(r"^书籍方向：【.*?】产品规格：【.*?】")
SEP = re.compile(r"^=+【需求内容】=+")


def body_only(text: str) -> str:
    keep = [l for l in text.replace("\r\n", "\n").splitlines()
            if l.strip() and not SUMMARY.match(l.strip()) and not SEP.match(l.strip())]
    return "\n".join(keep)


def main() -> int:
    c = McpHttpClient(url=resolve("erp_endpoint", "url", "ERP_MCP_URL"),
                      token=resolve("erp_endpoint", "token", "ERP_MCP_TOKEN"), timeout=180)
    c.initialize()
    bad = 0
    for unit, bdbh, rid, _h, _d, _w in NEW:
        det = c.call_tool("erp_quote_details", {"record_id": rid})
        rows = det.get("details") or det.get("rows") or det.get("products") or []
        cpid = str(rows[0].get("cpid") or "")
        cur = str((c.call_tool("erp_quote_detail", {"record_id": rid}).get("record") or {}).get("CPMS") or "")
        n_sum_before = sum(1 for l in cur.splitlines() if SUMMARY.match(l.strip()))
        body = body_only(cur)
        if n_sum_before == 1 and not re.search(r"\n{2,}", body):
            print(f"#{unit} {bdbh} 概要行=1，无需改")
            continue
        r = c.call_tool("erp_quote_update_product", {"record_id": rid, "cpid": cpid,
                                                     "product_desc": body, "confirm": True})
        back = str((c.call_tool("erp_quote_detail", {"record_id": rid}).get("record") or {}).get("CPMS") or "")
        n_sum = sum(1 for l in back.splitlines() if SUMMARY.match(l.strip()))
        has_parts = "发声器" in back or "前封面：泡壳夜灯" in back
        ok = bool(r.get("ok")) and n_sum == 1 and has_parts
        print(f"#{unit} {bdbh} 概要行 {n_sum_before}→{n_sum} 部件行={has_parts} → {'✅' if ok else '❌'}")
        if not ok:
            bad += 1
            print("   ", str(r)[:200])
    print(f"\n=== 异常 {bad} 单 ===")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
