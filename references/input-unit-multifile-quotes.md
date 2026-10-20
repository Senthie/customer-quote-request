# 输入单元：一份报价由几个文件组成（口径 + 实现 + 边界）

> 业务拍板：2026-10-20 森西。版本：1.0.37（单元）→ 1.0.38（子目录拆分）。
> 代码：`scripts/input_unit.py`，入口 `resolve_input_units(path)`；
> 合并：`extract_fields.merge_customer_data()`。

## 1. 口径（一句话）

**看有没有子目录。**

| 你丢进来的东西 | 语义 | 产出 |
|---|---|---|
| 单个文件 | 独一份报价 | 1 个 ERP JSON |
| 文件夹 / `.zip`，**没有子目录** | 这一份报价的**全部参考文件**（规格 + 确认单 + 补充说明…） | 1 个 ERP JSON（字段合并） |
| 文件夹 / `.zip`，**有子目录** | **每个子目录 = 一份独立报价**；子目录内部继续合并 | 每个子目录 1 个 ERP JSON |
| 子目录之外、根下散着的受支持文件 | 另算一份，绝不偷偷并进某个子目录 | 1 个 ERP JSON，`<父目录名>_根目录文件` |
| 一封邮件 ≥2 个附件 | 一次询价，附件是同一单的参考材料 | 1 个 ERP JSON（`ingest_email` 投成单元目录） |

判据细节：子目录里**确实有受支持的文件**才算（空目录、`__MACOSX` 不算）；
子目录内部**不再往下拆**（`A/B/x.txt` 全归 A）。

## 2. 合并口径（无子目录 / 子目录内部）

1. **主文件** = 抽到规范键最多的文件（同分 → 表格类优先 → 文件名排序），值以它为准；
2. 同键不同值 → `conflicts`（`.extracted.json` + `validation.json`）+ 日志 WARN，**不静默丢**；
3. `rules.scan`（FSC 这类 unique 键）在**整个单元**上重跑：A 写 `FSC-Mix`、B 写 `FSC Recycled`
   = 真混用 → 留空；
4. 文本块按 `# 文件：xxx` 分段；`sources[规范键] = 文件名` —— 写 CPMS / 《来源对照》的溯源锚点；
5. 原始输入**整包**归档进 `processed/<任务ID>/`（每份只搬自己那一坨）。

## 3. 批次拆分（1.0.38）的行为

- `main()`：`units = resolve_input_units(path)` → 循环 `process_unit()`。
  1 个 = 老行为；多个 = 逐份处理，**一份失败不影响其余份**。
- 失败隔离：失败那份进 `failed/failed_<时间>/`（zip 拆出来的份是**复制**），
  日志 `处理状态: 失败 —— 继续处理同批剩余的 N 份`；退出码 `1`。
- 压缩包拆多份：**原包只随第 1 份归档**（`archive_owner`），其余份 validation 记
  `source_archive`（包名）+ `archive_owner: false` —— 不复制 N 份压缩包。
- 共享 `_email.meta.json`：每份**复制**一份；失败那份复制进 `failed/` 留证；
  批次收尾 `finalize_batch()` 清原件、父目录搬空就删掉，还剩东西就日志明说。
- validation 的 `unit`：
  `kind / kind_cn / label / role / path / origin / source_archive / archive_owner / members / skipped`。
  `role` = `whole`（整包一份）| `subdir`（某子目录那份）| `root_files`（根下散文件那份）。

## 4. 过滤与安全

- 跳过并记账（`unit.skipped`）：`__MACOSX` / `.DS_Store` / `Thumbs.db` / `desktop.ini` /
  `~$*` 临时文件 / 隐藏文件 / 不支持的扩展名；`*.meta.json` 是我们自己的副档，不算跳过。
- zip：拒绝对路径、`..`、盘符；上限 200 个文件 / 解压后 200 MB；解压到临时目录，用完删。

## 5. 已知边界（还没做，交付时人工看）

1. **空子目录**不产生报价（没有文件就没什么可报的）；
2. 子目录里如果其实是**同一单的多个版本**（`单号A/v1.xlsx`、`单号A/v2.xlsx`），
   会按合并口径走（主文件优先 + 冲突记账），不会自动挑最新版本 —— 看 `conflicts`；
3. 根下散文件被当成独立一份；如果它其实是**给所有子目录看的公共说明**
   （比如 `总则.txt`），需要人工把它复制进各子目录后重跑；
4. `.eml` 里的附件如果是 zip，先落盘再由 `main.py` 拆；
5. 批次汇总只打印，不落一个「批次清单」文件（要审计就看各份的 `validation.json`）。

## 6. 回归测试

`tests/test_input_unit.py`：单元识别 / 垃圾过滤 / zip 穿越 / 合并口径 /
端到端一个文件夹只出一份 / 子目录拆分与根下散文件 / 无子目录不变 /
zip 拆分 + 只归档一次 / 批次收尾两种结局 / 端到端两个子目录出两份。
