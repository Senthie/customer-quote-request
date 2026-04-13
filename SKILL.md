---
name: customer-quote-request
description: 通过 CSV 或 XLSX 文件读取客户的报价需求，将报表表格提取为 JSON 格式的结构化数据。
metadata:
  openclaw:
    emoji: "👨‍💼"
    requires:
      bins:
        - uv
        - python3
---

# Customer Quote Request Parser

解析客户报价单文件（CSV/XLSX），提取关键信息并映射为标准化的 ERP JSON 格式。

## 功能特性

- **任务隔离**：每个任务使用独立目录，以 UUID4 作为任务ID
- **文件防重复**：处理完成后自动从 pending 目录移出
- **多格式输出**：同时生成 ERP JSON 和 CSV 格式
- **完整追溯**：原始文件、转换后的CSV、ERP JSON 都在同一任务目录下

---

## 输出目录结构

处理完成后，文件将按以下结构组织：

```
customer-files/
├── pending/              # 待处理文件放入此处
│   └── (空，处理完成后自动移出)
├── processed/            # 处理成功的任务
│   └── <task_id>/       # UUID4 格式的任务目录
│       ├── ERP_<task_id>_<YYYYMMDD>_<HHMMSS>.json   # ERP系统导入格式
│       ├── <原文件名>.csv                           # 转换后的CSV
│       └── <原文件名>.xlsx                          # 原始文件
└── failed/              # 处理失败的任务
    └── failed_<YYYYMMDD>_<HHMMSS>/
        └── <原文件名>.xlsx
```

---

## 工作流程（详细步骤）

> **日志记录要求**：每个步骤必须将开始、执行过程、结果写入日志文件。日志文件路径：`./logs/quote_request_<task_id>_<timestamp>.log`

---

### 步骤 1: 输入验证

**日志记录：**

```
[YYYY-MM-DD HH:MM:SS] ========== 步骤 1: 输入验证 ==========
[YYYY-MM-DD HH:MM:SS] 输入文件: <文件路径>
[YYYY-MM-DD HH:MM:SS] 文件格式检查: <通过/失败>
[YYYY-MM-DD HH:MM:SS] 文件大小: <字节数>
[YYYY-MM-DD HH:MM:SS] 格式转换: <xlsx→csv 完成>
[YYYY-MM-DD HH:MM:SS] 必需字段检查: <通过/缺失字段列表>
[YYYY-MM-DD HH:MM:SS] 步骤 1 完成，状态: <成功/失败>
```

**检查文件格式：**

- 支持的格式：`.csv`, `.xlsx`, `.xls`
- 不支持的格式会报错并中止

**格式转换：**
将 `xlsx` 和 `xls` 文件转为 `csv` 文件：

```bash
# 同一目录下生成 file.csv
python xlsx2csv.py /path/to/file.xlsx

# 指定输出文件
python xlsx2csv.py input.xlsx -o output.csv

# 指定工作表（名称或索引）
python xlsx2csv.py input.xlsx -s "Sheet2"

# 指定编码
python xlsx2csv.py input.xlsx --encoding utf-8
```

**检查必需字段：**

- 产品名称/系列名称
- 数量/款数
- 尺寸规格

如有缺失，提示用户确认。

---

### 步骤 2: 读取客户数据

**日志记录：**

```
[YYYY-MM-DD HH:MM:SS] ========== 步骤 2: 读取客户数据 ==========
[YYYY-MM-DD HH:MM:SS] 文件类型: <xlsx/csv>
[YYYY-MM-DD HH:MM:SS] 表格格式检测: <横向/纵向>
[YYYY-MM-DD HH:MM:SS] 读取行数: <N>
[YYYY-MM-DD HH:MM:SS] 提取字段数: <N>
[YYYY-MM-DD HH:MM:SS] 提取的字段列表:
[YYYY-MM-DD HH:MM:SS]   - <字段名1>: <值1>
[YYYY-MM-DD HH:MM:SS]   - <字段名2>: <值2>
[YYYY-MM-DD HH:MM:SS]   ...
[YYYY-MM-DD HH:MM:SS] 步骤 2 完成，状态: <成功/失败>
```

读取用户上传的报价单文件，提取数据内容保存为 `customer_data`。

**支持的文件格式：**

- CSV (逗号分隔)
- XLSX/XLS (Excel)

---

### 步骤 3: 读取 ERP 模板

**日志记录：**

```
[YYYY-MM-DD HH:MM:SS] ========== 步骤 3: 读取 ERP 模板 ==========
[YYYY-MM-DD HH:MM:SS] 模板文件: ./assets/add_customer_quote_request.json
[YYYY-MM-DD HH:MM:SS] 模板字段数: <N>
[YYYY-MM-DD HH:MM:SS] 模板字段列表:
[YYYY-MM-DD HH:MM:SS]   - <字段名1>: <默认值1>
[YYYY-MM-DD HH:MM:SS]   - <字段名2>: <默认值2>
[YYYY-MM-DD HH:MM:SS]   ...
[YYYY-MM-DD HH:MM:SS] 步骤 3 完成，状态: <成功/失败>
```

读取 `./assets/add_customer_quote_request.json` 获取标准字段列表：

- `key`: ERP 字段名
- `value`: 默认值

---

### 步骤 4: 生成任务 ID

**日志记录：**

```
[YYYY-MM-DD HH:MM:SS] ========== 步骤 4: 生成任务 ID ==========
[YYYY-MM-DD HH:MM:SS] 调用: uuid_utils.py
[YYYY-MM-DD HH:MM:SS] 生成任务ID: <UUID>
[YYYY-MM-DD HH:MM:SS] 步骤 4 完成，状态: 成功
```

```bash
uv run ./scripts/uuid_utils.py
```

输出示例：

```
550e8400-e29b-41d4-a716-446655440000
```

---

### 步骤 5: 字段映射

**日志记录：**

```
[YYYY-MM-DD HH:MM:SS] ========== 步骤 5: 字段映射 ==========
[YYYY-MM-DD HH:MM:SS] 开始映射 <N> 个字段...
[YYYY-MM-DD HH:MM:SS] 
[YYYY-MM-DD HH:MM:SS] [字段 1/32] CPDL
[YYYY-MM-DD HH:MM:SS]   来源: 固定值
[YYYY-MM-DD HH:MM:SS]   映射值: 书板
[YYYY-MM-DD HH:MM:SS]   状态: 成功
[YYYY-MM-DD HH:MM:SS] 
[YYYY-MM-DD HH:MM:SS] [字段 2/32] CPLB
[YYYY-MM-DD HH:MM:SS]   来源: Format=Paperback
[YYYY-MM-DD HH:MM:SS]   调用: product_categories.py --select Paperback
[YYYY-MM-DD HH:MM:SS]   查询结果: CPLB=无线胶装书, ZLBM=23.01
[YYYY-MM-DD HH:MM:SS]   映射值: 23.01;无线胶装书
[YYYY-MM-DD HH:MM:SS]   状态: 成功
[YYYY-MM-DD HH:MM:SS] 
[YYYY-MM-DD HH:MM:SS] [字段 3/32] CPMC
[YYYY-MM-DD HH:MM:SS]   来源: 任务ID + CPLB + Series Name
[YYYY-MM-DD HH:MM:SS]   映射值: <任务ID>-<CPLB>-<系列名称>
[YYYY-MM-DD HH:MM:SS]   状态: 成功
[YYYY-MM-DD HH:MM:SS] 
... (每个字段的详细日志)
[YYYY-MM-DD HH:MM:SS] 
[YYYY-MM-DD HH:MM:SS] 字段映射完成:
[YYYY-MM-DD HH:MM:SS]   成功: <N> 个
[YYYY-MM-DD HH:MM:SS]   使用默认值: <N> 个
[YYYY-MM-DD HH:MM:SS]   失败: <N> 个
[YYYY-MM-DD HH:MM:SS] 步骤 5 完成，状态: <成功/部分成功/失败>
```

#### 5.1 产品大类 (CPDL)

| 属性 | 值 |
|------|-----|
| 默认值 | `书板` |
| 数据来源 | 固定值 |

---

#### 5.2 产品类别 (CPLB)

**映射流程：**

1. 从 `customer_data` 提取 `Format` 字段值
2. 查询映射表：

```bash
uv run ./scripts/product_categories.py --select "Paperback"
```

1. 解析输出：

```
匹配结果：
CPLB: 无线胶装书, ZLBM: 23.01
```

1. 组合为 `23.01;无线胶装书`

**异常处理：**

- 无匹配时返回 `99.99;其他`
- 记录到日志

---

#### 5.3 书脊方向 (SJFX)

> **TODO:** 根据装订方式推断
>
> | 装订方式 | 默认书脊方向 |
> |----------|--------------|
> | 无线胶装 | 短书脊 |
> | 骑马钉 | 长书脊 |
> | 精装 | 四边裁 |

---

#### 5.4 装订方式 (ZDFS)

> **TODO:** 客户术语映射到 ERP 术语
>
> | 客户术语 | ERP 术语 |
> |----------|----------|
> | 无线胶装 | 胶状/PAD书 |
> | 精装书 | 精装书 |
> | 骑订书 | 骑马订 |
> | 盒类 | 其他 |
> | 套装 | 套装 |
> | 车线书 | 车线书 |
> | 线圈书 | 线圈书 |
> | BB书 | 快线BB书 |
> | BB书 | 中线BB书 |
> | BB书 | 慢线BB书 |

---

#### 5.5 产品名称 (CPMC)

**格式：** `<任务ID>-<CPLB>-<系列名称>`

**示例：**

```
550e8400-e29b-41d4-a716-446655440000-23.01;无线胶装书-Puzzle & Colour Disney
```

---

#### 5.6 款数 (TZS)

| 属性 | 说明 |
|------|------|
| 数据来源 | `Number of titles` |
| 类型 | 整数 |
| 默认值 | 1 |

---

#### 5.7 语言数量 (YYS)

| 属性 | 值 |
|------|-----|
| 默认值 | `1` |
| 覆盖条件 | customer_data 明确提及 |

---

#### 5.8 单位 (DWMC)

| 属性 | 值 |
|------|-----|
| 默认值 | `本` |

---

#### 5.9 产品数量

> **TODO:** 从 `Quantity` / `Print Run` / `数量` 提取，无明确信息时留空

---

#### 5.10 客户币种 (BZ)

| 属性 | 值 |
|------|-----|
| 默认值 | `美元` |
| 可选值 | 美元, 人民币, 港币, 欧元, 日元, 澳元, 英镑 |

---

#### 5.11 客户目标价 (KHMBJ)

| 属性 | 值 |
|------|-----|
| 默认值 | `0` |

---

#### 5.12 规格 (GG_length, GG_width, GG_height)

**数据来源：** `Size (tps) H x W x D mm`

**解析示例：**

```
输入: 198mm x 129mm x 10mm
输出:
  GG_length: 198
  GG_width: 129
  GG_height: 10
```

**如果没有提供 D 尺寸，需要计算：**

1. 提取 `Extent (pp + cover)`：

```
输入：120pp + cover 
输出：
  extent_pp: 120
  extent_cover: 1
```

1. 提取 `Text Material`：

```
输入：90gsm woodfree
输出：
  extent_pp_faw: 90
  extent_pp_type: woodfree
```

1. 提取 `Cover Material`：

```
输入：250gsm C1S artboard
输出：
  extent_cover_faw: 250
  extent_cover_type: C1S_artboard
```

1. 查询纸张厚度：

```bash
uv run ./scripts/select_page_thickness.py --pg 光粉纸 --faw 80
```

1. 计算 `gg_height`：

**公式：**

```
gg_height = (extent_pp / 2) * extent_pp_thickness + extent_cover * extent_cover_thickness
```

**计算示例：**

```
输入：
  extent_pp = 120
  extent_pp_thickness = 0.09 (90g 光粉纸)
  extent_cover = 1
  extent_cover_thickness = 0.25 (250g 光粉纸)

计算：
  (120 / 2) * 0.09 + 1 * 0.25 = 5.4 + 0.25 = 5.65mm
```

---

#### 5.13 包装方式 (BZFS)

> **TODO:** 可选值：纸箱包装 / 牛皮纸包装 / 胶带包装 / 卡板摆放

---

#### 5.14 台板类型 (TBLX)

> **TODO:** 可选值：
>
> - `1.2m*0.8m(欧式)实木消毒卡板`
> - `1.2*1.0m(欧式)实木消毒卡板`
> - `1.2m*0.8m(欧标)松木消毒卡板`
> - `1.2*1.0松木消毒卡板(四边喷环保蓝漆)`
> - `1.165m*1.165m(欧式)实木消毒木卡板`
> - `1.2*1.0m实木消毒卡板(Highights专用)`
> - `1.2m*1.0m(欧标)松木消毒卡板（有盖板）`
> - `不打卡板`
> - `1.0m*0.8m(欧式)实木消毒卡板`

---

#### 5.15 送货方式 (SHFS)

> **TODO:** 可选值：
>
> - FOB(NGV)
> - FOB盐田
> - 送香港公司
> - DPU
> - 直交工厂
> - DAP
> - 物流速递
> - 工厂自提
> - 送国内
> - FOB(美加线)-香港
> - FOB蛇口
> - FOB(美加线)-盐田
> - FOB香港
> - CIF DAT

---

#### 5.16 客户单号 (KHCPMC)

**数据来源：** `Works References`

---

#### 5.17 交货日期 (JHRQ)

**格式：** 时间戳（毫秒）

**处理流程：**

1. 提取 `Pub Month` 字段，如 `May-27`
2. 标准化为 `2027-05-01`
3. 转换为时间戳：

```bash
uv run ./scripts/string2timestamp.py 2027-05-01
# 输出: 1798752000000
```

**支持的日期格式：**

- `2027-05-01`
- `2027/05/01`
- `May-27` → `2027-05-01`
- `2027-05` → `2027-05-01`

**年份推断：**

- `May-27` 解释为 2027 年
- 如已过期，使用下一年

---

#### 5.18 FSC 类别

> **TODO:** 可选值：
>
> - FSC Mix
> - FSC 100%
> - FSC Mix 70%
> - FSC Mix 85%
> - FSC Mix Credit
> - FSC Recycled 85%
> - FSC Recycled 100%
> - FSC Mix 90%
> - FSC Mix 100%
> - FSC
> - FSC Recycled

---

#### 5.19 返单香港单号

> **TODO:** 返单产品时填写

---

#### 5.20 是否有配件 (SFYPJ)

| 条件 | 结果 |
|------|------|
| `Accessories` / `配件` 有具体描述 | `true` |
| 明确标注 "无" 或未提及 | `false` |

---

#### 5.21 配件描述 (Remark1)

当 `SFYPJ` 为 `true` 时填写。

---

#### 5.22 排序号 (PXH)

| 属性 | 值 |
|------|-----|
| 默认值 | `1` |

---

#### 5.23 是否需要安全检查 (has_safety_checks)

| 输入值 | 结果 |
|--------|------|
| `Y`, `Yes`, `是` | `true` |
| `N`, `No`, `否`, 空 | `false` |

**数据来源：** `Safety Checks Req? (Y/N)`

---

#### 5.24 用户年龄段 (target_age_group)

| 属性 | 说明 |
|------|------|
| 默认值 | `0` |
| 数据来源 | `Target Age Group` |
| 示例 | `7+` → `7`, `3-6` → `3` |

---

#### 5.25 产品类型 (CPLX)

| 类型 | 判断条件 |
|------|----------|
| 新产品 | 根据用户的订单信息判断 |
| 返单产品 | 根据用户的订单信息判断 |
| 半返单旧产品 | 根据用户的订单信息判断 |
| 半返单新产品 | 根据用户的订单信息判断 |
| 特殊产品 | 客户明确标注 |

> **TODO:** 需要与历史订单系统对接

---

#### 5.26 预计下单日期 (WCRQ)

| 属性 | 值 |
|------|-----|
| 默认值 | 当前日期 + 10 天 |
| 格式 | 时间戳（毫秒） |

---

#### 5.27 香港单号 (KHDDH)

| 属性 | 值 |
|------|-----|
| 默认值 | 空字符串 |

---

#### 5.28 产品描述 (CPMS)

**数据来源：** `Project description`

---

#### 5.29 补充描述 (BCMS)

| 属性 | 值 |
|------|-----|
| 默认值 | 空字符串 |

---

#### 5.30 检测项目 (Remark2)

| 属性 | 值 |
|------|-----|
| 默认值 | 空字符串 |

---

#### 5.31 修改记录 (Remark3)

| 属性 | 值 |
|------|-----|
| 默认值 | 空字符串 |

---

#### 5.32 备注 (CPBZ1)

**数据来源：** customer_data 中的备注字段

---

#### 5.33 任务 ID (id)

**数据来源：** 步骤 4: 生成任务 ID

---

### 步骤 6: 输出结果

**日志记录：**

```
[YYYY-MM-DD HH:MM:SS] ========== 步骤 6: 输出结果 ==========
[YYYY-MM-DD HH:MM:SS] 任务ID: <UUID>
[YYYY-MM-DD HH:MM:SS] 创建任务目录: <任务目录路径>
[YYYY-MM-DD HH:MM:SS] 
[YYYY-MM-DD HH:MM:SS] [文件 1/3] CSV转换
[YYYY-MM-DD HH:MM:SS]   源文件: <原文件.xlsx>
[YYYY-MM-DD HH:MM:SS]   输出: <原文件.csv>
[YYYY-MM-DD HH:MM:SS]   状态: 成功
[YYYY-MM-DD HH:MM:SS] 
[YYYY-MM-DD HH:MM:SS] [文件 2/3] ERP JSON
[YYYY-MM-DD HH:MM:SS]   文件名: ERP_<task_id>_<时间戳>.json
[YYYY-MM-DD HH:MM:SS]   字段数: <N>
[YYYY-MM-DD HH:MM:SS]   输出路径: <完整路径>
[YYYY-MM-DD HH:MM:SS]   状态: 成功
[YYYY-MM-DD HH:MM:SS] 
[YYYY-MM-DD HH:MM:SS] [文件 3/3] 原始文件归档
[YYYY-MM-DD HH:MM:SS]   源路径: <pending/原文件.xlsx>
[YYYY-MM-DD HH:MM:SS]   目标路径: <任务目录/原文件.xlsx>
[YYYY-MM-DD HH:MM:SS]   状态: 成功
[YYYY-MM-DD HH:MM:SS] 
[YYYY-MM-DD HH:MM:SS] 输出文件列表:
[YYYY-MM-DD HH:MM:SS]   - <任务目录>/ERP_<task_id>_<时间戳>.json
[YYYY-MM-DD HH:MM:SS]   - <任务目录>/<原文件>.csv
[YYYY-MM-DD HH:MM:SS]   - <任务目录>/<原文件>.xlsx
[YYYY-MM-DD HH:MM:SS]   - <任务目录>/quote_request_<task_id>_<时间戳>.log
[YYYY-MM-DD HH:MM:SS] 
[YYYY-MM-DD HH:MM:SS] 步骤 6 完成，状态: 成功
[YYYY-MM-DD HH:MM:SS] ========== 任务完成 ==========
[YYYY-MM-DD HH:MM:SS] 总耗时: <N> 秒
[YYYY-MM-DD HH:MM:SS] 处理状态: 成功
```

#### 任务目录结构

每个任务使用 UUID4 作为任务ID，创建独立目录：

```
processed/
└── 1954130b-9f01-42bf-9c66-9dae4b71757d/     # 任务ID（UUID4）
    ├── ERP_1954130b-9f01-42bf-9c66-9dae4b71757d_20260331_035709.json
    ├── PUZZLE COLOUR DISNEY.csv
    └── PUZZLE COLOUR DISNEY.xlsx
```

#### 输出文件命名规范

**ERP JSON 文件：**

```
ERP_<task_id>_<YYYYMMDD>_<HHMMSS>.json
```

示例：`ERP_1954130b-9f01-42bf-9c66-9dae4b71757d_20260331_035709.json`

**任务ID格式：**

- 使用标准 UUID4 格式（36字符，含连字符）
- 示例：`1954130b-9f01-42bf-9c66-9dae4b71757d`

**文件组织原则：**

1. **任务隔离**：每个任务拥有独立目录，防止文件冲突
2. **完整追溯**：同一任务的所有文件（原始XLSX、转换CSV、ERP JSON）在同一目录
3. **防重复处理**：原始文件处理完成后自动从 `pending` 目录移出

#### 输出格式

**ERP JSON 示例：**

```json
{
  "CPDL": "书板",
  "CPLB": "23.01;无线胶装书",
  "CPMC": "1954130b-9f01-42bf-9c66-9dae4b71757d-23.01;无线胶装书-Puzzle & Colour Disney",
  "TZS": 2,
  "YYS": 1,
  "DWMC": "本",
  "BZ": "美元",
  "KHMBJ": 0,
  "GG_length": 198,
  "GG_width": 129,
  "GG_height": 10,
  "JHRQ": 1798752000000,
  "WCRQ": 1799616000000,
  "has_safety_checks": true,
  "target_age_group": 7,
  "SFYPJ": false,
  "CPMS": "Puzzle and colouring book"
}
```

**CSV 文件：**

- 原始 Excel 文件的标准 CSV 转换
- 使用 UTF-8-SIG 编码（带BOM，兼容Excel）
- 保留所有原始数据行和列

---

## 错误处理

### 严重错误（中止任务）

| 错误类型 | 处理方式 |
|----------|----------|
| 文件格式不支持 | 报错，提示支持的格式 |
| 文件为空 | 报错，提示检查文件 |
| 缺少必需字段 | 报错，列出缺失字段 |
| uv 安装失败 | 提示手动安装 |

### 可恢复错误（使用默认值）

| 错误类型 | 处理方式 |
|----------|----------|
| 字段映射失败 | 使用默认值，记录日志 |
| 日期解析失败 | 使用默认日期，记录日志 |
| 产品类别匹配失败 | 使用 `99.99;其他`，记录日志 |
| 脚本执行失败 | 使用默认值，记录日志 |

### 日志记录

> **强制要求**：每个步骤必须记录开始、执行过程、结果。日志文件是任务追溯的重要依据。

**日志文件命名：**

```
./logs/quote_request_<task_id>_<timestamp>.log
```

**示例：**

```
./logs/quote_request_102091a5-16ec-4d2e-b724-9db2dc78081e_20260330_090900.log
```

**日志写入时机：**

1. **任务开始时** - 记录任务ID、源文件、开始时间
2. **每个步骤开始时** - 记录步骤名称、开始时间
3. **每个步骤执行中** - 记录关键操作、调用脚本、数据提取
4. **每个步骤结束时** - 记录步骤结果、状态、耗时
5. **任务结束时** - 记录总耗时、最终状态、输出文件列表

**日志内容结构（完整示例）：**

```
[2026-03-30 09:09:00] ========== 任务开始 ==========
[2026-03-30 09:09:00] 任务ID: 102091a5-16ec-4d2e-b724-9db2dc78081e
[2026-03-30 09:09:00] 源文件: PUZZLE COLOUR DISNEY.xlsx
[2026-03-30 09:09:00] 开始时间: 2026-03-30 09:09:00
[2026-03-30 09:09:00] 
[2026-03-30 09:09:00] ========== 步骤 1: 输入验证 ==========
[2026-03-30 09:09:00] 输入文件: /path/to/PUZZLE COLOUR DISNEY.xlsx
[2026-03-30 09:09:00] 文件格式检查: 通过 (.xlsx)
[2026-03-30 09:09:00] 文件大小: 235494 字节
[2026-03-30 09:09:00] 格式转换: xlsx→csv 完成
[2026-03-30 09:09:00] 必需字段检查: 通过
[2026-03-30 09:09:00] 步骤 1 完成，状态: 成功，耗时: 0.5s
[2026-03-30 09:09:00] 
[2026-03-30 09:09:00] ========== 步骤 2: 读取客户数据 ==========
[2026-03-30 09:09:00] 文件类型: xlsx
[2026-03-30 09:09:00] 表格格式检测: 横向(标签行+数据行)
[2026-03-30 09:09:00] 读取行数: 45
[2026-03-30 09:09:00] 提取字段数: 42
[2026-03-30 09:09:00] 提取的字段列表:
[2026-03-30 09:09:00]   - Pub Month: 2027-05-01 00:00:00
[2026-03-30 09:09:00]   - Series Name: Puzzle & Colour Disney
[2026-03-30 09:09:00]   - Number of titles: 2
[2026-03-30 09:09:00]   - Format: Paperback
[2026-03-30 09:09:00]   ... (共42个字段)
[2026-03-30 09:09:00] 步骤 2 完成，状态: 成功，耗时: 0.3s
[2026-03-30 09:09:00] 
[2026-03-30 09:09:00] ========== 步骤 3: 读取 ERP 模板 ==========
[2026-03-30 09:09:00] 模板文件: ./assets/add_customer_quote_request.json
[2026-03-30 09:09:00] 模板字段数: 32
[2026-03-30 09:09:00] 模板字段列表:
[2026-03-30 09:09:00]   - CPDL: 书板
[2026-03-30 09:09:00]   - CPLB: 
[2026-03-30 09:09:00]   - CPMC: 
[2026-03-30 09:09:00]   ... (共32个字段)
[2026-03-30 09:09:00] 步骤 3 完成，状态: 成功，耗时: 0.1s
[2026-03-30 09:09:00] 
[2026-03-30 09:09:00] ========== 步骤 4: 生成任务 ID ==========
[2026-03-30 09:09:00] 调用: uuid_utils.py
[2026-03-30 09:09:00] 生成任务ID: 102091a5-16ec-4d2e-b724-9db2dc78081e
[2026-03-30 09:09:00] 步骤 4 完成，状态: 成功，耗时: 0.2s
[2026-03-30 09:09:00] 
[2026-03-30 09:09:00] ========== 步骤 5: 字段映射 ==========
[2026-03-30 09:09:00] 开始映射 32 个字段...
[2026-03-30 09:09:00] 
[2026-03-30 09:09:00] [字段 1/32] CPDL
[2026-03-30 09:09:00]   来源: 固定值
[2026-03-30 09:09:00]   映射值: 书板
[2026-03-30 09:09:00]   状态: 成功
[2026-03-30 09:09:00] 
[2026-03-30 09:09:00] [字段 2/32] CPLB
[2026-03-30 09:09:00]   来源: Format=Paperback
[2026-03-30 09:09:00]   调用: product_categories.py --select Paperback
[2026-03-30 09:09:00]   查询结果: CPLB=无线胶装书, ZLBM=23.01
[2026-03-30 09:09:00]   映射值: 23.01;无线胶装书
[2026-03-30 09:09:00]   状态: 成功
[2026-03-30 09:09:00] 
[2026-03-30 09:09:00] [字段 3/32] CPMC
[2026-03-30 09:09:00]   来源: 任务ID + CPLB + Series Name
[2026-03-30 09:09:00]   映射值: 102091a5-16ec-4d2e-b724-9db2dc78081e-23.01;无线胶装书-Puzzle & Colour Disney
[2026-03-30 09:09:00]   状态: 成功
[2026-03-30 09:09:00] 
[2026-03-30 09:09:00] [字段 4/32] TZS
[2026-03-30 09:09:00]   来源: Number of titles=2
[2026-03-30 09:09:00]   映射值: 2
[2026-03-30 09:09:00]   状态: 成功
[2026-03-30 09:09:00] 
[2026-03-30 09:09:01] [字段 5/32] GG_length
[2026-03-30 09:09:01]   来源: Size (tps) H x W x D mm=198mm x 129mm
[2026-03-30 09:09:01]   解析: length=198, width=129
[2026-03-30 09:09:01]   映射值: 198
[2026-03-30 09:09:01]   状态: 成功
[2026-03-30 09:09:01] 
[2026-03-30 09:09:01] [字段 6/32] GG_height
[2026-03-30 09:09:01]   来源: 计算
[2026-03-30 09:09:01]   计算过程:
[2026-03-30 09:09:01]     - Extent: 120pp + cover
[2026-03-30 09:09:01]     - Text Material: 90gsm woodfree
[2026-03-30 09:09:01]     - Cover Material: 250gsm C1S artboard
[2026-03-30 09:09:01]     - 调用: select_page_thickness.py --pg 书纸 --faw 90g
[2026-03-30 09:09:01]     - 书纸厚度: 0.09
[2026-03-30 09:09:01]     - 调用: select_page_thickness.py --pg 光粉纸 --faw 250g
[2026-03-30 09:09:01]     - 光粉纸厚度: 0.25
[2026-03-30 09:09:01]     - 公式: (120/2)*0.09 + 1*0.25 = 5.65
[2026-03-30 09:09:01]   映射值: 5.65
[2026-03-30 09:09:01]   状态: 成功
[2026-03-30 09:09:01] 
[2026-03-30 09:09:01] [字段 7/32] JHRQ
[2026-03-30 09:09:01]   来源: Pub Month=2027-05-01 00:00:00
[2026-03-30 09:09:01]   调用: string2timestamp.py 2027-05-01 00:00:00
[2026-03-30 09:09:01]   转换结果: 1809129600000
[2026-03-30 09:09:01]   映射值: 1809129600000
[2026-03-30 09:09:01]   状态: 成功
[2026-03-30 09:09:01] 
[2026-03-30 09:09:01] [字段 8/32] has_safety_checks
[2026-03-30 09:09:01]   来源: Safety Checks Req? (Y/N)=Y
[2026-03-30 09:09:01]   映射值: true
[2026-03-30 09:09:01]   状态: 成功
[2026-03-30 09:09:01] 
[2026-03-30 09:09:01] [字段 9/32] target_age_group
[2026-03-30 09:09:01]   来源: Target Age Group=7+
[2026-03-30 09:09:01]   解析: 提取数字 7
[2026-03-30 09:09:01]   映射值: 7
[2026-03-30 09:09:01]   状态: 成功
[2026-03-30 09:09:01] 
[2026-03-30 09:09:01] [字段 10/32] KHCPMC
[2026-03-30 09:09:01]   来源: Works References=23992 / 23993
[2026-03-30 09:09:01]   映射值: 23992 / 23993
[2026-03-30 09:09:01]   状态: 成功
[2026-03-30 09:09:01] 
[2026-03-30 09:09:01] [字段 11/32] CPMS
[2026-03-30 09:09:01]   来源: Project description=Puzzle and colouring book
[2026-03-30 09:09:01]   映射值: Puzzle and colouring book
[2026-03-30 09:09:01]   状态: 成功
[2026-03-30 09:09:01] 
[2026-03-30 09:09:01] [字段 12/32] SJFX
[2026-03-30 09:09:01]   来源: 未提供
[2026-03-30 09:09:01]   使用默认值: ""
[2026-03-30 09:09:01]   状态: WARN
[2026-03-30 09:09:01] 
[2026-03-30 09:09:01] ... (其他字段类似)
[2026-03-30 09:09:02] 
[2026-03-30 09:09:02] 字段映射完成:
[2026-03-30 09:09:02]   成功: 11 个
[2026-03-30 09:09:02]   使用默认值: 21 个
[2026-03-30 09:09:02]   失败: 0 个
[2026-03-30 09:09:02] 步骤 5 完成，状态: 成功，耗时: 1.2s
[2026-03-30 09:09:02] 
[2026-03-30 09:09:02] ========== 步骤 6: 输出结果 ==========
[2026-03-30 09:09:02] 任务ID: 102091a5-16ec-4d2e-b724-9db2dc78081e
[2026-03-30 09:09:02] 创建任务目录: processed/102091a5-16ec-4d2e-b724-9db2dc78081e/
[2026-03-30 09:09:02] 
[2026-03-30 09:09:02] [文件 1/3] CSV转换
[2026-03-30 09:09:02]   源文件: PUZZLE COLOUR DISNEY.xlsx
[2026-03-30 09:09:02]   输出: PUZZLE COLOUR DISNEY.csv
[2026-03-30 09:09:02]   状态: 成功
[2026-03-30 09:09:02] 
[2026-03-30 09:09:02] [文件 2/3] ERP JSON
[2026-03-30 09:09:02]   文件名: ERP_102091a5-16ec-4d2e-b724-9db2dc78081e_20260330_090902.json
[2026-03-30 09:09:02]   字段数: 32
[2026-03-30 09:09:02]   输出路径: processed/102091a5-16ec-4d2e-b724-9db2dc78081e/ERP_102091a5-16ec-4d2e-b724-9db2dc78081e_20260330_090902.json
[2026-03-30 09:09:02]   状态: 成功
[2026-03-30 09:09:02] 
[2026-03-30 09:09:02] [文件 3/3] 原始文件归档
[2026-03-30 09:09:02]   源路径: pending/PUZZLE COLOUR DISNEY.xlsx
[2026-03-30 09:09:02]   目标路径: processed/102091a5-16ec-4d2e-b724-9db2dc78081e/PUZZLE COLOUR DISNEY.xlsx
[2026-03-30 09:09:02]   状态: 成功
[2026-03-30 09:09:02] 
[2026-03-30 09:09:02] 输出文件列表:
[2026-03-30 09:09:02]   - processed/102091a5-16ec-4d2e-b724-9db2dc78081e/ERP_102091a5-16ec-4d2e-b724-9db2dc78081e_20260330_090902.json
[2026-03-30 09:09:02]   - processed/102091a5-16ec-4d2e-b724-9db2dc78081e/PUZZLE COLOUR DISNEY.csv
[2026-03-30 09:09:02]   - processed/102091a5-16ec-4d2e-b724-9db2dc78081e/PUZZLE COLOUR DISNEY.xlsx
[2026-03-30 09:09:02]   - logs/quote_request_102091a5-16ec-4d2e-b724-9db2dc78081e_20260330_090900.log
[2026-03-30 09:09:02] 
[2026-03-30 09:09:02] 步骤 6 完成，状态: 成功，耗时: 0.5s
[2026-03-30 09:09:02] 
[2026-03-30 09:09:02] ========== 任务完成 ==========
[2026-03-30 09:09:02] 总耗时: 2.8 秒
[2026-03-30 09:09:02] 处理状态: 成功
[2026-03-30 09:09:02] ========== 任务结束 ==========
```

**日志记录级别：**

- `INFO`: 正常流程信息
- `WARN`: 使用默认值、数据缺失等警告
- `ERROR`: 处理错误、阻碍信息
- `DEBUG`: 详细调试信息（可选）

**日志目录结构：**

```
logs/
├── quote_request_<task_id>_<timestamp>.log    # 单个任务日志
└── archive/                                    # 归档日志（可选）
```

---

## 依赖管理

### 检查 uv 安装

```bash
which uv
```

### 自动安装

> 检测到未安装 `uv`（Python 包管理工具），是否自动安装？(yes/no)

**如果 yes：**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env
```

**如果 no：**

- 中止任务
- 提示手动安装：<https://docs.astral.sh/uv/getting-started/installation/>

### 安装依赖

```bash
uv sync
```

---

## 文件结构

```
customer-quote-request/
├── SKILL.md                          # 本文件
├── assets/
│   ├── add_customer_quote_request.json    # ERP 字段模板
│   ├── customer_erp_mapper.xlsx      # 客户字段映射表
│   ├── product_categories.xlsx       # 产品类别映射表
│   └── page_thickness.xlsx           # 纸张厚度表
├── customer-files/                   # 客户文件目录
│   ├── pending/                      # 待处理文件（处理前放入此处）
│   ├── processed/                    # 已处理完成的任务目录
│   │   └── <task_id>/               # UUID4 任务目录
│   │       ├── ERP_<task_id>_<timestamp>.json
│   │       ├── <原文件名>.csv
│   │       └── <原文件名>.xlsx
│   └── failed/                       # 处理失败的任务目录
│       └── failed_<timestamp>/
│           └── <原文件名>.xlsx
├── logs/                             # 日志目录
│   └── quote_request_<task_id>_<timestamp>.log
├── scripts/
│   ├── main.py                       # 主入口（推荐）
│   ├── uuid_utils.py                 # UUID 生成
│   ├── product_categories.py         # 产品类别查询
│   ├── customer2erp.py               # ERP 字段映射查询
│   ├── string2timestamp.py           # 日期转时间戳
│   ├── xlsx2csv.py                   # Excel 转 CSV
│   └── select_page_thickness.py      # 纸张厚度查询
├── pyproject.toml                    # Python 依赖
└── uv.lock                          # uv 锁定文件
```

---

## 脚本使用说明

### uuid_utils.py

生成 UUID4。

```bash
uv run ./scripts/uuid_utils.py
# 输出: 550e8400-e29b-41d4-a716-446655440000
```

### product_categories.py

查询产品类别映射。

```bash
uv run ./scripts/product_categories.py --select "Paperback"
# 输出:
# 匹配结果：
# CPLB: 无线胶装书, ZLBM: 23.01
```

### string2timestamp.py

日期转时间戳（毫秒）。

```bash
uv run ./scripts/string2timestamp.py 2027-05-01
# 输出: 1798752000000

uv run ./scripts/string2timestamp.py May-27
# 输出: 1798752000000 (解释为 2027-05-01)
```

### xlsx2csv.py

Excel 文件转 CSV。

```bash
# 基本用法
python xlsx2csv.py input.xlsx

# 指定输出文件
python xlsx2csv.py input.xlsx -o output.csv

# 指定工作表
python xlsx2csv.py input.xlsx -s "Sheet2"
```

### select_page_thickness.py

查询纸张厚度。

```bash
uv run ./scripts/select_page_thickness.py --pg 光粉纸 --faw 80
# 输出: 0.07
```

### customer2erp.py

查询客户 ERP 字段映射。

```bash
uv run ./scripts/customer2erp.py --field "Format" --value "Paperback"
```

---

## 版本历史

| 版本 | 日期 | 变更 |
|------|------|------|
| 1.0.0 | 2026-03-26 | 初始版本 |
| 1.0.1 | 2026-03-26 | 修复 uuid_utils.py 重复生成问题，优化日期解析 |
| 1.0.2 | 2026-03-30 | 添加纸张厚度计算，修复格式问题 |
| 1.0.3 | 2026-03-30 | 优化输出文件命名规范（ERP_<task_id>_<时间戳>.json），完善日志记录机制 |
| 1.0.4 | 2026-03-31 | 重构任务目录结构：使用 UUID4 作为任务ID，创建独立任务目录，同时输出 CSV 和 ERP JSON，自动移动原始文件防止重复处理 |
| 1.0.5 | 2026-03-10 | 子啊输出的JSON中加入`id`和`create_by`字段 |

---

## TODO 清单

- [x] 优化输出文件命名规范（ERP_<task_id>_<时间戳>.json）
- [x] 完善日志记录机制
- [x] 重构任务目录结构（UUID4 任务ID + 独立目录）
- [x] 添加 CSV 转换输出
- [x] 实现文件防重复处理机制
- [X] 完善书脊方向的选择和装订类型映射
- [ ] 添加纸张类型映射表
- [ ] 完善装订方式映射
- [ ] 添加包装方式映射
- [ ] 添加台板类型映射
- [ ] 添加送货方式映射
- [ ] 添加 FSC 类别映射
- [ ] 实现产品类型自动判断（需对接历史订单系统）
- [ ] 添加更多日期格式支持
- [ ] 添加单元测试
