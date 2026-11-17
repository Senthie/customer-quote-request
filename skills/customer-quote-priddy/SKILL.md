---
name: customer-quote-priddy
description: "Priddy Books / Neon Squid（英国 Macmillan）询价的专属知识 —— 英文标签清单、BB 书与卡片套装的区分、`Quantities: 1k-100k scales` 这种区间报价怎么落档位。处理 Priddy 报价时配合总纲 skill customer-quote-request 一起加载。"
metadata:
  openclaw:
    emoji: "🇬🇧"
    requires:
      bins: [uv, python3]
---

# Priddy Books 报价需求（子技能）

> 总纲：skill `customer-quote-request`（引擎、字段总览、CPMS 规范、推送 runbook）。
> **唯一真源：`customers/priddy.yaml`** —— 改行为改档案，不改文档，也不改代码。
> ERP 客户资料代码 `PRD21`（221 份）+ `PRD25`（69 份），同一客户两套代码。

## 1. 附件长什么样

纯英文正文，规格是「标签 + 值」（同行或下一行）：

```
TPS: 215(w) x 280(h) - portrait
Extent: 64pp (32pp/16 sheets stickers, 16pp coated, 16pp uncoated)
Uncoated contents: 4/4 on 120gsm uncoated woodfree offset
Coated contents:   4/4 on 180gsm glossy art plus gloss UV varnish
Sticker sheets:    4/0 on 100gsm reusable sticker stock, kiss cut with approx 25 stickers per sheet
Cover:             4/0 on 250gsm plus gloss UV varnish on one side
Binding:           Limp, sewn, cover drawn on, sticker sheets perforated 12mm from spine…
Packing:           In cartons and on pallets
Quantities:        1k-100k scales
```

另有一类**卡片套装**（`STF Flashcards`）：先写盒子（`Box: TPS: Size to house the 15 cards`），
再写卡片（`Flashcards: 15 cards / TPS: 160mm (h) x 110mm (w)`），最后列一堆触感工艺增价项。

## 2. 字段怎么来的

| 字段 | 来源 | 说明 |
|------|------|------|
| `TZS` | 扫描 `^N cards` / 款名 | 单体书 = 1。⚠️ **卡片套装现有实现会被扫成卡数**（`15 cards` → `TZS=15`），但按业务口径应是 **1 款 = 1 套 15 张** —— 这是待修的字段坑，**不要**拿它当 CPMS 依据（概要行按套装「件」写，见 §4） |
| `CPSL1..7` | `Quantities:` 一周 | **`1k-100k scales` 是区间**，档案开 `quantity_tiers.range_expand: true` → 按 1-2-5 阶梯展开成 7 档（1000/2000/5000/10000/20000/50000/100000），日志 WARN 标明「我方展开，需业务确认」 |
| `CD`/`KD` | `TPS:` 行 | 书写顺序落：`215(w) x 280(h)` → CD=215 / KD=280。**卡片套装**的盒 TPS 是散文（`Size to house the 15 cards`）→ 尺寸落 0/0/0 交人工，别把 15 当 CD |
| `GD` | 不做推算 | 语料 320/333 是 0 → `thickness.enabled: false` |
| `ZDFS` | `Binding:` + `rules.binding.map` | 卡片/盒子套装 → 套装；`perfect binding`/`paperback` → 胶装/PAD书；BB 书按机制分快线/中线/慢线 |
| `CPLB` | `rules.cplb.map` | 套装 → `29.04;其它套装`；BB 书 → 树里对应节点；贴纸书 → `23.01;无线胶装书` |
| `BZFS` / `SHFS` | 档案 `erp_defaults` | 纸箱包装 100% / FOB 盐田 100% |
| `TBLX` | **不设默认** | 语料「1.2*1.0 欧式 149 vs 1.2*0.8 欧式 57」不稳定 |
| `CPMC` / `CPMS` | **agent 手写** | 引擎只给占位符 |

## 3. 坑

1. **`1k-100k scales` 不是数量，是「报价区间」**。客户要你从 1 千报到 10 万。
   档案按 1-2-5 阶梯展开 7 档并留痕；**不要**把它当单一总数（会落 1 万），
   也不要自己拍一组别的档位。业务口径变了就改档案的 `quantity_tiers`。
2. **盒子/卡片的单位不同**。Flashcards 是「15 张卡片 + 1 个烟盒式盒」，
   ERP 记一条产品：`TZS` 应然是 **1 款**（一套 15 张 —— 15 是**件数**不是款数）、
   尺寸落 0/0/0（盒内单件尺寸不录）。
   ⚠️ 引擎当前会把 `15 cards` 扫成 `TZS=15`（`versions.title_pattern` 里的 `cards?`），
   这是**待修的字段坑**：产物保持原值，汇报里按「字段 / 现值 / 原文 / 应然值」点出来，
   **不要**按 `TZS` 写概要行、也不要悄悄改字段。
3. **`Limp, sewn` 判不出户品装订**。「锁线胶装 vs 车线书」语料里两种都有（车线 3 / 胶装 15），
   不足以定口径 → 档案**刻意不映射** `sewn`/`limp`，落 ERP 第一个合法值占位 +
   `Q_ZDFS` 确认项。CPMS 里必须写「装订方式无法判断值，需业务自行判断」。
4. **BB 书按机制分快线 / 中线 / 慢线**（语料配对稳定：精装BB书→快线、拉卡机关→中线、
   毛毡布偶→慢线）。客户没写机制时别猜，落占位 + 标注。
5. **`kiss cut` / `reusable sticker stock` 是 Priddy 的习惯写法**，可以当识别特征；
   但别拿 `sticker` 单字判客户（别家也贴纸）。
6. `Quantities:` 行偶尔写成 `1k, 2k, 3k, …` 或 `10k/25k/50k/100k` —— 引擎都认
   （2026-11-17 修了 `10k copies` 这种「k + 量词」叠写）。
7. 联系人写 `Kylie Hamley`（Head of Production）或 `Beth Warne`；同一封里常同时要
   「分别报价」的多种触感工艺（foil / felt / embossing）→ 这是**增价项**，进 CPMS 备注。
8. **文案易错点（2026-11-17 实测写错过）**：
   - `gloss lamination` = **光胶**，`matt lamination` 才是哑胶（Flashcards 的盒子是 gloss → 光胶）；
   - 客户只给克重不给纸种（`Cover: 4/0 on 250gsm plus gloss UV varnish`）→ 严格模式
     **不要补「单粉」**，CPMS 写 `封面：250g，4C+单面光油/0C` 即可（`cpms_lint` 认）；
   - `100gsm reusable sticker stock` = `100g可移胶贴纸`，原文没有「铜版」就不写铜版；
   - `perforated 12mm from spine` = 距书脊 12mm **打孔线**（不是「打孔」）。
9. **`Drop_SJFX` 方向别照抄字段就完事**：档案是 `spine.mode: cd_vs_kd`，而 `CD/KD` 按客户
   书写顺序落（`215(w) x 280(h)` → CD=215/KD=280）→ 竖版书被判「短书脊」；
   同一批 Nosy Crow 的 `300(h) x 220(w)` 却是「长书脊」。**竖版书脊沿长边，应然是长书脊**。
   字段疑义如实报，CPMS 概要行仍与字段一致（否则 `cpms_lint` 红），**不要**为了消 CPMS013 编方向。
10. **`KHDH` 报价阶段恒空**：语料 18476 份 KHDH 有值 0 份（PRD21 264 + PRD25 69 全空）→
    `priddy.yaml` 已声明 `rules.required_empty: [KHDH, KHDDH]`，不再生成卡交付的 `Q_KHDH`。
    老产物里那条直接 `waived`（answer 写清「客户邮件无 PO/RFQ 号 + 语料依据」），**不编单号**。

## 4. 交付检查清单

> 后半段套路（补 CPMS → **重导 `.erp-import.json` 侧档** → 关确认项 → 报字段疑义）见总纲技能
> `customer-quote-request` 的 `references/cpms-backhalf-cron.md`。改完 CPMS 不重导侧档 = 推 ERP 带旧文案。

```bash
uv run ./scripts/main.py <客户文件或目录> --today YYYY-MM-DD
uv run ./scripts/erp_schema.py --check <产物>.json --customer priddy
uv run ./scripts/cpms_lint.py <产物>.json
uv run ./scripts/cpms_content_audit.py --cpms <产物>.json --source <客户原始文件>
```

- [ ] `CPSL` 档位与客户写法自洽：给区间 → 1-2-5 阶梯且日志有 WARN；给具体档 → 逐档落
- [ ] 卡片套装的 `TZS` 当前落的是卡数（`15`）—— **已知字段坑，应然值是 1**；
      `CD/KD/GD` = 0/0/0 才是对的。汇报时点明，别按 `TZS` 写概要行
- [ ] `ZDFS` 若为占位值 → `Q_ZDFS` 存在 且 CPMS 里写了「需业务自行判断」
- [ ] `CPLB` = `29.04;其它套装`（套装）或 BB 书对应节点，不能是 `32.00;其他`
- [ ] CPMS 写清「分别报价」的增价工艺，但没写进 `ZDFS`/`CPLB`
- [ ] `CPMC`/`CPMC_ZH` 是中文品名（agent 手写），`ECPMC` 照抄客户英文品名
      —— ⚠️ 本档案**没有** `rules.ecpmc.source_keys`，引擎产物里 `ECPMC` 是空的，
      交付时由 agent 手写补上（2026-11-17 的 `STF Sticker Book` / `STF Flashcards` 即如此）
- [ ] 卡片套装（Flashcards 这类）的概要行写「共【N】件」，N = 组成声明实物件数
      （`1套=陈列盒+15张卡片` → 16）；`16` 原文里没有，`cpms_content_audit` 会报无出处 →
      审计必须带 `--accepted customers/priddy.cpms_accepted.json`（已建，条目写明依据）
- [ ] 卡片套装的 `TZS` 是字段坑：`15 cards` 会被扫成 `TZS=15`，但 15 是**卡数**不是款数
      （1 款 = 1 套 15 张）→ 汇报里按「字段 / 现值 / 原文 / 应然值」点出来，**不在 CPMS 里圆**
