---
name: customer-quote-ngv
description: NGV 客户报价需求提取（德英双语 enquiry 分区表单 → ERP 59 字段）。处理 NGV（德国/欧洲 Aldi、Circon 线）的报价需求、附件解析、口径核对时加载；总纲见 skill customer-quote-request。
metadata:
  openclaw:
    emoji: "🇩🇪"
    requires:
      bins:
        - uv
        - python3
---

# NGV 报价提取（子技能）

> **总纲：** skill `customer-quote-request` —— 架构 / 抽取引擎 / 邮件入口 / 字段总览 /
> **`CPMS` 必须 agent 手写** / 校验方法 / 新增客户 SOP。本文件只讲 **NGV 这一家**。
> **唯一真源是档案 `customers/ngv.yaml`** —— 改 NGV 的行为 = 改它，不改代码。
>
> ★ **开工前先读总纲 `references/business-alignment-and-scope.md`**（2026-10-19 三次纠偏的沉淀）：
> ① 业务给「真实已确认单据」时先问要哪一层 —— 默认是学 `CPMS` 写法，不是交全字段差异表；
> ② 「没有就是没有」= `CPMS` 严格模式（只写客户文件事实 + 产物留 `CPMS_来源对照.md`）；
> ③ 口径变更必须四处同步（档案 → 脚本 → 测试 → 文档），旧写法标「已作废」。
> 交付四道门命令也在这页。
> 改完必须重跑：`uv run pytest` + 本文件第 8 节的对照命令。

- 客户：NGV（德国/欧洲的 Aldi、Circon 线）
- 状态：`calibrated`（4 份真实邮件附件 + 随包权威 ERP 导出校准）
- 语料库：`customer-files/samples/ngv/`（客户原文，gitignored；含 `manifest.json` 已知差异表）
- 口径白名单：`customers/ngv.erp_diff.json`

---

## 1. 附件长什么样（这是 NGV 的全部难点）

**不是平铺键值对**，而是德英双语的 `enquiry` **分区表单**：一张工作表、`A1:K32`、
结构固定（4 份样本逐格一致）。用 igl 那套 `auto_pairs` 跑它只能抽出 4 个键，
而且值全错（校准前实测：`format` 抽成 `145`、`series_name` 抽成签字人 `Ebers, Lea`）。

```
        A（分区）              B（标签）              C 起（值）
  r1  enquiry/Anfrage      Peppa Pig Stickern…      ← B 列自身即值（合并格）
  r2  (继承)               offer deadline/…         2026-05-26
  r5  quantity/Auflage     0                        ← B 列自身即值
  r6  versions/Versionen   8 titles                 ← B 列自身即值
  r7  (继承)               TPS/Format               240 x 300 mm portrait
  r8  content/Inhalt       extent/Umfang            36 pp + Cover
  r9  (继承)               paper/Papier             120 gsm woodfree…
  r11 (ends)               paper/Papier             ← 环衬纸，常空
  r15 cover/Umschlag       paper/Papier             230 gsm art card C1S
  r18 (jacket)             paper/Papier             ← 护封纸，常空
  r22 finishing/Verarb.    Bindung                  perfect binding / 210 / mm side
  r24 specials/Sonstiges   <长文本>                 ← B 列自身即值
  r30 notes/Bemerkungen    <备注>                   ← B 列自身即值
  r31 (继承)               Ebers, Lea（联系人）      ← B 列自身即值
```

抽取走 **`section_pairs` + 档案 `sections:`**（10 个分区 / 43 个规范键 / 80 条别名）。

### 五个必须知道的坑（都已在档案里解决，换客户照抄思路）

1. **同一标签在不同分区含义不同。** `content` 下的 `paper` 是内文纸，
   `cover` 下的 `paper` 是封面纸 —— 只看 B 列字面必然串。
   → 解：`sections:` 每个分区各有 `entries`，标签只在**本分区内**匹配。
2. **同一分区里标签会重复。** `content` 下 `paper` 两次（内文纸 + 环衬纸）、
   `cover` 下 `paper`/`printing`/`finishing` 各两次（封面 + 护封）。
   → 解：**按行位置对齐** —— 分区从那行开始，第 i 行对应 `entries[i]`。
   （试过「按标签名 + 消费顺序」匹配：环衬/护封这些常年为空的行会让计数错位，
   实测把联系人写进了 `notes`。）
3. **A 列有干扰文字。** `ends` / `Vor-/Nachsatz` / `jacket` / `Schutzumschlag`
   不是分区，是行内子说明。→ 解：只认 `sections:` 里已声明的分区名。
4. **B 列自己可能是值。** 合并单元格（`B1:J1` / `B5:J5` / `B24:K24`…）里放的是产品名、
   数量、款数、specials、notes 正文。→ 解：entry 写 `{ section_value: <规范键> }`；
   `section_value: null` = 明确忽略（页脚那个语义未明的数字就这么吃掉）。
5. **声明的 `label` 同时是断言。** 表结构一变、行位置对不上 → 打 WARN
   「期望标签 X，实际是 Y」并记进 `unmatched_labels`，**宁可空着不猜**。
   不要为了让 WARN 消失去放宽标签匹配 —— 那正是静默取错值的入口。

> **`rules.scan`（不挂标签、只在某处出现一次的值）**：页脚 `PR-00093270`（我方编号）、
> 纸张行 `FSC Mix`、**文件名**里的「香港单号 CU26072301」、`210 mm side`（装订边）、
> `2.7 mm bookblock`（书芯厚度）。`fsc` 带 `unique: true`（见第 4 节）。

---

## 1.5 ★ 第二种形态：纯英文 enquiry 邮件（KiK 线，2026-10-19 实单 `CU26070802`）

> 实现细节、MIME 踩坑（inline 签名图、HTML 表格压平）、「正文即任务」契约、
> 以及文本模式两个**静默丢值** bug 的成因与修法，见
> `customer-quote-request/references/eml-reading.md`。这一节只留客户口径。

NGV 不只发表单附件。`Naumann & Göbel` 的 KiK 线发的是**纯英文邮件、没有附件**，
规格全在正文（有时只有 HTML 表格）：

```
This is a request for KiK:
Title: Ostermalblock
Quantity: 30.100cps (one title, one language)
Files to printer: ??.??.2026 (please advise)
ETD: Oct 23rd, 2026
TPS: 148 x 210 mm portrait
Extent: 64 pp + cover
Content: 100 gsm offset paper (FSC-Mix) / 1x1 col. euroscale
Cover: 230 gsm artcard C1S (FSC-Mix) / 4x0 col. euroscale dispersion varnish matt
Finished binding: softcover, perfect binding at the top, square back, shape die-cutting egg form
Packaging: NOT individually shrink-wrapped / 14 books per export carton
Delivery: FOB (Incoterms 2020)
```

**流程**（脚本已支持，不用手工复制正文）：

```bash
uv run ./scripts/ingest_email.py --eml "CU26070802 - price enquiry. Ostermalblock.eml"
# → 无附件 → 正文落成 CU26070802_price_enquiry._Ostermalblock.txt + sidecar
uv run ./scripts/main.py customer-files/pending/CU26070802_price_enquiry._Ostermalblock.txt --customer auto
```

本单确认值（`ingest_email` + `main.py` 实跑）：

| 字段 | 值 | 依据 |
|---|---|---|
| `CPLB` / `ZDFS` | `23.06;PAD头书(全封面)` / `胶装/PAD书` | `perfect binding at the top` |
| `CPSL1` | `30100` | `30.100cps`（德式千分位） |
| `CD`/`KD`/`GD` | `148` / `210` / `0` | `TPS: 148 x 210 mm portrait` |
| `TZS` / `YYS` | `1` / `0` | `one title, one language`（单语言留空） |
| `DWMC` | `本` | 档案口径 |
| `KHDDH` | `CU26070802` | ★ 只在 **`.eml` 文件名**里，表内/正文都没有 |
| `FSCMC` | **空** | 纸张写 `FSC-Mix`，同时又接受任意比例 `FSC Recycled` → 不唯一，按口径留空 |
| `JHRQ` / `WCRQ` | 空 | 档案口径（ETD 只进 CPMS 备注，不落库） |

**注意**：

- 邮件里的 `ETD`（Oct 23rd, 2026）、`Files to printer: ??.??.2026`、`FOB (Incoterms 2020)`
  → 进 **CPMS 备注**，不落到 `JHRQ`/`WCRQ`（档案 `dates.*.mode: empty`）。
- 识别靠**发件人域名 + 正文特征**，不靠文件名：`vemag-medien.de` / `naumann-goebel.de`，
  正文 `price enquiry`、`Finished binding`、`euroscale`（该单得分 530）。
- `Aldi` 是 Aldi 线的品牌前缀；**KiK 单不能套 `Aldi`** —— `CPMC` 的品牌段需要业务确认，
  脚本当前模板仍带 `Aldi`，交付前必须人工核对（本单已去掉）。

## 1.6 ★ 一份报价可能不止一个文件（输入单元，2026-10-20）

业务口径：**单个文件 = 独一份报价；文件夹 / 压缩包里的文件 = 这一份报价的全部参考文件**。
所以 NGV 常见形态「规格 enquiry + 确认单 + 补充说明」要**合成一份**，不是三份：

```bash
# 文件夹 / zip / 多附件邮件投出来的单元目录，都直接当「一份报价」跑
uv run ./scripts/main.py "customer-files/pending/<单号> 询价包/" --customer ngv
uv run ./scripts/main.py customer-files/pending/询价包.zip --customer ngv
```

实现：`scripts/input_unit.py`（单元识别 + zip 安全解压）+ `extract_fields.merge_customer_data`
（主文件优先 / 冲突记账 / `rules.scan` 整单元重跑 / 文本块按 `# 文件：` 分段）。
细节与已知边界见总纲 `references/input-unit-multifile-quotes.md`。

**NGV 场景的三个必看项**：
1. `validation.json` 的 `unit.members` —— 清单对不对（`__MACOSX`、`~$` 临时文件应进 `skipped`）；
2. `conflicts` —— 非空说明两份文件给了不同值（典型：`quantity` 规格表 30.100 vs 补充 28.000），
   **交付前必须人工核对**，并让 `CPMS_来源对照.md` 写清用了哪份文件的哪个值；
3. `sources` —— 每个规范键都要指到具体文件；`# 文件：xxx` 段就是写 CPMS 时的溯源锚点。

> ⚠️ 别把同一封邮件的多个附件**分开跑**（那会变成 N 份报价、N 个 ERP JSON）。
> `ingest_email.py` 遇到 ≥2 个附件会自动投成 `pending/<标题>_<时间>/` + 一份 `_email.meta.json`，
> 直接对目录跑即可；只有 1 个附件时才是单文件 + `<附件>.meta.json`。

**反过来：一个文件夹里有子目录 = 多份报价（1.0.38）**。客户把两张单放在
`两张单/CU26070001/`、`两张单/CU26070002/` 里时，直接对**父文件夹**跑，脚本会
把子目录各算一份、出一一对应的 N 个 ERP JSON（一条命令，逐份失败隔离）：

```bash
uv run ./scripts/main.py "customer-files/pending/两张单/" --customer ngv
```

判据只有一条：**看有没有子目录**。有 → 拆；没有 → 整包一份。
拆出来的每份在 `validation.json` 的 `unit.role = "subdir"`、`unit.origin` 指回父目录；
压缩包拆多份时原包只随第 1 份归档（`archive_owner`），其余份记 `source_archive`（包名）。

## 1.7 ★ 与业务员 ERP 记录对账：NGV 已知的三个坑（2026-10-20）

业务员把同一单手填进 ERP 的记录（本次 `BJXQ260708007` / `CU26070802`）是最好的参照，
但 NGV 这条线有三个**已经定位过**的坑，再对账时会立刻遇到，别重新推导：

1. **`CPLB` 的「PAD头书」是脚本推不出来的。** `customers/ngv.yaml` 里
   `perfect\s*binding → 23.06` 是从**单份**样本（`CU26072302`）反查来的，而那份原文
   **没有 `PAD` 字样** → 该值属人工结构判断。于是「冲形状无线胶装」会被误判成 PAD头书。
   树自己的 `paths:` 第 5 问写了正确优先级：**整书冲形状 → 23.03**。
   同时 `perfect binding **at the top**` 的 `top` 是**装订边位置**，不是 PAD 的「头」——
   术语误读。修法与连带代价见总纲
   `references/reconcile-with-salesperson-erp-record.md`。
2. **`CD/KD` 业务侧自己录得不一致。** 本次 `148x210 → CD=210,KD=148`（长在前），
   历史导出 `240x300 → CD=240,KD=300`、`145x210 → CD=145,KD=210`（TPS 第一段进 CD）。
   schema 标 `CD=长 / KD=宽`。**这是业务侧口径分歧，别自己挑一个改**——两派证据并列请森西拍板。
3. **`CPMS` 与 ERP 字段是两套口径。** 本次业务记录 `Drop_SJFX` 是空的，CPMS 却写
   `书籍方向：【短书脊】`。所以「`Drop_SJFX` 空 → CPMS 省略方向」是错的：
   竖版 + 装订在顶部 ⇒ 书脊在 148mm 短边，推得出来。
   另外装订段**不许照抄 `ZDFS` 的下拉值**（`胶装/PAD书`），要写客户原文的
   `冲形状无线胶装书（冲鸡蛋形状）`。
   ⚠️ 这两处 `cpms_lint` 都放过（0 error）——**lint 过 ≠ 文本对**。

对账纪律：`"" ≡ null` 归一后先读 `customers/ngv.erp_diff.json` 与
`customer-files/samples/ngv/manifest.json` 的 `known_differences`（8 项已拍板口径，
`CPDL/BZ/PXH/YYS/SFYPJ/Remark1/Remark3/FSCMC`），**先读白名单再动嘴**，否则整份报告会被读成「31 个 bug」。

## 1.8 ★ 第三种形态：BB 书 + 「Titel+Mengen」款量附表（2026-10-21 实单 `LG26090401` / A00142）

NGV 的 Wimmelbuch / BB 书项目常是**两附件一单元**，而两份文件承担的事完全不同：

| 文件 | 内容 | 结果 |
|---|---|---|
| `<尺寸>_<项目名>_PR-xxxxx_korr-CW.xlsx` | enquiry 分区表单（规格全在这里） | 抽到键最多 → 被选为**主文件** |
| `Titel+Mengen_<项目>_…xlsx` | 款数 / 语言 / 各市场数量（德文表头 `Titel 1..10`、`QTY per language`） | **一个规范键都不匹配** |

后果（实测）：`versions` 只抽到表单 B6 的 `see detailed breakdown` → 正则失配 →
**`TZS=1`（默认）、`YYS=0`（`when_missing`）、`CPSL1=0`**；而邮件正文明写
`10 titles in 10 languages`、款量表 `U227=410,000`（10 款 × 41,000）。
四道门**全绿** —— 因为这三个字段「有值」，只是值是默认值。

**开工必做**：单元里出现 `Titel+Mengen` / `Mengen` / 带 `Titel 1..10` 行的表 →
手算一遍 `TZS` / `YYS` / `CPSL1`（款数 × 每款 = 总数；德式点千分位 `410.000`），
与产物对不上按「字段 / 现值 / 原文 / 应然值」汇报，**不手改 JSON**（见 §9 定时任务那条）。

**顺带两个 NGV 结构缺口**（同日实单暴露，档案侧待补）：

- ★ **`board book` 没有映射**：`customers/ngv.yaml` 的 `cplb.map` 与 `binding.map`
  都不认 `board book` / `board book binding` → `CPLB=32.00;其他`、`ZDFS=""`，
  而 `erp_schema --check` 只报 2 条 **warn**（顶层过粗 / 装订空），不报 error。
  应然值：**`CPLB=27.01;普通BB书`、`ZDFS=普通BB书`**（`普通BB书` 在 ERP 装订枚举里）。
- ★ **表单模板会被跨项目沿用，B1 标题可能是上一个项目的**：
  A00142 的 B1 写 `Wimmelbuch Peppa Pig`（那是 PR-00092497 的旧模板），实际项目是
  `A00142 Wimmelbücher (Winter)`；B30 的 `A+B assorted` 与邮件「only spec A」冲突；
  G4 `ETD = 25 Dec 2027` 与邮件 `13 Aug 2027` 冲突。
  判据：**B1 标题 vs 邮件主题 / 款量表项目名**。处理：`CPMC`/`ECPMC` 取邮件 + 款量表的
  项目名（`Wimmelbücher (Winter)`，照抄不翻译）；这些残留**一律不写进 CPMS**，
  在 `CPMS_来源对照.md` 的「已删除/未写」表和汇报里点名。
- **多文件单元的 `cpms_content_audit`**：`--source` 只能指一份 → 用**承载规格的主表单**
  做门禁；另一份文件独有的事实（款量表的 PR 号 / 日期）会被判无出处，那是**预期**，
  写进来源对照即可。邮件独有的事实同理：初稿把 `CRD 06 Aug 2027、ETD 13 Aug 2027`
  写进备注，被 audit 判 `06`/`13` 无出处 → 删掉，日期留给业务/ERP 侧。

> 本单完整复盘（两份文件的原始数据、5 个字段落错、CPMS 成品与取舍）：
> `references/bb书与款量附表-a00142.md`。

## 2. 怎么认出这是 NGV

- **文件名关键词**：`ngv`
- **正文特征标签**（`min_signature_hits: 2`）：`enquiry`、`Angebotsfrist`、
  `VK-Verpackung`、`Sortierung/VE`、`Schutzumschlag`
- **工作表名**：`enquiry`

> ⚠️ **识别必须看正文**（2026-09-24 修的真 bug）：有份附件名 `160x104_Colouring Cards
> Aldi Süd…xlsx` 里含 `Colour`，命中 igl 的 `filename_keywords`（10 分）就胜出，
> 而正文里 NGV 的 500 分特征**压根没被读过** —— 结果整单全错（`CPLB` 落到兜底
> `32.00;其他`、`erp_fixed` 一个没上、日期按公共默认算了「今天+10 天」）。
> 现在表格类文件**一律把正文一起送进识别**，文件名只当加分项；得分 <30 会打 WARN。
> 回归：`test_识别必须看正文_文件名撞车也不许判错`。

---

## 3. 抽取结果：4 份真实附件的确认值

| 单号 | `CPLB` | `CPSL1` | `CD×KD` | `Drop_SJFX` | `DWMC` | `YYS` |
|------|--------|---------|---------|-------------|--------|-------|
| CU26051601-01 | `24.03;贴纸骑钉书` | 0 | 240×300 | 长书脊 | 本 | 0（附件没写语言数） |
| CU26061701 | `29.04;其它套装` | 512580 | 0×0（套装） | （空） | 套 | 5 |
| CU26072301 | `29.04;其它套装` | 70000 | 0×0（套装） | （空） | 套 | （空，单语言） |
| CU26072302 | `23.06;PAD头书(全封面)` | 70000 | 145×210 | 长书脊 | 本 | （空，单语言） |

### 3.1 第 5 单实测（2026-09-24，PR-00091345，不在语料库里）

`210x280_BastelnGestalten_Diamantsticker_Nageldesign_PR-00091345_20260612_CW.xlsx`
（文件名带 `_CW`，**没有**「香港单号 CU…」）→ 识别为 `ngv`（得分 400）后的确认值：

| 字段 | 值 | 说明 |
|------|-----|------|
| `CPLB` | `26.07;普通YO书` | ★ **2026-10-19 用真实 ERP 导出（`BJXQ260704002`）反查更正**：`Wire-O Binding` 是**双线圈 YO** → 26.07；旧实现把 `spiral|wire-o|线圈` 塞进同一条正则落 26.01「普通蛇圈书」（单线圈）是错的 |
| `ZDFS` | `线圈书` | ★ 本次新增映射（REP 合法值里叫「线圈书」，不是「蛇圈书」） |
| `CPSL1` | `21000` | 数量写法 `3 x 7000 cps. = 21.000 cps` —— ★ 见第 9 节「带 = 总数的写法」 |
| `CD`/`KD`/`GD` | `210` / `280` / `0` | 210 × 280 mm portrait；`GD` 恒 0（`thickness.enabled: false`） |
| `Drop_SJFX` | `长书脊` | 附件写 `280 mm side` = 较长边 |
| `TZS` / `YYS` | `3` / `（空）` | `3 titles, 1 language` → 单语言留空 |
| `FSCMC` | `FSC Mix` | 内文/封面/贴纸四处都标 `FSC Mix`（**同一个值**，故不算「不唯一」） |
| `KHDDH` | `（空）` | 附件与文件名里都没有「香港单号 CU…」→ **留空是预期结果**（业务 2026-09-24：默认就是空），需要时由邮件补 |
| `CPSX` | `null`（JSON null） | 附件没有年龄段/安全检查栏 → 档案给「无」→ 按业务口径（`rules.null_values`）落 **JSON null**，不是字符串「无」 |
| `offer_deadline` | `2026-06-26` | ★ 「offer deadline / Angebotsfrist」那行**有值** —— 该单已**过期 90 天**（基准日 2026-09-24）→ 触发提醒 |
| `CPMC` | `KM/Aldi Basteln&Gestalten Diamantsticker Nageldesign(FSC Mix)` | **英文表达**（1.0.22 起）；业务员 `KM` 由脚本带出 |
| `CPMC_ZH` | `KM/Aldi 钻石贴纸美甲设计 普通YO书/Basteln&Gestalten Diamantsticker Nageldesign(FSC Mix)` | 内部字段：**中文表达**；中文品名由 agent 补。⚠️ 真实单据里业务员是 `Corinna`、且**不带 `Aldi`**、还带类别与附属件 —— 见 3.2 |
| `ID` | 本次任务号 | 内部字段：= 产物目录名 = 日志里的「任务ID」 |
| `Reminder` | `报价截止日期 2026-06-26 已过期 90 天（基准日 2026-09-24） —— 请与业务确认这单是否还要报价` | 内部字段：把提醒固化进产物（1.0.23 起） |
| `version` | `2` | 内部字段：当前配置的版本（1.0.24 起）；来源 = 档案顶层 `version:`，改口径就 +1 |

本单还暴露了六个问题，均已修复：数量解析（第 9 节）、Wire-O 装订映射、`extras_found`
被纯标签污染、**`offer deadline` 被误当纯标题导致值被静默丢掉**、**`CPMC` 里中英混排**、
**提醒只留在日志里没进产物** —— 见 `SKILL.md` 1.0.20 / 1.0.21 / 1.0.22 / 1.0.23。

`ZDFS`：`骑马钉` / `套装` / `胶装/PAD书` / `线圈书` —— 看 `binding`（Softcover / ridged box）
**和** `Bindung`（safety saddle-stitched / perfect binding / lose）两行才够，
所以档案里 `binding.match_keys: [binding, binding_instructions]`。
`Drop_SJFX` 走 `spine.mode: binding_side`（附件给 `<N> mm side` 与较长边比较），
套装经 `spine.skip_tops: [套装]` 直接留空。

### 3.2 真实 ERP 回灌（2026-10-19，业务给了 `BJXQ260704002` 的完整 59 键）

> ★ **业务给真实单据时的正确姿势**（2026-10-19 被纠正过两次）：
>
> 1. **先确认它是用来教哪一块的** —— 原话是「给真实单据的目的是让你聚集到 CPMS 的编写」。
>    默认假设是「学交付物怎么写」，**不是**「逐字段对账」。别一上来就甩 59 字段差异表。
> 2. 确实要对账时才给，且分三层：**真错**（映射/规则写错 → 改代码 + 加测试）/
>    **口径差**（业务当初拍板 vs 真实单据 → 列出让业务定，不许自行改）/ **格式差**（忽略）。
> 3. **真实单据的内容不许搬进交付物** —— 它是范本，不是数据源。业务原话：「没有就是没有」。
>    `CPMS` 只写客户文件里的事实（严格模式见 9.5）。

业务直接甩来同一单的**真实 ERP JSON** 让对比 —— 这是最省钱的校准方式，**每次拿到都必须做**。
完整逐字段差异表 + 处置状态：`references/real-erp-diff-BJXQ260704002.md`。

四类差异，**不要混着报**：

| 类 | 含义 | 例子（本单） |
|---|---|---|
| ① 真错 | 档案/规则错，必须改代码 | `CPLB` 26.01 → **26.07;普通YO书**（Wire-O 是双线圈） |
| ② 口径 | 我们按拍的板填、真实单据相反，**要业务再拍** | `CPMC` 业务员 KM vs **Corinna**；`SFYPJ` 空 vs **是**；`Remark1` 空 vs **自厂购**；`Remark3` 空 vs **新款报价**；`PXH` 空 vs **1**；`DWMC`/`FSCMC`/`Drop_SJFX` 我们填、真实空；`CPSL1` 21000 vs **真实全空** |
| ③ 没抽 | 内部/ERP 侧字段，脚本按「不该碰」留空 | `BDBH=BJXQ260704002`、`YWY=颜娟`、`KHZL=NGVS16`、`GDY=肖也` |
| ④ 格式 | 客户明说「格式问题忽略」 | `CPLB` 无编号前缀、CPMC 双空格、导出叫 `KHCPMC` 而模板叫 `OLD_KHDDH` |

⚠️ **`CPMC` 的 `Aldi` 是模板写死的，NGV 不只做 Aldi**：本单是 **TCHIBO**
（外箱 YJF Jupiter / TCHIBO 封箱要求），真实 CPMC 是
`Corinna/普通YO书+钻石贴纸 <原文>  (FSC Mix)` —— 无 `Aldi`、带类别名+附属件、业务员按单变。
改模板前先确认这三件事，别默认 Aldi+KM。

---

## 4. 口径（业务已拍板 —— 四批，共 22 条）

### 4.1 第一批（2026-09-24，第二批权威导出）

| 口径 | 决定 | 依据 |
| --- | --- | --- |
| 套装（盒子）尺寸 | **`CD/KD/GD` 一律 `0/0/0`**（`rules.size.zero_tops: [套装]`） | 附件给的是盒内单件 TPS，不采用 |
| 交货日期 `JHRQ` | **不落库**（`dates.JHRQ.mode: empty`） | 4 份导出全空，附件给了 ETA 也不填 |
| 预计下单日期 `WCRQ` | **不落库**（`dates.WCRQ.mode: empty`） | 4 份导出全空；不用「今天+10 天」 |
| 纸板类型 `ZBLX` | **留空** | 导出里 3 份 `B3B普通芯` 1 份空 → ERP/业务侧填；业务给的完整下拉枚举里没有 `B3B普通芯` |
| 单语言 `YYS` | **留空**（`versions.empty_when_single: true`） | 写 `1 language` 的两单导出 YYS 都是空的；写 `5 languages` 的才是 5 |
| 必填豁免 | `rules.required_empty: [KHDH, Drop_SJFX]` | KHDH 报价阶段取不到、套装没有书脊方向；不豁免会淹掉真 error |

### 4.2 第二批（2026-09-24，业务逐条拍板）

| 口径 | 决定 |
| --- | --- |
| `CPLB` 写法 | **输出「编号;名称」**（如 `29.04;其它套装`）—— 编号让用户更直观看到类别标识 |
| `BZ` / `CPDL` | **我们填**：`美元` / `书版`（导出留空的部分由业务核对时补） |
| `PXH` 排序号 | **显式留空**（`PXH: ""`）—— 模板默认 `1` 必须靠它清掉 |
| `YYS` 提取不到 | **落 `0`**（`versions.when_missing: 0`）—— 邮件可能后补而附件未更新，0 = 待业务核对 |
| `SFYPJ` / `Remark1` 配件 | **推不出就留空**（删掉了写死的「否」—— CU26072301 确实有配件） |
| `FSCMC` | **不唯一就留空**（`rules.scan` 的 `fsc` 设 `unique: true`） |
| `Remark3` 修改记录 | **留空** —— 我们产出的就是第一版报价，不存在修改记录 |
| `YSLX` 运输类型 | **默认留空**；长期客户的稳定口径写进档案偏好表 |
| `CPSL2`~`CPSL10`、`KHMBDJ` | **落 `0` 可接受**（未使用档位 / 客户目标价） |
| 「产品备注」 | 先不需要（ERP 导出侧多出的键） |

### 4.3 客户偏好表（`preferences:`，15 条）

业务要的「喜爱表」：一眼看全该客户哪些字段固定填什么、哪些就该空着。机制见总纲 2.7。
查看：`uv run ./scripts/customer_profile.py --prefs ngv`。
**它和 `erp_fixed` 必须一致**（`--validate` + 测试会拦）—— 想留空就得在 `erp_fixed` 写 `""`。

### 4.4 第三批（2026-09-24 晚，业务原话 → 落地）★ 1.0.21

| 业务原话 | 决定 | 落点 |
| --- | --- | --- |
| 「业务员使用 **KM**」 | ~~`CPMC` 的第一段固定写 `KM`~~ **⚠️ 已被第 6 节取代（2026-10-19）**：`CPMC` 只放产品名称，`KM` 落 `YWY` | ~~`cpmc.template` 前缀~~ → 见第 6 节 |
| 「`KHDH`、`KHDDH` 香港单号默认是空」 | `KHDDH` 扫不到就是空，**不算缺陷**（不再报假 error）；扫到「香港单号 CU…」照常落库 | `rules.required_empty: [KHDH, KHDDH, Drop_SJFX]` |
| 「`CPSX` 如果没有获取到就显示**无**」 | 推不出年龄段 → 落「无」；**同日补充**：「『无』使用 json 里的 null 填充」→ 产物里是 **JSON `null`** | `rules.cpsx.when_missing: 无` + 全局 `rules.null_values: ["无"]`（null 属空值，不触发枚举校验 → 1.0.21 的 `enum_exceptions` 声明已删） |
| 「超过了报价截止日期，你记得提醒」 | 跑完在 stdout 末尾醒目提醒 + `validation.json` 的 `reminders` 段 + **内部字段 `Reminder`** | 引擎级 `check_offer_deadline()`（取 `offer_deadline` 规范键）+ `--today` 基准日期 |

### 4.5 第四批（2026-10-20，实单 `CU26070802` / Ostermalblock）

| 业务原话 | 决定 | 落点 |
| --- | --- | --- |
| 「`Ostermalblock` 译**复活节彩蛋涂色本**」 | 中文品名照此确认（存档备查，下次同类产品别另译一套） | 本节 |
| 「IGL 的 CPMC **不需要更改**」 | IGL 继续用「类别;系列名」旧写法，**不做**迁移 —— 改它会动 IGL 端到端基线 | IGL 档案不动（基线保持原样） |
| 「装订方向，如果我们识别不到则设置为**书脊方向为 Null**」 | ① 文件写了方向（`at the top` / `at the head` / `装订在顶部`）→ CPMS 照抄（`装订在头部`）；② **识别不到 → CPMS 不写方向词**；③ ERP `Drop_SJFX` 有 `N mm side` 才判长/短，**没有就留空（JSON `null`）** + WARN —— **不许**落「长书脊」当默认值，也不许由「装订在顶部」反推长/短 | `assets/cpms_spec.yaml`（装订段 notes + `meta.business_decisions`）、`docs/CPMS_写作规范.md` 3.3、`customers/ngv.yaml` `rules.spine.default: ""`、`scripts/main.py` 第 8 步日志、新增 `rules.scan: binding_direction`（只进 CPMS，不映射 ERP） |

> **为什么方向不能反推书脊**：`at the top` 说的是**装订边位置**（顶部/左侧…），
> `Drop_SJFX` 说的是**书脊在长边还是短边**，两个维度正交。
> 实单 `CU26070802` 就是 `perfect binding at the top` + 无 `N mm side` → `Drop_SJFX` **留空（null）**。

> 🔁 **复跑验证必读**（总纲 `references/复跑验证与默认值陷阱.md`）：改完档案要证明没弄坏已交付的单时，
> 三条会**静默出错**的操作 —— ① 只拷 `.txt` 不带 `.txt.meta.json` → 客户档案掉回 `generic`、
> 字段全空但 `校验通过` 照样打印；② `-o` 不决定产物落点，产物在 `<processed-dir>/<task_id>/`；
> ③ 源文件跑一次就被搬走。纪律：`profile.rule()` 第二个参数**不许**写业务默认值（写 `""`），
> 复跑 vs 交付的 diff 只允许 `ID/version/CPMC/CPMC_ZH/CPMS` 五项。

> ✅ **`CPSX` 已无待确认项**：落 `null`（= 空值）与 4 份权威导出完全一致，
> 导入端按空值处理，不需要业务再拍板「无 vs 其他」。
> 唯一遗留是**内部字段**（`ID`/`CPMC_ZH`/`Reminder`/`version`）导入端收不收 —— 若报「未知字段」，
> 用 `--strip-internal` 出纯净 59 键。


---

## 5. 单号：`KHDH` 与 `KHDDH` 必须分开

两个键**只差一个字母**，此前一直混用：附件文件名里的「香港单号 CU26051601-01」
被写进了 `KHDH`（客户单号），而模板第 43 键 `KHDDH`（中文标签就是「香港单号」）**永远是空的**。
4 份权威 ERP 导出给出答案：

```
KHDDH = CU26051601-01 / CU26061701 / CU26072301 / CU26072302   ← 香港单号（进 KHDDH）
KHDH  = 全空                                                    ← 客户单号（终端客户 PO，报价阶段还没有）
```

档案因此按 **`target`** 声明两组规则：

```yaml
email:
  khdh:  { target: KHDH,  attachment_keys: [],      subject_patterns: … }  # 只可能来自邮件
  khddh: { target: KHDDH, attachment_keys: [khddh], subject_patterns: … }  # 附件「香港单号 CU…」
```

- 优先级（每组独立）：邮件标题/正文（sidecar 的 `numbers`）→ 附件列 → 全表扫描的规范键。
- ⚠️ **CU26061701 的附件文件名与表内都没有单号** —— 只能从邮件标题/正文取。
  这就是 **IMAP 直连非做不可**的理由；在此之前该单 `KHDDH` 会留空。
- ★ **业务 2026-09-24：「`KHDH`、`KHDDH` 香港单号默认是空」** —— 扫不到就留空是**预期结果**，
  不是缺陷；已进 `rules.required_empty`，不再因此报假 error（`--check --customer ngv` → 0 error）。
  附件里带「香港单号 CU…」/ 邮件里有的，照常落库。
- ⚠️ **香港单号前缀不只 `CU`**：真实单据 `BJXQ260704002` 的 `KHDDH = YJ26070401`。
  档案里 `[A-Z]{2}\d{6,}` 本来就吃得下 `YJ`，真正的问题是**附件里根本没写**
  （文件名只有 `PR-00091345`）→ 只能从邮件取，`KHDDH` 会留空。别因为「前缀不是 CU」就判规则失效。
- ⚠️ **写单号正则要严**：`\b(?:po|order)\s*(?:no)?\s*([A-Z0-9]{5,})` 会把
  `Postkarten in Box` 的 `Po` 抓成 `stkarten` —— 必须要求出现 `no/nr/number/#` 这类编号词。
  有测试钉着（`test_客户单号正则不许被单词前缀误捕`）。

---

## 6. `CPMC` = 产品名称（中文译名）；业务员 `KM` 落 `YWY`

**业务 2026-10-19 更正（实单 `CU26070802`）**：

> 「`CPMC` 是这个产品的名称，你应该从文件中翻译出来；而 `KM` 也就是你，是你的名字，
> 他应该填写在 `YWY`。」

所以：

| 字段 | 内容 | 谁填 |
|---|---|---|
| **`CPMC`** | **产品名称**（从客户文件译成中文），不带业务员/品牌/类别/FSC | agent（脚本写 `【中文品名待定】` 占位） |
| **`ECPMC`** | **产品英文名**（客户原文 `Title: Ostermalblock` → `Ostermalblock`，照抄不翻译） | 脚本（`rules.ecpmc.source_keys: [product_name, series_name]`） |
| **`CPMC_ZH`** | 与 `CPMC` 一致（内部字段，保持两边同值） | agent |
| **`YWY`** | 业务员代号 `KM` | 脚本（`erp_fixed.YWY: KM`） |

档案配置：

```yaml
erp_fixed:
  YWY: KM                     # 业务员，不是 CPMC 前缀
rules:
  cpmc:
    salesperson: KM           # 仅留档说明，不再参与拼装
    allow_chinese: true       # CPMC 允许中文（产品名的中文译名）
    template: "【中文品名待定】"
    zh_template: "【中文品名待定】"
    needs_agent: true
```

- **产品名只能译，不能编**：`Title: Ostermalblock` → `复活节彩蛋涂色本`（Ostern 复活节 + Malblock 涂色本）。
  译名不确定就写来源 + 存疑（`备注`/交付说明），不许硬凑品类词。
- **`KM` 不是业务员姓名，是「我」的代号** → 只进 `YWY`；`CPMC` 里出现 `KM/Aldi` 是旧口径，已废。
- 品牌（Aldi / KiK / Tchibo）**各自有归属**：`KiK` 这类信息写在 `CPMS` 备注，不往 `CPMC` 里塞；
  Aldi 线历史导出里的 `Aldi` 前缀属于旧写法，不要照搬。
- `allow_chinese: true` 的客户，`CPMC` 出现中文**不再报警**；没声明的客户仍会 WARN
  （`test_决策_未开allow_chinese的客户_CPMC中文要报警` 钉住）。
- `CPMC_ZH` 是**内部字段**（`assets/internal_export_fields.yaml`），追加在 59 键之后；
  导入 ERP 前若被告知「不收未知字段」，用 `--strip-internal` 只输出 59 键。

> 历史写法（1.0.21–1.0.33）是 `<业务员>/Aldi <中文品名> <类别>/<原文>(<FSC>)`，
> 真实导出 `BJXQ260704002` 是 `Corinna/普通YO书+钻石贴纸 <原文>  (FSC Mix)` ——
> **这些都已作废**，只用于理解老单据，不要拿来拼新单。

---

## 7. `Remark4` 客户常量：必须逐字照抄

NGV 每单固定的第三方检测清单（378 字符，3 份导出逐字相同）。照抄时注意 ERP 原文里的
**既有错别字与格式**：`monochloroo-` 是双 o、`(MCPD).Colour` 之间没有空格、
`foam` 前有一处换行。档案里用 `|-`（保留内部换行）而不是 `>-`。
「顺手改成正确拼写」会让每单都多出一条无意义差异 —— 踩过。

---

## 8. 回归与核对命令

> ★ **改完档案/脚本，先跑这条再交付**：`scripts/recheck_order.py`（本技能自带）——
> 重新投递 + 重新映射一单，并与已交付 JSON 逐字段比对，只允许
> `ID/version/CPMC/CPMC_ZH/CPMS` 不同。它把「副档 `.meta.json` 必须一起带」
> 「`-o` 不决定落点」「源文件会被搬走」三个坑都处理好了。
> ```bash
> uv run python ~/.hermes/profiles/igl_agent/skills/customer-quote-ngv/scripts/recheck_order.py \
>     --eml "customer-files/processed/<task>/<原邮件>.eml" \
>     --delivered "customer-files/processed/<task>/ERP_*.json" --today 2026-10-20
> ```
>
> CPMS 写法样例：`references/cpms-线圈书水钻贴纸.md`（线圈书 + 水钻贴纸 + 多材料内文，
> v2 规范 0 error/0 warn，含逐条写法决策表）。
> 定时任务补写的门禁坑（德式千分位 `60.000` / 料号撞「印色」正则 / 并发写回纪律）：
> `references/cpms-backfill-gotchas.md`。
> 真实 ERP 逐字段差异表：`references/real-erp-diff-BJXQ260704002.md`（Wire-O 类别、CPMC 业务员、
> CPSL 空值等 8 条口径分歧的来源单据）。
> BB 书 + 款量表（`Titel+Mengen`）单元的复盘、5 个字段落错、CPMS 成品与取舍：
> `references/bb书与款量附表-a00142.md`（§1.8 的配套案例）。

> ⚙️ **执行环境**：本 profile 的会话里终端默认 cwd 可能指向一个已不存在的目录
> （命令会直接报 `cd: … No such file or directory`，连 `pwd` 都跑不了）。
> 每条命令都显式带 `workdir=/home/claw/customer-quote-request`（或命令里先绝对路径 `cd`），
> 不要依赖会话默认目录。

```bash
cd /home/claw/customer-quote-request

# 单份处理（客户自动识别，或 --customer ngv 显式指定）
uv run ./scripts/main.py "customer-files/pending/<附件>.xlsx" --customer ngv

# 全量测试（含 NGV 合成用例 + 真实样本回归）
uv run pytest -q                              # 当前 243 项

# 按档案豁免校验产物（推荐 —— 不传 --customer 会误报该客户「本来就空」的字段）
uv run ./scripts/erp_schema.py --check <ERP.json> --customer ngv

# 补跑/回归时可固定「今天」，让过期判据可复现
uv run ./scripts/main.py "<附件>.xlsx" --customer ngv --today 2026-09-24

# ★ 与权威 ERP 导出逐字段分类对照（改口径后必跑）
uv run ./scripts/compare_with_erp.py --customer ngv \
    --samples-dir customer-files/samples/ngv \
    --accepted customers/ngv.erp_diff.json \
    --map CU26061701=Colouring
# 期望输出：白名单外差异 0 条（退出码 0）

# 只看抽取结果（调试用）
uv run ./scripts/extract_fields.py "customer-files/pending/<附件>.xlsx" --customer ngv --json

# 档案 / 偏好表
uv run ./scripts/customer_profile.py --show ngv
uv run ./scripts/customer_profile.py --prefs ngv
```

**回归基线**（`tests/test_ngv_profile.py` 逐条钉住）：4 单 59 键顺序断言通过、
`CPLB` 24.03 / 29.04 / 29.04 / 23.06、`CPSL1` 0 / 512580 / 70000 / 70000、
套装尺寸 0/0/0、`DWMC` 本/套/套/本、`offer_deadline` 4 单逐条对齐
（2026-05-26 / 2026-06-17 / 2026-07-27 / 2026-07-27）。
校验 0 error（1 warn = `CPSX=无` 的口径例外）。

---

## 9. 坑（踩过的）

- **别用 `auto_pairs` 抽 NGV**（档案里 `layout.strategies` 只有 `section_pairs`）。
- **别按标签名匹配**，要按行位置对齐（坑 2 的教训）。
- **别拿 `our_ref`（`PR-00081126`）冒充客户单号** —— 那是我们的询价编号。
- **别把香港单号写进 `KHDH`**，进 `KHDDH`。
- **别给 `ZBLX` 填值**、**别给套装配尺寸**、**别给 `JHRQ`/`WCRQ` 填日期**。
- **`FSCMC` 多材质混用必须留空** —— 取第一个命中是错的（盒底 Recycled / 盒盖 Mix /
  竹底座 100%，ERP 留空）。
- ★ **数量的「带 = 总数」写法**（2026-09-24 踩过）：`3 x 7000 cps. = 21.000 cps` ——
  旧实现只认整串 `^N x M$`，匹配失败后退回「取第一个数字」，**静默落成 3**（真值 21000）。
  现在按「① `=` 后的总数 → ② `N x M` 乘法 → ③ 全串只有一个数字」解析，两者不一致会 WARN
  并以总数为准。数字写法的坑一律属于「错了整单报价都错」，看到数量先核。
- ★ **装订写 `Wire-O` 时 `ZDFS` 是「线圈书」**，而 **`CPLB` 是 `26.07;普通YO书`**
  （Wire-O = 双线圈 YO；`26.01;普通蛇圈书` 是**单线圈**，两者是不同树节点 ——
  2026-10-19 真实导出反查更正过，之前统一落 26.01 是错的）。档案 `cplb.map` 已拆成
  `wire[- ]?o|wiro|YO书|双线圈 → 26.07` 与 `spiral|蛇圈|单线圈|线圈 → 26.01` 两条，
  **别合并回去**。测试 `test_决策_CPMC存英文表达_CPMC_ZH存中文表达` 钉住了这条。
- **`KHDDH` 取不到不等于出错**：文件名与表内都没有「香港单号 CU…」时（实测 PR-00091345 就是这种）
  留空是**业务认的正常状态**（2026-09-24 拍板），已进 `required_empty`，不报 error；
  绝不能拿 `our_ref`（`PR-…`）冒充，需要时由邮件补。
- ★ **有值的标签被误当「纯标签」→ 值被静默丢掉**（2026-09-24 踩过）：
  `offer deadline / Angebotsfrist` 那行 C 列**有日期**（5/5 份样本都有），
  一度被我登记进 `ignored_labels` → `extracted.json` 里根本没有 `offer_deadline`。
  **教训**：往 `ignored_labels` 加东西前，先确认那一行的**右侧/下方真的没有值**；
  判断依据是 `extracted.json` 的 `extras_found` + 逐格看 CSV，不是「看起来像标题」。
  已加回归：逐单断言日期 + 断言它**不在** `ignored_labels` 里。
- ★ **报价截止日期一定会过期**：NGV 各单给的是来件后 1~4 周的短窗口，
  跑完脚本**必须看 stdout 末尾的「提醒业务」块**，并把它写进给业务的消息里
  （业务 2026-09-24 明确要求提醒）。它是 `validation.json` 的 `reminders`，**不计入** error/warn。
- **`CPMS` 脚本不许生成**：跑完必须由 agent 通读原文、翻译、分段后手写（总纲第 5 节）。
- ★ **业务给「真实 ERP 单据」时，那是 CPMS 的学习素材，不是字段核对题**（2026-10-19 被纠正过一次）：
  对方会把某单在 ERP 里**已确认**的导出丢过来。正确动作是**逐条对照人写的 `CPMS`**，
  看自己漏了哪些**信息维度**（估值有没有写「约/预估/暂定」、料号有没有留、材料的面/底与产地、
  责任方、客户说的「见某文件/按某要求」、工序先后、排布与随机性、客户专名），
  然后**重写自己的 CPMS**。字段级差异（导出侧留空、`PXH`/`DWMC`/`CPSL` 口径、`CPMC` 业务员）
  只在末尾做一小段附注，**不要拿二十行字段 diff 当主交付** —— 那等于没听懂需求。
  清单与真实样例：总纲第 5 节 + `docs/CPMS_规范与样例集.md` 样例 7 / 5.1「内容完整性九问」；
  复盘细节见总纲 `references/cpms-real-sample-bjqx260704002.md`。
- **`CPMC` 的中文品名同理** —— 业务员 `KM` 由脚本带出，**中文品名仍是每单 agent 补**。
- **跑完原附件就不在 `pending/` 了**：`main.py` 会把它移进 `processed/<task_id>/`，
  复核原文 / 重写 CPMS 读产物目录里的副本（实测直接读 `pending/` 会 `FileNotFoundError`）。
- **重复提取（2026-10-19 实跑 PR-00091345 第二次）**：`processed/` 里旧任务的 `CPMS` / `CPMC_ZH`
  可以直接拿来复用再按 v2 规范复核，不必从零通读；但交付必须用**新**任务目录的产物。
- **CPMS 多材料内文分行**：`内文` 标签重复三行（32PP 书纸 / 8PP 模切件 / 2PP 可移贴纸）是合法写法，
  lint 不查重复标签；但 `包装` / `装箱` 里的 `1/0C` 要手改 `1C/0C` —— `--fix` 只覆盖部件行。
- ★ **别用「默认值」冒充事实**（2026-10-20 被业务纠正过）：`rules.spine.default: 长书脊`
  让「认不出来的书脊方向」变成「长书脊」—— 四道门全过、日志还写了来源，错得像真的。
  业务口径是「识别不到 → **Null**」。纪律：`profile.rule(key, 默认值)` 的第二个参数
  **不许**写业务语义默认值（写 `""` / `None`），认不出来就留空 + WARN，让业务看见空白。
- ★ **业务短句要先落成「字段 → 值」，别扩写成文案**：原话「装订方向 如果我们识别不到
  则设置为 书脊方向为 Null」，我第一版理解成「CPMS 里写『装订在书脊方向』」并一路写进了
  `cpms_spec` / 文档 / 测试，被纠正。正确顺序：① 把它翻译成 `Drop_SJFX = null`；
  ② 只改这一个字段的链路；③ 短句里的措辞**不是**让我造新描述词。拿不准就用一句话复述
  「所以是 X 字段填 Y？」再动手 —— 比返工四处同步便宜。
- ★ **客户把混用写成 `FSC-Mix`（带连字符）**：正则 `FSC\s*Mix` 不认 `-` → 只扫到备注里的
  `FSC Recycled` → 把「真混用」误判成单一取值，`FSCMC` 差点落成 `FSC Recycled`。
  现在 `[\s-]*` + `unique_ignore_separators: true`：`FSC Mix` / `FSC mix` / `FSC-Mix` 算同一个值，
  `FSC-Mix` vs `FSC Recycled` 才算混用。**两条都栽过**（一次误留空、一次误填值），
  改这条正则必须同时跑 `test_扫描_连字符FSC写法要分开判真混用与同值`。
- ★ **定时任务补 CPMS 时的「抽错字段」处置**（2026-10-21 实单 `CU26070901`）：客户写 `3 titles`、
  `QTY: 60.000 St. in total (20.000 cps/title)`，但 `TZS` 落 1、`CPSL1` 落 0、`FSCMC` 落
  `FSC-Mix`（ERP 合法值是 `FSC Mix`）。`cpms_lint` 的 `CPMS011` 要求概要行**等于**字段，
  所以在概要行里写「共【3】款」会当场报 error，而手改 `TZS`/`CPSL1` 又会在引擎重跑时丢。
  正确动作：**概要行按字段写、客户事实写进 `备注`（带原文），汇报里逐条点名「字段/现值/原文/应然值」**，
  由引擎侧修完重跑。另两条同批教训：正方形成品（`CD=KD`，如 210×210）判不出长/短书脊 →
  不写方向词，`CPMS013` 那 1 条 warn 是**已知例外**（不许为清零编「长书脊」）；
  `cpms_content_audit --source` **要指具体客户文件，指目录不报错但等于没审**。
  全套命令/样例/v1 遗留产物（41 键那代只补内部字段、别拿 v2 lint 去「修」）见总纲
  `customer-quote-request/references/cpms-backfill-cron-notes.md`。
- ★ **「款数在第二份附件里」是 NGV 的固定失分点**（2026-10-21 A00142）：表单 B6 只写
  `versions: see detailed breakdown`、A5 `quantity` 空，款数/语言/数量全在
  `Titel+Mengen_*.xlsx` → `TZS=1 / YYS=0 / CPSL1=0` 静默落默认值，四道门全过。
  同时 `board book` 不被 `cplb.map` / `binding.map` 认（→ `32.00;其他` + `ZDFS` 空，
  只有 warn）。这两类都**不在 lint 能力范围内**，只能靠 §1.8 的手核 + 汇报。
  另：表单 B1 标题 / B30 备注 / G4 ETD 可能是**上一个项目**的模板残留，
  `CPMC`/`ECPMC` 要以邮件 + 款量表的项目名为准（同名残留不许进 CPMS）。
- ★ **德式千分位在 CPMS 里必须照抄，写 `60000` 会被门禁判成编造**（2026-10-21 实单）：
  `cpms_content_audit` 的溯源是**子串匹配**、不归一 `60.000`。写 `数量共60.000本（每款20.000本）`
  → 0 无出处；写 `60000本` → 退出码 3（`60000/20000/15000` 全报无出处）。NGV 的
  `60.000 / 17.500 / 30.100` 都是点作千分位，**别翻译成中文习惯的数字形态**。
- ★ **料号 `5/20/8478438/FSM` 会撞「印色」正则**：`cpms_lint` 把 `5/20` 当成 `4/0C` 那类印色
  → `CPMS052`。别改数字、也别删料号（转录九问要求料号抄全）—— 把它放到 `- ` 开头的三级子项行，
  lint 在解析标签前就跳过这类行：`- 料号：5/20/… Hubschrauber、…`。
- ★ **写回 `ERP_*.json` 前一定要重读 + 先备份**：定时任务可能与上一轮/人工同时写同一份产物
  （实测第一眼读到 `CPMS` 为空，几分钟后快照里已有别人写的 313 字）。纪律：`cp` 到 `/tmp/` 留原值 →
  写回前重新 `json.load`（不要用几分钟前读进内存的对象）→ **合并**别人版本里有出处的事实
  （漏抄也是缺陷）→ `json.dumps(ensure_ascii=False, indent=2)` 保持键序 → 收尾断言
  `list(out)[:59]==模板键序` 且 `changed ⊆ {CPMS, CPMC, CPMC_ZH}`。
  完整配方 / 本次成品 CPMS / 写法决策表见 `references/cpms-backfill-gotchas.md`。
- ★ **灰板 `mm → g` 会与溯源门禁打架**（2026-10-21 实测）：客户原文写 `boards 2 mm` 时，
  换算成 `1200g` 会让 `1200` 变成「无出处」→ audit 退出码 1；**照抄 `2mm` 只吃 `CPMS054`
  一条 warn、audit 0/0**。取舍写进汇报，别为清 warn 去赌 audit。任何**派生数字**
  （自己算的日期、换算的克重）同理。
- ★ **audit 只吃单一 source（xlsx/csv/txt/docx，不支持 `.eml`）**：规格在附件、上下文在邮件正文时，
  邮件里的数字（`PO#13826` / `2019` / `July 31st`）**不能进 CPMS**（拿附件当 `--source` 必被报无出处）；
  跑两遍取交集也不成立（邮件 pass 会把附件事实全判无出处）。邮件上下文进
  `CPMS_来源对照.md` + 汇报，单据号归 `KHDH` 字段。
- ★ **单附件邮件在 cron/Web 路径上会丢副档 → `KHDH`/`KHDDH` 静默为空**：`scan_pending.py` 认领时
  只搬文件、不搬 `<附件>.meta.json`，而这两个字段在 `required_empty` 里 → **四道门全绿也看不出来**。
  判别：`pending/` 里还躺着 `<附件>.meta.json`、任务目录里没有。**不要自己改这两个键**，
  按「字段/现值/原文/应然值」汇报给引擎侧（修法见总纲 `references/cpms-backfill-cron-notes.md` §8）。

---

## 9.5 ★ CPMS 严格模式（业务 2026-10-19 拍板）

`CPMS` **只写客户文件（附件/邮件正文）里出现过的事实**。业务确认单、ERP 导出、历史单据、
同类产品经验 —— 一律不能拿来「补」描述。

- 一句话判定：**每一句都要能指到客户附件的一个位置；指不到的删掉。**
- 漏抄（客户写了没写进来）是缺陷；**多写（客户没写却写进来）是错误信息，更严重**。
- 五种最常见的「柔性造数」：区间补成整数（`50-60` → `60`）、空栏补合理值（没给窗口尺寸就写 100\*100）、
  升级材料名（`polybag` → `OPP`）、搬确认单里的料号/指定供应商、把 `暂定/预估/see below` 丢掉。
- 机械门禁：`uv run ./scripts/cpms_content_audit.py --cpms <ERP.json> --source <客户原始文件>`
  （退出码 = 无出处条目数，必须 0）。
- 每单在产物目录留一份 `CPMS_来源对照.md`（逐行 → 单元格坐标 + 删除清单），交付时一起给业务。
- 详细案例（`BJXQ260704002`：合并版 1040 字 vs 严格版 521 字、被删的 12 组、工具用法）：
  `customer-quote-request/references/cpms-real-sample-bjqx260704002.md`；
  通用铁律与五种造数：`customer-quote-request/references/data-provenance-strict-mode.md`。
- 交互侧：业务丢来「已确认的真实单据」时，先问一句「要我校对字段口径，还是学 CPMS 写法？」
  —— 2026-10-19 默认做了 59 字段逐项差异对比，白跑一轮。

## 10. 交付检查清单（NGV 专用）

- [ ] 客户识别是 `ngv`（不是 igl；注意文件名含 `Colour`/`Puzzle` 的陷阱）
- [ ] ★ 若输入是**文件夹 / 压缩包 / 多附件邮件**：`validation.json` 的 `unit.members`
      与预期文件清单一致、`unit.skipped` 只有垃圾文件、**`conflicts` 为空或已人工核对**
      （参考文件合并成**一个** ERP JSON，不是一个文件一份 —— 见 1.6）
- [ ] 59 键顺序断言通过、`.validation.json` 里 **0 error / 0 warn**
- [ ] ★ **报价截止日期**：`Reminder` 字段（和 stdout 醒目块）有内容 → **必须写进给业务的消息**
      （业务要求提醒；现已过期的单要问「还报不报」）
- [ ] `CPLB` 与真实业务一致（套装 29.04 / 贴纸骑钉书 24.03 / PAD头书 23.06 / **Wire-O → 26.07;普通YO书** / 单线圈 → 26.01;普通蛇圈书 / **`board book` → 27.01;普通BB书**，`ZDFS=普通BB书` —— 档案当前无此映射，落 `32.00;其他` 要先当缺陷报）
- [ ] 套装单尺寸是 `0/0/0`，`Drop_SJFX` 空；非套装单：有 `N mm side` 才判长/短，**识别不到就是 `null`（空着算正常，不是缺陷）**
- [ ] ★ **品名三件套**（2026-10-20 起，替代旧的 `KM/Aldi …` 写法 —— 旧检查项已作废）：
      `CPMC` = **中文产品名**（agent 从 `Title:` 译，无占位符、无业务员/品牌/类别/FSC）；
      `ECPMC` = **原始产品名**（客户原文照抄，**不限语言** —— 德文 `Malbücher`、日文、法文一律照抄；
      业务 2026-10-21 澄清：ERP 字段 label 写「产品英文名」是历史命名，口径以这句为准）；
      `CPMC_ZH` = 与 `CPMC` 同值
- [ ] `cpms_todo.py` 现在**四个字段都查**（2026-10-21 补）：`CPMS` 空 / `CPMC`·`CPMC_ZH` 是占位符 /
      档案声明了 `rules.ecpmc.source_keys` 但 `ECPMC` 空 —— `CPMS_TODO: 0` 才是真的写完。
      实测 `CU26070901` 邮件没有 `Title:` 标签 → 脚本取不到 ECPMC（WARN 留空）→ agent 从原文
      照抄 `Malbücher` 补上；igl 没声明 `rules.ecpmc`，空着是对的，清单不会误报。
- [ ] `YWY` = `KM`（业务员在 `YWY`，**不在** `CPMC` 前缀里）
- [ ] `KHDDH` = 香港单号（扫不到就空 = 正常）；`KHDH` 只在邮件里有时才填（**不许拿 our_ref 冒充**）
- [ ] 数量核对：带 `= 总数` 的写法以总数为准，且 CPSL1 是「款数 × 每款数」的结果；
      ★ 单元里有 `Titel+Mengen` / `Titel 1..10` 这类**款量附表**时，必须与它逐项对齐
      `TZS` / `YYS` / `CPSL1`（表单 B6 常只写 `see detailed breakdown`，会把三者打成 1/0/0）
- [ ] `ZDFS` 非空（装订类词如 Wire-O 已在映射表里；空的要先查是不是新写法）
- [ ] `PXH` 空、`ZBLX` 空、`Remark3` 空、`SFYPJ`/`Remark1` 空（推不出就留空）
- [ ] `YYS`：单语言空 / 提取不到 0 / 多语言为真实数字
- [ ] `FSCMC`：单一材质才填，多材质留空
- [ ] `Remark4` 与 ERP 原文逐字一致（含错别字）
- [ ] `CPSX` = **JSON `null`**（附件没有年龄段栏 → 档案给「无」→ 按 `rules.null_values` 落 null；
      null 属空值，与权威导出一致，导入端按空处理）
- [ ] **`CPMS` 已由 agent 手写**（非空）；**严格模式**：只写客户文件里的事实，确认单/ERP 导出的补充信息不进 CPMS
- [ ] ★ **`cpms_content_audit.py` 退出码 0**（CPMS 里每个数字/单位/料号都能在客户附件里找到）
- [ ] ★ 产物目录有 `CPMS_来源对照.md`（逐句出处 + 从确认单删掉的内容清单）
- [ ] ★ **字段级自检（四道门抓不到这一层）**：`cpms_lint` / `cpms_content_audit` / `erp_schema` / `pytest`
      校验的都是「已经有值的字段」，**整个字段空着照样全绿**。交付前逐字段过一遍
      「本该有值的是否有值」：`CPMC` / `ECPMC` / `CPMC_ZH` / `CPLB` / `ZDFS` / `CPSL1` / `CD` / `KD` / `DWMC` /
      `KHDDH`（可空但要知道为什么空）/ `Remark4`。空着的必须能说出依据，说不出的就是漏项。
- [ ] `ID` = 本次任务号（= 产物目录名；排查问题时拿它对日志）
- [ ] `compare_with_erp.py` 白名单外差异 0 条
