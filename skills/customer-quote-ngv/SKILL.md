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
| `CPLB` | `26.01;普通蛇圈书` | `binding_instructions = Wire-O Binding` 命中档案 `cplb.map` 的 `wire[- ]?o` |
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
| `CPMC_ZH` | `KM/Aldi 钻石贴纸美甲设计 普通蛇圈书/Basteln&Gestalten Diamantsticker Nageldesign(FSC Mix)` | 内部字段：**中文表达**；中文品名由 agent 补 |
| `ID` | 本次任务号 | 内部字段：= 产物目录名 = 日志里的「任务ID」 |
| `Reminder` | `报价截止日期 2026-06-26 已过期 90 天（基准日 2026-09-24） —— 请与业务确认这单是否还要报价` | 内部字段：把提醒固化进产物（1.0.23 起） |

本单还暴露了六个问题，均已修复：数量解析（第 9 节）、Wire-O 装订映射、`extras_found`
被纯标签污染、**`offer deadline` 被误当纯标题导致值被静默丢掉**、**`CPMC` 里中英混排**、
**提醒只留在日志里没进产物** —— 见 `SKILL.md` 1.0.20 / 1.0.21 / 1.0.22 / 1.0.23。

`ZDFS`：`骑马钉` / `套装` / `胶装/PAD书` / `线圈书` —— 看 `binding`（Softcover / ridged box）
**和** `Bindung`（safety saddle-stitched / perfect binding / lose）两行才够，
所以档案里 `binding.match_keys: [binding, binding_instructions]`。
`Drop_SJFX` 走 `spine.mode: binding_side`（附件给 `<N> mm side` 与较长边比较），
套装经 `spine.skip_tops: [套装]` 直接留空。

---

## 4. 口径（业务已拍板 —— 三批，共 19 条）

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
| 「业务员使用 **KM**」 | `CPMC` 的第一段固定写 `KM`（不再留 `【业务员待填】`）；**中文品名是每单的**，仍由 agent 交付前补 | `rules.cpmc.salesperson: KM` + `cpmc.template: "{salesperson}/Aldi {product_name}({fsc})"`（英文）+ `cpmc.zh_template: "{salesperson}/Aldi {cn_name} {cplb_name}/…"`（中文 → `CPMC_ZH`） |
| 「`KHDH`、`KHDDH` 香港单号默认是空」 | `KHDDH` 扫不到就是空，**不算缺陷**（不再报假 error）；扫到「香港单号 CU…」照常落库 | `rules.required_empty: [KHDH, KHDDH, Drop_SJFX]` |
| 「`CPSX` 如果没有获取到就显示**无**」 | 推不出年龄段 → 落「无」；**同日补充**：「『无』使用 json 里的 null 填充」→ 产物里是 **JSON `null`** | `rules.cpsx.when_missing: 无` + 全局 `rules.null_values: ["无"]`（null 属空值，不触发枚举校验 → 1.0.21 的 `enum_exceptions` 声明已删） |
| 「超过了报价截止日期，你记得提醒」 | 跑完在 stdout 末尾醒目提醒 + `validation.json` 的 `reminders` 段 + **内部字段 `Reminder`** | 引擎级 `check_offer_deadline()`（取 `offer_deadline` 规范键）+ `--today` 基准日期 |

> ✅ **`CPSX` 已无待确认项**：落 `null`（= 空值）与 4 份权威导出完全一致，
> 导入端按空值处理，不需要业务再拍板「无 vs 其他」。
> 唯一遗留是**内部字段**（`ID`/`CPMC_ZH`/`Reminder`）导入端收不收 —— 若报「未知字段」，
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
- ⚠️ **写单号正则要严**：`\b(?:po|order)\s*(?:no)?\s*([A-Z0-9]{5,})` 会把
  `Postkarten in Box` 的 `Po` 抓成 `stkarten` —— 必须要求出现 `no/nr/number/#` 这类编号词。
  有测试钉着（`test_客户单号正则不许被单词前缀误捕`）。

---

## 6. `CPMC` 存英文表达、`CPMC_ZH` 存中文表达

业务 2026-09-24：「将 `CPMC` 修改为存英文表达，使用 `CPMC_ZH` 来标注中文表达」。

档案声明**两个**模板（占位符相同）：

```yaml
cpmc:
  salesperson: KM
  cn_name: ""                                                    # 每单 agent 补
  template:    "{salesperson}/Aldi {product_name}({fsc})"          # → CPMC（纯英文）
  zh_template: "{salesperson}/Aldi {cn_name} {cplb_name}/{product_name}({fsc})"  # → CPMC_ZH
  needs_agent: true
```

```
CPMC    = KM/Aldi Basteln&Gestalten Diamantsticker Nageldesign(FSC Mix)          ← 英文
CPMC_ZH = KM/Aldi 钻石贴纸美甲设计 普通蛇圈书/Basteln&Gestalten Diamantsticker Nageldesign(FSC Mix)
```

- `CPMC_ZH` 的写法 = **1.0.21 之前 `CPMC` 的历史写法**（权威导出格式：`<业务员>/Aldi <中文品名>
  [ <中文类别>]/<客户原文>(<FSC>)`），只是换了存放字段 —— 例：
  `Corinna/Aldi B款 开脊PAD头书/Type B - A00125 Post- und Klappkarten - Klappkarten-Buch (4 titles)(FSC Mix)`。
- **业务员固定 `KM`**（业务 2026-09-24 拍板）→ 脚本自动带出，不用管。
- **中文品名是每单的**（不是客户常量）：脚本在 `CPMC_ZH` 里填占位符 `【中文品名待定】` 并打 WARN
  点名；交付时 **agent 直接改产物 JSON 里的 `CPMC_ZH`**（`rules.cpmc.cn_name` 是给「品名稳定」
  的客户偶尔用的开关，NGV 保持空）。
- **`CPMC` 里出现中文 → 当场 WARN**（脚本检查中文字符），不用等 ERP 报错。
- 中文类别取 **CPLB 树里的权威名**（导出里人写的 `其他套装` 与树里的 `其它套装` 不一致 ——
  统一取树里的，差异记在 `customers/ngv.erp_diff.json`）。
- `CPMC_ZH` 是**内部字段**（`assets/internal_export_fields.yaml`），追加在 59 键之后；
  导入 ERP 前若被告知「不收未知字段」，用 `--strip-internal` 只输出 59 键。

---

## 7. `Remark4` 客户常量：必须逐字照抄

NGV 每单固定的第三方检测清单（378 字符，3 份导出逐字相同）。照抄时注意 ERP 原文里的
**既有错别字与格式**：`monochloroo-` 是双 o、`(MCPD).Colour` 之间没有空格、
`foam` 前有一处换行。档案里用 `|-`（保留内部换行）而不是 `>-`。
「顺手改成正确拼写」会让每单都多出一条无意义差异 —— 踩过。

---

## 8. 回归与核对命令

```bash
cd /home/claw/customer-quote-request

# 单份处理（客户自动识别，或 --customer ngv 显式指定）
uv run ./scripts/main.py "customer-files/pending/<附件>.xlsx" --customer ngv

# 全量测试（含 NGV 合成用例 + 真实样本回归）
uv run pytest -q                              # 当前 210 项

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
- ★ **装订写 `Wire-O` 时 `ZDFS` 是「线圈书」**（不是「蛇圈书」—— 那是 CPLB 树里的类别名，
  两者别混）。档案 `binding.map` 已补 `wire-o / wiro / spiral / coil`。
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
- **`CPMC` 的中文品名同理** —— 业务员 `KM` 由脚本带出，**中文品名仍是每单 agent 补**。

---

## 10. 交付检查清单（NGV 专用）

- [ ] 客户识别是 `ngv`（不是 igl；注意文件名含 `Colour`/`Puzzle` 的陷阱）
- [ ] 59 键顺序断言通过、`.validation.json` 里 **0 error / 0 warn**
- [ ] ★ **报价截止日期**：`Reminder` 字段（和 stdout 醒目块）有内容 → **必须写进给业务的消息**
      （业务要求提醒；现已过期的单要问「还报不报」）
- [ ] `CPLB` 与真实业务一致（套装 29.04 / 贴纸骑钉书 24.03 / PAD头书 23.06 / 蛇圈 26.01）
- [ ] 套装单尺寸是 `0/0/0`，`Drop_SJFX` 空；非套装单尺寸、书脊方向正确
- [ ] `KHDDH` = 香港单号（扫不到就空 = 正常）；`KHDH` 只在邮件里有时才填（**不许拿 our_ref 冒充**）
- [ ] 数量核对：带 `= 总数` 的写法以总数为准，且 CPSL1 是「款数 × 每款数」的结果
- [ ] `ZDFS` 非空（装订类词如 Wire-O 已在映射表里；空的要先查是不是新写法）
- [ ] `PXH` 空、`ZBLX` 空、`Remark3` 空、`SFYPJ`/`Remark1` 空（推不出就留空）
- [ ] `YYS`：单语言空 / 提取不到 0 / 多语言为真实数字
- [ ] `FSCMC`：单一材质才填，多材质留空
- [ ] `Remark4` 与 ERP 原文逐字一致（含错别字）
- [ ] `CPSX` = **JSON `null`**（附件没有年龄段栏 → 档案给「无」→ 按 `rules.null_values` 落 null；
      null 属空值，与权威导出一致，导入端按空处理）
- [ ] **`CPMS` 已由 agent 手写**（非空）
- [ ] `CPMC` = `KM/Aldi <英文品名原文>(FSC Mix)` —— **纯英文**，里面没有中文字符
- [ ] `CPMC_ZH` = `KM/Aldi <中文品名> <中文类别>/<英文品名原文>(FSC Mix)`
      —— **中文品名已补、没有 `【中文品名待定】` 占位符**
- [ ] `ID` = 本次任务号（= 产物目录名；排查问题时拿它对日志）
- [ ] `compare_with_erp.py` 白名单外差异 0 条
