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
| 视频 | **翻页感知抽帧** ≤28 帧（场景切换 + 均匀补齐，首尾必留）、短边 480 | P2 `video_user` | **逐页清单**（每页左右页/文字/立体件）+ 工艺 + 装订证据帧 + 页数线索 + 尺寸线索 + 画面文字 |

**视频为什么要「时序」**：客户拍的是翻样书，单帧看不出装订。P2 要求模型回答
「哪一帧看到书脊/订口/订线」，并把装订结论挂在具体帧号上 —— 这样人工复核能直接跳到那一帧。

**逐页清单是怎么来的（2026-11-24 实测，三次试错）**

目标：客户只发视频时，报价要能写「P3-P4 一个彩色怪兽 POP UP」，而不是「各种立体图案」。

| 方案 | 实测结果（84.5s、约 10 个跨页的翻书视频） | 结论 |
|---|---|---|
| ffmpeg 场景检测切页（`select='gt(scene,0.10)'`） | 0 个切点；阈值降到 0.06 也只有 1 个 | ❌ 书本/桌面/灯光不变，只有中间一块纸翻过去，全局 scene 分数上不去 |
| dHash 感知哈希聚类（64 位，阈值 8） | 60 帧里 57 帧互不相同；相邻帧距离平均 21/64 | ❌ 手持拍摄整帧一直在动，同一页的两帧也不像 |
| 均匀 12 帧 + 逐页提示词 | 10 页，内容各不相同 | ✅ 可用，但一页只有 1 帧，长视频会漏页 |
| 均匀 28 帧 + 逐页提示词 | **P8–P20 二十条全是同一句「彩色圆点图案/卡通怪兽」** | ❌ 4B 帧数一多就退化成复读 |
| 均匀 16 帧 + 逐页提示词 | 15 页，各有左右页/文字/立体件；复读只剩 1 条 | ✅ 当前口径 |

所以最终实现是：**采样密度是唯一杠杆**，`video_frame_interval`（默认 3.0s）与
`max_video_frames`（默认 16）决定覆盖率；哈希只用来丢「视频卡住/重复帧」（阈值 2）。
要真做自动切页得做局部特征匹配（ORB 之类），当前收益不值这个成本。

配三道护栏（都是实测逼出来的）：

1. **`{FRAME_COUNT}` 注入**：提示词里告诉模型本次共几帧，并要求「pages 条数必须明显少于帧数」；
2. **复读合并**：连续且 `left/right/text` 完全相同的页合并成一条，帧号并进 `frames`，
   `unknowns` 里写明「合并了 N 条复读，实际只剩 M 页，需人工对照视频核」；
3. **`has_quote_info` 认 pages**：只看得出页级内容的视频也是有效询价信息，
   不能再按「texts ≥3 行」卡掉（否则整段转录作废）。

⚠ **页级 `text` 可信度低于逐行转录**：实测 16 帧版里，模型会按书里的句式模板化造句子
（「La planta es un lugar / donde puedes encontrar / lo que te gusta」是拼出来的）。
定位是**页序与工艺清单**可用，**引文必须回看原视频**；`unknowns` 会写明这一条。

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
