#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""auto_profile.py —— 新客户询价 → 自动建一份**草稿档案**（status: draft）

业务口径（森西 2026-11-17）：
> 校准过的客户档案，未来遇到新的客户询价自动创建档案

背景：每次来一家没建过档的客户，`main.py` 都会落 `generic` 兜底档案 ——
字段大面积是模板默认值（CPLB=32.00;其他、CPSL 全 0、尺寸漏抓），
产物只能当「识别失败」扔掉，人还得从零 cp 模板、翻表头、逐条对列名。
这一步把它变机械劳动：**能自动的部分自动做**，剩下的列成清单交给人。

它做什么
--------
1. `auto_detect`：这家其实已被现有档案识别 → 直接退出（不重复建档）
2. 定 id（`--id`，或从发件人域名/文件名推）与 name
3. 从 `customers/_template.yaml` 复制骨架 → `customers/<id>.yaml`，
   填 id / name / status: **draft** / priority / detect.sender_domains
   / notes（写明「自动建档、未校准」+ 来源邮件）
4. 用新档案跑一遍样本，把「像标签但认不出」的单元格用 `suggest_canonical`
   分类，**高置信**的（有关键词命中）直接写进 fields: 别名，低置信的留清单
5. 把完整建议写成 `customers/_draft_<id>.yaml`（给人对照），并在 stdout 打印
   待人工校准项 + 下一步命令

**它不做什么**（红线）
----------------------
* 不写 CPMS、不动产物、不改已推 ERP 的东西
* 不把档案标成 calibrated —— 自动猜的别名一律留在 `status: draft`，
  `main.py` 会对非 calibrated 档案打 WARN（人工必须先核一遍）
* 不猜 `erp_khzl`（要 ERP 客户资料选择页里查）→ 留空 + 清单里点名

用法
----
    uv run ./scripts/auto_profile.py <样本文件> [--sender a@b.com] [--name "XX 出版社"]
    uv run ./scripts/auto_profile.py --from-generic-artifacts      # 扫最近落 generic 的产物
    uv run ./scripts/auto_profile.py <样本> --dry-run              # 只看会建什么，不落盘
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import yaml  # noqa: E402

from customer_profile import auto_detect, load_profile, profiles_dir  # noqa: E402
from calibrate_customer import (KEYWORD_HINTS, collect_label_like_cells,  # noqa: E402
                               suggest_canonical)
from extract_fields import load_customer_data  # noqa: E402

STATE_PATH = REPO / "customer-files" / ".auto_profile_state.json"
TEMPLATE = "customers/_template.yaml"
MIN_VALUES_FOR_SIGNAL = 3          # 抽出 ≥3 个规范键才算「像真的询价」
GENERIC_LOOKBACK_HOURS = 72


# ── 工具 ────────────────────────────────────────────────────────────────
def _slug(text: str, maxlen: int = 10) -> str:
    s = re.sub(r"[^a-z0-9]+", "", (text or "").lower())
    return s[:maxlen]


def guess_id(sample: Path, sender: str = "", explicit: str = "") -> str:
    """推一个档案 id：显式 > 发件人域名首段 > 文件名首词（都取字母数字）。"""
    if explicit:
        return _slug(explicit, 20) or "newcust"
    domain = ""
    m = re.search(r"@([\w.-]+)", sender or "")
    if m:
        domain = m.group(1).split(".")[0]
        if domain in ("mail", "email", "info", "no-reply", "noreply"):
            domain = m.group(1).split(".")[-2] if "." in m.group(1) else domain
    base = _slug(domain) or _slug(sample.stem.split("_")[0].split("-")[0])
    if not base:
        base = "newcust"
    existing = {p.stem for p in profiles_dir().glob("*.yaml")}
    if base in existing:
        for i in range(2, 100):
            cand = f"{base}{i:02d}" if len(base) > 6 else f"{base}{i}"
            if cand not in existing:
                return cand
    return base


def guess_name(sample: Path, sender: str = "") -> str:
    m = re.search(r"@([\w.-]+)", sender or "")
    if m:
        dom = m.group(1).lower().split(".")[0]
        return dom.capitalize() if dom else sample.stem
    return sample.stem


# ── 建档 ────────────────────────────────────────────────────────────────
def existing_profile_for(domain: str) -> Optional[str]:
    """已有档案里谁的 sender_domains / name 对得上这个域名 → 返回 id。

    ★ 为什么不问就建：Fleurus 这类客户早就有一份 `derive_customer_defaults.py`
    自动生成的语料档案（`fes22`，status=draft），再建一份 `fes26` 只会让同一个
    客户分叉成两份档案、统计和默认值全部割裂。命中已有档案时**应该去校准它**。
    """
    dom = (domain or "").lower().strip()
    if not dom:
        return None
    bare = dom.split(".")[0]
    for path in sorted(profiles_dir().glob("*.yaml")):
        if path.stem.startswith("_"):
            continue
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        doms = [str(d).lower() for d in ((raw.get("detect") or {}).get("sender_domains") or [])]
        if dom in doms:
            return path.stem
        name = str(raw.get("name") or "").lower()
        notes = str(raw.get("notes") or "").lower()
        if bare and (bare in name or bare in notes):
            return path.stem
    return None


def build_profile(sample: Path, *, sender: str = "", name: str = "", pid: str = "",
                  subject: str = "", dry_run: bool = False) -> Optional[dict]:
    texts = _sample_texts(sample)
    if texts:
        cands = auto_detect(texts, filename=sample.name, sender=sender, subject=subject)
        if cands and cands[0][1] >= 100:
            print(f"跳过：样本已被现有档案 `{cands[0][0].id}` 识别（得分 {cands[0][1]}）"
                  f" —— 不需要为它新建档案")
            return None

    m_dom = re.search(r"@([\w.-]+)", sender or "")
    domain = m_dom.group(1).lower() if m_dom else ""
    existing = existing_profile_for(domain) if domain else None
    if existing:
        print(f"跳过建档：发件人域名 `{domain}` 对应已有档案 `{existing}`"
              f" —— 应该校准那份，而不是新建一份（否则同一客户分叉成两份）\n"
              f"  下一步：uv run ./scripts/calibrate_customer.py --customer {existing} "
              f"--file {sample.name}")
        return None

    pid = guess_id(sample, sender=sender, explicit=pid)
    pname = name or guess_name(sample, sender=sender)
    tpl_path = REPO / TEMPLATE
    raw: dict[str, Any] = yaml.safe_load(tpl_path.read_text(encoding="utf-8")) or {}

    raw["id"] = pid
    raw["name"] = pname
    raw["priority"] = 30
    raw["status"] = "draft"          # ★ 红线：自动建的一律 draft，人工核过再改 calibrated
    raw["erp_khzl"] = []
    detect = dict(raw.get("detect") or {})
    doms: list = list(detect.get("sender_domains") or [])
    m = re.search(r"@([\w.-]+)", sender or "")
    if m and m.group(1).lower() not in [d.lower() for d in doms]:
        doms.append(m.group(1).lower())
    detect["sender_domains"] = doms
    raw["detect"] = detect
    raw["notes"] = (
        f"自动建档（auto_profile.py，{datetime.now():%Y-%m-%d %H:%M}）："
        f"来源样本 `{sample.name}`"
        + (f"，发件人 `{sender}`" if sender else "")
        + (f"，主题 `{subject}`" if subject else "")
        + "。status=draft —— fields 里的别名是脚本按关键词猜的，"
          "必须人工核一遍再改 calibrated；erp_khzl 还要去 ERP 客户资料页查了补上。"
    )

    # 用草稿跑一遍，拿「认不出的标签 → 建议规范键」
    target = profiles_dir() / f"{pid}.yaml"
    assigned: dict[str, list] = {}
    todo: list = []
    if not dry_run:
        target.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
        try:
            prof = load_profile(pid)
            data = load_customer_data(str(sample), prof)
            fields = dict(prof.fields or {})
            for label in _unmatched_labels(sample, prof):
                canonical, basis = suggest_canonical(label)
                if not canonical or canonical == "_uncertain" or canonical not in fields:
                    todo.append((label, canonical or "?", basis))
                    continue
                aliases = list(fields.get(canonical) or [])
                if label not in aliases:
                    aliases.append(label)
                    fields[canonical] = aliases
                    assigned.setdefault(canonical, []).append(label)
            if assigned:
                raw["fields"] = fields
                target.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
                                  encoding="utf-8")
            if data.values:
                print(f"  草稿档案自检：已能抽出 {len(data.values)} 个规范键 "
                      f"{sorted(data.values)[:8]}，数量档位 {data.quantity_tiers or '未识别'}")
        except Exception as exc:                       # 建档失败不能拖垮流水线
            print(f"  [WARN] 草稿自检失败（档案已落，人工接手）：{exc}")
        draft_path = profiles_dir() / f"_draft_{pid}.yaml"
        draft_path.write_text(
            f"# auto_profile.py 于样本 {sample.name} 生成的校准草稿\n"
            f"# 仅供对照，不参与加载。确认后手工并入 customers/{pid}.yaml 的 fields:\n\n"
            f"sample_file: {json.dumps(sample.name, ensure_ascii=False)}\n"
            f"auto_aliases:\n"
            + "".join(f"  {k}:\n" + "".join(f"    - {json.dumps(v, ensure_ascii=False)}\n" for v in vs)
                      for k, vs in sorted(assigned.items()))
            + "needs_human:\n"
            + "".join(f"  - {{label: {json.dumps(l, ensure_ascii=False)}, guess: {json.dumps(c, ensure_ascii=False)}, basis: {json.dumps(b, ensure_ascii=False)}}}\n"
                      for l, c, b in todo),
            encoding="utf-8")

    return {"id": pid, "name": pname, "path": str(target.relative_to(REPO)),
            "auto_aliases": assigned, "needs_human": todo, "dry_run": dry_run}


def _sample_texts(sample: Path) -> list:
    from customer_profile import collect_probe_texts
    try:
        return collect_probe_texts(sample)
    except Exception:
        return []


def _unmatched_labels(sample: Path, prof) -> list:
    """像标签但认不出的单元格（复用 calibrate 的收集 + 过滤口径）。"""
    from collections import Counter
    seen, out = set(), []
    for _col, label in collect_label_like_cells(str(sample), prof):
        norm = label.strip().lower()
        if not norm or norm in seen:
            continue
        seen.add(norm)
        if prof.match_label(label) or prof.is_known_extra(label) or prof.is_ignored_label(label):
            continue
        out.append(label)
    counts = Counter(_col for _col, _lb in collect_label_like_cells(str(sample), prof))
    return [lb for _c, lb in collect_label_like_cells(str(sample), prof)
            if counts[_c] < 8 and lb in out]


# ── 扫最近落 generic 的产物 ─────────────────────────────────────────────
def scan_generic_artifacts(hours: int = GENERIC_LOOKBACK_HOURS,
                           dry_run: bool = False) -> list:
    state = {}
    if STATE_PATH.exists():
        try:
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            state = {}
    done: dict = state.setdefault("done", {})
    cutoff = datetime.now() - timedelta(hours=hours)
    created = []
    base = REPO / "customer-files" / "processed"
    for d in sorted(base.iterdir()):
        if not d.is_dir() or d.name.startswith(("_", ".")):
            continue
        ext = next(iter(d.glob("*.extracted.json")), None)
        if not ext or d.name in done:
            continue
        try:
            meta = json.loads(ext.read_text(encoding="utf-8"))
        except Exception:
            continue
        if meta.get("profile_id") != "generic":
            continue
        if len(meta.get("values") or {}) < MIN_VALUES_FOR_SIGNAL:
            done[d.name] = "values_too_few"
            continue
        if datetime.fromtimestamp(ext.stat().st_mtime) < cutoff:
            done[d.name] = "too_old"
            continue
        sample = next((p for p in d.iterdir()
                       if p.suffix.lower() in (".xlsx", ".xlsm", ".xls", ".csv", ".pdf",
                                               ".docx", ".txt")), None)
        if not sample:
            done[d.name] = "no_sample"
            continue
        side = next(iter(d.glob("*.meta.json")), None) or next(
            iter((REPO / "customer-files" / "pending").glob(f"{sample.stem}.meta.json")), None)
        sender = subject = ""
        if side:
            try:
                sm = json.loads(side.read_text(encoding="utf-8"))
                sender, subject = str(sm.get("sender") or ""), str(sm.get("subject") or "")
            except Exception:
                pass
        res = build_profile(sample, sender=sender, subject=subject, dry_run=dry_run)
        done[d.name] = res["id"] if res else "skipped_recognized"
        if res:
            created.append(res)
    if not dry_run:
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    return created


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="新客户询价 → 自动建草稿档案（status: draft）")
    ap.add_argument("sample", nargs="?", help="样本文件（真实询价附件/正文 txt）")
    ap.add_argument("--sender", default="", help="发件人（用来补 detect.sender_domains 与推 id）")
    ap.add_argument("--subject", default="", help="邮件主题（写进 notes / 识别用）")
    ap.add_argument("--name", default="", help="客户显示名")
    ap.add_argument("--id", dest="pid", default="", help="指定档案 id")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不落盘")
    ap.add_argument("--from-generic-artifacts", action="store_true",
                    help="扫 processed/ 里最近落 generic 的产物自动建档")
    args = ap.parse_args(argv)

    if args.from_generic_artifacts:
        created = scan_generic_artifacts(dry_run=args.dry_run)
        if not created:
            print("没有需要建档的新客户产物")
            return 0
        for res in created:
            _report(res)
        return 0

    if not args.sample:
        ap.error("要么给样本文件，要么用 --from-generic-artifacts")
    sample = Path(args.sample)
    if not sample.exists():
        print(f"样本不存在: {sample}", file=sys.stderr)
        return 2
    res = build_profile(sample, sender=args.sender, name=args.name, pid=args.pid,
                        subject=args.subject, dry_run=args.dry_run)
    if not res:
        return 0
    _report(res)
    return 0


def _report(res: dict) -> None:
    print("=" * 66)
    print(f"新客户草稿档案：{res['id']}（{res['name']}）→ {res['path']}"
          + ("　【dry-run，未落盘】" if res["dry_run"] else ""))
    if res["auto_aliases"]:
        print("\n已自动补进 fields 的别名（脚本按关键词猜的，**必须核**）：")
        for canonical, labels in sorted(res["auto_aliases"].items()):
            print(f"  {canonical:20s} ← {labels}")
    if res["needs_human"]:
        print(f"\n需要人工判断的标签（{len(res['needs_human'])} 个）：")
        for label, guess, basis in res["needs_human"][:20]:
            print(f"  · {label[:44]:44s} → 猜 {guess:18s}（{basis}）")
    print("\n下一步：")
    print(f"  1. 核 `{res['path']}`：fields 别名是否对、extras 该登记的补上")
    print("  2. 去 ERP 客户资料选择页查客户代码，补 erp_khzl（脚本不猜）")
    print(f"  3. uv run ./scripts/main.py <样本> --customer {res['id']} 回归，"
          "值对了再把 status 改成 calibrated")
    print("=" * 66)


if __name__ == "__main__":
    sys.exit(main())
