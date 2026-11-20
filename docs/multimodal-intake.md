# 多模态报价件：图片 / 扫描 PDF / 视频 → 可引用的转录文本

> 2026-11-19 森西拍板：客户报价除了文本、Excel、PDF，还会发**图片**和**视频**，
> 用 vLLM 上的 `Qwen3-VL-4B-Instruct` 做识别。
> 端点与提示词：`assets/vlm_prompts.yaml`（改提示词只改这里）。

## 1. 它在流水线的哪一格

```
客户邮件/附件
   ├─ xlsx / csv / docx / txt / 有文字层的 pdf ──────────────→ 直接抽（原路，不变）
   ├─ 图片 jpg/png/webp/heic/gif ─┐
   ├─ 扫描版 pdf（无文字层）      ├→ multimodal.py → <名>.vlm.txt ─→ 抽取引擎
   └─ 视频 mp4/mov/avi/mkv       ─┘        （客户原文的替身，可被 CPMS 引用）
```

`scan_pending.sh` 在扫描前跑 `multimodal.py --prepass --limit 10`：

- `pending/` **根下**的媒体文件 → 先包成单元目录 `pending/<名>/`，再在同目录产 `.vlm.txt` + `.vlm.json`；
- **单元目录里**的媒体（`.eml` 展开出来的附件）→ 就地产 sidecar；
- 有文字层的 PDF **不送模型**（省钱省时，原路抽取）。

## 2. 模型口径：只做转录员

- 只写画面上看得见的文字与事实；看不到 → `null`；看不清 → `?`。
- 德/英/日原文照抄，不翻译（抽取引擎靠原文标签认字段）。
- 数字类必须同时出现在 `raw_text`（逐行原文）和 `evidence`（字段+引文）里才算数；
  只有 `raw_text` 没引文的，转录里标「（无引文，需复核）」。
- 拿不准的进 `unknowns` → CPMS 写「待确认」，**不许凭印象落库**。

## 3. 三种输入的具体处理

| 类型 | 预处理 | 提示词 | 输出要点 |
|---|---|---|---|
| 图片 | EXIF 转正、长边压到 1600px、HEIC→JPEG | P1 `image_user` | 原文逐行 + 结构化字段（带引文）+ unknowns/conflicts |
| 扫描 PDF | pymupdf 200dpi 渲染，最多 6 页 | P1（多图） | 同上 |
| 视频 | ffmpeg 均匀抽帧 ≤12 帧、短边 480 | P2 `video_user` | 时序理解：装订证据帧 / 工艺 / 页数线索 / 尺寸线索 / 画面文字 |

**视频为什么要「时序」**：客户拍的是翻样书，单帧看不出装订。P2 要求模型回答
「哪一帧看到书脊/订口/订线」，并把装订结论挂在具体帧号上 —— 这样人工复核能直接跳到那一帧。

## 4. 签名 logo / 横幅护栏（实测教训）

真值库抽 3 张真实图片附件跑首轮，**3 张全是签名 logo / 活动横幅**（VEMAG 徽标、
`Retail Innovation Days` banner）。模型照样读得出字，但把它写进 `.vlm.txt` 会让抽取引擎
把 `vermag` 当成品名。

护栏：`doc_type=logo_banner`（或既无 specs 又无数量/语言/书名）→ **只写 `.vlm.json`、不产 `.vlm.txt`**，
台账记 `status=no_quote_info`。扫描器看到该目录里没有文本，自然不会拿它建单。

## 5. 台账与缓存

- `customer-files/logs/vlm_ledger.jsonl`：每次识别一行（file / kind / status / frames / 耗时 / unknowns）。
- `customer-files/.vlm_cache/<sha256>.json`：同图不重复烧算力。
- 幂等：`.vlm.json` 存在就跳过（`--force` 强制重做）。

## 6. 门禁：CPMS 引用图片数值要能溯源

`cpms_content_audit.py --source` **必须把 `.vlm.txt` 一起传**（和客户原文并列）：

```bash
uv run ./scripts/cpms_content_audit.py --cpms processed/<task>/ERP_*.json \
  --source processed/<task>/<客户原文>.txt \
  --source processed/<task>/<附件名>.jpg.vlm.txt
```

只传原文不传 `.vlm.txt` → 从图片得出的尺寸/克重会被判「无出处」，看起来像编造。

## 7. 依赖与环境

- Python：`pillow`（已加进 pyproject）、`pymupdf`（原有）
- 系统：**ffmpeg / ffprobe**（只视频需要）
  ```bash
  sudo apt-get install -y ffmpeg     # 已装：/usr/bin/ffmpeg 6.1.1
  ```
- 端点：**不入库**（公开仓库）。配 `assets/vlm_endpoint.local.yaml`（照 `.local.example.yaml` 复制）
  或环境变量 `CQR_VLM_ENDPOINT` / `CQR_VLM_MODEL`；本机实测 vLLM `max_model_len=32768`，4B 单卡够用。
  实测单图 1.3–4.3s，视频 9 帧 5.9s。

## 8. 已知缺陷（别急着当 bug 修）

- **小字号数字会看错**：实测把 `43 x 31 x 26 cm` 读成 `28 cm`。所以外箱尺寸只进 `raw_text`，
  数字类一律要人工核；`26 vs 28` 这类差异靠 `unknowns` + 汇报点名，不靠模型。
- **`print_colors` 会把 `4/4-farbig` 抽成 `4`**：以 `raw_text` 原文为准。
- **视频的「帧N: 无变化」很多**：静态帧对比没有语义，正常；结论看 `binding` / `page_guess`。
- **4B 的页数推断只认显式页码**：翻页视频里没有页码就 `null`，不要当漏识别。

## 9. 相关脚本

| 脚本 | 作用 |
|---|---|
| `scripts/multimodal.py` | 媒体→文本；`--prepass` 给 cron，`--path` 单份调试 |
| `assets/vlm_prompts.yaml` | 端点 / 模型 / P1 P2 P3 提示词 |
| `scripts/scan_pending.sh` | 已挂第 1.8 步 prepass（失败不阻断扫描） |
| `tests/test_multimodal.py` | 不打网络的回归（FakeClient 顶替模型） |
