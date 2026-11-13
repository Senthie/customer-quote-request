# 回归语料：9 份 zip 询价 × ERP 已有单

把 2026-11-13 那两个 zip 里的 **9 个独立询价**固化成回归语料：
我方抽取（`customer-files/processed/<task>/ERP_*.json`）×
ERP 实际单（`erp_quote_list` 快照）。

**不用系统报价需求单 PDF 做 ground truth** —— PDF 要 OCR、字段也不全。
ERP 列表页直接给：产品类别 / 规格 / 单位 / 数量（最小-中间-最大）/ 款数 / 语言数 /
装订方式 / 交货日期 / 预计下单 / 是否有配件 / 包装方式 / 产品类型。

## 文件

| 文件 | 内容 |
|---|---|
| `corpus.md` | 人读：逐单对账表 + 真差异清单 + 差异归因 |
| `corpus.json` | 机器可读：每单每个字段的 `{我方, ERP, 判定}` |
| `erp_truth_snapshot.json` | ERP 侧原始快照（按询价单号分组） |
| `../../scripts/build_regression_corpus.py` | 从快照 + 产物重新生成上面两份 |

## 重新生成

```bash
cd /home/claw/customer-quote-request
uv run python scripts/build_regression_corpus.py
```

## 刷新 ERP 快照

`erp_quote_list` 的关键字筛选**在服务端是粘性的**：先查过 `key=BJXQ260825007`，
后面不传 key 也还是这个过滤条件（返回 1 条），传 `key=""` 也清不掉。
能清的只有换一个**更宽**的关键字（`key=BJXQ`）。

另外 `limit` 硬上限 500，而 2026 全年 `BJXQ` 有 1848 条 → **必须按月切**。
抓完用 `scripts/build_regression_corpus.py` 同目录口径喂给 `TRUTH`。

本语料的 9 个单号（ERP 侧）：

```
CU26082501 → BJXQ260825007     CU26100601 → BJXQ261006005
CU26082801 → BJXQ260828001     WQ26100601 → BJXQ261006007
CU26092401 → BJXQ260924004     LG26070701-01 → BJXQ260707001-01
LG26080302 → BJXQ260803007     LG26082601 → BJXQ260826002
LG26090101 → BJXQ260901002（+ 子单 -01）
LG26090401 → BJXQ260904006（语料里没有它自己的邮件，见下方「语料错配」）
```

## 两个坑（已写进 corpus.md 的归因表）

1. **这 9 份的产物大多没有单号**：2026-11-10 那批是把 xlsx 直接喂 `main.py`，
   没走 `ingest_email.py`，所以 `KHDH`/`KHDDH` 都是空。本表按内容人工对号，
   不靠单号 join。要补单号得从原 `.eml`（`customer-files/inbox/20261113/`）重跑。
2. **`LG26070701-01` 目录里装的是 `LG26090401` 的邮件**（正文 sha256 完全一致）。
   ERP 里 `BJXQ260707001` 是 6款4语/45204，`BJXQ260904006` 是 10款10语/410000，
   而包里的邮件写的是 `10 titles in 10 languages` / `410,000pcs` → 只对得上后者。
   `LG26070701-01` 自己那封信不在包里，**别拿它去改抽取逻辑**。

## 当前基线（2026-11-13）

`30 一致 / 45 我方留空 / 15 真差异`（共 90 项比较）。

- 「留空」里 `JHRQ`/`WCRQ` 是脚本**故意不推测**，属预期；
- `YYS`/`CPSL1`/`CD`/`KD` 留空是**抽取漏了**，算缺陷；
- 真差异 15 条逐条归因见 `corpus.md` §差异归因。
