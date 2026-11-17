# 产物推 ERP：字段口径与档案前置条件

> 2026-11-12 定稿（森西拍板）。代码：`scripts/erp_mcp_export.py`（导出）+ `scripts/erp_push.py`（发送）；
> 协议：`docs/erp-quote-create.md`；回归：`tests/test_erp_push.py` / `tests/test_pipeline.py`。

## 0. 导出文件 = MCP 入参（一份文件、一套字段名）

`main.py` 步骤 3b 写的 `<stem>.erp-import.json` **就是** `erp_quote_create` 的 arguments：

```json
[
  {
    "customer": "NGV10",
    "remark": "LG26090401 / Wimmelbücher (Winter)",
    "products": [
      { "name": "冬季找找图画书", "spec": "240*330*0", "qty": 410000,
        "fields": { "binding": "快线BB书", "cases": 10, "has_parts": "否", "...": "..." } }
    ]
  }
]
```

* 一个元素 = 一次 `erp_quote_create`（ERP 一次只收 1 条明细 → N 条产品 N 个元素）。
* `fields` 里的键全是 MCP 字段名（`binding`/`packing`/`qty_1`…），
  与 `erp_quote_product_form` 的 `upload.json_schema` 一一对应；
  **不再出现** `ZDFS`/`BZFS` 这类 ERP 控件名，也不再需要第二份映射表。
* 里面没有任何内部字段（`ID`/`version`/`question_answers`/`CPMC_ZH`），可以直接拿去调工具。
* 文件名沿用 `.erp-import.json` 是**故意的**：仓库 20+ 处靠这个后缀识别侧档
  （查看器 / `cpms_todo` / `migrate_erp_to_array` / `backfill_question_answers`），
  换名字会静默把副本当成主产物。

发送：

```bash
# 已烘好客户代号与数量 → 什么参数都不用给
uv run ./scripts/erp_push.py <task>/ERP_xxx.erp-import.json          # 干跑
uv run ./scripts/erp_push.py <task>/ERP_xxx.erp-import.json --confirm
# 需要临时改数量/客户时，仍然可以覆盖（--qty 是**单款本数**）
uv run ./scripts/erp_push.py ... --qty 41000 --customer NGV10 --date 2027-03-15
```

## 1. 业务兜底值（客户没给时自动补）

| ERP 字段 | 兜底值 | 触发条件 | 出处 |
| --- | --- | --- | --- |
| `binding`（装订方式） | `胶装/PAD书`（ERP 第一个合法值） | 判断不出装订方式（客户没提 / 提了但档案无映射） | 森西 2026-11-17（见 §4.0；2026-11-12 的「BB 类别→快线BB书」与 11-16 的「缺省快线BB书」均已作废） |
| `attribute`（产品属性） | `三岁以下` | 客户未指定 | 森西 2026-11-12 |
| `product_type`（产品类型） | `新产品` | 客户未指定（新询价） | 森西 2026-11-12 |
| `parts_desc`（配件描述） | `无` | 无配件 | 森西 2026-11-12 |
| `modify_log`（修改记录） | `无` | 无修改 | 森西 2026-11-12 |
| `delivery_date` / `expected_date` | `--date` 传入值 | 两处都空时 | 森西 2026-11-12「按最短的日期写」 |

**只补空值**，已有的不覆盖。每补一个都写进推送报告的 `defaulted[]`
（`{record, erp_field, value, reason}`），业务一眼看得出哪些不是客户给的。

⚠ **装订方式不再按 BB 类别推导**（2026-11-17 起）：`BINDING_BY_CATEGORY` 已停用 ——
历史单里 `普通BB书` 既有 `快线BB书` 也有 `慢线BB书`（`BJXQ261107001` 慢线 /
`BJXQ261105008` 快线），按类别猜会猜错一半。现在统一走 §4.0 的口径：
判断不出 → ERP 第一个合法值占位 + `Q_ZDFS` 确认项 + CPMS 标明。

⚠ **配件描述口径**：历史单据这一列写的是 `否` / `自厂购`，不是 `无`。
按 2026-11-12 指示暂用 `无`；若要跟历史统一，改 `FIELD_DEFAULTS` 一行即可。

## 2. 还硬拦的必填（缺了不推）

`JHRQ` `WCRQ`（可用 `--date` 兜）、`CPMS`、`BZFS`、`TBLX`、`SHFS`、`Remark4`。

原因：ERP 是「先建主表、再存明细」，明细被拒不回滚 → 留下一条**空单**。

## 3. 产品档案：这道坎在哪

**结论：`prepare`（confirm=false）完全不查产品档案，只有 `create`（confirm=true）才查。**

实测（2026-11-12）：
- `erp_quote_prepare(name="冬季找找图画书")` → `ok=true`，`errors=[]`
- `erp_quote_prepare(name="这个产品档案里绝对没有XYZ123")` → **同样 `ok=true`**

所以干跑全绿 ≠ 落地成功。**2026-11-12 实测已发生一次**：

```
status=archive_missing
error: Error executing tool erp_quote_create: 产品档案里找不到 '冬季找找图画书'
```

后果：ERP 列表里多出 `BJXQ261112006`（客户 NGV10、主表备注是本次的追溯串、
产品名称/报价状态/装订方式全空、明细 0 行）—— 一条**空单**，已用
`erp_quote_delete(confirm=true)` 清掉。

清空单的完整步骤（下次照做）：
1. `erp_quote_list(key="BJXQ2611", …)` 找「产品名称空 + 报价状态空」的新单
   ⚠ **别只按日期查**：删过单之后列表页会返回 `count=0 / total=1` 的空网格，
   必须带 `key`（服务端关键字）强制重新查一次
2. `erp_quote_detail(bdbh=...)` 拿 `record_id`
3. `erp_quote_details(record_id)` 确认 `count=0`（不是 0 就别删，先看为什么）
4. `erp_quote_delete(confirm=true)`，再用 `erp_quote_detail(bdbh=...)` 确认「查不到」

**档案里的「品名」长什么样**（`erp_quote_list` 真实数据，NGV 客户）：

| 表单编号 | 产品类别 | 产品名称 |
| --- | --- | --- |
| BJXQ261112002 | 无线胶装书 | `Kathrin/无线胶装书+海报/84497 Die besten Fußballtricks(FSC Mix)` |
| BJXQ261107001 | 普通BB书 | `Kathrin/普通磁胶BB书(12P内文)  reprint PO 15800(FSC Mix)` |
| BJXQ261105008 | 冲形状普通BB书 | `Miriam/冲形状普通BB书/Type C) Henkelpappen (FSC Mix)` |

命名规律：`{客户方负责人}/{产品类别}/{客户原品名}({FSC类别})`。
它**不是**我们产物的 `CPMC`（中文译名，「冬季找找图画书」这种）——
`CPMC` 是产品档案回填后的显示名，不是检索键。

**59 键里没有任何一个键是「档案键」**：`CPMC` 是译名、`KHDH` 是客户单号、
`CPLB` 是类别。要挂档案，只能外部给一个档案里已存在的 `产品名称` 或料号。

## 4. 数量：`qty` 是**单款本数**，落库要乘款数

业务口径（2026-11-12 定，2026-11-16 森西再确认并扩展）：
产物 `CPSL1..10` **存单款本数**，ERP 的 `txtCPSL1..10` 每档存**总量**
（`txtCPMKSL*` 每款数量由 ERP 自己算 = 总量 ÷ 款数）。

不变式（回归测试钉住，`tests/test_quantity_and_binding_20261116.py`）：

    CPSL1 × TZS == 客户总本数      产物侧
    qty    == CPSL1 × TZS          落库侧（exporter 负责乘）

| 传入 | TZS | 落到 ERP |
| --- | --- | --- |
| `--qty 41000` | 10 | `qty`=`qty_1`=`txtCPSL1`= **410000** |
| `CPSL1=3000, CPSL2=5000` | 4 | `qty_1`=12000、`qty_2`=20000 |

> ⚠ **2026-11-16 修正**：引擎此前在「客户只给总数」时把**总量**写进 `CPSL1`，
> 导出层再 × 款数 → 数量翻倍（真实邮件实测：`LG26090401` 410000 会被推成 4,100,000）。
> 现在只给总数时按款数**均分**（`130000 ÷ 8 → 16250`）；**除不尽就不均分**，
> 保留总数并写 `_qty_conflict` → 日志与「数量口径待确认」提醒，交人工。

推送报告里有 `quantity[]`：`{titles, per_title, total_landed}`，一眼核对倍数有没有错。
`qty` 永远等于第 1 档（总量），三档之间**不相加**。

`CPMS` 正文里写的合计数**不会**自动进 `CPSL1` —— 产物档位是 0 时必须显式 `--qty`。

### 4.0 装订方式判断不出：落 ERP 第一个合法值 + CPMS 标明（2026-11-17 森西）

> 森西原话：「有时候我们没办法判断装订方式，但是上传 ERP 可能需要，这个时候
> **默认选择第一个值**，然后在 **CPMS 中标明**，xxx 项无法判断值，需要业务自行判断」。
> **口径变更**：2026-11-16 的「没提到就默认快线BB书」**作废** —— 把一本 224pp
> 锁线胶装书标成「快线BB书」是明显错标（BB 书本身还分快线/中线/慢线/胶装）。

判断不出装订方式时（客户没提、或提了但档案没有对应映射）：

| 环节 | 行为 |
| --- | --- |
| 产物 `ZDFS` | 落 **ERP 下拉的第一个合法值**（`assets/erp_form_schema.yaml` → `ZDFS.options[0]` = `胶装/PAD书`）；类别/品名含「套装」→ 仍落 `套装` |
| 推送 `binding` | 同值（`erp_mcp_export.apply_defaults()` 兜底） |
| 内部确认清单 | 自动加一条 `Q_ZDFS`（open / 不卡交付）：「装订方式无法判断…已按 ERP 第一个合法值「胶装/PAD书」落库，**需业务自行判断**」 |
| `CPMS` 正文 | **必须**写明「装订方式无法判断值，需业务自行判断」。`cpms_lint` 新增 **CPMS081** 守着这条（建议写在备注段，别写进装订段 —— 装订段写 ZDFS 下拉值会踩 CPMS055） |

实现：`main.py::default_binding_for()`（取值）+ `binding_inferred_note()`（判定是否
「占位值」并生成确认项文案）+ `erp_mcp_export.apply_defaults()`（推送侧）。
「占位」的判据是**客户原文里没有任何 `binding.map` 关键词命中** —— 有证据（哪怕映射到
同一个值）就不算占位，不报 CPMS081。有个测试钉住「`DEFAULT_BINDING` 必须等于表单字典
的第一个枚举」，ERP 改了下拉顺序会当场红。

### 4.0.1 客户单号 `KHDH` 可用 RFQ 号（2026-11-16 森西）

终端客户常常还没给 PO，唯一稳定的客户侧编号是 **RFQ / 询价单号**
（`Request for Quotation #105819`、`RFQ 103859`、`Quotation request 42 PE`）。
这类编号现在算合法 `KHDH`，规则在 `customers/_defaults.yaml` 的
`email.khdh.subject_patterns / body_patterns`——**必须**「询价/报价请求」字样紧跟编号，
避免把年份、页码、脚注号当单号（裸号 `137424` 不取）。


### 4.1 `CPMS` → `product_desc` 要剥概要行（2026-11-16 实测）

| 环节 | 概要行 |
| --- | --- |
| 59 键产物 `CPMS` | **保留**（`cpms_lint` 的 CPMS010 要求它、业务要看） |
| 发给 ERP 的 `product_desc` | **剥掉**（`strip_cpms_header()` 自动处理） |
| ERP 里最终存的 `CPMS` | 只有 ERP 自己渲染的那一行 |

原因：ERP 需求录入页会按 `Drop_SJFX / CD / KD / GD / TZS` 自主生成概要行，我们再传一份
就变成两行。证据：实测 `BJXQ261113003` / `BJXQ261113004` 发出时 1 次、ERP 里 2 次；
语料 18395 份 ERP 快照中 18348 份（**99.74%**）只有 1 次。回归测试
`tests/test_cpms_header_dedup.py`。

## 5. 服务端 pydantic 入参（2026-11-12 拉 `tools/list` 核对）

`assets/erp_quote_create_schema.json` = 服务端 `erp_quote_create.inputSchema` 快照
（pydantic 自动生成，**唯一事实源**；协议改了就重抓，不要手抄第二份）。

**`products[]` 多了一个 `product_category`**（这是解档案问题的钥匙）：

| 键 | 类型 | 说明 |
| --- | --- | --- |
| `name` | string **必填** | 先按名称在产品档案模糊检索，唯一命中就用档案；搜不到 → 当新产品，**必须给 `product_category`** |
| `spec` | string **必填** | 只校验非空 |
| `qty` | number **必填** > 0 | 写 `txtCPSL` + `txtCPSL1` |
| `product_category` | string 可选 | `27.01` / `27.01;普通BB书` / `普通BB书` 都收；**给了就不要求档案存在** |
| `fields` | object | 51 个明细字段，键可用 MCP 名或页面控件名 |

明细 51 字段类型分布：31 个 string / 17 个 number / 4 个 boolean。
**number 不收 `"240"`、boolean 不收 `"否"`**（pydantic 直接 422，整单连主表都进不去）
→ `erp_mcp_export._coerce_fields()` 统一掰，掰不动的字段**丢掉并报错**，绝不送错类型。
离线自检用 `validate_call()`，`erp_push` 在发出去之前就跑一遍。

### 5.1 `qty_1` 绝不能显式传

`create()` 只在**没给** `qty_1` 时才补隐藏的 `txtCPSL`；显式给了就只写展示框 `txtCPSL1`，
ERP 报「请输入【产品数量】」。所以导出只传顶层 `qty`（第 1 档）与 `qty_2..10`。

### 5.2 free-text（新产品）分支：已通（2026-11-12 第二次实测）

```
status=created  header_saved=true  detail_saved=true  detail_error=""
verify: ok=true  row_created=true  qty_saved=true
bdbh=BJXQ261112008  record_id=89d48f0c-1fe3-4786-a768-a078f84e6f21
product: source=free-text  category=27.01;普通BB书  qty=410000  单位=本
```

**唯一硬约束：`qty_1` 绝不能显式传。** `create()` 只在「入参没给 qty_1」时才补隐藏的
汇总字段 `txtCPSL`；一旦显式传了 `qty_1`，它就只写展示框 → 汇总为空 → 报
「请输入【产品数量】」→ 明细行存不进去。第 2 档起（`qty_2..10`）可以传。

上午那次失败（6 个变体全挂）里「只给 `qty`」也炸了，说明当时服务端确实还没接好这条分支；
下午重试同一份 payload（仍然只给 `qty`、不传 `qty_1`）直接 `created`。→ 服务端已修，
我们这侧只要守住「不传 qty_1」。

> 注意：成功的那次 `save_product.message` **仍然**带着那句
> 「请输入【产品数量】…」的页面 alert，但行和数量都落了。**这句 alert 是噪音，
> 不能当失败依据**——只看 `status` + `verify`。

### 5.2.1 核验看列表页，别看 `erp_quote_detail` 的 null

新建成功后立刻 `erp_quote_detail` 会看到一大批 null：`CPSL1..10`、`DWMC`、`JHRQ`、
`WCRQ`、`CPSX`、`CPLX`、`FSCMC`、`ZXLB`、`ZBLX`、`ZXFS`、`DKMC`、`YSLX`、`SHDQ`、
`ECPMC`、`BZ` —— 这些控件在展示态是禁用/只读的，渲染不出值，不代表没落库。

**权威口径是 `erp_quote_list`**（`key=BJXQ...`）。同一个单在列表页里：

```
最小值/中间值/最大值 = 410000   数量描述 = 410000   单位 = 本
规格 = 240*330*0   款数 = 10   语言数 = 10   产品类别 = 普通BB书
产品名称 = 冬季找找图画书(FSC Mix)   ← ERP 自己按 FSC 追加后缀
预计下单/交货 = 2027-03-15   装订方式 = 快线BB书   包装方式 = 纸箱包装
是否有配件 = 否   配件描述 = 无   产品类型 = 新产品   修改记录 = 无
香港单号 = CU26111201（ERP 生成）   制单人 = 张爽
```

### 5.2.2 脚本直推 vs MCP 工具

`scripts/erp_push.py` 需要 `ERP_MCP_TOKEN`（或 `--token`），仓库和 profile 里目前**都没落盘**
→ 直接跑会 `HTTP 401 missing_bearer`。要拿到 token 得先 `POST /v1/auth/login`。
当下可用的推送通道是 MCP 工具 `erp_quote_create`（`confirm=false` 干跑 → `confirm=true` 落库），
参数结构与本目录下 `*.erp-import.json` 完全一致，**照着文件内容原样贴进去即可**。

### 5.3 成败只能看 `status`，不能看 `bdbh`

`failed` / `partial` 的响应里**也带 `bdbh`**（主表已建、明细没落）。之前 `_classify`
先判 `bdbh` → 把空单报成 `created`。现在 `status` 优先，并且在错误串里点明「主表已建：XXX，是空单」。
