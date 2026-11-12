# 产物推 ERP：字段口径与档案前置条件

> 2026-11-12 定稿（森西拍板）。代码：`scripts/erp_push.py`；协议：`docs/erp-quote-create.md`。
> 回归：`tests/test_erp_push.py`。

## 1. 业务兜底值（客户没给时自动补）

| ERP 字段 | 兜底值 | 触发条件 | 出处 |
| --- | --- | --- | --- |
| `binding`（装订方式） | `快线BB书` | 产品类别含 `BB`（如 `27.01;普通BB书`） | 森西 2026-11-12 |
| `attribute`（产品属性） | `三岁以下` | 客户未指定 | 森西 2026-11-12 |
| `product_type`（产品类型） | `新产品` | 客户未指定（新询价） | 森西 2026-11-12 |
| `parts_desc`（配件描述） | `无` | 无配件 | 森西 2026-11-12 |
| `modify_log`（修改记录） | `无` | 无修改 | 森西 2026-11-12 |
| `delivery_date` / `expected_date` | `--date` 传入值 | 两处都空时 | 森西 2026-11-12「按最短的日期写」 |

**只补空值**，已有的不覆盖。每补一个都写进推送报告的 `defaulted[]`
（`{record, erp_field, value, reason}`），业务一眼看得出哪些不是客户给的。

⚠ **装订方式按 BB 推导是默认值，不是铁律**：历史单里 `普通BB书` 既有 `快线BB书`
也有 `慢线BB书`（见 `BJXQ261107001` 慢线 / `BJXQ261105008` 快线）。
产品类别不是 BB 时**不推导**，照常拦住要人填。

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
1. `erp_quote_list(customer=…, from_date=今天, to_date=今天)` 找「产品名称空 + 报价状态空」的新单
2. `erp_quote_detail(bdbh=...)` 拿 `record_id`
3. `erp_quote_details(record_id)` 确认 `count=0`（不是 0 就别删，先看为什么）
4. `erp_quote_delete(confirm=true)`

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

业务口径（2026-11-12）：「数量」= **单款多少本**。而 ERP 的 `txtCPSL1..10` 每档存的是
**总量**（`txtCPMKSL*` 每款数量由 ERP 自己算 = 总量 ÷ 款数）。

所以 `--qty` / 产物 `CPSL1..10` 都按**单款**填，脚本落库时乘 `TZS`：

| 传入 | TZS | 落到 ERP |
| --- | --- | --- |
| `--qty 41000` | 10 | `qty`=`qty_1`=`txtCPSL1`= **410000** |
| `CPSL1=3000, CPSL2=5000` | 4 | `qty_1`=12000、`qty_2`=20000 |

推送报告里有 `quantity[]`：`{titles, per_title, total_landed}`，一眼核对倍数有没有错。
`qty` 永远等于第 1 档（总量），三档之间**不相加**。

`CPMS` 正文里写的合计数**不会**自动进 `CPSL1` —— 产物档位是 0 时必须显式 `--qty`。
