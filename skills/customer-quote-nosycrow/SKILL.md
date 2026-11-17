---
name: customer-quote-nosycrow
description: Nosy Crow（英国）询价的专属知识 —— 「分区标题 + 散文档」写法、逗号分隔的数量档怎么认、为什么这家的 ERP 默认值全是空的。处理 Nosy Crow 报价时配合总纲 skill customer-quote-request 一起加载。
metadata:
  openclaw:
    emoji: "🇬🇧"
    requires:
      bins: [uv, python3]
---

# Nosy Crow 报价需求（子技能）

> 总纲：skill `customer-quote-request`（引擎、字段总览、CPMS 规范、推送 runbook）。
> **唯一真源：`customers/nosycrow.yaml`** —— 改行为改档案，不改文档，也不改代码。
> ⚠️ **新客户：ERP 语料里还没有这家的单据**（`erp_khzl: []`），所以
> `erp_defaults` 一律空着 —— 这是故意的，不拿别家的口径套上去。

## 1. 附件长什么样

纯英文正文，**分区标题 + 下一行是值**（值常常是一整段散文）：

```
产品 Type   →  Picture Book
 Extent     →  16
  Format    →  300mm x 220mm -  trimmed. 300mm(h) x 220mm(w)
   Preparation & Text
Text Paper: ApolloPape C1S Ivory Board (FSC) (GSM: 300). Text Print: 4x4 CMYK.
   Binding
Orientation: Portrait. Binding: Paperback. Bind Type: Unsewn. Binding Extra - Double spine. …
   Cover
Cover Colours: 4. Cover Ink Notes: CMYK. Cover Stock: 300gsm. Cover Paper: ApolloPape C1S Ivory Board (FSC).
   Cover Finishes
Inside Cover Printing - CMYK. Matt Lamination - On outer cover…
   Interiors
Fold Out - Inside pages are concertina (9 panels with first stuck down to inside cover). Sticker Sheet - …
   Fastenings
Wafer Seal - Paper wafer seal to affix sticker sheets to inside back cover.
```

数量写在开场白里：`for 1k, 2k, 3k, 4k, 5k, 7k, 10k copies?` —— 逗号分隔、每档带 `k`。

## 2. 字段怎么来的

| 字段 | 来源 | 说明 |
|------|------|------|
| `CPSL1..7` | 开场白的 `for 1k, 2k, …, 10k copies` | 逗号 + `k` 后缀逐档落；2026-11-17 修了 `10k copies` 这种「k + 量词」叠写（原来整条解析失败 → 十档全 0） |
| `TZS` | 开场白/正文 | 单据里没写款数就落 1（这家的询价基本是单款） |
| `CD`/`KD` | `Format` 段 | `300mm x 220mm - trimmed. 300mm(h) x 220mm(w)` 是**两组**尺寸写一起 → 引擎取第一组，GD 保持 0（别让 220 跑到 GD） |
| `GD` | 不做推算 | `thickness.enabled: false`（新客户，无语料可证） |
| `ZDFS` | `Binding` 段 | `Paperback` / `Unsewn` → 胶装/PAD书；`Hardback` → 精装书；`concertina` 也归胶装（ERP 没有风琴折枚举） |
| `CPLB` | `rules.cplb.map` | paperback/unsewn/concertina → `23.01;无线胶装书`；hardback → `22.02;方脊精装书` |
| `BZFS`/`SHFS`/`TBLX` | **空** | 新客户没有语料 → 进 `question_answers` 问业务，别抄别家的「FOB 盐田」 |
| `CPMC` / `CPMS` | **agent 手写** | 引擎只给占位符 |

## 3. 坑

1. **别把 `Format` 段整句丢给尺寸解析**。这句里有两组 `A x B`，`parse_size` 按 `x`
   切会切出三段 → 第三段被塞进 `GD`（实测 GD=220）。引擎 2026-11-17 起会先捞
   **第一组** `A x B`（`300 x 220 mm`），看到日志里的「不是单一 A x B」就当正常。
2. **`Interiors` 段常常是产品的关键**（风琴折 9 面、贴纸袋 4 张、口袋位置）。
   这些字段 ERP 没有对应枚举，**必须写进 CPMS**（部件/工艺段），否则等于漏抄。
3. **外链 PDF**（Dropbox 的 Pocket / Book 样张）是附件形态的一部分，不是正文事实。
   CPMS 只写客户文字里说过的东西，链接里的内容要么下载来看，要么问客户。
4. **默认值全空是刻意的**。`BZFS`（包装）/`SHFS`（送货）/`TBLX`（台板）会让
   `question_answers` 冒几条 `Q_*`，那是「要业务填」的正常信号，不要为了清零去抄
   NGV/Priddy 的取值 —— 送货方式直接决定报价基数。
5. `Unsewn` 里含 `sewn`：档案里 `unsewn` 必须排在 `sewn` 之前，否则「不锁线」被判成「锁线」。
6. 这一家的产品经理常直接把英国客户的原始询价转发过来（发件人是我们同事），
   识别靠正文里的 `Nosy Crow` / `Preparation & Text` / `Fastenings` 等特征，
   以及正文里的客户域名。
7. **推送会缺 `customer`**：`erp_khzl: []` → `export_calls(customer="")`，
   `.erp-import.json` 里 `customer` 是空串（2026-11-17 实测 `Little Museum` 即如此）。
   真实推送前必须拿到 ERP 客户资料代码（问业务 / 在客户资料页查），别拿别家代码凑。
8. **`KHDH`/`KHDDH` 报价阶段恒空**：语料 18476 份 KHDH 有值 0 份 → 档案已声明
   `rules.required_empty: [KHDH, KHDDH]`，不再生成卡交付的 `Q_KHDH`。
   老产物里那条直接 `waived`（answer 写清「客户邮件无 PO/RFQ 号 + 语料依据」），**不编单号**。
9. **改完 CPMS 要重导 `.erp-import.json` 侧档**（`product_desc` 是生成时的快照，不会自动跟）——
   配方见总纲 `references/cpms-backhalf-cron.md` §2。

## 4. 交付检查清单

```bash
uv run ./scripts/main.py <客户文件或目录> --today YYYY-MM-DD
uv run ./scripts/erp_schema.py --check <产物>.json --customer nosycrow
uv run ./scripts/cpms_lint.py <产物>.json
uv run ./scripts/cpms_content_audit.py --cpms <产物>.json --source <客户原始文件>
```

- [ ] `CPSL1..n` = 开场白里那排数量（7 档就 7 档），`GD` = 0、`CD/KD` = 300/220
- [ ] `ZDFS` 不是「Unsewn → 车线书」这种反义错判
- [ ] `BZFS`/`SHFS`/`TBLX` 空着时，确认 `question_answers` 里有对应待确认项（别硬填）
- [ ] CPMS 写了 `Interiors`（风琴折 9 面 / 4 张贴纸 / 口袋位置）与 `Fastenings`（封口贴）
- [ ] `CPMC`/`CPMC_ZH` 是中文品名（agent 手写），`ECPMC` 照抄客户英文品名
      —— ⚠️ 本档案**没有** `rules.ecpmc.source_keys`，引擎产物里 `ECPMC` 是空的，
      交付时由 agent 手写补上（「引擎不产出」≠「这单不该有」；2026-11-17 实单
      `Little Museum` 就是这么补的）
- [ ] 这是新客户：**交付前确认业务要不要建档默认值**（语料为空，口径无从验证）
