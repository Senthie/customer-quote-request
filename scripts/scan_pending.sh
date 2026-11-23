#!/usr/bin/env bash
# Hermes 定时任务入口（no_agent 模式）——
#   stdout 非空 → 原样发给人看；stdout 为空 → 安静（没东西要抽时不打扰）
# 用法（手动跑也一样）：./scripts/scan_pending.sh [--dry-run] [--verbose]
set -u
cd /home/claw/customer-quote-request || exit 0

# ── 第 0 步之前的补发：上一轮的 ERP 录入回执 ───────────────────────────
# 业务 2026-11-20：「提取任务最后把录入系统的询价结果发回收信邮箱」。
# 主路径是 cron prompt 里最后一步跑 round_report_mail.py；这里只是**兜底**
# （agent 那轮被中断也不会漏报）。台账去重，跑两次不会发两封；发失败不重发。
export PATH=/home/claw/.local/bin:$PATH
timeout 150 uv run ./scripts/round_report_mail.py --quiet \
    2>>/tmp/cqr_scan.stderr >>/tmp/cqr_report.log

# ── 第 0 步：收信（2026-11-19 起已移交「邮件分拣 agent」）────────────────
# 分工：收信 + 「这封信算不算询价」由分拣 agent 负责（scripts/mail_agent_fetch.sh
# → staging/ → mail_agent_route.py → 只把询价投进 pending/）。
# 报价侧本脚本**不再收信**，只看 pending/ 里已经判成询价的信。
#
# 开关：customer-files/.mail_agent_active 存在 = 分拣侧已接管，跳过收信；
# 该文件被删（分拣 agent 停摆、要临时回到单 agent 模式）→ 恢复旧行为自己收信。
# 这样无论哪边在跑，pending/ 里都不会出现两套去重台账互相抢信的情况。
if [ ! -f customer-files/.mail_agent_active ] && command -v himalaya >/dev/null 2>&1; then
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

# ── 第 1.8 步：多模态转文本（2026-11-19）──────────────────────────────
# 客户会发图片（规格表截图/封面/刀线）和视频（翻样书看装订工艺）；main.py 只吃文本，
# 所以先把它们转成 `<名>.vlm.txt`（模型只转录不推断，见 assets/vlm_prompts.yaml）。
# pending 根下的媒体会被包成单元目录，这样扫描器能当一份报价处理。
# 失败**不阻断**扫描：错误进 stderr，报告里只出「❌ 失败 N 份」。
mm_out=$(timeout 240 uv run ./scripts/multimodal.py --prepass --limit 10 2>>/tmp/cqr_scan.stderr)
[ -n "$mm_out" ] && printf '%s\n' "$mm_out"

# uv / venv 的噪音进日志，别混进要发给人的那段话；
# 真出事了去 /tmp/cqr_scan.stderr 看（不静默吞掉错误）。
uv run ./scripts/scan_pending.py "$@" 2>>/tmp/cqr_scan.stderr
scan_rc=$?

# ── 第 3 步：询价 zip 附件兜底（业务规则 2026-11-23 森西）──────────────
# 「抽完 → 压图打 zip → 建单 → 挂附件」里的最后一环。
#   主路径：erp_push.py --confirm 建单成功后会自己调 quote_attachment 挂钩（同进程）。
#   这里兜的是漏网的：① agent 直接调 MCP 工具建单、绕过了 erp_push.py；
#                     ② 上轮附件失败 / 服务端当时 413；③ 附件被人在 ERP 里删了。
# 幂等：附件名称以「询价原始件」开头的已存在就跳过（一个单只需一份）。
# 输出：只有真挂了或真失败才有一行，平时完全安静（符合本脚本「没内容不打扰」）。
att_out=$(timeout 240 uv run ./scripts/quote_attachment.py --backfill --confirm --quiet \
          2>>/tmp/cqr_scan.stderr)
[ -n "$att_out" ] && printf '%s\n' "$att_out"

exit $scan_rc
