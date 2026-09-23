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
> **版本：** 1.0.15（2026-09-23 产品类别树重设计 + 四条业务决策落地）

把**客户发来的报价需求**（邮件标题/正文 + 附件，或直接给的表格/文档）转换成
**ERP 导入用的 59 字段 JSON**。

---

## 0. 一句话架构（先看这个）

```
                    customers/<客户id>.yaml   ← 客户习惯全在这里（唯一要改的地方）
                              │
  邮件 ──► ingest_email.py ──►│
          （附件落 pending     │
            + 客户单号 KHDH）  ▼
  客户文件 ──► extract_fields.py ──► 规范键 → 值
                                    │
                     main.py ───────┴──► 归一化 + 校验（erp_schema.py）
                                    │      ×  assets/erp_form_schema.yaml（ERP 合法枚举）
                                    │
                                    ├──► ERP JSON（59 字段）
                                    └──► CPMS 留空 → ★ agent 亲自补写
```

**三层职责，不要混：**

| 层 | 文件 | 职责 | 新增客户要改吗 |
|----|------|------|---------------|
| 客户档案 | `customers/*.yaml` | 这个客户怎么写字号/表头/档位/材质；怎么从邮件取单号 | **要**（只改这里） |
| 抽取引擎 | `scripts/extract_fields.py` | 按档案把任意文件抽成「规范键 → 值」 | 一般不用（除非布局全新） |
| 业务映射 | `scripts/main.py` | 规范键 + 档案规则 → ERP 字段；输出断言 | 不用 |
| **取值权威** | `assets/erp_form_schema.yaml` | **ERP 只认哪些值**（下拉枚举/类型/必填） | 不用（ERP 全局恒定） |

> **铁律：`main.py` 里不允许出现任何客户的列名。**
> 列名只属于档案。看到 `main.py` 里写死了某个客户的表头，就是重构退化了。

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

---

## 2. 客户档案（新增客户只看这一节）

每个客户一份 `customers/<id>.yaml`。**改客户的解析方式 = 改档案，不改代码。**

### 2.1 现有档案

| 档案 | 客户 | 状态 |
|------|------|------|
| `customers/igl.yaml` | IGL | ✅ calibrated（用真实样本校准过） |
| `customers/ngv.yaml` | NGV | ⚠️ **draft，未校准**（手上还没有 NGV 样本） |
| `customers/_defaults.yaml` | 兜底 + 公共默认值 | 所有档案都叠加在它之上；认不出客户时用它（会打 WARN） |
| `customers/_template.yaml` | 新客户骨架 | 复制它开始 |

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
uv run pytest                                      # 72 项测试必须全过
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

# ── ④ ERP 固定值 ─────────────────────────────────────────
erp_fixed:
  CPDL: 书版
  YYS: 1

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

> 表头即标签的整列表格不需要单独策略：引擎统一按 `header=None` 读表，
> 真表头会落在第 0 行，由 `auto_pairs` 按「密集标签行 → 取其下方」处理。
>
> **实测**（igl 的真实报价单）：`auto_pairs` 一条规则同时吃下了
> `Imprint|Autumn`、`Requested By|(空格)|Christine`、`Group/Series Number|(空格)|6366`
> （横向，值隔着空格）与 `Pub Month`/数据行、`Size…`/下一列（纵向），共 23 个规范键。

---

## 3. 邮件入口 `ingest_email.py`

**传输无关**：谁都可以调，不依赖具体收信方式。

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
2. 按**客户档案**的 `email.khdh` 规则从**邮件标题/正文**取客户单号 `KHDH`
3. 给每个附件写 sidecar `<附件名>.meta.json`（标题/正文/发件人/单号/识别到的客户）

`main.py` 处理该附件时会**自动读取 sidecar**：客户单号优先级是
**邮件标题/正文 → 附件列（`email.khdh.attachment_keys`）**；处理完 sidecar 会一起归档进任务目录。

**客户识别顺序**：邮件标题/正文/文件名 → 认不出就**读附件内容**再判
（报价单里的招牌栏目比文件名可靠，例如 igl 的 `Works References For all Titles`）。

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

日志：`logs/quote_request_<时间戳>.log`；任务目录：`customer-files/processed/<task_id>/`。

### 4.1 脚本赋值了哪些字段（诚实口径）

`map_fields()` 真正赋值的是：

1. **档案固定值**（`erp_fixed`，igl 当前 6 个）：`CPDL` `YYS` `BZ` `PXH` `KHMBDJ` `DWMC`
2. **规范键推导**（9 个）：`CPLB`（format 查表）、`CPMC`（CPLB+系列名）、`TZS`、
   `CD` `KD` `GD`（尺寸/厚度计算）、`Drop_SJFX`（CD vs KD）、`JHRQ`、`WCRQ`、`KHDH`
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
| `CPMC` | `<CPLB>-<series_name>` |
| `TZS` | `number_of_titles`（非数字则 1） |
| `CPSL1`~`CPSL10` | 数量档位（见 4.4） |
| `CD`/`KD`/`GD` | 规范键 `size`，按档案 `size.order`（igl 是 H x W x D → CD/KD/GD）；尺寸里没给 D 时按页数+纸张算厚度 |
| `GD`（计算） | `(extent_pp / 2) × 内页厚度 + extent_cover × 封面厚度` |
| `Drop_SJFX` | `CD > KD` → `长书脊`，否则 `短书脊`（档案 `rules.spine`）。合法枚举见 4.6：`短书脊`/`长书脊`/`四边裁` |
| `JHRQ` | 档案 `dates.JHRQ.from` 指定的规范键（默认 `pub_month`）→ `YYYY-MM-DD` |
| `WCRQ` | 当前日期 + `dates.WCRQ.days` 天 → `YYYY-MM-DD` |
| `KHDH` | 邮件标题/正文 → 附件列（见第 3 节） |
| `ZDFS` | 档案 `binding.map` 匹配 `binding` 文本（igl 已开启）；**命中不到用 `binding.default`**，两者都必须落在 ERP 合法枚举内（见 4.6） |
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
| 2 | `YWY` | 业务员 | ERP 自动带出 | `""` | ⚠️ |
| 3 | `KHZL` | 客户资料 | ERP 自动带出 | `""` | ⚠️ |
| 4 | `GDY` | 跟单员 | ERP 自动带出 | `""` | ⚠️ |
| 5 | `Remark` | 报价备注 | ERP 自动带出 | `""` | ⚠️ |
| 6 | `CPMX` | 产品明细 | ERP 自动带出 | `""` | ⚠️ |
| 7 | `CPDL` | 产品大类 | 档案 `erp_fixed` | `"书版"` | ✅ |
| 8 | `CPLB` | 产品类别 | `format` 查表 | `""` | ✅ |
| 9 | `Drop_SJFX` | 书脊方向 | `CD` vs `KD` | `""` | ✅ |
| 10 | `ZDFS` | 装订方式 | `binding`（档案映射，合法值见 4.6） | `""` | ✅ |
| 11 | `CPMC` | 产品名称 | `CPLB` + `series_name` | `""` | ✅ |
| 12 | `TZS` | 共有款数 | `number_of_titles` | `0` | ✅ |
| 13 | `YYS` | 每款语言数 | 档案 `erp_fixed` | `1` | 🔒 |
| 14 | `DWMC` | 单位 | 档案 `erp_fixed`（`本`，合法值见 4.6） | `""` | ✅ |
| 15~24 | `CPSL1`~`CPSL10` | 产品数量 ①~⑩ | 数量档位 | `0` | ✅ |
| 25 | `BZ` | 币种 | 档案 `erp_fixed` | `"美元"` | 🔒 |
| 26 | `KHMBDJ` | 客户目标价-金额 | 档案 `erp_fixed` | `0` | 🔒 |
| 27 | `CD` | 规格-长 (mm) | `size` 第 1 段 | `0` | ✅ |
| 28 | `KD` | 规格-宽 (mm) | `size` 第 2 段 | `0` | ✅ |
| 29 | `GD` | 规格-高/厚 (mm) | `size` 第 3 段 / 计算 | `0` | ✅ |
| 30 | `BZFS` | 包装方式 | 客户要求 | `""` | ⚠️ |
| 31 | `TBLX` | 台板类型 | 客户要求 | `""` | ⚠️ |
| 32 | `SHFS` | 送货方式 | 客户要求 | `""` | ⚠️ |
| 33 | `KHDH` | 客户单号 | 邮件标题/正文 → 附件列 | `""` | ✅ |
| 34 | `JHRQ` | 交货日期 | `pub_month` → `YYYY-MM-DD` | `""` | ✅ |
| 35 | `FSCMC` | FSC 类别 | 客户要求 | `""` | ⚠️ |
| 36 | `OLD_KHDDH` | 返单香港单号 | 返单时填 | `""` | ⚠️ |
| 37 | `SFYPJ` | 是否有配件（**string**） | `Accessories` / 配件 | `""` | ⚠️ |
| 38 | `Remark1` | 配件描述 | `SFYPJ` 有值时 | `""` | ⚠️ |
| 39 | `PXH` | 排序号 | 档案 `erp_fixed` | `1` | 🔒 |
| 40 | `CPSX` | 产品属性 | 年龄段 + 安全检查（档案 `rules.cpsx` 推导） | `""` | ✅ |
| 41 | `CPLX` | 产品类型 | 历史订单比对 | `""` | ⚠️ |
| 42 | `WCRQ` | 预计下单日期 | 今天 + N 天 → `YYYY-MM-DD` | `""` | ✅ |
| 43 | `KHDDH` | 香港单号 | 返单/香港单 | `""` | ⚠️ |
| 44 | `chkXBJF` | 不需报价 | 开关 | `false` | 🔒 |
| 45 | `ECPMC` | 产品英文名 | `series_name`（英文） | `""` | ⚠️ |
| 46 | `chkZDBJF` | 有排交期 | 开关 | `false` | 🔒 |
| 47 | `ZXLB` | 纸箱类别 | 客户要求 | `""` | ⚠️ |
| 48 | `ZBLX` | 纸板类型 | 客户要求 | `""` | ⚠️ |
| 49 | `ZXFS` | 装箱方式 | 客户要求 | `""` | ⚠️ |
| 50 | `chkBSYTB` | 不使用台板 | 开关 | `false` | 🔒 |
| 51 | `chkBSYJZX` | 不使用集装箱 | 开关 | `false` | 🔒 |
| 52 | `DKMC` | 底卡名称 | 客户要求 | `""` | ⚠️ |
| 53 | `YSLX` | 运输类型 | 客户要求 | `""` | ⚠️ |
| 54 | `SHDQ` | 送货地区 | 客户要求 | `""` | ⚠️ |
| 55 | `KHDQ` | 客户地区 | 客户资料 | `""` | ⚠️ |
| 56 | `CPMS` | 产品描述 | **agent 亲自**（脚本不生成） | `""` | 👤 |
| 57 | `BGMS` | 补充描述 | — | `""` | ⚠️ |
| 58 | `Remark4` | 检测项目 | — | `""` | ⚠️ |
| 59 | `Remark3` | 修改记录 | — | `""` | ⚠️ |

> **注意：** v2 没有 `Remark2`。备注类是 `Remark` / `Remark1` / `Remark3` / `Remark4`。
> `BDBH`/`YWY`/`KHZL`/`GDY`/`Remark`/`CPMX` 业务已确认**由 ERP 自动带出，留空即正确**。
> `CPDL` 是「书**版**」不是「书**板**」。

### 4.4 数量档位 `CPSL1`~`CPSL10`

**业务确认（2026-09-23）：** 默认 `0`，取值由**客户订单的数量档位**决定 ——
客户报价单会给出一行档位，按**从左到右**依次填 `CPSL1`、`CPSL2`…，没用到的留 `0`。

```
Total Costs | 3k | 5k | 7.5k | 10k | 20k | 25k | 30k
                ↓
CPSL1=3000 CPSL2=5000 CPSL3=7500 CPSL4=10000 CPSL5=20000 CPSL6=25000 CPSL7=30000 CPSL8~10=0
```

识别规则刻意做严（**宁可漏判，不可误判**），写法由档案 `rules.quantity_tiers` 控制：

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
   - `warn`：建议填写字段为空 / 字段不在字典里
3. **报警**：有 `error` 时终端打出醒目块（`!! ERP 取值校验：N 个 error !!`）+ 日志，
   提示「导入前必须处理」。

**产物**：`customer-files/processed/<task_id>/<原名>.validation.json`

```json
{ "schema": "assets/erp_form_schema.yaml", "customer": "igl",
  "errors": [], "warnings": [], "summary": {"error": 0, "warn": 0} }
```

**常用命令**

```bash
uv run ./scripts/erp_schema.py                      # 字典概览（哪些字段有枚举）
uv run ./scripts/erp_schema.py --field ZDFS         # 单字段定义 + 别名表
uv run ./scripts/erp_schema.py --values Drop_SJFX   # 只列合法值
uv run ./scripts/erp_schema.py --check <ERP.json>   # 校验任意一份结果
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
| `CPLB` | 树形下拉，值是 `<编号>;<名称>`（如 `23.01;无线胶装书`）→ 校验**编号是否为树节点**；只写到顶层（`23.00`）会给 WARN，因为太粗会影响生产/计价。名称会被树里的规范名覆盖（防「编号对、名字错」） |
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
| 缺年龄段（或只有安全检查） | — | **留空 + WARN**，不猜 |

实测（igl 样本 `PUZZLE COLOUR DISNEY.xlsx`：`Target Age Group=7+`、`Safety Checks=Y`）
→ `CPSX = "3岁以上（要寄第三方测试）"`。

> ⚠️ **落库不替代 `CPMS`**：产品描述里仍必须写清年龄段与安全要求（见第 5 节）。

---

## 5. `CPMS` —— agent 亲自撰写（脚本绝不参与）

> ★ **这是本技能唯一「必须 agent 亲力亲为」的环节。**
> 业务确认（2026-09-23）：产品描述**无法用 Python 统一处理** —— 每个客户的报价文件结构、
> 栏目命名、信息分布都不一样。脚本只能做机械拼串，产出既非成品、又会让人误以为
> 「已经处理过了」。因此 `build_cpms_draft()` / `CPMS_SECTIONS` / `CPMS_NOISE_KEYS`
> **已全部删除**（1.0.11，不要恢复）。

**脚本跑完会打印：**

```
================================================================
!! CPMS 为空 —— 必须由 agent 亲力亲为补写 !!
   产物: <ERP JSON 路径>
   抽取明细: <*.extracted.json 路径>
   步骤: 通读客户原文 -> 翻译成中文 -> 按类别分段优化 -> 写回 CPMS
   别漏这些非 ERP 字段的附加信息（N 项）:
     Quotation Number, Carton Weight, ...
================================================================
```

**agent 必须做：**

1. **通读原始客户文件**（任务目录里的原始文件 / `.csv` / `.extracted.json` 里的
   `values` + `extras_found`），不要只看抽取出来的字段
2. **翻译成中文**
3. **按类别分段优化** —— 建议分段：【项目基本信息】【产品类型说明】【尺寸规格】
   【内文印刷规格】【封面印刷规格】【装订与表面处理】【安全合规要求】【包装与运输】【其他备注】
4. **写回** `ERP_<task_id>_<时间戳>.json` 的 `CPMS` 字段
5. **自检**：`CPMS` 非空；`safety_checks` / `target_age_group` 已写进【安全合规要求】；
   `extras_found` 里的附加信息一条不漏

> **`CPMS` 为空的 JSON 视为未完成，不得交付。**
> v2 已删除 `has_safety_checks` / `target_age_group` 两个独立字段 ——
> 「安全检查」「目标年龄段」**只在 CPMS 文本里体现**，是唯一载体。

---

## 6. 字段一致性校验（必读）

被问「输出是否和处理的字段一致」时，**不要只看输出文件**。走这三步：

1. **字段集合 + 顺序** —— 输出 JSON 的键必须与 `assets/add_customer_quote_request.json`
   的 **59 个键完全一致且同序**。集合对、顺序错也算不一致。
   ```bash
   python3 - <<'PY'
   import json
   tpl = json.load(open("/home/claw/customer-quote-request/assets/add_customer_quote_request.json"))
   out = json.load(open("<输出的 ERP JSON 路径>"))
   print("字段数一致:", len(tpl) == len(out))
   print("键顺序一致:", list(tpl) == list(out))
   print("缺失:", set(tpl) - set(out))
   print("多余:", set(out) - set(tpl))
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
   uv run ./scripts/erp_schema.py --check <输出的 ERP JSON 路径>
   ```
   等价于读任务目录里的 `<原文件名>.validation.json`。有 `error` 就别导入。

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
├── SKILL.md
├── README.md
├── customers/                          # ★ 客户档案（新增客户只动这里）
│   ├── README.md                       # 新增客户的 3 步 SOP
│   ├── _defaults.yaml                  # 公共默认值 + 兜底档案
│   ├── _template.yaml                  # 新客户骨架
│   ├── igl.yaml                        # IGL（已校准）
│   └── ngv.yaml                        # NGV（draft，待校准）
├── assets/
│   ├── add_customer_quote_request.json # ERP 字段模板（59 键，权威来源）
│   ├── erp_form_schema.yaml            # ★ ERP 表单权威字典（合法枚举 / 别名 / 类型）
│   ├── customer_erp_mapper.xlsx        # 客户字段↔ERP 字段对照（igl 的，客户相关）
│   ├── product_categories.xlsx         # 产品类别源表（ERP 导出，88 行，含业务定义）
│   ├── cplb_tree.yaml                  # ★ 产品类别权威树 + 重设计导航树（由脚本生成）
│   ├── cplb_tree.html                  # ★ 产品类别可视化选取器（可搜索、点击复制）
│   └── page_thickness.xlsx             # 纸张厚度表
├── scripts/
│   ├── main.py                    # ★ 主入口（档案驱动，不含客户列名）
│   ├── customer_profile.py        # ★ 档案加载 / 校验 / 自动识别（可单独跑 CLI）
│   ├── extract_fields.py          # ★ 抽取引擎（布局策略 + 数量档位 + 文本解析）
│   ├── erp_schema.py              # ★ ERP 取值归一化 / 校验（步骤 5b，可单独跑 CLI）
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
├── tests/                         # 132 项测试（uv run pytest）
│   ├── conftest.py
│   ├── test_customer_profile.py   # 档案层
│   ├── test_extract_fields.py     # 抽取引擎
│   ├── test_erp_schema.py         # ★ ERP 枚举归一化 / 校验（含历史单据缺陷回归 + 决策钉死）
│   ├── test_cplb_tree.py          # ★ 产品类别树（与源表一致 / 导航树全覆盖 / 档案映射有效）
│   ├── test_ingest_email.py       # 邮件入口
│   └── test_pipeline.py           # 端到端回归（真实样本 + 逐字段值断言）
├── customer-files/                # pending / processed / failed（产物，不进仓库）
├── logs/                          # 任务日志（不进仓库）
├── pyproject.toml                 # 依赖（pandas / openpyxl / python-docx / pymupdf / pyyaml）
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

# 档案管理
uv run ./scripts/customer_profile.py --list
uv run ./scripts/customer_profile.py --validate
uv run ./scripts/customer_profile.py --detect "customer-files/pending/QUOTE.xlsx"

# 新客户校准
uv run ./scripts/calibrate_customer.py --customer ngv --file <样本> --layout

# 邮件投递
uv run ./scripts/ingest_email.py --eml mail.eml

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
- **两份 SKILL.md 是软链接，只需维护一份**：
  仓库源（**改这个**）`/home/claw/customer-quote-request/SKILL.md`
  ← 技能加载路径 `~/.hermes/profiles/igl_agent/skills/customer-quote-request/SKILL.md`。
  用 `ls -l` 确认链接未断；断了说明有人把它换回了实体文件。
- ⚠️ **`hermes skills check/update/uninstall` 会穿透软链接覆盖仓库文件** ——
  该技能在 hub 里登记过（`~/.hermes/profiles/igl_agent/skills/.hub/lock.json`，
  来源 `Senthie/customer-quote-request`）。执行这些命令前先备份仓库副本，或摘掉 hub 登记项（见 TODO）。

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
  - **我们的态度是不跟着错**：`KHCPMC` 与模板第 44 键 `KHDDH`（香港单号）只差一个字母，
    沿用会误导填单的人。**ERP 导入接口由 ERP 侧修改适配**（已确认，不是待办）。
  - 溯源信息保留：字典里 `KHDH.form_name = txtKHCPMC`，方便回查 ERP 页面。
  - 有测试钉着：`test_决策_客户单号用_KHDH_且不得出现_KHCPMC`。
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
      若实际是「按数量区间只填一档」，规则要重写
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

### P1 · 待实现

- [ ] **NGV 档案校准** —— 拿到第一份 NGV 报价文件后按 `customers/README.md` 走 3 步，
      把 `customers/ngv.yaml` 从 `draft` 改成 `calibrated`
      （`fields` 别名表、`cplb.map`、`cpsx`、`binding` 都还是占位值）
- [ ] **IMAP 直连收信** —— 需要先确认公司收信方式（IMAP 服务器 / 转发到本地 / 邮箱 API），
      再加「取信 → 落 `.eml` → 调 `ingest_email.py`」的适配层
- [ ] `CPLX` 产品类型（新产品 / 返单产品 / …）需对接历史订单系统
- [ ] `ECPMC` 产品英文名（可直接取 `series_name`）
- [ ] `SFYPJ` / `Remark1` 配件判断与描述（注意 `SFYPJ` 是 string `是`/`否`）
- [ ] `BZFS` / `TBLX` / `SHFS` 包装、台板、送货方式
- [ ] `FSCMC` / `OLD_KHDDH` / `YSLX` / `SHDQ` / `KHDQ`
- [ ] `ZXLB` / `ZBLX` / `ZXFS` / `DKMC` / `BGMS` / `Remark4` / `Remark3`
- [ ] 4 个 `chk*` 开关（确认业务是否要脚本推导）

### 运维 / 文档

- [ ] 摘掉 hub 登记项（`skills/.hub/lock.json` 里的 `customer-quote-request`），
      避免 `hermes skills check/update` 穿透软链接把仓库 `SKILL.md` 覆盖回 GitHub 旧版
- [x] 软链接体检 cron 已上线（`cqr_skill_link_watchdog.sh`，每小时 07 分，断链自动重建）
- [x] 「文档 ↔ main.py ↔ 模板」三方一致性：由 `tests/` 的 59 键断言 + 逐字段值回归承担
- [x] **「模板 ↔ ERP 表单字典」一致性**：`tests/test_erp_schema.py` 断言 59 个模板键
      在字典里都有定义（1.0.14）
- [x] **「产品类别树 ↔ 源表 ↔ 档案映射」一致性**：`tests/test_cplb_tree.py`
      逐节点核对 + 导航树全覆盖 + 档案 `cplb.map`/`fallback` 都必须在树里（1.0.15）
- [x] 单元测试（**132 项**，1.0.15）
- [ ] 书脊方向按装订方式细化（当前仅按 `CD` vs `KD` 判 `长书脊`/`短书脊`；
      `四边裁` 缺判据 —— 合法值已确认，等业务给触发条件）
- [ ] 支持更多日期格式
- [ ] 客户档案可以再拆一层「客户 → 单据模板」（若将来同一客户有多种报价单格式）
