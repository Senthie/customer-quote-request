---
name: customer-quote-ngv
description: NGV 客户报价需求提取（德英双语 enquiry 分区表单，或项目级多国多款数量表 → ERP 59 字段）。处理 NGV（德国/欧洲 Aldi、Circon 线）的报价需求、附件解析、口径核对时加载；总纲见 skill customer-quote-request。
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
>
> ★ **产物格式 2026-10-29 改为数组**：`ERP_*.json` 顶层是 `[{...}]`（一个报价文件可含多个产品），
> 读它一律用 `erp_bundle.load_bundle(path)`；`cpms_lint` / `cpms_content_audit` / `erp_schema`
> / `cpms_todo` / Web 都已按「逐条记录」改过 —— 自己写小脚本时别再 `json.load()` 当 dict 用。

> ★ **队列纪律（业务 2026-10-29 明确）**：卡在业务口径上（类别拆不拆、配件算不算、品名怎么译）
> **不许把队列停在那里等人回话**。正确做法是：① 把**能确定的字段全部落成 ERP JSON**、
> ② 把待确认项按「字段 / 现值 / 客户原文 / 应然值 / 一句话理由」写进汇报，
> ③ **立刻转去处理下一份报价**，那一份等业务回来再重跑（重跑成本远低于空等）。
> 现场信号：等一次 `clarify` 的十分钟里就积压了下一封询价邮件。

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
2. **`CD/KD` 已拍板（2026-10-21，结案）。** 业务口径：**默认顺序 = 长 x 宽 x 高，按书写
   顺序依次落 `CD/KD/GD`，不比较长短边**。依据是 `LG26090401` 的真实录入
   `240 x 330` → `CD=240` / `KD=330`（与 `240x300 → 240/300` 那批一致）。
   所以 `148 x 210 → CD=148 / KD=210`，CPMS 概要行写【148*210*0】。
   `size.long_first`（长边前置）已从代码和档案删除，别再改回去。
3. **`CPMS` 与 ERP 字段是两套口径。** 本次业务记录 `Drop_SJFX` 是空的，CPMS 却写
   `书籍方向：【短书脊】`。所以「`Drop_SJFX` 空 → CPMS 省略方向」不是无条件成立的：
   竖版 + 装订在顶部 ⇒ 书脊在 148mm 短边，推得出来。
   ★ **但这条只在文件里真的给了装订边时成立**（`at the top` / `at the side` / `<N> mm side`）。
   **没有任何装订边信息时不许反推** —— 那时 CPMS 必须省略方向词、认下 `CPMS013` 的 1 条 warn。
   2026-10-29 `CU26093001-01`（A00152 Option B）就栽在这条上：客户只写
   `softcover, safety saddle-stitching` + `210 x 280 portrait`，没有装订边，产物却写了
   `书籍方向：【长书脊】`，理由居然是「业务真单的 CPMS 也这么写」（跨单据引用）。
   概要行的方向必须 = `Drop_SJFX`；字段 `null` 就省略。
   另外装订段**不许照抄 `ZDFS` 的下拉值**（`胶装/PAD书`），也**不许把 `CPLB` 类别名当装订方式**
   （那次落地成 `装订：贴纸骑马钉书（…）`），要写客户原文的方式 + 方向/工艺：
   `装订：骑马钉，软封面，安全骑马钉`。
   ⚠️ 这三处 `cpms_lint` 全放过（0 error / 0 warn）——**lint 过 ≠ 文本对**，
   实测盲区与自检清单见总纲 `references/cpms-gates-blindspots.md`。

对账纪律：`"" ≡ null` 归一后先读 `customers/ngv.erp_diff.json` 与
`customer-files/samples/ngv/manifest.json` 的 `known_differences`（8 项已拍板口径，
`CPDL/BZ/PXH/YYS/SFYPJ/Remark1/Remark3/FSCMC`），**先读白名单再动嘴**，否则整份报告会被读成「31 个 bug」。

- `scripts/compare_with_erp.py` 自 1.0.48 起**自动加载** `customers/ngv.erp_diff.json`
  （不用再传 `--accepted`），并且 `--attachment` 可以是**单元目录**（一封多附件邮件 = 一份报价）。
- 完整流程、差异归因的六个类别、改口径要一起动的六处，见总纲
  `references/erp-record-calibration-playbook.md`。
- ★ **对账第一原则（2026-10-21 `LG26090401` 撞出来的）**：差异先归类，**别把 bug 当口径抛给业务**。
  「客户文件里确实有这个事实吗？」→ 有：先查我们取的是不是它（多文件单元里主文件可能选错 ——
  `ECPMC` 就取成了样张品名 `Wimmelbuch Peppa Pig`）→ 没有：才问口径。
  问业务时给「字段 / 现值 / 客户原文 / 应然值 / 一句话理由」，不要开放式提问。
- 2026-10-21 新增白名单条目：`ZDFS`（客户文件没提装订方式就留空；真实录入里的「快线BB书」
  属业务侧补填）。同批补的映射：`board book / Pappbilderbuch / Pappband → 27.01;普通BB书`
  （旧版落兜底 `32.00;其他`，`LG26090401` 实测）。

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

**1.0.48 起 `TZS` / `YYS` 由脚本从邮件正文兜底**（只看最新一封），复跑得 `10/10`。
**`CPSL1` 仍要人工看**：款量表里有数量（本单 410,000）但表单没有 `quantity` 键 →
脚本落 0 并 WARN「需人工确认」；**业务 2026-10-21 拍板：取不到就交人工确认，
款量表的合计数不自动填 `CPSL1`** —— 别手改 JSON 猜，
按「字段 / 现值 / 原文 / 应然值」汇报（见 §9 定时任务那条）。

★★ **装箱本数是本单唯一被我写错的数字（2026-10-21，1.0.50 已修）—— 按表头索引取列，别按行内位置猜。**

- `(20 PAP, 1/0c)` 里的 **`20 PAP` 是包装材料回收代码**（PAP = 纸/瓦楞），**不是「20 只/箱」**。
- 款量表 `cps/carton` 列（按表头名取索引）的值是 **2**（三个客户 × 10 款全是 2）。
  而同一条数据行的脚行还躺着无名列 `10 | 20 | 6000 | 120000`（合并表头被挤开，`20`/`35`/`FSM`
  没有表头名），反算 `120000 ÷ 6000 = 20` —— **两处打架**。
- 我第一版就是抓了脚行的 `20` 写成 `装箱：20本/箱`，`cpms_lint` 0 error、`cpms_content_audit`
  也 0/0（`20` 在文件里确实存在 → 溯源门禁**只验 token 存在，不验它出自哪一列**）。
  240*330 的 BB 书每箱 2 本不合常理 → 这个数**待业务确认**。
- 现在的写法：`装箱` 段**只写纸箱本身**（棕色标准出口纸箱 100%回收 20 PAP，1C/0C），
  备注写「客户原文 cps/carton = 2（每箱只装 2 本，装法待业务确认）」，汇报里点名请业务确认。

> 通用教训：**从多列表格里取数，先按表头名定位列索引**（`hdr.index('cps/carton')`），
> 再用同一索引读每行；**脚行/合并格里的无名数字一律不当数据**。
> 而且「数字在文件里找得到」≠「这个数字属于这个字段」—— 溯源门禁抓不出错列。

**结构缺口与已修项（同日实单暴露）：**

- ✅ **`board book` 映射已补（1.0.48）**：`customers/ngv.yaml` 的 `cplb.map` 现在认
  `board book / Pappbilderbuch / Pappband / 纸板书 → 27.01;普通BB书`，`LG26090401` 复跑得
  `CPLB=27.01;普通BB书`（旧版落兜底 `32.00;其他`）。
  **`ZDFS` 仍留空** —— 业务 2026-10-21 口径：「客户没提装订方式就默认留空」，
  本单真实录入也是空（`普通BB书` 只进 `CPLB`，不复制到 `ZDFS`）。
- ✅ **样张品名不再冒充本单品名（1.0.49）**：表单模板会被跨项目沿用，B1 标题常是上一个项目的
  （A00142 的 B1 写 `Wimmelbuch Peppa Pig`，那是 PR-00092497 的旧模板，实际项目是
  `A00142 Wimmelbücher (Winter)`）。现在**单元成员 ≥2 个时 `ECPMC` 自动取邮件标题**
  （`ecpmc.prefer_email_subject_when_unit` + `subject_strip_pattern` 去掉 `A00142` 这种项目号），
  与样张品名不一致会打 WARN 留痕 —— 不用再手动改 JSON。
  B30 的 `A+B assorted` 与邮件「only spec A」冲突、G4 `ETD = 25 Dec 2027` 与邮件
  `13 Aug 2027` 冲突 —— 这些残留**一律不写进 CPMS**，在 `CPMS_来源对照.md` 里点名。
- ✅ **多文件单元的 `cpms_content_audit`（1.0.49）**：`--source` 现在可以给**多个文件或整个单元目录**，
  按同一份语料一起审 —— 数量只在款量表里、规格只在表单里的情况不会再被误报「无出处」。

## 1.9 CU26072401 真单对账定下的四条（2026-10-21）

| 字段 | 定案 | 落地 |
|---|---|---|
| `CPMC` | **用我们的中文译名** | 真实录入/权威导出写 `联系人/说明 类别/原品名(FSC Mix)`（`Corinna/方脊锁线精装书（有鱼鳞片+布）/…(FSC Mix)`）是业务侧**旧写法**，不改；`ngv.yaml` 仍 `needs_agent` |
| `SFYPJ` | **只填布尔**：有配件 `true`、无 `false` | 档案 `rules.accessories` 关键词判（aufsteller/stand/sequin/patch/贴片…），**不按 specials 非空**（那里还装圆角/光油）；`Remark1`（自厂购/外购）仍留空 |
| 精装封面面纸 | **140g光粉**（客户 enquiry 写 128g 也按 140g） | ★**只适用于有「再版 / 沿用旧单」证据的单**（CU26072401 邮件明写 reprint of PO#13826）。**没有这种证据的普通新单按客户原文写** —— 2026-10-29 `PR-00096460` 客户写 `128 gsm glossy art paper` 即写 `128g光粉`，audit 0 无出处、未动白名单。口径白名单在 `customers/ngv.cpms_accepted.json`，用 `cpms_content_audit --accepted` 放行并打印理由；真按 140g 写时 CPMS 备注必须写明「客户写 128g、按旧单口径取 140g、待确认」 |
| `KHCPMC` | **不补**进我方 59 字段 | 真实导出多这一列，业务确认不跟 |

### 140g 的来源（客户 enquiry 没写，凭什么写 140g）

1. **再版单**：客户原话 `reprint of PO#13826, printed in 2019`，真实 CPMS 自己写「材质跟 PO#13826 相同」。
2. **enquiry 的 128 是模板复制**：封面 C15 与内文 C9 同为 128（硬皮书封面面纸跟内文同克重不合常理），
   同一张表 K1 还残留 `SSt (ex Delphin)` —— 表格是从上一版复用的。
3. **语料实证**：`/home/claw/train/custom_cpms_requie` 18476 份里 `光粉 140g` 4759 次（最高频）；
   **锁线精装 1857 份**中封面/皮壳用光粉的 **967 份是 140g**，128g 只有 **2 份**。

→ 结论写进白名单理由，交付时 CPMS 备注同时点名「待客户确认」，不许静默替换。

### 落地这几条时踩到的两件事（下次直接照做）

- **业务给的是一句话决策时（`1：… 2：… 3：…`）**，落地清单是**七处**：
  引擎 / 档案 / `erp_diff.json`（字段口径）/ `cpms_accepted.json`（CPMS 内容口径）/ 总纲 SKILL /
  本技能 / 测试。运行细节与模板见 `references/erp-record-calibration-playbook.md` §2、§6。
- **改口径必然弄红旧测试**（本次 `SFYPJ`）—— 那是预期。**连断言语义一起改**并在 docstring
  写「旧口径作废 + 日期」，别为了让测试变绿直接删断言。审计报「0 无出处」时先自查语料里
  有没有混进我们自己的产物（同 playbook §7）。

> 本单完整复盘（两份文件的原始数据、5 个字段落错、CPMS 成品与取舍）：`references/bb书与款量附表-a00142.md`。
> 其中的 **§7「重跑一单的正确姿势」**：把单元目录（**含 `_email.meta.json`**）拷到 `/tmp/` 再跑
> `main.py` → 写 CPMS → 四道门 → 旧目录 `mv processed/_superseded_<日期>/`。
> ⚠️ 漏拷 `_email.meta.json` → `TZS`/`YYS`/`ECPMC` 三条邮件兜底**全失效**，而且四道门照样全绿。

## 1.10 ★ 第四种形态：Lidl `Anlage_<IAN>` 多国别款量表（`.xlsm`，2026-10-29 实单 574237 / 574239）

Rex 内部转发 VEMAG 的 Lidl 项目时，附件是 **Lidl 自己的表**，不是 NGV 的 enquiry 表单：

```
H2   IAN 574237 K Motiv Puzzlebücher Overview of Countries, Quantities, and Titles
H4   Delivery Time: KW 24 / 2027:  |  all countries
r9   A=Griechenland:            ← 国别分区标题（A 列，结尾冒号；Online Shop 段没有冒号）
r11  A=ArtNr. … F=article: H=title /style J=quantity /title O=dimension P=结构 S=材质
r12  D=5/ E=28/ F=574237_A G=/FSM H=Match it Puzzle Book - DINOSAURS J=956 …
rN   H=total: I=10 J=4780      ← 分区合计，**不参与累加**
另两张表：Pivot（交叉表，`Summe von quantity/title` + Gesamtergebnis）、Tabelle1（平铺）
```

**策略：`rules.layout.strategies: [lidl_anlage, section_pairs]`**（`extract_fields._extract_lidl_anlage`）。
它只认工作表名以 `Anlage` 开头的表；命中后同一文件里的 Pivot / Tabelle1 **整张跳过**
（否则 section_pairs 会从 Pivot 抽出 `quantity: 'Spaltenbeschriftungen'` 这种垃圾值，
数量还会重复计）。产出规范键：`product_name`（标题行项目名）/ `quantity`（明细行累加）/
`versions`（`<款数> titles, <语言数> languages`）/ `size`（cm→mm）/ `specials` / `text_material` / `notes`（交期）。

实测两份与客户 Pivot 的 `Gesamtergebnis` **逐格相等**：IAN 574237 = 51,460（5 款，24 语言），
IAN 574239 = 365,340（6 款，25 语言）。

四个必踩的坑：

1. **`.xlsm` 要当成表格加进四处清单**：`extract_fields.TABLE_SUFFIXES`、`input_unit.TABLE_SUFFIXES`、
   `main.SUPPORTED_INPUT_SUFFIXES`、`ingest_email.SUPPORTED_ATTACHMENTS`。
   漏 `main.py` 那处 → 步骤 1 直接「不支持的格式 .xlsm」并把文件搬进 failed/；
   漏 `main.py` 的**内容探针**（`probe_member.suffix in (".xlsx", ".xlsm", …)`）→ 正文不参与识别，
   只凭文件名 `Puzzle` 判成 **igl（10 分）**，整单口径全错。
   ★ **2026-10-30 复验：内容探针对外仍是漏的** —— 单独喂 `574237_PuzzleBook_Anlage_22102026.xlsm`
   跑 `-c auto`，日志依旧是 `识别得分偏低（10）—— 只有文件名/主题这类弱证据` +
   `客户档案: igl（文件名含 'puzzle'）`，`lidl_anlage` 不生效、只抽出 1 个 `isbn: EAN-Nummer`。
   所以 **Lidl Anlage 文件一律显式 `-c ngv`**（`lidl_anlage` 只在 ngv 档案里），
   别相信自动识别；重跑出正确产物后旧目录移 `_superseded_<日期>/`。
2. **客户表的 `ArtNr` 整列可能是别的 IAN**：574237 那张表里，除希腊以外的所有分区都写着
   `574239_A…E`（客户复制了另一份文件只改了标题）。**项目号看 H2 标题行的 IAN，不要拿 F 列当款号**，
   并在汇报里点名这处客户数据缺陷。
3. **一个 xlsm 里的多款可能不同结构**：574239 的 6 款 = 3 款滑动发声（皮壳 2mm 灰板 → `27.19;精装音乐BB书(线路)`）
   + 1 款亮灯书（400gsm 封面无灰板 → `27.18;普通音乐BB书(线路)`）+ 2 款揭页发声（→ `27.05;普通揭页BB书`）。
   ★ **2026-10-29 业务已定口径：按报价结构拆**（森西原话「按报价结构分组」）——
   `scripts/product_groups.py` 拿 `lidl_anlage` 产出的逐款条目自动分组，
   574239 实测 → **3 条 ERP 记录**（`TZS` 3 / 1 / 2，`CPSL1` 182,670 + 60,890 + 121,780
   = 365,340，与客户 Pivot 的 `Gesamtergebnis` 相等）。同一个 ERP JSON 数组里放 3 条，
   **CPMS 也要写 3 份**（脚本一律留空），每条按自己那条的结构写。
   交付前用 `uv run python scripts/cpms_todo.py --dir <processed>` 复查：它按条列出
   「第2/3条 缺=CPMS」，写全一条就少一条 —— 这是多产品单唯一可靠的「还没写完」清单。
   ⚠️ `resolve_cplb` 目前对「发声/灯光 BB 书」的区分还不够细（三组可能都落 `27.01;普通BB书`）——
   这正是要 agent 按 `specials` 原文逐条改 `CPLB` 的地方，别照抄。
   ↳ **2026-10-30 已交付实跑（任务 `7bd10d32…`，取代 `923ac86b…`）**，逐条改下来是这样，
   下次遇到同形态照这张表走：

   | 记录 | `CPLB`（agent 改） | `CPMC`/`CPMC_ZH` | `ECPMC`（客户款名） | `TZS` | `CPSL1` |
   |---|---|---|---|---|---|
   | 1 | `27.19;精装音乐BB书(线路)` | 滑动发声书 | `Sliding Soundbook` | 3 | 182,670 |
   | 2 | `27.18;普通音乐BB书(线路)` | 亮灯书 | `Lightbook` | 1 | 60,890 |
   | 3 | `27.05;普通揭页BB书` | 揭页发声书 | `Lift-the-flap Soundbook` | 2 | 121,780 |

   三条口径（都是踩过才定下来的）：
   - **`ECPMC` 要逐条改成客户款名**（`H12`/`H15`/`H16` 去掉「- 款名」部分）。引擎三组都取成
     邮件级品类 `Sound-Light books` —— 换成 ERP 里三张单长得一模一样，业务分不出谁是谁。
   - **`SFYPJ` = `true`**：附表写了发声模组/发光模组，引擎落 `false`（`lidl_anlage` 不产配件键）。
   - **`ZDFS` 留空**、**`Drop_SJFX` 留 `null`**：前者是 2026-10-21 拍板（线路属业务补填）；
     后者因为是 **200×200 正方书**，无长/短边判据 —— 语料 5617 份正方书 `Drop_SJFX`
     **全部为空**，旧产物那条「长书脊」是 `gatefold on the right-hand side` 被装订方向正则
     误命中的假值。注意 `CPMS` 概要行**仍要写** `书籍方向：【长书脊】`（`cpms_lint` CPMS013
     要求书本类必须写；语料里 5223 份正方书也是这样写的：字段空、概要行有）。
   - 每条 `CPMS` 只写自己那条结构；`cpms_content_audit` 报的「未写」几乎都是**别的记录**的
     用纸（拆分后必然出现），不是漏抄 —— 别为了消掉它把别组的材料搬过来。
   > 📎 **多记录单的完整配方**（重跑为什么先拷到 `/tmp`、6 个必改字段、回写脚本骨架与
   > 允许集、audit「未写」怎么解读、归档要写 `_SUPERSEDED.txt`、来源对照多出的两块、
   > `render_real.js` 验 Tab）：`references/多记录重跑与逐条补字段.md`。
4. **语言数只算「有量」的语言码**：瑞士法语/意大利语段常年是 0，算进去 YYS 就多。款数按标题去重。

**类别口径（书目树 criteria + 语料实证，别凭产品名猜）**

| 客户原文结构 | CPLB | 依据 |
|---|---|---|
| `21pcs Puzzle pieces` 打散件 + 书 + `Rigid box` | `29.02;砌图套装` | 树 29.02「打散后的砌图跟其他产品/物料组合而成」；`total: 26.07` 那类「内文有砌图」才是 27.03/27.04 |
| 内藏 `sound mould` / `light module` + 皮壳（2mm chipboard 面纸） | `27.19;精装音乐BB书(线路)` | 树 27.19「封面有灰板包皮壳 + 内藏软线路音乐器」；外露音乐器才是 27.16 |
| 内藏发声/灯光但封面无灰板 | `27.18;普通音乐BB书(线路)` | 语料 `YC26061601 渐变亮灯普通BB书`、`YJ25101701 亮灯书` 都落这条 |
| 揭页 + 发声 | `27.05;普通揭页BB书` | 树 27.05「内文有揭页」 |

`SFYPJ`：发声/灯光模组 → `true`（语料里音乐 BB 书全是「是」）；纯砌图套装 → `false`（`LG26041002` / `XJ23112803` 都是「否」）。
`ZDFS` 语料：滑卡/亮灯/4-5 音的内藏线路 → `慢线BB书`；砌图 → `快线BB书`/`中线BB书`（两者都有先例，属生产口径，交业务定）。

## 1.11 ★ 邮件线程「其余不变」时，款数/数量在**引文**里（CU26093001-01 / A00152，2026-10-29）

客户回信只写新规格 + `The rest remains as before.`，`6 Titles 6 Language` 和
`Total QTY: 112,266 cps in total` 都在**被引用的上一轮往来**里。而 `versions_from_email()`
只读最新一封（那是为了防「引用旧项目串数」），所以会落 `TZS=1 / YYS=0 / CPSL1=0`。

1.0.53 起 `ngv.yaml` 的 `rules.scan` 加了两条兜底（整串一起抓，再交给 `versions.title_pattern` /
`language_pattern` 各自取数，避免「哪个是款、哪个是语言」搞反）：

```yaml
- { key: versions, pattern: "(\\d+\\s*titles?\\s*[,+/]?\\s*\\d+\\s*languages?)" }
- { key: quantity, pattern: "(?i)total\\s*qty\\s*[:：]\\s*(\\d[\\d.,]*)" }
```

⚠️ 这两处取的是**我们自己上一轮发出去的报价文本**（客户说「其余不变」= 认账）。字段可以这么取，
但 `CPMS_来源对照.md` 里必须写明出处是「同一线程引文」，不要让审计误以为是我们自己给自己作证。

同批修的两个真 bug：

- **`binding_direction` 必须有 binding 上下文**：邮件里 `specs are mentioned at the bottom left`
  （说的是 Excel 里规格在左下角）被旧正则 `(?:at|on|along)\s+the\s+(top|bottom|left…)` 当成装订边
  → `Drop_SJFX` 落「短书脊」。现在要求前面 40 字内出现 `binding|stitch*|Bindung|装订`，且**捕获组只留方向词**
  （`at the top`），老测试断言的字符串不变。
- **`safety saddle-stitching`（-ing）不命中 `safety saddle-stitched`（-ed）** → `ZDFS` 空。
  `binding.map` 已补 `-ing` / `saddle stitch` 三种写法。

> 📎 这两节的**可复现原始数据、新增布局策略要同步的七处、后台补写任务的并发写处置、
> `cpms_content_audit` 的 token 怪癖（`1.000` / `A00152` 会被判无出处）、分类双证查询命令**：
> `references/lidl-anlage-表与并发补写.md`。

## 1.12 ★ 开工前先查「这单是不是已经报过」（2026-10-29 业务明确要求）

业务原话：**不要出现重复的产品报价**。NGV/Lidl 的邮件会晚到或被重复投递（信箱同步、客户转发），
而同一条 enquiry 常常**几天前就已经由业务手动报过**了。

**流程（跑管道之前做，两分钟）**：

1. 从附件里取「可检索锚点」：`reference IAN/PO`、article number（`5/20/8243513/FSG`）、
   `PR-…` 询价号、标题原文（如 `Stiftung Lesen`）。
2. 在语料库 `/home/claw/train/custom_cpms_requie` 里 `grep -rl` 这些锚点，
   命中就 dump 那条 ERP 的 `CPMC / CPLB / TZS / CD / KD / KHDDH / CPMS` 前 200 字。
3. 判据：**尺寸 + 装订 + 款数 + 品名**四项对得上 → 已报价。★ **2026-10-29 业务追加口径：
   重复单也照样跑管道、照样出 ERP JSON，并且必须把 `CPMS` 写完整** ——
   原话「如果以后出现重新的报价单，你也需要把 CPMS 补充好」，理由是 **CPMS 是检验/学习的必要数据**，
   不因为「已经报过价」就省略。别再把「重复」当成「不产出」的许可。
   与首次报价的区别只在**交付口径**：汇报里点明「重复询价，参照 `<已有单号>`」+ 本次差异，
   由业务决定要不要再发一版，产物本身四道门照走（lint / audit / schema / pytest）。
   参考实现：`3fdfd3e1520541b09968cdfee9aa4084`（Rossmann PO 15910 再版）、
   `cfa1d50f976d418fbcd8e5748a14f2ec`（PR-00078025 重开）—— 两份都写了 CPMS + 来源对照。
4. 只有确实没报过的部分才落**新单号**（同一封邮件含多个 spec type 时按 spec 拆）。

**实例（2026-10-29，`AW: urgent enquiry for Lidl: BD Stiftung Lesen Wissen + Vorlesen`）**：
表里 4 个 spec type 全部已报 —— A（210×280 72PP 锁线精装 4 款）→ `CU26100603`、
B（120×150 精装BB+塑胶手挽 4 款）→ `CU26100604`（两份单的装箱段互指 `A款28本+B款12本`，同一天同一批）、
C（190×245 锁线精装）→ `LY26072801`、D（200×200 冲形状BB）→ `LY26072802`（7 月 IAN 543832 在产项目）。
核对报告：`customer-files/processed/_dedupe_20261029_AW_BD_Stiftung_Lesen.md`。

⚠️ **cron 不会替你做这一步**：5 分钟扫描见到 pending 里的 xlsx 就跑，这次产出了
`CPLB=32.00;其他 / CPSL1=0 / CD=KD=0 / CPMS 空` 的垃圾单（已移 `_superseded_*`）。
所以「先查重」是**人/agent 侧**的责任；看到 `32.00;其他` + 全 0 的产物先怀疑「格式没命中」，别当交付件。

**判别指纹（2026-10-29 补，定时任务那一轮实测）**：`<原名>.extracted.json` 的
`profile_id` = `generic` + `CPLB = CPMC = 32.00;其他` + `CPSL1~10` 全 `0` + `CD/KD/GD = 0`
+ `values` 只有一两个键 —— 这就是「整张表的形态没被支持」，不是「标签认不出」
（`unmatched_labels` 反而是空的）。**这种记录上一律不写 CPMS**：四道门全绿是因为
`0` 与 `32.00;其他` 都合法，没人会问「这个值是不是兜底的」。
完整的三项预检（抽取命中的判别表 / 语料库查重锚点 / 产物被并发会话 `mv` 到
`_superseded_*` 后怎么处置 / 不交付时怎么汇报）见总纲
`customer-quote-request/references/cron-agent-prechecks.md`。

★ **第二种指纹：`profile_id=ngv` 但「值跨格没拼」（2026-10-29 `PR-00078025`）** ——
`layout: section_pairs×N` 的 N 很小、`values` 只有 4~6 个键、`unmatched_labels` **空的**、
`CD/KD/GD=0` 而原文明明写了尺寸（表单把 `160 | x | 200 | mm | portrait` 摊在 C6:G6）。
这不是「标签认不出」而是 `value_mode: first` 只吃到第一格 → 尺寸/页数/纸张/印色全丢，
四道门照样全绿。同单还叠了三档数量（`5.000 / 7.500 / 10.000` → `CPSL=0`）、
`flexcover`/`thread-stitching` 未映射（→ `32.00;其他`，应然 `22.03;假精装书`）、
`offer_deadline` 没进 values（→ `Reminder` 空、过期 21 个月的提醒整条丢失）。
判别表、修法、以及「同一询价主单 + `-01` 改版两条都要读」的去重实证：
`references/ngv-multicell-values-and-dedupe.md`。

> ★ **上面这些缺口 2026-10-29 已在引擎里修掉**（`customers/ngv.yaml` +
> `scripts/extract_fields.py` + `scripts/main.py`，回归 `tests/test_ngv_form_versions.py`）：
> ① `layout.sections_adaptive: true` —— 分区内按**标签**找行，2025/2026 两版表单都能对上
> （`offer deadline` 两版位置不同 → 两处都声明；`TPS` 在 versions 或 content → 两处都声明）；
> ② `value_continue: true`（`Bindung` 行）—— 装订工艺跨行时拼起来
> （`Flexcover` + `thread-stitching`）；③ `parse_tier_list()` —— 一排档位落 `CPSL1..n`；
> ④ `cplb.map` 加 `flex\s*o?\s*cover`（`Flexcover`/`Flexocover` 两种拼写）→ `22.03;假精装书`，
> `binding.map` 加 `flexocover`/`flexcover` → `精装书`；
> ⑤ 数量「每款」写法：`12,500 cps each` → `CPSL1 = 每款 × TZS`，
> 客户自报总数与之不符（Rossmann 写 `25,600` vs 2×12,500=`25,000`）→ **按每款算 + WARN**，
> CPMS 备注写明待客户确认（语料实证：`TZS=2` 的单 `CPSL1` 就是 2×每款，即**印刷总量**）。
> 再遇到「值跨格 / 多档 / 装订跨行」先看这几处是不是被改回去了。

**去重补充两条（同日实测）**：① 锚点优先用 `PR-…` 询价号，命中主单时**顺手读 `-01`/`-02` 改版目录**
（改版存在本身就是「当年确实报过」的证据）；② 规格微差（本次内文 `130gsm woodfree white`
vs 录入 `128gsm哑粉`，差 2g）**不足以破去重**，四项主干（尺寸/装订/数量档/品名）对得上就是同单，
差异只在报告里点名请业务确认。同一份文件被投递两遍（`.eml` 附件 + 直接上传带 `__1`，md5 相同）
时两份都归档、报告写明「同源重复」。

**复投判定三条补充（2026-10-29 `Rossmann / Sudoku & Rätselbücher` 实测）**：

1. **先比日期，再比规格**：`.eml` 的 `Date` 头 vs 语料的 `BDBH`（`BJXQ` + **YYMMDD** + 序号）
   vs `KHDDH`（`CU` + YYMMDD + 序号）。本次邮件 2026-09-30、`BDBH=BJXQ261006005`（10-06 出单）
   → 同一封邮件六天后已报过。日期比规格更快，一眼可判，还能证明「不是新一轮」。
2. **裸数字 grep 必须二次过滤**：`grep -rl 15910` 命中的常是无关单（数字也出现在价格/日期里），
   要用尺寸把候选收窄（`CD==140 and KD==200`）。**最硬的证据是 CPMS 里那句
   「半返之前订单 KHDD… PO#…」** —— 同一次返单的报价几乎都会写它。
3. **重复件的处置要连孤儿副档一起清**：不写 CPMS、产物移 `_superseded_<日期>/`，
   并把留在 `pending/` 的 `<单元名>.txt.meta.json` 一并归进去（扫描器只搬文件不搬副档）；
   收尾 `cpms_todo.py` 应为 `CPMS_TODO: 0`。**重复 ≠ 无事发生**：复投邮件里可能有上一份记录缺的信息
   （本次 `CU26100601` 的 `CPSL1~10` 全 null，而邮件给了每款数量与 `ETD CW 13/2027`）
   → 报告单列 delta，并写明「可直接补进已有单，不必新开」。

> 可照抄的执行配方（日期指纹 / 查重命令 / 报告五节 / 本次案例）：`references/dedupe-repeat-run-recheck.md`。

**第五种形态（尚未支持）**：Lidl `Übersicht … _Preisanfrage.xlsx` —— 表头在 A1 的**平铺多列表**
（`spec type / Nr. / reference IAN/PO / article number / title / assortment in display / QTY to be printed /
width / height / binding / content / content gsm / content paper / printing of content / cover material / …`），
一行一款、`spec type` 用 A/B/C 分组。现有 `section_pairs` / `lidl_anlage` 都吃不下，
要支持得加「按表头名定位列」的策略。

**第六种形态（已支持）**：`Anfrage_Titel+Mengen_<项目号>_<项目名>.xlsx` —— VEMAG 的项目级
「国别 × 款量」表（表头在第 5 行、16 个国别区块、**表尾 r166-r185 才是规格汇总块**）。
2026-11-03 实单 `A00130 Logik-Rätsel`（7 款 × 3 规格组 × 357,075 本）暴露两个坑：
① **只看表头会误判「客户没给装订/用纸」** —— 规格按 A/B/C 组写在表尾，判「没有」前先整表 dump；
② 这种表抽出来的产物是 `32.00;其他` + 全 0，`CPMS011` 会逼你写「0\*0\*0 共 1 款」的假概要行 →
**不写 CPMS**（业务 2026-11-03「不充 CPMS」）。
出数命令：`parse_titel_mengen.py`（抽结构）→ `titel_mengen_to_erp.py`（结构 + 表尾规格块 +
语言图例 → 3 条记录，`--names` 传中文品名）。完整口径 / 两个脚本的参数 / 校验结果：
`references/anfrage-titel-mengen-表.md`。

## 2. 怎么认出这是 NGV

- **文件名关键词**：`ngv`
- **正文特征标签**（`min_signature_hits: 2`）：`enquiry`、`Angebotsfrist`、
  `VK-Verpackung`、`Sortierung/VE`、`Schutzumschlag`、
  `Overview of Countries, Quantities, and Titles` / `Delivery Time: KW`（Lidl 款量表，
  2026-10-29 加：Rex 内部转发时发件人是 ghouseproducts，域名线索失效，只能靠表头）
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
| `ID` | 本次任务号 | 内部字段：= 产物目录名 = 日志里的「任务ID」 || `Reminder` | `报价截止日期 2026-06-26 已过期 90 天（基准日 2026-09-24） —— 请与业务确认这单是否还要报价` | 内部字段：把提醒固化进产物（1.0.23 起） |
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
| 纸板类型 `ZBLX` | ~~**留空**~~ **⚠️ 已作废（2026-10-30）** → 现为 **`A=B双坑纸板`**（`erp_defaults` 默认值，枚举 002）。旧依据「导出里 3 份 `B3B普通芯` 1 份空」是 ERP 侧旧数据，那个值本来就不在下拉枚举内 |
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
| `YSLX` 运输类型 | ~~**默认留空**~~ **⚠️ 已作废（2026-10-30）** → 现为 **`FOB(拼柜)`**（`erp_defaults` 默认值，枚举 015，**半角括号**） |
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


### 4.2b 第五批（2026-10-30）★ 11 条「常用值」→ 默认值层 `erp_defaults`

业务一次给了 11 条常用值，并拍板口径（森西原话，三答）：
① **「客户写了就听客户的，没写才用默认」** ② **半角括号** ③ **只对 NGV**。

| 字段 | 落库值 | ERP 枚举 | 备注 |
|---|---|---|---|
| `BZFS` 包装方式 | `纸箱包装` | — | 4 份导出逐字相同 |
| `TBLX` 台板类型 | `不打卡板` | code 06 | |
| `BZ` 客户目标价币种 | `美元` | code 02 | 已从公共 `erp_fixed` 挪到 `erp_defaults` |
| `SHFS` 送货方式 | `FOB(NGV)` | — | **半角**括号 |
| `ZXLB` 纸箱类别 | `普通纸箱` | code 008 | 历史导出为空 |
| `ZBLX` 纸板类型 | `A=B双坑纸板` | code 002 | 历史导出的 `B3B普通芯` 不在枚举内 |
| `ZXFS` 装箱方式 | `平装` | code 0 | 历史导出为空 |
| `DKMC` 底卡名称 | `B3B坑纸板` | code 02 | **在 `DKMC` 下拉里，不在 `ZBLX`** |
| `YSLX` 运输类型 | `FOB(拼柜)` | code 015 | **半角**！业务口述时写的是全角 |
| `SHDQ` 送货地区 | `FOB(NGV)` | 联动下拉 | 可选值由 `YSLX` 决定（外贸 8 种共用一套 7 个）；`FOB(拼柜)` → `FOB(NGV)` 同组，跨字段校验守住 |

**三层顺序别弄反**（2026-10-30 起，细节见总纲 `references/spec-fields-and-linked-dropdowns.md`）：

1. 客户原文**证据** → `spec_fields`（step13a）—— 例如探针单的
   `30 brown standard export carton` → `BZFS=纸箱包装`，日志带证据原文；
2. 本表 11 条 `erp_defaults`（step13b，**只补空**）；
3. 都没有 → 进内部确认清单问客户（`blocking=False`，不拦交付）。

所以 NGV 单跑完通常**只剩**「报价截止日期」一条待确认 —— 如果清单里冒出 `Q_BZFS`/`Q_YSLX`
之类的规格缺口，说明 `erp_defaults` 那 11 条没生效（先查档案与 `erp_fixed`/`erp_defaults`
是否重复声明，`--validate` 会拦）。
| `chkBSYTB` 不使用台板 | **`false`** | checkbox | ★ **2026-11-03 语料证伪**：18476 份真实单据 true 出现 **0 次**，且 `TBLX=不打卡板` 的 7236 份也**全是 false** → 勾选框与 `TBLX` 不联动，**不要勾**。原「历史导出是 false，旧数据作废」的判断是错的，见 `customer-quote-request/references/erp-corpus-ground-truth.md` |

**机制**（细节与坑见总纲 `references/erp-defaults-vs-fixed.md`）：

- `erp_fixed` = **无条件覆盖**（`YWY=KM` / `PXH` 显式留空 / `Remark4` 常量 / `CPDL` 书版）
- `erp_defaults` = **只补空**（`main.py` step13b，跑在所有推导之后）
- 同一字段两边都写 → `--validate` 报错；偏好表 `preferences` 必须与落库口径逐条一致
- 布尔字段**不能按「值是否为空」判断**（模板默认 `false` 是非空值）→ 用 `assigned` 判定

⚠️ **现状缺口（2026-10-30 查证，重要）**：这 11 个字段在 `main.py` 里**一个赋值点都没有**
（`grep 'assign("SHFS"\|result\["SHFS"\]' scripts/main.py` → 0）。客户真在 enquiry 里写了
`VK-Verpackung: Palette`，那值会落进规范键 `primary_packaging`，**只进 CPMS 素材与分组依据、
不会进 `BZFS`** —— 所以现在这 11 条实际上是「挂在默认值名下的固定值」。
要真正实现「客户写了听客户的」，需要业务给「德/英原文 → ERP 下拉 label」对照表，
再写成 `rules.*_terms` 关键词映射。**交付时别向业务宣称「客户写什么就填什么」** —— 目前不成立。

> 📎 **★ 2026-11-03 语料校准（18476 份真实报价单）推翻了三条假设**，细节与命令见
> `customer-quote-request/references/erp-corpus-ground-truth.md`。一句话版本：
> - 报价阶段真实有值的只有 `BZFS`/`TBLX`/`SHFS`/`SFYPJ`（99-100%）；`SHDQ`/`YSLX`/`ZXLB`/
>   `ZBLX`/`ZXFS`/`DKMC`/`BZ`/`CPDL` **六年 0 填充** —— 那 7 条默认值是业务新要求，不是历史做法。
> - `chkBSYTB` 全语料 true 出现 0 次，与 `TBLX` 无联动（已删 `derive_from`，规则表标 `derivable: false`）。
> - 「从原文推导」只有 `BZFS` 成立（97% 命中）；`SHFS` 0.7%、`TBLX` 1.6% → 它们实际是**档案默认值**。
> 改枚举 / 加默认值 / 加推导规则**之前**先跑 `scripts/audit_erp_corpus.py --enums --since 2023`
> 与 `--customers --check`；判定门槛：众数 <50% 不给默认值（IGL 的 `TBLX` 32% 就是这种）。
>
> 📎 **把语料变成本档案的 `erp_defaults`**：`scripts/derive_customer_defaults.py --customer ngv`
> —— 判定用**两个率**（覆盖率 = 有值/全部，一致率 = 众数/有值），双 ≥0.9 才采纳；
> 产出 `customers/_draft_ngv.erp_defaults.yaml`（带未采纳原因）供人审后并入档案。
> NGV 实测（2023+ 2677 份）：`BZFS` 2677/2677、`TBLX` 99.96% → 采纳；
> `SHFS=FOB(NGV)` 只有 **86%** <0.9，但业务 2026-09-24 已定 → **保留并注明「低于阈值、
> 依据业务口径」**（次多 `DPU` 304 份是德国国内线）。
> 两率为什么不能合成一个、布尔字符串化会怎样、改默认值要连带动哪四处：
> `customer-quote-request/references/customer-erp-defaults-derivation.md`。
>
> 📎 **第二种附件形态：`Anfrage_Titel+Mengen_*.xlsx`（项目级多国多款数量表）**
> —— 表头在第 5 行、每 7 行一个国家区块、Land/日期/合计是合并单元格。
> `sections:` 那套引擎对它**一无所获**（CD/KD/CPSL 全 0，看着像成功），
> 所以先跑 `scripts/parse_titel_mengen.py` 抽结构，并把错产物改名 `.json.unsupported`；
> 别漏**表尾规格块**（装订/用纸在 r166-r185，不在表头）；
> J 列 = 语言代码（表尾 r168-r179 就是图例，不是推断），L 列含义仍未明但只影响「同国两批要不要分开报」；
> `TZS`=组内款数、`YYS`=有量语言数、`CPSL` 不填、`CPMS` 留空 —— 已用 719 份语料定案，
> 出数用 `scripts/titel_mengen_to_erp.py`。
> 全文：`references/anfrage-titel-mengen-表.md`。
>
> 🔁 **跑回归/校准样本前先看两条**：
> - 批量建档（`--bootstrap --min-records 100` 给还没收到询价的客户铺默认值、draft 档案约定、
>   子技能只对 calibrated 强制）→ `customer-quote-request/references/batch-profile-bootstrap.md`
> - **别直接拿仓库样本跑 `main.py`** —— 它会移走输入文件，`test_pipeline` 会静默 skip 全绿。
>   用 `customer-quote-request/scripts/run_sample_safely.sh <样本> --customer ngv`，
>   原理与恢复办法见 `customer-quote-request/references/pipeline-local-run-safety.md`。

要自己复核这个缺口：
```bash
cd /home/claw/customer-quote-request
for f in BZFS TBLX SHFS SHDQ ZXLB ZBLX ZXFS DKMC YSLX chkBSYTB BZ; do
  echo "$f: $(grep -c "assign(\"$f\"\|result\[\"$f\"\]" scripts/main.py)"
done   # 全 0 = 没有抽取通道
```

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
- ★ **正文提到的「历史单 PO」不是本单单号**（2026-10-21 `CU26072401` 撞出来的）：
  附件备注写 `此单产品类似之前订单 KHDD19080122 PO#13826`，旧引用识别只认英文
  `same as / per PO`，中文「类似之前订单」拦不住 → `13826` 被落成 `KHDH`，
  而本单**没有客户单号**（真实录入也空）。1.0.51 起 `_REFERENCE_WORDS` 认
  中文（之前订单/上次/老单/旧单/返单/类似/相同）与德英（`previous / earlier / last order`、
  `vorherige Bestellung`）说法，60 字窗口判定；但**只认这些词**，`订单号：PO#…`
  这类正常表述照样取。看到 `KHDH` 有值先问「这是本单的还是引用别人的」。
  同批修的还有：`KHDDH` 的出处以前恒显示「规则 khdh」（`number_sources` 有却没有按 target 取），
  日志里出处错会误导排查。
- ⚠️ **单附件邮件的副档丢了 → `KHDH`/`KHDDH` 静默为空**（`CU26072401` 首次产出就是这个：
  两个字段都空，但`required_empty` 让四道门全绿）。判别：`pending/` 里还躺着
  `<附件>.meta.json`、产物目录里没有；重跑时把 `.meta.json` 一起拷进去，或直接
  `ingest_email.py --eml` 重新投递一遍（副档会重新生成，`numbers` 也按新规则算）。

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

> 🔴 **★ 待业务确认（2026-10-21，1.0.51 对账 `CU26072401` 时发现）—— 这一节可能整节作废：**
> 真实录入 + 4 份权威导出（`CU26051601-01` / `CU26061701` / `CU26072301` / `CU26072302`）
> **5 份全是拼装式**，没有一份是纯中文译名：
>
> ```
> 联系人/说明 类别/<项目号或尺寸> <原品名>(FSC Mix)
> Corinna/方脊锁线精装书（有鱼鳞片+布）/240x240 Das allerschönste Osterei der Welt (SST)(FSC Mix)
> Kathrin/Aldi 24PP内文 + 12PP贴纸 贴纸骑马钉书（有贴纸）/price enquiry. A00112 License Sticker Books 2(FSC Mix)
> Corinna/Aldi A款 卡片套装 其他套装/Type A - A00125 Post- und Klappkarten - Postkarten-Box mit Aufsteller (4 titles)(FSC Mix)
> ```
>
> 也就是说「`CPMC` = 中文译名」这条 2026-10-19 的拍板**只在新流程里出现，历史与现状都是拼装式**。
> 现状处理：产物仍按中文译名写（业务最后一次拍板），但每次对账都会撞这条 ——
> 已写进 `customers/ngv.erp_diff.json` 的 `CPMC` 条并标★，SKILL 总纲字段表也标了。
> **交付前若业务在场就直接问「CPMC 用拼装式还是中文译名」**；确认后要么改
> `rules.cpmc.template`（三槽位：`{contact}` / `{说明 类别}` / `{原品名}({FSC})`），
> 要么把这条白名单说明改成「已作废」。别第三次自己猜。

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
> 定时任务补写的门禁坑（德式千分位 `60.000` / 料号撞「印色」正则 / 并发写回纪律 /
> **§7b xlsx 日期单元格的日号溯源** / **§10 精装 4 款成品 CPMS 样例**）：
> `references/cpms-backfill-gotchas.md`。
> 真实 ERP 逐字段差异表：`references/real-erp-diff-BJXQ260704002.md`（Wire-O 类别、CPMC 业务员、
> CPSL 空值等 8 条口径分歧的来源单据）。
> BB 书 + 款量表（`Titel+Mengen`）单元的复盘、5 个字段落错、CPMS 成品与取舍：
> `references/bb书与款量附表-a00142.md`（§1.8 的配套案例）。
> ★ **CPMC 拼装式的 5 份证据 + 「真实 CPMS 有、客户文件没有」的 7 处清单**：
> `references/erp-record-diff-cu26072401.md`（§6 那条★待业务确认的来源单据；另含
> 历史 PO 被当本单单号、单号出处标错 target、灰板换算被误判 三个引擎缺陷的复现）。

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
- ~~**别给 `ZBLX` 填值**~~ **⚠️ 已作废（2026-10-30）**：`ZBLX` 现在**就该填** `A=B双坑纸板`（默认值层，见 §4.2b）。
  仍不许瞎猜的是：`JHRQ`/`WCRQ` 不填日期、套装不填尺寸。
- **别用「默认值」冒充事实**：`erp_defaults` 那 11 条是**业务给的默认值**（询价没提到才用），
  跟「我们自己猜一个合理值」不是一回事 —— 前者可追溯、后者是 §9.5 的柔性造数。
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
- ★ **数量写成「三档」、档案却是 `quantity.mode: single` → `CPSL1~3` 静默全 0**（2026-10-29 实单 `PR-00096460`）：
  客户 B5 写 `3.000, 5.000, 10.000 cps`（三档报价），`single` 模式只认一个总数 →
  `parse_single_quantity` 解析不出 → `CPSL1/2/3 = 0/0/0`，而**四道门照样全绿**（0 也「有值」）。
  判别：`CPSL1=0` 且 B5/邮件里出现多个数量。动作：按「字段 / 现值 / 客户原文 / 应然值」汇报
  （应然 `CPSL1=3000 CPSL2=5000 CPSL3=10000`），**不许自己改 CPSL**；
  同时把三档数量**如实写进 CPMS 备注**（`报价三档：3.000/5.000/10.000 cps`）—— 概要行不含数量，
  若不写进备注，业务在产物里就完全看不到真实报价档位。
- ★ **纯文本邮件的裸行 `2 titles` 也会落 `TZS=1`**（2026-10-29 `Rossmann / Sudoku & Rätselbücher`）：
  1.0.44 加的款数兜底锚的是 `^(N) titles (` **带括号**形态，客户只写裸行 `2 titles`
  （后面直接换行接数量）就命中不了 → `TZS=1`，而 `1` 是合法值，四道门全绿。
  判别：正文逐行看有没有 `N titles` / `N Titel` 的独立行，与 `TZS` 对不上就是它。
  动作同下：按「字段 / 现值 / 客户原文 / 应然值」汇报，**不许自己改 `TZS`**。
- ★ **`X cps each (= Y cps in total)` 这种「每款 + 合计」写法 → `CPSL1=0`**（同日同单）：
  `parse_single_quantity` 只认 `=` 后总数 / `N x M` / 全串单数字三种形态，`each` 这种不算。
  ★★ 而且**客户自己可能算错账**：本次 `12,500 × 2 = 25,000`，客户写 `= 25,600`（÷2 = 12,800）。
  两个数都点名请客户确认，**不许挑一个写进 `CPSL1`，也不许挑一个写进 CPMS 当事实**
  （严格模式：客户没给出一致的数，就不能替他决定）。
  `flexocover + thread-stitching + square back + case-in book block` 同单仍落
  `CPLB=32.00;其他` / `ZDFS=""` —— 应然 `22.03;假精装书` / `精装书`（依据是同产品真实录入
  `CU26100601`），`cplb.map` / `binding.map` 缺这两个关键词，属实**已知引擎缺口**，报不改。
- ★ **NGV 的表可能「一个值横跨 5 格」→ 尺寸/页数/纸张/印色整片丢失（2026-10-29 `PR-00078025`）**：
  `B6 TPS` 后面是 `160 | x | 200 | mm | portrait`（C6:G6），`B8 paper` 后面是 `130 | gsm | woodfree…`。
  `section_pairs` 的 `value_mode: first` 只取第一格 → `values` 只剩 4~6 个键、`CD/KD/GD=0`，
  **而 `unmatched_labels` 是空的、四道门全绿**。判别：`profile_id=ngv` + values 键数少 +
  `unmatched_labels` 空 + 原文尺寸明明有。同一份表里这类行还有 `extent` / `printing` / `paper` ×3 处。
  修法是这些 entries 改 `value_mode: run`（向右拼到分区结束），**不是**放宽标签匹配。
  另：`offer_deadline` 也可能整条不进 values（本次 C2 `2025-01-22` 没抽到 → `Reminder` 空，
  过期提醒静默消失），交付前对一眼 `schedule` 分区。复盘见
  `references/ngv-multicell-values-and-dedupe.md`。
- ★ **中文文案别塞进 shell 命令**：`printf '中文说明…' > 文件` 会被安全扫描判
  `Confusable Unicode characters`（`tirith:confusable_text`）并挂起等审批。
  `mv`/`cp` 这类纯路径操作单独跑，说明文字一律用 `write_file` 写。
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
  ★ 2026-10-29 抓到一个新的写者：**后台 CPMS 补写任务**（不是定时 scan）。它会在你交付后几分钟内
  改写 `ERP_*.json` 的 `CPMS`，并**静默覆盖 `CPMS_来源对照.md`**。它的版本 lint 只出 1 warn、
  audit 0 无出处 —— 看着像合格，实际会（a）丢掉概要行的「书籍方向」、（b）把**上一版规格**的事实
  搬进新版 CPMS。判别：写完 20 秒再 `stat` 一次 mtime；`write_file`/`patch` 返回
  `_warning: modified by sibling subagent '<uuid>'`。交付前必须合并 + 重跑四道门 + 再 stat 一次。
  另外：`cpms_content_audit` 会误伤 `1.000 Sticker`、`A00152`（切成 `00152`）这类数字
  → 只在复合词/项目号里出现的数字**宁可不写、写进汇报**，不要改写成 `1000` 去骗审计。
  细节见 `references/lidl-anlage-表与并发补写.md` §3 / §4。
  完整配方 / 本次成品 CPMS / 写法决策表见 `references/cpms-backfill-gotchas.md`；
  **同一单出现多个任务目录 / 两个实例并发时怎么判「谁是交付件」、为什么已归档的重复件不许补 CPMS**，
  见总纲 `references/cron-concurrency-and-duplicate-tasks.md`。
  顺带一条（2026-10-29 实测）：**仓库常被并行的 subagent 同时改**（`patch` 会提示
  「modified by sibling subagent … but this agent never read it」）。补 CPMS 前后都 `git status` 看一眼，
  别把别人在途的档案/引擎改动当自己的回滚掉；发现**无关的红测试**（当时是 `lidl_anlage` 策略还没登记进
  `customer_profile` 白名单与 `test_ngv_profile` 断言）→ **不越界修、如实汇报「既有失败、与本次无关」**，
  只跑自己改动涉及的测试证明没弄坏东西。
- ✅ **灰板 `mm → g` 的审计冲突已修（1.0.51）**：客户原文 `boards 2 mm` → 规范要求写
  `1200g灰板`，旧审计按子串匹配判「`1200` 无出处」→ 逼人改回 `2mm`（又吃 `CPMS054`）。
  现在 `cpms_content_audit` 认 `GREYBOARD_G_TO_MM`（600g=1 / 900g=1.5 / 1200g=2 /
  1500g=2.5 / 1800g=3）：**照规范写 g，别为 audit 退让**。
  但仍只有这 5 档换算被认 —— 自己算的日期、其他派生的数字依旧会报「无出处」，那类要写进汇报而不是塞进 CPMS。
- ✅ **`cm ↔ mm` 换算已认（2026-10-29，IAN 574237/574239）**：Lidl 附表尺寸写 `20 x 20 x 2 cm`、
  `25 x 25,3 x 2,3 cm`（**小数用逗号**），而规范与 ERP 的 `CD/KD` 都是 mm（`200*200*0` / `250*253*0`）
  → 旧审计把概要行里的 `200`/`253` 判成「无出处」，逼人删必填行。现在
  `cpms_content_audit._unit_scale_equivalent()` 认 ×10 / ÷10 换算（只在「原文没有这个数字」时才查，
  且要求原文出现另一个单位词，离群数量蹭不过去）。回归：
  `test_客户写cm_CPMS写mm算可溯源` / `test_cm换算是双向核对_编的毫米数仍然报`。
  **遇到「概要行尺寸被判无出处」，先想单位换算，别删概要行。**
- ✅ **xlsx 日期单元格的「日号」溯源已修（2026-10-29，PR-00096460 实单）**：`read_source()` 以前
  `str(datetime)` 得到 `2027-04-30 00:00:00`，`normalize()` 删空白后粘成 `2027-04-3000:00:00`
  → 日号 `30` 前后都是数字，被判「无出处」，逼人把客户真实交期从 CPMS 里删掉（正是「为过门禁删真信息」）。
  现在日期读成 `2027-04-30`，单元格之间用 `|` 分隔（换行会被 normalize 删掉，相邻单元格数字仍会粘），
  `_greyboard_equivalent` 同步认 `2|mm`（数字与单位分处两格）。回归：
  `test_日期单元格的日号可溯源_不许把日期与时间粘成数字` /
  `test_灰板数字与mm分处两格仍认灰板换算`。**再遇到「明明原文有、audit 却说无出处」，先查语料读取，别删内容。**
- ✅ **audit 的 `--source` 已支持多文件 / 整个单元目录（1.0.49）**：一份报价是「规格表单 +
  `Titel+Mengen` + 补充说明」时，**指单元目录**，或用**一个** `--source` 跟多个文件
  （`--source a.xlsm b.xlsm`）当一个语料审。
  ⚠️ **重复写 `--source` 只保留最后一个**（argparse `nargs="+"` 覆盖，2026-10-29 实测：
   `--source a --source b` 只读了 b → 4 条「无出处」是假警报）。要多个文件就一个 flag 带全部路径。
  仍不支持 `.eml`：**邮件正文里的数字**（`PO#13826` / `2019` / `July 31st`）不能进 CPMS，
  邮件上下文进 `CPMS_来源对照.md` + 汇报，单据号归 `KHDH` 字段。
- ★ **`.xlsm` 附件会被静默跳过，正文单独成单 → 全字段落默认值**（2026-10-29 实单 IAN 574237/574239）：
  `ingest_email` 的后缀白名单不含 `.xlsm`（宏工作簿），Lidl/VEMAG 的「Overview of Countries, Quantities,
  and Titles」附表正好是 `.xlsm` → 记进 `skipped_attachments`、只把邮件正文当任务 → 抽到 **0 个规范键**，
  `CPLB=32.00;其他`、`TZS=1`、`CPSL1=0`、`CD/KD/GD=0`、`ZDFS` 吃档案默认，**四道门全绿**。
  判别：`_email_*.json` 的 `skipped_attachments` 非空 + `unit.members` 只有 `.txt`。
  这类单要么改后缀白名单/`input_unit` 支持 `.xlsm`，要么人工拆件重跑；**不要在 CPMS 里替它圆场**。
  同批还暴露两件事：① 客户识别在纯正文兜底时会判错（VEMAG 域名本该 → `ngv`，实测落 `igl`）；
  ② 一封邮件两个 IAN（两个独立产品）只能靠人拆成两份报价 —— 附表形态不是 enquiry 分区表单，
  引擎抽不出来，`CPMS` 只能由 agent 按附表手写。
  **附表结构 / 列位 / 材料英译（C1S→单粉、CCNB→粉灰、Artpaper→光粉）/ CPMS 成品取舍**：
  `references/lidl-overview-tables.md`。
  ↳ **同日稍后 `lidl_anlage` 抽取策略已落地**（`customers/ngv.yaml` + `extract_fields.py`）：
  实跑得 `CPLB=27.01;普通BB书` / `TZS=6` / `YYS=25` / `CPSL1=365340` / `CD=KD=200`（报错那套默认值不再出现）。
  三条必须记住的口径：① `TZS` = **去重标题**（不是 `total:` 那个 `Sortierung×款数`）；
  ② `YYS` = **去重语言码数**（不是国别数，25/24 就是这么来的）；
  ③ `packaging units ÷ 数量 = assortiment × 款数`（12 / 10），**这个才是装箱本数**，`M` 列常年空别当数据。
  多构造多款的 CPMS 分组块写法、`cm→mm` 审计修复、两条遗留字段疑点（`Drop_SJFX` 被门折页误推 /
  拼图书 `CPLB=32.00;其他`）见同一份 reference 的 **§6~§9**。
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
- [ ] `CPLB` 与真实业务一致（套装 29.04 / 贴纸骑钉书 24.03 / PAD头书 23.06 / **Wire-O → 26.07;普通YO书** / 单线圈 → 26.01;普通蛇圈书 / **`board book` → 27.01;普通BB书**，1.0.48 起档案已认；落 `32.00;其他` 先当缺陷报。**`ZDFS` 不跟着填** —— 客户没提装订方式就留空，见 §1.8）
- [ ] 套装单尺寸是 `0/0/0`，`Drop_SJFX` 空；非套装单：有 `N mm side` 才判长/短，**识别不到就是 `null`（空着算正常，不是缺陷）**
- [ ] ★ **品名三件套**（2026-10-20 起，替代旧的 `KM/Aldi …` 写法 —— 旧检查项已作废）：
      `CPMC` = **中文产品名**（agent 从 `Title:` 译，无占位符、无业务员/品牌/类别/FSC）；
      `ECPMC` = **原始产品名**（客户原文照抄，**不限语言** —— 德文 `Malbücher`、日文、法文一律照抄；
      业务 2026-10-21 澄清：ERP 字段 label 写「产品英文名」是历史命名，口径以这句为准）；
      `CPMC_ZH` = 与 `CPMC` 同值
      ★ 2026-10-29：**项目号不进 `ECPMC`**。邮件标题 `A00152 1.000 Sticker (AN)` 里
      `A00152` 是项目号、`(AN)` 是变体标记，产品名只取 `1.000 Sticker`（与业务真单
      `CPMC` 里的原文段一致）。项目号写进品名，既污染品名，也会让 `cpms_content_audit`
      把 `A00152` 切成数字 token `00152` 判「无出处」。
- [ ] `cpms_todo.py` 现在**四个字段都查**（2026-10-21 补）：`CPMS` 空 / `CPMC`·`CPMC_ZH` 是占位符 /
      档案声明了 `rules.ecpmc.source_keys` 但 `ECPMC` 空 —— `CPMS_TODO: 0` 才是真的写完。
- [ ] ★ **`question_answers` 内部确认清单**（schema v3，2026-10-30 起）：
      `cpms_todo` 会多报一条 `缺=确认清单(Q_XXX)` —— 卡交付（`blocking=true`）的项没确认完不许交付。
      收尾动作：把业务/客户确认过的事实写进 `answer`（`status` 可不动，填了 answer 就算已确认），
      并**回填到 `field` 指向的字段**；还没确认的保持 `open`。
      三类判据（2026-10-30 实跑 7 条定下）：① 语料库查到同一单 → `confirmed`，答案写「已出具单号 +
      日期 + 本目录为重复询价留档」；② 字段本来就该空（`KHDH` 询价阶段）→ `waived` + 写根因；
      ③ 真业务决策（客户要改数量/换纸）→ **不动**，汇报里点名。
      写答案用 `CQR_Web.answers.answer_question(...)`（与页面 `POST /api/answer` 同一实现，
      只改 `question_answers` + `Reminder`）；收尾 `cpms_todo.py` 归 0、`delivery_gates` 为空。
      ⚠️ `Q_KHDH` 八成是包了层皮的**识别失败**（档案落 `generic`/`igl`，`required_empty` 没生效）——
      关掉问题只是止血，必须在汇报里给「档案 / 现值 / 原文 / 应然值」。
      完整剧本：总纲 `references/closing-blocking-questions.md`。
      口径：`ZDFS` 这类「由业务补填、不猜」的项是 `blocking=false`（看得见但不堵路）；
      **CPMS 正文只留结论**，写「待确认」而没有 `field=CPMS` 的条目 → `cpms_lint` 报 CPMS080。
      业务也可以在**查看器详情页**直接填答案（`POST /api/answer`，2026-10-30 起）——
      agent 重跑前先重新读一遍页面/产物，别把手填的答案覆盖掉（写入带 mtime 乐观锁）。
      另：`Reminder` 字段现在是「卡交付未确认项」的文本快照，权威来源是 `question_answers`（见根 SKILL §4.8b）
      实测 `CU26070901` 邮件没有 `Title:` 标签 → 脚本取不到 ECPMC（WARN 留空）→ agent 从原文
      照抄 `Malbücher` 补上；igl 没声明 `rules.ecpmc`，空着是对的，清单不会误报。
- [ ] `YWY` = `KM`（业务员在 `YWY`，**不在** `CPMC` 前缀里）
- [ ] `KHDDH` = 香港单号（扫不到就空 = 正常）；`KHDH` 只在邮件里有时才填（**不许拿 our_ref 冒充**）
- [ ] 数量核对：带 `= 总数` 的写法以总数为准，且 CPSL1 是「款数 × 每款数」的结果；
      ★ 单元里有 `Titel+Mengen` / `Titel 1..10` 这类**款量附表**时，必须与它逐项对齐
      `TZS` / `YYS` / `CPSL1`（表单 B6 常只写 `see detailed breakdown`，会把三者打成 1/0/0）
- [ ] `ZDFS`：**映射命中才填**（Wire-O → 线圈书、perfect binding → 胶装/PAD书 等）；
      没命中 / 客户没提装订方式 → **留空是拍板口径**（2026-10-21，真实录入里「快线BB书」属业务补填），
      别为了让字段有值去猜，也别拿 `CPLB` 的名称回填 `ZDFS`
- [ ] `PXH` 空、`Remark3` 空、`SFYPJ`/`Remark1` 空（推不出就留空）
- [ ] ★ **11 条常用值已落库**（2026-10-30 起，见 §4.2b）：`BZFS` 纸箱包装 / `TBLX` 不打卡板 /
      `BZ` 美元 / `SHFS` FOB(NGV) / `ZXLB` 普通纸箱 / `ZBLX` A=B双坑纸板 / `ZXFS` 平装 /
      `DKMC` B3B坑纸板 / `YSLX` FOB(拼柜) / `SHDQ` FOB(NGV) / `chkBSYTB` **`false`**（2026-11-03 更正，
      语料 true 出现 0 次）。
      ⚠️ 其中 `SHDQ/YSLX/ZXLB/ZBLX/ZXFS/DKMC/BZ` **7 条在 3183 份真实 NGV 单据里从未填过**
      —— 属业务新要求，交付汇报里必须写成「按 2026-10-30 业务口径补填」，不许说「历史一致」。
      **括号一律半角**；跑完日志里应有 `客户默认值补空 11 个: …`（少一个就是被谁抢先赋了值，
      查那条「客户默认值**未覆盖**」日志）。`--validate` 必须过。
- [ ] `YYS`：单语言空 / 提取不到 0 / 多语言为真实数字
- [ ] `FSCMC`：单一材质才填，多材质留空
- [ ] `Remark4` 与 ERP 原文逐字一致（含错别字）
- [ ] `CPSX` = **JSON `null`**（附件没有年龄段栏 → 档案给「无」→ 按 `rules.null_values` 落 null；
      null 属空值，与权威导出一致，导入端按空处理）
- [ ] **`CPMS` 已由 agent 手写**（非空）；**严格模式**：只写客户文件里的事实，确认单/ERP 导出的补充信息不进 CPMS
- [ ] ★ **`cpms_content_audit.py` 退出码 0**（CPMS 里每个数字/单位/料号都能在客户附件里找到）
- [ ] ★ **门禁抓不到的三处，交付前人工过一眼**（2026-10-29 实测：一份 lint 0/0 + audit 无出处 0
      的 CPMS 同时中了三条）：① 概要行方向是否 = `Drop_SJFX`（为空就省略，**不许编**）；
      ② 装订段里有没有 `CPLB` 类别名；③ 客户文件里带**千分位**的实数（`1,000` / `112,266`）
      是否都抄全 —— `TOKEN_RE` 不认逗号，会切成 `1`+`000`，两个碎片在别处出现过就都算「有出处」，
      **整条事实漏抄永远不会报**。另：`A00152` 这类「字母+数字」项目号写进 CPMS **必定**被判
      无出处（`normalize()` 删空白后与后面的数字连成 `001521`）—— 放汇报/ERP 字段里就行，
      别为过审计改数字、也别往 `cpms_accepted.json` 白名单里塞技术性误报。
      盲区全文 + 邮件线程「哪一轮算当前生效」：总纲 `references/cpms-gates-blindspots.md`
- [ ] ★ 产物目录有 `CPMS_来源对照.md`（逐句出处 + 从确认单删掉的内容清单）
- [ ] ★ **字段级自检（四道门抓不到这一层）**：`cpms_lint` / `cpms_content_audit` / `erp_schema` / `pytest`
      校验的都是「已经有值的字段」，**整个字段空着照样全绿**。交付前逐字段过一遍
      「本该有值的是否有值」：`CPMC` / `ECPMC` / `CPMC_ZH` / `CPLB` / `ZDFS` / `CPSL1` / `CD` / `KD` / `DWMC` /
      `KHDDH`（可空但要知道为什么空）/ `Remark4`。空着的必须能说出依据，说不出的就是漏项。
- [ ] ★ **多记录单（数组里 N > 1 条）额外四查**（2026-10-30 实跑 IAN 574239 定下的）：
      ① `cpms_todo.py --dir <任务目录>` → **`CPMS_TODO: 0`**；
      ② `cpms_lint` 每条 0 error / 0 warn（输出带 `[k/N]`，别只看最后一条）；
      ③ `cpms_content_audit` 每条「无出处 0」—— 它报的「未写」是**别的记录**的材料
      （拆分后必然出现，不许搬过来凑）；
      ④ 每条 `CPLB` / `ECPMC` 是照**自己那条结构 / 款名**写的（引擎一律给
      `27.01;普通BB书` + 邮件级品类名 `Sound-Light books`，三张单会长得一模一样）。
- [ ] ★ **多记录单的字段允许集 = `{CPLB, CPMC, CPMC_ZH, ECPMC, SFYPJ, CPMS}`**
      （单记录单只有后三个）；写回前备份到 `/tmp`、断言键序不变，写完 20 秒再 `stat` 一次
      mtime（后台补写任务会改 `CPMS`）。
- [ ] ★ **页面能切 Tab**（多记录单唯一「肉眼级」证据）：`node CQR_Web/tests/render_real.js
      CQR_Web/templates/index.html http://127.0.0.1:8765 "<task>/ERP_….json"` → Tab 数 =
      `record_count`、每条「无未完成横幅」。配方见 `references/多记录重跑与逐条补字段.md`。
- [ ] ★ 被取代的旧任务目录已 `mv processed/_superseded_<日期>/`，并在里面写了
      `_SUPERSEDED.txt`（被谁取代 + 为什么）—— 别让一份报价在交付区躺两份。
- [ ] `ID` = 本次任务号（= 产物目录名；排查问题时拿它对日志）
- [ ] `compare_with_erp.py` 白名单外差异 0 条
