# 客户 ERP 常用值：从 18476 份真实单据反推 `erp_defaults`

> 谁需要读：要给某个客户加/改 `erp_defaults`、或者怀疑「这个默认值是哪来的」的人。
> 一句话：**客户习惯不是拍脑袋定的，是从 6 年真实 ERP 报价单里统计出来的，而且门槛是两个率。**

## 1. 真值源在哪

```
/home/claw/train/custom_cpms_requie/<单号>/<单号>_erp.json
```

- 18476 份，2020–2026，每份是**一条** ERP 报价需求记录（59 键，与我们导出的字段同名）
- 210 个客户资料代码（`KHZL`，形如 `NGV10` / `IBL10` / `FES 22`）
- 不在仓库里（152MB），但任何字段都能随时重算，不需要把数据拷进来

**语料 = 报价阶段（BJXQ）的 payload**，不是生产/出货单。这条决定了下面最重要的一个结论。

## 2. 实测事实（2026-11-03 全量统计）

| 字段 | 填充率 | 说明 |
|---|---|---|
| `BZFS` `TBLX` `SHFS` `SFYPJ` | 99–100%（2023+ 全部 100%） | 报价阶段唯一稳定有值的字段 |
| `BZ` `CPDL` `SHDQ` `YSLX` `ZXLB` `ZBLX` `ZXFS` `DKMC` | **0/18476** | 六年一份都没填过，2026 年新单照样空 |
| `chkBSYTB` `chkZDBJF` `chkBSYJZX` | 100% 填充但**恒 `false`** | `chkXBJF`、`Drop_SJFX` 恒 `null` |

推论：

- 报价阶段只需要包装/台板/送货这三个（+ 配件 `SFYPJ`）。`ZXLB/ZBLX/ZXFS/DKMC` 是生产阶段才填的。
- **勾选框没有任何信息量**。曾以为 `TBLX=不打卡板` ⇒ `chkBSYTB=true`（同一件事两种写法），
  语料直接证伪：TBLX=不打卡板的 7236 份记录 `chkBSYTB` 全是 `false`，全语料 `true` 出现 **0 次**。
  所以 `spec_fields.yaml` 里 `chkBSYTB` 标 `derivable: false`，只跟档案。

## 3. 枚举校准：2023 年分界

ERP 在 2023 年做过一次下拉规范化。**2023 年之前的值是废弃写法，不能收录**：

| 字段 | 2022 及以前（废弃） | 2023+（现行，与 schema 完全一致） |
|---|---|---|
| `TBLX` | `不用打卡板`(813)、`木卡板(1.2*1m)`(403)、`欧洲板(1.2*0.8M）`(365) …共 7 种 | 9 种，全部在 schema 内 |
| `BZFS` | 同现行 4 种 | `纸箱包装` `牛皮纸包装` `胶袋包装` `卡板摆放` |
| `SHFS` | 同现行 14 种 | 14 种（schema 里的 `DAT` 从未用过） |

所以统计一律 `--since 2023`。`scripts/audit_erp_corpus.py --enums --since 2023` 会做双向差分：
真实有而 schema 没有的（ERP 会**静默丢弃**）、schema 有而从未用过的。

## 4. 判定门槛：两个率，不是一个

```
覆盖率 coverage = 有值单据 / 全部单据      —— 防「这字段本来就没人填」
一致率 rate     = 众数次数 / 有值单据      —— 防「每单都在变」
采纳 = coverage ≥ 0.9 且 rate ≥ 0.9
```

为什么不能只看一个率：

- 只算「出现率」→ `SHDQ` 的众数是空值、出现率 100%，会被当作「常用值 = 空」；
- 只算「有值里的一致率」→ IGL 的 `TBLX` 不打卡板占 34%，但覆盖率高，容易被误当默认值。

实测对照：

| 客户 | 字段 | 覆盖率 | 一致率 | 结论 |
|---|---|---|---|---|
| IGL（IBL10，2520 份） | `BZFS=纸箱包装` | 100% | 100% | 采纳 |
| IGL | `SHFS=FOB 盐田` | 100% | 99% | 采纳 |
| IGL | `TBLX=不打卡板` | 100% | **34%** | 不采纳（分布分散，问客户） |
| NGV（NGV10+NGVS16，2677 份） | `TBLX=不打卡板` | 100% | 99.96% | 采纳 |
| NGV | `SHFS=FOB(NGV)` | 100% | **86%** | 低于门槛，但业务 2026-09-24 已定 → 保留并注明依据 |

**业务口径可以越过阈值，但必须在档案注释里写明「低于阈值、依据是业务要求」**，
不能让人以为它是从数据推出来的。

## 5. 三个坑

1. **布尔会被字符串化**。JSON 里是 `false`，统计时 `str()` 成 `'False'`，写回档案就变成字符串，
   ERP 当非法值静默丢弃。工具的 `coerce()` 负责还原，测试钉住（`test_草稿里布尔是真的_false`）。
2. **`erp_fixed` 与 `erp_defaults` 不能共存同一个字段**。前者无条件覆盖、后者只补空，
   同时写等于默认值静默失效。校验器会拦。
3. **`preferences` 是同一份事实的第二视图**，改了 `erp_defaults` 必须同步，否则校验报错
   （`chkBSYTB` 从 true 改成 false 时就是这么被抓出来的）。

## 6. 操作手册

```bash
# 看某个客户统计结果（不动文件）
uv run ./scripts/derive_customer_defaults.py --customer ngv

# 产出可并入档案的草稿（带未采纳原因）
uv run ./scripts/derive_customer_defaults.py --customer igl

# 批量建档：单据 >100 份且还没有档案的客户
uv run ./scripts/derive_customer_defaults.py --bootstrap --min-records 100

# 校验 + 回归
uv run ./scripts/customer_profile.py --validate && uv run pytest -q
```

改默认值的连带影响（四处，缺一不可）：

1. `customers/<id>.yaml` 的 `erp_defaults`
2. 同档案的 `preferences`（人看的，校验器会比对）
3. `tests/test_*_profile.py` 里对该字段的断言（端到端基线）
4. 对应子技能 `skills/customer-quote-<id>/SKILL.md` 的口径说明（仅 status: calibrated 的客户需要）

## 7. 批量建档的边界

- **阈值（业务 2026-11-03 森西拍板）**：语料里单据 **> 100 份**才建档；**≤ 100 份明确不建档**
  （175 个客户 / 2399 份，平均 13 份，统计不可信）—— 等真实询价来了再按 README 的 3 步走。
- `--bootstrap` 只写**语料能证明的部分**：`erp_defaults` + `erp_khzl` + notes 里的语料画像。
- `fields / detect / rules` 一律留空，`status: draft` —— 没收到真实询价文件，写解析规则就是猜列名。
- 子技能只对 `status: calibrated` 强制要求（`tests/test_skill_docs.py::calibrated_profiles`），
  否则批量建档会灌出一堆空壳技能文档。
- 第一份询价来了：`calibrate_customer.py` 校准 → 填 `fields/detect/rules` → 回归 → 改 `status: calibrated`。

## 8. 跑样本的坑（会移动文件）

`main.py` 跑完会把**输入文件移走**归档。拿仓库里的 `customer-files/samples/` 或
`customer-files/processed/` 下的样本直接跑，会把回归测试用的样本搬去归档目录，
导致 `test_pipeline.py` 全部 skip。必须：

```bash
cp "<样本>" /tmp/scratch/pending/          # 先拷出来
uv run ./scripts/main.py /tmp/scratch/pending/<样本> \
    --processed-dir /tmp/scratch/processed --failed-dir /tmp/scratch/failed
```

（`customer-files/samples/` 是 `.gitignore` 掉的客户原文，丢了找不回。）
