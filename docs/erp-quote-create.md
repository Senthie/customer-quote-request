# `erp_quote_create` 入参协议

> 本文逐字段对照以下实现校对（2026-11-12）：
> [`service.py`](../src/erp_tools/tools/quote_create/service.py)（`prepare()` / `create()`）、
> [`fields.py`](../src/erp_tools/tools/quote_create/fields.py)（明细字段注册表）、
> [`parser.py`](../src/erp_tools/tools/quote_create/parser.py)（`HEADER_FIELDS` / `PRODUCT_ARG_FIELDS`）。
> 字段清单来自真实页面抓取：`scripts/probe-quote-fields.py`。

**会写入 ERP。** 新建一条「客户报价需求」= 主表 + 1 条产品明细（含产品数量）。
`confirm=false`（默认）只校验并返回 `needs_confirmation`，不落库。

| 项目 | 值 |
| --- | --- |
| 工具名 | `erp_quote_create` |
| 写库 | ✅（必须先过 `erp_quote_prepare` 同一套校验） |
| 一次明细数 | **仅 1 条**，多条返回 `status=unsupported` |
| 必填 | 协议层：`customer` / `sales`（档案可补）/ `products[0].name/spec/qty`；ERP 端另有 **14 个**明细必填字段，见 3.4 |
| 可上传明细字段 | **51 个**（DOM 首屏可见 且 可写），逐字段见 3.3 |
| 鉴权 | `Authorization: Bearer <api-key>` |

---

## 1. 参数形态

就是 MCP 工具调用的 `arguments` 对象，等价于
`QuoteCreateService.create(**kwargs)` 的关键字参数：**全部扁平、全部可选**，
只有 `customer` / `sales` / `products` 三项在业务上必需，其余留空即沿用客户档案默认值。

## 2. 完整 JSON 示例

```json
{
  "customer": "IBL10",
  "sales": "许月霞",
  "merchandiser": "李东员",
  "contact": "张三",
  "phone": "028-1234",
  "mobile": "13800000000",
  "fax": "028-5678",
  "discount": "10",
  "remark": "MCP 51 字段端到端验证",
  "product_memo": "多产品数量-表头产品描述测试",
  "confirm": true,
  "products": [
    {
      "name": "512961503",
      "spec": "75*28*298",
      "qty": 3000,
      "fields": {
        "product_name": "512961503  INDIVIDUAL  BOX,SO15631(B),STD",
        "unit": "个",
        "customer_order_no": "PO-MCP-51-001",
        "product_name_en": "INDIVIDUAL BOX",
        "product_group": "书版",
        "length_mm": 75,
        "width_mm": 28,
        "height_mm": 298,
        "spine_direction": "长书脊",
        "sort_no": 1,
        "binding": "胶装/PAD书",
        "packing": "纸箱包装",
        "pallet": "不打卡板",
        "delivery": "FOB(NGV)",
        "transport_type": "FOB盐田(普通)",
        "delivery_area": "FOB(NGV)",
        "carton_type": "普通纸箱",
        "board_type": "A=B双坑纸板",
        "packing_orientation": "平装",
        "bottom_card": "无需",
        "no_pallet": false,
        "no_container": false,
        "delivery_date": "2026-12-20",
        "expected_date": "2026-12-01",
        "has_schedule": false,
        "attribute": "三岁以上",
        "product_type": "新产品",
        "fsc_category": "FSC Mix",
        "has_parts": "否",
        "no_quote": false,
        "product_desc": "MCP 51 字段验证：独立彩盒，四色印刷",
        "extra_desc": "补充描述测试",
        "remark": "明细备注测试",
        "parts_desc": "无",
        "modify_log": "MCP 自动化验证，可删除",
        "inspection": "无",
        "old_hk_order_no": "XY26111002",
        "target_price_currency": "人民币",
        "target_price": 2.5,
        "cases": 1,
        "languages": 1,
        "qty_1": 3000,
        "qty_2": 5000,
        "qty_3": 6000,
        "qty_4": 7000,
        "qty_5": 0,
        "qty_6": 0,
        "qty_7": 0,
        "qty_8": 0,
        "qty_9": 0,
        "qty_10": 0
      }
    }
  ]
}

```

> 老写法（明细字段直接平铺在 `products[0]` 上，如 `"packing": "纸箱包装"`）仍然有效，
> 与写进 `fields` 完全等价；新代码建议统一用 `fields`，和 `upload.json_schema` 一一对应。

> **51 个字段全填**的可运行样例：[`samples/quote-create-51-fields.json`](samples/quote-create-51-fields.json)
> （已对真实 ERP 跑通，见第 8 节实测记录）。

## 3. 完整 JSON 逐字段说明

> 逐条对应第 2 节那份 JSON。**`ERP 字段名` 就是页面上真实的控件 `name`**——
> 传它和传「MCP 字段名」完全等价（`fields` 里两者都认）。

### 3.1 外层 / 表头参数（12 个）

| MCP 字段名 | ERP 字段名 | 中文含义 | 备注 |
| --- | --- | --- | --- |
| `customer` | `txtKHID` / `txtKHBM` / `txtKHMC` | 客户 | 传名称/编码/简称的**片段**，在客户资料里模糊检索，**必须唯一命中**（0 条或多条都报错并列出候选）。不是直接提交的文本，而是通过客户资料页把 ID + 编码 + 名称一起带过来 |
| `sales` | `txtYGXM` | 业务员 | 传**显示名**（如 `许月霞`），内部换成页面下拉里的 GUID；留空取客户档案默认值 |
| `merchandiser` | `txtGDYXM` | 跟单员 | 同上，必须是该下拉里的显示名 |
| `price_system` | `txtDJTX` | 单价体系 | 示例里没写；可选，形如 `II级(标准)`，同样必须在下拉里 |
| `contact` | `txtLXR` | 联系人 | 覆盖客户档案默认值 |
| `phone` | `txtLXDH` | 电话 | 覆盖客户档案默认值 |
| `mobile` | `txtSJ` | 手机 | ⚠ 实测**不落库**：保存后回读为空（建单、改单两条路径都验证过） |
| `fax` | `txtCZ` | 传真 | ⚠ 实测**不落库**，同上 |
| `discount` | `txtZKL` | 折扣率 | 沿用客户档案的写法，实测形如 `10` |
| `remark` | `txtRemark` | 表头备注 | 与明细备注 `fields.remark`（`txtRemark`，明细子页同名控件）不是同一个 |
| `product_memo` | `txtCPMX` | 表头「产品描述」 | 与明细产品描述 `fields.product_desc`（`txtCPMS`）是两个字段 |
| `confirm` | —（不提交） | 落库开关 | `false`（默认）只校验并返回 `needs_confirmation`；`true` 才真正保存。不会写进 ERP |

> **客户单号不在表头**。`Add.aspx` 实测 103 个控件里没有客户单号输入框（2026-11-12 核对），
> 它是**明细级**字段 `txtKHCPMC`，见 3.3 的 `customer_order_no`。
>
> 表头页目前只对外开这 12 个参数；其余控件（客户名称/编码、制单人、收货人、审核人、
> 各类 `chk*` 开关）由客户档案或 ERP 内部维护，**不通过 MCP 写入**。
> 用 `erp_quote_new_form` 可以看到当前账号页面上真实存在哪些表头控件。

> 客户检索是**本地过滤**（服务端 `txtKey` 不可用，单页最多 500 行）：
> `erp_quote_customer_search` 的结果可以直接拿来确认 `customer` 写什么。

### 3.2 `products[]` 每个元素（4 个键）

| MCP 字段名 | ERP 字段名 | 中文含义 | 备注 |
| --- | --- | --- | --- |
| `name` | 检索用；落库回填 `txtCPID`/`txtCPBH`/`txtCPMC`/`txtGG`/`txtDWMC`/`txtLBID`/`txtLBBM` 等 | 产品名称 | 在【产品档案】里模糊检索，必须**唯一匹配**（精确同名优先）；档案会顺带回填约 35 个控件 |
| `spec` | —（不直接落库） | 规格 | 只做非空校验（形如 `210*148`）。**实际规格取档案的 `txtGG`**；要改传 `length_mm`/`width_mm`/`height_mm` |
| `qty` | `txtCPSL` + `txtCPSL1` | 产品数量（第 1 档） | 必须是正数；同时写隐藏汇总值与展示框。显式给了 `qty_1` 时以 `qty_1` 为准 |
| `fields` | —（容器） | 明细字段集合 | 见 3.3。也可以把这 51 个键**直接平铺**在 `products[0]` 上，完全等价 |

### 3.3 `fields` 里的 51 个明细字段

> 只有 **DOM 首屏可见且可写**的 51 个在这里。折叠面板里的 142 个隐藏字段、10 个
> ASP.NET 状态域一律拒绝（`allow_readonly` 也绕不过）。
> 下拉字段一律传**显示文字**，落库时按真实页面换成提交值；checkbox 传 `true` / `false`。

| MCP 字段名 | ERP 字段名 | 中文含义 | 备注 |
| --- | --- | --- | --- |
| `product_name` | `txtCPMC` | 产品名称 | 档案已回填，要覆盖才传 |
| `unit` | `txtDWMC` | 单位 | 下拉（`个`/`本`/`套`/`箱`…）；默认取档案 |
| `customer_order_no` | `txtKHCPMC` | 客户单号 | 明细级字段（表头没有）；列表「客户单号」列 |
| `product_name_en` | `txtECPMC` | 产品英文名 | 自由文本 |
| `product_group` | `txtCPDL` | 产品大类 | 下拉；实测当前只有 `书版` |
| `length_mm` | `txtCD` | 长（MM） | 数字；会覆盖档案尺寸 |
| `width_mm` | `txtKD` | 宽（MM） | 数字 |
| `height_mm` | `txtGD` | 高（MM） | 数字 |
| `spine_direction` | `Drop_SJFX` | 书脊方向 | 下拉：短书脊 / 长书脊 / 四边裁 |
| `sort_no` | `txtPXH` | 排序号 | 明细行排序，整数 |
| `binding` | `txtZDFS` | 装订方式 | ✅ **必填**；下拉；必填链第 2 环（产品类别之后） |
| `packing` | `txtBZFS` | 包装方式 | ✅ **必填**；下拉：纸箱包装 / 牛皮纸包装 / 胶袋包装 / 卡板摆放 |
| `pallet` | `txtTBLX` | 台板类型 | ✅ **必填**；下拉，选项是长文案（`1.2m*0.8m(欧式)实木消毒卡板`…） |
| `delivery` | `txtSHFS` | 送货方式 | ✅ **必填**；下拉：FOB(NGV) / FOB 盐田 / 送香港公司 / DPU … |
| `transport_type` | `txtYSLX` | 运输类型 | 下拉：FOB盐田(普通) / FOBHK / FOB(拼柜) / DPU / CIF … |
| `delivery_area` | `txtSHDQ` | 送货地区 | ⚠ 级联下拉（由送货方式 JS 填充），服务端渲染为空，**纯 HTTP 写不进** |
| `carton_type` | `txtZXLB` | 纸箱类别 | 下拉：普通纸箱 / 半翼箱纸箱 / 特殊纸箱 / 天地盖纸箱 |
| `board_type` | `txtZBLX` | 纸板类型 | 下拉：A=B双坑纸板 / A3B单坑纸板 / A=A双坑纸板 … |
| `packing_orientation` | `txtZXFS` | 装箱方式 | 下拉：平装 / 竖装 |
| `bottom_card` | `txtDKMC` | 底卡名称 | 下拉：B3B坑纸板 / A=B普通芯 / 无需 / K=K加强芯 … |
| `no_pallet` | `chkBSYTB` | 不使用台板 | checkbox；传 `false` 等于不勾选（不提交） |
| `no_container` | `chkBSYJZX` | 不使用集装箱 | checkbox |
| `delivery_date` | `txtJHRQ` | 交货日期 | ✅ **必填**；`2026-12-20`，ERP 也接受 `2026/12/20` |
| `expected_date` | `txtWCRQ` | 预计下单日期 | ✅ **必填**；同上 |
| `has_schedule` | `chkZDBJF` | 有排交期 | checkbox |
| `attribute` | `txtCPSX` | 产品属性 | ✅ **必填**；下拉：三岁以上 / 三岁以下 / 其他 / 3岁以下（要寄第三方测试）… |
| `product_type` | `txtCPLX` | 产品类型 | ✅ **必填**；下拉：新产品 / 返单产品 / 半返单旧产品 / 半返单新产品 / 特殊新产品 |
| `fsc_category` | `txtFSCMC` | FSC类别 | 下拉：FSC Mix / FSC 100% / FSC Recycled … |
| `has_parts` | `txtSFYPJ` | 是否有配件 | 下拉：是 / 否；选「是」时 `parts_desc` 必填 |
| `no_quote` | `chkXBJF` | 不需报价 | checkbox |
| `product_desc` | `txtCPMS` | 产品描述（明细） | ✅ **必填**；textarea；必填链最后一环 |
| `extra_desc` | `txtBGMS` | 补充描述 | textarea |
| `remark` | `txtRemark` | 明细备注 | textarea；列表「备注」列显示的就是它 |
| `parts_desc` | `txtRemark1` | 配件描述 | ✅ **必填**；textarea；`has_parts=否` 的记录该框是只读的 |
| `modify_log` | `txtRemark3` | 修改记录 | ✅ **必填**；textarea |
| `inspection` | `txtRemark4` | 检测项目 | ✅ **必填**；textarea |
| `old_hk_order_no` | `txtOLD_KHDDH` | 返单香港单号 | 填了相当于标记「返单」，ERP 会带入原单快照，**非返单场景别传** |
| `target_price_currency` | `txtBZ` | 客户目标价币种 | 下拉：人民币 / 港币 / 美元 / 欧元 / 日元 / 澳元 / 英镑 |
| `target_price` | `txtKHMBDJ` | 客户目标价 | 数字（单价） |
| `cases` | `txtTZS` | 款数 | ✅ **必填**；与 `qty_n`（数量档位）**无关** |
| `languages` | `txtYYS` | 语言数 | ✅ **必填**；同上 |
| `qty_1` | `txtCPSL1` | 第 1 档报价数量（总量） | = 顶层 `qty`；列表「数量描述」「最小值」 |
| `qty_2` | `txtCPSL2` | 第 2 档报价数量 | 列表「中间值」 |
| `qty_3` | `txtCPSL3` | 第 3 档报价数量 | 中间档，列表不单独显示 |
| `qty_4` | `txtCPSL4` | 第 4 档报价数量 | 列表「最大值」取最大的一档 |
| `qty_5` | `txtCPSL5` | 第 5 档报价数量 | 同上；填 0 或不传等于不报价该档 |
| `qty_6` | `txtCPSL6` | 第 6 档报价数量 | 同上 |
| `qty_7` | `txtCPSL7` | 第 7 档报价数量 | 同上 |
| `qty_8` | `txtCPSL8` | 第 8 档报价数量 | 同上 |
| `qty_9` | `txtCPSL9` | 第 9 档报价数量 | 同上 |
| `qty_10` | `txtCPSL10` | 第 10 档报价数量 | 同上 |

> `qty_n` 与「款数」的关系、以及列表页三列怎么取，见第 4 节「数量档位 vs 款数」。

### 3.4 必填字段清单

分两层，缺任何一层都建不出单：

**① 协议层必填**（`prepare()` 直接拒绝，`status=invalid`）

| MCP 字段名 | 说明 |
| --- | --- |
| `customer` | 必须唯一命中客户档案 |
| `sales` | 客户档案里有默认业务员时可以省略 |
| `products[0].name` | 必须在产品档案里唯一匹配 |
| `products[0].spec` | 只校验非空（实际规格取档案） |
| `products[0].qty` | 必须 > 0 |

**② ERP 服务端必填（14 个）**——顺序就是服务端报错的顺序，缺哪个就提示「【XX】不能为空！」：

| # | MCP 字段名 | ERP 字段名 | 中文含义 |
| --- | --- | --- | --- |
| 1 | `binding` | `txtZDFS` | 装订方式 |
| 2 | `cases` | `txtTZS` | 款数 |
| 3 | `languages` | `txtYYS` | 语言数 |
| 4 | `packing` | `txtBZFS` | 包装方式 |
| 5 | `pallet` | `txtTBLX` | 台板类型 |
| 6 | `delivery` | `txtSHFS` | 送货方式 |
| 7 | `delivery_date` | `txtJHRQ` | 交货日期 |
| 8 | `parts_desc` | `txtRemark1` | 配件描述 |
| 9 | `attribute` | `txtCPSX` | 产品属性 |
| 10 | `product_type` | `txtCPLX` | 产品类型 |
| 11 | `expected_date` | `txtWCRQ` | 预计下单日期 |
| 12 | `product_desc` | `txtCPMS` | 产品描述（明细） |
| 13 | `modify_log` | `txtRemark3` | 修改记录 |
| 14 | `inspection` | `txtRemark4` | 检测项目 |

链上还有两个**不用你填**的环节：

| 环节 | 由谁满足 |
| --- | --- |
| 产品类别 `txtCPLB`（链上第 1 环） | 按产品档案自动补进 `txtCPLB_Input`/`txtLBID`/`txtLBBM` |
| 产品数量 `txtCPSL`（链上第 5 环） | `qty` 自动同时写 `txtCPSL` + `txtCPSL1` |

> ⚠ **缺了②里的字段，`prepare` 现在会给 warning**（不会硬拦，因为个别产品可能放宽）。
> 必须当真：ERP 是「先建主表、再存明细」，明细被拒时**主表已经存在**，会留下一条空单。
> 宁可先补字段，也别指望 `status=partial` 之后自动回滚。
## 4. `products[]` 明细


### 业务必填

| 键 | 校验 | 是否落库 |
| --- | --- | --- |
| `name` | 先在【产品档案】里模糊检索，必须**唯一匹配**（精确同名优先）；否则报「匹配到 N 条」 | ✅ 以产品档案为准 |
| `spec` | 只做非空校验（形如 `210*148`） | ❌ 不生效，规格取自产品档案；要覆盖请传首屏可见的 `length_mm`/`width_mm`/`height_mm`（`txtCD/txtKD/txtGD`），或改档案 |
| `qty` | 必须是正数 | ✅ 同时写隐藏字段 `txtCPSL` 与展示框 `txtCPSL1`（显式给 `qty_1` 时不会被覆盖） |

### 数量档位 vs 款数（很容易搞混）

`qty_1` … `qty_10`（`txtCPSL1..10`）**不是「每款数量」，是「报价数量档位」**。
用真实历史单 `BJXQ261110003` 校对的结果：

| 字段 | 该单的值 | 含义 |
| --- | --- | --- |
| `txtTZS` 款数 | 4 | 款数（另有 `txtYYS` 语言数） |
| `txtCPSL` | 12000 | = 第1档总量；列表页「数量描述」用它 |
| `txtCPSL1/2/3/4` | 12000 / 20000 / 30000 / 40000 | 第 1~4 档报价**总量** |
| `txtCPMKSL1/2/3` | 3000 / 5000 / 7500 | 对应档的**每款数量** = 总量 ÷ 款数（ERP 自动算） |
| 列表页列 | 最小值 12000 / 中间值 20000 / 最大值 40000 | 取自**首档 / 第二档 / 末档** |

所以：

* 想报「3000 / 5000 / 7000 三个量级」→ 填 `qty_1`、`qty_2`、`qty_3`，**款数保持 1**；
* 想报「4 款，每款 3000」→ 款数 = 4，`qty`/`qty_1` = 12000，每款数量由 ERP 自己算；
* `qty` 必须等于 `qty_1`（都以第1档为准），否则「数量描述」与档位对不上。

> 实测（`BJXQ261112006`）：`qty=3000` + `qty_1..4 = 3000/5000/6000/7000` + 款数 1
> → ERP 存下 4 个档位，列表显示 最小值 3000 / 中间值 5000 / 最大值 7000，数量描述 3000。
> ERP **不会**帮你重算或校正，发什么就存什么。

### 其余明细字段：**只有首屏可见的 51 个允许上传**

每一个字段的「MCP 字段名 / ERP 控件名 / 中文含义」见 **3.3 节的 51 行表格**。
子页 `Add_CPZL.aspx` 的 DOM 里一共有 **205** 个控件：

| 分类 | 数量 | 能否写入 |
| --- | --- | --- |
| 首屏可见 | **53** | — |
| ├ **可上传（可见 且 可写）** | **51** | ✅ |
| └ 可见但只读（香港单号 `txtKHDDH`、客户地区 `txtKHDQ`） | 2 | ❌ |
| 折叠面板（`table#TableVisible` 等 `display:none`） | **142** | ❌ **硬拒绝**，`allow_readonly` 也绕不过 |
| ASP.NET 状态域 | 10 | ❌ |

隐藏字段（计价财务、条形码号码、印刷工艺、返单快照、数量明细 GUID）**不接受写入**：
它们不是「可选高级项」，而是别的分区的数据。想改请到 ERP 界面里改。

> 产品档案回填（编码/单位/类别/尺寸等约 35 个字段）是「在界面上选产品」的等价行为，
> 不属于用户输入；用户输入只限上面这 51 个字段。其中 `txtCPBH`（产品编号）、
> `txtGG`（规格存档值）等虽然会被档案回填，但**你也传不进去**（不在首屏）。

### 上传 JSON 结构

`erp_quote_product_form` 返回的 `upload` 就是可直接拿去做表单/校验的 JSON Schema：

```jsonc
{
  "tool": "erp_quote_create",
  "argument": "products[0]",
  "json_schema": {
    "type": "object",
    "required": ["name", "spec", "qty"],
    "additionalProperties": false,
    "properties": {
      "name": { "type": "string", "title": "产品名称" },
      "spec": { "type": "string", "title": "规格", "examples": ["210*148"] },
      "qty":  { "type": "number", "title": "产品数量", "exclusiveMinimum": 0 },
      "fields": {
        "type": "object",
        "additionalProperties": false,
        "properties": {
          "binding": { "type": "string", "enum": ["胶装/PAD书", "精装书", "骑马钉", "…"] },
          "packing": { "type": "string", "enum": ["纸箱包装", "牛皮纸包装", "胶袋包装", "卡板摆放"] },
          "no_pallet": { "type": "boolean" },
          "cases": { "type": "number" },
          "delivery_date": { "type": "string", "examples": ["2026-11-20"] }
          // …共 51 个，每个都带 x-control 指向真实控件名
        }
      }
    }
  },
  "example": {
    "name": "示例产品名称", "spec": "210*148", "qty": 12000,
    "fields": { "binding": "胶装/PAD书", "packing": "纸箱包装", "unit": "本",
                "no_pallet": true, "cases": 1, "languages": 1 }
  },
  "excluded": { "hidden_count": 142, "hidden": ["txtHL", "txtTMLX", "…"],
                "readonly": ["txtKHDDH", "txtKHDQ"] }
}
```

### 三种写法，可混用

```jsonc
{
  "products": [{
    "name": "...", "spec": "130*130", "qty": 100,

    // ① 别名（老的 14 个名字继续有效）
    "packing": "纸箱包装",

    // ② 页面控件名（与别名等价）
    "txtCPSX": "三岁以上",

    // ③ 显式分组（推荐给「按页面生成」的数据）
    "fields": {
      "carton_type": "普通纸箱",
      "chkBSYTB": true,
      "parts_desc": "无"
    }
  }]
}
```

* 下拉传**显示文字**（`纸箱包装`），落库时按真实页面换成提交值；
* checkbox 传 `true` / `"on"` 为勾选，`false` / `""` 不提交；
* 隐藏字段（如 `exchange_rate`、`payment_term`、`txtTMLX`）传了**直接报错**；
* 首屏可见但只读的字段（`hk_order_no`、`customer_region`）要传需加 `allow_readonly=true`；
* 键名拼错会**直接报错并给出相近字段**，不再静默丢弃。

完整清单用 `erp_quote_product_form` 查：

```jsonc
{ "control": "txtBZFS", "alias": "packing", "label": "包装方式",
  "group": "装订包装", "kind": "select",
  "visible": true, "writable": true, "uploadable": true,
  "known_options": ["纸箱包装", "…"],
  "page": { "present": true, "value": "-0-", "css_hidden": false },
  "options": [ { "text": "纸箱包装", "value": "纸箱包装" } ] }
```

不传 `record_id` 时返回静态注册表（`fields.py`）；传了会打开真实页面，
把**当前值 / 真实下拉选项 / 真实只读状态**一起带回来。

### 仍然由产品档案决定的部分

编码、单位、类别、尺寸等默认从产品档案反查回填（先回填、再被你的入参覆盖）。
唯一例外是 **产品类别 `txtCPLB`**：ComponentArt 树的值只存在前端 JS 里，
服务端渲染只有占位符，ERP 侧存不住（`txtCPLB_Input` 也是只读）。

## 5. 返回结构与 `status`

| `status` | 含义 |
| --- | --- |
| `invalid` | `prepare` 校验没过，看 `errors[]` |
| `unsupported` | `len(products) != 1`，需分次调用 |
| `needs_confirmation` | 校验通过但 `confirm=false`，返回值即预览（`header` / `products` / `errors` / `warnings`） |
| `rejected` | ERP 拒收，`stage=create_header`，`message` 是页面原始提示（客户/业务员无效等） |
| `created` | 主表 + 明细 + 数量回读**全部一致**（`ok=true`） |
| `partial` | 主表建了、明细没落成功；看 `detail_error` |
| `failed` | 明细行都没建出来 |

成功时还会返回 `record_id`、`bdbh`（表单编号）、`url`、`steps[]`（`create_header` /
`save_product` / `verify` 三步各自的 `ok`）。

## 6. 推荐调用顺序

```
erp_quote_product_form              看明细子页能填什么（控件名/别名/下拉值）
erp_quote_customer_search           确认客户唯一命中 + 默认业务员
  → erp_quote_options(field="sales")   确认业务员在下拉里
  → erp_quote_prepare                  预览（不落库）
  → erp_quote_create(confirm=true)     落库，回读 CPMC + txtCPSL
  → erp_quote_details                  拿 cpid
  → erp_quote_update / erp_quote_update_product
  → erp_quote_delete(confirm=true)
```

## 7. 已知限制与坑

- **一次只支持 1 条明细**，多条请分次调用。
- **只有首屏可见的 51 个明细字段能写入**；折叠面板里的 142 个隐藏字段一律拒绝，
  产品类别（树形下拉）与多语言数量明细也不落库（只写汇总值 `txtCPSL`，由 `qty` 间接写）。
- **表头页只开 12 个参数**（3.1 节）；想加更多表头字段需要先扩 `prepare()` 的
  `CUSTOMER_TO_HEADER` / 入口参数，并同样遵守「可见才能写」。
- **手机 `txtSJ` / 传真 `txtCZ` 提交后不落库**：建单、改单两条路径都实测过，
  保存后回读恒为空（估计由【客户联系人】派生）。`prepare` 会对此给出 warning。
- **送货地区 `txtSHDQ` 提交后不落库**：服务端渲染时该下拉是空的（靠【送货方式】级联 JS 填充），
  纯 HTTP 写不进，回读为空。
- **产品类别 `txtCPLB` 能过校验但存不住**：建单时按产品档案把值补进 `txtCPLB_Input` 才过得去
  「请选择【产品类别】的数据！」，但保存后列表页「产品类别」列仍为空。
- `spec` 必填但**不生效**，实际规格来自产品档案——想改规格要改档案或用
  `erp_quote_update_product`；也可以传 `length_mm`/`width_mm`/`height_mm`。
- `qty` 必须**两个字段一起发**（`txtCPSL1` + `txtCPSL`），只发展示框会一直被挡在
  「【产品数量】不能为空！」。这件事 `create()` 内部已经处理，调用方不用管。
- **不能用返回页里的 alert 文案判断成败**：ERP 保存成功后仍可能残留一条提示，
  所以必须以**写后回读**为准（这正是 `partial` / `failed` 存在的原因）。
- 写完后**不要再拿 `btn_SonNew` 之前的旧快照重发 `btnSave`**——那会把刚写入的明细整行覆盖掉。

## 8. 变更记录

| 日期 | 变更 |
| --- | --- |
| 2026-11-12 | **补齐必填说明**：新增 3.4「必填字段清单」（协议层 5 项 + ERP 服务端 14 项），3.3 表格里的必填行加 ✅ 标记；`prepare()` 缺必填字段时给 warning（避免留下空单） |
| 2026-11-12 | **第 3 节改成「完整 JSON 逐字段说明」**：外层 12 个参数 + `products[]` 4 个键 + `fields` 51 个字段，统一用「MCP 字段名 / ERP 字段名 / 中文含义 / 备注」四列表格 |
| 2026-11-12 | **搞清数量语义**：`txtCPSL1..10` = 报价数量**档位**（列表的 最小值/中间值/最大值），`txtCPMKSL1..10` = 每款数量；已修正字段标签与文案 |
| 2026-11-12 | **51 字段端到端实测**：样例 `samples/quote-create-51-fields.json` 建单成功（`status=created`，主表+明细+数量回读一致）；发现手机/传真、送货地区不落库；修复建单时产品类别未回填的回归 |
| 2026-11-12 | 明细字段从 14 个扩到「首屏可见 51 个」；新增 `erp_quote_product_form`（含 `upload.json_schema`）；隐藏字段硬拒绝；补 `merchandiser` / `price_system`；修正 `remark`/`description` 被静默丢弃 |
| 2026-11-12 | **移除表头参数 `customer_order_no`**：表头页没有该控件，客户单号改走明细 `products[].customer_order_no`（`txtKHCPMC`） |
| 2026-11-11 | 新增写入链（主表 + 1 条明细 + 产品数量），开放 `erp_quote_create` |

---

字段级踩坑记录另见
[quote_create/README.md](../src/erp_tools/tools/quote_create/README.md)；
工具总览见 [mcp.md](mcp.md)。
