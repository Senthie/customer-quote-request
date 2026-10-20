---
name: customer-quote-request
description: 读取客户（igl / NGV / …）的报价需求（邮件 + 附件 / XLSX / CSV / DOCX / TXT / PDF），按客户档案抽取字段并映射为 ERP JSON。新增客户只需加一份 YAML 档案。
metadata:
  openclaw:
    emoji: "👨‍💼"
    requires:
      bins:
        - uv
        - python3
---

# Customer Quote Request Parser（多客户版）

> **仓库路径：** `/home/claw/customer-quote-request/`
> 所有命令在仓库根目录执行（或写绝对路径）。
> **版本：** 1.0.39（2026-10-20 **口径复核**：`CD`=长 / `KD`=宽、`CPLB` 按类别树第 5 问判冲形状、
> 书脊方向由装订边推出、`YWY`=生成方标记 `KM`（业务复核确认）、CPMS 装订段不许照抄 `ZDFS`）
> —— 前序 1.0.38：批次拆分：文件夹 / 压缩包里**有子目录 → 子目录各算一份报价**；
> 根下散文件另算一份；没有子目录仍是一份；压缩包只归档一次）
> —— 前序 1.0.37：输入单元 —— 单文件 = 一份报价；文件夹 / 压缩包 / 多附件邮件 = 一份报价的**全部参考文件**
> —— 前序 1.0.36：`ECPMC`（产品英文名）由档案 `rules.ecpmc.source_keys` 从客户原文照抄
> —— 前序 1.0.35：装订方向 ≠ 书脊方向（识别不到 → CPMS 不写方向词、`Drop_SJFX` 留空 null）
> —— 前序 1.0.34：`CPMC` = 产品名（中文译名）、业务员 `KM` 落 `YWY`
> —— 前序 1.0.33：`.eml` 读取（正文即任务 / HTML 表格 / 文件名取单号）；1.0.32：CPMS 严格模式
> —— 前序 1.0.30：**CPMS 规范 v2**：业务拍板 5 条 —— 套装多件写「共【N】件」、
> `--fix` 自动补印色 C、备注允许留出处/人名、灰板一律写 g、`装箱：待确认` 可交付）
> —— 前序 1.0.29：`docs/CPMS_规范与样例集.md`（格式速览 + 6 份样例，由 `tests/test_cpms_doc_samples.py` 钉住）
> —— 前序 1.0.28（2026-10-17）**CPMS 写法定死**：18476 份历史单据实测 → `assets/cpms_spec.yaml`
> 骨架 + 闭集词表 + 槽位语法，`scripts/cpms_lint.py` 一条命令校验，`docs/CPMS_写作规范.md`
> 含 3 个真实单据的逐行改写）

把**客户发来的报价需求**（邮件标题/正文 + 附件，或直接给的表格/文档）转换成
**ERP 导入用的 59 字段 JSON**（产物末尾另追加 4 个我方内部字段：`ID` / `CPMC_ZH` / `Reminder` / `version`，
可用 `--strip-internal` 去掉 —— 见 1.0.22 / 1.0.23 与 `assets/internal_export_fields.yaml`）。

---

## 0. 一句话架构（先看这个）

```
                    customers/<客户id>.yaml   ← 客户习惯全在这里（唯一要改的地方）
                              │
  邮件 ──► ingest_email.py ──►│（≥2 个附件 → 投成一个单元目录）
          （附件落 pending     │
            + 客户单号 KHDH）  ▼
  输入单元 ──► input_unit.py ──► 单文件 = 1 份报价 / 文件夹·zip = 1 份报价的 N 个参考文件
                                    （文件夹里有子目录 → 子目录各算 1 份报价）
                                    │
                     extract_fields.py（多文件 → 合并成一个 CustomerData）
                                    │
                     main.py ───────┴──► 归一化 + 校验（erp_schema.py）
                                    │      ×  assets/erp_form_schema.yaml（ERP 合法枚举）
                                    │
                                    ├──► ERP JSON（59 字段 + 内部字段 ID / CPMC_ZH / version）
                                    └──► CPMS 留空 → ★ agent 按 assets/cpms_spec.yaml 写
```

### 0.0 输入单元：一份报价到底由几个文件组成（业务 2026-10-20 拍板）

| 你丢进来的东西 | 语义 | 产出 |
|---|---|---|
| **单个文件**（xlsx / csv / txt / docx / pdf / eml 正文） | **独一份报价** | **1 个** ERP JSON |
| **文件夹**（没有子目录） | 这一份报价的**全部参考文件**（规格表 + 确认单 + 补充说明…） | **1 个** ERP JSON（字段合并） |
| **压缩包 `.zip`**（没有子目录） | 同上（安全解压后按文件夹处理） | **1 个** ERP JSON |
| **文件夹 / 压缩包里有子目录** | **每个子目录 = 一份独立报价**（子目录内继续合并）；根下散文件另算一份 | **每个子目录 1 个** ERP JSON |
| **一封邮件 ≥2 个附件** | 同上：客户的一次询价，附件都是同一单的参考材料 | **1 个** ERP JSON（`ingest_email` 自动投成单元目录） |

```bash
# 单个文件（老用法，不变）
uv run ./scripts/main.py customer-files/pending/报价.xlsx --customer auto
# 文件夹 / 压缩包 = 一份报价（里面没有子目录）
uv run ./scripts/main.py "customer-files/pending/CU26070802 询价包/" --customer auto
uv run ./scripts/main.py customer-files/pending/询价包.zip --customer auto
# 里面有两个独立单（子目录）= 两份报价，一条命令全跑完
uv run ./scripts/main.py customer-files/pending/两张单/ --customer auto
#   → 子目录 CU26070001/ CU26070002/ 各出一个 ERP JSON；任一份失败不影响另一份
```

**合并口径**（`extract_fields.merge_customer_data`）：

1. **主文件** = 抽到规范键最多的文件（同分→表格类优先→文件名排序），值以它为准；
2. 同一规范键在别的文件里**给了不同的值** → 记进 `.extracted.json` 的 `conflicts`
   + `validation.json` 的 `conflicts`，日志 WARN —— **交付前必须人工核对，不许静默丢**；
3. `rules.scan`（FSC 这类 `unique` 键）**在整个单元上重跑**：A 文件写 `FSC-Mix`、
   B 文件写 `FSC Recycled` 就是真混用 → 留空；
4. 文本块按 `# 文件：xxx` 分段拼接，**CPMS 与《来源对照》必须写清出自哪个文件**；
5. 原始文件夹/压缩包整包归档进 `processed/<任务ID>/`（参考文件必须待在一起）；
6. **有子目录就拆**（1.0.38）：`resolve_input_units()` 把「一包」拆成「一包里的 N 份」，
   每份只吃自己子目录里的文件 —— 子目录内部仍然按上面 1~5 条合并，**不再往下拆**。

**过滤与安全**：`__MACOSX` / `.DS_Store` / `Thumbs.db` / `~$临时文件` / 隐藏文件 / 不支持的扩展名
一律跳过并记账（`validation.unit.skipped`）；zip 拒绝绝对路径与 `..` 穿越，
上限 200 个文件 / 解压后 200 MB。

**三层职责，不要混：**

| 层 | 文件 | 职责 | 新增客户要改吗 |
|----|------|------|---------------|
| 客户档案 | `customers/*.yaml` | 这个客户怎么写字号/表头/档位/材质/**分区表**；怎么从邮件取单号 | **要**（只改这里） |
| 输入单元 | `scripts/input_unit.py` | 判断「这一份报价由哪几个文件组成」：单文件 / 文件夹 / zip；**有子目录 → 子目录各算一份**（`resolve_input_units`）；安全解压、垃圾文件过滤 | 不用 |
| 抽取引擎 | `scripts/extract_fields.py` | 按档案把任意文件抽成「规范键 → 值」，多文件按 `merge_customer_data` 合并。**两套范式**：平铺表走 `auto_pairs`（igl），分区表单走 `section_pairs`（NGV）—— 各家的细节见对应子技能（2.6） | 一般不用（除非布局全新） |
| 业务映射 | `scripts/main.py` | 规范键 + 档案规则 → ERP 字段；输出断言 | 不用 |
| **取值权威** | `assets/erp_form_schema.yaml` | **ERP 只认哪些值**（下拉枚举/类型/必填） | 不用（ERP 全局恒定） |

> **客户专属知识在独立子技能里**：处理 IGL 前加载 `customer-quote-igl`、
> 处理 NGV 前加载 `customer-quote-ngv`（附件形态 / 抽取范式 / 口径 / 坑 / 检查清单）。
> 本文件是**总纲**：架构、引擎、邮件入口、字段总览、`CPMS` 规则、校验方法、新增客户 SOP。
>
> **铁律：`main.py` 里不允许出现任何客户的列名。**
> 列名只属于档案。看到 `main.py` 里写死了某个客户的表头，就是重构退化了。
>
> **推论**：客户差异一律走档案开关 —— `quantity.mode`、`spine.mode`、`unit`、
> `cpmc.template`、`binding.match_keys`、`thickness.enabled`、`rules.scan`、
> `versions.empty_when_single|when_missing`、`size.zero_tops`、`required_empty` …
> 加一个客户**不该**新增 `if 客户 == xxx`。现有客户是 igl（平铺）与 NGV（分区表单），
> 两个范式都已校准；再加客户先判它属于哪一种（见 2.1）。
>
> ★ **业务 2026-09-24 强调：每个客户的附件类型不一样、格式也不一样。**
> 所以**永远不要把一个客户的结构套到另一个客户身上** —— 拿真实附件校准（`calibrate_customer.py`），
> 判形态 → 填档案 → 用 `scripts/compare_with_erp.py` 与权威 ERP 导出对照。
> 「上一个客户是平铺表，这个客户估计也是」是这类工作里最贵的假设。

---

## 0.1 一页速查（2026-10-19 实单 `BJXQ260704002` 教训沉淀）

> 时间紧只看这一节。细节在后面各章；数字与命令都实测过。

### CPMS 严格模式（业务拍板，默认）

- **只写客户文件（附件 / 邮件正文）里出现过的事实**；确认单、ERP 导出、历史单据、别的客户档案里的信息**一律不写**。
- 判定口径：**每一句都要能指到客户文件的一个位置；指不到的删掉**。
- 客户给区间就照抄区间（`50-60 stickers` 不许写成 `60`）；客户空栏就是空栏（窗口尺寸没写就不写）。
- 不许把英文泛称「翻译」成具体材料：`polybag` ≠ `OPP`，`foil package` 就是铝箔袋，`art card C1S` 就是单粉。
- 客户自己标的不确定表述照抄：`预估`、`暂定`、`see below`、`按…预`。
- 附件里的**语义未明**内容（页脚数字、内部代号）放 `备注` 并注明「客户原文 / 语义未明」，不许猜。
- **漏抄是缺陷，多写是错误信息**。宁可短，不许编。

### 交付前四道门（顺序执行，全绿才算完）

```bash
cd /home/claw/customer-quote-request
F=<产物目录>/ERP_<task_id>_<时间戳>.json
X=<同一目录下的客户原始文件>

uv run ./scripts/cpms_lint.py "$F"                                  # ① 退出码 = error 数，必须 0
uv run ./scripts/cpms_content_audit.py --cpms "$F" --source "$X"    # ② 退出码 = 无出处条目数，必须 0
uv run ./scripts/erp_schema.py --check "$F" --customer <客户>        # ③ 必须 0 error
uv run pytest -q                                                    # ④ 全绿（当前 349 项）
```

② 会同时报「客户文件有、CPMS 没写」的漏抄项 —— 逐条判是真漏还是有意不写。

### 客户给的是邮件（.eml）时

```bash
uv run ./scripts/ingest_email.py --eml "客户邮件.eml"     # 附件落 pending / 正文落 .txt，两种都行
uv run ./scripts/main.py <上面打印的那个文件> --customer auto
```

- **附件版**（igl）：附件 + sidecar 进 `pending/`；
- **正文版**（NGV/KiK 这类 enquiry，无附件）：正文落成 `<单号>_<标题>.txt`，
  HTML 表格会被压成 `标签: 值` 行；
- 单号（`KHDDH`）可能只在 **`.eml` 文件名**里 → 脚本会扫，来源标「邮件文件名」。

### `CPMC` 与 `YWY` 的分工（业务 2026-10-19 更正）

| 字段 | 内容 | 谁填 |
|---|---|---|
| `CPMC` | **产品名称**，从客户文件译成中文（不带业务员/品牌/类别/FSC） | agent（脚本留 `【中文品名待定】`） |
| `CPMC_ZH` | 与 `CPMC` 同值（内部字段） | agent |
| `YWY` | 业务员代号 **KM**（= 我） | 脚本固定值 |

- 旧写法 `KM/Aldi <原文>(FSC)` **已废**；品牌（Aldi/KiK/Tchibo）不塞进产品名。
- 译名只能译、不能编：`Ostermalblock` → `复活节彩蛋涂色本`；没把握就标注存疑，别凑品类词。

### 每单产物里要留的两份东西

| 文件 | 内容 |
|---|---|
| `ERP_*.json` | 59 字段 + `ID`/`CPMC_ZH`/`Reminder`/`version`，`CPMS` 非空、`CPMC_ZH` 无占位符 |
| `CPMS_来源对照.md` | 逐句出处（单元格坐标）+ 已删除项 + 未写项；交付时一起给业务 |

> 来源对照不是形式：业务 2026-10-19 明确要求「你写的东西必须是通过客户文件得到的」，
> 这份表就是自证材料。写不出出处的句子，当场删。

### 客户档案不能凭空推断（同批踩过）

- `Wire-O` = **双线圈 → 26.07;普通YO书**；`spiral`/单线圈才是 26.01 普通蛇圈书。档案已拆成两条规则，别再合并。
- 同一条正则里塞多个近义词，迟早会把两个树节点混成一个 —— 宁可多写几条，每条一个节点。
- 判断口径只认**真实 ERP 导出**，不认「看起来更合理」。

---

## 1. 快速开始

```bash
cd /home/claw/customer-quote-request

# ① 有邮件（.eml 或纯文本）—— 附件会自动落 pending，并带上客户单号
uv run ./scripts/ingest_email.py --eml /path/to/mail.eml
uv run ./scripts/ingest_email.py --subject "PO 23992" --body "..." --attach /path/to/quote.xlsx

# ② 处理（客户自动识别；不确定就显式指定）
uv run ./scripts/main.py "customer-files/pending/QUOTE.xlsx" --customer auto

# ③ 看输出
#    customer-files/processed/<task_id>/
#      ├── ERP_<task_id>_<时间戳>.json        ← 交付物（CPMS 此时为空）
#      ├── QUOTE.extracted.json              ← 抽取明细（规范键/未识别标签/CPMS 素材）
#      ├── QUOTE.csv
#      └── QUOTE.xlsx
```

成功的样子（两行必须都出现）：

```
字段一致性断言: 通过（59 字段，顺序与模板一致）
客户档案: igl（自动识别）
```

然后**轮到 agent 亲自写 CPMS**（见第 5 节）。

**跑完之后的三条常规动作：**

- 原附件已被移进 `customer-files/processed/<task_id>/` —— **重跑读那份副本**，别再读 `pending/`（会 `FileNotFoundError`）。
- 同一单重跑出新任务目录后，把旧目录 `mv` 到 `processed/_superseded_<YYYYMMDD>/`，别让一个报价在交付区躺两份。
- 业务若给了**真实 ERP 数据**要求对差异 —— 走 `references/真实ERP回灌比对.md` 的流程（先分桶「真错 / 口径 / 没抽 / 格式」，再动档案）。
- 业务说「我上传了」但 `pending/` 没新文件时 —— **先自己找**（`pending/` 列表 + 全盘 `find -newermt`），
  确认没落盘再让他重发；别默认文件一定到了（聊天附件可能只有文字过来）。写的时候记住两条实测事实：

- **同一标签可以重复出现**：多材料内文就写三行（`内文：32PP，100g书纸(FSC)，4C/4C` /
  `内文：8PP，230g单粉，4C+哑油/0C，啤` / `内文：2PP，80g铜版贴纸，4C/0C，可移胶`），
  `cpms_lint` 只校验段落 `order` 单调、**不禁止重复标签** —— 比把三种材料塞进一行可读得多。
- **`cpms_lint --fix` 的「印色补 C」只动 order 100~299 的部件行**，`包装` / `装箱` / `备注` 里的
  `1/0C` 不会被修（CPMS052 一直报），必须手改成 `1C/0C`。别以为跑过 `--fix` 就干净了，要复跑看退出码。

### 1.1 三条操作事实（每次都会遇到）

- **`main.py` 跑完会把原附件从 `pending/` 移进 `processed/<task_id>/`** —— 之后复核原文、
  重写 CPMS 要读产物目录里那份副本，别再找 `pending/`（找不到会误判成「文件丢了」）。
- **重复提取同一份文件**：先看 `processed/` 里旧任务目录有没有现成的 `CPMS` / `CPMC_ZH`，
  有就复用再按当前规范复核（省一遍通读），然后照常重跑生成新任务目录、用新产物交付。
- **用户说「上传了」但 `pending/` 为空**：先搜全盘近几小时的 xlsx/xls/csv/pdf/docx/eml，
  确实没有就直说「没收到」并请对方重发或放进 `pending/`；**不要拿目录里的旧文件凑数，也不要猜**。

---

## 2. 客户档案（新增客户只看这一节）

每个客户一份 `customers/<id>.yaml`。**改客户的解析方式 = 改档案，不改代码。**

### 2.1 现有档案

| 档案 | 客户 | 子技能（客户专属知识） | 状态 | 文件形态 |
|------|------|------------------------|------|----------|
| `customers/igl.yaml` | IGL | **`customer-quote-igl`** | ✅ calibrated（真实样本校准） | 平铺 + 横向键值对表 |
| `customers/ngv.yaml` | NGV | **`customer-quote-ngv`** | ✅ calibrated（4 份真实附件 + ERP 导出） | **德英双语 enquiry 分区表单** |
| `customers/_defaults.yaml` | 兜底 + 公共默认值 | 所有档案都叠加在它之上；认不出客户时用它（会打 WARN） | — |
| `customers/_template.yaml` | 新客户骨架 | 复制它开始 | — |

两种文件形态对应两套抽取策略，**新增客户前先判你手上是哪一种**
（判错形态会把时间浪费在调别名表上）：

| | igl 范式 | NGV 范式 |
|---|---|---|
| 形态 | 标签与值同行/同列平铺 | A 列分区 + B 列标签 + C 列起值 |
| 策略 | `auto_pairs` | `section_pairs` |
| 档案段落 | `fields:` 别名表 | `fields:` + **`sections:` 分区表** |
| 麻烦点 | 值可能隔空列、一行多组键值 | **同标签在不同分区含义不同；同分区内标签会重复** |
| 详见 | 子技能 `customer-quote-igl` + 本文 2.5 | 子技能 `customer-quote-ngv` + 本文 2.6 |

```bash
uv run ./scripts/customer_profile.py --list       # 有哪些档案
uv run ./scripts/customer_profile.py --show igl   # 某份档案摘要
uv run ./scripts/customer_profile.py --validate   # 校验全部档案
uv run ./scripts/customer_profile.py --detect <文件> --subject "<邮件标题>"
```

### 2.2 新增一个客户：3 步

**第 1 步 · 校准**（拿一份真实报价文件喂给脚本）

```bash
cp customers/_template.yaml customers/<新客户id>.yaml
uv run ./scripts/calibrate_customer.py --customer <新客户id> --file <样本文件> [--layout]
```

> **先判形态**：打开样本看一眼 —— 是「标签/值平铺」（→ `auto_pairs` + `fields:`），
> 还是「分区表」（A 列分区 / B 列标签 / C 列起值 → `section_pairs` + `sections:`）？
> 判错形态会把时间浪费在调别名表上（NGV 的样本用平铺策略实测只抽出 4 个键，
> 而且值全是错的）。

脚本会打印：已识别的规范键、数量档位、**像标签但认不出的单元格**（附候选规范键建议），
并写出 `customers/_draft_<id>.yaml` 供对照。报告自动过滤两类噪音：

- 已被抽出来的**值**（Autumn / Low / Y …）
- 整列出现的**下拉候选值清单 / 成本明细表**（同列 ≥8 个「像标签」的单元格）
- 已在档案 `extras:` 登记过的**非 ERP 字段**

**第 2 步 · 填档案**：把报告里剩下的标签补进 `fields:`，把该客户特殊口径写进 `rules:`。
只写与 `_defaults.yaml` 不同的部分（list 是整体替换，不是追加）。

**第 3 步 · 回归**：

```bash
uv run ./scripts/main.py <样本文件> --customer <新客户id>
uv run pytest                                      # 全部测试必须绿（当前 332 项）

# 有权威 ERP 导出时，做一次分类对照（差异原因写进 customers/<id>.erp_diff.json）
uv run ./scripts/compare_with_erp.py --customer <新客户id> \
    --attachment <样本附件> --erp <对应的 ERP 导出.json> \
    --accepted customers/<新客户id>.erp_diff.json
```

59 键断言通过 + 字段值人工核对无误 → 把 `status: draft` 改成 `calibrated`，
并在上面的「现有档案」表里登记。

### 2.3 档案规范

```yaml
id: igl                       # 必填，与文件名一致
name: IGL
priority: 100                 # 自动识别冲突时的优先级
status: calibrated            # draft | calibrated（draft 会被 WARN，需人工核对）
notes: 一句话说明这家的报价单长什么样

# ── ① 身份识别 ────────────────────────────────────────────
detect:
  label_signatures:           # 文件里出现这些标签之一 → 判为该客户（最可靠，100 分/个）
    - "Works References For all Titles"
  min_signature_hits: 1
  filename_keywords: [puzzle, colour]   # 文件名包含匹配（10 分/个）
  sender_domains: []                    # 发件人域名（30 分/个）
  subject_keywords: []                  # 邮件主题关键词（5 分/个）

# ── ② 字段别名表：客户列名 → 规范键（客户习惯全在这）──────
fields:
  series_name: ["Series Name", "Series"]
  format:      ["Format"]
  ...

# ── ③ 非 ERP 字段的附加信息（必须写进 CPMS）─────────────
extras:
  - Quotation Number
  - Carton Weight

# ── ④ ERP 固定值（落库值）─────────────────────────────────
erp_fixed:
  CPDL: 书版
  YYS: 1
  PXH: ""                    # 写空串 = 显式留空（模板默认的 1 会被真的清掉）

# ── ④b 客户偏好表（给人看：同一批字段 + 依据）──────────────
# 业务 2026-09-24 要的「喜爱表」：长期客户有稳定口径，一眼看全、快速填写。
# value 非空 = 落库这个值；value 空 = 显式留空（推不出来不猜）。
# 必须与 erp_fixed 一致（validate + 测试拦着）。查看：customer_profile.py --prefs
preferences:
  - { field: CPDL, value: "书版", reason: "唯一合法值" }
  - { field: PXH,  value: "",     reason: "由 ERP/业务侧填 → 留空" }

# ── ⑤ 解析规则 ───────────────────────────────────────────
rules:
  layout:
    strategies: [auto_pairs, column_pairs, shifted_pairs]
    label_col: 1                 # column_pairs 用
    value_col: 2
    min_label_row_hits: 2        # ≥2 个标签的行算「密集标签行」→ 优先取下方
    max_label_len: 60            # 超长单元格不可能是标签
  size:
    unit: mm
    order: [CD, KD, GD]          # 客户写 H x W x D 的顺序
  quantity_tiers:
    k_suffix: true               # 支持 3k / 7.5k
    plain: true                  # 支持 3000
    plain_label_hint: "quantity|qty|total|cost|数量"
    max_tiers: 10
  materials:                     # ★「纸张类型映射表」已落地
    text:
      default: 书纸
      map:
        - { match: "woodfree|uncoated|书纸", page_style: 书纸 }
        - { match: "art ?paper|c1s|铜版", page_style: 光粉纸 }
    cover:
      default: 光粉纸
      map: [...]
  binding:                       # 客户装订术语 → ERP ZDFS（按需开启）
    enabled: false
    map: {"perfect bound": 胶装/PAD书}   # ★ 值必须是 ERP 合法枚举（`无线胶装` 不是）
  cplb:                          # 产品类别：客户写法 → 产品类别树节点编号
    match_keys: [format, binding, finishes, project_description, series_name]
    map:
      - { match: "board\\s*book|纸板书", node: "27.02" }
      - { match: "perfect\\s*bound|paperback|无线胶装", node: "23.01" }
  product_category:
    fallback: "32.00;其他"        # 兜不住时用（必须在 assets/cplb_tree.yaml 里）
  cpsx:                          # 产品属性：年龄段 + 安全检查 → ERP 中文枚举
    threshold: 3
    above: 三岁以上
    below: 三岁以下
    above_with_test: "3岁以上（要寄第三方测试）"
    below_with_test: "3岁以下（要寄第三方测试）"
    safety_true: [y, yes, true, 是, 1, ✓]
  dates:
    JHRQ: { from: pub_month }    # 哪个规范键当交货日期
    WCRQ: { mode: today_plus_days, days: 10 }
  spine:
    mode: cd_vs_kd
    long: 长书脊
    short: 短书脊

# ── ⑥ 邮件规则 ───────────────────────────────────────────
email:
  khdh:
    attachment_keys: [works_references]     # 附件里哪一列是客户单号
    subject_patterns: ['(?i)\b(?:p\.?o\.?|ref|order)\s*[:#]?\s*(\d{4,6}(?:\s*[/,]\s*\d{4,6})*)']
    body_patterns:    ['(?i)works?\s+references?\s*[:#]?\s*(\d{4,6}(?:\s*[/,]\s*\d{4,6})*)']
```

### 2.4 规范键（`fields:` 的键只能用这些）

抽象出「客户怎么写」与「ERP 要什么」之间的中间层。新增规范键 = 同时改 `main.py`。

| 规范键 | 含义 | 规范键 | 含义 |
|--------|------|--------|------|
| `imprint` | 品牌/印名 | `size` | 成品尺寸串 |
| `complexity` | 复杂度 | `format` | 装帧形式（查 CPLB） |
| `requested_by` | 申请人 | `extent` | 页数（含封面） |
| `pub_month` | 出版月份（默认 → JHRQ） | `text_printing_spec` | 内文印刷 |
| `series_name` | 系列名（→ CPMC / ECPMC） | `text_material` | 内文纸张 |
| `number_of_titles` | 款数（→ TZS） | `cover_printing_spec` | 封面印刷 |
| `group` / `category` | 组 / 类别 | `cover_material` | 封面纸张 |
| `safety_checks` | 是否安全检查 | `spine_width` | 脊宽 |
| `target_age_group` | 目标年龄段 | `binding` | 装订说明 |
| `works_references` | 作品参考编号 | `finishes` | 表面处理 |
| `group_series_number` | 组别编号 | `quantity_tiers` | 数量档位行的标签 |
| `isbn` | ISBN | `khdh` | 客户单号（若客户有专门一列） |
| `project_description` | 项目描述 | | |

> `safety_checks` / `target_age_group` 在 v2 模板里没有对应字段，但**会落库到 `CPSX`
> 产品属性**（业务确认 2026-09-23），同时**仍必须写进 `CPMS`**（见 4.7）。

### 2.5 布局策略（`rules.layout.strategies`）

按顺序执行，**先到先得**（不覆盖已有值），所以可以互补：

| 策略 | 规则 | 适用 |
|------|------|------|
| `auto_pairs` | **主策略**。对每个标签单元格：先看**同列下方**一格（非空且非标签就用它）；否则看**同行右侧**最近的「非空且非标签」格。所在行是「密集标签行」时优先取下方，否则优先取右侧 | 绝大多数报价单（含横向键值对与标签行/数据行混排） |
| `column_pairs` | 固定列：标签在 `label_col`、值在 `value_col` | 列位固定的表 |
| `shifted_pairs` | 只向右扫（纯横向键值对） | 横向键值对布局 |
| `section_pairs` | **分区表**。A 列分区（向下继承），**按行位置**对齐 `sections:` 声明的条目：`section_value` 取 B 列自身；`label` 取右侧（`value_mode: first`/`run`） | 分区式表单（NGV，见子技能 `customer-quote-ngv`） |

> 表头即标签的整列表格不需要单独策略：引擎统一按 `header=None` 读表，
> 真表头会落在第 0 行，由 `auto_pairs` 按「密集标签行 → 取其下方」处理。
>
> **实测**（igl 的真实报价单）：`auto_pairs` 一条规则同时吃下了
> `Imprint|Autumn`、`Requested By|(空格)|Christine`、`Group/Series Number|(空格)|6366`
> （横向，值隔着空格）与 `Pub Month`/数据行、`Size…`/下一列（纵向），共 23 个规范键。

---

### 2.6 客户专属知识在子技能里（已拆分）

引擎只认「形态」；每家客户的表单结构、口径、坑放在**独立子技能**里，按需加载：

| 客户 | 子技能 | 附件形态 | 抽取策略 |
|------|--------|----------|----------|
| IGL | **`customer-quote-igl`** | 平铺 + 横向键值对报价表 | `auto_pairs`（+ `column_pairs` / `shifted_pairs`） |
| NGV | **`customer-quote-ngv`** | 德英双语 `enquiry` 分区表单（`A1:K32`：A 列分区 / B 列标签 / C 列起值） | `section_pairs` + 档案 `sections:` |

**处理一份文件的顺序**：读本总纲 → 看识别结果 → **加载对应子技能** → 按子技能里的
口径 / 坑 / 交付检查清单干活。新增客户见 2.2，校准完同样给它写一份子技能（模板照现成的两份）。

引擎侧对两种范式只需要知道这么多：

- **`auto_pairs`**：主策略，标签在下方或右侧，一条规则通吃「横向键值对 + 标签行/数据行」。
- **`section_pairs`**：按**行位置**对齐档案 `sections:` 声明的条目 ——
  `section_value` 取 B 列自身（合并格即值），`label` 取右侧（`value_mode: first`/`run`）。
  声明的 `label` 兼作**断言**：结构一变、行位置对不上就打 WARN 并把该格记进
  `unmatched_labels`，**宁可空着不猜**。

分区表单那些真正的坑（同一标签跨分区含义不同、同分区内标签重复、A 列干扰文字、
B 列自身即值）**全部在 `customer-quote-ngv` 子技能里**，改 NGV 之前先读它。

### 2.7 客户偏好表（`preferences:`）—— 业务「喜爱表」

> 业务要求（2026-09-24）：「因为有些客户和我们的达成合作比较久，所以我们会有一些默认值，
> 你也需要根据不同的用户设置一份喜爱表，方便快速填写用户习惯。」

每个客户档案多一段 `preferences:`，把「这个客户哪些字段固定填什么、哪些字段就该空着」
连同**依据**写在一处。口径只有两种，**都必须显式声明**，不许留空白让人猜：

| 写法 | 含义 |
|------|------|
| `value` 非空 | 落库这个值（必须与 `erp_fixed` 一致） |
| `value: ""` （空） | **显式留空** —— 该客户这个字段就该空着，推不出来不猜。<br>⚠️ 模板自带默认值时必须靠它清掉（如 `PXH` 模板默认 `1`） |

```yaml
preferences:
  - { field: BZ,    value: "美元", reason: "该客户报价一律美元（长期口径）" }
  - { field: PXH,   value: "",     reason: "排序号由 ERP/业务侧填 —— 模板默认 1 必须清掉" }
```

**与 `erp_fixed` 的分工（刻意分开，别合并）**

| | 谁读 | 内容 |
|---|------|------|
| `erp_fixed` | **机器**（`main.py` 只认它） | 落库值。写 `""` = 显式留空，`main.py` 会真的把模板默认值清掉（1.0.18） |
| `preferences` | **人**（业务/agent） | 同一批字段 + **依据**，供快速核对与填写 |

两者必须一致：`customer_profile.py --validate` 与 `tests/test_customer_profile.py`
都会拦（"偏好表写留空、`erp_fixed` 还填着 1" 这种漂移，产物里会悄悄留下模板默认值）。

```bash
uv run ./scripts/customer_profile.py --prefs        # 所有客户的偏好表
uv run ./scripts/customer_profile.py --prefs ngv    # 单看某客户
uv run ./scripts/customer_profile.py --validate     # 一致性校验
```

现有偏好表：`igl`（6 条）/ `ngv`（15 条）。新增客户时照抄 `_template.yaml` 里的骨架。

## 3. 邮件入口 `ingest_email.py`

**传输无关**：谁都可以调，不依赖具体收信方式。

### 3.1 `.eml` 怎么读（1.0.33 实单 `CU26070802`）

客户邮件有三种形态，脚本都吃：

| 形态 | 例子 | 怎么处理 |
|---|---|---|
| **附件里有报价表** | igl 的 `PUZZLE COLOUR DISNEY.xlsx` | 附件落 `pending/` + 每个附件一份 sidecar |
| **正文即规格（无附件）** | NGV/KiK `price enquiry. Ostermalblock.eml` | ★ **正文落成 `.txt` 任务**（`--body-as-task`，默认开），`main.py` 直接吃 |
| **只有 HTML（无 text/plain）** | 网页版邮件 / 转发件 | HTML 转文本；表格压成 `标签: 值` 行（`html_tables_to_text`） |

读 `.eml` 的四条实现要点（都踩过）：

1. **`text/plain` 优先**，HTML 兜底；HTML 表格单独压平成 `TPS: 148 x 210 mm portrait` 这种行 ——
   规格常只存在表格里。
2. **内嵌图片（`Content-Disposition: inline`）不是附件**：签名 logo、公司图都算 inline，落盘只会污染 pending。
3. **单号也从 `.eml` 文件名取**：实测 `CU26070802 - price enquiry. Ostermalblock.eml`
   表内没有单号，号码只在文件名里 → `KHDDH=CU26070802`。取值说明写「邮件文件名」。
4. **正文任务的文件名 = 单号 + 标题**（`CU26070802_price_enquiry._Ostermalblock.txt`），
   便于在 `pending/` 里认；`--no-body-as-task` 可关掉（只留 `_email_*.json` 记录）。

### 3.2 文本模式（`.txt` / 正文任务）抽取的两个坑

- ★ **值里带标签词会被整条丢掉**（1.0.33 修的）：旧实现对「标签下一行」用
  `match_label()`（整词包含）判断是不是标签，于是
  `64 pp + cover`（含 `cover`）、`100 gsm offset paper`（含 `paper`）、
  `30.100cps (one title, one language)`（含 `title`）全被当成「标签」跳过 → 值静默消失。
  现在只拒绝**整行就是一个标签**（`match_label_exact`，允许尾随冒号）。
- ★ **`Quantity:` 会落到 `quantity_tiers` 合成键**：文本模式没有档位行，
  `Quantity: 30.100cps` 就是单一数量 → 抽取时改投 `quantity`，交给
  `main.parse_single_quantity()`（德式千分位 `30.100 → 30100`、`= 总数` 都在那儿）。

### 3.3 客户识别也要看发件人

纯英文 enquiry 邮件没有 `enquiry` 表单标签。NGV 档案已补：

```yaml
detect:
  label_signatures: [..., "price enquiry", "Finished binding", "euroscale",
                     "Technical Specification and Print Runs"]
  sender_domains: [vemag-medien.de, naumann-goebel.de, ngv.de]   # 30 分
```

实测 `CU26070802` 得分 530（5 个正文特征 + 域名），不再落到兜底档案。
新增客户时同样要把「域名 + 正文特征」一起填，光靠文件名迟早判错。

```bash
# A. .eml 文件（邮件客户端导出 / 任何 IMAP 脚本落盘）
uv run ./scripts/ingest_email.py --eml mail.eml

# B. 只有纯文本（Hermes 邮件工具 / 剪贴板 / 其它系统推送）
uv run ./scripts/ingest_email.py \
    --subject "PO 23992 / 23993 - PUZZLE COLOUR DISNEY" \
    --body "Works References: 23992 / 23993" \
    --attach "/tmp/PUZZLE COLOUR DISNEY.xlsx"

# C. 整个目录当附件
uv run ./scripts/ingest_email.py --subject "..." --body "..." --attach-dir /path/to/dir
```

它做三件事：

1. 附件落到 `customer-files/pending/`（重名自动加 `__1` 后缀，不覆盖）
2. 按**客户档案** `email:` 段声明的**每组单号规则**（`target` 指定落哪个 ERP 字段）
   从**邮件标题/正文**取号 —— 例：NGV 的 `KHDDH`（香港单号）
3. 给每个附件写 sidecar `<附件名>.meta.json`（标题/正文/发件人/`numbers`/识别到的客户）

`main.py` 处理该附件时会**自动读取 sidecar**：每组单号的优先级是
**邮件标题/正文（sidecar 的 `numbers`）→ 附件列（`attachment_keys`）→ 全表扫描的规范键**；
处理完 sidecar 会一起归档进任务目录。

**客户识别顺序（2026-09-24 修正）**：**表格正文优先** —— 表格类文件一律把正文文本
一起送进识别，文件名/主题只当加分项；得分 <30（只有文件名/主题这类弱证据）会打 WARN。
原因：NGV 那份文件名含 `Colour`，命中 igl 的 `filename_keywords`（10 分）就胜出，
正文里 500 分的 NGV 特征**压根没被读过** → 整单字段全错。
各客户的特征标签 / 关键词写在各自档案的 `detect:` 段，细节见对应子技能。

> ⚠️ **尚未实现：IMAP 直连收信。** 需要先确认公司收信方式（IMAP 服务器 / 转发到本地 /
> 邮箱 API），再加「取信 → 落 .eml → 调本脚本」的适配层。脚本刻意做成传输无关就是为了这步不返工。

---

## 4. 工作流程（脚本侧，客户无关）

| 步骤 | 做什么 | 日志要点 |
|------|--------|---------|
| 1 | 输入验证（后缀、大小、是否在 pending） | `文件格式检查: 通过` |
| 2 | **读客户档案** + 抽取规范键 | `客户档案: igl（自动识别）`、`布局命中`、`提取规范键: N 个`、未识别标签、CPMS 素材 |
| 3 | 读 ERP 模板（`assets/add_customer_quote_request.json`，59 键） | `模板字段数: 59` |
| 4 | 生成任务 ID（UUID4 hex） | `生成任务ID: ...` |
| 5 | 字段映射（规范键 + 档案规则 → ERP 字段） | 逐字段来源与值；末尾诚实统计「被赋值的字段」 |
| **5b** | **按 ERP 表单权威字典归一化 + 校验取值**（`assets/erp_form_schema.yaml`） | `[归一化]`、`[ERP字典] ERROR/WARN`、`校验通过` |
| 6 | 输出：CSV / 抽取明细 / ERP JSON / **校验报告** / 原始文件归档 | **59 键断言** |
| 7 | ★ **CPMS 由 agent 亲自撰写**（脚本之外，必做） | 脚本会打印提醒与产物路径 |
| 8 | ★ **交付前过交付闸门**（1.0.25）：`CPMS` 非空 + `CPMC_ZH` 不含 `【中文品名待定】` | `uv run python -m erp_view --no-open`（带「未完成」红徽章的就是没补完） |

日志：`logs/quote_request_<时间戳>.log`；任务目录：`customer-files/processed/<task_id>/`。

### 4.1 脚本赋值了哪些字段（诚实口径）

`map_fields()` 真正赋值的是：

1. **档案固定值**（`erp_fixed`，igl 当前 6 个）：`CPDL` `YYS` `BZ` `PXH` `KHMBDJ` `DWMC`
   —— ⚠️ 值为 `""` 的条目是**显式留空**（1.0.18）：模板自带默认值时必须靠它清掉
   （NGV 的 `PXH`/`YYS` 就是这样，否则模板默认 `1` 会悄悄留在产物里）。见 2.7
2. **规范键推导**：`CPLB`（format 查表）、`CPMC`（CPLB+系列名）、`TZS`、
   `CD` `KD` `GD`（尺寸/厚度；档案可 `rules.size.zero_tops` 让套装写 0/0/0）、
   `Drop_SJFX`（书脊方向）、`JHRQ` `WCRQ`（档案可 `mode: empty` 不落库）、
   `KHDH` / `KHDDH`（两组单号，见子技能 `customer-quote-ngv` 第 5 节）、`CPSX`、`FSCMC`
3. **数量档位**（0~10 个）：`CPSL1`~`CPSLn`
4. **档案映射**：`ZDFS`（`rules.binding.enabled: true` 时，igl 已开启）

其余字段沿用模板默认值（见 4.3）。跑完的日志会打印真实清单，例如 igl 样本：

```
被赋值的字段（25 个）: CPDL YYS BZ PXH KHMBDJ DWMC CPLB CPMC TZS
                       CPSL1 CPSL2 CPSL3 CPSL4 CPSL5 CPSL6 CPSL7
                       CD KD GD Drop_SJFX ZDFS JHRQ WCRQ KHDH CPSX
```

> ⚠️ 字段「出现在 JSON 里」≠「被逻辑处理过」。这是最容易误判的点。

### 4.2 关键字段的取值来源（规范键口径）

| ERP 字段 | 来源 |
|----------|------|
| `CPLB` | 档案 `cplb.map`（客户关键词正则 → 产品类别树节点编号）→ 命中即用；未命中退回 `product_categories.py` 查表（旧路径）→ 最后用 `product_category.fallback`（默认 `32.00;其他`）。结果一律校验是否在 `assets/cplb_tree.yaml` 里（见 4.7） |
| `CPMC` | 档案 `cpmc.template` 拼装（**英文表达**），占位符 `{salesperson} {cn_name} {cplb} {cplb_name} {product_name} {series_name} {fsc} {our_ref}`；默认模板 `{cplb}-{series_name}`（igl 口径）。NGV 用 `{salesperson}/Aldi {product_name}({fsc})`；**中文表达**由 `cpmc.zh_template` 渲染后落内部字段 `CPMC_ZH`（1.0.22），中文品名要人工拟 → 脚本给占位符 `【中文品名待定】` 并标 `cpmc.needs_agent: true`，**必须 agent 补全** |
| `TZS` | `versions` + 档案 `versions.title_pattern`（如 `12 titles` → 12）；没有则退回 `number_of_titles` |
| `YYS` | 档案 `versions.language_pattern` 从 `versions` 取（如 `5 languages` → 5）。三种口径必须分清（业务 2026-09-24）：`empty_when_single: true` 时**语言数 1 留空**；提取不到语言数时按 `versions.when_missing` **落 0**（NGV，0 = 待业务核对 —— 邮件可能后补而附件未更新）；两个开关都没声明才保留档案固定值 + WARN |
| `CPSL1`~`CPSL10` | 按档案 `quantity.mode`：`tiers`（igl，一排档位）/ `single`（NGV，一个总数 → 只填 `CPSL1`）。见 4.4 |
| `CD`/`KD`/`GD` | 规范键 `size`，按档案 `size.order`（igl 是 H x W x D → CD/KD/GD）；尺寸里没给 D 时按页数+纸张算厚度 —— 但档案 `rules.thickness.enabled: false` 可关掉（NGV 的 ERP 里 `GD` 恒为 0）。**套装另说**：档案 `size.zero_tops: [套装]` 时 CD/KD/GD 一律 0/0/0，附件里的盒内单件尺寸不采用（业务决定 2026-09-24） |
| `GD`（计算） | `(extent_pp / 2) × 内页厚度 + extent_cover × 封面厚度` |
| `Drop_SJFX` | 档案 `rules.spine.mode`：`cd_vs_kd`（`CD > KD` → `长书脊`，否则 `短书脊`）/ `binding_side`（装订边 `N mm side` ≥ 较长边 → 长书脊；**识别不到装订边/装订方向 → 留空（JSON null）+ WARN**，业务 2026-10-20 拍板：`spine.default: ""`，不猜长/短书脊；`spine.skip_tops` 里的类别顶层直接留空，NGV 的套装就是这种）。⚠️ **装订方向原文（`at the top`）只进 CPMS，不许拿来反推长/短书脊** —— 顶/底/左/右是装订边位置，和 `Drop_SJFX` 不是同一维度。合法枚举见 4.6：`短书脊`/`长书脊`/`四边裁` |
| `DWMC` | 档案 `rules.unit`：按 `CPLB` 的**顶层名**定单位（NGV：书本 `本`、套装 `套`） |
| `FSCMC` | 规范键 `fsc`（由 `rules.scan` 从纸张行扫出，如 `230 gsm art card C1S (FSC Mix)`）→ 按 ERP 枚举归一化。扫描条目带 `unique: true` 时**不唯一就留空**（多材质混用 FSC 时该填哪个推不出来，业务 2026-09-24 拍板），原因记进 `.extracted.json` 的 `scan_notes` |
| `JHRQ` | 档案 `dates.JHRQ.from` 指定的规范键（igl 默认 `pub_month`）→ `YYYY-MM-DD`；档案 `dates.JHRQ.mode: empty` 则不落库（NGV 口径，2026-09-24） |
| `WCRQ` | 当前日期 + `dates.WCRQ.days` 天 → `YYYY-MM-DD`；档案 `dates.WCRQ.mode: empty` 则不落库（NGV 口径） |
| `KHDH`（客户单号） | 只来自**邮件标题/正文**（档案 `email.khdh`）。**不许拿 `our_ref`（PR-xxxxx）冒充** —— 那是我们的询价编号；也**不许**把附件里的香港单号写进来（2026-09-24 修正；NGV 的双号分流见子技能 `customer-quote-ngv` 第 5 节） |
| `KHDDH`（香港单号） | 附件文件名/表内的「香港单号 CU…」（规范键 `khddh`）或邮件 → `email.khddh`。4 份 NGV 导出逐条核对通过 |
| `ZDFS` | 档案 `binding.map` 匹配 `binding.match_keys`（可指定多个规范键一起拼，如 NGV 要 `binding` + `binding_instructions` 两行才够）；**命中不到用 `binding.default`**，两者都必须落在 ERP 合法枚举内（见 4.6） |
| `CPSX` | 档案 `rules.cpsx` 推导：年龄段 ≥3/<3 岁 × 有无安全检查 → 四个中文枚举之一；推不出（缺年龄）就留空 + WARN（见 4.7） |
| `CPMS` | ★ **脚本一律留空**，agent 亲自写（见第 5 节） |

### 4.3 ERP 字段总览（59 字段 · v2 schema）

> **权威来源：** `assets/add_customer_quote_request.json` —— 59 个键，键顺序 = ERP 导入顺序。
> 输出必须与它**字段集合一致、键顺序一致**，不允许增删（脚本里有硬断言）。

**图例**

| 标记 | 含义 |
|------|------|
| ✅ | 脚本赋值（脚本侧逻辑固定，客户差异走档案） |
| 🔒 | 固定值/开关，脚本不参与映射（沿用模板默认值） |
| ⚠️ | 脚本未实现 → 输出即模板默认值（需人工或后续实现） |
| 👤 | **agent 手工**：脚本一律不处理，必须亲自完成（当前只有 `CPMS`） |

| # | 字段 | 中文名 | 数据来源 | 模板默认值 | 状态 |
|---|------|--------|----------|-----------|------|
| 1 | `BDBH` | 表单编号 | ERP 自动带出 | `""` | ⚠️ |
| 2 | `YWY` | 业务员 | **脚本固定 `KM`**（生成方标识） | `""` | ✅ |
| 3 | `KHZL` | 客户资料 | ERP 自动带出 | `""` | ⚠️ |
| 4 | `GDY` | 跟单员 | ERP 自动带出 | `""` | ⚠️ |
| 5 | `Remark` | 报价备注 | ERP 自动带出 | `""` | ⚠️ |
| 6 | `CPMX` | 产品明细 | ERP 自动带出 | `""` | ⚠️ |
| 7 | `CPDL` | 产品大类 | 档案 `erp_fixed` | `"书版"` | ✅ |
| 8 | `CPLB` | 产品类别 | `format` 查表 | `""` | ✅ |
| 9 | `Drop_SJFX` | 书脊方向 | `CD` vs `KD` | `""` | ✅ |
| 10 | `ZDFS` | 装订方式 | `binding`（档案映射，合法值见 4.6） | `""` | ✅ |
| 11 | `CPMC` | 产品名称（**英文表达**） | 档案 `cpmc.template` 拼装；中文表达见内部字段 `CPMC_ZH` | `""` | ✅ |
| 12 | `TZS` | 共有款数 | `number_of_titles` | `0` | ✅ |
| 13 | `YYS` | 每款语言数 | `versions` 提取；单语言留空 / 提取不到落 0（档案 `versions.*`） | `1` | ✅ |
| 14 | `DWMC` | 单位 | 档案 `erp_fixed`（`本`，合法值见 4.6） | `""` | ✅ |
| 15~24 | `CPSL1`~`CPSL10` | 产品数量 ①~⑩ | 数量档位 | `0` | ✅ |
| 25 | `BZ` | 币种 | 档案 `erp_fixed`（业务 2026-09-24：**我们填「美元」**，导出留空由业务核对时补） | `"美元"` | 🔒 |
| 26 | `KHMBDJ` | 客户目标价-金额 | 档案 `erp_fixed` | `0` | 🔒 |
| 27 | `CD` | 规格-长 (mm) | `size` 第 1 段 | `0` | ✅ |
| 28 | `KD` | 规格-宽 (mm) | `size` 第 2 段 | `0` | ✅ |
| 29 | `GD` | 规格-高/厚 (mm) | `size` 第 3 段 / 计算 | `0` | ✅ |
| 30 | `BZFS` | 包装方式 | 客户要求 | `""` | ⚠️ |
| 31 | `TBLX` | 台板类型 | 客户要求 | `""` | ⚠️ |
| 32 | `SHFS` | 送货方式 | 客户要求 | `""` | ⚠️ |
| 33 | `KHDH` | 客户单号 | **只**来自邮件标题/正文（档案 `email.khdh`）；附件里的香港单号**不许**写这里 | `""` | ✅ |
| 34 | `JHRQ` | 交货日期 | 档案 `dates.JHRQ`（`from: 规范键` 或 `mode: empty` 不落库） | `""` | ✅ |
| 35 | `FSCMC` | FSC 类别 | 规范键 `fsc`（`rules.scan`，可带 `unique: true` → **不唯一就留空**，业务 2026-09-24 拍板） | `""` | ✅ |
| 36 | `OLD_KHDDH` | 返单香港单号 | 返单时填 | `""` | ⚠️ |
| 37 | `SFYPJ` | 是否有配件（**string**） | 推不出就留空（业务 2026-09-24 拍板；写死「否」是错的） | `""` | 🔒 |
| 38 | `Remark1` | 配件描述 | 随 `SFYPJ`，推不出就留空 | `""` | 🔒 |
| 39 | `PXH` | 排序号 | 档案 `erp_fixed`（业务 2026-09-24：**留空**；NGV 用 `PXH: ""` 显式清掉模板默认 1） | `1` | 🔒 |
| 40 | `CPSX` | 产品属性 | 年龄段 + 安全检查（档案 `rules.cpsx` 推导） | `""` | ✅ |
| 41 | `CPLX` | 产品类型 | 历史订单比对 | `""` | ⚠️ |
| 42 | `WCRQ` | 预计下单日期 | 档案 `dates.WCRQ`（`today_plus_days` 或 `mode: empty`） | `""` | ✅ |
| 43 | `KHDDH` | 香港单号 | 附件「香港单号 CU…」（规范键 `khddh`）或邮件 → 档案 `email.khddh` | `""` | ✅ |
| 44 | `chkXBJF` | 不需报价 | 开关 | `false` | 🔒 |
| 45 | `ECPMC` | 产品英文名 | 档案 `rules.ecpmc.source_keys`（按顺序取第一个非空规范键）：NGV = `product_name`（客户原文 `Title:` 后面的英文品名，**照抄**）→ `series_name`。**不翻译、不拼类别、不带 FSC**。没声明 `rules.ecpmc` 的客户留空（igl 现状） | `""` | ✅ |
| 46 | `chkZDBJF` | 有排交期 | 开关 | `false` | 🔒 |
| 47 | `ZXLB` | 纸箱类别 | 客户要求 | `""` | ⚠️ |
| 48 | `ZBLX` | 纸板类型 | 客户要求（业务 2026-09-24 定：**NGV 留空**；同日给出完整下拉枚举，导出里的 `B3B普通芯` 不在枚举内） | `""` | ⚠️ |
| 49 | `ZXFS` | 装箱方式 | 客户要求 | `""` | ⚠️ |
| 50 | `chkBSYTB` | 不使用台板 | 开关 | `false` | 🔒 |
| 51 | `chkBSYJZX` | 不使用集装箱 | 开关 | `false` | 🔒 |
| 52 | `DKMC` | 底卡名称 | 客户要求 | `""` | ⚠️ |
| 53 | `YSLX` | 运输类型 | 默认留空；长期客户的实际口径写在档案「客户偏好表」（业务 2026-09-24） | `""` | 🔒 |
| 54 | `SHDQ` | 送货地区 | 客户要求 | `""` | ⚠️ |
| 55 | `KHDQ` | 客户地区 | 客户资料 | `""` | ⚠️ |
| 56 | `CPMS` | 产品描述 | **agent 亲自**（脚本不生成） | `""` | 👤 |
| 57 | `BGMS` | 补充描述 | — | `""` | ⚠️ |
| 58 | `Remark4` | 检测项目 | — | `""` | ⚠️ |
| 59 | `Remark3` | 修改记录 | 留空（我们产出的就是第一版报价，不存在修改记录，业务 2026-09-24） | `""` | 🔒 |

> **注意：** v2 没有 `Remark2`。备注类是 `Remark` / `Remark1` / `Remark3` / `Remark4`。
> `BDBH`/`KHZL`/`GDY`/`Remark`/`CPMX` 业务已确认**由 ERP 自动带出，留空即正确**。
> **`YWY` 例外：必须填 `KM`** —— 业务 2026-10-20：「这是我们区分谁生成的单」，
> `KM` 是「这份报价由脚本/agent 产出」的标记（ERP 里人写的单会带业务员本人姓名，
> 如「陈纯」）。所以别把它当「ERP 自动带出」清空，也别改成业务员姓名。
> `CPDL` 是「书**版**」不是「书**板**」。

### 4.4 数量 `CPSL1`~`CPSL10`

**业务确认（2026-09-23）：** 默认 `0`，取值由**客户订单的数量**决定。
两种客户口径由档案 `rules.quantity.mode` 决定 —— 这是本仓库里最容易搞错的一处：

| `quantity.mode` | 客户怎么写 | 落到哪 | 例子 |
|---|---|---|---|
| `tiers`（默认，igl） | 一行**档位**，按从左到右依次填 | `CPSL1`…`CPSLn`，其余留 `0` | `Total Costs \| 3k \| 5k \| 7.5k \| 10k` → `CPSL1=3000 … CPSL4=10000` |
| `single`（NGV） | **一个总数** | 只填 `CPSL1`，`CPSL2~10` 留 `0` | `512580cps` → `CPSL1=512580`；`4 x 17.500` → `CPSL1=70000` |

> 两种口径的具体写法与例子见子技能（`customer-quote-igl` 用 `tiers`、
> `customer-quote-ngv` 用 `single`）。新增客户先判它属于哪一种。

`single` 的解析要点（`main.py: parse_single_quantity`）：

- 欧式数字：`4 x 17.500` 是 4 × **17500**（点作千分位），`7,5` 才是 7.5 ——
  靠位数判定（点后正好 3 位且形如 `1.234.567` → 千分位）。
- 后缀（`cps` / `pcs`）忽略。
- 数量为 `0` 或解析不出 → 保持默认 `0` + WARN。**这不是 bug**：
  NGV 常在报价阶段留 `0`，真实数量在邮件里（实测 CU26051601-01 附件写 `0`，
  ERP 里是 `193820`）。

`tiers` 的识别规则刻意做严（**宁可漏判，不可误判**），写法由档案 `rules.quantity_tiers` 控制：

| 判定 | 条件 |
|------|------|
| ① 带 `k` 后缀 | 同一行 ≥2 个形如 `3k` / `7.5k` 的单元格，**列方向连续**，且档位前有标签 |
| ② 纯数字 | 同一行 ≥2 个 3~7 位整数，同样要求连续；**额外要求**标签命中 `plain_label_hint`（大小写不敏感） |
| 去重 | 多组档位行取**最长**的一组，值去重保持首次顺序 |
| 上限 | 超 `max_tiers`（默认 10）截断并告警 |
| 兜底 | 识别不到 → 全部保持 `0`，日志 WARN |

> ① 的「连续 + 有标签 + ≥2 个」三重条件就是防误判：单独冒出一个 `3k`、
> 或尺寸表里的两列数字（`215 / 558`）都不会被当成档位。
> 写成 `3,000`、`3 K` 分格、或每档单独一行都识别不到 → 会静默全 0（日志有 WARN）。
> **改客户文件写法或新增格式时，先补 `rules.quantity_tiers` 的开关/规则。**

> ⚠️ **档位顺序口径**：现为「客户表列顺序 → `CPSL1..n`」。
> 若业务实际是按「数量区间只填一档」，规则需重写。

### 4.5 日期与厚度计算

- **日期**：`JHRQ` / `WCRQ` 都是 **string `YYYY-MM-DD`**。支持
  `2027-05-01` / `2027/05/01` / `2027-05` / `May-27`（`May-27` → `2027-05-01`，已过期则下一年）。
  实现：`main.py: parse_date_to_datestr()` → 先调 `string2timestamp.py` 取毫秒再换算，失败回退正则。
- **纸张厚度**：`select_page_thickness.py --pg <纸张类型> --faw <裸数字>`。
  ⚠️ **克重不能带单位**（表里 `faw/g` 列存 `"250"` 这样的裸数字，传 `250g` 查不到）；
  表里没有的克重按**同类型最近克重**取值（并列取较大者，宁厚勿薄），提示写 stderr。
- **纸张类型**由档案 `rules.materials` 映射（`woodfree` → 书纸、`C1S artboard` → 光粉纸…），
  未命中用档案默认值并 WARN。

**报价截止日期（`offer_deadline`）—— 过期必须提醒业务**（1.0.21 新增）

`offer deadline / Angebotsfrist` 是**有值**的标签（不是栏目标题），抽到规范键 `offer_deadline` 后：

- 它**不是 ERP 字段**（模板里没有报价截止日期）→ 落 `validation.json` 的 **`reminders`** 段，
  并在跑完的 stdout 末尾打一个醒目块（业务 2026-09-24：「超过了报价截止日期，你记得提醒」）。
- 判据 = 截止日期 vs **运行基准日期**：`uv run ./scripts/main.py <文件> --today 2026-09-24`
  （默认系统当天）。**别把「今天」写进代码** —— 否则「今天没过期、明天过期了」，
  测试与补跑都不可复现。`rules.deadline.warn_when_expired`（默认 `true`）可关。
- 该提醒**不计入** error/warn 计数（它不是取值缺陷），但 `reminders` 为空/非空是可断言的。
  同一内容还会落**内部字段 `Reminder`**（见 4.8）—— 产物里就有，不用翻日志。

**报价截止日期检查 → 三条通道（同一来源，不会漂）**：内部字段 `Reminder` /
`validation.json` 的 `reminders`（结构化）/ stdout 末尾醒目块。

### 4.6 ERP 表单权威字典与取值校验（1.0.14 新增）

> **字典：** `assets/erp_form_schema.yaml`（59 字段 + 35 个仅表单字段）
> **来源：** 客户真实 ERP「报价需求单 / 产品资料」表单 HTML（2026-09-23 森西提供）。
> **执行：** `main.py` 步骤 5b，`scripts/erp_schema.py` 实现。

为什么必须有这一层：**ERP 的下拉框只认固定枚举**。客户文件的写法五花八门
（`Paperback` / `无线胶装` / `骑马订` / `1` / `CPSX001`…），填错的值导入时会被静默
丢弃或直接报错。实测**历史单据 `0e39e2e2`（2026-05-25 手填）就有 3 个真错误**：
`ZDFS="无线胶装"`（合法值是 `胶装/PAD书`）、`JHRQ/WCRQ` 存的是毫秒时间戳（业务口径是
`YYYY-MM-DD`）、`SFYPJ` 存布尔 `false`（ERP 是 `是`/`否`）。字典就是为拦这类问题建的。

**步骤 5b 做三件事**

1. **归一化**：`normalize_value()` —— 去首尾空白、大小写、代码↔label、同义词。
   例：`无线胶装` → `胶装/PAD书`；`1` → `长书脊`（`Drop_SJFX` 表单存数字代码）；
   `02` → `美元`；`CPSX001` → `三岁以上`；`快线BB书␣␣␣` → `快线BB书`。
2. **校验**：`validate_result()` —— 三档检查，产出 `.validation.json`：
   - `error`：取值不在 ERP 合法枚举里 / 类型不对（整数、日期、布尔）/ 业务必填字段为空
   - `warn`：建议填写字段为空 / 字段不在字典里 / **档案声明的口径例外**（见下）
3. **报警**：有 `error` 时终端打出醒目块（`!! ERP 取值校验：N 个 error !!`）+ 日志，
   提示「导入前必须处理」。
4. **提醒**：`reminders` 段 —— 给业务的提醒（不是取值缺陷，**不计入 error/warn**）。
   目前是「报价截止日期已过期」（见 4.5）；同一内容也会落内部字段 `Reminder`（见 4.8）。

**两种「按档案放宽」，都必须在档案里显式声明（不许全局放宽）**

| 段落 | 用途 | 例子 |
|------|------|------|
| `rules.required_empty` | 该客户**本来就该为空**的字段 → 不报必填 error | NGV：`KHDH`（报价阶段客户还没给单号）、`KHDDH`（香港单号，扫不到就空）、`Drop_SJFX`（套装没有书脊方向） |
| `rules.enum_exceptions` | 业务拍板用、但**不在 ERP 下拉枚举里**的**字符串**取值 → 报 WARN（带业务原因）而非 error | 目前**没有客户在用**（NGV 的 `CPSX=无` 已改走 `rules.null_values` → `null`，见 4.9）；机制保留备用 |

> ⚠️ **先看 `rules.null_values` 再考虑 `enum_exceptions`**：业务说「无」时，正确做法是
> 落 JSON `null`（= 空值，天然不触发枚举校验），而不是往枚举里塞一个非法字符串。
> `enum_exceptions` 只留给「业务坚持要用枚举外的字符串值」这种真例外。
> 声明的豁免**只认 (字段, 取值) 这一对**：乱填的取值继续报 error；
> 不带 `--customer` 按字典硬校验时也照样报 error（可见性不被吞掉）。
> `main.py` 与 `erp_schema.py --check --customer <id>` 走同一份豁免。

**产物**：`customer-files/processed/<task_id>/<原名>.validation.json`

```json
{ "schema": "assets/erp_form_schema.yaml", "customer": "igl",
  "errors": [], "warnings": [], "reminders": [], "summary": {"error": 0, "warn": 0} }
```

**常用命令**

```bash
uv run ./scripts/erp_schema.py                      # 字典概览（哪些字段有枚举）
uv run ./scripts/erp_schema.py --field ZDFS         # 单字段定义 + 别名表
uv run ./scripts/erp_schema.py --values Drop_SJFX   # 只列合法值
uv run ./scripts/erp_schema.py --check <ERP.json> --customer ngv   # 按档案豁免校验（推荐）
uv run ./scripts/erp_schema.py --check <ERP.json>   # 按字典硬校验（会报该客户「本来就空」的字段）
```

**存储口径：统一中文 label（业务确认 2026-09-23）**

所有下拉字段一律输出**中文显示文本**，不输出内部代码。字典里每个字段的 `store` 都是
`label`（有测试守着，防止有人改回 `code`）。`CPSX001` / `02` / `008` 这类代码只作溯源，
归一化时能被识别（客户写代码也认），但**落进 JSON 的永远是 `三岁以上` / `美元` / `普通纸箱`**。
历史单据佐证一致：`0e39e2e2` 里 `BZ="美元"`（不是 `02`）。

**几条只有从表单才能知道的坑**

| 字段 | 坑 |
|------|-----|
| `KHDH` | ERP 表单里这个控件的 name 是 **`txtKHCPMC`**，隐藏区还有一个 `lblKHCPMC`=「客户产品名称」—— ERP 侧命名混乱。**业务决策（2026-09-23）：保留 `KHDH`，不跟着错**；ERP 导入接口由 ERP 侧改，输出侧**不得出现 `KHCPMC`**（与 `KHDDH` 香港单号只差一个字母，会误导） |
| `Drop_SJFX` | 表单里选项值是数字（`0`/`1`/`2`），但按统一口径输出中文（`短书脊`/`长书脊`/`四边裁`） |
| `ZDFS` | `快线BB书   `（三个尾随空格）—— 不改会永远匹配不上 |
| `BZ` | 历史单据存 label（`美元`），不是 code（`02`）→ 统一口径下一致 |
| `CPLB` | 树形下拉，值是 `<编号>;<名称>`（如 `23.01;无线胶装书`）→ 校验**编号是否为树节点**；只写到顶层（`23.00`）会给 WARN，因为太粗会影响生产/计价。名称会被树里的规范名覆盖（防「编号对、名字错」）。★ 业务 2026-09-24 拍板：**我们输出「编号;名称」**（原话：编号让用户更直观地看到任务的标识）；真实 ERP 导出存的是纯名称，纯名称输入仍判 WARN 提醒补编号 |
| `CPSX` 产品属性 | 选项 `三岁以上`/`三岁以下(要寄第三方测试)` 就是**年龄段+安全检查的落库位**。业务确认（2026-09-23）**要落库**，规则见 4.7；但**落库不替代 CPMS** |
| `SFYPJ` | ERP 是字符串枚举 `是`/`否`；历史单据写成布尔 `false`（错） |
| `SHDQ`/`CPDL` | 联动下拉，静态 HTML 里取不到全量枚举（`SHDQ` 目前为空，需业务导出） |

### 4.7 产品类别树（`CPLB`）与 `CPSX` 推导（1.0.15 新增）

#### 4.7.1 产品类别：权威树 + 重设计导航树

**数据源**：`assets/product_categories.xlsx`（ERP 产品类别表导出，88 行）
→ 生成 `assets/cplb_tree.yaml`（生成器 `scripts/cplb_tree.py --rebuild`，可确定性重放）。

树 = 11 个顶层节点（`22` 精装书 … `32` 其他）+ 77 个可选子类。**每个子类都带 ERP 里的业务定义**
（`REMARK` 列），那就是「什么时候该选它」的判据，例如：

```
23.01 无线胶装书   无需锁线的胶装书，如内文是书纸外的其他纸不建议做无线胶装书，容易掉页
27.06 精装揭页BB书 封面有灰板，且内文有揭页的BB书
```

**为什么要重设计**（业务要求 2026-09-23：「重新设计树，让产品类别更加方便选取」）：

1. 原树按编号平铺，**27 BB书 一个父节点挂 23 个子类**、26 线圈书挂 14 个；编号顺序与工艺属性
   无关（27.01 普通BB书 → 27.02 精装BB书 → 27.03 普通砌图BB书…），人工扫一遍很慢。
2. 子类名大量用内部行话（蛇圈 / YO / BB书 / 揭页 / 砌图 / 带机封），非老手看不懂。
3. **编号一个都没改** —— 只在上面叠加「导航层」，ERP 兼容性零风险。

重设计后的结构（`cplb_tree.yaml` 的 `navigation` 段）：

- 顶层按**成品形态**分 4 类：A 书籍类（22~28）· B 组合/包装类（29 套装、30 盒类）·
  C 单张/卡牌类（31）· D 其他（32）。
- 组内按**业务决策顺序**排列：装订 → 封面灰板（有=精装变体 / 无=普通变体）→ 内文工艺。
- 同属性的兄弟节点被排到一起（原树里被打散），例如 BB书拆成
  「基础 / 揭页 / 砌图 / 拉卡机关 / POP UP / 音乐发声 / 磁胶 / 毛毡布偶 / 冲形状」9 个子分组。
- 每个节点带结构化属性（`cover_board` 有/无、`features` 工艺特征）与**英文/行话别名**
  （`lift the flap` / `board book` / `蛇圈` / `YO`…），方便按客户文件里的词反查。
- `quick_pick`：常用速选 10 项（⚠️ 按业务经验排序，**不是** ERP 真实频次数据）。
- `paths`：6 条决策路径 —— 「是不是成册的书」→「装订方式」→「封面有没有灰板」→
  「内文工艺」→「是否冲形状/带机封」→「是不是 PAD 书」，逐层收敛到具体子类。

**选类别怎么用**

```bash
uv run ./scripts/cplb_tree.py --list                  # 按导航树列全部类别（含业务定义）
uv run ./scripts/cplb_tree.py --group BB书            # 看某一组（含灰板/特征）
uv run ./scripts/cplb_tree.py --node 23.01            # 单个节点
uv run ./scripts/cplb_tree.py --search "lift the flap"   # 关键词（中文/英文/行话）
uv run ./scripts/cplb_tree.py --suggest "hardcover board book with lift the flap"
uv run ./scripts/cplb_tree.py --validate              # 自检 + 与源表逐节点核对
uv run ./scripts/cplb_tree.py --html assets/cplb_tree.html   # 生成可视化选取器（可搜索、点击复制）
```

> `assets/cplb_tree.html` 是人用的选取器：搜索框 + 分组折叠 + 点一下复制
> `编号;名称`，右侧常显常用速选与决策路径。业务选类别时直接开这个页面。

**脚本侧怎么定类别**：`main.py resolve_cplb()` 按 ①档案 `cplb.map`（正则关键词 → 节点编号）
→ ②旧查表 `product_categories.py`（`customer_product_categories` 列，历史路径，只有 `paperback`
一行有值）→ ③`product_category.fallback`。**任何一步的编号都必须存在于树上**，否则 WARN 并降级 ——
编号写错等于把单子导成别的类别。新增客户时在档案里写 `cplb.map` 就够，不用改代码。

#### 4.7.2 `CPSX` 产品属性：年龄段 + 安全检查落库

业务确认（2026-09-23）：这两项**要落库**，落点就是 `CPSX`。`main.py derive_cpsx()` 按档案
`rules.cpsx` 推导（阈值与取值都可被客户档案覆盖）：

| 年龄段 | 安全检查 | 输出（ERP 中文枚举） |
|--------|----------|----------------------|
| ≥ 3 岁 | 无 | `三岁以上` |
| ≥ 3 岁 | 有 | `3岁以上（要寄第三方测试）` |
| < 3 岁 | 无 | `三岁以下` |
| < 3 岁 | 有 | `3岁以下（要寄第三方测试）` |
| 缺年龄段（或只有安全检查） | — | 档案 `rules.cpsx.when_missing`：空 → **留空 + WARN**（公共默认，不猜）；非空 → 落该值（NGV 业务 2026-09-24：「CPSX 如果没有获取到就显示**无**」，再经 `rules.null_values` 翻成 **JSON `null`**，见 4.9） |

实测（igl 样本 `PUZZLE COLOUR DISNEY.xlsx`：`Target Age Group=7+`、`Safety Checks=Y`）
→ `CPSX = "3岁以上（要寄第三方测试）"`。

⚠️ **`when_missing` 的值可能不在 ERP 枚举里**（`无` 就不在）→ 必须同时在档案
`rules.enum_exceptions` 里声明，否则每一单都会报 1 个 error（见 4.6 的「口径例外」）。

> ⚠️ **落库不替代 `CPMS`**：产品描述里仍必须写清年龄段与安全要求（见第 5 节）。

---

### 4.8 内部导出字段：`ID` / `CPMC_ZH` / `Reminder` / `version`（1.0.22 起）

> **声明文件：** `assets/internal_export_fields.yaml` ｜ **加载：** `scripts/internal_fields.py`
> **业务背景（2026-09-24）：** 「导出现在缺少一份内部导出字段 `ID`，该字段标注我们的当前执行的任务，
> 方便日志维护；将 `CPMC` 修改为存英文表达，使用 `CPMC_ZH` 来标注中文表达」；
> 随后「（过期提醒）加一个字段做提醒」；再「给导出的 json 再添加一个字段 `version`，
> 标识当前配置的版本，目前设置版本为 2，这样方便我们的技能迭代」。

| 键 | 值 | 说明 |
|----|-----|------|
| `ID` | 本次任务号 | 与**产物目录名**、日志里的「任务ID」、`validation.json` 的 `task_id` 是同一个 ID |
| `CPMC_ZH` | 产品名称的中文表达 | `CPMC` 只放英文（业务口径），中文表达落这里 |
| `Reminder` | 给业务的提醒文本 | 目前是「报价截止日期已过期」；没有待办时为空字符串 |
| `version` | 当前配置的版本（**现在 = 2**） | 跑出这份产物的那份**客户档案配置**的版本；口径一变就 +1 |

**`version` 的取值来源**：客户档案顶层 `version:` —— 公共默认值写在 `customers/_defaults.yaml`（= 2），
客户档案可覆盖（deep-merge 会带上）。改口径就 +1，判别语义：

| 版本 | 含义 |
|------|------|
| （无该键） | 第 1 代口径：多客户架构之前 / 2026-09-24 之前的产物 |
| `1` | 保留值，未实际使用 |
| `2` | 2026-09-24 这批：多客户档案架构 + 内部字段 `ID`/`CPMC_ZH`/`Reminder`/`version` + 「无」→ `null` + NGV 分区表单范式 |

`CustomerProfile.validate()` 会拦非法值（必须是 ≥1 的整数）；`customer_profile.py --show <id>`
会打印「配置版本」。**页面上也显示**（见 §4.10），所以业务拿到一份产物能立刻判断是不是最新口径跑出来的。

**机制（四条硬规则）**

1. **位置**：一律追加在 59 个 ERP 键**之后**（ERP 自己的导出也是这么干的：`Remark3` 后面挂 `产品备注`）。
   59 键的位置一个不动 —— 下游按位置读的解析器不受影响。
2. **断言**：`step6` 检查「前 59 键 = 模板（集合 + 顺序）」**且**「其后 = 声明的内部字段（同序）」；
   `map_fields` 多产出任何未声明的键会当场抛错。`tests/test_pipeline.py` 有测试钉着。
3. **校验**：内部字段**不参与** ERP 字典校验（`ID`/`CPMC_ZH`/`version` 字典里确实没有）——
   否则每份产物都会报「字典里没有这个字段」。`erp_schema.py --check` 会自动跳过。
4. **开关**：`--strip-internal` → 只输出 59 键（给「ERP 导入端不收未知字段」兜底）。

```bash
# 默认：59 + 4 个内部字段（ID / CPMC_ZH / Reminder / version）
uv run ./scripts/main.py "customer-files/pending/<附件>.xlsx" -c ngv
# 纯净 59 键（导入 ERP 前若被告知「不收未知字段」）
uv run ./scripts/main.py "customer-files/pending/<附件>.xlsx" -c ngv --strip-internal
```

**加新内部字段**：在 `assets/internal_export_fields.yaml` 的 `fields:` 里加一条
（`key` + `source`），并在 `main.py` 的 `providers` 里补上该 source 的实现（一行）。
**ERP 要的字段不要往这里加** —— 那属于 59 键模板 + 字典。

---

### 4.9 「无」落 JSON `null`（`rules.null_values`，1.0.23 新增）

业务 2026-09-24：「`CPSX` 或者其他值的『无』使用 json 里的 null 填充」。

```yaml
rules:
  null_values: ["无"]        # 公共默认在 _defaults.yaml；想关掉写 []
```

- **触发条件**：某个字段的取值**整格恰好等于**声明里的词（先 trim）。长文本里的「无」
  不受影响（`客户无需提供刀模…` 原样保留），`无特殊要求` 也不转换。
- **为什么这样最对**：ERP 自己的导出里**空值就是 `null`**（权威导出的 `CPLD`/`KHDH`/`CPSX`
  都是 `None`）。所以「无」落 `null` 既符合业务语言、又天然属空值 → **不触发下拉枚举校验**。
- **实践建议**：业务说「取不到就显示无」时，档案写 `when_missing: 无` + 靠本机制翻成 `null`；
  **不要**用 `enum_exceptions` 往枚举里塞「无」这个非法字符串。
- 实现位置：`main.py map_fields()` 第 14 步（很靠后，所有字段都已赋值），日志会打出
  `业务口径 null_values=['无'] → 落 JSON null 的字段: CPSX('无'→null)`。
- ⚠️ 与「空值」的区别：**没有待办/本来就没有** → 空字符串（模板默认）；
  **业务明确说「无」** → `null`。两者在 JSON 里长得像，但来源不同，别把 `Reminder` 也写成 null。

---

### 4.10 产物查看器 `erp_view/` 包（Flask，1.0.24 新增 / 1.0.26 改 Flask）

业务 2026-09-24：「有一个旧的展示 ERP JSON 格式的 html，但是我们的导出结构发生了变化，
需要修改它；让它可以自动扫描 `customer-files/processed` 的导出 ERP 文件」，
随后：「将 `erp_viewer.py` 创建一个 `erp_view` 文件夹，然后使用 flask 重新编写」。

```bash
uv run python -m erp_view                         # 起服务 + 自动开浏览器（默认 127.0.0.1:8765）
uv run python -m erp_view --port 9000             # 换端口
uv run python -m erp_view --no-open               # 无头/远程：不开浏览器
uv run python -m erp_view --dir <其它目录>         # 改扫描根目录
uv run ./scripts/erp_viewer.py …                  # 等价（`scripts/erp_viewer.py` 已只是转发壳）
```

**包结构（三层，刻意分开）**

```
erp_view/
├── data.py               ← 唯一实现：扫描 / 路径安全 / ERP 字典 / 取值校验 / 交付闸门（**不 import Flask**）
├── app.py                ← 只管 HTTP：create_app() 工厂 + 4 个路由 + CLI main()
├── templates/index.html  ← 只管显示（零依赖原生 JS；**刻意不含 Jinja 语法**）
└── __main__.py           ← `python -m erp_view`
```

| 层 | 文件 | 职责 | 不许做什么 |
|----|------|------|-----------|
| 逻辑 | `data.py` | 扫描、路径校验、字典、`validate_result`、`delivery_gates` | 不许 import Flask（要能脱离服务端单独测） |
| HTTP | `app.py` | 路由、状态码、缓存头、把 `data` 的结果 jsonify | 不许写业务判定 |
| 显示 | `templates/index.html` | 渲染、折叠、徽章 | 不许自己判枚举合法性/交付闸门；不许出现 Jinja 变量 |

> ⚠️ **扫描根在 `app.config["PROCESSED_DIR"]`，不再是模块级全局**（1.0.26）。
> 旧版靠 `global PROCESSED_DIR` 让 `--dir` 覆盖 —— 全局既脏又不可测，两个 app 会互相污染。
> 现在 `create_app(processed_dir=…)` 显式传入，测试用 `app.test_client()` 就够，不用抢端口。

**为什么需要一个服务端**：浏览器**不允许** `file://` 页面列出本地目录。所以「自动扫描」只能是
一个本地 HTTP 服务把目录列表喂给页面。

| 接口 | 作用 |
|------|------|
| `GET /` | 页面本体（`erp_view/templates/index.html`，Jinja 渲染） |
| `GET /api/exports` | 递归扫描结果（每份产物的摘要，**最新的在前**）+ 当次运行的校验计数与客户档案 |
| `GET /api/export?path=<相对路径>` | 某一份产物的完整 JSON + **权威取值校验结果**（`validation`）+ **交付闸门**（`incomplete`） |
| `GET /api/schema` | ERP 字段字典（标签/类型/合法取值）+ 内部字段 + 分组 |

> **JSON 口径**：`PrettyJSONProvider` 定死三件事 —— `ensure_ascii=False`（中文不转义，
> `curl` 直读）、`sort_keys=False`（保持我们构造的键序）、`compact=False`（缩进 2 格）。
> 默认 provider 会转义中文 + 按字母重排键 + 输出紧凑格式，`curl` 看着很难受。

**页面能看出什么**

- **配置版本**：顶部徽章显示当前配置版本（读 `_defaults.yaml`），并和产物里的 `version` 对照 ——
  一致 / 不一致 / 产物根本没有 `version`（= 第 1 代口径）三种状态分开显示。
- **结构对照**：这份产物 vs 当前 59 键结构 —— 缺了哪些键、多出哪些键、内部字段缺哪几个。
  历史产物（40 键的手填单、59 键的中间版本）都能正常打开，缺失处明确标注而不是留白。
- **取值校验**：显示 `erp_schema.validate_result()` 的结论（见下「刻意设计 5」）——
  顶部「校验 0 error / 0 warn（本次运行报告·客户 ngv）」徽章，有问题的字段行单独标
  `校验 error` / `校验 warn` 并带出后端给的原因原文。
- **枚举参考**：折叠列出字典里的合法取值（`CPLB` 88 类、`DWMC` 65 单位），**纯参考、不做判定**。
- **空值口径**：`null` 显示成「null（空值）」，跟空字符串「（空字符串）」区分开（§4.9）。
- **`Reminder`**：非空时页面顶部弹醒目横幅。
- ★ **交付闸门**：`CPMS` 没写 / `CPMC_ZH` 还是占位符 `【中文品名待定】` → 列表上打红徽章
  **「未完成」**、详情页顶部弹 **「❗ 未完成 · 不得交付」** 横幅并逐条列出原因。
  这两处是脚本**刻意不写**的（agent 的活），所以**管道刚跑完的产物天然是未完成状态** ——
  以前这个状态只活在文档里，结果一份未完成产物在 `processed/` 里躺了半天（见 §5）。
- **已被取代**：`_superseded_*` 目录下的产物带「已被取代」标记。
- 支持 `?path=<相对路径>` 深链接，方便把某一份产物发给别人。

**排版（1.0.27 重做）与导出 PDF**

业务 2026-09-24（第七批）：「每个字段单独成一行不方便阅读，其次添加一个导出为 PDF 的按钮」

- **字段改成自适应网格**：同组内 `grid-template-columns: repeat(auto-fill, minmax(290px, 1fr))`
  —— 窗口越宽列越多；**只有长值跨整行**（`.field.wide` → `grid-column: 1/-1`）。
  宽窄由**值**决定（`fieldIsWide()`：长文本 / >40 字 / 带校验原因的跨行），不是写死字段名。
  旧排版一个字段占一整行（左 190px 标签 + 右侧值），63 个字段要滚 ~2800px，而 `TZS=1`
  这类短值把大半行宽度白扔了；现在短值 4 个一行（1720px 窗口），滚动长度约降到 **1/3**。
- **标签压成一行**：中文名 + 字段代码并列（原来上下两行），省掉一半标签高度。
- **导出 PDF**：工具栏「🖨 导出 PDF」→ `window.print()` → 浏览器「另存为 PDF」。
  **零依赖**：不引 PDF 库（那些要 CDN，页面必须离线可用；而且引库还得再抄一份排版，跟屏幕必漂）。
  PDF 版式 = 屏幕版式 + 一层 `@media print` 覆盖：
  - `@page { size: A4; margin: 12mm }`；字段固定 3 列；`break-inside: avoid` 防字段被页边切断
  - 隐藏侧栏 / 工具栏 / 原始 JSON / **字典合法取值清单**（那是查询参考，不是报价内容）
  - `pre.long` 解除 190px 高度限制 → 长文本（`CPMS`）**全量进 PDF**
  - 页眉的标题与横幅（`Reminder`、「未完成 · 不得交付」）**保留** —— 打印出来也要看得见待办
- **PDF 默认文件名**：`syncTitle()` 把 `document.title` 设成
  `ERP · 报价需求单导出查看器 · <任务号前 8 位> · <中文品名>`（非法字符换 `_`、截 120 字）——
  不然每份 PDF 都叫「ERP · 报价需求单导出查看器」。

**几处刻意的设计（改动前先读）**

1. **零外部依赖**。旧页面引 `unpkg` 的 Vue3 + `jsdelivr` 的 dayjs —— 断网/内网直接白屏。
   这是内部生产工具，改成纯原生 JS。`tests/test_viewer.py` 钉着「不许再出现外部 `<script src>`」。
2. **字段分组在 `erp_view/data.py` 的 `FIELD_GROUPS`，不在页面里**。加了新 ERP 字段却忘了归组，
   页面上会**悄悄不显示**；所以有一条测试断言「分组恰好覆盖 59 个字段各一次」，漏了就红。
   没归组的会落到「其他（未归组）」组，仍然可见。
3. **字段元数据来自 `assets/erp_form_schema.yaml`**，不另抄一份（测试比对同源）。
4. **容错**：坏 JSON 也列出来并标「文件损坏」，不让用户以为「没产出」。
5. ★ **取值校验只有一份实现，页面只负责显示**。
   页面**不许**自己拿值和字典的 `legal` 列表比字符串 —— `CPLB` 我们的口径是「编号;名称」
   （`26.01;普通蛇圈书`），而字典 `legal` 是树里 88 个**裸名称**，前端直接比会把
   **每一份产物的 `CPLB` 都误标成非法**（这个 bug 真的写出来过，2026-09-24 用 jsdom 实跑才发现）。
   `erp_schema.validate_result()` 才是权威（它按 `;` 切编号再查树）。
   服务端给 `/api/export` 带一个 `validation` 段，取值优先级：
   - `source: run_report` —— 产物同目录的 `*.validation.json`（**当次运行**的原始报告，
     含客户档案豁免，最可信）。注意报告文件名跟**输入文件**、不跟产物名，只能按目录找。
   - `source: recomputed` —— 没有报告（旧产物 / 手动导入）→ 现场重算，并**明确标注**
     「未按客户档案豁免必填」（NGV 的 `KHDH` 本来就该空，会误报）。
   - `source: unavailable` —— 校验器本身出错，如实标注，不让产物看不了。

6. ★ **交付闸门同理：服务端算一份（`erp_view/data.py: delivery_gates()`），页面只显示**。
   `/api/exports` 的每一项带 `incomplete`（空列表 = 可交付），`/api/export` 也带一份。
   页面**不许**自己判「CPMS 是不是空的」—— 占位符文案改了、或者以后又多一处 agent 手填的字段，
   前端抄的那份就会跟服务端漂。`tests/test_viewer.py` 有 9 项钉着（含「页面里不许出现第二份判定」）。
7. ★ **模板里不许出现 Jinja 语法**（1.0.26 新增的坑）。页面数据**全部**走 `/api/*` fetch，
   模板里一旦写下双花括号，Jinja 会把它当变量：轻则渲染报错、重则把一部分 JS 吃掉，
   而且报错位置很难查。有测试断言模板里不含双花括号/百分号花括号/井号花括号三种写法。
   **第一条注释就误写过一次**（描述这条规矩时把符号写出来了），当场被那条测试拦下。
8. ★ **分层不许退化**（1.0.26）：`data.py` 不许 import Flask、`scripts/erp_viewer.py` 只许是
   转发壳（有测试断言它不含 `delivery_gates`/`scan_exports` 之类的实现且不超过 40 行）。
   这次重写的风险不是「跑不起来」，而是**长回一团**。
9. ★ **导出 PDF 不许引外部库**（1.0.27）：路子是 `window.print()`，页面保持离线可用。
   测试钉的是**加载机制**（`<script src>` / `import` / `require` / CDN 域名），**不是禁词** ——
   第一版写成「不许出现 jsPDF 这些字」，结果被自己注释里提到的库名绊倒
   （跟第 7 条模板注释是同一类错误：拿词表当代理，代替不了真检查）。
10. ★ **排版宽窄由值决定，不许写死字段名**（1.0.27）：`fieldIsWide()` 按值判断（长文本/长值/
    带校验原因），所以新增字段不用回来改排版；写死 `["CPMS","BGMS"]` 那种清单迟早漏。

> ⚠️ 页面按字典（`erp_form_schema.yaml`）顺序渲染；产物按模板（`add_customer_quote_request.json`）
> 顺序落盘。两者**集合相同、顺序不同**（模板管位置、字典管语义），不要去强行对齐 ——
> `tests/test_viewer.py::test_字典与模板键集合一致` 只钉集合。

---

## 5. `CPMS` —— agent 亲自撰写（脚本绝不参与）

> ★ **这是本技能唯一「必须 agent 亲力亲为」的环节。**
> 业务确认（2026-09-23）：产品描述**无法用 Python 统一处理** —— 每个客户的报价文件结构、
> 栏目命名、信息分布都不一样。脚本只能做机械拼串，产出既非成品、又会让人误以为
> 「已经处理过了」。因此 `build_cpms_draft()` / `CPMS_SECTIONS` / `CPMS_NOISE_KEYS`
> **已全部删除**（1.0.11，不要恢复）。
>
> ★ 但**写法**从 1.0.28 起是**定死的**：`assets/cpms_spec.yaml`（机器可读）+ 
> `docs/CPMS_写作规范.md`（人读，含 3 个真实单据的逐行改写）+ `scripts/cpms_lint.py`（校验）。
> 「agent 亲自写」= 亲自判断事实，**不是亲自发明格式**。
>
> ★ **动笔前先看 `docs/CPMS_规范与样例集.md`**：格式速览（骨架/槽位/词表/硬规矩）+ **7 份可复制样例**
> （平装锁线 / 精装 / 多款 BB / YO 线圈 / 套装 / 精装发声书 / **线圈书+钻石贴纸装配**），每份都带对应的 ERP 字段、
> 都过 `cpms_lint` **0 error / 0 warn**。照最接近的那份改，比从空白开始写快且不容易错。
> 样例由 `tests/test_cpms_doc_samples.py` 直接解析该 markdown 校验 —— 文档与规范不会漂。
>
> ★ **第 7 份样例是严格模式的范本**：来自真实单据 `BJXQ260704002`，但**只写客户附件里出现过的事实**，
> 每句都能指到单元格。业务 2026-10-19 给真实单据，是让 agent 学「客户给了什么就写什么、怎么排」，
> **不是**把确认单/历史单据里的补充信息搬进 `CPMS`。
>
> ★★ **严格模式（业务 2026-10-19 拍板，默认执行）**：
> `CPMS` **只允许写客户提供的文件（附件/邮件正文）里出现过的事实**。
> 从 ERP 导出、业务确认单、历史单据、其他客户档案「补」进来的内容一律不许写 ——
> 那些属于业务侧，在 ERP 字段或邮件回复里解决。
> 判定口径一句话：**每一句都要能指到客户文件的一个位置；指不到的删掉。**
> 估值照抄区间（`50-60 stickers` 不许写成 `60`）、空栏就是空栏、
> `polybag` 不许推断成 `OPP`、`frame/foil` 不许换材料。
> 漏抄（客户写了没写进来）是缺陷；多写（客户没写却写进来）是**错误信息**，更严重。
> 转录完整性按同文档 5.1 的「九问」自检（问的是「客户给了有没有漏」，不是「怎么补全」）。
> 复盘：`references/cpms-real-sample-bjqx260704002.md`；机械自检（缺数字/单位/料号）：
> `scripts/cpms_content_audit.py --mine <我们的 ERP.json> --ref <人已确认的 .json|.txt>`。
> 写样例/加规则前先读 `references/cpms-spec-samples-and-lint-gotchas.md`：
> **「文档即测试」配方**（解析 md 代码块 → `lint_text`，含 import 技巧）+
> **linter 实操坑**（后封面 order=102 在 前封面=103 之前、数字间 `×` 会被判 CPMS051、
> `--text` 不核对概要行、「书本类」判定要带 `CPLB`/`ZDFS`）。
>
> 设计方法（先量后定）、语料实测数字、以及写这类 linter 时踩过的坑，见
> `references/cpms-writing-standard.md` —— 要改规范或加规则前先读它，别重算一遍语料。
>
> **改规则的配方与 v2 拍板细节**（5 条业务口径、「件」= 实物件数的算法、文档即测试、
> 语料度量怎么读、linter 设计坑）见 `references/cpms-v2-decisions.md`。
> 改 CPMS 规范按那里的「固定顺序」走：spec.yaml → cpms_lint.py → 两份 docs → 测试 → 语料回归。
> 语料回归用 `scripts/cpms_corpus_metrics.py`（已封装「一单一目录」的 `*/*_erp.json` 坑）。

**脚本跑完会打印：**

```
================================================================
!! CPMS 为空 —— 必须由 agent 亲力亲为补写 !!
   产物: <ERP JSON 路径>
   抽取明细: <*.extracted.json 路径>
   步骤: 通读客户原文 -> 按 docs/CPMS_写作规范.md 写 -> cpms_lint 清零
   别漏这些非 ERP 字段的附加信息（N 项）:
     Quotation Number, Carton Weight, ...
================================================================
```

### 5.1 骨架（顺序固定，可整段省略，不可乱序）

```
[0]   概要行   书籍方向：【长书脊】产品规格：【CD*KD*GD】共【N】款/件（件=套装多件，见 5.1.1）
[100] 主体部件 皮壳 → 封面 → 后封面 → 前封面 → 皮壳衬纸 → 内兜 → 内兜衬纸 → 衬纸
               → 内文 → 内文连衬纸 → 封面连内文 → 夹层 → 内文夹层 → 灰板 → 面纸
[200] 附属部件 贴纸 → 单张 → 卡片 → 砌图 → 说明书 → 内托/陈列盒 → 信封 → 吸塑
               → 自粘袋 → 发声器 → 磁胶玩具 → 配件 → 客供配件 → 布袋
[300] 工序     装订 → 装配 → 包装 → 装箱 → 箱唛
[400] 报价备注 客户目标价 → 请拆分 → 请选配 → 此单半返 → 备注
```

两条原则：**部件自外向内、自前向后**；**工序按生产顺序，商务要求一律最后**。
`order` 相同的两段 = 同级，先后都合法（如 `陈列盒` 与 `内托`）。

**概要行是派生数据，不许手写漂移** —— 它必须等于 `Drop_SJFX` / `CD*KD*GD` / `TZS` / `YYS`
（实测 16772 份历史单据 100% 一致，说明这行本来就该算出来）。

#### 5.1.1 计数词：书本写「款」，套装多件写「件」

业务 2026-10-19 拍板。判据与取值：

| 产品形态 | 计数词 | N |
|---|---|---|
| 书本 / 多款（`CPLB` 顶层 22~28，或 `ZDFS` ≠ 套装） | **款** | `TZS`（≤1 省略） |
| 套装 / 盒类（`CPLB` 顶层 29~30，或 `ZDFS` = 套装） | **件** | **实物件数**：组成声明 `1套=A+B+C` 里每项开头数量之和，没写数量算 1 件（`陈列盒+4款卡片+信封+自粘袋` = 7 件）；没有声明就不写 |

- 套装实物 **≥2 件**时**必须**写「共【N】件」，漏写或写错都是 `CPMS011`；N 是实物件数（各项数量求和），不是「组成里有几项」；
- 书本类写「件」、套装写「款」同样报 `CPMS011`；
- 组成声明要**按实物写**：`4款卡片` = 每套 4 张；一款多张时直接写总数（`36张卡片`），不要写「4款×9张」—— 件数是逐项数量求和，不做乘法。
- 历史 18476 份里「共【N】件」出现 **0 次** —— 这是**新约定**，不是从旧单学来的。

### 5.1.2 装订方向：写了就照抄，没写就不写方向词（书脊方向留空）

业务 2026-10-20 拍板（实单 `CU26070802`）：

| 客户文件里的情况 | CPMS「装订」段怎么写 | ERP `Drop_SJFX` |
|---|---|---|
| 写了方向（`at the top` / `at the head` / `at the side` / `装订在顶部`） | 照抄方向：`装订：胶装/PAD书，装订在头部，…` | 有 `N mm side` → 与较长边比；**没有 → 留空（null）** |
| 没写方向 | `装订：胶装/PAD书，…`（**不写方向词**，也不写「书脊方向」） | 同上：认不出来就 **null** |

- **认不出来就空着**（业务原话：「识别不到则设置为 书脊方向为 Null」）——
  不许落「长书脊」当默认值，那是替业务拍板；也不许由「装订在顶部」反推「短书脊」。
- **方向和书脊是两回事**：`at the top` 是**装订边位置**，`Drop_SJFX` 是**书脊在长边/短边**。
- `Drop_SJFX` 为空时，概要行照规范省略「书籍方向：【…】」，直接从「产品规格：」开始；
  该字段在 NGV 档案的 `rules.required_empty` 里（不算缺陷）。
- 脚本用 `rules.scan: binding_direction` 把方向原文扫进 `.extracted.json`（只给 CPMS 用，不映射 ERP）。

### 5.2 行内语法：一行一个部件，槽位固定

```
部件：{数量}，{材料}，{尺寸}，{印色+表面处理}，{结构工艺}
封面：300g单粉(FSC)，4C+哑胶+局部UV(30%)/0C，圆角
内文：5张，222*280mm，2层300g单粉(FSC)对裱，4C/4C，共33个揭页
```

- 克重 `300g`（不写 gsm/克）；灰板写 g（`1500g灰板`，写 mm 报 `CPMS054`）；厚度 `2.5mm`；尺寸 `228.6*254mm`（不写 x/X/×，单位只写一次）
- 印色 `{n}C(+工艺)*/{m}C`：`4C/4C`、`4C+哑胶+局部UV(30%)/0C`、`Pantone 806C/0C`、不印写 `0C/0C`
- 表面处理书写顺序：`光油 → 哑油 → 光胶 → 哑胶 → UV → 哑UV → 局部UV → 烫金 → 击凸 → 压纹`
- 词表外的东西：`- 第一张内文右边：…`（三级子项，`- ` 开头豁免词表）或写进 `备注：`

### 5.3 六条硬规矩（违规即 error）

1. **只写当前生效的状态** —— 变更过程不进 CPMS（49.6% 的历史单据混了内部讨论块）
2. 不写 `=====` 分隔线、不写 `【姓名   日期】产品补充描述`（变更点折成 `备注` 一行）
3. 不写空标签（只有「封面」没有值的，历史上有 5289 处）
4. 不用同义标签（`内页→内文`、`装订方式→装订`、`箱唛贴→箱唛`…共 3176 种写法待收敛）
5. 请拆分/请选配/客户目标价放最后
6. 一行只写一个部件

必填段：全部要 `装箱`；书本类还要 `封面`（或`皮壳`）+ `内文` + `装订`。
**只有必填段允许写「待确认」**（`装箱：待确认（原单未给装箱要求）`），其余推不出来就整行不写。

### 5.4 写完必须过校验（交付闸门的一部分）

```bash
uv run ./scripts/cpms_lint.py <产物 ERP.json>            # 退出码 = error 数，必须 0
uv run ./scripts/cpms_lint.py <产物 ERP.json> --fix      # 先机械规范化（CR/单位/冒号/同义词）
uv run ./scripts/cpms_lint.py --vocab                    # 打印骨架与闭集词表
uv run python -m erp_view --no-open                      # 「未完成」红徽章（CPMS 空 / 占位符）
```

`--fix` 做机械修复：版式 / 单位 / 同义词 / 尺寸分隔符，加上 **部件行整槽补印色 C**
（`4/0c → 4C/0C`、`4c+光油/0c → 4C+光油/0C`）。补 C 的边界（业务 2026-10-19 拍板）：

- **只碰部件行**（order 100~299），`备注` / `请拆分` 段不动；
- **只碰整槽**（值按 `，` 切槽），`书脊1/2处烫金` 这类比例不会被改成 `1C/2C`；
- **灰板 mm → g 不自动换**（属材料语义），只报 `CPMS054`，人按 `600g=1mm` 表换算。

18476 份历史数据实测：中位 5 个 error、0 份合规；只做 `--fix` 后中位 3 个、13.7% 合规 ——
剩下的必须靠改写法，不是改格式。

> **`CPMS` 为空的 JSON 视为未完成，不得交付。**
> v2 已删除 `has_safety_checks` / `target_age_group` 两个独立字段 ——
> 「安全检查」「目标年龄段」**只在 CPMS 文本里体现**，是唯一载体。
> CPMS 里必须写清年龄段与安全要求（`CPSX` 落库不替代它）。

> ★ **未完成产物的事故记录（2026-09-24）**：拿语料库样本试跑管道（`main.py` 直接喂
> `customer-files/samples/ngv/…`，没把归档目录指到临时区）→ ①样本被移出语料库（gitignored，
> 差点找不回）②`processed/` 里留下一份 `CPMS=""` + `【中文品名待定】` 的产物。
> 脚本当时只打了 WARN 就继续跑了。**这个坑踩了两次** → 1.0.25 起 `main.py` 加了硬闸
> `sample_run_guard()`（见 §10），查看器加了交付闸门徽章（见 §4.10）。
> 业务是看 `processed/` 时问出来的：「为什么你导出的 json 里会缺少 CPMS？」

---

## 6. 字段一致性校验（必读）

被问「输出是否和处理的字段一致」时，**不要只看输出文件**。走这三步：

1. **字段集合 + 顺序** —— 输出 JSON 的**前 59 个键**必须与
   `assets/add_customer_quote_request.json` 的 **59 个键完全一致且同序**。集合对、顺序错也算不一致。
   其后只允许跟 `assets/internal_export_fields.yaml` 声明的**内部字段**
   （`ID` / `CPMC_ZH` / `Reminder`），顺序同样要对；用 `--strip-internal` 则应该一个内部字段都没有。
   ```bash
   python3 - <<'PY'
   import json
   tpl = json.load(open("/home/claw/customer-quote-request/assets/add_customer_quote_request.json"))
   out = json.load(open("<输出的 ERP JSON 路径>"))
   tpl_keys = list(tpl)
   print("ERP 59 键顺序一致:", list(out)[:59] == tpl_keys)
   print("内部字段:", list(out)[59:])
   print("缺失:", set(tpl_keys) - set(out))
   print("ERP 部分多余:", set(list(out)[:59]) - set(tpl_keys))
   PY
   ```
2. **读生成代码，不看产物猜** —— 打开 `scripts/main.py` 的 `map_fields()`，看它真正
   赋值了哪些字段（第 4.1 节列了当前口径），扣掉之后**剩余字段直接沿用模板默认值**。
   `CPMS` 不赋值（留空）—— 由 agent 亲自撰写。
   ⚠️ 字段「出现在 JSON 里」≠「被逻辑处理过」。
3. **反查可产出性** —— 逐个 diff 输出值 vs 代码**能**产出的值。出现代码不可能产出的值，
   说明这份产物是人工/上一手填的，不是脚本跑的，不能当作流程验证通过。
4. **核对取值合法性**（1.0.14 新增）—— 取值必须落在 ERP 合法枚举内，否则导入会丢：
   ```bash
   uv run ./scripts/erp_schema.py --check <输出的 ERP JSON 路径> --customer ngv
   ```
   等价于读任务目录里的 `<原文件名>.validation.json`。有 `error` 就别导入。
   内部字段（`ID`/`CPMC_ZH`）会被自动跳过，不会报「字典里没有这个字段」。
5. ★ **与权威 ERP 导出逐字段对照**（1.0.18 新增）—— 校准客户 / 改口径后必做：
   ```bash
   uv run ./scripts/compare_with_erp.py --customer ngv \
       --samples-dir customer-files/samples/ngv \
       --accepted customers/ngv.erp_diff.json \
       --map CU26061701=Colouring
   ```
   把差异分成四类：**口径已定（白名单命中）** / `0 与空` / **⚠️ 需要核对** /
   ERP 侧多出的键。**只有「需要核对」那一类要人看**；退出码 = 该类差异条数，
   可以当门禁。白名单是 `customers/<id>.erp_diff.json`（每条都写明为什么该不同，
   改口径时同步它，免得差异报告被噪音淹没）。

6. ★ **交付闸门**（1.0.25 新增）—— 上面 5 步都过了还不够：产物里有两处是**脚本刻意不写**的
   （`CPMS`、`CPMC_ZH` 里的中文品名），必须 agent 亲手补完才算「可交付」。一条命令看：
   ```bash
   uv run python -m erp_view --no-open         # 列表上带红徽章「未完成」的就是还没补
   ```
   判据与展示见 §4.10 的交付闸门；事故来历见 §5 末尾。

> **实战案例（2026-05，v1 时期）：** processed 里某份 ERP JSON 的 `ZDFS="无线胶装"` /
> `DWMC="本"` / `CPLX="新产品"`，而 `main.py` 对前三个字段**根本没有任何赋值语句** →
> 判定该 JSON 为手工构造。日志同时露馅：「模板字段数: 41」（41 = 当时模板 40 + 内部键 `_task_id`）。
> ⚠️ 该判据依赖**当时的代码版本** —— 1.0.14 起 `ZDFS`/`DWMC` 已可由脚本赋值（档案驱动），
> 但那份旧 JSON 里的 `ZDFS="无线胶装"` 至今仍是**非法值**（ERP 只认 `胶装/PAD书`），
> 用第 4 步的 `--check` 一样能拦住它。

通用「源数据 → 固定 schema 产物」审计方法与可复用脚本，见技能
**`data-mapping-audit`** 及其 `references/customer-quote-request-audit.md`。做这类核对前先加载它。

---

## 7. 文件结构

```
customer-quote-request/
├── SKILL.md                            # ★ 总纲（本文件）
├── README.md
├── docs/
│   ├── CPMS_写作规范.md                # ★ CPMS 人读规范（设计依据 + 骨架/词表/3 个真实单据改写）
│   └── CPMS_规范与样例集.md            # ★ CPMS 日常用：格式速览 + 6 份新规范样例（含 ERP 字段）
├── skills/                             # ★ 客户子技能（软链到 Hermes 技能目录，见下）
│   ├── customer-quote-igl/SKILL.md     #   IGL 专属：平铺表范式 / 口径 / 基线 / 检查清单
│   └── customer-quote-ngv/SKILL.md     #   NGV 专属：分区表单范式 / 口径 / 语料库 / 检查清单
├── customers/                          # ★ 客户档案（新增客户只动这里）
│   ├── README.md                       # 新增客户的 3 步 SOP
│   ├── _defaults.yaml                  # 公共默认值 + 兜底档案
│   ├── _template.yaml                  # 新客户骨架
│   ├── igl.yaml                        # IGL（已校准）—— 平铺键值对范式
│   ├── ngv.yaml                        # NGV（已校准）—— 分区表单范式（2.6）
│   └── ngv.erp_diff.json               # ★ NGV 的「已定口径差异」白名单（compare_with_erp 用）
├── assets/
│   ├── add_customer_quote_request.json # ERP 字段模板（59 键，权威来源）
│   ├── internal_export_fields.yaml     # ★ 内部导出字段（ID / CPMC_ZH / Reminder / version）
│   ├── erp_form_schema.yaml            # ★ ERP 表单权威字典（合法枚举 / 别名 / 类型）
│   ├── cpms_spec.yaml                  # ★ CPMS 写作规范（骨架 / 词表 / 槽位语法 / 规则码）
│   ├── customer_erp_mapper.xlsx        # 客户字段↔ERP 字段对照（igl 的，客户相关）
│   ├── product_categories.xlsx         # 产品类别源表（ERP 导出，88 行，含业务定义）
│   ├── cplb_tree.yaml                  # ★ 产品类别权威树 + 重设计导航树（由脚本生成）
│   ├── cplb_tree.html                  # ★ 产品类别可视化选取器（可搜索、点击复制）
│   └── page_thickness.xlsx             # 纸张厚度表
├── erp_view/                           # ★ 产物查看器包（Flask，1.0.26 从 scripts/ 搬来）
│   ├── data.py                         #   逻辑：扫描 / 路径安全 / ERP 字典 / 校验 / 交付闸门（不 import Flask）
│   ├── app.py                          #   Flask 工厂 + 4 路由 + CLI（create_app / main）
│   ├── __main__.py                     #   `uv run python -m erp_view`
│   └── templates/index.html             #   页面本体（零依赖原生 JS；刻意不含 Jinja 语法）
├── scripts/
│   ├── main.py                    # ★ 主入口（档案驱动，不含客户列名）
│   ├── compare_with_erp.py        # ★ 与权威 ERP 导出逐字段分类对照（1.0.18）
│   ├── customer_profile.py        # ★ 档案加载 / 校验 / 自动识别（可单独跑 CLI）
│   ├── extract_fields.py          # ★ 抽取引擎（布局策略 + 数量档位 + 文本解析）
│   ├── erp_schema.py              # ★ ERP 取值归一化 / 校验（步骤 5b，可单独跑 CLI）
│   ├── cpms_lint.py               # ★ CPMS 写法校验 / 机械规范化（写 CPMS 后必跑）
│   ├── internal_fields.py         # ★ 内部导出字段声明加载（ID / CPMC_ZH / Reminder / version）
│   ├── erp_viewer.py              # ★ 只是转发壳 → erp_view/app.py（1.0.26 起，逻辑不在 scripts/ 了）
│   ├── cplb_tree.py               # ★ 产品类别树：生成 / 查询 / 校验 / 出 HTML
│   ├── ingest_email.py            # ★ 邮件入口（附件 + 客户单号 KHDH）
│   ├── calibrate_customer.py      # ★ 新客户校准器
│   ├── product_categories.py      # 产品类别查表（旧路径，被 cplb_map 取代中）
│   ├── select_page_thickness.py   # 纸张厚度查询
│   ├── string2timestamp.py        # 日期 → 毫秒
│   ├── customer2erp.py            # 客户字段映射查询
│   ├── uuid_utils.py              # UUID4
│   ├── xlsx2csv.py                # Excel → CSV
│   └── pdf2json.py / docx2json.py / txt2json.py   # 文本类文件的调试前端（薄封装，统一走 extract_fields）
├── tests/                         # 332 项测试（uv run pytest）
│   ├── conftest.py
│   ├── test_customer_profile.py   # 档案层
│   ├── test_extract_fields.py     # 抽取引擎
│   ├── test_erp_schema.py         # ★ ERP 枚举归一化 / 校验（含历史单据缺陷回归 + 决策钉死）
│   ├── test_cplb_tree.py          # ★ 产品类别树（与源表一致 / 导航树全覆盖 / 档案映射有效）
│   ├── test_ngv_profile.py        # ★ NGV 分区表单（section_pairs 机制 + 真实样本回归）
│   ├── test_ingest_email.py       # 邮件入口
│   ├── test_cpms_lint.py          # ★ CPMS 规范（样例自证 + 每条规则码 + --fix 不越界）
│   ├── test_cpms_doc_samples.py   # ★ CPMS_规范与样例集.md 里的 6 份样例必须零违规（文档即测试）
│   ├── test_viewer.py             # ★ 产物查看器（扫描 / 路径安全 / 分组覆盖 / HTTP 端点）
│   └── test_pipeline.py           # 端到端回归（真实样本 + 逐字段值断言）
├── customer-files/                # pending / processed / failed（产物，不进仓库）
│   └── samples/ngv/               # ★ NGV 校准语料库（真实附件 + ERP 导出 + manifest，不进仓库）
├── logs/                          # 任务日志（不进仓库）
├── pyproject.toml                 # 依赖（pandas / openpyxl / python-docx / pymupdf / pyyaml / **flask**）
└── uv.lock
```

**任务目录结构：**

```
processed/<task_id>/
├── ERP_<task_id>_<YYYYMMDD_HHMMSS>.json   # 交付物（CPMS 需 agent 补写）
├── <原文件名>.extracted.json               # 抽取明细：values / unmatched_labels / extras_found
├── <原文件名>.validation.json              # ★ ERP 取值校验报告（步骤 5b：枚举/类型/必填）
├── <原文件名>.csv                          # 转换后的 CSV
├── <原文件名>.xlsx                         # 原始文件（已从 pending 移出，防重复处理）
└── <原文件名>.xlsx.meta.json               # 邮件副档（若来自 ingest_email）
```

---

## 8. 常见操作

```bash
cd /home/claw/customer-quote-request

# 处理（自动识别客户）
uv run ./scripts/main.py "customer-files/pending/QUOTE.xlsx"

# 指定客户（识别不准时）
uv run ./scripts/main.py "customer-files/pending/QUOTE.xlsx" --customer igl

# 只看抽取结果（不产出 ERP JSON，调试用）
uv run ./scripts/extract_fields.py "customer-files/pending/QUOTE.xlsx" --customer igl
uv run ./scripts/extract_fields.py "customer-files/pending/QUOTE.xlsx" --json

# 档案管理 / 客户偏好表
uv run ./scripts/customer_profile.py --list
uv run ./scripts/customer_profile.py --validate
uv run ./scripts/customer_profile.py --prefs            # 客户偏好表（全部客户）
uv run ./scripts/customer_profile.py --prefs ngv        # 单看某客户

# 与权威 ERP 导出逐字段对照（改口径后必跑）
uv run ./scripts/compare_with_erp.py --customer ngv \
    --samples-dir customer-files/samples/ngv --accepted customers/ngv.erp_diff.json \
    --map CU26061701=Colouring
uv run ./scripts/customer_profile.py --detect "customer-files/pending/QUOTE.xlsx"

# 新客户校准
uv run ./scripts/calibrate_customer.py --customer ngv --file <样本> --layout

# 看产物（自动扫描 customer-files/processed，浏览器里看）
uv run python -m erp_view                           # 起服务 + 自动开浏览器
uv run python -m erp_view --no-open --port 9000     # 远程/无头
uv run ./scripts/erp_viewer.py --no-open            # 等价（那个文件只是转发壳）

# 邮件投递
uv run ./scripts/ingest_email.py --eml mail.eml

# CPMS 写法（agent 写完必须跑）
uv run ./scripts/cpms_lint.py <ERP.json>            # 退出码 = error 数，必须 0
uv run ./scripts/cpms_lint.py <ERP.json> --fix      # 机械规范化（CR/单位/冒号/同义词）
uv run ./scripts/cpms_lint.py --vocab               # 骨架 + 闭集词表

# 单点查询
uv run ./scripts/cplb_tree.py --node 23.01          # 产品类别树：单个节点
uv run ./scripts/cplb_tree.py --search 揭页          # 按中文/英文/行话搜类别
uv run ./scripts/cplb_tree.py --suggest "hardcover board book"
uv run ./scripts/product_categories.py --select "Paperback"   # 旧查表（历史路径）
uv run ./scripts/select_page_thickness.py --pg 书纸 --faw 90      # 0.11（表里无 90g，就近取 100g）
uv run ./scripts/string2timestamp.py May-27                        # 1809129600000
uv run ./scripts/customer2erp.py --field "Format" --value "Paperback"

# 测试
uv run pytest
uv run pytest tests/test_pipeline.py -v
```

可用纸张类型（厚度表）：
`书纸` / `光粉纸` / `单粉` / `哑粉` / `坑纸` / `灰板` / `牛油纸` / `粉灰` / `贴纸` /
`紫罗兰高压双铜` / `紫罗兰高压复合`。

---

## 9. 错误处理

| 类型 | 处理 |
|------|------|
| 文件格式不支持 / 文件为空 | **中止**，原始文件移入 `customer-files/failed/` |
| 59 键断言失败 | **中止**（宁可不产出，也不产出脏 JSON） |
| 客户识别不出 | 用兜底档案 `generic` 继续，但打 **WARN**；字段值必须人工核对 |
| 档案 `status: draft` | 继续，但打 **WARN**；字段值必须人工核对 |
| 单个字段映射失败 | 用模板默认值，记日志 |
| 产品类别匹配失败 | 用档案 `product_category.fallback`（默认 `32.00;其他`，必须在树里），记日志 |
| 类别编号不在产品类别树里 | **WARN + 降级到兜底值** —— 编号错等于导成别的类别，宁可粗不可错 |
| 日期解析失败 | 返回 `""` 并 WARN |

日志级别：`INFO` 正常 / `WARN` 用默认值、数据缺失 / `ERROR` 阻塞性错误 / `DEBUG` 调试。

---

## 10. 已知陷阱

**架构 / 维护**

- **客户列名只能出现在档案里。** 在 `main.py` / `extract_fields.py` 里写死某客户表头 = 重构退化。
- **改 `customers/_defaults.yaml` 等于改所有客户** —— 改完必须对每份档案各跑一遍回归。
- **`status: draft` 的档案不能信**（NGV 就是）：能跑出 JSON，但字段值要人工核对。
- **三个 SKILL.md 都是软链接，仓库是唯一源（只改仓库）**：
  `SKILL.md`（总纲）、`skills/customer-quote-igl/SKILL.md`、
  `skills/customer-quote-ngv/SKILL.md`
  ← 技能加载路径 `~/.hermes/profiles/igl_agent/skills/<同名技能>/SKILL.md`。
  一份仓库源 = 一份技能文档，不存在双份漂移。用 `ls -l` 确认链接未断；
  断了说明有人把它换回了实体文件（自愈脚本见下）。
- **客户专属内容不许写回总纲**：附件结构、口径、坑、基线值都属于对应子技能
  （`customer-quote-igl` / `customer-quote-ngv`）。总纲只留「引擎能力 + 机制」，
  否则总纲会重新长成第二个客户档案。新增子技能时照抄现成的两份结构。
- ⚠️ **`hermes skills check/update/uninstall` 会穿透软链接覆盖仓库文件** ——
  该技能在 hub 里登记过（`~/.hermes/profiles/igl_agent/skills/.hub/lock.json`，
  来源 `Senthie/customer-quote-request`）。执行这些命令前先备份仓库副本，或摘掉 hub 登记项（见 TODO）。
- ⚠️ **别拿语料库里的原始文件直接跑 `main.py`** —— 跑完会把输入**移走**（归档进任务目录，
  `[文件 5/5] 原始文件归档`），`customer-files/samples/<客户>/` 就被破坏，相关测试随即大面积报
  `FileNotFoundError`。**1.0.25 起这里已经是硬闸**（`main.py: sample_run_guard()`）：
  输入在 `customer-files/samples/` 下、而归档目录没全指到外面 → **直接拒绝执行**（退出码 1），
  并把可照抄的命令打出来。放行条件是 `--processed-dir` **和** `--failed-dir` **都**指到语料库之外
  （只指一个不够：失败时文件照样会被移进默认的 `failed/`）。

  ```bash
  # ✅ 想让样本原地不动：两个归档目录都指到临时区
  uv run ./scripts/main.py "customer-files/samples/ngv/<附件>.xlsx" -c ngv \
      --processed-dir /tmp/cqr_scratch/processed --failed-dir /tmp/cqr_scratch/failed
  # ✅ 只想抽取/校准、不产出交付物（更常用）：extract_fields.py 本来就不动原文件
  uv run ./scripts/extract_fields.py "customer-files/samples/ngv/<附件>.xlsx" -c ngv
  # 已经吃掉了？从任务目录把原文件拷回 samples/ 即可（mtime 会保留）
  ```

  **为什么从 WARN 升级成硬闸**：原来的 WARN（`文件不在 pending 目录，可能已被处理`）拦不住人 ——
  2026-09-24 这个坑踩了两次；第二次还顺手往 `processed/` 里留了一份 `CPMS` 为空的「未完成」产物，
  被业务看见才暴露。语料库是 **gitignored 的真实客户原文**，丢了就是永久丢失。
- ⚠️ **`processed/` 是交付区，不是工作区**：跑管道做验证时把归档目录指到 `/tmp/...`，
  别让「样本试跑」在 `processed/` 里留下痕迹 —— 产物一旦落在那里就会被当成交付物。
  `CPMS` 为空 / 中文品名还是占位符的产物**不得留在 `processed/`**（判定与展示见 §4.10 交付闸门）。

**解析**

- **`search_files` 用中文正则会假阴性**（返回 0 命中，即使文件里确实有）。
  查中文一律改用 `grep -n`。
- **同一行可能有多个键值对**：`Imprint | Autumn | Complexity | Low | Requested By | (空格) | Christine`。
  只做「同列标签行 + 数据行」配对会把这些字段**整行丢掉** —— `auto_pairs` 策略就是为它写的。
- **相邻两行都是标签行时不能把下一行的标签当值**：`auto_pairs` 对「密集标签行」会先检查
  下方单元格是不是标签，是则改取右侧。
- **自由文本切标签必须用精确匹配**（`match_label_exact`）：模糊匹配会让 `Series Nam`
  这类前缀也算命中 `Series Name`，于是「最长前缀」永远等于整行、值反而丢。
- **档案里并存短别名与长标签时取最长者**：`Group` 不会抢走 `Group/Series Number`，
  `Series` 不会截胡 `Series Name`。
- **数量档位的纯数字写法**必须靠标签提示词判定，且**提示词匹配大小写不敏感**
  （`Quantity` vs `quantity`）—— 这个坑踩过一次。
- **文本类旧解析器已统一**：`pdf2json` / `docx2json` / `txt2json` 现在只是
  `extract_fields` 的薄封装。历史上它们各有一套「行里有 `:` 就当键值对」的弱解析，
  同一个 PDF 两个入口两种结果 —— 不要再恢复那种写法。

**数据**

- ★ **`KHDH` vs `KHCPMC` —— 已由业务拍板（2026-09-23），不要再改回去。**
  - **决策：保留 `KHDH`，输出侧不得出现 `KHCPMC`。** 理由：ERP 表单里控件名是
    `txtKHCPMC`（label「客户单号」），而隐藏区还有个 `lblKHCPMC` = 「客户产品名称」——
    同一个 `KHCPMC` 在 ERP 内部被用在两处，是 ERP 侧的命名错误。
  - **我们的态度是不跟着错**：`KHCPMC` 与模板第 43 键 `KHDDH`（香港单号）只差一个字母，
    沿用会误导填单的人。**ERP 导入接口由 ERP 侧修改适配**（已确认，不是待办）。
  - 溯源信息保留：字典里 `KHDH.form_name = txtKHCPMC`，方便回查 ERP 页面。
  - 有测试钉着：`test_决策_客户单号用_KHDH_且不得出现_KHCPMC`。
- ★ **`KHDH`（第 33 键，客户单号）与 `KHDDH`（第 43 键，香港单号）必须分开 —— 2026-09-24 修正。**
  两个键只差一个字母，此前一直混用：附件文件名里的「香港单号 CU26051601-01」
  被写进了 `KHDH`，而 `KHDDH` **永远是空的**（模板里定义了却没人填 —— 这种「定义了
  却从不赋值」的字段就是缺陷信号）。4 份权威 ERP 导出：`KHDDH`=单号、`KHDH` 全空。
  档案 `email:` 段用 `target` 声明落哪个字段（细节见子技能 `customer-quote-ngv` 第 5 节）。
- ★ **客户识别必须看正文（2026-09-24 修的真 bug）。** 只凭文件名打分极度危险：
  NGV 那份文件名含 `Colour` → 命中 igl 的 `filename_keywords`，10 分就胜过正文
  500 分的 NGV 特征（因为正文压根没被读），整单字段全错。现在表格类文件一律把正文
  一起送进识别；得分 <30（只有文件名/主题这类弱证据）会 WARN。各家特征见其档案 `detect:` 与子技能。
- ✅ **`0` 与「空」已由业务定案（2026-09-24）**：未使用的 `CPSL2`~`CPSL10` 与
  `KHMBDJ`（客户目标价）**我们落 `0` 可接受**（导出存空不算差异）。逐字段对照时
  `compare_with_erp.py` 会把这类差异单列成「0 与空」一类，不再当缺陷噪音。
- ★ **「显式留空」必须写 `""`，光在偏好表里写「留空」没用。** 模板自带默认值的字段
  （`PXH`=`1`、`YYS`=`1`、`BZ`=`美元`…）如果只在 `preferences` 里标 empty 而
  `erp_fixed` 还留着值，产物里就会悄悄带着默认值 —— 这种漂移由 `validate()` 与
  `tests/test_customer_profile.py::test_偏好表_拦住文档与落库值不一致` 拦下。
  正确写法：`erp_fixed: { PXH: "" }`（`main.py` 会真的清掉并对字段记账）。
- ★ **多材质混用的字段宁可留空，也不许「取第一个命中」。** `rules.scan` 的条目加
  `unique: true`：全文扫出多个不同取值就不落值（NGV 的 `FSCMC` 就是这种 ——
  盒底 FSC Recycled / 盒盖 FSC Mix / 竹底座 FSC 100%，ERP 里是空的）。
  留空的原因会记进 `.extracted.json` 的 `scan_notes` 并在日志里打 WARN，不是静默。
- ★ **`YYS` 有三种状态，别用「空」一锅端**：有语言数 → 数字；单语言 → **留空**
  （`empty_when_single`）；提取不到 → **`0`**（`when_missing`，代表「邮件可能后补、
  待业务核对」）。用 0 冒充真实值、或用固定值 1 兜底，都会让业务看不出该核哪一单。
- ⚠️ **别把 ERP 导出的「多出的键」当字段用**：CU26072302 那份导出有 60 个键
  （多了个「产品备注」—— 业务 2026-09-24：先不需要），4 份都带 ERP 侧的 `KHCPMC` 键。
  差异核对时先按模板的 59 键对齐，再看多出来的键是什么。
- **`Drop_SJFX` / `ZDFS` / `DWMC` 的合法取值已由 ERP 真表单确认**（1.0.14）：
  `短书脊`/`长书脊`/`四边裁`、`胶装/PAD书` 等 10 项、单位为 `本`。
  **`无线胶装` 不是合法值** —— 历史单据就填错了（正确是 `胶装/PAD书`）。
  改动这些枚举前先更新 `assets/erp_form_schema.yaml`，再跑 `uv run pytest`。
- **`CPMS` 完全不是脚本的事**：留空的 JSON 视为未完成，不得交付。
- **纸张克重传裸数字**，不要带 `g`（表里存 `"250"`）。这个坑曾让厚度查询**每次都失败**，
  一直悄悄走 `克重 × 0.001` 的粗估。
- ⚠️ **本文件曾出现过两个假的厚度示例**：`0.09/0.25 → 5.65`（其实是查表失败的粗估）、
  `0.10/0.32 → 6.32`（2026-05 那份**手工伪造日志**里的数字）。**以实跑为准**
  （igl 样本：`0.11 / 0.25 → 6.85`）。别照抄文档里的示例值。
- **`ZXLB`/`ZBLX`/`ZXFS`/`DKMC`/`ECPMC` 的中文名 v1 文档写错过**：
  分别是 纸箱类别 / 纸板类型 / 装箱方式 / 底卡名称 / 产品英文名。
- 改完 `scripts/*.py` 或 `customers/*.yaml` 后，**回头同步 SKILL.md 的 4.3 表与示例**，
  并跑 `uv run pytest`，否则文档/测试会再次和实现脱节。

---

## 11. 版本历史

| 版本 | 日期 | 变更 |
|------|------|------|
| 1.0.0 | 2026-03-26 | 初始版本 |
| 1.0.1 | 2026-03-26 | 修复 uuid 重复生成，优化日期解析 |
| 1.0.2 | 2026-03-30 | 添加纸张厚度计算 |
| 1.0.3 | 2026-03-30 | 输出命名规范 + 日志机制 |
| 1.0.4 | 2026-03-31 | 任务目录结构（UUID4 隔离，防重复处理） |
| 1.0.5 | 2026-04-10 | 输出加入 `id` / `create_by` |
| 1.0.6 | 2026-05-25 | 文档校正（字段清单重写） |
| 1.0.7 | 2026-09-23 | 技能副本改软链接（根治双份漂移）+「字段一致性校验」章节 |
| 1.0.8 | 2026-09-23 | **schema 破坏性升级 v1(40) → v2(59)** |
| 1.0.9 | 2026-09-23 | `main.py` 适配 v2；修厚度表查询；更正被伪造日志污染的示例 |
| 1.0.10 | 2026-09-23 | `KHCPMC`→`KHDH`；修客户表**横向键值对漏解析**；文本解析器字段名适配 v2；加 `.gitignore` |
| 1.0.11 | 2026-09-23 | **`CPMS` 改由 agent 亲力亲为**（删除脚本生成逻辑） |
| 1.0.12 | 2026-09-23 | **`CPSL1`~`CPSL10` 落地**（数量档位识别） |
| 1.0.13 | 2026-09-24 | **多客户架构重构**（见下） |
| **1.0.14** | **2026-09-23** | **ERP 表单权威字典 + 取值校验**（见下） |
| **1.0.15** | **2026-09-23** | **产品类别树重设计 + 四条业务决策落地**（见下） |
| **1.0.16** | **2026-09-23** | **NGV 客户校准：分区表单范式 + `section_pairs` 策略**（见下） |
| **1.0.17** | **2026-09-24** | **NGV 口径落地（第二批权威导出）+ 修客户识别真 bug + `KHDH`/`KHDDH` 拆分**（见下） |
| **1.0.18** | **2026-09-24** | **NGV 第二批 10 条口径全部拍板落地 + 客户偏好表 + 「不唯一就留空」+ 对照工具**（见下） |
| **1.0.19** | **2026-09-24** | **技能拆分：总纲 + 两个客户子技能（`customer-quote-igl` / `customer-quote-ngv`）**（见下） |
| **1.0.20** | **2026-09-24** | **实单暴露的三处缺陷修复 + 「宁可留空不许猜」加固**（见下） |
| **1.0.21** | **2026-09-24** | **第三批业务口径：`CPMC` 业务员=KM / `CPSX`=无（口径例外机制）/ `KHDDH` 默认空 / 报价截止过期提醒**（见下） |
| **1.0.22** | **2026-09-24** | **内部导出字段 `ID` / `CPMC_ZH`；`CPMC` 改存英文表达**（见下） |
| **1.0.23** | **2026-09-24** | **内部字段 `Reminder` 把提醒固化进产物；「无」落 JSON `null`（`enum_exceptions` 随之删除）**（见下） |
| **1.0.24** | **2026-09-24** | **产物加内部字段 `version` 标识配置版本（= 2）；旧查看器按新结构重写 + 自动扫描 `processed/`**（见下） |
| **1.0.25** | **2026-09-24** | **交付闸门：「未完成」产物不许留在 `processed/`** —— 语料库样本硬闸 + 查看器「未完成」徽章（见下） |
| **1.0.26** | **2026-09-24** | **查看器搬进 `erp_view/` 包 + 改用 Flask**：三层分离、扫描根进 `app.config`、测试改用 `test_client`（见下） |
| **1.0.27** | **2026-09-24** | **查看器排版重做 + 导出 PDF**：字段自适应网格（长值跨行）、标签压一行、`window.print()` 版 PDF（见下） |
| **1.0.28** | **2026-10-17** | **CPMS 写法定死**：18476 份历史单据实测 → 骨架 + 闭集词表 + 槽位语法 + `cpms_lint`（见下） |
| **1.0.29** | **2026-10-19** | **CPMS 规范样例集**：`docs/CPMS_规范与样例集.md`（格式速览 + 6 份新规范样例，覆盖平装/精装/多款 BB/线圈/套装/发声书）+ `tests/test_cpms_doc_samples.py`（文档即测试） |
| **1.0.30** | **2026-10-19** | **CPMS 规范 v2（业务拍板 5 条）**：套装多件「共【N】件」、`--fix` 自动补印色 C、备注留出处、灰板写 g、装箱待确认可交付（见下） |
| **1.0.31** | **2026-10-19** | **CPMS 样例 7（真实确认单 `BJXQ260704002`）+ 内容完整性「九问」**：业务给确认单作参照，提炼 9 条自检项（**1.0.32 已修正为严格模式**） |
| **1.0.32** | **2026-10-19** | **CPMS 严格模式（业务拍板）**：只写客户文件里的事实；新增 `scripts/cpms_content_audit.py` 溯源门禁 + `0.1 一页速查` + 两份 references（复盘 / 通用铁律）；样例 7 改为严格版（见下） |
| **1.0.33** | **2026-10-19** | **`.eml` 读取（实单 `CU26070802` / KiK `Ostermalblock`）**：正文即任务（无附件也能处理）、HTML 表格压平、内嵌图不落盘、单号可从 `.eml` 文件名取；修「值里含标签词被整条丢弃」与 `Quantity→quantity_tiers` 两个静默 bug；NGV 识别补发件人域名与英文 enquiry 特征 |
| **1.0.34** | **2026-10-19** | **`CPMC` 改为产品名（中文译名，不带业务员/品牌/类别/FSC）；业务员 `KM` 落 `YWY`**；`allow_chinese` 开关；旧 `KM/Aldi` 口径作废（见下） |
| **1.0.35** | **2026-10-20** | **装订方向 ≠ 书脊方向**：识别不到装订方向 → CPMS **不写方向词**、ERP `Drop_SJFX` **留空（JSON null）**（新增 `rules.scan: binding_direction`，只进 CPMS）；**顺修 FSC 扫描**：`FSC-Mix` 连字符此前漏匹配，把「混用」误判成单一值（见下） |
| **1.0.36** | **2026-10-20** | **`ECPMC`（产品英文名）补上**：`CPMC` 改成中文译名后英文名没了着落 —— 新增档案开关 `rules.ecpmc.source_keys`（NGV：`product_name` → `series_name`，客户原文照抄）；未声明的客户保持留空（不动 igl 基线） |
| **1.0.37** | **2026-10-20** | **输入单元（业务口径）**：**单文件 = 独一份报价**；**文件夹 / 压缩包 / 多附件邮件 = 同一份报价的全部参考文件**，合并成一个 ERP JSON。新增 `scripts/input_unit.py`（安全解压 / 垃圾文件过滤）、`merge_customer_data()`（主文件优先 + 冲突记账 + 扫描类整单元重跑）、`ingest_email` 多附件投成单元目录（见 0.0） |
| **1.0.38** | **2026-10-20** | **子目录各算一份报价（批次拆分）**：文件夹 / 压缩包里有子目录 → **子目录各算一份**、根下散文件另算一份、没有子目录仍是一份；一份失败不影响其余份；压缩包只归档一次（见下） |
| **1.0.39** | **2026-10-20** | **业务真单复核（`BJXQ260708007` vs 我们的产物）**：`CD`=长/`KD`=宽（NGV 落 `210/148`）、`CPLB` 命中冲形状 → `23.03`（旧规则把 `perfect binding` 一律当 `23.06` PAD头书）、书脊方向改由「装订边 + 版式」推出、`YWY` 确认必须填 `KM`（生成方标记）、CPMS 装订段不许照抄 `ZDFS`（见下） |

### 1.0.39 详细变更 —— 拿业务真单复核（一单一账户，四类错误）

**起因（2026-10-20 森西）**：把业务在 ERP 里手写的同一单（`BJXQ260708007` / 客户邮件
`CU26070802 price enquiry. Ostermalblock`）当 ground truth，和我们的 `1c0de636` 逐字段对。
31 处差异里 **4 类是我们的错**，其余是已拍板口径 / ERP 自动带出 / 业务录入习惯。

#### ① `CD` = 长、`KD` = 宽（业务拍板，覆盖历史习惯）

- 客户 TPS `148 x 210 portrait` → **`CD=210` / `KD=148`**（真单一致；CPMS 概要行【210*148*0】）。
- 实现：`customers/<客户>.yaml` 的 `rules.size.long_first: true` → `main.parse_size()`
  解析完若 `CD < KD` 就交换并打日志（`尺寸交换: 客户写 148 x 210 … → CD=210 KD=148`）；
  原始两段留在 `parsed["_raw"]`（书脊方向要拿它算横竖版）。
- **只给声明了该开关的客户开**：NGV 已开。igl 的附件列本身是 `H x W x D`、已核对口径是
  `CD=H` —— **业务 2026-10-20 拍板：igl 暂不改**（等拿到 igl 的 ERP 导出反证再议，别再主动提）（历史导出 `240x300`/`145x210` 那种
  「TPS 第一段进 CD」与 ERP 字段定义矛盾，视为业务录入习惯，不再跟随）。

#### ② `CPLB`：先认装订方式，再用**结构**细分（冲形状 / PAD）

旧规则 `perfect\s*binding|open\s*pad|pad\s*bind → 23.06` 三个错叠在一起：

1. `perfect binding` 只是**装订方式**，不是产品形态；
2. `at the top` 的 `top` 被当成 PAD 的「头」（那是**装订边位置**）；
3. 整书冲形状（`shape die-cutting egg form`）根本没参与判定 → 正解 `23.03`。

现在是「装订方式 → 结构」两级，并新增 `cplb.map[].requires`（同一份原文里还必须另有证据）：

```yaml
- { match: "safety\\s*saddle|saddle[- ]stitch", node: "24.03" }
- { match: "wire[- ]?o|wiro|YO书|双线圈", node: "26.07" }
- { match: "spiral|蛇圈|单线圈|线圈",        node: "26.01" }
- { match: "case\\s*bound|hardcover|精装",   node: "22.02" }
- { match: "ridged?\\s*box|folded\\s*box|\\blose\\b", node: "29.04" }
- { match: "pad\\s*(?:head|头)|露\\s*PAD|PAD\\s*头|open\\s*pad", node: "23.06" }   # PAD 必须原文有 PAD
- { match: "(?:shape\\s*die|die[- ]?cut|egg\\s*form|冲形状|啤\\s*形状)",
    node: "23.03", requires: "perfect\\s*binding|softcover|open\\s*pad|无线胶装|胶装" }
- { match: "perfect\\s*binding|softcover|无线胶装", node: "23.01" }        # 基础节点
```

- **PAD头书（23.05/23.06）只能人工判**：前提是「封面大于内文 / 露 PAD 头 / 二次上胶装」。
  `CU26072302` 的 `specials` 里确实写着 `open pad binding at long side (210 mm)` → 脚本仍落 23.06 ✓；
  但只写 `perfect binding` 的（如本单）落 23.01，交业务改 —— **宁可粗，不可错**。

#### ③ 书脊方向：装订边 + 版式 → 长/短书脊（把 1.0.35 的「不猜」收窄）

- 1.0.35 说「顶/底/左/右 反推不出长/短」—— 那是在 `CD/KD` 顺序没定的前提下。
  ① 落地后这就是**几何换算**：上/下边装订 → 书脊沿**宽**边；左/右边 → 沿**长**边。
- 真单 `148x210 portrait` + `perfect binding at the top` → **短书脊**（业务 CPMS 前四个字就是它）。
- 实现 `main.spine_from_binding_direction()`：判不出来才留空（`Drop_SJFX` = null），不猜。
- 顺手补了 `rules.scan.binding_direction` 正则的 `side` —— 客户写 `at the side` 以前抓不到。

#### ④ `YWY` = `KM`（**生成方标记**，不是业务员姓名）

业务原话（2026-10-20）：「YWY 必须填你的名字，这是我们区分谁生成的单」。
`KM` = 这份报价由脚本/agent 产出的标记；业务手写的单那格是本人姓名（如「陈纯」）。
**所以：既不许清空（1.0.24 之前的老表格写「留空」是错的），也不许换成业务员姓名。**

#### ⑤ CPMS 两条新闸门（这次的两个漏网）

| 码 | 级别 | 拦什么 |
|---|---|---|
| `CPMS055` | **error** | 装订段照抄了 ERP「装订方式 `ZDFS`」的下拉值（`胶装/PAD书`）—— 一个字段同时代表「胶装」和「PAD 书」两类，不是装订方式；要写客户原文（本单：`冲形状无线胶装书（冲鸡蛋形状）`） |
| `CPMS013` | warn | 书本类概要行缺「书籍方向：【长/短书脊】」—— `Drop_SJFX` 为空只表示 ERP 侧没落库，不代表 CPMS 可以不写方向（旧 `CPMS010` 把这个坑放过了） |

> **已拍板（2026-10-20 森西）：`*** 请报底价` 不固化进 CPMS 骨架** —— 那是业务手写习惯，
> 客户原文没有就不写（严格模式）。以后别再问、也别擅自加。

> **教训**：脚本字段对 ≠ CPMS 对。CPMS 那段文本必须回客户原文抄装订方式、
> 方向，并且概要行与 ERP 字段**各自**自洽（可以一个有一个没有，但要能解释）。

#### ⑥ 喂错入口（踩到的坑）

`.eml` **不能直接喂 `main.py`**（它只吃 csv/xlsx/docx/txt/pdf）。正确姿势：

```bash
uv run ./scripts/ingest_email.py --eml "客户邮件.eml"   # 正文落成 .txt + 副档（含 KHDDH）
uv run ./scripts/main.py <pending 里那个 .txt> --customer auto
```

直接喂 `.eml` 会得到「不支持的文件格式」；拿 `.txt` 手动跑而跳过 `ingest_email`，
`KHDDH` 会丢（单号只在 `.eml` 文件名/正文头里）。

### 1.0.38 详细变更 —— 子目录各算一份报价（批次拆分）

**业务原话（2026-10-20 森西，回答 1.0.37 留下的边界问题）**：
> 「2」—— 即「文件夹里有子目录就按子目录拆，子目录内合并」。

| 输入 | 语义 | 产出 |
|---|---|---|
| 文件夹 / zip，**没有子目录** | 一份报价的全部参考文件 | 1 个 ERP JSON（1.0.37 口径，一行没变） |
| 文件夹 / zip，**有子目录** | **每个子目录 = 一份独立报价**，子目录内部继续合并 | 每个子目录 1 个 ERP JSON |
| 子目录之外、根下散着的受支持文件 | 也算一份（绝不偷偷并进某个子目录） | 1 个 ERP JSON，名字 `<父目录名>_根目录文件` |
| ↳ 口径拍板（2026-10-20 森西选 **1**） | **保持独立成单**，不并进/不复制到子目录 | `validation.unit.note` 写一句「若它是给各子目录共用的说明，请人工复制进各子目录后重跑」 |
| 邮件 ≥2 个附件且都是子目录 | 一封邮件一次询价，但里面是多个独立单 | 多份，各 1 个 ERP JSON |

**规则细节**

- 入口 `resolve_input_units(path) -> list[InputUnit]`：返回 1 个 = 老行为；返回多个 = 逐份处理，
  **互不影响**（一份失败不影响其余份）。
- 「有子目录」= 子目录里**确实有受支持的文件**（空目录、`__MACOSX` 不算）。
- 子目录内部**不再往下拆**：`A/B/x.txt` 全部算 A 那一份（「子目录内合并」）。
- 每份产物：`processed/<任务ID>/` 下 ERP JSON / extracted / validation / CSV + **它自己那个子目录**
  （整包搬进去，参考文件跟着自己那份走）。
- 压缩包拆成多份：**压缩包只随第 1 份归档**（`archive_owner=True`），其余份在 validation 里记
  `source_archive`（包名）+ `archive_owner: false` —— 不复制 N 份压缩包。
- 共享邮件副档 `_email.meta.json`：每份**复制**一份；批次收尾清原件；失败那份复制进 `failed/` 留证。
- 批次收尾 `finalize_batch()`：父目录被搬空 → 删掉；还剩东西（不支持的类型等）→ 日志明说
  「没被任何一份报价用上」。
- 退出码：全部成功 `0`；**任一份失败 → `1`**（成功那几份的 ERP JSON 照样在 processed 里）。
- 失败隔离：失败那份的原路径搬进 `failed/failed_<时间>/`（zip 拆出来的份是复制），日志写
  `处理状态: 失败 —— 继续处理同批剩余的 N 份`。

**新增/改动**

- `scripts/input_unit.py`：`InputUnit` 增 `origin / role / from_archive / archive_owner` 与
  `is_batch_member`；新增 `resolve_input_units()`、`finalize_batch()`、`_child_dirs_with_files()`、
  `_root_files()`；`_email.meta.json` 不再被报成「跳过的客户文件」；
  `find_unit_meta()` 支持「子单元 → 父目录 → 压缩包旁」三级查找。
- `scripts/main.py`：`main()` 改为「解析成 N 份 → 循环 + 逐份失败隔离 + 批次汇总」；
  原单份流程收进 `process_unit()`；新增 `move_unit_to_failed()`；
  `step6` 增加「压缩包只归档一次」「共享副档复制」；validation 的 `unit` 增
  `kind_cn / role / path / origin / source_archive / archive_owner`。
- 测试：`tests/test_input_unit.py` +6 项（子目录拆分与根下散文件、无子目录老口径不变、
  zip 拆分 + 只归档一次、批次收尾两种结局、端到端两个子目录出两份报价）。

> ⚠️ 别把「一个文件夹 = 一份报价」和「子目录 = 一份报价」记混：**看有没有子目录**。
> 根下散文件的口径是**独立成单**（业务选 1），不是共享给每个子目录 —— 这条由
> `test_口径1_根下散文件单独成单_不并进任何子目录` 钉住。
> 一个文件夹里躺着单号 A、单号 B 两个目录 → 两份报价、两个 ERP JSON。

### 1.0.37 详细变更 —— 输入单元：一份报价由几个文件组成

**业务原话（2026-10-20 森西）**：
> 「单个文件属于独一份报价，文件夹和压缩包里的文件则为报价的全部参考文件。」

| 输入 | 语义 | 产出 | 实现 |
|---|---|---|---|
| 单文件 | **独一份报价** | 1 个 ERP JSON | 老路径，行为不变 |
| 文件夹 | 这一份报价的**全部参考文件** | 1 个 ERP JSON | `input_unit.resolve_input_unit` → `folder` |
| `.zip` | 同上 | 1 个 ERP JSON | 安全解压到临时目录 → 同 folder（用完删） |
| 邮件 ≥2 附件 | 同上（一次询价） | 1 个 ERP JSON | `ingest_email.ingest` 投成 `pending/<标题>_<时间>/` + `_email.meta.json` |

**新增/改动**
- `scripts/input_unit.py`（新）：单元解析、zip 安全解压（拒 `..` / 绝对路径，上限 200 个文件 / 200 MB）、
  垃圾文件过滤（`__MACOSX` `.DS_Store` `Thumbs.db` `~$*` 隐藏文件 不支持的扩展名）并记账到 `unit.skipped`。
- `extract_fields.merge_customer_data()` / `load_customer_unit()`（新）：多文件合并成**一个** `CustomerData`；
  主文件 = 规范键最多的（同分→表格类→文件名），同键不同值 → `conflicts`；`rules.scan` 整单元重跑；
  文本块按 `# 文件：xxx` 分段；`sources[规范键] = 文件名` 可溯源。
- `main.py`：`resolve_input_unit()` 统一入口；`step1/step2/step6` 改吃单元；
  单元产出 `<单元名>.csv`（长表：来源文件 / 规范键 / 值）；原始文件夹/zip **整包**归档进 `processed/<任务ID>/`；
  `validation.json` 增加 `unit`（kind/members/skipped）与 `conflicts`。
- `ingest_email.py`：≥2 附件 → 单元目录 + 一份共享副档；1 个附件仍是单文件（不制造目录）。
- 测试：`tests/test_input_unit.py`（9 项：单元识别 / 垃圾过滤 / zip 穿越 / 合并口径 / 端到端一个文件夹只出一份）
  + `tests/test_ingest_email.py` 两项（多附件成单元、单附件不变）。

> **一份报价 = 一个 ERP JSON。** 看到同一单产出多个 JSON，先查是不是把「参考文件」
> 当成「独立报价」逐个跑了 —— 那是这个口径要防的错。

### 1.0.36 详细变更 —— `ECPMC` 产品英文名

**业务 2026-10-20 指出**：「JSON 中 CPMS 有了，但是 ECPMC 却没有」。

ERP 表单里 `txtCPMC`（产品名称）与 `txtECPMC`（产品英文名）是**两个独立字段**。
1.0.34 把 `CPMC` 改成中文译名后，英文品名就只剩 `ECPMC` 这一个去处，而它一直是空的。

| 字段 | 这一单的值 | 来源 | 谁填 |
|---|---|---|---|
| `CPMC` | `复活节彩蛋涂色本` | agent 从 `Title: Ostermalblock` 翻译 | agent |
| `ECPMC` | `Ostermalblock` | 客户原文**照抄**（不翻译、不拼类别、不带 FSC） | 脚本（`rules.ecpmc.source_keys`） |

- 档案写法：`rules.ecpmc.source_keys: [product_name, series_name]`（按顺序取第一个非空）。
- **没声明 `rules.ecpmc` 的客户不产出该字段** —— igl 的 CPMC 里已经含英文系列名，业务说「不需要更改」，所以 igl 维持空值。
- 测试：`test_决策_产品英文名落ECPMC`（含「未声明就留空」的反例）。

### 1.0.35 详细变更 —— 装订方向与书脊方向分家

**业务原话（2026-10-20，实单 `CU26070802` / `Ostermalblock`）**：
> 「装订方向 如果我们识别不到则设置为 书脊方向为 Null」

| 事项 | 决定 | 落点 |
| --- | --- | --- |
| 装订方向写了 | 照抄原文（`at the top` → `装订在头部`） | CPMS「装订」段 |
| 装订方向识别不到 | CPMS **不写方向词**（不猜「书脊方向」/「装订在长边」） | `assets/cpms_spec.yaml` 装订段 notes + `docs/CPMS_写作规范.md` 3.3 |
| ERP `Drop_SJFX` | 有 `N mm side` → 按装订边与较长边比较；**没有 → 留空（JSON `null`）** + WARN。**不许**落「长书脊」当默认值，也**不许**由「装订在顶部」反推短书脊 | `customers/*/rules.spine`: `default: ""`、`scripts/main.py` 第 8 步 |
| 方向原文的抽取 | 新增 `rules.scan: binding_direction`（`at the top / at the head / at the side`），**只给 CPMS 与日志用，不映射任何 ERP 字段** | NGV 档案 |

> 为什么必须分开：`at the top` 说的是**装订边位置**，`Drop_SJFX` 说的是**书脊在长边还是短边**，
> 两个维度正交。混在一起会得出「PAD 头书 = 短书脊」这种没有依据的结论；
> 而落「长书脊」当默认值，则是**用默认值冒充事实**——业务要的是空着让人看见。

**顺带修的 FSC 坑（同一单暴露，`scripts/extract_fields.py`）**：
客户写混用时长这样 —— `230 gsm artcard C1S (FSC-Mix)`。旧正则 `FSC\s*Mix` 不认连字符，
只扫到备注里的 `FSC Recycled` → 把「混用」误判成单一取值，`FSCMC` 险些落成 `FSC Recycled`。
现在：正则分隔符 `[\s-]*` + `unique_ignore_separators: true`（判等时空格/连字符/破折号一视同仁）——
`FSC Mix` / `FSC mix` / `FSC-Mix` 算同一个值，`FSC-Mix` vs `FSC Recycled` 才算混用 → 留空 + `scan_note`。

### 1.0.34 详细变更 —— `CPMC` 只放产品名，业务员落 `YWY`

业务 2026-10-19 对实单 `CU26070802` 的更正（原话）：
「`CPMC` 是这个产品的名称，你应该从文件中翻译出来；而 `KM` 也就是你，是你的名字，
他应该填写在 `YWY`。」

| 项 | 旧（1.0.21–1.0.33） | 新 |
|---|---|---|
| `CPMC` | `KM/Aldi <英文原文>(<FSC>)` 或 `<业务员>/<类别>+<附属件> <原文>(FSC)` | **产品名称的中文译名**，无前缀无类别无 FSC |
| `CPMC_ZH` | 中文复合串 | 与 `CPMC` 同值 |
| `YWY` | 留空（ERP 侧） | **`KM`**（脚本 `erp_fixed` 固定） |
| `CPMC` 含中文 | 报警 | `allow_chinese: true` 的客户不报 |

落地：

- `customers/ngv.yaml`：`erp_fixed.YWY: KM`；`rules.cpmc` 改 `template/zh_template = 【中文品名待定】`
  + `allow_chinese: true`；`salesperson` 仅留档。
- `scripts/main.py`：中文硬不变量改为按 `cpmc.allow_chinese` 开关；占位符 WARN 现在同时点名
  `CPMC` 与 `CPMC_ZH`。
- 测试：原「CPMC 存英文表达」用例改写为 `test_决策_CPMC是产品名称_业务员落YWY`
  （断言 `CPMC=【中文品名待定】`、`YWY=KM`、Wire-O → 26.07），另加
  `test_决策_未开allow_chinese的客户_CPMC中文要报警` 保护旧行为。

### 1.0.33 详细变更 —— `.eml` 读取（实单 `CU26070802`）

业务给了 `CU26070802 - price enquiry. Ostermalblock.eml`：**没有 xlsx 附件**，规格全在邮件正文
（NGV / KiK 线，`Naumann & Göbel` 发的纯英文 enquiry）。旧流程只会写一份 `_email_*.json`，
`main.py` 无从下手 —— 等于这单读不进来。

四个改动：

| # | 改动 | 位置 |
|---|---|---|
| 1 | **正文即任务**：无可用附件时把正文落成 `<单号>_<标题>.txt` + sidecar，`main.py` 文本通道直接处理（`--no-body-as-task` 可关） | `ingest_email.py::write_body_task` |
| 2 | **HTML 表格压平**：`<td>TPS:</td><td>148 x 210 mm portrait</td>` → `TPS: 148 x 210 mm portrait`，纯文本缺失时也能抽 | `html_tables_to_text` |
| 3 | **内嵌图片不当附件**：`Content-Disposition: inline` 的 logo/签名图不落 pending | `parse_eml` |
| 4 | **单号可从 `.eml` 文件名取**：`CU26070802 - price enquiry….eml` → `KHDDH=CU26070802`，来源标「邮件文件名」 | `extract_numbers(extra_texts=)` |

同时修掉两个**静默丢值**的 bug（都只有真实邮件能暴露）：

- 文本模式对「标签下一行」用 `match_label()`（整词包含）判标签 →
  `64 pp + cover`、`100 gsm offset paper`、`30.100cps (one title, one language)` 全被跳过。
  改为只拒绝「整行就是一个标签」。
- `Quantity:` 命中合成键 `quantity_tiers`（文本模式没有档位行）→ 改投 `quantity`，
  由 `parse_single_quantity()` 解析德式千分位（`30.100cps → 30100`）。

配套：NGV 档案补 `sender_domains`（vemag-medien.de 等）与英文 enquiry 正文特征
（`price enquiry` / `Finished binding` / `euroscale`），该单识别得分 530；
`fields:` 补 `title` / `ETD` / `content` / `finished binding` / `packaging` 别名。
测试 +5（正文任务 / 关闭开关 / 文件名取号 / inline 图 / HTML 表格）。

### 1.0.32 详细变更 —— 严格模式（CPMS 只转录客户文件）

1.0.31 把「人工确认单」当成补全清单，把确认单里才有的信息写进了 `CPMS`（窗口尺寸、钻石材质、
背卡、料号、指定供应商、箱唛…），业务当场纠正：**没有就是没有**。

规则（已写进第 5 节）：

- `CPMS` 只允许写**客户提供的文件（附件 / 邮件正文）**里出现过的事实；
- 客户没给的尺寸/材料/颜色数/料号/责任方，**一律不写**；空栏就是空栏；
- 客户给区间（`50-60 stickers`）就照抄区间，不许「补成 60」；
- `polybag` 不许推断成 `OPP`，`foil package` 不许换材料；
- 业务/ERP 侧信息（指定供应商、纸箱强度、料号、供料责任）走 ERP 字段或邮件回复。

落地：

| 文件 | 变化 |
|---|---|
| `docs/CPMS_规范与样例集.md` | 5.1 节改为「严格模式 + 转录九问」；样例 7 换成严格版（含「删掉了什么」清单） |
| `scripts/cpms_content_audit.py` | 新增：`--cpms <ERP.json> --source <客户文件>`，退出码 = **无出处条目数**，必须 0；同时报「客户有、我们没写」的漏抄项 |
| `tests/test_cpms_content_audit.py` | 新增 11 项：编造数字/料号必须被抓、计数词按数字溯源、数字与单位分处两格可溯源、`gsm≡g`、`UV/0C` 不算料号 |
| `references/cpms-real-sample-bjqx260704002.md` | 实单复盘：附件事实全集、合并版 vs 严格版、工具用法 |
| `SKILL.md` 新增 0.1 节 | **一页速查**：严格模式 7 条 + 交付四道门命令 + 产物两份文件 + 档案别硬合并近义词 |
| 两个子技能交付清单 | IGL 第 8 节 / NGV 第 10 节各加 3 条：严格模式、溯源审计 0、产物留来源对照 |

实测：本单严格版 **0 无出处 / 0 漏抄**；合并版会被抓出 8 处无出处。

### 1.0.31 详细变更 —— 真实确认单当参照，补「内容完整性」

业务 2026-10-19：给真实 ERP 单据的目的**不是核对字段口径**，而是让 agent 对照人写的 `CPMS`
学「写什么」（原单 `BDBH=BJXQ260704002`，线圈书 + 钻石贴纸装配）。

落地：`docs/CPMS_规范与样例集.md` 新增样例 7 + 5.1 节「内容完整性九问」；
`tests/test_cpms_doc_samples.py` 样例数 6 → 7；10 条内容维度差异与交付形状的复盘见
`references/cpms-real-sample-bjqx260704002.md`；机械自检脚本 `scripts/cpms_content_audit.py`。
同批还修了一处真错：`Wire-O` 被映射到 26.01 普通蛇圈书，真实是 **26.07 普通YO书**（Wire-O = 双线圈）。

### 1.0.30 详细变更 —— CPMS 规范 v2（业务拍板 5 条）

业务 2026-10-19 对 `docs/CPMS_写作规范.md` 末尾挂着的 5 条待确认项逐条给定，全部落地。
这次没有一条是「改建议」—— 都是把原来「两者都接受」的地方**收成一个值**。

| # | 业务原话 | 落地 | 谁读 |
|---|---|---|---|
| 1 | 套装多件用「共**N件**」 | 概要行正则接受 `款/件`；`_is_set()` 判形态：套装写「款」、书本写「件」都报 `CPMS011`；套装 ≥2 件时**必须**写件数，且必须等于组成声明的**实物件数**（每项开头数量之和，没写算 1 件：`陈列盒+4款卡片+信封+自粘袋` = 7 件） | `cpms_lint.py` |
| 2 | 印色补 C 需要自动修复 | `normalize_text()` 新增 `_fix_ink_slot()`：**部件行（order 100~299）+ 整槽**才补 C（`4/0c → 4C/0C`、`4c+光油/0c → 4C+光油/0C`）；`备注`/`请拆分` 段与 `1/2` 这类比例不碰 | `--fix` |
| 3 | 备注里保留变更出处/人名 | 不新增规则，`备注` 的 `notes` 写明「一行、允许出处/人名、仍禁 `【姓名 日期】` 块」；`CPMS007` 照旧拦讨论块 | 规范 + linter 不变 |
| 4 | 灰板写 g | 新增 `CPMS054`（warn）：`2.5mm灰板` → 应写 `1500g灰板`；换算表 `600g=1mm / 900g=1.5mm / 1200g=2mm / 1500g=2.5mm / 1800g=3mm`（与 `page_thickness.xlsx` 一致，实测反查）。**不自动换算** —— 属材料语义，只提醒 | `cpms_lint.py` + 规范 |
| 5 | `装箱：待确认` 能交付 | 规范 `required.pending_ok` 写明可交付；仍是**唯一**允许「待确认」的位置，其余必填段缺失照样 `CPMS040` error | 规范（linter 行为不变） |

**规范版本**：`assets/cpms_spec.yaml` `version: 1 → 2`，`meta.business_decisions` 记录这 5 条原文口径。

**连带改动**

- 6 份样例（`docs/CPMS_规范与样例集.md`）同步：灰板 `2.5mm/2mm/1.5mm` → `1500g/1200g/900g`；套装样例概要行补 `共【4】件`。
- `docs/CPMS_写作规范.md` 的「待业务确认」改成「已拍板（表格）」；3 个真实单据改写里的 `2mm/2.5mm灰板` 一并换成 g。
- 测试 **323 → 332 项**（`test_cpms_lint.py` +9：印色整槽修补与比例保护、套装款/件、实物件数求和、件数与组成声明对账、漏写件数、无声明不强制、书本不许写件、灰板 mm 告警）；
  `--fix` 那条老测试从「印色保持原样」翻成「印色必须补好且修完合规」。

> ⚠️ 一个刻意的边界：**灰板 mm→g 不做自动修复**。`--fix` 的口径是「机械、无语义」，而厚度换克重是材料语义（还要判断是哪种灰板）；
> 表外厚度更不该猜。所以只报 `CPMS054`，由 agent 按表改。

### 1.0.27 详细变更 —— 排版重做与导出 PDF

业务 2026-09-24（第七批）：

> 「我需要你对页面进行重新排版，每个字段单独成一行不方便阅读，其次添加一个导出为 PDF 的按钮」

**1) 排版：一行一个 → 自适应网格**

| | 之前（1.0.24–1.0.26） | 现在 |
|---|---|---|
| 字段容器 | 无（直接平铺 `<div>`） | `.field-grid`：`repeat(auto-fill, minmax(290px,1fr))` |
| 标签 | 上下两行（中文名 / 代码），占 190px 宽 | 同一行并列，宽度自适应 |
| 长值 | 跟短值一样占整行 | `.field.wide` → `grid-column: 1/-1` 跨整行 |
| 宽窄判据 | — | `fieldIsWide(key, value)`：长文本 / >40 字 / 带校验原因（**按值，不按字段名**） |
| 63 字段滚动长度 | ~2800px | ~1/3（1720px 窗口下 4 列，只有 6 个字段跨行） |

**2) 导出 PDF：`window.print()` + `@media print`（零依赖）**

刻意**不引** PDF 库：这页面必须离线可用（公司内网/断网），引 CDN 会白屏；而且引库等于再抄一份排版，
跟屏幕上的必然漂。所以 PDF = 同一套 DOM + 一层打印覆盖规则：

- `@page { size: A4; margin: 12mm }`，字段固定 3 列，`.field` 加 `break-inside: avoid` 防切断
- 隐藏侧栏 / 工具栏 / 原始 JSON / 字典合法取值清单（查询参考，不属于报价内容）
- `pre.long` 解除 190px 限高 → `CPMS` 长文本**全量**进 PDF
- 页眉的标题、`Reminder` 横幅、「未完成 · 不得交付」横幅**保留**（打印件也要能看见待办）
- `syncTitle()` 让 `document.title` = `ERP · 报价需求单导出查看器 · <任务号前8位> · <中文品名>`
  —— 浏览器拿它当「另存为 PDF」的默认文件名，否则每份 PDF 都叫同一个名字

**3) 测试 268 → 274 项**（`test_viewer.py` +6：网格排版 / 三个容器都用网格 / PDF 按钮 /
打印样式 / 不许引外部库 / 标题随产物走）

另外用 jsdom 实跑页面（服务端渲染的那一份）加了两套检查：

| 脚本 | 断言 | 结果 |
|------|------|------|
| `/tmp/erp-viewer-check/check-layout.js` | 63/63 字段一个没丢、`.field-grid` 13 个、CPMS/CPMC_ZH 带 `.wide` 而 TZS 不带、跨行只 6/63、标签压一行、**点「导出 PDF」真的调了 `window.print()`**（stub 后点一次）、`document.title` 带任务号且无非法字符 | **22/22** |
| `/tmp/erp-viewer-check/check-css.js` | 用 CSSOM **真解析**样式表：`@media print` 块解析出 13 条规则，含 `aside.list`/`.toolbar`/`pre.raw`/`details.legal`/`pre.long`/`.field`/`.field-grid` | 全过 |

**这一轮的工具局限（如实记下）**

- **没有真浏览器**：playwright 的 chromium 在（`~/.cache/ms-playwright/chromium-1223`）但缺
  `libnspr4`/`libnss3`/`libatk`/`libcups`/`libgbm`/`libpango` 等十多条系统库，本机**无 root**
  装不了（试过 `apt-get download` 抽 .so 塞 `LD_LIBRARY_PATH`，libnss3 在镜像里 404，放弃）。
  → 所以**像素级的版面与真实打印输出没验过**；验到的是结构（DOM/类名/事件/CSSOM 解析）。
  真要看效果：`uv run python -m erp_view` 打开点「导出 PDF」。
- **jsdom 的 cssom 不建模 `@page` 的 `size`**（`cssText` 只剩 `margin: 12mm`）——
  这一项退回模板原文校验，不是样式的问题。

**又踩了一次同类错误**：「不许引 PDF 库」的第一版测试写成**禁词表**（`assert "jsPDF" not in html`），
结果被我自己注释里提到的库名绊倒（跟 1.0.26 模板注释里写双花括号一模一样）。
已改成检查**加载机制**（`<script src>` / `import` / `require` / CDN 域名）——
**拿词表当代理，代替不了真检查**。


### 1.0.28 详细变更 —— CPMS 写法定死（18476 份实测驱动）

业务 2026-10-17：

> 「路径下是我从 ERP 拉取的近 4 年的客户报价需求单的 json 文件，我需要你学习里面 `CPMS` 的写法，
> 因为有非常多的人编写，所以格式也会不一样，第一步我需要你重新设计 `CPMS` 的写法」

**做法：先量，再定。** 语料 `/home/claw/train/custom_cpms_requie`：**18476 份**（2021-08 起），
键集 100% 一致（59 键），81 份 CPMS 为空。统计结论：

| 发现 | 数据 | 对设计的含义 |
|---|---|---|
| 「书籍方向：【X】产品规格：【a*b*c】共【N】款」开头 | **91.2%** | 不是推倒重来，是把多数人已在用的写法钉死 |
| 概要行 ↔ `Drop_SJFX` / `TZS` | **100% / 100%**（0 例外） | 概要行是**派生数据**，必须由字段算，不许手写 |
| 概要行尺寸 ↔ `CD/KD/GD` | 98.1%（352 份差异全是取整） | 保留精确值；`CD/KD` 整数化造成的差是**允许且明确**的 |
| 段落主干 封面→内文→(夹层)→装订→包装→装箱 | 转移频次最高链 | 顺序照这个钉，只修 包装/装箱 摇摆 |
| 不同行首标签 | **3176 种** | 必须给闭集词表 + 同义收敛 |
| 混入【姓名 日期】产品补充描述 | **49.6%** | 硬规矩：只写当前生效状态 |
| `=====` 分隔线 | **49.4%** | 同上 |
| 四段齐全（封面/内文/装订/装箱） | **53.9%** | 按产品形态定必填段 |
| 空标签（只有「封面」没值） | **28.6%**（5289 处） | 空标签 = error，推不出来整行不写 |
| 段序错乱 | **25.0%** | `order` 检查 |
| 印色写法不统一 | **72.4%**（`4/0c` `4C/0C` `4c+光油/0c` …） | 定 `{n}C(+工艺)*/{m}C` |
| 克重 `g`/`gsm`/`克` | 11831 / 3769 / 2538 | 定 `g` |
| CR/CRLF | **99.6%** | 只允许 LF |
| 长度 p50 / p90 / p99 / max | 510 / 1083 / 2149 / 13537 | 超 4000 字基本混了变更过程 |
| **合规度** | **0 / 18476 完全合规**，中位 5 个 error，最多 143 | 这就是要重设计的量化理由 |

**落地四件套**

| 文件 | 作用 |
|---|---|
| `assets/cpms_spec.yaml` | **机器可读真源**：骨架（46 段 + `order`）、必填段、五槽位语法、闭集词表（纸名/表面处理/装订）、同义收敛、19 个规则码。`order` 相同 = 同级（如 `陈列盒`/`内托`），先后合法 |
| `docs/CPMS_写作规范.md` | **人读**：诊断数据、骨架表、槽位与单位硬规矩、六条硬规矩、**3 个真实单据的逐行改写**（套装多件 / 多款烫金 / 含内部讨论块）、落地方式与 5 条待业务确认 |
| `scripts/cpms_lint.py` | 校验 + `--fix` + `--vocab` + `--json`；退出码 = error 数，可当门禁 |
| `tests/test_cpms_lint.py` | **36 项**：合规样例自证零违规、19 个规则码逐个真触发、概要行与字段对账、`--fix` 不越界（**印色不许自动补 C**）、CLI |

**几个刻意的取舍**

1. **不加分组标题**（`【部件】`/`【包装运输】`）。实测 0% 用过，扁平「标签：值」已可 100% 解析，
   加标题只是多打字。结构靠**固定顺序 + 固定词表**表达。
2. **`--fix` 不补印色里的 C**（`4/0c → 4C/0C`）。这在打印语境下几乎无疑义，但脱离语境就有歧义
   （`1/2` 可能是比例），属于「宁可漏修，不许猜」。印色只报 WARN 带建议。
3. **必填段允许写「待确认」**，其余段落推不出来就整行不写 —— 否则会被逼着写空标签（就是那 5289 处的来源）。
4. **灰板 g 与 mm 都接受**。客户原文写 `1800g灰板` 就保留，换算成 mm 是猜。
5. **`order` 相同 = 同级**，而不是给 `陈列盒`/`内托` 硬排先后 —— 内托常在盒里，谁先写都合理。

**实测回归**

- 三份改写样例（`BJXQ220312001` / `BJXQ240405003` / `BJXQ250322003-02`）跑 `cpms_lint` **0 error / 0 warn**。
- 全量 18476 份跑一遍：中位 5 error；`--fix` 后中位 3 error、**13.7% 直接合规** ——
  剩下 86.3% 是「印色写法 / 词表外标签 / 内部讨论块 / 空标签 / 段序」，只能改写法。
- 测试 **274 → 310 项全绿**。


### 1.0.26 详细变更 —— 查看器包化 + Flask 重写

业务 2026-09-24（第六批）：

> 「将 `erp_viewer.py` 创建一个 `erp_view` 文件夹，然后使用 flask 重新编写」

**做法：整块搬进包，顺手把三层拆开（不是把 `http.server` 换成 Flask 就完事）**

| 之前（1.0.24 起） | 现在（1.0.26） |
|------------------|--------------|
| `scripts/erp_viewer.py`（501 行，逻辑 + HTTP 混在一起） | `erp_view/data.py`（逻辑，**不 import Flask**）+ `erp_view/app.py`（工厂 + 4 路由 + CLI） |
| `scripts/ERP_data_show.html`（静态文件，`read_bytes()` 送出） | `erp_view/templates/index.html`（`render_template`，**刻意零 Jinja 语法**） |
| `global PROCESSED_DIR`，`--dir` 改全局 | `app.config["PROCESSED_DIR"]`，`create_app(processed_dir=…)` 显式传 |
| 测试：每个 HTTP 用例真起 `ThreadingHTTPServer` + 抢端口 | 测试：`app.test_client()`（WSGI 直连）；**真 socket 另有一条**兜底 |
| `json.dumps(..., ensure_ascii=False, indent=2)` | `PrettyJSONProvider`（`ensure_ascii=False` / `sort_keys=False` / `compact=False`） |
| 入口：`uv run ./scripts/erp_viewer.py` | `uv run python -m erp_view`（`scripts/erp_viewer.py` 保留为**转发壳**，老命令照旧） |

**为什么保留那个转发壳**：SKILL 与 `customers/README.md` 里 6 处命令都写着它，业务也按这个敲；
壳子里**没有一行逻辑**（测试断言它不含 `delivery_gates`/`scan_exports`/`FIELD_GROUPS`/`http.server`
且 < 40 行），不存在两份实现漂移的问题。

**新增依赖**：`flask 3.1.3`（`uv add flask`，连带 blinker / click / itsdangerous / jinja2 / markupsafe / werkzeug）。
这是本仓库第一个 Web 框架依赖 —— 之前查看器刻意用标准库，是为了**零依赖**；业务明确要求 Flask 后，
「零依赖」这条约束转移到**页面**上（页面仍然零 CDN、离线可用，`tests/test_viewer.py` 钉着）。

**踩到的两个坑（都当场被测试拦住）**

1. ★ **模板注释里写出了双花括号**：给模板加「不许出现 Jinja 语法」这条说明时，把符号本身写进去了 ——
   Jinja 会把它当空表达式（渲染直接报错）。`test_模板里不许出现Jinja语法` 立刻红。已改成文字描述。
2. ★ **`app.json.indent` 在 Flask 2.3 起不存在**（`compact` 也改语义了）：先写了 `dumps()` 覆写，
   读源码后发现正路是 `compact = False` —— Flask 自己就会 `indent=2`，不用自己接管序列化。
   另外默认 provider 会 `sort_keys=True` 按字母重排键、`ensure_ascii=True` 转义中文，
   都得显式关掉（否则 `curl` 直读全是 `\uXXXX`、键序跟业务顺序对不上）。

**回归**：测试 **259 → 268 项**（`test_viewer.py` +9：真服务器连通性、两个应用扫描根互不干扰、
`data.py` 不依赖 Flask、转发壳无逻辑、模板无 Jinja 语法、JSON 口径、`python -m erp_view --help`、
缓存头、端口占用中文提示）。
另用 jsdom 对着**Flask 后端 + Jinja 渲染出来的页面**重跑交付闸门检查（`/tmp/erp-viewer-check/check-gate.js`）
**18/18 通过** —— 页面改成模板后渲染没走形。

> ⚠️ 顺带记一笔：检查脚本原来读本地 html 文件，搬包后路径失效（`ENOENT`）。
> 已改成**从服务端取页面**（`GET /`）—— 这样测的是 Jinja 的真实渲染结果，比读文件更准。


### 1.0.25 详细变更 —— 交付闸门（未完成产物不许留在 processed/）

业务 2026-09-24（第五批）：

> 「为什么你导出的 json 里会缺少 `CPMS`？你应该严格按照技能的流程去执行」

**查出来的事实**：交付物本身没问题（`3cf2194b…` 的 `CPMS` 2128 字、`CPMC_ZH` 已补中文品名、
63 键顺序断言通过、`--check --customer ngv` 63 字段全过）。**问题在 `processed/` 里多了一份**
`2302b811…` —— `CPMS=""`、`CPMC_ZH` 还是 `【中文品名待定】`、63 键、`version: 2`。

它是**拿语料库样本试跑留下的**：`main.py` 直接喂
`customer-files/samples/ngv/香港单号 CU26072301 …xlsx`，没把归档目录指到临时区。日志里两句自证：

```
07:31:15 [WARN] 文件不在 pending 目录，可能已被处理: customer-files/samples/ngv/…
07:31:16 [WARN] CPMS 留空 —— 脚本不生成产品描述，必须由 agent 亲自补写
```

脚本**当场 WARN 了两次**，我还是跑完了 → ①样本被移出语料库（后来从任务目录拷回，md5 一致，
无损失）②`processed/` 里躺下一份「未完成」产物，直到业务问才暴露。
**踩的是 §10 已经写明的坑，第二次。**

**落地三件事**

| 项 | 内容 |
|----|------|
| ① 交付闸门（机制） | `erp_viewer.py: delivery_gates(data)` —— `CPMS` 空/缺失/纯空白、或 `CPMC_ZH` 含占位符 `【中文品名待定】` → 返回原因列表。`/api/exports` 每项带 `incomplete`（空 = 可交付）+ `cpms_len`；`/api/export` 也带一份。页面：列表红徽章**「未完成」**（tooltip 列出原因）、详情页顶部横幅**「❗ 未完成 · 不得交付」**。判定**只在服务端算一份**，页面只显示（同 §4.10 设计 5 的枚举校验口径） |
| ② 语料库硬闸 | `main.py: sample_run_guard()` —— 输入在 `customer-files/samples/` 下、而 `--processed-dir` / `--failed-dir` 没**都**指到语料库之外 → **拒绝执行**（退出码 1）并打出可照抄的命令。从 WARN 升级为硬闸的理由：语料库是 gitignored 的**真实客户原文**，移走 = 永久丢失；而这个 WARN 已经拦不住人两次了 |
| ③ 状态清理 | `2302b811…` 从 `processed/` 移到 `/tmp/cqr_scratch/`（它是样本试跑的副产物，不是交付物，一条命令可复现）；`processed/` 现在只剩真交付（`0e39e2e2` 历史单 / `3cf2194b` 本单 / `_superseded_20260924/`）。语料库 4 份 xlsx 齐全，md5 与任务目录里的副本一致 |

**回归**：测试 **243 → 259 项**（`test_pipeline.py` +6：样本闸 5 项单测 + 1 项真跑集成，
断言「拒绝执行 + 样本仍在原处 + md5 不变」；`test_viewer.py` +10：交付闸门 9 项 + 页面显示 1 项）。
另用 jsdom 真跑页面（`/tmp/erp-viewer-check/check-gate.js`，18 项断言）：
未完成产物 → 列表红徽章 + 详情横幅（点名 CPMS 与中文品名）；真交付产物 → **无**横幅、
仍显示 `Reminder` 横幅、`CPMS` 长文本正常渲染（2148 字），零 console 错误。

> ⚠️ 写这条检查脚本时**又踩了一次「搜错范围」**：头两条断言分别搜了 `header`（CPMS 正文在字段区）
> 和 `body`（把 `<script>` 源码也算进去）—— 一个假失败、一个假通过。这已经是**同一类错误第三次**
> （前两次见 §11 的 1.0.24）。**jsdom 断言必须明确指到承载该信息的那个元素**。


### 1.0.24 详细变更 —— 配置版本 + 产物查看器重写

业务 2026-09-24（第四批）：

> ①「在 `scripts/ERP_data_show.html` 有一个旧的展示 ERP JSON 格式的 html，但是我们的导出结构
> 发生了变化，我需要你修改它」
> ②「并且让它可以自动扫描 `customer-files/processed` 的导出 ERP 文件」
> ③「给导出的 json 再添加一个字段 `version`，标识当前配置的版本，目前设置版本为 2，
> 这样方便我们的技能迭代」

**1) 配置版本 `version`（= 2）**

- 单一真源：`customers/_defaults.yaml` 顶层 `version: 2` → `CustomerProfile.version` →
  `main.py` 的 provider `config_version` → 产物内部字段 `version`（追加在最后）。
- 客户档案可覆盖（deep-merge 带上）；`validate()` 拦非整数或 <1 的值；`--show <id>` 会打印。
- 语义：**改口径就 +1**。历史产物没有该键 = 第 1 代口径。

**2) 查看器重写（`scripts/ERP_data_show.html` + 新增 `scripts/erp_viewer.py`）**

- 旧页面的字段名全是**上一代结构**（`SJFX` / `KHMBJ` / `GG_length` / `has_safety_checks` /
  `create_by` …）—— 对着新产物根本填不出来。已按 59 键 + 4 内部字段全部重写。
- **自动扫描**必须有本地 HTTP 服务（浏览器不许 `file://` 页面列目录），
  新增 `erp_viewer.py`（纯标准库）：`/` 页面、`/api/exports` 扫描、`/api/export?path=` 取单份、
  `/api/schema` 字典。`path` 做 resolve + 前缀校验，出不了 `processed/`。
- **零外部依赖**：旧页面引 unpkg(Vue3) + jsdelivr(dayjs)，断网/内网白屏 → 改纯原生 JS。
- 页面新增能力：配置版本徽章（与产物 `version` 对照）、**结构对照**（缺/多哪些键，
  历史 40 键产物也能打开）、**枚举校验标红** + 展开合法取值、`null` 与空串区分显示、
  `_superseded_*` 标记、`?path=` 深链接。
- 字段分组放在 `erp_viewer.py` 的 `FIELD_GROUPS`，并有测试断言
  「恰好覆盖 59 个字段各一次」—— 防止加了字段却漏配分组、页面上悄悄不显示。
- **取值校验只有一个实现**（同日修掉的真 bug）：页面最初自己拿值和字典 `legal` 列表比字符串，
  而 `CPLB` 我们的口径是「编号;名称」、字典里是 88 个裸名称 →
  **每一份产物的 `CPLB` 都被误标成「不在字典枚举内」**。改为服务端调用权威的
  `erp_schema.validate_result()`，`/api/export` 带回 `validation` 段（优先用产物同目录的
  当次运行报告 `*.validation.json`，没有则现算并声明「未按客户档案豁免」），页面只显示结论。
  ⚠️ 这个 bug 是**用 jsdom 真跑页面**才发现的 —— 静态看一眼不会发现，因为文案和渲染都「正常」。

**3) 测试 210 → 243 项**（新增 `tests/test_viewer.py`）。
顺手修掉一个真 bug：`scan_exports(root=PROCESSED_DIR)` 的默认参数在**定义时**就固化了，
导致 `--dir` 与测试 monkeypatch 全部失效 → 改成默认 `None`、函数内回落。

**4) 重跑上一单实单（业务要求）**

`version` 加好后，PR-00091345 那单的产物**仍是旧版**（无 `version`）——
只跑新样本做验证是不够的，交付物必须重跑：新任务 `3cf2194baaf24cc99c2ef75775d5bf05`，
59 个 ERP 字段与上一版**逐字节一致**，`version: 2`，`CPMS` 由 agent 手写补齐（2128 字），
旧产物归档到 `_superseded_20260924/afcc93fd…/`。

### 1.0.23 详细变更 —— 提醒固化 + 「无」落 `null`

业务 2026-09-24（第三批）：

> ①「（截止日期过期提醒）加一个字段做提醒」
> ②「igl 的先不做，后续补充真实的 ERP 信息再做处理」
> ③「CPSX 或者其他值的『无』使用 json 里的 null 填充」

**① 新增内部字段 `Reminder`（提醒）**

1.0.21 的过期提醒只在 `validation.json` 的 `reminders` + stdout 末尾 —— 产物里没有。
现在固化成一个字段：

| 情况 | `Reminder` |
|------|-----------|
| 有提醒（如截止日期已过期） | 文本：`报价截止日期 2026-06-26 已过期 90 天（基准日 2026-09-24） —— 请与业务确认这单是否还要报价` |
| 没有待办 | `""`（空字符串） |

结构化版本仍在 `validation.json` 的 `reminders`（`kind` / `deadline` / `days_over`），stdout 醒目块也保留。
三条通道（字段 / 报告 / 终端）同一个来源 `step6` 开头算好的 `reminders`，不会互相漂。

**② 「无」→ JSON `null`（`rules.null_values`）**

- 口径写在公共默认层：`rules.null_values: ["无"]` → **整格恰好等于**「无」的字段，产物里写 `null`。
- **为什么这是最干净的解法**：ERP 自己的导出里**空值就是 `null`**（权威导出的 `CPLD`/`KHDH`/`CPSX`
  都是 `None`）。所以「无」落 `null` 既符合业务语言，又天然属**空值**、不触发下拉枚举校验。
- **连带简化**：1.0.21 为 `CPSX=无` 加的 `rules.enum_exceptions` 声明**随之删除** ——
  现在 `CPSX` 是 `null`，校验直接过（实测 62 字段 **0 error / 0 warn**）。
  机制本身保留（将来若真要用枚举外的**字符串**值再用）。
- 只是**整格匹配**：长文本里的「无」不受影响（`客户无需提供刀模…` 原样保留）；
  `CPMS` 仍固定留空。
- 反例也钉住了：不是整格的 `无特殊要求` 不转换。

**③ `igl` 的 `CPMC` 英文表达：暂缓**

业务拍板「先不做，后续补充真实的 ERP 信息再做处理」→ igl 保持历史写法、`CPMC_ZH` 为空，
TODO 里的 P0 项措辞改成「待真实 ERP 信息」。（NGV 已迁移，不受影响。）

**实测**（PR-00091345 重出）：62 键 = 59 ERP + `ID`/`CPMC_ZH`/`Reminder`；
`CPSX = null`；`Reminder` 带「已过期 90 天」；`--check --customer ngv` → **62 字段全过（0 error / 0 warn）**；
`compare_with_erp` 白名单外差异 0 条。测试 **210 项全绿**。

### 1.0.22 详细变更 —— 内部导出字段 + `CPMC` 中英分离

业务 2026-09-24（第二批）：

> 「我们的导出现在缺少一份内部导出字段 `ID`，该字段标注我们的当前执行的任务，方便日志维护；
> 将 `CPMC` 修改为存英文表达，使用 `CPMC_ZH` 来标注中文表达。」

**① 内部导出字段机制**（新文件 `assets/internal_export_fields.yaml` + `scripts/internal_fields.py`）

| 决定 | 理由 |
|------|------|
| 内部字段**追加在 59 个 ERP 键之后** | ERP 自己也是这么干的 —— 权威导出在 `Remark3` 之后多挂 `产品备注`。59 键的位置一个不动，下游按位置读的解析器不受影响 |
| 键序断言升级为「**前 59 键 = 模板** + **其后 = 声明的内部字段（同序）**」 | 集合对、顺序错照样红。`map_fields` 多产出任何未声明的键也会当场抛错 |
| 内部字段不参与 ERP 字典校验 | 否则每份产物都会报「字典里没有这个字段」（`ID`/`CPMC_ZH` 字典里确实没有 —— 已核） |
| 新增 `--strip-internal` | 产出**纯净 59 键**，给「ERP 导入端不收未知字段」兜底（默认是追加） |

新增的两个字段：

| 键 | 值 | 用途 |
|----|-----|------|
| `ID` | 本次任务号（`uuid_utils.py` 生成） | 业务原话的「标注我们的当前执行的任务」——与**产物目录名**、**日志里的「任务ID」**、`validation.json` 的 `task_id` 是同一个 ID，产物 ↔ 日志一步对回去 |
| `CPMC_ZH` | 产品名称的中文表达 | `CPMC` 只放英文，中文表达归这里 |

**② `CPMC` 中英分离**（档案 `rules.cpmc.template` + `rules.cpmc.zh_template`）

```
CPMC     = {salesperson}/Aldi {product_name}({fsc})                        ← 纯英文
CPMC_ZH  = {salesperson}/Aldi {cn_name} {cplb_name}/{product_name}({fsc})   ← 中文表达
```

- 中文表达 = 1.0.21 之前的 `CPMC` 写法（权威导出里的历史格式），**原样迁移**，只是换了存放字段。
- 中文品名仍是**每单**由 agent 补：脚本在 `CPMC_ZH` 里填可见占位符 `【中文品名待定】` 并在日志点名。
- **硬不变量**：档案声明了 `zh_template` 的客户，`CPMC` 里出现中文/全角字符 → 当场 WARN
  （写错档案不必等 ERP 报错）。
- ⚠️ **`igl` 还没迁移**：它的 `CPMC` 历史口径是 `编号;名称-系列`（含中文类别名），
  英文版该怎么写**要业务拍板** → 已记入第 12 节 P0，不猜（该客户 `CPMC_ZH` 暂为空）。

**实测**（PR-00091345 重出）：61 键 = 59 ERP + `ID`/`CPMC_ZH`；`ID=d9ae2b3a…`（= 任务目录名）、
`CPMC=KM/Aldi Basteln&Gestalten Diamantsticker Nageldesign(FSC Mix)`、
`CPMC_ZH=KM/Aldi 钻石贴纸美甲设计 普通蛇圈书/Basteln&Gestalten Diamantsticker Nageldesign(FSC Mix)`；
校验 **0 error / 1 warn**（CPSX=无 的口径例外）；`--strip-internal` 另跑一次 → 正好 59 键。
测试 **208 项全绿**。

### 1.0.21 详细变更 —— 第三批口径（业务 2026-09-24 逐条拍板）

业务一次性给了四条，全部落地（前三条是**档案开关**，第四条是**引擎级**能力）：

| 业务原话 | 落地 |
|----------|------|
| 「业务员使用 **KM**」 | Ngv `rules.cpmc.salesperson: KM` + `main.py` 新增 `{salesperson}` 占位符。NGV 的 `CPMC` 结构确认为 `<业务员>/Aldi <中文品名>[ <中文类别>]/<英文原文>(<FSC>)`（4 份权威导出逐字比对）。**中文品名是「每单」的**（不是客户常量）→ `{cn_name}` 留空时填可见占位符 `【中文品名待定】`，并由 needs_agent 检查在日志里点名，agent 交付前直接改产物 |
| 「`KHDH`、`KHDDH` 香港单号默认是空」 | `rules.required_empty` 补 `KHDDH` —— 附件文件名带「香港单号 CU…」时 `rules.scan` 照常扫出来落库，扫不到就留空，**不算缺陷**（PR-00091345 只有 PR 号 → KHDDH 空是预期结果） |
| 「`CPSX` 如果没有获取到就显示**无**」 | 新增档案开关 `rules.cpsx.when_missing`（公共默认 `""` = 留空 + WARN；NGV 设 `无`）。**配套**：`无` 不在 ERP 下拉枚举里，若按字典硬校验会**每一单都报 1 个 error**、把「0 error」这道闸门变成噪声 → 新增 `rules.enum_exceptions`（见下行） |
| 「超过了报价截止日期，你记得提醒」 | 新增引擎能力 `check_offer_deadline()` + `validation.json` 的 **`reminders`** 段 + 跑完 stdout 末尾的醒目提醒块。判据由**基准日期**驱动：`main.py --today YYYY-MM-DD`（默认系统当天）—— 否则测试自身会随「今天几号」漂移 |

**顺带修掉一个静默丢值的真 bug**（这是本轮最值钱的一条）：

`offer deadline / Angebotsfrist` 那一行**是有值的**（`2026-06-26`），1.0.20 我却把它当成
「纯标题」塞进了 `ignored_labels` —— 结果值被静默丢掉（`extracted.json` 里根本没有
`offer_deadline`），业务要的「过期提醒」也就无从谈起。本机 5/5 份样本实测都有值：
`CU26051601-01=2026-05-26` / `CU26061701=2026-06-17` / `CU26072301=2026-07-27` /
`CU26072302=2026-07-27` / `PR-00091345=2026-06-26`。已改成 `sections.enquiry` 里的**有值标签**，
并加了两条回归（逐单断言日期 + 断言它**不在** `ignored_labels` 里）。

**新增机制：档案声明的「口径例外」`rules.enum_exceptions`**

业务拍板的取值有时就是不在 ERP 下拉枚举里（`CPSX=无`）。三种做法的取舍：

| 做法 | 问题 |
|------|------|
| 按字典硬报 error | **每一单**都红一次 → 「0 error」闸门失效，真缺陷被淹没 |
| 静默放过 | 拿业务口径当没看见，导入端是否接受「无」这件事永远没人管 |
| **档案显式声明 + 降级 WARN + 带上原因**（采纳） | `{field, value, reason}` 写在档案里，`validate_result(allow_values=…)` → WARN 并打印业务原因；**豁免只认声明过的那个值**（乱填仍报 error），不传客户档案按字典硬校验时**照样报 error**（可见性不被吞掉） |

`main.py step5b` 与 `erp_schema.py --check --customer <id>` 走**同一份**豁免，CLI 与管道不会打架。

**实测**（PR-00091345 重出）：59 键顺序断言通过、校验 **0 error / 1 warn**（那 1 warn 就是
CPSX=无 的口径例外）、`CPMC=KM/Aldi 钻石贴纸美甲设计 普通蛇圈书/Basteln&Gestalten
Diamantsticker Nageldesign(FSC Mix)`、`CPSX=无`、`reminders` 里带「已过期 90 天」。
测试 **204 项全绿**。

### 1.0.20 详细变更 —— PR-00091345 实单暴露的缺陷

用一份真实询价单（NGV `210x280 Basteln&Gestalten Diamantsticker Nageldesign`，
PR-00091345）试跑，抓出三处缺陷 —— 都是「**静默算错**」这一类最危险的：

| 项 | 缺陷 | 修复 |
|----|------|------|
| ★ 数量解析**静默算错** | `3 x 7000 cps. = 21.000 cps` → 落成 **3**（真值 21000）。旧实现只认整串 `^N x M$`，这种带「= 总数」的写法匹配失败后退回「取第一个数字」 | `parse_single_quantity()` 重写为三段：① `=` 后的**显式总数** → ② `N x M` 乘法 → ③ **仅当整串只有一个数字**才取它。①② 不一致 → WARN 并以总数为准；③ 不成立时**宁可留 0 报 WARN，绝不取第一个数字**。`_de_number()` 同时支持英文千分位（`17,500` → 17500） |
| ★ `extras_found` 被**纯标签**污染 | NGV 的栏目标题（`TPS\nFormat`、`back\nRücken`…）登记在 `extras:` 里，于是它们同时出现在「CPMS 素材」清单（看起来像漏了信息）并被校准报告当作附加信息掩盖 | 新增档案段落 **`ignored_labels:`** —— 纯标题（不含值）：既不算「认不出的标签」，也不进 `extras_found`。判定「块内每一行都是标题」才算，避免误杀「标签 + 同行值」。NGV 的 35 条标签从 `extras:` 迁出，`extras: []`（该表单没有附加信息栏位） |
| ★ 装订新写法缺失 | `Wire-O Binding` 命中不了 `binding.map` → `ZDFS` 空（warn） | 补 `wire-o / wire o / wiro / spiral / coil` → **`线圈书`**（ERP 合法值；注意不是 CPLB 树里的「蛇圈书」） |
| 顺手加固 | `erp_schema.py --check <json>` 不知道客户档案的必填豁免，NGV 的输出每次都被误报 `KHDH` error | `--check` 新增 `--customer <id>`，按 `rules.required_empty` 豁免（`--check x.json --customer ngv` → 59 字段全过） |
| 顺手加固 | 脚本跑完的「待 agent 亲自完成」提示只列 `extras_found`；分区表单客户（`extras` 为空）看不到「原文还有长段规格要翻译」的信号 | 该提示新增**长文本规范键**清单（≥80 字符，如 `specials`），明确指向 `extracted.json` 的 `values` |
| 实测结论 | 该单：59 键顺序断言通过、校验 **0 error / 0 warn**；`CPLB=26.01;普通蛇圈书`、`ZDFS=线圈书`、`CPSL1=21000`、`Drop_SJFX=长书脊`、`FSCMC=FSC Mix`；`CPMS`/`CPMC` 由 agent 手写 | 学习点已回写子技能 `customer-quote-ngv` 第 3.1 节（第 5 单实测）与第 9 节坑 |
| 测试 | 新增 5 项（数量三种写法 + 不一致告警 + 纯标签语义 + 实单 extras 回归）→ 累计 **194 项全绿** |

### 1.0.19 详细变更 —— 技能按客户拆分（总纲 + 两个子技能）

**动机**（业务 2026-09-24）：igl 与 ngv 的报价提取放在一个技能里不好维护 ——
两份 SKILL.md 混着总纲、igl 细节、NGV 细节，改一家要在 1300 行里翻。

**做法**：**引擎零重复**，只拆「客户专属知识」这一层。

| 技能 | 内容 | 仓库源 |
|------|------|--------|
| `customer-quote-request`（总纲） | 架构 / 抽取引擎（两种范式）/ 邮件入口 / 字段总览 / **`CPMS` 规则** / 校验方法 / 新增客户 SOP / 客户偏好表机制 / 陷阱 / 版本历史 | `SKILL.md` |
| `customer-quote-igl`（子技能） | IGL 附件形态（平铺 + 横向键值对）· 识别特征 · 偏好表 6 条 · 字段来源 · 与 `tests/test_pipeline.py` 一致的**逐字段基线** · 坑 · 交付检查清单 | `skills/customer-quote-igl/SKILL.md` |
| `customer-quote-ngv`（子技能） | NGV 附件形态（`A1:K32` 分区表单结构图 + 五个坑）· 识别与文件名撞车史 · 4 单确认值 · 两批 15 条口径 · 单号分流 · `CPMC`/`Remark4` 人工环节 · 语料库与对照命令 · 交付检查清单 | `skills/customer-quote-ngv/SKILL.md` |

**同时**：

- 总纲把 NGV 专属段（原 2.6 / 2.6.1 / 2.6.2 / 2.6.3 共 ~5.9KB）移出，改成 2.6「客户专属知识在
  子技能里」的**路由表** + 引擎级说明；处理文件时的顺序写成「读总纲 → 看识别结果 →
  加载对应子技能 → 按它的口径/检查清单干活」。
- 三个技能都是**软链接**指向仓库（仓库是唯一源）；体检 cron 同步覆盖三个。
- 新增 `tests/test_skill_docs.py`：档案 ↔ 子技能一一对应、frontmatter 合法、
  回指总纲、总纲路由表不漏客户 —— 以后加客户忘了写子技能会直接红。
- 铁律新增一条：**客户专属内容不许写回总纲**（否则总纲会重新长成第二个客户档案）。

### 1.0.18 详细变更 —— 第二批口径拍板落地

业务把 1.0.17 末尾挂着的 **10 条 P0 待拍板项逐条给定**，全部落地为**档案开关**（不写死代码）：

| 项 | 变更 |
|----|------|
| ★ 客户偏好表 `preferences:` | 新档案段落 + `customer_profile.py --prefs`（CLI）：把「这个客户哪些字段固定填什么、哪些就该空着」连同**依据**写在一处（业务原话：**喜爱表**，方便快速填写用户习惯）。口径只有两种：写值 → 落库；写空 → **显式留空**。与 `erp_fixed` 的一致性由 `validate()` + 4 项测试拦着（防「文档说留空、代码里还填着 1」） |
| ★ 显式留空机制 | `main.py` 的 `erp_fixed` 循环新增：值为 `""` 时**真的把模板默认值清掉**并对字段记账（`PXH` 模板默认 `1`，不显式清就会悄悄留在产物里）。日志明确打「显式留空」「空值即正确」 |
| ★ `rules.scan` 新增 `unique: true` | **不唯一就留空**：全文扫描该正则的所有命中，去重后只剩一个才落值；多个不同取值时不落值并把原因记进 `.extracted.json` 的 `scan_notes`（`main.py` 在 FSCMC 段把它打出来，不静默）。`rules.scan` 从 `re.search` 改成 `re.findall`（取第一个命中，语义不变） |
| ★ `rules.versions.when_missing` | 提取不到语言数时落 **0**（NGV）：邮件可能后补而附件未更新，0 是「待业务核对」信号。与「单语言留空」（`empty_when_single`）严格区分 —— 三态：有语言数 / 单语言空 / 提取不到落 0 |
| 逐条口径落地 | `CPLB` 输出「编号;名称」（业务定）；`BZ` 我们填「美元」、`CPDL` 我们填「书版」；`PXH` 显式留空；`SFYPJ`/`Remark1` 从 `erp_fixed` **删掉写死的「否」**（CU26072301 有配件，写死是错的）；`Remark3` 留空（第一版报价无修改记录）；`FSCMC` 不唯一留空；`YSLX` 默认留空 + 走偏好表；`CPSL2~10`/`KHMBDJ` 落 0 已确认可接受 |
| `ZBLX` 枚举确认 | 业务给出 `txtZBLX` 完整 HTML —— 与字典转录的 6 项**完全一致** → `B3B普通芯`（4 份导出里 3 份用它）确实不在枚举内，属 ERP 侧数据。`observed_values` 备注更新，测试加断言钉住 6 项枚举 |
| ★ 新增对照工具 `scripts/compare_with_erp.py` | 拿附件跑一遍管道与权威 ERP 导出**逐字段分类对照**（口径已定 / 0 与空 / **需要核对** / ERP 侧多出的键），退出码 = 白名单外差异数（可当门禁）。口径白名单落在 `customers/<id>.erp_diff.json`（NGV 已有）。之前是临时脚本，现已入仓 —— 校准新客户直接用它 |
| 测试 | 新增 8 项（唯一性扫描 3 态 / YYS 三态 / PXH 显式留空 / 配件与修改记录留空 / 偏好表一致性 4 项 / ZBLX 枚举）→ 累计 **179 项全绿** |
| 实测 | 4 份真实附件全部 0 error / 0 warn；`compare_with_erp.py` 报**白名单外差异 0 条** —— 业务拍板的口径全部生效；igl 回归无变化 |
| 教训固化 | 「**每个客户的附件类型不一样、格式也不一样**」（业务 2026-09-24 强调）→ 已写进第 0 节铁律与新增客户 SOP：判形态（平铺 / 分区表）→ 校准 → 填档案，绝不套用上一个客户的结构 |

### 1.0.17 详细变更 —— NGV 口径落地与识别 bug

业务给了修正后的 4 份 ERP 导出（每单对应自己那份）与 4 条口径，逐条落地：

| 项 | 变更 |
|----|------|
| ★ 修客户识别**真 bug** | 附表文件名含 `Colour` → 命中 igl 的 `filename_keywords`（10 分）就胜出，正文里 NGV 的 500 分特征**从未被读过**，整单字段全错（`CPLB`=32.00;其他、`erp_fixed` 全丢、日期按公共默认算）。现在表格类文件**一律把正文一起送进识别**（`customer_profile.collect_probe_texts()`），文件名只当加分项；得分 <30 打 WARN（弱证据）。回归：`test_识别必须看正文_文件名撞车也不许判错` |
| ★ `KHDH` / `KHDDH` **拆开** | 两键只差一个字母，此前把附件的「香港单号 CU…」写进 `KHDH`，而 `KHDDH`（第 43 键，标签就是「香港单号」）**永远是空的**。4 份导出：`KHDDH`=单号、`KHDH` 全空。档案 `email:` 段改按 **`target` 声明多组规则**（`profile.number_rules()`），`ingest_email.py` 新增 `extract_numbers()` 与 sidecar 的 `numbers`/`number_sources` |
| ★ 套装尺寸 0/0/0 | 新档案开关 `rules.size.zero_tops` —— 套装（盒子）录系统尺寸一律 0/0/0，附件里的盒内单件 TPS 不采用（业务决定） |
| ★ 日期不落库 | 新档案开关 `dates.<字段>.mode: empty` —— NGV 的 `JHRQ`/`WCRQ` 都留空（4 份导出全空，即使附件给了 ETA） |
| ★ 单语言不落库 | 新档案开关 `versions.empty_when_single: true` —— 附件写 `1 language` 时 `YYS` 留空而不是 1（两份单语言导出的 YYS 都是空的） |
| ★ `ZBLX` 默认留空 | 从 `ngv.yaml` 的 `erp_fixed` 删掉「B3B普通芯」—— 导出里 3 份有 1 份空，属 ERP/业务侧手工填，不由我们产出 |
| ★ 必填豁免 | 新档案开关 `rules.required_empty` —— `erp_schema.validate_result(result, allow_empty=...)`。NGV 豁免 `KHDH`（报价阶段取不到）与 `Drop_SJFX`（套装无书脊方向），否则每单报假 error 淹没真问题 |
| 修正则误捕 | 客户单号正则 `\b(?:po|order)\s*(?:no)?\s*([A-Z0-9]{5,})` 把 `Postkarten` 的 `Po` 抓成 `stkarten` → 必须出现 `no/nr/number/#` 编号词。回归：`test_客户单号正则不许被单词前缀误捕` |
| 语料库换血 | `customer-files/samples/ngv/*.erp.json` 换成修正版导出；被替换的旧文件归档到 `_superseded_20260924/`；`manifest.json` 重写成「已知差异分类表」（设计如此 / 口径待定 / 附件取不到需邮件 / ERP 导出侧） |
| 测试 | 新增 9 项（识别回归、单号分流、正则误捕、套装尺寸、日期与 `ZBLX` 留空、扫描键改名、档案规则声明）→ 累计 **171 项全绿** |
| 逐字段核对结果 | 4 份附件的 59 键顺序断言通过、校验 **0 error / 0 warn**。剩余差异全部归入四类（见 `manifest.json`）：设计如此（`CPMS`/`CPMC`/ERP 自动带出/`ZBLX`）、口径待定（`CPLB` 格式、`BZ`/`PXH`/`CPDL`、0 vs 空）、附件取不到需邮件（`YYS`=3、`CPSL1`=193820、`YSLX`、`Remark3`、`SFYPJ`、`FSCMC`、`KHDDH`）、ERP 导出侧（`KHCPMC` 键、多出的「产品备注」） |

### 1.0.16 详细变更 —— NGV 校准（分区表单范式）

| 项 | 变更 |
|----|------|
| ★ 新布局策略 `section_pairs` | 应对「A 列分区 + B 列标签 + C 列起值」的表单。**按行位置对齐**档案 `sections:` 声明的条目；`section_value` 取 B 列自身（合并格即值），`label` 取右侧（`value_mode: first`/`run`）。声明的 `label` 兼作**断言**，结构变了就打 WARN 并记 `unmatched_labels`，宁可空着不猜 |
| ★ 新档案段落 `sections:` | `customers/<id>.yaml` 里按表单行序声明 `{ section_value }` / `{ label, key }`；分区名与标签都支持双语别名。`customer_profile.py` 新增 `match_section()` + 结构校验（未知规范键、缺 label、entries 为空都报错） |
| ★ 新档案段落 `rules.scan` | 全表正则扫描：值不挂任何标签、只在某处出现一次时用。NGV 用它取 `our_ref`(PR-xxxxx) / `fsc` / `text_thickness`(mm bookblock) / `binding_side`(mm side) / `khdh`(**文件名**里的「香港单号 CU26072301」)。扫描 haystack 含单格 + 整行拼接 + 文件名 |
| ★ NGV 档案转正 | `customers/ngv.yaml` 从占位 draft 改成 **calibrated**：10 个分区、43 个规范键、80 条别名；`quantity.mode: single`、`versions` 两个正则、`thickness.enabled: false`、`spine.mode: binding_side`、`unit`、`cpmc` 模板、`binding.match_keys`、`cplb.map`（24.03 / 23.06 / 29.04）、`erp_fixed`（含 378 字符的客户常量 `Remark4`，逐字照抄 ERP 原文，含其错别字） |
| ★ 映射层新增能力（均档案驱动，其他客户不受影响） | `parse_single_quantity()`（德式千分位 `4 x 17.500` → 70000）；`TZS`/`YYS` 从 `versions` 提取；`CPMC` 改模板拼装（`rules.cpmc.template` + `needs_agent`）；`DWMC` 按 `CPLB` 顶层定「本/套」（`rules.unit`）；`FSCMC` 由 `rules.scan` 的 `fsc` 落地；`Drop_SJFX` 支持 `binding_side` 模式 + `skip_tops`；`rules.thickness.enabled` 可关掉厚度推算 |
| ★ 修 3 个真 bug | ① `CustomerData.get()` 用 `values.get(key, default)` 导致 **`scanned` 永远读不到**（KHDH/FSCMC 明明扫到却是空串）；② NGV 附件没有单号时**拿 `our_ref`(PR-xxxxx) 冒充客户单号**（把 CU26061701 填成 PR-00081126）→ 档案 `attachment_keys` 只留 `khdh`；③ 档案 YAML **重复键静默丢失** → 新增 `_UniqueKeyLoader`，重复键直接报错 |
| ★ 字典两条降级规则 | 「实测值」（在真实导出里出现但不在表单静态枚举里，如 `ZBLX=B3B普通芯`）与「纯名称 CPLB」（真实导出存 `其它套装`，我们输出 `29.04;其它套装`）从 error 降为 **WARN** —— 既不误报成缺陷，也让待确认项一直可见；明确非法的值仍是 error |
| 回归语料库 | `customer-files/samples/ngv/`（4 份真实附件 + ERP 导出 + `manifest.json` 说明对应关系；客户原文，已被 `.gitignore` 排除） |
| 测试 | 新增 `tests/test_ngv_profile.py`（合成用例锁 `section_pairs` 机制 + 数量解析 + 扫描 + 真实样本回归）；累计 **165 项全绿**（1.0.17 增至 171） |
| 实测 | 4 份真实附件全部跑通：59 键顺序断言通过、校验 0 error；`CPLB` 24.03 / 29.04 / 29.04 / 23.06；`CPSL1` 0 / 512580 / 70000 / 70000；`Drop_SJFX` 长书脊 / 空 / 空 / 长书脊；`DWMC` 本 / 套 / 套 / 本 —— 逐条与 ERP 导出核对。**另发现上游 2 份 ERP 导出装错**，详见第 12 节 P0 |
| 文档 | 新增 2.6 节（分区表单范式：结构图 + 五个坑 + 新增同类客户的做法）；4.2 表格补齐 8 个字段的新来源；4.4 改写成两种数量口径；第 12 节新增「NGV 校准新发现的待确认项」11 条 |

### 1.0.15 详细变更 —— 产品类别树重设计 + 四条决策

| 项 | 变更 |
|----|------|
| ★ 产品类别树重设计 | 新增 `scripts/cplb_tree.py` + `assets/cplb_tree.yaml`（88 节点，11 顶层 + 77 子类）+ `assets/cplb_tree.html`（可视化选取器）。编号一个没改，只加导航层：按「成品形态 → 装订 → 封面灰板 → 内文工艺」重组，附业务定义（源表 `REMARK`）、结构化属性、中英文/行话别名、常用速选、6 条决策路径 |
| ★ `CPLB` 解析改档案驱动 | `main.py` 新增 `resolve_cplb()`：档案 `cplb.map`（正则 → 节点编号）优先 → 旧查表兜底 → 档案兜底值；**任何编号都必须存在于树中**，否则 WARN 降级。`product_category.key`（死配置）删除，`fallback` 由废弃的 `99.99;其他` 改为树里的 `32.00;其他` |
| ★ `CPLB` 校验加强 | `erp_schema` 从树读取节点（`tree_source`）；校验编号是否为树节点，只到顶层（`23.00`）给 WARN；归一化时**名称以树为准**（`23.01;胶状书` → `23.01;无线胶装书`） |
| 决策① 保留 `KHDH` | 不输出 `KHCPMC`（ERP 侧命名错误，与 `KHDDH` 只差一字母，会误导）；ERP 导入接口由 ERP 侧改。字典 notes / SKILL 第 10 节 / 测试全部同步，P0 关闭 |
| 决策② `CPSX` 落库 | 新增 `main.py derive_cpsx()`：年龄段 ≥3/<3 岁 × 有无安全检查 → 四个中文枚举；缺年龄则留空 + WARN。档案 `rules.cpsx` 可覆盖阈值与取值 |
| 决策③ 统一中文 label | 字典里 9 处 `store: code` 全部改为 `store: label`（`Drop_SJFX`/`TBLX`/`SFYPJ`/`CPSX`/`ZXLB`/`ZBLX`/`ZXFS`/`DKMC`/`YSLX`）；加测试禁止回退。代码仍可用于溯源与归一化输入 |
| 档案层 | `_defaults.yaml` / `igl.yaml` / `ngv.yaml` 新增 `rules.cplb`（含映射表）与 `rules.cpsx`；`ngv` 的 `binding` 值从非法的 `无线胶装` 改为 `胶装/PAD书` |
| 测试 | 新增 `tests/test_cplb_tree.py`（18 项：与源表逐节点一致、导航树全覆盖且不重复、BB书拆组、档案映射有效、兜底值在树里…）；`test_erp_schema.py` +3 项决策钉死；`test_pipeline.py` +4 项端到端断言。合计 **132 项全绿**（cplb_tree 18 / customer_profile 16 / erp_schema 36 / extract_fields 29 / ingest_email 16 / pipeline 17） |
| 实测 | igl 样本：`CPLB=23.01;无线胶装书`（档案 cplb.map 命中）、`CPSX=3岁以上（要寄第三方测试）`（7+ 且安全 Y）、校验 0 error 0 warn、59 键顺序断言通过 |

### 1.0.14 详细变更 —— ERP 表单权威字典

**背景**：森西提供了 ERP「报价需求单 / 产品资料」的**真实表单 HTML**（ASP.NET），
这是**合法取值的唯一权威来源**。此前 `Drop_SJFX` 的 `长书脊`/`短书脊` 是猜的、
`ZDFS` 的口径悬着、`DWMC` 不敢填。

| 项 | 说明 |
|----|------|
| ➕ `assets/erp_form_schema.yaml` | 59 字段 + 35 个仅表单字段的权威字典：`form_name`（控件名，溯源用）、中文 label、类型、**合法枚举**（含代码/显示文本）、别名表、必填级别、`store`（存 label 还是 code） |
| ➕ `scripts/erp_schema.py` | 归一化（别名/代码/空白）+ 校验（枚举/类型/必填）+ CLI（`--field` / `--values` / `--check`） |
| ➕ `main.py` **步骤 5b** | 出 JSON 前先归一化再校验；有 `error` 时终端打醒目块 + 写报告 |
| ➕ 产物 `<原名>.validation.json` | 每个任务目录里的校验报告（error / warn 明细） |
| ✅ TODO 落地 ×3 | `Drop_SJFX` 合法值 `短书脊`/`长书脊`/`四边裁`；`ZDFS` 合法值 10 项（**`无线胶装` 不是合法值**）；`DWMC` = `本` |
| ✅ 候选值清单 | `CPSX`/`YSLX`/`ZXLB`/`ZBLX`/`ZXFS`/`TBLX`/`DKMC`/`BZFS`/`SHFS`/`FSCMC` 全拿到 |
| ♻️ `customers/igl.yaml` | `binding.enabled` 改 `true`、映射值换成合法枚举；`erp_fixed` 加 `DWMC: 本`；`spine` 补 `four_sided` |
| ✅ `tests/test_erp_schema.py` | 35 项：字典覆盖 59 键、枚举/别名/代码归一化、类型与必填校验、**历史单据 3 个真缺陷回归** |
| 🐛 **查出历史单据 3 个真错误** | `0e39e2e2`（2026-05-25 手填）：`ZDFS="无线胶装"`（非法）/ `JHRQ`+`WCRQ` 是毫秒时间戳（口径是 `YYYY-MM-DD`）/ `SFYPJ` 是布尔（ERP 是 `是`/`否`） |
| ✅ **命名冲突已决** | 「客户单号」在 ERP 表单里的控件名是 `txtKHCPMC`（且 `lblKHCPMC` 又被用作「客户产品名称」）—— 业务 2026-09-23 拍板：**保留 `KHDH`，不跟着错**，ERP 侧改接口（1.0.15 关闭 P0） |

**回归**：igl 样本 `59 键顺序一致`；新增赋值 `ZDFS=胶装/PAD书`、`DWMC=本`（当时 24 个赋值字段）；
校验报告 `0 error / 0 warn`；`uv run pytest` 107 项全绿（当时的用例数，1.0.15 已增至 132 项）。

### 1.0.13 详细变更 —— 多客户架构

**问题**：技能原先只服务 igl —— 客户列名（`Imprint` / `Pub Month` / `Works References…`）、
固定值、数量档位写法、纸张类型（`书纸`/`光粉纸`）全部硬编码在 `main.py` 里。
要来第二个客户（NGV）就得改主逻辑，且无法保证「按客户的邮件和附件正确提取」。
另外**邮件标题/正文从未被解析**，`KHDH` 只能靠附件列。

**做法**：引入「客户档案」层，把客户习惯从代码里彻底剥离。

| 变更 | 内容 |
|------|------|
| ➕ `customers/` | 客户档案目录：`igl.yaml`（已校准）、`ngv.yaml`（draft）、`_defaults.yaml`（公共默认+兜底）、`_template.yaml`、`README.md` |
| ➕ `scripts/customer_profile.py` | 档案加载 / 深合并 / 规范键白名单校验 / 别名匹配 / **自动识别**（特征标签 100 分、发件人域名 30、文件名 10、主题 5）；带 CLI |
| ➕ `scripts/extract_fields.py` | 【原有】`auto_pairs` 一条规则吃下横向键值对 + 标签行/数据行混排；多工作表取命中最多者并以其它表补缺；**旧布局策略名全部替换** |
| ➕ `scripts/ingest_email.py` | 邮件入口：`.eml` / `--subject --body --attach` / `--attach-dir`；附件落 pending + sidecar；**从标题/正文取 `KHDH`**；认不出客户时**读附件内容**再判 |
| ➕ `scripts/calibrate_customer.py` | 新客户校准器：报告「认不出的标签」+ 候选规范键建议，自动过滤值/下拉清单/已登记 extras；输出 `_draft_<id>.yaml` |
| ➕ `tests/` | **72 项测试**（档案层 / 抽取引擎 / 邮件 / 端到端回归），`uv run pytest` |
| ➕ 依赖 | `pyyaml`（档案）、`pytest`（dev） |
| ♻️ `scripts/main.py` | **去掉所有客户列名**，改为读规范键 + 档案规则；赋值统计改为**诚实计数**（跑完打印真实被赋值的字段清单）；KHDH 优先级改为「邮件 → 附件列」；输出新增 `.extracted.json` 抽取明细；读邮件 sidecar |
| ♻️ 纸张类型映射表 | **TODO 落地**：档案 `rules.materials`（`woodfree` → 书纸、`C1S artboard` → 光粉纸…），可被任一客户覆盖，未命中 WARN + 默认值 |
| ♻️ 装订方式映射 | 机制落地（档案 `rules.binding`），igl **默认关闭**（`无线胶装` vs `胶状/PAD书` 口径待业务确认） |
| ♻️ 三个文本解析器 | `pdf2json` / `docx2json` / `txt2json` 改为 `extract_fields` 的**薄封装**，消灭第二套弱解析；docx 表格也会被读入 |
| ♻️ `SKILL.md` | 按「架构 / 客户档案 / 抽取 / 邮件 / CPMS / 校验」重排；igl 专属细节移入 `customers/igl.yaml`，不再在文档里重复维护列名 |
| ✅ 回归验证 | 同一份真实样本（`PUZZLE COLOUR DISNEY.xlsx`）：**59 键顺序一致**，23 个规范键，24 个赋值字段值**与重构前逐字段一致**（`CD=198 KD=129 GD=6.85`、`CPSL1~7`、`KHDH=23992 / 23993`…），29 个未触碰字段等于模板默认值 |

**修掉的真 bug（测试逼出来的）**

1. 数量档位「纯数字写法」的标签提示词**大小写敏感** → `Quantity` 匹配不到 `quantity`，静默全 0。
2. 自由文本切标签用模糊匹配 → `Series Nam` 也算命中 `Series Name`，「最长前缀」永远等于整行，
   值退化成单个字母（实测抽出来是 `'y'` / `'e'`）。
3. 短别名截胡长标签 → `Series` 抢走 `Series Name`，值变成 `Name`。

### v1 → v2 变更（1.0.8，破坏性，留档）

v1 的 40 字段中 13 个已删除：`id` `create_by` `has_safety_checks` `target_age_group`
`SJFX` `KHMBJ` `GG_length` `GG_width` `GG_height` `ZDBJF` `BSYJZX` `BCMS` `CPBZ1`。
改名：`SJFX`→`Drop_SJFX`、`GG_*`→`CD`/`KD`/`GD`、`KHMBJ`→`KHMBDJ`、`BCMS`→`BGMS`、
`ZDBJF`→`chkZDBJF`、`BSYJZX`→`chkBSYJZX`。
类型变更：`JHRQ`/`WCRQ` number→string；`SFYPJ` boolean→string。
中文名纠正：`ZXLB` 纸箱类别、`ZBLX` 纸板类型、`ZXFS` 装箱方式、`DKMC` 底卡名称、`ECPMC` 产品英文名。

---

## 12. TODO

### P0 · 导入前必须确认

- [x] ~~`KHDH` vs `KHCPMC` 试导一次~~ —— **已由业务拍板（2026-09-23）：保留 `KHDH`，
      不输出 `KHCPMC`；ERP 导入接口由 ERP 侧修改适配**（见第 10 节）。P0 关闭。
- [x] ~~`CPSX = 无` 能否被 ERP 导入端接受~~ —— **已解决（2026-09-24，1.0.23）**：
      「无」落 **JSON `null`**（= 空值），枚举校验与口径例外都不再需要。P0 关闭。
- [ ] **内部字段 ERP 导入端收不收**（`ID` / `CPMC_ZH` / `Reminder`）—— 现在默认追加在 59 键之后。
      若导入端报「未知字段」，用 `--strip-internal` 出纯净 59 键（已实现，一条命令的事）。
- [ ] **`igl` 的 `CPMC` 英文表达**（2026-09-24 业务拍板：**先不做**）—— 原话「igl 的先不做，
      后续补充真实的 ERP 信息再做处理」。所以 igl **保持历史写法**
      （`23.01;无线胶装书-Puzzle & Colour Disney`，含中文类别名）、`CPMC_ZH` 为空，
      等拿到真实 ERP 信息后再决定英文表达怎么拼（改 `customers/igl.yaml` 的
      `rules.cpmc.template` / `zh_template` 两行即可）。**不要凭猜迁移。**

### P1 · 待业务确认（阻塞实现）

- [x] `Drop_SJFX` 合法枚举 —— **已确认**（1.0.14）：`短书脊`/`长书脊`/`四边裁`；
      「默认由 CD/KD 决定」已落地。剩「四边裁」的判据未给（暂无规则触发）
- [x] `ZDFS` 口径 —— **已确认**（1.0.14）：合法值是 `胶装/PAD书`（**不是** `无线胶装`），
      igl 的 `rules.binding.enabled` 已改 `true`，映射表全部换成合法值
- [x] `DWMC` 是否补 `本` —— **已确认**（1.0.14）：ERP 单位枚举含 `本`，已加进 igl `erp_fixed`
- [x] `CPSX` / `YSLX` / `ZXLB` / `ZBLX` / `ZXFS` / `TBLX` / `DKMC` / `BZFS` / `SHFS` /
      `FSCMC` 候选值清单 —— **已从 ERP 真表单拿到**（`assets/erp_form_schema.yaml`）；
      仍未拿到：`SHDQ`（联动下拉，静态 HTML 为空）、`CPDL` 全量（当前只 `书版`）
- [ ] `CPSL` 档位**顺序口径**：现为「客户表列顺序 → `CPSL1..n`」；
      若实际是「按数量区间只填一档」，规则要重写。**NGV 侧已解决**（它的数量是
      单一总数 → `quantity.mode: single` 落 `CPSL1`，见 4.4）；**待确认的只剩 igl** ——
      它对「列顺序」还是「选中一档」的口径未拍板
- [x] `CPSX` 是否承接年龄段/安全检查 —— **已确认（2026-09-23）：要落库**，
      规则见 4.7.2（年龄 ≥3/<3 岁 × 有无安全检查 → 四个中文枚举），igl/ngv 已配好
- [x] 哪些字段要用**数字代码**而不是 label 导入 —— **已确认（2026-09-23）：统一用中文
      label**，字典 9 处 `store: code` 已全部翻转，有测试守着（见 4.6）
- [ ] **产品类别树的「常用速选」排序**目前是业务经验值，不是 ERP 真实频次 ——
      若能从 ERP 导出各 `CPLB` 的历史使用次数，按数据重排（改 `cplb_tree.py`
      的 `QUICK_PICK` 后 `--rebuild`）
- [ ] **产品类别树是否要同步回 ERP** —— 重设计的导航层目前只在我们这边（用于选类别 +
      agent 判断）；若业务希望 ERP 下拉本身也按新结构显示，需要 ERP 侧配合（编号不变即可无缝切换）
- [ ] **`CPLB` 顶层 vs 子类**：ERP 允许只选顶层（`23.00;胶装书`），但生产/计价需要子类；
      现在只给 WARN。若业务要求「必须选到子类」，把它升级为 error
- [ ] **`SHDQ` 送货地区的枚举**（仍是空的联动下拉，静态 HTML 取不到）—— 业务 2026-09-24 只给了 `ZBLX`
- [ ] **`PXH` 是否对 igl 也留空**（业务 2026-09-24 对 NGV 定的是留空；igl 现仍填 1）——
      一行的事，等业务点头（见 `customers/igl.yaml` 的偏好表备注）

### P1 · 待实现

- [x] **NGV 档案校准** —— **已完成（1.0.16）**：4 份真实附件（CU26051601-01 /
      CU26061701 / CU26072301 / CU26072302）+ 随包 ERP 导出，
      `status: calibrated`；新增 `section_pairs` 策略与 `sections:` 分区表（细节 1.0.19 起移入子技能 `customer-quote-ngv`），
      回归语料库 `customer-files/samples/ngv/`（客户原文，gitignored）
- [ ] **IMAP 直连收信** —— 需要先确认公司收信方式（IMAP 服务器 / 转发到本地 / 邮箱 API），
      再加「取信 → 落 `.eml` → 调 `ingest_email.py`」的适配层。
      ⚠️ NGV 实测暴露出**非做不可**的理由：CU26061701 的客户单号在附件里完全不存在
      （文件名与表内都没有），只能从邮件标题/正文取
- [ ] `CPLX` 产品类型（新产品 / 返单产品 / …）需对接历史订单系统
- [ ] `ECPMC` 产品英文名（可直接取 `series_name`；NGV 可取 `product_name`）
- [ ] **`YSLX` 运输类型** —— 业务 2026-09-24：**默认留空**，长期客户的实际口径写进档案
      「客户偏好表」（`preferences`，见 2.7）。要做到自动取还需邮件入口
      （NGV 的 `FOB(底价)` / `FOB(拼柜)` 在邮件语境里）
- [ ] `CPMC` 的中文品名（NGV 格式是 `<业务员>/<中文品名> <类别>/<英文原文>(<FSC>)`）——
      中文部分只能人工拟，脚本产出英文底稿 + WARN；`ngv.yaml` 已标 `cpmc.needs_agent`
- [x] ~~`Remark3` 修改记录~~ —— 业务 2026-09-24：**留空**（我们产出的就是第一版报价）
- [x] ~~`SFYPJ` / `Remark1` 配件判断~~ —— **业务 2026-09-24 改为「推不出就留空」**
      （1.0.18，此前写死「否」在 CU26072301 上是错的）；真正要识别配件需来源（邮件）
- [ ] `BZFS` / `TBLX` / `SHFS` 包装、台板、送货方式 —— **NGV 已按固定值落地**；其他客户待样本
- [ ] `OLD_KHDDH` / `SHDQ` / `KHDQ`
- [ ] `ZXLB` / `ZXFS` / `DKMC` / `BGMS`
- [ ] 4 个 `chk*` 开关（确认业务是否要脚本推导）

### NGV 待确认项 → 已全部关闭（明细见子技能）

NGV 的两批口径（2026-09-24 共 15 条）业务已逐条拍板并落地为档案开关，
**明细与依据在 skill `customer-quote-ngv` 第 4 节**，本文件不再重复维护
（避免同一件事写在两处、改一处忘另一处）。

仍挂着的两条：**`SHDQ` 送货地区枚举**（联动下拉，静态 HTML 取不到）、
**`igl` 的 `PXH` 是否也留空**（业务 2026-09-24 对 NGV 定的是留空）。

### 运维 / 文档

- [ ] 摘掉 hub 登记项（`skills/.hub/lock.json` 里的 `customer-quote-request`），
      避免 `hermes skills check/update` 穿透软链接把仓库 `SKILL.md` 覆盖回 GitHub 旧版
- [x] 软链接体检 cron 已上线（`cqr_skill_link_watchdog.sh`，每小时 07 分）——
      1.0.19 起**覆盖全部三个技能**（总纲 + igl + ngv）：断链/指向错误/被 `hermes skills
      update` 换成实体文件都会自动备份并重建，另外做内容快照与未推送提醒
- [x] 「文档 ↔ main.py ↔ 模板」三方一致性：由 `tests/` 的 59 键断言 + 逐字段值回归承担
- [x] **「模板 ↔ ERP 表单字典」一致性**：`tests/test_erp_schema.py` 断言 59 个模板键
      在字典里都有定义（1.0.14）
- [x] **「产品类别树 ↔ 源表 ↔ 档案映射」一致性**：`tests/test_cplb_tree.py`
      逐节点核对 + 导航树全覆盖 + 档案 `cplb.map`/`fallback` 都必须在树里（1.0.15）
- [x] 单元测试（**179 项**，1.0.18）
- [x] **NGV 分区表单回归**：`tests/test_ngv_profile.py`（合成用例 + 真实样本）（1.0.16）
- [x] **档案重复键检测**：`_UniqueKeyLoader` 把 YAML 重复键变成硬错误（实测踩过：`rules.unit` 写两遍，前一份被静默吃掉）（1.0.16）
- [x] **书脊方向按装订边细化**（NGV，1.0.16）：`rules.spine.mode: binding_side` ——
      由附件里的 `<N> mm side` 与较长边比较决定；套装类经 `spine.skip_tops` 留空。
      依据：CU26072302 的 145x210 + `210 mm side` → ERP `Drop_SJFX=长书脊`
- [ ] `四边裁` 缺判据（合法值已确认，等业务给触发条件）
- [ ] 支持更多日期格式
- [ ] 客户档案可以再拆一层「客户 → 单据模板」（若将来同一客户有多种报价单格式）
