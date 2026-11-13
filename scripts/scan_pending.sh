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

# uv / venv 的噪音进日志，别混进要发给人的那段话；
# 真出事了去 /tmp/cqr_scan.stderr 看（不静默吞掉错误）。
exec uv run ./scripts/scan_pending.py "$@" 2>>/tmp/cqr_scan.stderr
