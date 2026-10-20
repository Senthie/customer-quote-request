#!/usr/bin/env bash
# Hermes 定时任务入口（no_agent 模式）——
#   stdout 非空 → 原样发给人看；stdout 为空 → 安静（没东西要抽时不打扰）
# 用法（手动跑也一样）：./scripts/scan_pending.sh [--dry-run] [--verbose]
set -u
cd /home/claw/customer-quote-request || exit 0

# uv / venv 的噪音进日志，别混进要发给人的那段话；
# 真出事了去 /tmp/cqr_scan.stderr 看（不静默吞掉错误）。
exec uv run ./scripts/scan_pending.py "$@" 2>>/tmp/cqr_scan.stderr
