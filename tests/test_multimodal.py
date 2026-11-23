"""multimodal.py 的回归测试 —— 不打网络，用 FakeClient 顶替 vLLM。

覆盖：
  * 模型回复可能是裸 JSON / ```json 栅栏 / 带前言后语 → 三种都要解析出来
  * 转录文本必须保留「原文逐行」与「未确定」两段（抽取与人工复核都靠它）
  * 扫描版 PDF 与有文字层 PDF 的判别
  * prepass 把 pending 根下的图片包成单元目录、写 sidecar、已有就跳过
  * 没有 ffmpeg 时视频要给出可读错误，不许静默成功
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import multimodal as MM  # noqa: E402

CFG = {
    "endpoint": "http://127.0.0.1:1/v1/chat/completions",
    "model": "Qwen3-VL-4B-Instruct",
    "timeout": 5,
    "retries": 0,
    "max_edge_px": 800,
    "max_pdf_pages": 2,
    "max_video_frames": 2,
    "video_short_side": 240,
    "system": "",
    "image_user": "x",
    "video_user": "y",
    # 测试图都是几十字节的纯色块；生产默认 12000 会把它们当签名图标滤掉。
    # 「小图确实该滤」由 test_small_image_is_filtered 单独盯。
    "min_image_bytes": 0,
}

FAKE_PAYLOAD = {
    "doc_type": "spec_table",
    "titles": ["Stickerbuch Bauernhof"],
    "specs": {"finished_size_mm": "210 x 148 mm", "extent_pp": 24,
              "paper_gsm": ["170 g/m²"], "print_colors": ["4/4"],
              "binding": "Klebebindung", "finish": [], "special": []},
    "quantities": ["5.000", "8.000"],
    "languages": ["Deutsch"],
    "raw_text": ["Format: 210 x 148 mm", "Innenteil: 170 g/m² Bilderdruck matt",
                 "Karton: 43 x 31 x 26 cm"],
    "evidence": [{"field": "paper_gsm", "quote": "Innenteil: 170 g/m² Bilderdruck matt",
                  "where": "第2行"}],
    "unknowns": ["装订方式图上未写明"],
    "conflicts": [],
}


class FakeClient:
    model = "FakeVL"

    def __init__(self, reply: str | None = None, payload: dict | None = None):
        self.calls = 0
        self.reply = reply if reply is not None else json.dumps(payload or FAKE_PAYLOAD)

    def chat(self, image_paths, user_prompt):
        self.calls += 1
        assert image_paths, "至少要送一张图"
        return {"text": self.reply, "latency_s": 0.1, "usage": {}}


# ── 回复解析 ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw", [
    json.dumps(FAKE_PAYLOAD),
    "```json\n" + json.dumps(FAKE_PAYLOAD) + "\n```",
    "好的，结果如下：\n" + json.dumps(FAKE_PAYLOAD) + "\n希望有帮助。",
])
def test_parse_json_reply_three_shapes(raw):
    data = MM.parse_json_reply(raw)
    assert data and data["specs"]["binding"] == "Klebebindung"


def test_parse_json_reply_garbage_returns_none():
    assert MM.parse_json_reply("模型胡说八道，没有 JSON") is None


# ── 转录文本 ──────────────────────────────────────────────────────────
def test_build_transcript_keeps_raw_text_and_unknowns():
    txt = MM.build_transcript(FAKE_PAYLOAD, Path("a.jpg"), "image", "M", 1.2)
    assert "原文逐行" in txt and "Karton: 43 x 31 x 26 cm" in txt      # 外箱尺寸留在原文里
    assert "未确定" in txt and "装订方式图上未写明" in txt
    assert "170 g/m²" in txt and "无引文" not in txt.split("用纸/克重")[1][:40]


def test_build_transcript_video_sequence():
    payload = {"summary": "翻样书", "sequence": [{"frame": 1, "what_changed": "合上"},
                                                 {"frame": 2, "what_changed": "翻开书脊"}],
               "binding": {"value": "Klebebindung", "frame": 2, "why": "书脊可见胶"},
               "craft": ["UV-Lack"], "page_guess": {"value": None, "basis": ""},
               "texts": ["Bauernhof"], "unknowns": ["页数看不清"]}
    txt = MM.build_transcript(payload, Path("a.mp4"), "video", "M", 3.0)
    assert "视频内容（时序）" in txt and "帧2" in txt and "Klebebindung" in txt
    assert "画面文字" in txt and "Bauernhof" in txt


# ── PDF 判别 ──────────────────────────────────────────────────────────
def _make_text_pdf(path: Path):
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Format: 210 x 148 mm\nUmfang: 24 Seiten")
    doc.save(str(path))
    doc.close()


def _make_scan_pdf(path: Path):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (600, 300), "white")
    ImageDraw.Draw(img).text((20, 20), "Format 210 x 148 mm", fill="black")
    img.save(path.parent / "scan.jpg")
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    page = doc.new_page()
    page.insert_image(page.rect, filename=str(path.parent / "scan.jpg"))
    doc.save(str(path))
    doc.close()


def test_pdf_needs_vision(tmp_path):
    text_pdf = tmp_path / "text.pdf"
    scan_pdf = tmp_path / "scan.pdf"
    _make_text_pdf(text_pdf)
    _make_scan_pdf(scan_pdf)
    assert MM.pdf_needs_vision(text_pdf) is False
    assert MM.pdf_needs_vision(scan_pdf) is True


# ── prepass ───────────────────────────────────────────────────────────
def test_prepass_wraps_root_image_and_writes_sidecars(tmp_path, monkeypatch):
    pending = tmp_path / "pending"
    pending.mkdir()
    from PIL import Image
    Image.new("RGB", (200, 100), "white").save(pending / "quote.jpg")
    monkeypatch.setattr(MM, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(MM, "LOG_DIR", tmp_path / "logs")

    results = MM.prepass(pending, CFG, FakeClient())

    assert results and results[0]["status"] == "ok"
    unit = pending / "quote"                      # 根下媒体 → 包成单元目录
    assert (unit / "quote.jpg").exists()
    assert (unit / "quote.jpg.vlm.txt").exists()
    payload = json.loads((unit / "quote.jpg.vlm.json").read_text(encoding="utf-8"))
    assert payload["_meta"]["model"] == "FakeVL"

    fake = FakeClient()
    again = MM.prepass(pending, CFG, fake)        # 已有转录 → 跳过，不再烧模型
    assert again[0]["status"] == "skipped" and fake.calls == 0


def test_small_image_is_filtered(tmp_path, monkeypatch):
    """生产默认 min_image_bytes=12000：邮件签名小图标（实测 0–3KB）不送模型。"""
    pending = tmp_path / "pending"
    pending.mkdir()
    from PIL import Image
    Image.new("RGB", (16, 16), "white").save(pending / "icon.png")
    monkeypatch.setattr(MM, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(MM, "LOG_DIR", tmp_path / "logs")

    fake = FakeClient()
    assert MM.prepass(pending, {**CFG, "min_image_bytes": 12000}, fake) == []
    assert fake.calls == 0
    # 阈值放到 0 就照常处理（证明差别只在阈值，不是文件本身有问题）
    assert MM.prepass(pending, {**CFG, "min_image_bytes": 0}, FakeClient())[0]["status"] == "ok"


def test_prepass_reports_video_error_without_ffmpeg(tmp_path, monkeypatch):
    pytest.importorskip("fitz")
    pending = tmp_path / "pending"
    pending.mkdir()
    (pending / "demo.mp4").write_bytes(b"not a real video")
    monkeypatch.setattr(MM, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(MM, "LOG_DIR", tmp_path / "logs")
    if shutil.which("ffmpeg"):
        pytest.skip("本机有 ffmpeg，这条只验证缺 ffmpeg 时的可读报错")

    results = MM.prepass(pending, CFG, FakeClient())
    assert results[0]["status"] == "failed"
    assert "ffmpeg" in results[0]["reason"]


# ── 签名 logo / 横幅：不许污染抽取 ────────────────────────────────────
def test_logo_banner_writes_json_but_no_transcript(tmp_path, monkeypatch):
    from PIL import Image
    monkeypatch.setattr(MM, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(MM, "LOG_DIR", tmp_path / "logs")
    img = tmp_path / "sig.png"
    Image.new("RGB", (160, 60), "white").save(img)
    logo = {"doc_type": "logo_banner", "titles": [], "specs": {}, "quantities": [],
            "languages": [], "raw_text": ["vermag", "DIE ZUKUNFT IST BUCH"],
            "evidence": [], "unknowns": [], "conflicts": []}

    result = MM.process_file(img, CFG, FakeClient(payload=logo))

    assert result["status"] == "no_quote_info"
    assert (tmp_path / "sig.png.vlm.json").exists()
    assert not (tmp_path / "sig.png.vlm.txt").exists()      # 关键：不产文本，抽取引擎就不会读到
    assert MM.has_quote_info(logo) is False


def test_has_quote_info_true_for_spec_table():
    assert MM.has_quote_info(FAKE_PAYLOAD) is True


# ── 2026-11-23 部件扫查改版：截断修复 / 三态护栏 / 转录渲染 ─────────────
def test_parse_json_reply_repairs_truncated_reply():
    """components 加进来后单图就可能撞 max_tokens，截断的要能救回前半段。"""
    trunc = ('{"doc_type":"cover_art","specs":{"special":[]},'
             '"components":{"lights":{"state":"real","count":nul')
    data = MM.parse_json_reply(trunc)
    assert data and data["doc_type"] == "cover_art"
    assert data["components"]["lights"]["state"] == "real"
    assert data.get("_meta", {}).get("repaired") is True


def test_normalize_components_downgrades_unjustified_printed():
    """说 printed 但 evidence 里没有任何「印刷」字样 → 降级 unclear，细节字段清空。"""
    payload = {"components": {"battery": {"state": "printed", "cell_type": "CR2032",
                                          "cell_count": 3, "evidence": "Made in China"}},
               "unknowns": []}
    out = MM.normalize_components(payload)
    bat = out["components"]["battery"]
    assert bat["state"] == "unclear"
    assert bat["cell_type"] is None and bat["cell_count"] is None
    assert any("电池" in u and "客户确认" in u for u in out["unknowns"])


def test_normalize_components_keeps_real_and_true_alias():
    payload = {"components": {"sound_module": {"state": True, "keys": 6,
                                              "key_shape": "圆键",
                                              "evidence": "底部有格栅出音孔"}},
               "unknowns": []}
    out = MM.normalize_components(payload)
    sm = out["components"]["sound_module"]
    assert sm["state"] == "real" and sm["keys"] == 6      # real 的细节字段保留


def test_build_transcript_renders_components_section():
    payload = {"doc_type": "cover_art", "titles": [], "specs": {}, "raw_text": ["abc"],
               "components": {"sound_module": {"state": "real", "keys": 6,
                                               "key_shape": "圆键",
                                               "evidence": "底部格栅出音孔"},
                              "lights": {"state": "printed", "type": None,
                                         "evidence": "插图中金色灯饰"}},
               "unknowns": [], "conflicts": []}
    txt = MM.build_transcript(payload, Path("a.jpg"), "image", "M", 1.0)
    assert "结构部件" in txt
    assert "发声模块: state=real" in txt and "键数=6" in txt
    assert "灯光: state=printed" in txt


def test_has_quote_info_true_for_component_only_photo():
    """只有电池仓/按键背面的特写，也算有效询价信息，别当 logo 丢掉。"""
    payload = {"doc_type": "component_closeup", "specs": {},
               "components": {"battery": {"state": "real", "cell_type": "AAA"}},
               "raw_text": []}
    assert MM.has_quote_info(payload) is True


def test_raw_text_line_cap_enforced(tmp_path, monkeypatch):
    """提示词要求 ≤40 行，模型不听话时代码兜底截断并记 unknowns。"""
    from PIL import Image
    monkeypatch.setattr(MM, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(MM, "LOG_DIR", tmp_path / "logs")
    img = tmp_path / "page.jpg"
    Image.new("RGB", (200, 100), "white").save(img)
    reply = json.dumps({"doc_type": "cover_art", "titles": [],
                        "specs": {"binding": "Klebebindung"},
                        "components": {}, "raw_text": [f"line{i}" for i in range(10)],
                        "unknowns": [], "conflicts": [], "quantities": [], "languages": []})
    cfg = {**CFG, "max_raw_text_lines": 3}
    out = MM.process_file(img, cfg, FakeClient(reply=reply), force=True)
    assert out["status"] == "ok"
    payload = json.loads((tmp_path / "page.jpg.vlm.json").read_text(encoding="utf-8"))
    assert len(payload["raw_text"]) == 3
    assert any("未转录" in u for u in payload["unknowns"])
