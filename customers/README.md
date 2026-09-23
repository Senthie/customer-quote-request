# 客户档案（customers/）

每个客户一份 YAML。**改客户的解析方式 = 改这里，不改 `main.py`。**

## 文件说明

| 文件 | 作用 |
|------|------|
| `_defaults.yaml` | 公共默认值 + **兜底档案**（自动识别失败时用它）。所有档案都 deep-merge 到它之上 |
| `_template.yaml` | 新客户骨架，复制它开始 |
| `igl.yaml` | IGL（已用真实样本校准） |
| `ngv.yaml` | NGV（**draft，待用样本校准**） |
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

### 第 3 步：回归

```bash
uv run ./scripts/main.py <样本文件> --customer <新客户id>
```

必须看到两行才算通过：

```
字段一致性断言: 通过（59 字段，顺序与模板一致）
客户档案: <新客户id>（来源: 显式指定 | 自动识别）
```

再逐个核对输出 JSON 的字段值是否合理（`CPMS` 除外 —— 那个永远由 agent 手写）。
通过后把 `status: draft` 改成 `status: calibrated`，并在 SKILL.md 的客户清单里登记。

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
  与 igl 口径不同时才需要写（默认在 `_defaults.yaml`）

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
