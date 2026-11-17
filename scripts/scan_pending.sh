#!/usr/bin/env bash
# Hermes 定时任务入口（no_agent 模式）——
#   stdout 非空 → 原样发给人看；stdout 为空 → 安静（没东西要抽时不打扰）
# 用法（手动跑也一样）：./scripts/scan_pending.sh [--dry-run] [--verbose]
set -u
cd /home/claw/customer-quote-request || exit 0

# ── 第 0 步：收信 ──────────────────────────────────────────────────────
# 先把邮箱里的新询价信落成 pending/*.eml，同一轮里顺手抽掉（业务 2026-11-13：
# 「收信接入流水线」）。失败**绝不**阻断扫描：错误只进 stderr 日志。
if command -v himalaya >/dev/null 2>&1; then
    timeout 150 uv run ./scripts/mail_fetch.py 2>>/tmp/cqr_scan.stderr
fi

# ── 第 1.5 步：新客户自动建档（业务 2026-11-17：森西）──────────────────
# 产物落 `generic` 兜底档案且抽出 ≥3 个规范键 = 来了没建过档的客户
# （Fleurus / Casterman / Kosmos 那批就是这样，产物只能当「识别失败」）。
# 这一步自动建一份 `status: draft` 的草稿档案：能按关键词猜的别名先补上，
# 剩下的列成清单交给人核（**不自动 calibrated、不猜 erp_khzl**）。
# 已有档案对得上域名时会跳过并提示去校准那一份，不会把同一客户分叉成两份。
# 输出只在真建了档案时才有内容 —— 平时对 stdout 没有贡献。
auto_out=$(timeout 120 uv run ./scripts/auto_profile.py --from-generic-artifacts 2>>/tmp/cqr_scan.stderr)
[ -n "$auto_out" ] && printf '%s\n' "$auto_out"

# uv / venv 的噪音进日志，别混进要发给人的那段话；
# 真出事了去 /tmp/cqr_scan.stderr 看（不静默吞掉错误）。
exec uv run ./scripts/scan_pending.py "$@" 2>>/tmp/cqr_scan.stderr
