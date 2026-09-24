"""ERP 导出查看器（scripts/erp_viewer.py + scripts/ERP_data_show.html）回归测试。

业务 2026-09-24 三件事：
  ① 旧 HTML 是按**上一代**结构写的（SJFX / KHMBJ / GG_length…），全错，要按新结构重写；
  ② 要能**自动扫描** customer-files/processed 下的导出产物；
  ③ 产物要加内部字段 `version` 标识当前配置版本（目前 = 2）。

这里钉住的是**机制**，不是某一份产物的具体值：
  * 扫描能递归找到全部 ERP_*.json、最新在前、_superseded 目录会被标出来
  * `path` 参数出不了 processed 目录（路径穿越）
  * 字段分组必须**恰好覆盖** 59 个 ERP 字段各一次 —— 加了字段忘了归组会直接红，
    避免新字段在页面上悄悄不显示
  * 页面不许再引外部 <script src>（旧版引 unpkg/jsdelivr，断网白屏）
  * HTTP 四个端点真的能应答
"""
from __future__ import annotations

import json
import os
import re
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

import erp_viewer as ev
import erp_schema
from main import TEMPLATE_PATH

REPO_ROOT = Path(__file__).resolve().parent.parent
PAGE_PATH = REPO_ROOT / "scripts" / "ERP_data_show.html"


# ═══════════════════════════════════════════════════════════════════════
# 扫描
# ═══════════════════════════════════════════════════════════════════════
def _make_export(root: Path, task_id: str, payload: dict, *, rel_dir: str | None = None,
                 stamp: str = "20260924_010203", mtime: float | None = None) -> Path:
    d = root / (rel_dir if rel_dir is not None else task_id)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"ERP_{task_id}_{stamp}.json"
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    if mtime is not None:
        # 显式设 mtime：否则同一秒内创建的多份产物排序会不稳，测试就飘
        os.utime(p, (mtime, mtime))
    return p


def test_扫描递归找出全部产物_最新在前(tmp_path):
    _make_export(tmp_path, "aaa", {"CPMC": "甲"},   # 老产物：没有内部字段 version
                 stamp="20260101_000000", mtime=1_700_000_100)
    _make_export(tmp_path, "bbb", {"version": 2, "CPMC": "乙"},
                 stamp="20260102_000000", mtime=1_700_000_200)
    _make_export(tmp_path, "ccc", {"CPMC": "丙"}, rel_dir="_superseded_x/ccc",
                 stamp="20260103_000000", mtime=1_700_000_300)

    items = ev.scan_exports(tmp_path)
    assert len(items) == 3
    assert [it["task_id"] for it in items] == ["ccc", "bbb", "aaa"], "最新在前"
    assert items[0]["superseded"] is True, "_superseded 目录下的要标出来"
    assert items[1]["superseded"] is False
    assert items[1]["version"] == 2
    assert items[2]["version"] is None, "老产物没有 version → None（页面上显示「缺」）"
    assert items[1]["CPMC"] == "乙"
    assert items[1]["dir"] == "bbb"
    assert items[1]["filename"].startswith("ERP_bbb_")
    assert items[1]["mtime_iso"].startswith("2023-11-1"), items[1]["mtime_iso"]


def test_扫描忽略非_ERP_文件(tmp_path):
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "input.validation.json").write_text("{}", encoding="utf-8")
    (tmp_path / "t" / "input.extracted.json").write_text("{}", encoding="utf-8")
    _make_export(tmp_path, "real", {"CPMC": "真的"})
    items = ev.scan_exports(tmp_path)
    assert [it["task_id"] for it in items] == ["real"]


def test_扫描_坏文件不炸只标记(tmp_path):
    d = tmp_path / "broken"
    d.mkdir()
    (d / "ERP_broken_20260924_000000.json").write_text("{ 这不是 json", encoding="utf-8")
    _make_export(tmp_path, "good", {"CPMC": "好的"})
    items = ev.scan_exports(tmp_path)
    assert len(items) == 2, "坏文件也必须列出来，否则用户以为没产出"
    broken = next(it for it in items if it["task_id"] == "broken")
    assert broken["broken"] and "JSONDecodeError" in broken["broken"]
    assert broken["key_count"] == 0
    # 顶层不是对象也要标出来
    (d / "ERP_broken_20260924_000000.json").write_text("[1,2,3]", encoding="utf-8")
    assert ev.scan_exports(tmp_path)
    assert next(it for it in ev.scan_exports(tmp_path) if it["task_id"] == "broken")["broken"]


def test_扫描目录不存在返回空(tmp_path):
    assert ev.scan_exports(tmp_path / "并不存在") == []


def test_扫描真实产物_每份都能解析():
    """真实 processed/ 里的产物（有就给跑）：扫描不能抛异常，字段要认得出来。"""
    items = ev.scan_exports()
    for it in items:
        if it["broken"]:
            raise AssertionError(f"{it['relpath']} 解析失败: {it['broken']}")
        assert it["key_count"] > 0, it["relpath"]
    # 至少有一份带内部字段 version（本轮之后跑出来的产物）
    if items and any(it["version"] is not None for it in items):
        assert all(it["version"] == 2 for it in items if it["version"] is not None)


# ═══════════════════════════════════════════════════════════════════════
# 路径安全
# ═══════════════════════════════════════════════════════════════════════
def test_解析路径_禁越界(tmp_path):
    _make_export(tmp_path, "ok", {"CPMC": "好"})
    secret = tmp_path.parent / "secret.json"
    secret.write_text('{"别":"看"}', encoding="utf-8")
    try:
        assert ev.resolve_export("ok/ERP_ok_20260924_010203.json", tmp_path).is_file()
        for bad in ("../secret.json", "ok/../../secret.json", "/etc/passwd",
                    "..%2Fsecret.json", ""):
            with pytest.raises(ValueError):
                ev.resolve_export(bad, tmp_path)
    finally:
        secret.unlink(missing_ok=True)


def test_解析路径_只收_json(tmp_path):
    d = tmp_path / "t"
    d.mkdir()
    (d / "note.txt").write_text("hi", encoding="utf-8")
    with pytest.raises(ValueError):
        ev.resolve_export("t/note.txt", tmp_path)


def test_解析路径_不存在(tmp_path):
    with pytest.raises(ValueError):
        ev.resolve_export("nope/ERP_nope_20260924_000000.json", tmp_path)


def test_符号链接逃逸也被挡住(tmp_path):
    """软链指向目录外 → resolve() 后前缀校验要拦住。"""
    outside = tmp_path.parent / "outside_dir"
    outside.mkdir(exist_ok=True)
    (outside / "ERP_x_20260924_000000.json").write_text('{"a":1}', encoding="utf-8")
    root = tmp_path / "root"
    root.mkdir()
    link = root / "esc"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("该文件系统不支持软链")
    with pytest.raises(ValueError):
        ev.resolve_export("esc/ERP_x_20260924_000000.json", root)


# ═══════════════════════════════════════════════════════════════════════
# 字典 / 分组
# ═══════════════════════════════════════════════════════════════════════
def test_字典载荷_59字段带标签与合法取值():
    payload = ev.build_schema_payload()
    fields = payload["erp_fields"]
    assert len(fields) == 59
    by_key = {f["key"]: f for f in fields}
    assert by_key["CPLB"]["label"] == "产品类别"
    assert by_key["CPLB"]["type"] == "select"
    assert len(by_key["CPLB"]["legal"]) == 88, "产品类别来自权威树（88 节点）"
    assert by_key["CPSX"]["legal"], "CPSX 必须有合法取值"
    assert by_key["CPMC"]["type"] == "text"
    assert by_key["chkXBJF"]["type"] == "checkbox"
    assert by_key["CPMS"]["type"] == "textarea"
    assert by_key["BDBH"]["auto_filled"] is True
    assert by_key["KHDH"]["required"] == "error"


def test_分组恰好覆盖59字段各一次():
    """加了新 ERP 字段却忘了归组 → 页面上会悄悄不显示。这条就是防这个。"""
    payload = ev.build_schema_payload()
    schema_keys = [f["key"] for f in payload["erp_fields"]]
    grouped = [k for g in payload["groups"] for k in g["keys"]]
    assert len(grouped) == len(set(grouped)), f"有键被分到多个组: {grouped}"
    assert set(grouped) == set(schema_keys), (
        f"没归组的键: {sorted(set(schema_keys) - set(grouped))}；"
        f"归组了但字典里没有: {sorted(set(grouped) - set(schema_keys))}"
    )


def test_分组顺序覆盖全部59键_不含内部字段():
    payload = ev.build_schema_payload()
    internal = {f["key"] for f in payload["internal_fields"]}
    grouped = [k for g in payload["groups"] for k in g["keys"]]
    assert not (set(grouped) & internal), "内部字段不该混进 ERP 分组"


def test_字典载荷_内部字段含version():
    payload = ev.build_schema_payload()
    keys = [f["key"] for f in payload["internal_fields"]]
    assert keys == ["ID", "CPMC_ZH", "Reminder", "version"]
    ver = next(f for f in payload["internal_fields"] if f["key"] == "version")
    assert ver["source"] == "config_version", "version 的取数来源必须是档案的配置版本"
    assert ver["label"], "要有中文标签"


def test_字典与erp_schema同源():
    """查看器的字段元数据必须来自 assets/erp_form_schema.yaml，不能另抄一份。"""
    payload = ev.build_schema_payload()
    authoritative = erp_schema.load_schema().get("fields") or {}
    assert [f["key"] for f in payload["erp_fields"]] == list(authoritative)
    for f in payload["erp_fields"]:
        assert f["label"] == str((authoritative[f["key"]] or {}).get("label", ""))
        assert f["type"] == str((authoritative[f["key"]] or {}).get("type", "text"))


# ═══════════════════════════════════════════════════════════════════════
# 配置版本
# ═══════════════════════════════════════════════════════════════════════
def test_配置版本_读的是档案真源():
    """页面上显示的「当前配置版本」= customers/_defaults.yaml 的顶层 version。"""
    import yaml

    from customer_profile import load_profile, load_defaults

    raw = yaml.safe_load((REPO_ROOT / "customers" / "_defaults.yaml").read_text(encoding="utf-8"))
    assert raw.get("version") == 2, "业务 2026-09-24：目前设置版本为 2"
    assert ev.read_config_version() == 2
    assert load_defaults().get("version") == 2
    # 客户档案从公共默认层继承
    for cid in ("ngv", "igl"):
        assert load_profile(cid).version == 2, f"{cid} 应从 _defaults 继承 version=2"


def test_配置版本_档案校验会拦非法值(tmp_path):
    from customer_profile import CustomerProfile

    base = dict(id="t", name="t", priority=1, status="calibrated",
                detect={"keywords": ["x"]})
    ok = CustomerProfile.from_raw({**base, "version": 2})
    assert ok.validate() == [], ok.validate()
    for bad in (None, 0, -1, "2", 2.5, True):
        prof = CustomerProfile.from_raw({**base, "version": bad})
        assert any("version" in p for p in prof.validate()), f"{bad!r} 应被拦下"


def test_配置版本_出现在档案描述里():
    from customer_profile import load_profile
    assert "配置版本: 2" in load_profile("ngv").describe()


# ═══════════════════════════════════════════════════════════════════════
# 页面本体
# ═══════════════════════════════════════════════════════════════════════
def test_页面无外部脚本依赖():
    """旧版引 unpkg(Vue3) + jsdelivr(dayjs)，断网/内网直接白屏。这是内部工具，必须离线可用。"""
    html = PAGE_PATH.read_text(encoding="utf-8")
    srcs = re.findall(r"<script[^>]*\bsrc\s*=", html, flags=re.I)
    assert srcs == [], f"不允许外部脚本（断网会白屏）: {srcs}"
    assert "unpkg.com" not in html.replace("旧版引了 unpkg", ""), "别把 CDN 加回来"


def test_页面用当前结构的字段名():
    """旧页面写的是上一代字段名，这些一个都不该再出现。"""
    html = PAGE_PATH.read_text(encoding="utf-8")
    stale = ['"SJFX"', "formData.SJFX", "KHMBJ", "GG_length", "GG_width",
             "GG_height", '"create_by"', "has_safety_checks", "target_age_group"]
    for s in stale:
        assert s not in html, f"页面里还有上一代字段名: {s}"


def test_页面调用的接口都存在():
    html = PAGE_PATH.read_text(encoding="utf-8")
    for endpoint in ("api/schema", "api/exports", "api/export?path="):
        assert endpoint in html, f"页面没调用 {endpoint}"
    # 服务端真的实现了这些路由
    server_src = (REPO_ROOT / "scripts" / "erp_viewer.py").read_text(encoding="utf-8")
    for route in ('"/api/exports"', '"/api/export"', '"/api/schema"'):
        assert route in server_src, f"服务端没实现 {route}"


def test_页面认得出内部字段与空值口径():
    html = PAGE_PATH.read_text(encoding="utf-8")
    assert "null（空值）" in html, "「无」→ JSON null 的展示口径"
    assert "已被取代" in html, "_superseded 目录要标出来"
    assert "校验 error" in html and "校验 warn" in html, "要能显示权威校验结果"


def test_页面不自己判定枚举合法性():
    """回归（2026-09-24 真 bug）：页面曾拿值直接和字典 legal 列表比字符串，
    而 `CPLB` 我们的口径是「编号;名称」、字典里是 88 个**裸名称** →
    **每一份产物的 CPLB 都被误标成「不在字典枚举内」**。
    认定必须交给 erp_schema.validate_result（它按 `;` 切编号查树），页面只显示结论。
    """
    html = PAGE_PATH.read_text(encoding="utf-8")
    assert "legal.includes(text)" not in html, "别在前端重写枚举判定"
    assert "不在字典枚举内" not in html, "前端不该有这种判定文案"
    assert "校验 error" in html, "应改为显示服务端给的校验结论"


def test_校验载荷_优先用当次运行报告(tmp_path):
    """产物同目录有 `*.validation.json`（文件名跟**输入**、不跟产物名）→ 原样采用。"""
    p = _make_export(tmp_path, "t1", {"CPDL": "书版", "version": 2})
    report = {"customer": "ngv", "checked_fields": 63,
              "errors": [], "warnings": [], "summary": {"error": 0, "warn": 0},
              "reminders": [{"kind": "offer_deadline_expired"}]}
    (p.parent / "QUOTE.validation.json").write_text(
        json.dumps(report, ensure_ascii=False), encoding="utf-8")

    payload = ev.build_validation_payload(p, {"CPDL": "书版", "version": 2})
    assert payload["source"] == "run_report"
    assert payload["customer"] == "ngv"
    assert payload["errors"] == [] and payload["warnings"] == []
    assert payload["note"] is None, "用了权威报告就不需要免责声明"

    # 列表摘要也要带上客户与 error/warn 计数
    item = ev.scan_exports(tmp_path)[0]
    assert item["customer"] == "ngv"
    assert item["validation"] == {"source": "run_report", "error": 0, "warn": 0}


def test_校验载荷_报告坏掉不影响看产物(tmp_path):
    p = _make_export(tmp_path, "t2", {"CPDL": "书版"})
    (p.parent / "QUOTE.validation.json").write_text("{ 坏 json", encoding="utf-8")
    payload = ev.build_validation_payload(p, {"CPDL": "书版"})
    assert payload["source"] == "recomputed", "报告坏了应回退到现算，而不是报错"
    assert payload["note"], "回退时必须说明「未按客户档案豁免」"


def test_校验载荷_无报告时现算并声明局限(tmp_path):
    p = _make_export(tmp_path, "t3", {"CPDL": "书版", "KHDH": ""})
    payload = ev.build_validation_payload(p, {"CPDL": "书版", "KHDH": ""})
    assert payload["source"] == "recomputed"
    assert "未按客户档案豁免" in payload["note"]
    assert payload["customer"] is None


def test_校验载荷_CPLB编号写法不算非法(tmp_path):
    """★ 这个 bug 的根：CPLB 输出「编号;名称」，字典 legal 是裸名称。
    权威校验器认得编号写法（按 `;` 切），所以要判为「不是 error」。
    """
    p = _make_export(tmp_path, "t4", {"CPLB": "26.01;普通蛇圈书", "version": 2})
    (p.parent / "QUOTE.validation.json").write_text(
        json.dumps({"customer": "ngv", "errors": [], "warnings": [],
                    "summary": {"error": 0, "warn": 0}}, ensure_ascii=False),
        encoding="utf-8")
    payload = ev.build_validation_payload(p, {"CPLB": "26.01;普通蛇圈书", "version": 2})
    assert payload["errors"] == [], f"CPLB 编号写法不许判 error: {payload['errors']}"

    # 现算那条路也不许把编号写法判成 error
    (p.parent / "QUOTE.validation.json").unlink()
    payload2 = ev.build_validation_payload(p, {"CPLB": "26.01;普通蛇圈书", "version": 2})
    assert [i for i in payload2["errors"] if i.get("field") == "CPLB"] == [], payload2["errors"]

    # 而真正不存在的编号必须报出来（否则就是漏判）
    payload3 = ev.build_validation_payload(
        p, {"CPLB": "99.99;不存在的类别", "version": 2})
    assert [i for i in payload3["errors"] if i.get("field") == "CPLB"], payload3["errors"]


# ═══════════════════════════════════════════════════════════════════════
# HTTP 端到端
# ═══════════════════════════════════════════════════════════════════════
@pytest.fixture
def viewer(tmp_path, monkeypatch):
    """起一个真实（临时目录的）服务端，返回 (base_url, processed_dir)。"""
    _make_export(tmp_path, "tid1", {"version": 2, "CPMC": "产品甲", "CPLB": "26.01;普通蛇圈书",
                                    "CPSX": None, "ID": "tid1", "CPMC_ZH": "产品甲（中）",
                                    "Reminder": "报价截止日期 2026-06-26 已过期 90 天"},
                 stamp="20260924_010203", mtime=1_700_000_300)   # 较新
    _make_export(tmp_path, "tid0", {"CPMC": "老产物"}, rel_dir="_superseded_x/tid0",
                 stamp="20260924_010000", mtime=1_700_000_100)   # 较旧 + 已被取代
    monkeypatch.setattr(ev, "PROCESSED_DIR", tmp_path)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ev.Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", tmp_path
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)


def _get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def test_http_首页返回页面(viewer):
    base, _ = viewer
    status, body = _get(base + "/")
    assert status == 200
    assert "ERP" in body and "api/exports" in body


def test_http_exports_列出产物(viewer):
    base, processed_dir = viewer
    status, body = _get(base + "/api/exports")
    assert status == 200
    payload = json.loads(body)
    assert payload["count"] == 2
    assert payload["exports"][0]["task_id"] == "tid1", "最新在前"
    assert payload["exports"][0]["version"] == 2
    assert payload["exports"][1]["superseded"] is True
    assert payload["root"] == str(processed_dir), "要回显真正在扫的目录，方便排错"


def test_http_export_返回内容(viewer):
    base, _ = viewer
    rel = "tid1/ERP_tid1_20260924_010203.json"
    status, body = _get(f"{base}/api/export?path={rel}")
    assert status == 200
    payload = json.loads(body)
    assert payload["data"]["CPMC"] == "产品甲"
    assert payload["data"]["CPSX"] is None, "「无」→ null 要原样带过来"
    assert payload["data"]["version"] == 2
    # 取值校验由服务端权威实现给出，页面只显示
    assert "validation" in payload, "要带上校验结果，页面不自己判枚举"
    assert payload["validation"]["source"] in {"run_report", "recomputed", "unavailable"}


def test_http_export_拒绝越界(viewer):
    base, _ = viewer
    for bad in ("../etc/passwd", "..%2F..%2Fetc%2Fpasswd", "/etc/passwd", ""):
        status, _ = _get(f"{base}/api/export?path={bad}")
        assert status == 404, f"{bad!r} 应该 404"


def test_http_schema_含配置版本(viewer):
    base, _ = viewer
    status, body = _get(base + "/api/schema")
    assert status == 200
    payload = json.loads(body)
    assert payload["config_version"] == 2
    assert len(payload["erp_fields"]) == 59
    assert [f["key"] for f in payload["internal_fields"]][-1] == "version"
    assert payload["groups"]


def test_http_未知路径404(viewer):
    base, _ = viewer
    assert _get(base + "/api/nope")[0] == 404


# ═══════════════════════════════════════════════════════════════════════
# 与产物结构对齐
# ═══════════════════════════════════════════════════════════════════════
def test_字典与模板键集合一致():
    """字典（页面渲染用）与模板（产物落盘用）必须是**同一批 59 个键**。

    顺序**故意不同**，各自有各的用途，不要去对齐：
      * assets/add_customer_quote_request.json = 产物里键的位置（下游按位置读，权威）
      * assets/erp_form_schema.yaml           = ERP 表单里的排布，页面按语义分组渲染
    两边集合一旦漂移（加字段只改了一边），页面就会漏字段或多显示幽灵字段。
    """
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        tpl_keys = list(json.load(f).keys())
    schema_keys = [f["key"] for f in ev.build_schema_payload()["erp_fields"]]
    assert len(tpl_keys) == 59 and len(schema_keys) == 59
    assert set(tpl_keys) == set(schema_keys), (
        f"只在模板里: {sorted(set(tpl_keys) - set(schema_keys))}；"
        f"只在字典里: {sorted(set(schema_keys) - set(tpl_keys))}"
    )
