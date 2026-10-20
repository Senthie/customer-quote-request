# 实单复盘：`BJXQ260704002` / `PR-00091345`（严格模式的由来）

> 2026-10-19 · 线圈书 + 钻石贴纸装配 · NGV（TCHIBO 线）
> 业务给了这份**已在 ERP 确认过的单据**，用途是让 agent 学「客户给了什么就写什么」。
> 我一开始理解偏了：把它当「补全清单」，把确认单里的信息也写进了 `CPMS`。
> 业务当场纠偏 —— **CPMS 只写客户文件里的事实，没有就是没有**。本文留档，防复发。

---

## 1. 附件里到底有什么（59 个单元格的事实全集）

| 位置 | 内容 |
|---|---|
| B1 / K1 | `Basteln&Gestalten Diamantsticker Nageldesign` / `SST` |
| B2·C2 | `offer deadline` → `2026-06-26` |
| B5 | `3 x 7000 cps. = 21.000 cps` |
| B6 | `3 titles, 1 language` |
| C7·E7·G7 | `210` / `280` / `portrait` |
| C8 | `42 pp + Cover` |
| B24（specials） | 42pp 构成（8pp 模切 230gsm C1S 4/0c+1/s 哑油；2pp 贴纸 80gsm 可移 kiss-cut 50-60/款；32pp 100gsm offset 4/4c）；钻石贴纸 180×214、570 颗（90 花 8mm / 168 星 6mm / 312 圆 6mm）、polybag 1/0c、2 点可移胶固定封面 p2；封面 400gsm C1S 4/0c+UV、按款开窗；Wire-O 符合 EN71-1 弯线头；书口右侧透明封口贴 1/0c |
| C15·C16·C17·C21·C22·C23 | `400 gsm art card C1S (FSC Mix)` / `4x0 col.` / `UV varnish` / `Softcover` / `Wire-O Binding` / `square` |
| I9·I11·I13·I15 | `FSC mix`（内文 / 环衬 / 纸板 / 封面） |
| I22 | `280 mm side` |
| J9·K9·K10 | `vol.` `1.25` `mm bookblock thickness` |
| C25 / C26 / C27 | `foil package (1/0c)` / `price sticker (Ø 40 mm, 1/0c PMS)` / `18 cps` |
| C29 | `3 x 1 cpy per foil package, 6 foil packages per carton` |
| F27 | `export carton TCHIBO style(100% recycling, 1/0c)` |
| B31·F31·I31·B32 | `Ebers, Lea` / `12.06.26` / `PR-00091345` / `37` |

**附件里没有的**：窗口尺寸、背面过光油、钻石材质与厚度、颜色数、行列排布、OPP 袋厚度与透气孔、
背卡、封口贴规格与料号、价格贴料号与面底结构、指定供应商与纸箱强度、封箱胶带、箱唛、供料责任方。

---

## 2. 我写错在哪（合并版 vs 严格版）

| | 合并版（错） | 严格版（对） |
|---|---|---|
| 篇幅 | 1040 字 | 521 字 |
| 信息源 | 附件 + 业务确认单混写 | 只有附件 |
| 典型句子 | `窗口约100*100mm`、`钻石PS聚苯乙烯+透明PET，0.07mm(7丝)`、`背卡250g单粉(FSC)…由华辉提供` | `按款开窗啤`（尺寸不写）、`570颗水钻贴`（材质不写） |
| 风险 | 客户没说的被当成客户要求 → 报错价 / 做错货 | 只承担客户明确写下的责任 |

合并版看着「更专业」，实际是**替客户做了决定**：窗口大小、材料、颜色数、供应商全是我们补的，
一旦与客户真实意图不符，返工成本由我们承担。

---

## 3. 现在怎么防（工具与规则）

1. **规则**：`skills/.../SKILL.md` 第 5 节「严格模式」；`docs/CPMS_规范与样例集.md` 5.1 节。
2. **范本**：同文档样例 7 = 本单严格版，且被 `tests/test_cpms_doc_samples.py` 钉住（文档即测试）。
3. **机械门禁**：`scripts/cpms_content_audit.py`

```bash
uv run ./scripts/cpms_content_audit.py --cpms <ERP.json> --source <客户原始文件>
# 退出码 = 无出处条目数，必须 0
```

它抓两类：`无出处`（CPMS 有、客户文件没有 → 必须删）与 `未写`（客户文件有、CPMS 没写 → 人判）。
实测：本单严格版 **0 无出处**；合并版被它抓出 8 处（`100*100`、`30mm`、`65g`、`1600kPa`…）。

---

## 4. 结论一句话

**CPMS 是「客户要求的转录」，不是「我们对这单的理解」。**
客户的空栏就是空栏，客户的估值就是估值，客户的 `polybag` 不许写成 `OPP`。
业务侧的补充信息（供应商、强度、料号、责任方）进 ERP 字段或邮件回复，不进 CPMS。
