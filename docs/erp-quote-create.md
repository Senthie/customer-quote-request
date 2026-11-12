# `erp_quote_create` 入参协议

> 本文由 2026-11-12 09:19 的会话回答整理，并逐字段对照
> [`src/erp_tools/tools/quote_create/service.py`](../src/erp_tools/tools/quote_create/service.py)
> 校对（`prepare()` / `create()` / `SUB_FIELD_MAP` / `PRODUCT_ARG_FIELDS`）。

**会写入 ERP。** 新建一条「客户报价需求」= 主表 + 1 条产品明细（含产品数量）。
`confirm=false`（默认）只校验并返回 `needs_confirmation`，不落库。

| 项目 | 值 |
| --- | --- |
| 工具名 | `erp_quote_create` |
| 写库 | ✅（必须先过 `erp_quote_prepare` 同一套校验） |
| 一次明细数 | **仅 1 条**，多条返回 `status=unsupported` |
| 必填 | `customer`、`sales`（可由客户档案补）、`products[0].name/spec/qty` |
| 鉴权 | `Authorization: Bearer <api-key>` |

---

## 1. 参数形态

就是 MCP 工具调用的 `arguments` 对象，等价于
`QuoteCreateService.create(**kwargs)` 的关键字参数：**全部扁平、全部可选**，
只有 `customer` / `sales` / `products` 三项在业务上必需，其余留空即沿用客户档案默认值。

## 2. 完整 JSON 示例

```json
{
  "customer": "HAS10",
  "sales": "陈纯",
  "products": [
    {
      "name": "512961503",
      "spec": "75*28*298",
      "qty": 3000,
      "binding": "胶装/PAD书",
      "packing": "纸箱包装",
      "pallet": "07",
      "delivery": "FOB(NGV)",
      "delivery_date": "2026/11/20",
      "expected_date": "2026/11/20",
      "parts_desc": "无",
      "modify_log": "无",
      "inspection": "无",
      "product_desc": "MCP数量验证",
      "attribute": "三岁以上",
      "product_type": "新产品",
      "cases": 1,
      "languages": 1
    }
  ],
  "contact": "张三",
  "phone": "028-1234",
  "mobile": "13800000000",
  "fax": "028-5678",
  "discount": "0.85",
  "remark": "备注",
  "product_memo": "产品描述（表头）",
  "confirm": true
}
```

## 3. 表头字段

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `customer` | string | 客户名称 / 编码 / 简称的**片段**，在客户资料里模糊检索，**必须唯一命中**；0 条或多条都直接报错并列出候选 |
| `sales` | string | 业务员**显示名**，必须存在于页面下拉里；留空则取客户档案的默认业务员（档案也没有就报错） |
| `contact` / `phone` / `mobile` / `fax` | string | 联系人 / 电话 / 手机 / 传真，覆盖客户档案里的默认值 |
| `discount` | string | 折扣率 |
| `remark` | string | 表头备注 |
| `product_memo` | string | 表头「产品描述」 |
| `confirm` | bool = `false` | `false` 只校验并返回 `status=needs_confirmation`，**不落库** |

> 客户检索是**本地过滤**（服务端 `txtKey` 不可用，单页最多 500 行）：
> `erp_quote_customer_search` 的结果可以直接拿来确认 `customer` 写什么。

## 4. `products[]` 明细

### 业务必填

| 键 | 校验 | 是否落库 |
| --- | --- | --- |
| `name` | 先在【产品档案】里模糊检索，必须**唯一匹配**（精确同名优先）；否则报「匹配到 N 条」 | ✅ 以产品档案为准 |
| `spec` | 只做非空校验（形如 `210*148`） | ❌ **不写进 ERP**，规格取自产品档案 |
| `qty` | 必须是正数 | ✅ 同时写隐藏字段 `txtCPSL` 与展示框 `txtCPSL1` |

### 可选的 14 个明细字段 → 页面控件

| 入参 | 控件 | 含义 |
| --- | --- | --- |
| `binding` | `txtZDFS` | 装订方式 |
| `packing` | `txtBZFS` | 包装方式 |
| `pallet` | `txtTBLX` | 台板类型 |
| `delivery` | `txtSHFS` | 送货方式 |
| `delivery_date` | `txtJHRQ` | 交货日期 |
| `expected_date` | `txtWCRQ` | 预计下单日期 |
| `parts_desc` | `txtRemark1` | 配件描述 |
| `modify_log` | `txtRemark3` | 修改记录 |
| `inspection` | `txtRemark4` | 检测项目 |
| `product_desc` | `txtCPMS` | 产品描述 |
| `attribute` | `txtCPSX` | 产品属性 |
| `product_type` | `txtCPLX` | 产品类型 |
| `cases` | `txtTZS` | 款数 |
| `languages` | `txtYYS` | 语言数 |

其他键（`description`、`remark`、单位、类别、`product_id`…）**会被忽略**：
单位 `txtDWMC`、类别 `txtCPLB`、编码、尺寸等都从产品档案反查回填。

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
erp_quote_customer_search    确认客户唯一命中 + 默认业务员
  → erp_quote_options(field="sales")   确认业务员在下拉里
  → erp_quote_prepare                  预览（不落库）
  → erp_quote_create(confirm=true)     落库，回读 CPMC + txtCPSL
  → erp_quote_details                  拿 cpid
  → erp_quote_update / erp_quote_update_product
  → erp_quote_delete(confirm=true)
```

## 7. 已知限制与坑

- **一次只支持 1 条明细**，多条请分次调用。
- **产品类别（树形下拉）与多语言数量明细不落库**；只写汇总值 `txtCPSL`。
- `spec` 必填但**不生效**，实际规格来自产品档案——想改规格要改档案或用
  `erp_quote_update_product`。
- `qty` 必须**两个字段一起发**（`txtCPSL1` + `txtCPSL`），只发展示框会一直被挡在
  「【产品数量】不能为空！」。
- **不能用返回页里的 alert 文案判断成败**：ERP 保存成功后仍可能残留一条提示，
  所以必须以**写后回读**为准（这正是 `partial` / `failed` 存在的原因）。
- 写完后**不要再拿 `btn_SonNew` 之前的旧快照重发 `btnSave`**——那会把刚写入的明细整行覆盖掉。

---

字段级踩坑记录另见
[quote_create/README.md](../src/erp_tools/tools/quote_create/README.md)；
工具总览见 [mcp.md](mcp.md)。
