# 邮件分拣 ↔ 报价流水线 的分工契约

> 2026-11-19 业务口径（森西）：「邮件分类转移给 mail agent，报价侧只负责处理询价邮件」。
> 一份文件管两边：**收信与分类权在邮件分拣 agent，报价侧永不再判「这封算不算询价」**。
> 本契约改了要同时改两边 cron 的 prompt（见 §5）。

## 1. 边界（一句话）

| 谁 | 管什么 | 入口 | 出口 |
|---|---|---|---|
| 邮件分拣 agent | 收信、去噪、判「是不是新的请报价」、投递/归档、报告 | `scripts/mail_agent_fetch.sh` | `customer-files/pending/`（仅询价）/ `customer-files/inbox/<日期>/_non_enquiry/`（非询价） |
| 报价侧（本 profile） | 只看 `pending/`：展开 .eml → 抽字段 → 写 CPMS → 过门禁 → 推 ERP | `scripts/scan_pending.sh` | `customer-files/processed/<task_id>/` + ERP |

判据知识（什么算询价、8 张「看着像询价其实不是」的面孔、隔离口径）**只在分拣侧**：
skill `quote-inbox-triage`。报价侧不再复述这套判据。

## 2. 目录与台账

```
customer-files/
  staging/                      # 分拣侧暂存：收下来但还没判的信（.eml）
  pending/                      # 交接面：只有被判为询价的信才允许出现在这里
  inbox/<YYYYMMDD>/             # 原始邮件归档（ingest_email 展开后由报价侧搬入）
  inbox/<YYYYMMDD>/_non_enquiry/ # 分拣侧归档的非询价原件（保留，不删）
  logs/mail_agent_ledger.jsonl  # 投递台账：{ts,file,status,dest,reason}
  .mail_agent_state.json        # 分拣侧去重台账（与报价侧 .mail_fetch_state.json 分开）
  .mail_agent_active            # 存在 = 分拣侧已接管，报价侧 scan_pending.sh 自己不再收信
```

**为什么两个去重台账分开**：同一封信不能被两边各收一次（会双建单）。
`.mail_agent_active` 这个文件就是「谁在收信」的唯一开关 —— 删掉它，报价侧立刻回到
「自己收信」的旧行为（分拣 agent 停摆时的手动回滚手段，不用改代码）。

## 3. 分拣侧操作（agent 每轮照做）

```bash
cd /home/claw/customer-quote-request

# 1) 收信 → staging/（脚本已做去噪：系统通知、退信、我方外发回信一律不落盘）
./scripts/mail_agent_fetch.sh          # stdout 空 = 没新信；非空 = 一封一行文件名

# 2) 逐封判（判据见 skill quote-inbox-triage，读 .eml 原件，不读 .txt.meta.json）

# 3) 投递 / 归档 —— 只准走这个脚本，别用裸 mv
uv run ./scripts/mail_agent_route.py --list
uv run ./scripts/mail_agent_route.py --quote     "customer-files/staging/<文件>.eml"
uv run ./scripts/mail_agent_route.py --non-quote "customer-files/staging/<文件>.eml" \
    --reason "CMA 检测结果通知：Result is passed，无产品规格"
```

规矩：

1. **一封都不许留在 staging/**。判不了的两条路：① 有产品事实但方向可疑 → 按询价投
   `pending/`（宁可让报价侧判，也别吞客户单）；② 完全无产品事实 → 非询价归档。
2. 文件名从 `--list` 原样复制（主题里常有 NBSP `\xa0`，手敲空格会 no such file）。
3. 判成询价的信**不改名、不拆附件**，报价侧的 `ingest_email.py` 会自己展开。
4. 非询价**只归档不删除**，`--reason` 必写一句（一句话说清为什么不是询价）；
   里面有业务要执行的结论（货期 / 包装确认 / 返利政策）时，报告里单独一段点出来。
5. 不加新的 `skip_senders` / `skip_subjects` 规则除非同一类噪声复发 2 次以上；
   客户域名、项目名会复发，按标题过滤会误杀真单（见 skill §4 红线 2）。

## 4. 报价侧操作（本 profile）

```bash
./scripts/scan_pending.sh          # 只扫 pending/（含 .eml 展开），不再收信
uv run ./scripts/cpms_todo.py --grace-minutes 15
```

拿到的东西一律已是「判为询价」的信 → **不再走三分类闸门**，直接进总纲
`customer-quote-request` 的抽取流程。遇到明显不像询价的（分拣侧判错），
不要自己隔离归档，按 §6 退回分拣侧并附证据。

## 5. 由谁跑

分拣是**技能** `wf-customer-email-categories`（工作流 + 判据速查 + 脚本入口），
**2026-11-20 起配一个 cron 常驻跑**：

| 时间 | 决策 |
|---|---|
| 2026-11-19 | 建 cron「邮件分拣 agent」`26d4e039dd93`；当日按业务要求删除，改为纯技能形态 |
| 2026-11-20 | 森西要求「恢复两个 cron，验证收信→发信全流程」→ 重建 `30c122af6196`（`*/5`，`mail_agent_fetch.sh`，加载该技能，汇报回 Discord） |

技能与 cron 并存：cron 只是「定时触发」，流程/判据全在技能里；手动补跑、换 profile 触发都不用改代码。

| 活 | 加载的技能 | 脚本 | 产物 |
|---|---|---|---|
| 分拣（收信+判） | `wf-customer-email-categories`（判据细节 `quote-inbox-triage`） | `scripts/mail_agent_fetch.sh`、`scripts/mail_agent_route.py` | `pending/` 仅询价；`inbox/<日期>/_non_enquiry/` 非询价 |
| 报价 | `customer-quote-request` | `scripts/cqr_scan_and_cpms.sh` | `processed/<task_id>/` + ERP |

两个脚本不重叠：分拣只写 staging/pending，报价只读 pending。

> 为什么没有独立 profile：`email-work`（H1）没有自己的 Discord bot token，跑在那边汇报只能落本地
> 文件。要真隔离，给 H1 配独立 bot token + 起 gateway 后，把本技能交给它加载即可，脚本/契约不用改。


## 6. 判错怎么退

分拣侧判成询价、报价侧一看明显不是 → **不自行归档**（否则两边台账打架），
在 pending/ 里原样留着 + 在汇报里写清：

```
退回分拣：【文件名】判成询价但实际是 <类型>，证据：<原文一句>，建议归到 _non_enquiry/<理由>
```

由人（森西）决定：① 分拣侧改判 + 补 skill 案例；② 报价侧照抽（宁可多建单）。

## 7. 回滚（分拣 agent 停摆）

```bash
rm customer-files/.mail_agent_active        # 报价侧恢复自己收信
# 分拣 job 用 cronjob pause（别 remove，配置要留）
mv customer-files/staging/*.eml customer-files/pending/   # 残留一并处理
```
