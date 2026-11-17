---
name: customer-quote-ullmann
description: Ullmann Medien（德国）询价的专属知识 —— 纯文本德文「款名表 + 七行规格」怎么写、哪些词是增价项不是规格、交付前查什么。处理 Ullmann 报价时配合总纲 skill customer-quote-request 一起加载。
metadata:
  openclaw:
    emoji: "🇩🇪"
    requires:
      bins: [uv, python3]
---

# Ullmann Medien 报价需求（子技能）

> 总纲：skill `customer-quote-request`（引擎、字段总览、CPMS 规范、推送 runbook）。
> **唯一真源：`customers/ullmann.yaml`** —— 改行为改档案，不改文档，也不改代码。
> ERP 客户资料代码 `UMG19`（语料 824 份，其中 2023+ 的 787 份用于统计默认值）。

## 1. 附件长什么样

**没有表格附件**，正文就是一封德文规格清单（纯文本，`.txt` 或邮件正文）：

```
ich bitte Sie um ein Angebot einer Aktion im Umfang von 4 Titeln:

No.   Title                          ISBN            Edition
01    Crime Mysteries Mordakte       9783741529627   4.000
02    Rätseluniversum Einstein       9783741529634   4.000
…
total                                                16.000

Format:             140x198mm
Extent:             T01+03: 224pp content +4pp cover
                    T02+04: 256pp content + 4pp cover
Content:            4/4c (euroscale) on 120 gsm Offset paper, FSC mix
Cover:              4/0c (euroscale) on 300 gsm C1S artboard, FSC mix + 1/s matt lamination + UV-spot varnish
Binding:            Softcover, perfect binding
Packaging:          title by title packed in jumbo cartons, stacked on europallets (EPAL)
Delivery:           FOB Yantian; date: tbc
Bitte senden Sie mir das Angebot bis zum 28.10.26 zu.
```

布局是 `标签:            值` 的**平铺键值对**（`text_lines`），引擎自动识别，不需要额外配置。
款数写在散文里（「im Umfang von 4 Titeln」）、总数写在 `total` 行 —— 两条都走档案的
`rules.scan` 正则，不靠标签。

## 2. 字段怎么来的

| 字段 | 来源 | 说明 |
|------|------|------|
| `TZS` | `rules.scan` 的「im Umfang von N Titeln」 | 客户有时写 `4 Titeln`、有时 `4 Titel` |
| `CPSL1` | `rules.scan` 的 `total 16.000` ÷ 款数 | **CPSL 存单款**（森西 2026-11-16），除不尽就留总量 + 待确认，不许四舍五入 |
| `CD`/`KD` | `Format:` 一行 | 按**书写顺序**：`140x198mm` → CD=140 / KD=198（不比较长短边） |
| `GD` | 不做推算 | 语料 818/824 都是 0 → 档案 `thickness.enabled: false` |
| `ZDFS` | `Binding:` 行 + `rules.binding.map` | `Softcover, perfect binding` → 胶装/PAD书 |
| `CPLB` | `rules.cplb.map`（在 binding/format 里找关键词） | perfect binding → `23.01;无线胶装书`；锁线/精装各有节点 |
| `BZFS` / `SHFS` | 档案 `erp_defaults`（语料 100% / 94%） | 纸箱包装 / FOB 盐田 |
| `TBLX` | **不设默认** | 语料里「不打卡板 472 vs 欧标松木卡板 337」互不相让 → 逐单看原文或问客户 |
| `CPMC` / `CPMS` | **agent 手写** | 引擎只给占位符 |

## 3. 坑

1. **增价项不是规格**。客户常在规格后面追加问价：
   「Mehrkosten für eine **Fadenheftung**」（锁线）、「**80mm breite Klappe**」（勒口）、
   「**Farbschnitte** 1c/4c」（书口刷色）。
   这些**不是本单的装订/工艺**，写成 `ZDFS` 会覆盖真正的 `Softcover, perfect binding`
   —— 档案的 `binding.map` 刻意**不收录** `sewn` / `Fadenheftung`。写进 CPMS 的
   「备注/增价项」段，字段不动。
2. **两个 Extent**。`T01+03: 224pp` 与 `T02+04: 256pp` 是不同款的页数。引擎只取一个
   → CPMS 里两款分别写清楚，别让业务以为全单同页数。
3. **`total 16.000` 是德式千分位**（= 16000），不是 16。引擎已按德式解析；手算时别当成 16。
4. **报价截止日在散文里**（「Angebot bis zum 28.10.26」），不在标签行；
   档案 `rules.scan` 有 `offer_deadline` 正则，抽不到就问客户，别默认一个日期。
5. **`euroscale` 是 NGV 的特征词**，Ullmann 正文也会出现。识别不会串：Ullmann 靠
   `Ullmann Medien` / `im Umfang von` / `europallets` 等独有标记，NGV 要求 ≥2 个特征命中。
6. 客户偶尔用 `PAD` 系列（语料有 `PAD头书` / `PAD头书(全封面)`）→ CPMS 按原文写，
   `CPLB` 用 `rules.cplb.map` 的节点，别自己拼编号。

## 4. 交付检查清单

```bash
# 1) 单份跑通（会自动识别成 ullmann）
uv run ./scripts/main.py <客户文件或目录> --today YYYY-MM-DD
# 2) 校验 59 键 + 枚举
uv run ./scripts/erp_schema.py --check <产物>.json --customer ullmann
# 3) CPMS 双闸门
uv run ./scripts/cpms_lint.py <产物>.json
uv run ./scripts/cpms_content_audit.py --cpms <产物>.json --source <客户原始文件>
```

- [ ] `TZS` = 「im Umfang von」里的款数；`CPSL1 × TZS` = `total` 行的总本数
- [ ] `CD`/`KD` 与 `Format:` 的书写顺序一致；`GD` = 0
- [ ] `ZDFS` 来自 `Binding:` 行；若落的是占位值，`question_answers` 里必有 `Q_ZDFS`
      且 CPMS 里写了「装订方式无法判断值，需业务自行判断」
- [ ] `CPLB` 是类别树里的节点、并且与装订方式自洽（锁线 ≠ 无线胶装）
- [ ] CPMS 写了增价项（Fadenheftung / Klappe / Farbschnitte），但没写进 `ZDFS`
- [ ] 两个 Extent 在 CPMS 里分开写清楚
- [ ] `CPMC`/`CPMC_ZH` 是中文品名（agent 手写），`ECPMC` 照抄客户原文（这里就是德文）
