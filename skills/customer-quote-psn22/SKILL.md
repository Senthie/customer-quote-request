---
name: customer-quote-psn22
description: PSN22（Magic Cat Publishing，英国童书/纸板书）报价需求提取 —— 无标签 docx 规格书 + 刀模 PDF → ERP 59 字段。处理 PSN22 的报价需求、附件解析、口径核对时加载；总纲见 skill customer-quote-request。
metadata:
  openclaw:
    emoji: "🇬🇧"
    requires:
      bins:
        - uv
        - python3
---

# PSN22 报价提取（子技能）

> 总纲：skill **`customer-quote-request`**（引擎、四道门、推送 runbook 都在那边）。
> 唯一真源：**`customers/psn22.yaml`** —— 本文件只是「怎么读它、踩过什么」，改行为改档案。
> 校准于 2026-11-13（第一份真实询价 `Sleuth and Clue` → ERP `BJXQ261113003`）。

## 1. 附件长什么样

英国 **Magic Cat Publishing** 的询价，多由 ghouseproducts（Alex / Rex）转来。
一封邮件 = **一份报价**，单位是「一个文件夹」：

```
Sleuth and Clue.docx                  ← 规格正文（唯一有信息量的文件）
Revised die and glue INT.pdf          ← 刀模 + 上胶图
Revised die and glue NESTING.pdf      ← 拼版刀模图
```

docx **只有值、没有标签**（这是这个客户唯一的难点）：

```
Sleuth and Clue                                                  ← 产品名
270 x 210mmm (P)                                                 ← 尺寸 + P=竖版
12 spreads + case + 21 tip-on flaps.                             ← 结构
Text 4/0 on 190gsm C1S artboard fsc + gloss press varnish        ← 内文纸+印色+表面处理
Case 4/0 on 157gsm matt art paper fsc + matt lamination          ← 封面纸+印色+表面处理
Case bound, accordian binding, text folded and glued on foredge only.
9 tip on flaps 4/4 sheet size 520 x 490mm + …                    ← 揭页贴片的展开尺寸
12 tip on flaps 4/4 sheet size 520 x 440mm + …
2000/3000/5000/7000/10000/12000/15000/18000/20000/25000/30000    ← 数量档位（斜杠串）
1c plate change to text                                          ← 换版
90% foil to front case / 5th colour on case / 90% spot uv …      ← 选配
```

策略仍然走 `text_lines`（自由文本），但**认标签**和**认正则**两条路一起用：

| 内容 | 靠什么 |
|---|---|
| 内文/封面纸张印色 | `fields` 别名（行首 `Text` / `Case`） |
| 尺寸 | `rules.scan` 正则 → `270 x 210` |
| 数量档位 | `rules.scan` 正则抓斜杠串 → `split_tier_list()` 拆档 |
| 装订 | `rules.scan` 整行抓（**不能让 `Case bound` 当标签被吃掉**，见坑 2） |
| 类别 CPLB | `rules.cplb.map` 按关键词落树节点 |
| 中文品名 / CPMS / 选配措辞 | **agent 手写**（引擎给不了） |

```bash
cd /home/claw/customer-quote-request
# 单跑一份（输出到 /tmp，别让测试产物落进 processed/）
uv run ./scripts/main.py <单元目录> --customer psn22 -o /tmp/psn22-out --today 2026-11-13
uv run ./scripts/customer_profile.py --show psn22      # 档案摘要
uv run ./scripts/customer_profile.py --prefs psn22     # 偏好表（落库值）
```

## 2. 怎么认出这是 PSN22

发件人域名**靠不住**（几乎都是 ghouseproducts 转发，那个域名同时服务多个客户），
所以识别靠 docx 的写法组合（`detect.label_signatures`，至少命中 2 个）：
`tip-on` / `press varnish` / `case bound` / `spreads + case`。

```bash
uv run ./scripts/customer_profile.py --detect "<某个 docx>"
```

## 3. 品牌常识（判类别时用得上）

- 产品线以 **BB 书（board book / 纸板书）**为主：语料 454 份里
  普通BB书 56 / 精装BB书 45 / 其它套装 40 / 精装音乐BB书(线路) 33 / 方脊精装书 32；
  另有大量揭页（lift-the-flap）、拉卡机关、砌图、POP UP、发声书。
- **风琴折的书走 `27.21;风琴揭页BB书`**，即使封面是精装（有灰板）：
  语料 3 单「精装封面 + 内文风琴折」业务都落这一类（LR25032502 系列，
  ERP 备注原文写「装订方式：精装风琴折页BB书」）。业务 2026-11-13 也确认
  「按树里的节点为主」→ 不要用 `22.02;方脊精装书`。
- 客户的市场常是 UK / US / 出口多国，检测标准（EN71 / ASTM / Japan Food Sanitation Law）
  因市场而变 → `Remark4` **不设默认值**，按原文照抄。

## 4. 坑（都已在档案里解决，改之前先看）

1. **斜杠串档位不是单一数量。**
   `2000/3000/…/30000` 交给 `parse_single_quantity()` 会被判「一串多个数字 → 不敢猜」→
   落 0，十档全丢。`extract_fields.split_tier_list()` 认 ≥3 档的斜杠串，
   超 `rules.quantity_tiers.max_tiers`（10）按顺序截断并打 WARN。
   ERP 只有 `CPSL1..10`，客户给 11 档时的业务口径是**删最后一档**。

2. **`Case bound` 不要当 `fields` 标签。**
   当标签吃掉之后，值里只剩 `accordian binding, …` —— `case bound` 这几个字丢了，
   装订映射（`ZDFS`）和类别映射（`CPLB`）都判不出来，静默落空/落 22.02。
   现在的做法是 `rules.scan` **整行抓**（值里保留 `Case bound`）。

3. **客户把 accordion 拼成 `accordian`。**
   类别正则必须写 `accordi[ao]n`，只写 `accordion` 会漏 → 落到 `case bound` 分支。

4. **数量口径 = 单款 × 款数。**
   产物 `CPSL1..10` 存**单款**，推送时 exporter 才 × `TZS`。若某单客户只给总合计，
   要拿 CPMS 里的箱数/总本数反算核对，别让「总合计」直接进 `CPSL1`（会成倍放大）。
   2026-11-16 口径硬化：只给总合计时引擎按款数**均分**（`160000 ÷ 4 → 40000`）；
   **除不尽就不均分**，保留总数并写 `_qty_conflict` → 输出阶段 WARN 进待确认。
   不变式（回归测试钉住）：`CPSL1 × TZS == 客户总本数`。

5. **日期语料 454/454 全空 → 档案口径不落库。**
   但 ERP 明细里 `delivery_date` / `expected_date` 是**强制必填**，推送时必须兜底：
   `export_calls(..., default_date={"JHRQ": "…", "WCRQ": "…"})`。
   兜底值只进 `.erp-import.json`，**不写回 59 键产物**。

6. **`CPDL` / `DWMC` 语料 454/454 为空**，但公共默认层会填「书版 / 本」。
   不影响建单（都不是 ERP 必填），要不要清空待业务拍板 —— 别自己改。

## 5. 客户偏好表（落库值，`customer_profile.py --prefs psn22` 为准）

| 字段 | 值 | 依据 |
|---|---|---|
| `BZFS` | 纸箱包装 | 语料 454/454 一致 100%（默认值层） |
| `PXH` | 1 | 语料 454/454 都是 1（与公共默认值一致，无需单独覆盖） |
| `chkBSYTB` | false | 语料 454/454 false |
| `chkZDBJF` | false | 语料 454/454 false |
| `chkBSYJZX` | false | 语料 454/454 false |

**没有默认值**的（数据不支持，别为了「填满」去补）：
`SHFS`（FOB 盐田 87%）、`TBLX`（不打卡板 29%）、
`SFYPJ`（否 58%）、`Remark1` / `Remark3` / `Remark4`（每单不同）、
`FSCMC` / `CPSX`（语料全空 → 按原文判，判不出就留空 + 问业务）。

> **`CPLX`（产品类型）2026-11-20 起有工具**：`uv run ./scripts/product_type.py --text <task> --customer <代号> --lookup`
> 判据见总纲 `docs/产品类型判定.md`（业务给的原文）。PSN22 这类新项目一般是 `新产品`；
> 客户称返单（Reprint / re-order）时**必须查 ERP 客户订单历史单**再落档，不许凭词落档。

> ⚠️ **`ZDFS` 已于 2026-11-16 由业务改口径 → 不再是「判不出就留空」**：
> 客户**没提到**装订方式时一律落 **快线BB书**，唯一例外是类别/品名含「套装」→ 落「套装」。
> 语料里「快线BB书 21%」只是历史分布，**不能**当成「该留空」的依据（旧写法已作废）。
> 产物侧 `main.py::default_binding_for()`、推送侧 `erp_mcp_export.apply_defaults()` 双兜。
> 客户**提到了**但 PSN22 档案没映射（如 `perfect binding` 之类）时，产物仍留空、
> 只在推送那一刻兜成 快线BB书 —— 这种情况要在交付说明里点出来。

## 6. 业务拍板口径（2026-11-19/20，森西；已落 `customers/psn22.cpms_accepted.json` 白名单）

这三条是**客户文件里没有、但必须这么写**的，`cpms_content_audit.py --accepted customers/psn22.cpms_accepted.json`
放行它们；改口径改白名单，不要在 CPMS 里临时让步。

1. **客户要的克重我们没有 → 先查常用克重表，再换等厚度的那一档。**
   实例：docx 写 `190gsm C1S artboard`，我厂**没有 190g 单粉** → 报 **210g 单粉**。
   ⚠ 森西当场纠正过：**不许凭感觉换**，必须
   `uv run ./scripts/paper_weight.py --parse "190g单粉"` 核「常用克重表 + 厚度」
   （190g 与 210g 厚度同为 0.26mm 才成立），并把「客户询价 190g 不常用，我厂按 210g 报价」
   写进 CPMS 备注。这条对**所有客户**成立，不只是 PSN22。
2. **精装书必须有灰板。** 客户 docx 只写 `Case bound`、不给灰板克重时，
   按「常用 1200g / 1800g 取**便宜**的」→ **1200g 灰板（FSC）**，写进皮壳那一段
   （`157g 哑粉(FSC) 裱 1200g 灰板(FSC)`）。属加工必需件，不算编数字。
3. **烫金 / 局部 UV / 击凸按 100% 报。** 客户文件里的 `90% foil…` 是上一轮询价的旧口径；
   这家图稿基本 100% 满版烫金，报价**先按贵的（100%）**。同段还要保留内文/揭页/皮壳的版费
   与皮壳第五色。
4. **装箱、检测清单不需要补齐**（业务明确）：装箱保持「待确认」，检测保持 `EN71 part1,2,3,9`，
   不许为了「看起来完整」自己编箱规或加检测项。

## 7. 交付检查清单

- [ ] 抽取后先看 `unit.extracted.json`：`size` / `quantity_tiers` / `binding` 三个值对不对
- [ ] `CPLB` 是否落在树里的具体子类（不是 `32.00;其他`）；风琴折必须是 `27.21;风琴揭页BB书`
- [ ] `ZDFS` 是否由 `Case bound` → 精装书（原文明写 Hardcover 时也一样）
- [ ] `CPSL1..10` 与客户档位逐档对齐；客户给 11 档时确认「删最后一档」并打 WARN
- [ ] `CPMC` / `CPMC_ZH` 是**人拟的中文品名**（引擎只会给 `类别-英文名` 底稿）
- [ ] `CPMS` 由 agent 通读原文后手写：皮壳/内文/贴片/装订/装箱/请拆分/请选配/备注
- [ ] `TBLX` / `SHFS` / `Remark4` 推送前必须补（ERP 明细强制必填，客户通常不给）
- [ ] 四道门：`cpms_lint`（0 error 0 warn）→ `cpms_content_audit`（无出处 0）→
      `erp_schema.py --check <产物> --customer psn22`（65 字段全过）→ 幂等重生 `.erp-import.json`
- [ ] 推送：`erp_quote_create(confirm=false)` 干跑必须 `errors=[]` **且** `warnings=[]`
      → `confirm=true` 判 `status=created` + `verify.row_created/qty_saved`
      → `erp_quote_list(key=BJXQ…)` 回读核对（详见总纲 skill `customer-quote-request` 的推送 runbook）
- [ ] 回归：`uv run pytest tests/test_psn22_profile.py -q`
