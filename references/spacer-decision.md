# 夹层（Spacer）判定与补厚口径

权威来源：森西 2026-11-24 口述。工具：`scripts/spacer_calc.py`。

## 一、要不要加纸 —— 三种情况，顺序判断

BB 书**可以做成薄款**，所以默认是「不加纸」。

| # | 询价里的信号 | 结论 |
|---|---|---|
| 1 | 客户明确提到要「加纸」 | **加纸**。同时给了厚度就按第 2 条落地；没给则目标厚度待定 |
| 2 | 给了「书脊厚度」（如 `spine: approx. 42mm`） | **加纸到该厚度**，T = 客户给的值 |
| 3 | 两条都没有 | **不插纸**（做薄款）。这是缺省值，不是"待问客户" |

第 3 条是最容易搞错的一条：以前以为「没给厚度=信息缺失，要问」，实际是「没给=客户要薄款，夹层留空就是对的」。**不要凭经验补厚度。**

## 二、坑：器件厚度 ≠ 书脊厚度

发声器 / 灯盒装在书的**右侧**，不在书脊上，所以：

- 器件厚度与书脊厚度**无关**，不能用器件厚度反推书脊厚度
- 只有业务或客户明确要求「书芯与器件齐平」时，才把器件厚度当目标厚度 T

反面案例：Hijaiyahku / Waktunya 两本，业务单上写「1500g 灰板 6 张」推出成品约 20.7mm，
而发声器厚 14.5mm。用 14.5mm 当 T 算出来只有 14.6mm，差 6mm —— 因为业务那个 20.7mm
是自己拍的经验值，客户材料里没有任何依据。这正是第 3 条要禁止的行为。

另外业务的写法是**按间隔位置分配**纸（"每张内文之间各放 1 张，共 6 张"），
不是"凑够某个总厚度"。两种逻辑不同，不要混。

## 三、补厚计算

```
已有层厚度 = Σ(纸种厚度 × 张数)      # 查 assets/page_thickness.xlsx
需补 Δ     = T − 已有层厚度
夹层方案   = 内部纸种组合出 Δ，偏差 ≤ 容差，张数最少
```

常用厚度（内部用纸表实测）：

```
灰板   600g→1.00  900g→1.50  1200g→2.00  1500g→2.50  1800g→3.00  2100g→3.50  (mm)
粉灰   250g→0.31  300g→0.36  350g→0.43   400g→0.48   450g→0.53   500g→0.64
单粉   210g→0.26  300g→0.41  350g→0.47   400g→0.54
```

「最便宜」：仓库**没有纸价表**，工具只保证「张数最少」（灰板为单位厚度最便宜的板材料）。
要按金额最优必须先有纸种单价表。

## 四、工具用法

```bash
# 判定要不要加纸（情况 3）
uv run ./scripts/spacer_calc.py --decide

# 客户提到要加纸但没给厚度（情况 1）
uv run ./scripts/spacer_calc.py --decide --spacer-requested

# 客户给了书脊厚度（情况 2）
uv run ./scripts/spacer_calc.py --target 14.5 --existing "单粉:300:10" --paper 灰板

# 从询价原文自动检测信号（字符串或文件路径）
uv run ./scripts/spacer_calc.py --decide --text "Spine width: approx. 42 mm"

# 列出全部纸种与厚度
uv run ./scripts/spacer_calc.py --list
```

`--json` 用于接流程。

### 参数说明

- `--decide`：只做判定，不给 --target 也能跑
- `--spacer-requested`：标记「客户提到加纸」，未给厚度时输出 `pending`
- `--device-thickness`：器件厚度，**仅当业务确认书芯与器件齐平时**才作为 T
- `--text`：询价原文（字符串或文件路径），自动检测「加纸 / 书脊厚度」
- `--tolerance`：允许超出目标的容差，默认 0.4mm

## 四之二、已接进报价准备流程（2026-11-24）

`scripts/main.py` 每次跑都会多出一个 **3c 步骤**，产物是 `<任务名>.spacer.json`：

```
[文件 3c/5] 夹层判定
  结论: 不插纸（薄款）—— 询价未提加纸、也未给书脊厚度 → 不插纸，做薄款（夹层留空即正确）
  输出: xxx.spacer.json
```

实现要点：

- 输入：`customer_data.get("spine_width")`（客户档案里已抽取的规范键）+ `text_blocks` 全文
- 判定：`spacer_calc.decide()`，与 CLI 同一套逻辑
- `spacer_needed = pending` 时**自动追加一条 reminder**（`spacer_thickness_missing`），
  进 validation.json 的 reminders，提醒业务补厚度
- `spacer_needed = yes` 时只给目标厚度，**补厚方案不由流程算**（已有层/内文张数在那一步不齐全），
  agent 拿到 target 后用 `spacer_calc.py --target` 算
- **不进 ERP 59 字段**：ERP 没有夹层字段，spacer.json 是报价准备信息

产物结构：

```json
{
  "spacer_needed": "yes | pending | no",
  "target_mm": 42.0,
  "reason": "询价给了书脊厚度 42.00mm → 加纸到该厚度",
  "evidence": ["Spine width: approx. 42 mm …"],
  "source": "客户书脊厚度",
  "spine_width_raw": "approx. 42 mm",
  "customer": "ngv"
}
```

### 已知边界

- 多产品询价按**整份**判定一次，不按产品分队（同封询价里各产品厚度不同的情况暂不细分）
- `evidence` 保留命中原文片段，自动判定必须可追溯 —— 复核时先看 evidence 再信结论
- 误判风险：德文 `Rücken` 也可能指书脊方向；`thicker` 可能指克重。命中后仍需人看一眼 evidence

## 五、和其它字段的关系

- 夹层不进 CPMS（规格 11 字段由 spec_fields 从原文推）
- 夹层是否计价取决于业务：只有确认加纸时才算材料成本
- 情况 3（不加纸）时，ERP 的装订方式仍按装订口径走（判断不出→落 ERP 下拉第一个合法值）
