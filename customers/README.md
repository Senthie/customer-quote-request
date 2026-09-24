# 客户档案（customers/）

每个客户一份 YAML。**改客户的解析方式 = 改这里，不改 `main.py`。**

## 文件说明

| 文件 | 作用 |
|------|------|
| `_defaults.yaml` | 公共默认值 + **兜底档案**（自动识别失败时用它）。所有档案都 deep-merge 到它之上 |
| `_template.yaml` | 新客户骨架，复制它开始 |
| `igl.yaml` | IGL（已用真实样本校准） |
| `ngv.yaml` | NGV（**calibrated**：4 份真实附件 + ERP 导出校准，德英双语 enquiry 分区表单） |
| `<id>.erp_diff.json` | 该客户**已定口径差异**白名单，`compare_with_erp.py --accepted` 用 |
| `../skills/customer-quote-<id>/SKILL.md` | **该客户的子技能**（附件形态 / 口径 / 坑 / 交付检查清单）—— 新增客户时一并写，别把细节塞回总纲 SKILL.md |
| `_draft_<id>.yaml` | `calibrate_customer.py` 生成的校准草稿，仅供对照，不参与加载 |

## 新增一个客户：3 步

### 第 1 步：校准（拿样本喂给脚本）

```bash
cp customers/_template.yaml customers/<新客户id>.yaml
cd /home/claw/customer-quote-request
uv run ./scripts/calibrate_customer.py --customer <新客户id> --file <一份真实报价文件>
```

脚本会打印：

- 文件里所有「像标签」的单元格原样文本
- 已经能被现有档案识别的规范键
- **没被识别的标签**（这些就是你要补进 `fields:` 或改 `layout:` 的）
- 并写出 `customers/_draft_<id>.yaml` 供对照

### 第 2 步：填档案

按草稿把未识别的标签补进 `fields:`，把该客户与别人不同的口径写进 `rules:`。
只写**与 `_defaults.yaml` 不同**的部分。

★ **每个客户的附件类型不一样、格式也不一样**（业务 2026-09-24 强调）——
先判形态再动手：标签/值平铺（`auto_pairs` + `fields:`）还是分区表单
（`section_pairs` + `sections:`，见 SKILL.md 2.6）。**不要套用上一个客户的结构。**

顺手把该客户的**客户偏好表**（`preferences:`）补上：哪些字段固定填什么、
哪些字段就该空着（`value: ""` = 显式留空），每条都写 `reason`。
它和 `erp_fixed` 必须一致（`--validate` 会拦）。查看：`--prefs`。

### 第 3 步：回归

```bash
uv run ./scripts/main.py <样本文件> --customer <新客户id>
```

必须看到两行才算通过：

```
字段一致性断言: 通过（ERP 59 字段顺序与模板一致，其后追加内部字段 ['ID', 'CPMC_ZH', 'Reminder', 'version']）
选定客户档案: <新客户id>（来源: 显式指定 | 自动识别）
```

再逐个核对输出 JSON 的字段值是否合理（`CPMS` 除外 —— 那个永远由 agent 手写）。
想看产物结构对不对，跑 `uv run python -m erp_view` 在浏览器里看（SKILL.md 4.10）：
它会标出「缺哪些键 / 多哪些键 / 哪个值不在 ERP 枚举里」。

**有权威 ERP 导出时**（客户给的「标准答案」），做一次分类对照 —— 这是最省时间的
一步，它会把差异分成「口径已定 / 0 与空 / ⚠️ 需要核对 / ERP 侧多出的键」：

```bash
uv run ./scripts/compare_with_erp.py --customer <新客户id> \
    --attachment <样本附件> --erp <对应的 ERP 导出.json> \
    --accepted customers/<新客户id>.erp_diff.json
```

把「业务已拍板所以本该不同」的差异写进 `customers/<id>.erp_diff.json`（每条都要有理由），
剩下的「需要核对」逐条问业务。

### 第 4 步：写该客户的子技能

`status: draft` → `calibrated` 之后，照 `skills/customer-quote-ngv/SKILL.md` 的结构写一份
`skills/customer-quote-<id>/SKILL.md`（附件形态 / 识别特征 / 偏好表 / 字段来源 / 基线 /
坑 / 交付检查清单），并建软链接：

```bash
ln -sfn /home/claw/customer-quote-request/skills/customer-quote-<id>/SKILL.md \
        ~/.hermes/profiles/igl_agent/skills/customer-quote-<id>/SKILL.md
```

**客户细节只写在子技能里**，总纲（`SKILL.md`）只留引擎与机制 ——
`tests/test_skill_docs.py` 会检查「档案 ↔ 子技能一一对应」和「客户细节不回流总纲」。

## 字段速查

- 规范键（`fields:` 的键）清单 → SKILL.md「规范键 ↔ 客户列名」
- `rules:` 各段的含义 → SKILL.md「客户档案规范」
- 布局策略（`rules.layout.strategies`）→ SKILL.md「布局策略」
- **ERP 合法取值（下拉枚举）** → `assets/erp_form_schema.yaml`，
  查值：`uv run ./scripts/erp_schema.py --field <字段名>`
- **产品类别（`rules.cplb.map` 的 `node` 编号）** → `assets/cplb_tree.yaml`，
  查树：`uv run ./scripts/cplb_tree.py --search <关键词>` /
  `--suggest "<客户文件里的描述>"`；可视化选取器 `assets/cplb_tree.html`
- **`rules.cpsx`（产品属性）** → 年龄段 + 安全检查 → ERP 中文枚举，阈值可覆盖；
  与 igl 口径不同时才需要写（默认在 `_defaults.yaml`）。
  `when_missing` = 推不出年龄段时落什么（默认空 = 留空 + WARN；NGV 按业务口径写 `无`）
- **`rules.enum_exceptions`（口径例外）** → 业务拍板用、但**不在 ERP 枚举里**的**字符串**取值
  （`{field, value, reason}`）。声明后校验只报 WARN（带原因）而不是 error；
  不声明就会**每一单**都红一次、把「0 error」闸门变成噪声。
  ⚠️ **当前没有客户在用** —— 业务说「无」走 `rules.null_values`（见下），别往这里塞非法字符串
- **`rules.null_values`（「无」→ JSON null）** → 业务 2026-09-24：「CPSX 或者其他值的『无』
  使用 json 里的 null 填充」。整格恰好等于声明里的词就落 `null`（公共默认 `["无"]`；
  ERP 自身导出里空值也是 null，null 属空值、不触发枚举校验）。想关掉写 `[]`
- **`rules.required_empty`（该客户本来就该空的字段）** → 不报必填 error。
  NGV：`KHDH` / `KHDDH`（香港单号默认空）/ `Drop_SJFX`
- **`rules.deadline.warn_when_expired`** → 报价截止日期过期时是否提醒（默认 `true`，
  取规范键 `offer_deadline`；结果落内部字段 `Reminder` + `validation.json` 的 `reminders`）
- **`rules.cpmc`** → `CPMC` 拼装。`template` = **英文表达**（占位符 `{salesperson} {cn_name}
  {cplb} {cplb_name} {product_name} {series_name} {fsc} {our_ref}`）；
  `zh_template` = **中文表达** → 落内部字段 `CPMC_ZH`（未声明则 `CPMC_ZH` 为空）；
  `salesperson` 是该客户固定的业务员（NGV = `KM`）；`cn_name` 为空时脚本填
  `【中文品名待定】` 并在日志点名要求 agent 补全
- **内部导出字段** → `assets/internal_export_fields.yaml`：`ID`（本次任务号，方便日志追溯）、
  `CPMC_ZH`（产品名称中文表达）、`Reminder`（给业务的提醒）。它们**不是 ERP 字段**，
  追加在 59 键之后、不参与字典校验；`--strip-internal` 可只输出 59 键

## 新增客户时最好一并确认的两段

新客户除了 `fields:` 别名表，这两段值得花 5 分钟确认，否则类别/属性会走兜底：

```yaml
rules:
  cplb:                       # 客户文件里的写法 → 产品类别树节点编号
    map:
      - { match: "board\\s*book|纸板书", node: "27.02" }
      - { match: "perfect\\s*bound|paperback", node: "23.01" }
  cpsx:                       # 产品属性：年龄段 + 安全检查（与默认不同才写）
    threshold: 3
    above: 三岁以上
    above_with_test: "3岁以上（要寄第三方测试）"
```

> 所有 `node` 编号必须在 `assets/cplb_tree.yaml` 里 —— `tests/test_cplb_tree.py`
> 会逐个档案核对；写错的编号会被它拦下来。

## 四条铁律

1. **不要在 `main.py` 里写任何客户的列名。** 列名只属于档案。
2. **改 `_defaults.yaml` 等于改所有客户**，必须对每个客户档案各跑一遍回归。
3. **`status: draft` 的档案不能被信任** —— 自动识别到 draft 档案时，日志会打 WARN，
   输出照样生成，但字段值需要人工核对。
4. **档案里映射到 ERP 下拉字段的值必须是合法枚举**（`erp_fixed` / `binding.map` /
   `materials` / `cplb.map` 的节点编号等）。写错的值导入时会被静默丢弃 ——
   历史单据 `ZDFS="无线胶装"` 就是这么错的，合法值是 `胶装/PAD书`。
   `main.py` 步骤 5b 会归一化 + 校验并写 `.validation.json`。
   **万一业务说的是「无 / 没有」**：不要往枚举里塞「无」这个非法字符串 ——
   用 `rules.null_values: ["无"]` 落 JSON `null`（`null` 属空值，天然不触发枚举校验，见 SKILL.md 4.9）。
   只有「业务坚持要用枚举外的**字符串**值」这种真例外，才用 `rules.enum_exceptions`
   声明 `{field, value, reason}`（机制保留备用，**目前没有客户在用**）。
