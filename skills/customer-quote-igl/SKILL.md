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
>
> ★ 开工前先读总纲 `references/business-alignment-and-scope.md`：
> 业务给参照单先问「比哪一层」（默认学 `CPMS`）、`CPMS` 严格模式、「没有就是没有」、
> 口径变更四处同步、交付四道门。
>
> ★ **`CPMC` / `ECPMC` / `GD` 三条口径 2026-10-21 已定案（业务拍板，IGL 跟 NGV）：**
> ① `CPMC` = 产品名称的**中文译名**（脚本给占位符 `【中文品名待定】`，agent 交付前补），
> `CPMC_ZH` 同值；旧写法 `23.01;无线胶装书-<系列名>` **作废**（那是历史 ERP 记录的写法）。
> ② `ECPMC` = 报价**原始产品名**（照抄客户原文，不译不拼类别；igl 取 `series_name`）。
> ③ `GD` **不做推算** —— 客户写了厚度就照抄，没写就是 `0`。
> 改口径 = **六处同步**（不是「四处」）：档案 `customers/igl.yaml` → `scripts/main.py` →
> `tests/test_pipeline.py` 基线（`EXPECTED_VALUES` / `UNTOUCHED`）→ `tests/test_igl_profile.py`
> + `tests/test_cpms_todo.py` → `assets/erp_form_schema.yaml` 的 `notes` → 本文件 + 总纲 `SKILL.md`。
>
> ★ **产物格式 2026-10-29 改为数组**：`ERP_*.json` 顶层是 `[{...}]`（一个报价文件可含多个产品），
> 读它一律用 `erp_bundle.load_bundle(path)`，别再 `json.load()` 之后当 dict 用。
> ★ **完整 SOP 见 `customer-quote-request/references/changing-a-field-convention.md`**（口径变更要动的
> 所有位置清单、找残留旧值的 grep、关能力时的双向测试、外科手术式改已交付件、闸门逐字段查。
> 类级内容 —— NGV 等其它客户同样适用，别在客户子技能里另抄一份。
>
> ★ 业务说「**按新规格重新开一单**」时走 `customer-quote-request/references/reissuing-an-order.md`：
> `ingest_email.py --eml <原邮件>` 重投 → `main.py` 出**新 task 目录**（旧目录保留）→ agent 手写
> `CPMS`/`CPMC`/`CPMC_ZH`/`ECPMC` → 写 `CPMS_来源对照.md` → 四道门 → 汇报点明「新单号取代旧单号」
> 并**问**是否把旧目录移 `processed/_superseded_<日期>/`（别自己删）。
> 审计源要同时给 `--source <xlsx> <邮件正文.txt>` 才能覆盖只在邮件里的事实
> （`.eml` 不能直接当源，先用 `email` 模块转 txt —— 片段见该 reference）。

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
| `CPMC` 产品名称 | ★ 2026-10-21 新口径：**中文译名**（档案 `cpmc.template: "【中文品名待定】"` + `needs_agent: true`）→ 脚本给占位符，agent 交付前写真的译名（`BIBLE HEROES Treasury` → `圣经英雄故事珍藏集`）。旧写法 `{cplb}-{series_name}`（`23.01;无线胶装书-Puzzle & Colour Disney`）**已作废** |
| `CPMC_ZH` | 与 `CPMC` 同值（同一次补写里两个字段一起改） |
| `ECPMC` 产品英文名 | `rules.ecpmc.source_keys: [series_name]` → 客户原文品名照抄（`Puzzle & Colour Disney` / `BIBLE HEROES Treasury`），先被 `series_name.strip_pattern` 洗掉表单前缀与引号 |
| `TZS` 款数 | `number_of_titles` / `versions`（样本 2） |
| `CPSL1`~`CPSL10` | **档位口径**（`quantity.mode: tiers`）：客户给一排档位按列序依次落 `CPSL1..n`，其余 `0`（样本 3000/5000/7500/10000/20000/25000/30000） |
| `CD`/`KD`/`GD` | `size` 按 `size.order: [CD, KD, GD]`（客户写 `H x W x D`）。样本 `198 × 129`（客户**没给厚度**）→ `GD=0`。★ 2026-10-21：**`thickness.enabled: false` + `boards.enabled: false`，不做任何厚度推算**；客户写了 `D` 就照抄，没写就是 0 |
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
以及 IGL 样本里客户文件根本没提的 `FSCMC` `OLD_KHDDH` `SFYPJ` `Remark1` `CPLX` `KHDDH`
`KHDQ` `BGMS` `Remark4` `Remark3` —— 它们应**等于模板默认值**，`tests/test_pipeline.py`
的 `UNTOUCHED` 列表逐个断言。往这些字段里加逻辑前先想清楚：**是通用能力还是 IGL 特有**。

> ★ **2026-10-30 起 `BZFS` 已不在这份名单里**：新增的 `spec_fields` 推导层会从客户原文推
> 包装/台板/运输/纸板等 11 个字段（`BZFS` `TBLX` `SHFS` `SHDQ` `ZXLB` `ZBLX` `ZXFS`
> `DKMC` `YSLX` `chkBSYTB` `BZ`）。IGL 的样本里有 `carton` 字样 → `BZFS=纸箱包装`，
> 所以它已从 `UNTOUCHED` 移出（留在名单里的那 10 个正好守住「没证据不许碰」）。
> ★★ **2026-11-03 起 IGL 有 `erp_defaults` 了（用语料反推，不是业务口头给的）**：
> `BZFS=纸箱包装`(2520/2520)、`SHFS=FOB 盐田`(2497/2520)、`chkBSYTB/chkZDBJF/chkBSYJZX=false`。
> **`TBLX` 故意不设默认** —— 2520 份里一致率只 **34%**（不打卡板 34% / 欧标松木 27% /
> 欧式实木 20%），给了就是瞎猜。
> 后果一：IGL 单**现在会自动带上 `SHFS=FOB 盐田`**，`tests/test_pipeline.py` 的 `SHFS`
> 已从 `UNTOUCHED` 移到 `EXPECTED_VALUES` —— 基线里多这一行不是回归。
> 后果二：仍进确认清单问客户的只剩 `TBLX` / `SHDQ` / `YSLX` / `ZXLB` / `ZBLX` / `ZXFS` /
> `DKMC`（`blocking=False` 不拦交付）—— 设计行为，别去 `pending/` 找原因，也别因为
> 「`SHFS` 突然有值了」去查引擎。
> ↳ 重算/校准命令、两率判据（覆盖率×一致率，业务确认可越线）、`erp_khzl` 归组：
> `customer-quote-request/references/customer-erp-defaults-derivation.md`。
>
> 🔁 **跑回归/校准样本前先看两条**：
> - 批量建档（`--bootstrap --min-records 100` 给还没收到询价的客户铺默认值、draft 档案约定、
>   子技能只对 calibrated 强制）→ `customer-quote-request/references/batch-profile-bootstrap.md`
> - **别直接拿仓库样本跑 `main.py`**（`customer-files/processed/*/PUZZLE COLOUR DISNEY.xlsx`
>   是 test_pipeline 的基线，跑一次就没了）—— 用
>   `customer-quote-request/scripts/run_sample_safely.sh <样本> --customer igl`，
>   原理与恢复办法见 `customer-quote-request/references/pipeline-local-run-safety.md`。
> 机制与坑见 `customer-quote-request/references/spec-fields-and-linked-dropdowns.md`。

> ★ **`ECPMC` 2026-10-21 起不再留空**：业务拍板「IGL 按 NGV 口径做」→ 档案已声明
> `rules.ecpmc.source_keys: [series_name]`，`ECPMC` = 客户原文品名，并已从 `UNTOUCHED` 移出。
> `cpms_todo.py` 现在会把「igl 单 ECPMC 空着」列为欠账（`tests/test_cpms_todo.py` 钉着）。
> 系列名会被 `series_name.strip_pattern` 洗掉 `Working Title:` 前缀与包裹引号 ——
> `main.py` 的 ECPMC 分支用的是**洗过的** `series_name`（否则同一单 CPMC 干净、ECPMC 带前缀）。

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
CPLB=23.01;无线胶装书   CPMC=【中文品名待定】(agent 补)   ECPMC=Puzzle & Colour Disney   TZS=2
CPSL1..7 = 3000/5000/7500/10000/20000/25000/30000（CPSL8~10 = 0）
CD=198  KD=129  GD=0(客户没给厚度)   Drop_SJFX=长书脊   ZDFS=胶装/PAD书   DWMC=本
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

## 7.5 ★ 2026-10-29 实单 `BIBLE HEROES Treasury`（Igloo / Christine Still）暴露的四处

表形态：单表 `V1 - 25.02.26 - Printer`，`New Product Development Cost Request Form`。
`Format=Hardback`、`Cased book, section sewn, cover drawn on over 3mm boards`、
64pp text + 8pp ends、`over 3mm boards`、`Spine width: To suit extent`（客户没给厚度）。

| 问题 | 现象 | 修法 |
|---|---|---|
| **`ZDFS` 落错** | `cased` 不在 `binding.map`、`case bound` 也匹配不上（cased ≠ case bound）→ 落到最后的 `sewn` → **车线书** | map 里加 `cased` / `case bound` / `hardcover` / `hardback → 精装书`，**排在 `sewn` 前面**。语料 1597 条 `CPLB=方脊精装书` 里 1588 条 `ZDFS=精装书`（99.4%）；`车线书` 那 157 条配的是 `普通车线书`/`精装车线书`（25 类软封面车线） |
| **`GD` 一度算错** | 旧公式只有「内文 + 封面纸」→ 64PP/128g 出 5.16mm。当日加了 `rules.thickness.boards`（命中精装就 `+ 板厚 × 2`）算出 11.16mm —— **业务 2026-10-21 否掉了整个推算**：「GD 设置为 0」「没有厚度就设置为 0」 | 现在 `thickness.enabled: false` + `boards.enabled: false`：客户给了 `D` 就照抄，没给就是 `0`。`board_allowance()` 代码保留（通用能力，别的客户打开开关即可用），`tests/test_igl_profile.py` 同时钉住「igl 关掉 → 0」和「打开时 3mm×2=6mm 仍算得对」 |
| **系列名带表单痕迹** | `Series Name = Working Title: 'BIBLE HEROES Treasury'` → 旧口径下 CPMC 变成 `22.02;方脊精装书-Working Title: 'BIBLE HEROES Treasury'` | `rules.series_name.strip_pattern`：只清前缀与包裹引号，不翻译。2026-10-21 起系列名的去处是 **`ECPMC`**（`BIBLE HEROES Treasury`），`CPMC` 是中文译名位 —— 两条路径都要用洗过的系列名 |
| **环衬/实际尺寸抽不出** | `End Paper Material`、`End Paper Printing Spec`、`Size (actual) H x W x D mm` 三行没有别名 → CPMS 只能手读原文 | 补规范键 `ends_material` / `ends_printing_spec` / `size_actual`（`CANONICAL_KEYS` 同步加） |

**派生值 GD 与审计的冲突**（通用经验，别的客户打开厚度推算时会再遇到）：概要行
`【250*210*11.16】` 里的 GD 若是**算出来的**，`cpms_content_audit` 按子串找原文必然报「无出处」→
逼人删掉或改成 0。审计有 `derived` 通道：数值 == 当前产物 `GD` 字段才放行并打印理由
（`cpms_lint` CPMS011 已保证概要行与字段一致），改个数字就立刻不被放行。
**但现在 IGL 不再推算 GD**（业务 2026-10-21），所以 IGL 单的概要行厚度就该是客户给的或 `0` ——
`0` 也必须写进概要行（`书籍方向：【长书脊】产品规格：【250*210*0】`），lint 要求三段齐全。

**邮件重复投递**：同一封 `.eml` 被投了 3 份（md5 相同），cron 5 分钟扫描跑出 3 个任务目录
（`__1` / `__2` 后缀）。处理顺序：先 `md5sum` 去重 → 只保留一个任务目录 → 其余移
`processed/_superseded_<日期>/`。同名 `*.meta.json` 会因为 cron「只搬文件不搬副档」留在 `pending/`，
记得一起清。

## 8. 交付检查清单（IGL 专用）
- [ ] 客户识别是 `igl`（不是 NGV；正文特征优先）
- [ ] ★ 若输入是**文件夹 / 压缩包 / 多附件邮件**：确认里面**确实是同一单**的参考文件
      （`validation.json` 的 `unit.members` 全是这一单的材料、`conflicts` 已核对）。
      IGL 常一单一个报价表；客户把**多单**塞进一个目录时会被合并成一份 —— 那是错的，
      要拆开逐个跑（口径见总纲 `references/input-unit-multifile-quotes.md`）
- [ ] 59 键顺序断言通过、`.validation.json` 里 **0 error**
- [ ] 与 `tests/test_pipeline.py` 的基线逐字段一致（尤其是 `CPSL1~7`、`CD/KD/GD`）
- [ ] `CPLB` 是树里真实节点，名称与树一致
- [ ] `KHDH` 来自邮件或附件列，允许 `23992 / 23993` 这种多号写法
- [ ] 档位数与客户表一致的**列序**（这是「档位顺序口径」待确认项，若业务改口径需重写规则）
- [ ] `CPMS` 已由 agent 手写（非空），**严格模式**：只写客户文件里的事实，确认单/历史单据的补充信息不进 CPMS
- [ ] ★ `CPMC` / `CPMC_ZH` 已由 agent 译成中文品名（**不能留 `【中文品名待定】` 占位符**，两者同值）
- [ ] ★ `ECPMC` 是客户原文品名（照抄不译）；空着就说明「客户原文没给」而不是漏填
- [ ] ★ `GD`：客户给了厚度 → 照抄；没给 → `0`（概要行也写 0）
      —— 业务给真实单据时，目的是**学交付物怎么写**，不是逐字段对账；确需对账也只报「真错 / 口径差 / 格式差」三层
      （口径与依据见 `customer-quote-ngv` 3.2、总纲第 5 节严格模式）
      —— 规则与五种「柔性造数」见 `customer-quote-request/references/data-provenance-strict-mode.md`
- [ ] ★ 严格模式自检：`uv run ./scripts/cpms_content_audit.py --cpms <ERP.json> --source <客户原始文件>` 退出码 0；
      产物目录有 `CPMS_来源对照.md`（逐句出处）
- [ ] `extras_found` 里的附加信息一条不漏
      （纸箱重量/尺寸、报价有效期、加箔/UV/击凸等增值工序都只存在于此）
- [ ] **定时任务「后半段」（cron 只列出 `CPMS_TODO`、由 agent 补写）**：按下面第 9 节执行，
      并把 audit / lint 的真实结论如实写进汇报（`.eml` 正文要转 txt 才能当 `--source`；
      概要行派生 `GD` 现行 audit 会放行，旧的「无出处 1」是版本差异）

---

## 9. 定时任务「后半段」补 CPMS（IGL 注意点）

触发：`cqr_scan_and_cpms.sh` 打印 `CPMS_TODO: N`，agent 逐条补 `CPMS`/`CPMC`/`CPMC_ZH`/`ECPMC`。
通用坑（audit 不收 `.eml`、`GD` 必然无出处、档位写 `3k`、CPMS013↔011 死锁、重复上传、
一文件多组规格）见 `customer-quote-request/references/cpms-backfill-gotchas.md`。

IGL 专属七条：

1. **`CPMC`/`CPMC_ZH` 写中文品名**（同值、反映产品实质、不带业务员/品牌/类别/FSC），
   例如 `Working Title: 'BIBLE HEROES Treasury'` → `圣经英雄故事珍藏集`；
   `Puzzle & Colour Disney` → `拼图涂色迪士尼`。
   这**就是**现在的主口径（2026-10-21 起与 `tests/test_pipeline.py` 基线一致），不再是「补写路径例外」。
   脚本给的是可见占位符 `【中文品名待定】` —— **不许交付带占位符的产物**。
2. **`ECPMC` 必须填**：igl 已声明 `rules.ecpmc.source_keys: [series_name]`（见 §5），
   值是客户原文品名（照抄、不译、不拼类别）。脚本能抽到就自动填；
   抽不到（纯文本邮件没系列名）要 agent 从客户原文抄，抄不出来就**留空并在汇报里说明**，
   不许自己编英文名。`cpms_todo.py` 会把空 ECPMC 列为欠账。
   不补就永远 `CPMS_TODO: 1`（**首轮清单 ≠ 全部工作量**）。取值 = 客户 `Series Name` 去掉表单前缀与
   包裹引号（`Working Title: 'BIBLE HEROES Treasury'` → `BIBLE HEROES Treasury`）。
   （§5 已同步更新，不再有「IGL 明确不改」的旧说法；判据以**当前档案** + `cpms_todo.py` 为准。）
3. **`YWY` 实测为空**（`igl.yaml` 的 `erp_fixed` 里没有 `YWY`，只有 `ngv.yaml` 配了 `KM`），
   而总纲 §4.1/4.3 写「脚本固定 `KM`」—— 属**文档与实现漂移**。
   补 CPMS 时**只改 4 个字段**（`CPMS`/`CPMC`/`CPMC_ZH`/`ECPMC`），不要顺手补 `YWY`；
   在汇报里列为字段缺陷：「`YWY` 空，业务口径应为 `KM`」，留给引擎侧改 `igl.yaml`。
4. **精装（cased hardback）别套平装结构**：`Cased book, section sewn, cover drawn on over 3mm
   boards, ends secured, square backed` → `皮壳 / 皮壳衬纸 / 内文 / 装订`。实测映射
   （`BIBLE HEROES Treasury` 成本表，2026-10-29）：
   - 封面 `128gsm Gloss Artpaper over 3mm boards` → `皮壳：128g光粉+1800g灰板`
     （3mm=1800g；`4C+哑胶/0C` 来自 `Printed 4/0` + `Finishes: Matt lam`）
   - `8pp ends` + `160gsm woodfree` + `Printed 4/4` → `皮壳衬纸：8PP，160g书纸，4C/4C`
   - `64pp text` + `128gsm matt art` + `Printed 4/4` → `内文：64PP，128g哑粉，4C/4C`
   - `Additional Costs to cover`（Add 100%foil / 50%foil / holo foil / spot UV / emboss /
     1 PMS / pallets）→ **`请拆分` 段**，不要塞进 `备注`
   - `3 shipment splits`、`20 x Advance copies`（重印 6 本）、`Target Age`/`Safety Checks`、
     `Project description`、`Spine width: To suit extent` → `备注`
   - `装箱`：客户给的是「最大箱重 14kg 或 60 本」+ 箱规区间，**装箱数量写「待确认」**
     （原文：carton qty will be confirmed when the order is placed），不是漏抄
5. **`CPMS054` 会在装订段复触发**：`装订：精装，…，封面裱3mm灰板` 同样被判「灰板写 mm」。
   全篇凡提灰板一律写 g（`装订：精装，锁线，方脊，封面裱1800g灰板，环衬粘牢`）→ 0 warn。
6. **档位列表用顿号**：`3k、5k、7.5k、10k、20k、25k、30k`。用 `/` 会被印色正则抓成 `5/10`
   报 CPMS052（与总纲 gotchas §3 同源）。
7. **`GD` 不做推算**（2026-10-21 业务拍板）：客户表里写了 `D` 就照抄；没写（`Spine width:
   To suit extent` / 只给 `250 x 210`）就是 **`0`**，概要行也必须写 `0`
   （`产品规格：【250*210*0】`）。别再用「书芯 + 灰板」推 11.16 ——
   旧产物里的 11.16 已按业务口径改成 0，`CPMS_来源对照.md` 同步改了。
8. **补完必须复跑 `uv run ./scripts/cpms_todo.py`，直到 `CPMS_TODO: 0`** ——
   清单是当下快照，补完 `CPMC_ZH` 后可能冒出 `ECPMC`（档案在会话中途被 sibling 提交过，
   或 `_requires_ecpmc()` 读档案失败时静默返回 False）。判「该不该填」以**当前档案**为准，
   不以「客户名叫 igl」为准。首轮清单 ≠ 全部工作量。
   同时**同步 `CPMS_来源对照.md`**：口径一变（`CPMC` 换中文名、`ECPMC` 由空变有值），
   那张表对应行必须跟着改，否则交付给业务的是两份互相矛盾的文件。
   详见 `customer-quote-request/references/cpms-backfill-gotchas.md` §8 / §11 / §12。

