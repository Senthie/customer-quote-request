# 内部用词表 —— 两套表的分工与维护

## 两套表

| 表 | 位置 | 形态 | 谁用 |
|---|---|---|---|
| 单词表（业务上传） | `docs/华辉内部中英单词表.txt` | `EN\tZH`，639 行 | `scripts/glossary.py` 机器查；`--check` 体检 CPMS 用词 |
| **内部用词对照表** | `docs/内部用词对照表.md` | 四列：客户英文原文 / 内部用词 / 直译·常见误译 / 出处 | 人读。写 CPMS、起产品名、填字段判定时先查这张 |

**关系**：单词表是「英文 → 内部中文」的机器可读映射；对照表多两列 —— **常见误译**和**出处**，专门治「字面译对了但工艺不是那个东西」的错。对照表里能机器匹配的条目，同步追加进单词表，让 `glossary.py --check` 也能拦住。

## 用法

```bash
# 写 CPMS 前查一个词
uv run ./scripts/glossary.py --lookup "googly eye"

# 写完体检：报告词表外的中文写法
uv run ./scripts/glossary.py --check "<CPMS 正文>"

# 反查：这个中文词表里有没有
uv run ./scripts/glossary.py --zh 滚金边
```

对照表按章节查：A 材料用纸 / B 装订工艺 / C 配件结构 / D 送货交期 / E 单证商务 / F ERP 字段判定 / G 命名与固定值。

## 冲突处理

三套口径打架时，优先级 **ERP 实单写法 > 单词表 > 我的直译**。已知冲突：

| 词 | 单词表 | ERP 实单 | 取 |
|---|---|---|---|
| 书口刷金 | `3 edges golden gilt → 三面烫金边` | 书口三边滚金边 | **滚金边**（三面烫金边是封面三面烫金，另一工艺） |
| 书头布 | `headband → 笃(绳)头布(H.T)` | 书头布 | 书头布；笃头布作别名 |
| 烫金 | `foil stamping → 烫色` | 烫金 | 同一单内只用一种 |
| slipcase | `french fold jacket/slipcase → 法式书套` | 书套盒 | **书套盒**（法式书套是 french fold，不是 slipcase） |

## 维护规则

1. 新词条必须带出处（单号或邮件），推断词不进表。
2. 只收出现在 CPMS / 品名 / 检测项目 / ERP 下拉判定里的词；产品题材、客户自造书名不进。
3. 每发现一次「照字面译错」，就把正确写法和错例一起补进去 —— 只记正确写法拦不住下次。
4. 同步动作：对照表加条目 → 能机器匹配的追加到 `docs/华辉内部中英单词表.txt` → `glossary.py --lookup` 验一遍能查到。

## 2026-11-24 这轮补进来的词（24 条）

`googly eye(s)` 活动眼(眼仔)、`board book binding` 板纸书装订(BB书装订)、`slipcase` 书套盒、`rounded corner/edge` 圆角、`head and tail band(s)` 书头布+书尾布(笃头布)、`artboard` 粉咭、`text paper` 内文纸、`jobbing paper` 书纸、`creamy` 米色/奶白、`Munken` 蒙肯纸、`extent` 页数/P数、`bill of material`/`BOS` 物料清单、`hidden 2-sided color` 隐藏双面印色、`waterbased varnish` 水油、`matt lamination` 哑胶、`gloss lamination` 光胶、`hot foil stamping` 烫金/烫色、`gold-edged pages` 书口滚金边、`die-cut the opposite pages` 对页啤穿、`self-purchased` 自厂购、`spine: straight` 方脊、`scratch-resistant` 防刮。
