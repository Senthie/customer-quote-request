#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""reprint_crosscheck.py —— 重印/翻单（reprint）件：新单 vs 原单逐项对照。

为什么要它
    客户写「reprint from PO 16186」/「Nachdruck」时，CPMS 里的工艺往往是从原单继承的。
    这件事本身是对的，但要能回答三个问题：
      ① 哪些槽位原单与本次询价逐字一致（可继承，写明「工艺同原单」）；
      ② 哪些是本次新增（红胶布、数量档、ETD…）；
      ③ 哪些原单有、本次询价没提（要吗？还是要删？）。
    靠人眼对两段 CPMS 容易漏（2026-11-25 Tedi Schultüten 实测：三张单都漏了原文的
    `3 sides trimmed`，且「半翻订单」与实际 36,000 全量重印矛盾）。

用法
    uv run ./scripts/reprint_crosscheck.py --target BJXQ261125010 \
        --reprint BJXQ250920001 --reprint BJXQ260826002 --out /tmp/cross.md

    # 只看摘要（不写文件）
    uv run ./scripts/reprint_crosscheck.py --target <新单> --reprint <原单>

输出
    stdout：每对「一致槽 / 仅原单有 / 仅新单有 / 值不同」的条数（不刷全文）
    --out：markdown 对照表（部位 × 原单值 / 新单值 / 判定）

退出码：0 正常；2 取不到单据。
"""
from __future__ import annotations

import argparse
import json
import re
import ssl
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
from local_config import resolve  # noqa: E402

GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
SPLIT_RE = re.compile(r"[，,；;]")


def rpc(url: str, token: str, name: str, arguments: dict) -> dict:
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": name, "arguments": arguments}}).encode()
    req = urllib.request.Request(url, data=payload, method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream"})
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, context=ctx, timeout=60) as r:
        body = json.loads(r.read().decode())
    if "error" in body:
        raise RuntimeError(json.dumps(body["error"], ensure_ascii=False))
    inner = json.loads(body["result"]["content"][0]["text"])
    if inner.get("error"):
        raise RuntimeError(str(inner["error"]))
    return inner


def fetch(url: str, token: str, ref: str) -> dict:
    args = {"record_id": ref} if GUID_RE.match(ref) else {"bdbh": ref}
    d = rpc(url, token, "erp_quote_detail", args)
    rec = d.get("record") or {}
    if not rec:
        raise RuntimeError(f"{ref} 取不到（已删/无权限）")
    detail = rpc(url, token, "erp_quote_details", {"record_id": d["record_id"]})
    cpid = detail["details"][0]["cpid"] if detail.get("details") else None
    fields: dict[str, str] = {}
    if cpid:
        pf = rpc(url, token, "erp_quote_product_form", {"record_id": d["record_id"], "cpid": cpid})
        fields = {str(x.get("control")): ("" if (x.get("page") or {}).get("value") is None
                                          else str((x.get("page") or {}).get("value")))
                  for x in pf.get("fields", [])}
    return {"ref": ref, "bdbh": rec.get("BDBH") or ref, "record": rec, "fields": fields}


def field_value(rec: dict, ctl: str) -> str:
    """明细字段的真值在 product_form（edit 态）；主表 quote_detail 对这些列常返回空。"""
    v = rec["fields"].get(ctl)
    if v in (None, ""):
        v = rec["record"].get(ctl[3:] if ctl.startswith("txt") else ctl)
    return "" if v is None else str(v).strip()


def parse_cpms(text: str) -> list[tuple[str, str, str]]:
    """→ [(kind, label, value)]；kind: part / sub / note。跳过概要行、分隔线与空行。"""
    out: list[tuple[str, str, str]] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("书籍方向") or "需求内容" in line:
            continue
        if line.startswith("- "):
            body, kind = line[2:].strip(), "sub"
        else:
            body, kind = line, "part"
        m = re.match(r"^([^：:]{1,14})[：:]\s*(.*)$", body)
        if not m:
            continue
        label, value = m.group(1).strip(), m.group(2).strip()
        if not value:
            continue
        out.append(("note" if label in ("备注", "说明") else kind, label, value))
    return out


def slots(value: str) -> list[str]:
    return [s.strip() for s in SPLIT_RE.split(value) if s.strip()]


def compare(target: dict, base: dict) -> list[dict]:
    t = {f"{k}|{lab}": v for k, lab, v in parse_cpms(target["record"].get("CPMS"))}
    b = {f"{k}|{lab}": v for k, lab, v in parse_cpms(base["record"].get("CPMS"))}
    rows: list[dict] = []
    for key in list(b) + [k for k in t if k not in b]:
        bv, tv = b.get(key), t.get(key)
        label = key.split("|", 1)[1]
        if bv is None:
            rows.append({"label": label, "base": "", "target": tv, "verdict": "仅新单有"})
            continue
        if tv is None:
            rows.append({"label": label, "base": bv, "target": "", "verdict": "原单有 · 新单没有"})
            continue
        bs, ts = slots(bv), slots(tv)
        only_b = [s for s in bs if s not in ts]
        only_t = [s for s in ts if s not in bs]
        if not only_b and not only_t:
            verdict = "一致（可继承）"
        elif not only_t:
            verdict = "原单多出：" + " / ".join(only_b)
        elif not only_b:
            verdict = "新单新增：" + " / ".join(only_t)
        else:
            verdict = "双方各有：" + " / ".join(only_b) + " ｜ " + " / ".join(only_t)
        rows.append({"label": label, "base": bv, "target": tv, "verdict": verdict})
    return rows


def tiers(rec: dict) -> list[str]:
    f = rec["fields"]
    return [str(f.get(f"txtCPSL{i}")) for i in range(1, 11) if str(f.get(f"txtCPSL{i}") or "").strip()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="重印/翻单件：新单 vs 原单 CPMS 逐项对照")
    ap.add_argument("--target", required=True, help="新单：表单编号或记录ID")
    ap.add_argument("--reprint", action="append", default=[], required=True,
                    help="原单/真实单：表单编号或记录ID（可重复；第一条作主基准）")
    ap.add_argument("--out", default="", help="markdown 输出路径")
    ap.add_argument("--url", default="")
    ap.add_argument("--token", default="")
    a = ap.parse_args(argv)
    url = a.url or resolve("erp_endpoint", "url", "ERP_MCP_URL")
    token = a.token or resolve("erp_endpoint", "token", "ERP_MCP_TOKEN")
    if not (url and token):
        print("缺 ERP 端点/token（assets/erp_endpoint.local.yaml 或 --url/--token）", file=sys.stderr)
        return 2

    try:
        tgt = fetch(url, token, a.target)
        bases = [fetch(url, token, r) for r in a.reprint]
    except Exception as e:  # noqa: BLE001
        print(f"取数失败：{e}", file=sys.stderr)
        return 2

    md = [f"# 重印对照：{tgt['bdbh']} vs " + " / ".join(b["bdbh"] for b in bases), ""]
    md.append("| 部位 | " + " | ".join(f"原单 {b['bdbh']}" for b in bases) + " | 新单 | 判定 |")
    md.append("|---|" + "---|" * (len(bases) + 2))

    labels: list[str] = []
    for b in bases:
        for k, lab, _ in parse_cpms(b["record"].get("CPMS")):
            if lab not in labels:
                labels.append(lab)
    for k, lab, _ in parse_cpms(tgt["record"].get("CPMS")):
        if lab not in labels:
            labels.append(lab)

    counts: dict[str, int] = {"一致": 0, "差异": 0, "仅新单有": 0, "仅原单有": 0}
    for lab in labels:
        bvals, tval = [], ""
        for b in bases:
            v = next((v for _, l, v in parse_cpms(b["record"].get("CPMS")) if l == lab), "")
            bvals.append(v)
        tval = next((v for _, l, v in parse_cpms(tgt["record"].get("CPMS")) if l == lab), "")
        if not any(bvals):
            verdict = "仅新单有"
        elif not tval:
            verdict = "仅原单有"
        else:
            same = all(v and not [s for s in slots(v) if s not in slots(tval)]
                       and not [s for s in slots(tval) if s not in slots(v)] for v in bvals)
            verdict = "一致" if same else "差异"
        counts[verdict] += 1
        md.append(f"| {lab} | " + " | ".join(v.replace("\n", " ") for v in bvals)
                  + f" | {tval.replace(chr(10), ' ')} | {verdict} |")

    # 数量档 / 日期 / 检测
    md += ["", "## 字段级", "", "| 字段 | " + " | ".join(b["bdbh"] for b in bases) + f" | {tgt['bdbh']} |", "|---|" + "---|" * (len(bases) + 1)]
    for lab, key in (("数量档(总量)", "CPSL"), ("交货日期 JHRQ", "JHRQ"), ("预计下单 WCRQ", "WCRQ")):
        bv = [" / ".join(tiers(b)) if key == "CPSL" else field_value(b, "txt" + key) for b in bases]
        tv = " / ".join(tiers(tgt)) if key == "CPSL" else field_value(tgt, "txt" + key)
        md.append(f"| {lab} | " + " | ".join(bv) + f" | {tv} |")
    same_insp = len({field_value(b, "txtRemark4") for b in bases}
                    | {field_value(tgt, "txtRemark4")}) == 1
    md.append(f"| 检测项目 Remark4 | " + " | ".join(
        (field_value(b, "txtRemark4")[:40] + "…") for b in bases)
        + f" | {'与基准一致' if same_insp else '与基准不同，需核对'} |")

    for b in bases:
        rows = compare(tgt, b)
        n_diff = sum(1 for r in rows if r["verdict"] != "一致（可继承）")
        print(f"{tgt['bdbh']} vs {b['bdbh']}: 部位 {len(rows)} 个，差异 {n_diff} 个"
              f"（仅新单 {sum(1 for r in rows if r['verdict'] == '仅新单有')}，"
              f"仅原单 {sum(1 for r in rows if r['verdict'] == '原单有 · 新单没有')}）")

    if a.out:
        Path(a.out).write_text("\n".join(md) + "\n", encoding="utf-8")
        print("→", a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
