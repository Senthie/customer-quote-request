#!/usr/bin/env bash
# 邮件分拣 agent 的收信入口（Hermes 定时任务脚本）——
#   把邮箱新信落到 customer-files/staging/，**不**直接进报价队列。
#   分类由 agent 看完再调 mail_agent_route.py 投递：
#       询价   → customer-files/pending/（报价流水线认领）
#       非询价 → customer-files/inbox/<日期>/_non_enquiry/
#
# stdout 契约：
#   没有新信且无残留 → 一个字都不打（agent 收到空上下文，回 [SILENT]）
#   有新信/残留      → 一行摘要 + 每封一行文件名（agent 据此逐封判）
#
# 去重台账独立于报价侧：customer-files/.mail_agent_state.json
# ⚠ 别用 mail_fetch.py --json：那个模式下 per-file 日志也打到 stdout，
#   混进 JSON 前缀会让解析全灭（2026-11-19 实测，空跑一次才发现）。
#   这里改成「跑前跑后各列一遍 staging，取差集」。
set -u
REPO=/home/claw/customer-quote-request
ERR=/tmp/cqr_mail_agent.stderr
cd "$REPO" || exit 0

STAGING=customer-files/staging
STATE=customer-files/.mail_agent_state.json
mkdir -p "$STAGING" || exit 0

snapshot() { ls -1 "$STAGING" 2>/dev/null | grep '\.eml$' | sort; }
before=$(snapshot)

timeout 150 uv run ./scripts/mail_fetch.py --pending "$STAGING" --state "$STATE" \
    >>/dev/null 2>>"$ERR"
rc=$?
if [ $rc -ne 0 ]; then
    echo "!! 收信失败（rc=$rc，见 $ERR 末行）：$(tail -1 "$ERR" 2>/dev/null | cut -c1-120)"
    exit 0
fi

after=$(snapshot)
new=$(comm -13 <(printf '%s\n' "$before") <(printf '%s\n' "$after") | sed '/^$/d')
total=$(printf '%s\n' "$after" | sed '/^$/d' | wc -l)

[ "$total" -eq 0 ] && exit 0

if [ -n "$new" ]; then
    new_n=$(printf '%s\n' "$new" | wc -l)
    printf '收信 %s 封 → staging/（待分类）：\n' "$new_n"
else
    printf 'staging/ 里有 %s 封上轮未分类的残留：\n' "$total"
fi
printf '%s\n' "$after" | sed 's/^/  /'
exit 0
