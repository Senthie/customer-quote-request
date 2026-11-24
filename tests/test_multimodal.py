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


def test_build_transcript_video_pages_逐页清单():
    payload = {"summary": "翻样书",
               "pages": [
                   {"no": 1, "kind": "cover", "frames": [1],
                    "left": "彩色怪兽", "right": None,
                    "text": ["el monstruo de colores"],
                    "popup": {"state": "null", "subject": None, "type": None}},
                   {"no": 2, "kind": "spread", "frames": [3, 4],
                    "left": "玻璃瓶", "right": "山景",
                    "text": ["Estás en tu emoción."],
                    "popup": {"state": "real", "subject": "彩色怪兽",
                              "type": "立体弹起", "spans": "跨页", "motion": "翻开时弹起"},
                    "note": "左页被手挡住"},
               ],
               "sequence": [], "texts": [], "unknowns": ["视频共看到 2 页"]}
    txt = MM.build_transcript(payload, Path("a.mp4"), "video", "M", 3.0)
    assert txt.startswith("【视频抽帧识别】")      # 表头不能被 pages 段覆盖（回归）
    assert "逐页清单" in txt
    assert "P1 [封面] 帧1" in txt
    assert "P2 [跨页] 帧3/4" in txt
    assert "左页: 玻璃瓶｜右页: 山景" in txt
    assert "立体件: state=real｜主体=彩色怪兽｜类型=立体弹起" in txt
    assert "⚠ 左页被手挡住" in txt


def test_sample_times_84秒视频按3秒一帧():
    cfg = {"max_video_frames": 28, "video_frame_interval": 3.0}
    times = MM._sample_times(84.5, cfg)
    assert len(times) == 28
    assert times[0] == 0.0
    assert times[-1] >= 80.0        # 片尾必留（封底/版权页）
    assert all(b - a >= 2.5 for a, b in zip(times, times[1:]))


def test_sample_times_短视频不会被拉稀():
    cfg = {"max_video_frames": 28, "video_frame_interval": 3.0}
    times = MM._sample_times(12.0, cfg)
    assert len(times) <= 6 and times[0] == 0.0


def test_sample_times_长视频按上限封顶():
    cfg = {"max_video_frames": 28, "video_frame_interval": 3.0}
    times = MM._sample_times(600.0, cfg)
    assert len(times) == 28 and times[-1] >= 590.0


def test_group_by_page_只合并真重复帧():
    items = [(0.0, 0b0), (1.0, 0b0), (2.0, 0b1111), (3.0, 0b1111)]
    # 阈值 2：0 与 0b1111 差 4 位 > 2，不许合并
    assert [len(g) for g in MM._group_by_page(items, threshold=2)] == [2, 2]


def test_dhash_同图同哈希_不同图不同哈希(tmp_path):
    from PIL import Image, ImageDraw
    a = tmp_path / "a.jpg"
    b = tmp_path / "b.jpg"
    c = tmp_path / "c.jpg"
    Image.new("L", (64, 64), 200).save(a)
    Image.new("L", (64, 64), 200).save(b)
    img = Image.new("L", (64, 64), 200)
    ImageDraw.Draw(img).rectangle([0, 0, 32, 32], fill=0)   # 左上一半涂黑
    img.save(c)
    assert MM._dhash(a) == MM._dhash(b)
    assert MM._hamming(MM._dhash(a), MM._dhash(c)) > 8


def test_复读护栏_连续相同页合并并记unknown(monkeypatch, tmp_path):
    """28 帧时 4B 会复读同一页描述；process_file 里的护栏要把它们合并掉。"""
    dup = {"no": 8, "kind": "spread", "frames": [8], "left": "彩色圆点", "right": "卡通怪兽",
           "text": ["Estás en tu emoción."]}
    payload = {"pages": [dict(dup), dict(dup, no=9, frames=[9]), dict(dup, no=10, frames=[10]),
                         {"no": 11, "kind": "spread", "frames": [11], "left": "树林",
                          "right": "猫头鹰", "text": ["El bosque"]}],
               "unknowns": []}
    src = tmp_path / "a.mp4"
    src.write_bytes(b"x")
    (tmp_path / "a.mp4.vlm.json").write_text("{}", encoding="utf-8")

    class FakeClient:
        model = "M"
        def chat(self, images, prompt):
            return {"text": json.dumps(payload), "latency_s": 1.0, "usage": {}}

    monkeypatch.setattr(MM, "prepare_images", lambda *a, **k: ("video", []))
    out = MM.process_file(src, MM.load_config(), FakeClient(), force=True)
    data = json.loads((tmp_path / "a.mp4.vlm.json").read_text(encoding="utf-8"))
    assert len(data["pages"]) == 2          # 3 条复读合成 1 条 + 树林页
    assert data["pages"][0]["frames"] == [8, 9, 10]
    assert [p["no"] for p in data["pages"]] == [1, 2]   # 合并后重新编号，不留洞
    assert any("复读" in u for u in data["unknowns"])
    assert out["status"] == "ok"


def test_merge_page_runs_两遍都有的页采信():
    a = [{"no": 1, "kind": "spread", "frames": [2], "left": "彩色怪兽立体件弹出", "right": "女孩插图",
          "text": ["Este es el monstruo"], "popup": {"state": "real", "subject": "怪兽", "type": "立体弹起"}}]
    b = [{"no": 1, "kind": "spread", "frames": [3], "left": "彩色怪兽弹出", "right": "女孩插图",
          "text": ["Este es el monstruo"], "popup": {"state": "printed", "subject": None}}]
    merged, single = MM.merge_page_runs(a, b, 0.4)
    assert len(merged) == 1 and not single
    assert merged[0]["frames"] == [2, 3]            # 帧号合并
    assert merged[0]["popup"]["state"] == "real"    # real 优先


def test_merge_page_runs_只出现一遍的页不进采信():
    a = [{"no": 1, "left": "树林与猫头鹰", "right": "吊床"},
         {"no": 2, "left": "云朵与雨滴", "right": "纸船"}]
    b = [{"no": 1, "left": "树林与猫头鹰", "right": "吊床"}]
    merged, single = MM.merge_page_runs(a, b, 0.4)
    assert len(merged) == 1 and len(single) == 1
    assert "云朵" in single[0]["left"]


def test_merge_page_runs_换句说法也能对上():
    a = [{"no": 1, "left": "一个立体的玻璃瓶和彩色山丘结构", "right": "卡通女孩"}]
    b = [{"no": 1, "left": "立体的玻璃瓶与彩色山丘", "right": "卡通女孩站在旁边"}]
    merged, _ = MM.merge_page_runs(a, b, 0.55)
    assert len(merged) == 1 and merged[0]["consensus"] >= 0.55


def test_merge_page_runs_完全不相关则不匹配():
    a = [{"no": 1, "left": "树林与猫头鹰", "right": "吊床与青蛙"}]
    b = [{"no": 1, "left": "玻璃瓶", "right": "山丘"}]
    merged, single = MM.merge_page_runs(a, b, 0.55)
    assert not merged and len(single) == 2


def test_merge_page_runs_合并后重新编号():
    a = [{"no": 5, "left": "甲页", "right": "乙"}, {"no": 9, "left": "丙页", "right": "丁"}]
    b = [{"no": 1, "left": "甲页", "right": "乙"}, {"no": 2, "left": "丙页", "right": "丁"}]
    merged, _ = MM.merge_page_runs(a, b, 0.55)
    assert [m["no"] for m in merged] == [1, 2]


def test_build_transcript_渲染只出现一遍段():
    payload = {"pages": [{"no": 1, "kind": "spread", "frames": [1], "left": "甲页", "right": "乙"}],
               "pages_single_run": [{"left": "丙页", "right": "丁", "frames": [7]}],
               "unknowns": []}
    txt = MM.build_transcript(payload, Path("a.mp4"), "video", "M", 1.0)
    assert "只出现一遍" in txt and "丙页" in txt


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


# ── 互动工艺（2026-11-24）：客户文字不写、只能从图片看出来 ────────────
def test_craft_五类工艺都在词表里():
    assert set(MM._CRAFT_ZH) == {"flap", "pull_tab", "mechanism", "felt", "die_cut_shape"}
    assert MM._CRAFT_ZH["flap"] == "揭页" and MM._CRAFT_ZH["felt"] == "毛毡布"


def test_normalize_crafts_real_保留工序要素():
    payload = {"craft": {"flap": {"state": "real", "where": "P2、P4、P6", "per_page": "2个",
                                  "total": "10个", "finger_notch": "有", "waste_removal": "需",
                                  "staple_allowance": "留钉位", "size_mm": "约145*145mm",
                                  "evidence": "可见纸边开口与手指位缺口"}}}
    out = MM.normalize_crafts(payload)
    f = out["craft"]["flap"]
    assert f["state"] == "real"
    assert f["total"] == "10个" and f["staple_allowance"] == "留钉位"


def test_normalize_crafts_printed无印刷词降级为unclear并清空细节():
    """插画里画出来的揭页不是实物 —— 数量和位置都不许留，否则直接算错成本。"""
    payload = {"craft": {"flap": {"state": "printed", "where": "P2", "total": "6个",
                                  "evidence": "画面里画着一扇小门"}},
               "unknowns": []}
    out = MM.normalize_crafts(payload)
    f = out["craft"]["flap"]
    assert f["state"] == "unclear"
    assert f["where"] is None and f["total"] is None
    assert any("揭页" in u and "确认" in u for u in out["unknowns"])


def test_normalize_crafts_printed有印刷词才保留printed():
    payload = {"craft": {"felt": {"state": "printed", "count": "5张",
                                  "evidence": "封底印着毛毡布图案"}}}
    out = MM.normalize_crafts(payload)
    assert out["craft"]["felt"]["state"] == "printed"
    assert out["craft"]["felt"]["count"] is None


def test_normalize_crafts_全空不动():
    payload = {"craft": {"flap": {"state": None, "where": None},
                         "felt": {"state": None}}}
    out = MM.normalize_crafts(payload)
    assert out["craft"]["flap"]["state"] is None
    assert not out.get("unknowns")


def test_build_transcript_渲染互动工艺段用厂内术语():
    payload = {"doc_type": "interior_spread", "titles": [], "specs": {}, "raw_text": ["x"],
               "craft": {"flap": {"state": "real", "where": "P2、P4、P6、P8、P10",
                                  "per_page": "2个", "total": "10个",
                                  "origin": "内文本身啤出", "finger_notch": "有",
                                  "waste_removal": "需", "staple_allowance": "留钉位",
                                  "evidence": "可见纸边开口"},
                         "felt": {"state": "real", "count": "5张", "thickness_mm": "1mm",
                                  "mount": "内文啤刀线再嵌入", "evidence": "可见毛毡层"}},
               "unknowns": [], "conflicts": []}
    txt = MM.build_transcript(payload, Path("a.jpg"), "image", "M", 1.0)
    assert "互动工艺" in txt
    assert "揭页: state=real" in txt and "共=10个" in txt and "钉位=留钉位" in txt
    assert "毛毡布: state=real" in txt and "厚度=1mm" in txt
    # 术语必须是厂内口径
    assert "翻翻页" not in txt and "felt" not in txt and "不织布" not in txt


def test_has_quote_info_true_for_craft_only_photo():
    """只拍出「有揭页」的样书内页，客户正文一字不提也是有效询价信息。"""
    payload = {"doc_type": "interior_spread", "specs": {},
               "craft": {"flap": {"state": "real", "total": "10个"}}, "raw_text": []}
    assert MM.has_quote_info(payload) is True
