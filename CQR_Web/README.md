# CQR_Web

**客户报价需求（CQR）产物的 Web 前端**：把 `customer-files/processed/` 里导出的 ERP JSON
按业务视角摊开看 —— 字段分组、取值是否合法、交付闸门（CPMS 空 / 品名占位符）、原始文件清单。

> 现在**只有看，没有上传**（2026-10-20 从技能里抽出来之后的第一阶段）。
> 上传 / 编辑 / 多人协作是后续的编码任务，接口与目录结构已经为它们留好位置。

## 1. 它是什么、不是什么

| 是 | 不是 |
|---|---|
| 展示层：读产物 JSON、调 `data.py` 算好的结论、渲染页面 | ❌ 不解析客户文件（那是技能 `extract_fields.py` 的事） |
| 一个独立的可运行项目（自带 `pyproject.toml` / 测试 / README） | ❌ 不自己维护一份 ERP 字段字典（权威在 `../assets/erp_form_schema.yaml`） |
| 复用技能仓库的 `scripts/erp_schema.py` 做取值校验 | ❌ 不在页面里做任何判定（合法性、交付闸门都算在服务端 `data.py`） |

**为什么还要依赖技能仓库**：字段字典、合法枚举、交付闸门的口径必须只有**一份实现**，
复制一份就等着两边漂。CQR_Web 只拿走「展示」这一层，判定仍调技能的 `erp_schema`。

## 2. 起服务

```bash
# 在技能仓库根目录（推荐：uv 环境里已经有 flask/pyyaml）
uv run python -m CQR_Web                    # 127.0.0.1:8765 + 自动开浏览器
uv run python -m CQR_Web --no-open          # 远程/无头
uv run python -m CQR_Web --port 9000        # 换端口
uv run python -m CQR_Web --dir /别的/processed   # 换数据源

# 老命令仍可用（只是转发壳，里面没有逻辑）
uv run ./scripts/erp_viewer.py --no-open

# 也可以把 CQR_Web 当独立项目管理（自带 pyproject）
cd CQR_Web && uv run python -m CQR_Web
```

## 3. 结构

```
CQR_Web/
├── pyproject.toml          # 独立项目声明（flask + pyyaml；pytest 在 dev 组）
├── README.md
├── __init__.py             # 对外导出：create_app / scan_exports / delivery_gates …
├── __main__.py             # `python -m CQR_Web`
├── data.py                 # ★ 纯逻辑：扫描 / 路径安全 / 字段分组 / 交付闸门 / 校验（不 import Flask）
├── app.py                  # 路由 + CLI（create_app / main）
├── templates/index.html    # 页面本体（零外部依赖的原生 JS，不引 CDN）
└── tests/
    ├── conftest.py         # 把 ../scripts 和仓库根加进 sys.path
    └── test_viewer.py      # 机制回归：扫描 / 路径穿越 / 分组覆盖 59 字段 / 分层不退化
```

分层规矩（别退化）：

1. `data.py` **不许 import Flask** —— 这样它能被测试、被将来的命令行/批处理直接复用；
2. 页面**不许**自己算合法性/闸门，只显示服务端算好的结论；
3. `../scripts/erp_viewer.py` 只许当转发壳（有测试钉住它 < 40 行、不含实现）。

## 4. HTTP 接口（页面消费的就是这几个）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 页面本体 |
| GET | `/api/exports` | 扫描结果（每份产物摘要，最新在前；`_superseded*` 会被标出） |
| GET | `/api/export?path=<相对路径>` | 某一份产物的完整 JSON（含 `validation`、`incomplete`） |
| GET | `/api/schema` | ERP 字段字典 + 内部字段 + 展示分组 |

安全：`path` 一律经 `data.resolve_export()` 做 resolve + 前缀校验，出不了扫描根。

## 5. 测试

```bash
cd CQR_Web && uv run pytest        # 只跑 CQR_Web 自己的
uv run pytest                      # 技能仓库根：跑技能全套（CQR_Web 的分层边界有单独一条）
```

## 6. 下一步（待排期的编码任务）

- [ ] **上传**：把客户报价文件（xlsx / csv / txt / docx / pdf / eml）丢进 `pending/`，
      落盘后调 `main.py` 抽取，页面直接看到新产物（现在只能「看已经生成好的」）
- [ ] 上传件的输入单元语义对齐技能 1.0.38（单文件 = 一份 / 文件夹·zip = 参考文件合计 /
      有子目录 = 各算一份），页面上要能改「这批文件是几份报价」
- [ ] 多人使用：只读链接 + 操作留痕（谁上传、谁改了 CPMS）
- [ ] CPMS 在线编辑（现在必须 agent 手写；改成「人写 + `cpms_lint` 即时校验」）
