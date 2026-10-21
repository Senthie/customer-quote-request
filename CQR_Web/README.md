# CQR_Web

**客户报价需求（CQR）产物的 Web 前端**：把 `customer-files/processed/` 里导出的 ERP JSON
按业务视角摊开看 —— 字段分组、取值是否合法、交付闸门（CPMS 空 / 品名占位符）、原始文件清单。

**1.0.43（2026-10-20）起：上传 + 展示**。上传的客户文件落到 `customer-files/pending/`，
抽取交给 Hermes 定时任务（`scripts/scan_pending.py`，每 5 分钟扫一次）——本服务
**不起进程、不管队列、不记事状态**，页面每 10 秒刷新，抽好就自动出现在列表里。

## 1. 它是什么、不是什么

| 是 | 不是 |
|---|---|
| 展示层：读产物 JSON、调 `data.py` 算好的结论、渲染页面 | ❌ 不解析客户文件（那是技能 `extract_fields.py` 的事） |
| 上传口：把客户原始文件安全落到 `pending/`（`upload.py`） | ❌ 不跑抽取（那是定时任务 `scripts/scan_pending.py` 的事） |
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
uv run python -m CQR_Web --lan              # 对局域网开放（0.0.0.0，打印可访问地址）
uv run python -m CQR_Web --pending-dir /别的/pending

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
├── upload.py               # ★ 纯逻辑：上传落盘 / 文件名净化 / 后缀白名单 / 排队清单（不 import Flask）
├── app.py                  # 路由 + CLI（create_app / main）
├── templates/index.html    # 页面本体（零外部依赖的原生 JS，不引 CDN）
└── tests/
    ├── conftest.py         # 把 ../scripts 和仓库根加进 sys.path
    └── test_viewer.py      # 机制回归：扫描 / 路径穿越 / 分组覆盖 59 字段 / 分层不退化
```

分层规矩（别退化）：

1. `data.py` / `upload.py` **不许 import Flask** —— 这样它们能被测试、被将来的命令行/批处理直接复用；
2. 页面**不许**自己算合法性/闸门，只显示服务端算好的结论；
3. `../scripts/erp_viewer.py` 只许当转发壳（有测试钉住它 < 40 行、不含实现）；
4. 本服务**不许**起子进程跑抽取 —— 抽取只在 `scripts/scan_pending.py` 里发生（定时任务调它）。

## 4. HTTP 接口（页面消费的就是这几个）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 页面本体 |
| GET | `/api/exports` | 扫描结果（每份产物摘要，最新在前；`_superseded*` 会被标出） |
| GET | `/api/export?path=<相对路径>` | 某一份产物的完整 JSON（含 `validation`、`incomplete`） |
| GET | `/api/schema` | ERP 字段字典 + 内部字段 + 展示分组 |
| POST | `/api/uploads` | 上传客户文件（multipart：`files` 多个、`group=one\|each`） |
| GET | `/api/pending` | 还在等抽取的文件（页面排队条） |

`/api/exports` 的每一条都带 `source_name` —— **用户上传时的原始文件名**（列表主标题就用它，
`task_id` 降级成悬停提示）。取值顺序：副档 `eml_name` → 同目录 `.eml` → 报告 `unit.path`
→ 同目录原件（多个则「首个 +N」）→ 空（页面回落 `task_id`）。实现在 `data.source_name()`。

`/api/uploads` 的两种口径（对应 `main.py` 的输入单元语义，别自己造）：

| `group` | 落盘 | 含义 |
|---|---|---|
| `one` | `pending/<批次号>/`（多文件）/ `pending/<文件名>`（单文件） | 这几份是**同一份报价**的参考文件 |
| `each` | `pending/<文件名>` 平铺 | 每个文件**各算一份报价** |

单文件（含单个 `.zip`）一律平铺 —— 放进目录里 `input_unit` 会判「文件夹里只有压缩包」而拒。

`.eml`（1.0.46）：**只许单独传**（混在 `group=one` 批次里会被拒收，原因写在 `rejected[].reason`）。
本服务只落盘；展开由定时扫描器调 `scripts/ingest_email.py` 做 —— 正文落 `.txt`、附件落盘
（≥2 个附件 → 一个单元目录），原 `.eml` 归档到 `customer-files/inbox/<YYYYMMDD>/`。

安全：`path` 一律经 `data.resolve_export()` 做 resolve + 前缀校验，出不了扫描根；
上传文件名经 `upload.safe_filename()` 净化（只取 basename、去危险字符、挡 `..`），
后缀白名单 `xlsx/xls/csv/pdf/docx/txt/zip/eml`，请求体上限 64 MB。

> ⚠️ **没有登录认证**（业务明确：局域网内自己人用）。`--lan` 暴露出去就等于
> 「同网段能打开这个地址的人都能上传文件、看产物」。

## 4.1 抽取由谁做

```
浏览器 ──POST /api/uploads──▶ pending/            （本服务只写到这）
                                   │
              Hermes 定时任务（每 5 分钟）│  scripts/scan_pending.py
                                   ▼
                             main.py 逐份抽取
                                   │
                     processed/<task_id>/ERP_*.json
                                   │
浏览器 ◀──GET /api/exports（每 10 秒）──┘  → 新产物自动出现并被选中
```

`scan_pending.py` 的几条规矩（都有测试钉住）：

* **空 pending 不打印任何东西** —— 定时任务保持安静，不会每 5 分钟骚扰一次
* 处理前把条目 `os.replace` 进 `pending/_claimed/`（原子认领）；失败退回 `pending/`
  并记次数，**3 次**后进 `failed/_stuck_<时间戳>/`，不会无限重试
* `flock` 非阻塞互斥：上一轮还没跑完，这一轮直接退出（不抢同一批文件）
* 成功判据 = 退出码 0 **且** 输入已被 `main.py` 搬走（只看退出码不够）

## 5. 测试

```bash
cd CQR_Web && uv run pytest        # 只跑 CQR_Web 自己的
uv run pytest                      # 技能仓库根：跑技能全套（CQR_Web 的分层边界有单独一条）
```

## 6. 下一步（待排期的编码任务）

- [x] **上传**（1.0.43）：客户文件丢进 `pending/`，定时任务抽，页面 10 秒内自动看到
- [x] 输入单元语义对齐（`group=one|each` ↔ 单文件=一份 / 多文件=参考文件合计）
- [ ] 抽取明细可视化：`*.extracted.json` 里的「未识别标签」直接在页面上列出来（校准信号）
- [ ] 上传失败重试 / 清理：`pending/_claimed/` 与 `failed/_stuck_*` 的管理界面
- [x] **`.eml` 也走上传**（1.0.46）：上传口照收，扫描器调 `ingest_email.py` 展开后再抽，原邮件归档 `inbox/`
- [ ] CPMS 在线编辑（现在必须 agent 手写；改成「人写 + `cpms_lint` 即时校验」）
- [ ] 操作留痕：谁上传的、谁改了 CPMS（多人用时才需要）
