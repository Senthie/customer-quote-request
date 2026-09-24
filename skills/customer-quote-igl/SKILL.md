---
name: customer-quote-igl
description: IGL 客户报价需求提取（平铺 / 横向键值对报价表 → ERP 59 字段）。处理 IGL 的报价需求、附件解析、字段口径核对时加载；总纲见 skill customer-quote-request。
metadata:
  openclaw:
    emoji: "📋"
    requires:
      bins:
        - uv
        - python3
---

# IGL 报价提取（子技能）

> **总纲：** skill `customer-quote-request` —— 架构 / 抽取引擎 / 邮件入口 / 字段总览 /
> **`CPMS` 必须 agent 手写** / 校验方法 / 新增客户 SOP。本文件只讲 **IGL 这一家**。
> **唯一真源是档案 `customers/igl.yaml`** —— 改 IGL 的行为 = 改它，不改代码。
> 改完必须重跑：`uv run pytest`（含端到端基线 `tests/test_pipeline.py`）。

- 客户：IGL（香港书商；许可证 `LICENSE` 同仓库）
- 状态：`calibrated`（用真实报价表 `PUZZLE COLOUR DISNEY.xlsx` 校准）
- 别名表来源：`assets/customer_erp_mapper.xlsx`（25 行客户字段 ↔ ERP 字段对照）
- 端到端基线：`tests/test_pipeline.py`（逐字段期望值写死在那儿）
- 语料库：`customer-files/processed/*/PUZZLE COLOUR DISNEY.xlsx`（归档的真实样本，gitignored）

---

## 1. 附件长什么样

IGL 的报价表 = **平铺 + 横向键值对**混排的 XLSX。和 NGV 是完全两套东西
（业务 2026-09-24 强调：**每个客户的附件类型不一样、格式也不一样**）。

典型形态：**第一行就是横向键值对**，同一行右侧还可能续上第 3、4 组：

```
Imprint | Autumn | Complexity | Low | Requested By | (空格) | Christine
Group/Series Number | (空格) | 6366
Pub Month      → 下面一行是值
Size (tps) H x W x D mm | 198 | 129 | 6.85 …
Total Costs | 3k | 5k | 7.5k | 10k | 20k | 25k | 30k
```

抽取走 **`auto_pairs`（主策略）+ `column_pairs` + `shifted_pairs`**，
一条 `auto_pairs` 规则同时吃下：横向键值对（值隔着空格）、标签行/数据行（取下方）、
标签列/值列。实测抽出 **23 个规范键**。

---

## 2. 怎么认出这是 IGL

- **文件名关键词**：`puzzle`、`colour`
- **正文特征标签**（`min_signature_hits: 1`）：
  `Works References For all Titles`、`Safety Checks Req? (Y/N)`、`Extent (pp + cover)`

> ⚠️ 光靠文件名（`colour`）判客户**极度危险**：NGV 那份 `160x104 Colouring Cards` 就
> 因为这个被误判成 igl，整单字段全错（2026-09-24 修的真 bug，详见 NGV 子技能第 2 节）。
> 现在表格类文件一律把**正文**一起送进识别；得分 <30（只有文件名/主题这类弱证据）打 WARN。

---

## 3. 客户偏好表（固定值口径）

| 字段 | 值 | 依据 |
|------|----|------|
| `CPDL` 产品大类 | `书版` | ERP 枚举 `4=书版`，唯一合法值 |
| `YYS` 每款语言数 | `1` | 该客户报价单口径为单语言（**⚠️ 待业务确认**：NGV 已改「单语言留空」，igl 是否跟？） |
| `BZ` 币种 | `美元` | 历史单据存 label（不是 code `02`） |
| `PXH` 排序号 | `1` | **⚠️ 待业务确认**：业务 2026-09-24 对 NGV 定的是留空，igl 是否跟？ |
| `KHMBDJ` 客户目标价 | `0` | 报价阶段没有；业务 2026-09-24 确认 0 可接受 |
| `DWMC` 单位 | `本` | 业务 2026-09-23：书版产品单位用「本」（ERP 单位枚举含「本」） |

查看：`uv run ./scripts/customer_profile.py --prefs igl`。
改口径要**同时改** `erp_fixed`（落库值）与 `preferences`（给人看的清单），
`--validate` 与测试会拦不一致；想留空就在 `erp_fixed` 里写 `""`。

---

## 4. 字段来源对照（IGL 口径）

| ERP 字段 | 来源 |
|----------|------|
| `CPLB` 产品类别 | `rules.cplb.map` 关键词 → 树节点（样本 → `23.01;无线胶装书`）。长正则在前，`board book` 限定写法防误命中 |
| `CPMC` 产品名称 | 默认模板 `{cplb}-{series_name}` → `23.01;无线胶装书-Puzzle & Colour Disney` |
| `TZS` 款数 | `number_of_titles` / `versions`（样本 2） |
| `CPSL1`~`CPSL10` | **档位口径**（`quantity.mode: tiers`）：客户给一排档位按列序依次落 `CPSL1..n`，其余 `0`（样本 3000/5000/7500/10000/20000/25000/30000） |
| `CD`/`KD`/`GD` | `size` 按 `size.order: [CD, KD, GD]`（客户写 `H x W x D`）。样本 `198 × 129 × 6.85`；`GD` 由页数+纸张算（`extent/2 × 内页厚 + 封面厚`） |
| `ZDFS` 装订方式 | `rules.binding.map`（`perfect bound`/`paperback`/`无线胶装` → `胶装/PAD书`），未命中用 `default`（同样必须合法） |
| `Drop_SJFX` 书脊方向 | `spine.mode: cd_vs_kd`（`CD > KD` → `长书脊`）。样本 `198×129` → `长书脊` |
| `DWMC` 单位 | 档案固定值 `本` |
| `CPSX` 产品属性 | `rules.cpsx`：`Target Age Group=7+` + `Safety Checks=Y` → `3岁以上（要寄第三方测试）` |
| `JHRQ` 交货日期 | `dates.JHRQ.from: pub_month` → `YYYY-MM-DD`（样本 `May-27` → `2027-05-01`） |
| `WCRQ` 预计下单日期 | `today_plus_days: 10` |
| `KHDH` 客户单号 | 邮件标题/正文优先，其次附件列 `works_references`（样本 `23992 / 23993`） |
| `CPMS` | ★ **agent 亲自写**，脚本留空 |

**尺寸/厚度两个坑**：① 纸张克重传**裸数字**（`250`，不是 `250g`）—— 带 `g` 查不到，
会静默退化成 `克重 × 0.001` 的粗估；② 表里没有的克重按**同类型最近克重**取值（宁厚勿薄）。

---

## 5. 不该被脚本碰的字段

`BDBH` `YWY` `KHZL` `GDY` `Remark` `CPMX`（ERP 自动带出，业务已确认留空即正确）、
以及 IGL 样本里客户文件根本没提的 `BZFS` `TBLX` `SHFS` `FSCMC` `OLD_KHDDH` `SFYPJ`
`Remark1` `CPLX` `KHDDH` `ECPMC` `ZXLB` `ZBLX` `ZXFS` `DKMC` `YSLX` `SHDQ` `KHDQ`
`BGMS` `Remark4` `Remark3` —— 它们应**等于模板默认值**，`tests/test_pipeline.py`
的 `UNTOUCHED` 列表逐个断言。往这些字段里加逻辑前先想清楚：**是通用能力还是 IGL 特有**。

---

## 6. 回归基线与命令

```bash
cd /home/claw/customer-quote-request

# 单份处理
uv run ./scripts/main.py "customer-files/pending/<附件>.xlsx" --customer igl

# 全量测试（含 IGL 端到端逐字段基线）
uv run pytest -q                              # 当前 179 项
uv run pytest tests/test_pipeline.py -v       # 只看 IGL 端到端

# 只看抽取结果（调试用）
uv run ./scripts/extract_fields.py "customer-files/pending/<附件>.xlsx" --customer igl --json

# 抽查某份产物的取值合法性
uv run ./scripts/erp_schema.py --check <ERP JSON 路径>
```

**基线（`tests/test_pipeline.py::EXPECTED_VALUES`，改代码必须过）**：

```
CPDL=书版  YYS=1  BZ=美元  PXH=1  KHMBDJ=0
CPLB=23.01;无线胶装书   CPMC=23.01;无线胶装书-Puzzle & Colour Disney   TZS=2
CPSL1..7 = 3000/5000/7500/10000/20000/25000/30000（CPSL8~10 = 0）
CD=198  KD=129  GD=6.85   Drop_SJFX=长书脊   ZDFS=胶装/PAD书   DWMC=本
CPSX=3岁以上（要寄第三方测试）   JHRQ=2027-05-01   KHDH=23992 / 23993
CPMS=''（必须为空，交给 agent）
抽取明细：unmatched_labels=[]，quantity_tiers=[3000,5000,7500,10000,20000,25000,30000]
附加信息（CPMS 素材）：Carton Weight / Quotation Number 等必须被列出
```

---

## 7. 坑（踩过的）

- **同一行可能有多个键值对**：`Imprint | Autumn | Complexity | Low | Requested By | (空格) | Christine`
  —— 只做「标签列 + 值列」配对会把这些字段**整行丢掉**（`auto_pairs` 就是为它写的）。
- **相邻两行都是标签行时，不能把下一行的标签当值**：`auto_pairs` 对「密集标签行」
  先看下方是不是标签，是则改取右侧（`min_label_row_hits: 2`）。
- **自由文本切标签必须精确匹配**：模糊匹配会让 `Series Nam` 也算命中 `Series Name`，
  「最长前缀」永远等于整行 → 值退化成单个字母（实测抽出 `'y'`）。
- **短别名会截胡长标签**：`Series` 抢 `Series Name`、`Group` 抢 `Group/Series Number`
  —— 档案里**最长别名优先**。
- **数量档位的纯数字写法**靠标签提示词判定，且必须**大小写不敏感**
  （`Quantity` vs `quantity`）—— 这个坑让档位静默全 0 过一次。
- **档位识别刻意做严**（宁可漏判不可误判）：`3,000` / `3 K` 分格 / 每档单独一行都识别不到，
  会静默全 0（日志有 WARN）。改客户文件写法时先补 `rules.quantity_tiers`。
- **`ZDFS` 不合法值**：`无线胶装` 不是 ERP 合法值（历史单据 `0e39e2e2` 就填错了），
  正确是 `胶装/PAD书`。映射表里 `无线胶装` 已经被映射过去。
- **`CPMS` 完全不是脚本的事**：留空的 JSON 视为未完成，不得交付。

---

## 8. 交付检查清单（IGL 专用）

- [ ] 客户识别是 `igl`（不是 NGV；正文特征优先）
- [ ] 59 键顺序断言通过、`.validation.json` 里 **0 error**
- [ ] 与 `tests/test_pipeline.py` 的基线逐字段一致（尤其是 `CPSL1~7`、`CD/KD/GD`）
- [ ] `CPLB` 是树里真实节点，名称与树一致
- [ ] `KHDH` 来自邮件或附件列，允许 `23992 / 23993` 这种多号写法
- [ ] 档位数与客户表一致的**列序**（这是「档位顺序口径」待确认项，若业务改口径需重写规则）
- [ ] `CPMS` 已由 agent 手写（非空），`extras_found` 里的附加信息一条不漏
      （纸箱重量/尺寸、报价有效期、加箔/UV/击凸等增值工序都只存在于此）
