# 定时任务补 CPMS 的作业本（cron → CPMS → 交付）

> 起因：2026-10-21 业务问「为什么自动任务不会触发 CPMS？」—— 定时任务当时是 `no_agent`
> 纯脚本，只抽结构，CPMS 天生没人写。本文件是「补写与验收」的标准动作，配套
> `scripts/cpms_todo.py`、cron job `97e2e4bb05d0`、`SKILL.md` §4.12。

## 0. 一句话流程

```
pending/ ──scan_pending.py──► processed/<task_id>/ERP_*.json（CPMS 空）
                                      │
                          cpms_todo.py 列出欠账（CPMS / CPMC / CPMC_ZH）
                                      │
                          agent 读原文 → 写 CPMS + 中文品名
                                      │
                          cpms_lint → 0 error，cpms_content_audit → 0 无出处
                                      │
                          cpms_todo.py 归零 = 这一单可交付
```

写 **4 个字段**：`CPMS`（产品描述）、`CPMC`（产品名称）、`CPMC_ZH`（同 CPMC）、
`ECPMC`（英文品名，客户档案声明了 `rules.ecpmc.source_keys` 才需要）。

## 1. 交付前四道门（顺序执行）

```bash
cd /home/claw/customer-quote-request
F=<任务目录>/ERP_<task_id>_<时间戳>.json
X=<同一目录下的客户原始文件（.txt / .xlsx / .csv）>

uv run ./scripts/cpms_lint.py "$F"                                # ① 退出码 = error 数，必须 0
uv run ./scripts/cpms_content_audit.py --cpms "$F" --source "$X"   # ② 退出码 = 无出处条目，必须 0
uv run ./scripts/erp_schema.py --check "$F" --customer <客户id>     # ③ 必须 0 error
uv run pytest -q                                                  # ④ 全绿
```

⚠️ ② 的 `--source` **必须指具体客户文件**。指目录不报错，但等于没审 —— 这是个静默失效的坑。

## 2. 写之前先看清「原文怎么说」

按 `CPMS 严格模式`：只能写客户文件里出现过的事实；每一句都要能指到一个位置。
写不出出处的句子，当场删。产物目录里另存 `CPMS_来源对照.md`（逐句出处 + 刻意没写的项）。

## 3. 概要行必须等于 ERP 字段

`cpms_lint` 的 `CPMS011` 会拿概要行和 `CD/KD/GD/TZS/YYS/Drop_SJFX` 逐项比。
所以**不要手改产物里的字段去迁就一句话** —— 手改会在引擎重跑时丢。
正确动作：

1. 概要行按**字段现值**写；
2. 客户事实（原文的款数/数量）写进 `备注`，附原文；
3. 汇报里点名「字段 / 现值 / 原文 / 应然值」，由引擎侧修完重跑。

## 4. 已知例外：CPMS013（正方形 / 装订边未给）

`CD = KD`（如 210×210）或客户没写装订边位置（`at the top/side`）时，长/短书脊**推不出来**，
`Drop_SJFX` 落 `null`，CPMS 不写方向词 → `CPMS013` 必然报 1 条 warn。

**这 1 条是已知且有意接受的例外**，不许为了清零编一个「长书脊」。

## 5. 历史产物（v1，41 键那代）

2026-05 之前的产物是 v1 结构（41 键、`ZDFS` 非法值、时间戳格式日期）。
**不要拿 v2 的 `cpms_lint` 去「修」它们** —— 只补内部字段（`CPMC_ZH`），
需要交付时整单重跑。

## 6. 并发：别和 cron 抢同一份文件

cron 每 5 分钟一轮，agent 环节会直接改 `ERP_*.json`。手工改这些文件前先暂停任务：

```bash
# 通过 cronjob 工具：action=pause / action=resume，job_id=97e2e4bb05d0
```

改完、验收绿了再恢复。否则你写完的 CPMS 可能被下一轮覆盖（2026-10-21 实测发生过一次）。

## 7. 看完这一单，顺手看一眼引擎

补 CPMS 时发现「字段本身落错了」（不是文案问题），**不要在 CPMS 里掩盖**，
要按「先写测试、再改引擎、然后重跑」的路径修：

```bash
uv run pytest tests/test_ngv_text_enquiry.py -q      # 真实缺陷的回归
uv run ./scripts/main.py "<产物目录里的原文件副本>" --customer auto   # 重跑出新任务目录
# 旧目录 mv 到 customer-files/processed/_superseded_<YYYYMMDD>/
```

已修的三类（2026-10-21，CU26070901）：

| 症状 | 根因 | 修法 |
|---|---|---|
| `CPSL1=0` | `QTY: 60.000 St. in total (20.000 cps/title)` —— 总数写在数字**后面** | `parse_single_quantity()` 增加「数字在前 + `in total`」形态 |
| `TZS=1` | 纯文本 enquiry 没有 `versions:` 标签 | `rules.scan` 加 `^(N) titles (` 行首锚点（不锚会抓到客户疑问句里的 `4 titles`） |
| `FSCMC` 报 error | 客户写 `FSC-Mix` | `erp_form_schema.yaml` 的 `FSCMC.aliases` 归一化到 `FSC Mix` |
