"""ERP 导出查看器（`CQR_Web/` 包：data.py + app.py + templates/index.html）回归测试。

业务 2026-09-24 四件事：
  ① 旧 HTML 是按**上一代**结构写的（SJFX / KHMBJ / GG_length…），全错，要按新结构重写；
  ② 要能**自动扫描** customer-files/processed 下的导出产物；
  ③ 产物要加内部字段 `version` 标识当前配置版本（目前 = 2）；
  ④ 把 `scripts/erp_viewer.py` 搬进 `CQR_Web/` 文件夹、用 Flask 重写。

这里钉住的是**机制**，不是某一份产物的具体值：
  * 扫描能递归找到全部 ERP_*.json、最新在前、_superseded 目录会被标出来
  * `path` 参数出不了扫描根（路径穿越）
  * 字段分组必须**恰好覆盖** 59 个 ERP 字段各一次 —— 加了字段忘了归组会直接红，
    避免新字段在页面上悄悄不显示
  * 页面不许再引外部 <script src>（旧版引 unpkg/jsdelivr，断网白屏）
  * 四个端点真的能应答；取值校验与交付闸门**只有服务端一份实现**
  * 分层不许退化：`data.py` 不 import Flask、页面不许有判定逻辑、转发壳里不许有业务逻辑
"""
from __future__ import annotations

import json
import os
import re
import threading
import urllib.request
from pathlib import Path

import pytest

import CQR_Web as ev
import erp_schema
from CQR_Web import create_app
from main import TEMPLATE_PATH

# 本文件在 CQR_Web/tests/ 里：parent=CQR_Web（项目根，也是包目录），parent.parent=技能仓库根
PROJ_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = PROJ_ROOT.parent
PKG_DIR = PROJ_ROOT
PAGE_PATH = PKG_DIR / "templates" / "index.html"



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
    # 服务端真的实现了这些路由 —— 直接问 Flask 的路由表（比 grep 源码稳）
    rules = {str(r) for r in create_app().url_map.iter_rules()}
    for route in ("/api/exports", "/api/export", "/api/schema", "/"):
        assert route in rules, f"服务端没实现 {route}；现有: {sorted(rules)}"


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
def viewer(tmp_path):
    """两份产物（一份最新 + 一份 _superseded）+ Flask 应用 → 返回 (test_client, 扫描根)。

    为什么不再起真 HTTP 服务（1.0.26 前的写法）：Flask 的 `test_client()` 直接走 WSGI，
    不用抢端口也不会偶发超时，测试从 2 秒级掉到毫秒级。**真服务器**的连通性另有一条
    测试（`test_真实服务器能起来并回应`），别让「测试全绿但其实起不来」蒙过去。
    """
    _make_export(tmp_path, "tid1", {"version": 2, "CPMC": "产品甲", "CPLB": "26.01;普通蛇圈书",
                                    "CPSX": None, "ID": "tid1", "CPMC_ZH": "产品甲（中）",
                                    "Reminder": "报价截止日期 2026-06-26 已过期 90 天"},
                 stamp="20260924_010203", mtime=1_700_000_300)   # 较新
    _make_export(tmp_path, "tid0", {"CPMC": "老产物"}, rel_dir="_superseded_x/tid0",
                 stamp="20260924_010000", mtime=1_700_000_100)   # 较旧 + 已被取代
    app = create_app(processed_dir=tmp_path)
    app.config["TESTING"] = True
    return app.test_client(), tmp_path


def _get(client, path: str) -> tuple[int, str]:
    r = client.get(path)
    return r.status_code, r.get_data(as_text=True)


def test_http_首页返回页面(viewer):
    client, _ = viewer
    status, body = _get(client, "/")
    assert status == 200
    assert "ERP" in body and "api/exports" in body


def test_http_exports_列出产物(viewer):
    client, processed_dir = viewer
    status, body = _get(client, "/api/exports")
    assert status == 200
    payload = json.loads(body)
    assert payload["count"] == 2
    assert payload["exports"][0]["task_id"] == "tid1", "最新在前"
    assert payload["exports"][0]["version"] == 2
    assert payload["exports"][1]["superseded"] is True
    assert Path(payload["root"]) == Path(processed_dir).resolve(), \
        "要回显真正在扫的目录，方便排错"


def test_http_export_返回内容(viewer):
    client, _ = viewer
    rel = "tid1/ERP_tid1_20260924_010203.json"
    status, body = _get(client, f"/api/export?path={rel}")
    assert status == 200
    payload = json.loads(body)
    assert payload["data"]["CPMC"] == "产品甲"
    assert payload["data"]["CPSX"] is None, "「无」→ null 要原样带过来"
    assert payload["data"]["version"] == 2
    # 取值校验由服务端权威实现给出，页面只显示
    assert "validation" in payload, "要带上校验结果，页面不自己判枚举"
    assert payload["validation"]["source"] in {"run_report", "recomputed", "unavailable"}


def test_http_export_拒绝越界(viewer):
    client, _ = viewer
    for bad in ("../etc/passwd", "..%2F..%2Fetc%2Fpasswd", "/etc/passwd", ""):
        status, _ = _get(client, f"/api/export?path={bad}")
        assert status == 404, f"{bad!r} 应该 404"


def test_http_schema_含配置版本(viewer):
    client, _ = viewer
    status, body = _get(client, "/api/schema")
    assert status == 200
    payload = json.loads(body)
    assert payload["config_version"] == 2
    assert len(payload["erp_fields"]) == 59
    assert [f["key"] for f in payload["internal_fields"]][-1] == "version"
    assert payload["groups"]


def test_http_未知路径404(viewer):
    client, _ = viewer
    status, body = _get(client, "/api/nope")
    assert status == 404
    assert "未知路径" in json.loads(body)["error"], "错误也回 JSON，且文案是中文"


def test_http_响应不许被缓存(viewer):
    """内部工具：每跑一单 processed/ 就变，浏览器缓存住会看到旧产物。"""
    client, _ = viewer
    r = client.get("/api/exports")
    assert r.headers.get("Cache-Control") == "no-store"


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


# ═══════════════════════════════════════════════════════════════════════
# 交付闸门：「CPMS 为空的 JSON 视为未完成，不得交付」要看得见
#
# 背景（2026-09-24，业务问出来的）：CPMS 与 CPMC_ZH 的中文品名都是 agent 亲手填的，
# 所以**管道刚跑完的产物天然是「未完成」**。以前这个状态只写在文档里 —— 一份样本
# 试跑的未完成产物在 processed/ 里躺了半天，最后是业务问「为什么这份缺少 CPMS」。
# 现在服务端算一份（delivery_gates），列表与详情页都要标出来。
# ═══════════════════════════════════════════════════════════════════════
def test_交付闸门_CPMS没写要报出来():
    gates = ev.delivery_gates({"CPMS": "", "CPMC_ZH": "KM/Aldi 甲乙丙 普通蛇圈书"})
    assert len(gates) == 1
    assert "CPMS" in gates[0]


def test_交付闸门_CPMS键整个缺失也算未写():
    """第 1 代产物 / 被剥掉内部字段的产物 —— 不能因为「没有这个键」就当通过。"""
    assert ev.delivery_gates({})


def test_交付闸门_空白CPMS算未写():
    assert ev.delivery_gates({"CPMS": "   \n\t "})


def test_交付闸门_中文品名占位符要报出来():
    gates = ev.delivery_gates({
        "CPMS": "【项目基本信息】…",
        "CPMC_ZH": "KM/Aldi 【中文品名待定】 其它套装/Postkarten in Box mit Aufsteller",
    })
    assert len(gates) == 1
    assert "中文品名" in gates[0] and "占位符" in gates[0]


def test_交付闸门_两处都补齐才算可交付():
    assert ev.delivery_gates({
        "CPMS": "【项目基本信息】…",
        "CPMC_ZH": "KM/Aldi 钻石贴纸美甲设计 普通蛇圈书/Basteln&Gestalten…",
    }) == []


def test_交付闸门_历史产物没有CPMC_ZH键不误报():
    """第 1 代 40 键产物里没有 CPMC_ZH；只按 CPMS 判，不拿「键不存在」报警。"""
    assert ev.delivery_gates({"CPMS": "老的产品描述", "CPSX": None}) == []


def test_扫描结果带交付闸门(tmp_path):
    _make_export(tmp_path, "unfinished", {"version": 2, "CPMS": "",
                                          "CPMC_ZH": "KM/Aldi 【中文品名待定】 x"},
                 stamp="20260924_010203", mtime=1_700_000_100)
    _make_export(tmp_path, "done", {"version": 2, "CPMS": "【项目基本信息】…",
                                    "CPMC_ZH": "KM/Aldi 甲 乙"},
                 stamp="20260924_020203", mtime=1_700_000_200)
    items = {it["task_id"]: it for it in ev.scan_exports(tmp_path)}
    assert len(items["unfinished"]["incomplete"]) == 2, "CPMS + 中文品名两处都没补"
    assert items["unfinished"]["cpms_len"] == 0
    assert items["done"]["incomplete"] == []
    assert items["done"]["cpms_len"] > 0


def test_坏文件不报交付闸门(tmp_path):
    """读不出来就没法判交付状态 —— 交回空列表，让「文件损坏」徽章去说事。"""
    d = tmp_path / "tid"
    d.mkdir(parents=True)
    (d / "ERP_tid_20260924_010203.json").write_text("{不是 json", encoding="utf-8")
    item = ev.scan_exports(tmp_path)[0]
    assert item["broken"]
    assert item["incomplete"] == []


def test_http_export_带回交付闸门(viewer):
    client, _ = viewer
    status, body = _get(client, "/api/export?path=tid1/ERP_tid1_20260924_010203.json")
    assert status == 200
    payload = json.loads(body)
    assert "incomplete" in payload, "页面不自己判交付状态，必须由服务端给"
    assert payload["incomplete"], "tid1 没写 CPMS → 必须报未完成"


def test_页面显示交付闸门():
    """页面只负责显示，判定逻辑不许抄一份到前端（会跟服务端漂）。"""
    page = PAGE_PATH.read_text(encoding="utf-8")
    assert "未完成 · 不得交付" in page
    assert "incomplete" in page


# ═══════════════════════════════════════════════════════════════════════
# Flask 版分层（1.0.26）：逻辑一份、路由一份、显示一份
#
# 这次重写的风险不是「能不能跑」，而是**长回一团**：把 Flask 路由和扫描逻辑混在
# 一个文件里、或者页面又自己算一遍判定。下面几条就是钉住分层别退化。
# ═══════════════════════════════════════════════════════════════════════
def test_真实服务器能起来并回应(tmp_path):
    """`test_client` 走 WSGI、绕过了真 socket —— 这里真起一次服务，确认连得上。

    别让「测试全绿但其实起不来」蒙过去（旧版是每个 HTTP 测试都起真服务，所以天然覆盖）。
    """
    from werkzeug.serving import make_server

    _make_export(tmp_path, "tid1", {"version": 2, "CPMS": "已写", "CPMC_ZH": "甲"},
                 stamp="20260924_010203")
    server = make_server("127.0.0.1", 0, create_app(processed_dir=tmp_path), threaded=True)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        with urllib.request.urlopen(base + "/api/exports", timeout=15) as r:
            assert json.loads(r.read().decode("utf-8"))["count"] == 1
        with urllib.request.urlopen(base + "/", timeout=15) as r:
            assert b"<!DOCTYPE html>" in r.read()
    finally:
        server.shutdown()
        server.server_close()
        t.join(timeout=5)


def test_两个应用的扫描根互不干扰(tmp_path):
    """旧版靠 `global PROCESSED_DIR` 改全局：`--dir` 与测试会互相污染。

    现在扫描根在 `app.config` 里，两个应用各扫各的。
    """
    dir_a, dir_b = tmp_path / "a", tmp_path / "b"
    _make_export(dir_a, "ta", {"CPMS": "甲"}, stamp="20260924_010203")
    _make_export(dir_b, "tb", {"CPMS": "乙"}, stamp="20260924_010203")
    _make_export(dir_b, "tb2", {"CPMS": "丙"}, stamp="20260924_010204")
    client_a, client_b = create_app(dir_a).test_client(), create_app(dir_b).test_client()
    assert client_a.get("/api/exports").get_json()["count"] == 1
    assert client_b.get("/api/exports").get_json()["count"] == 2
    # 再问一次 a：不许被 b 带跑
    assert client_a.get("/api/exports").get_json()["count"] == 1


def test_data层不依赖flask():
    """`data.py` 是纯逻辑（可单独测、可被别处复用）；Flask 只许出现在 app.py。"""
    src = (PKG_DIR / "data.py").read_text(encoding="utf-8")
    assert "from flask" not in src and "import flask" not in src


def test_转发壳里没有业务逻辑():
    """`scripts/erp_viewer.py` 只剩转发 —— 逻辑搬进 CQR_Web/ 后不许两处各留一份。"""
    src = (REPO_ROOT / "scripts" / "erp_viewer.py").read_text(encoding="utf-8")
    assert "from CQR_Web.app import main" in src
    for leaked in ("delivery_gates", "def scan_exports", "FIELD_GROUPS", "http.server"):
        assert leaked not in src, f"转发壳里不该有实现: {leaked}"
    assert len(src.splitlines()) < 40, "转发壳要够短，别又长回一个应用"


def test_模板里不许出现Jinja语法():
    """页面数据全部走 `/api/*` fetch。模板里一旦出现 `{{ }}`，Jinja 会把它当变量，
    要么渲染报错、要么把 JS 的一部分吃掉 —— 这种 bug 很难看出来。"""
    html = PAGE_PATH.read_text(encoding="utf-8")
    for token in ("{{", "{%", "{#"):
        assert token not in html, f"模板里出现了 Jinja 语法 {token!r}；数据请走 /api/*"


def test_JSON口径_不转义中文_不排序键_缩进两格(viewer):
    client, _ = viewer
    body = client.get("/api/schema").get_data(as_text=True)
    assert "\\u" not in body, "中文不该被转义（否则 curl 直读没法看）"
    assert '": ' in body, "要缩进（compact=False），方便人肉核对"
    # 键序按我们构造的顺序（sort_keys=False）—— 默认 provider 会按字母重排
    payload = client.get("/api/export?path=tid1/ERP_tid1_20260924_010203.json").get_json()
    assert list(payload) == ["relpath", "data", "validation", "incomplete"], list(payload)


def test_模块入口可跑():
    """`python -m CQR_Web --help` 能跑 —— CLI 参数在 app.main 里，别让它烂掉。"""
    import subprocess
    import sys

    r = subprocess.run([sys.executable, "-m", "CQR_Web", "--help"],
                       cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    for flag in ("--host", "--port", "--no-open", "--dir"):
        assert flag in r.stdout, f"CLI 少了 {flag}"


def test_端口被占用时给中文提示(capsys):
    """werkzeug 自己把 EADDRINUSE 消化掉了（英文报错 + `sys.exit(1)`），异常传不出来，
    所以 `main()` 起服务前先探一次端口 —— 这条钉住那个前置检查别被删掉。"""
    import socket

    from CQR_Web.app import main as cli_main

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        busy_port = sock.getsockname()[1]
        rc = cli_main(["--no-open", "--port", str(busy_port)])

    captured = capsys.readouterr()
    assert rc == 1, "端口被占用要返回 1"
    assert "端口已被占用" in captured.err, captured.err
    assert "换个端口试试" in captured.err, captured.err


# ═══════════════════════════════════════════════════════════════════════
# 排版 + 导出 PDF（1.0.27）
#
# 业务 2026-09-24（第七批）：「每个字段单独成一行不方便阅读，其次添加一个导出为 PDF 的按钮」
#   * 63 个字段一行一个要滚 ~2800px；TZS=1 这种短值把整行宽度白扔 → 改成自适应网格，
#     只有长值跨整行（版面细节由 jsdom 实跑页面验，这里钉住结构与「不许引 PDF 库」）。
# ═══════════════════════════════════════════════════════════════════════
def test_字段排版是自适应网格():
    html = PAGE_PATH.read_text(encoding="utf-8")
    assert ".field-grid" in html, "字段要装在网格容器里"
    assert "repeat(auto-fill, minmax(" in html, "按窗口宽度自动分列（窗口越宽列越多）"
    assert ".field.wide" in html and "grid-column: 1 / -1" in html, "长值要跨整行"
    assert "fieldIsWide" in html, "宽窄由值决定，不是写死字段名"


def test_三个字段容器都用了网格():
    """ERP 分组 / 内部字段 / 多出的键 —— 漏一个就有一组还是一行一个。"""
    html = PAGE_PATH.read_text(encoding="utf-8")
    assert html.count('class: "field-grid"') >= 3, html.count('class: "field-grid"')


def test_有导出PDF按钮():
    html = PAGE_PATH.read_text(encoding="utf-8")
    assert "导出 PDF" in html
    assert "window.print()" in html, "零依赖做法：交给浏览器打印 / 另存为 PDF"


def test_打印样式定好A4并藏掉导航():
    html = PAGE_PATH.read_text(encoding="utf-8")
    assert "@media print" in html
    block = html.split("@media print", 1)[1]
    for must in ("@page", "size: A4", "aside.list", ".toolbar", "pre.raw", "details.legal",
                 "pre.long", "break-inside: avoid"):
        assert must in block, f"打印样式里少了 {must}"


def test_导出PDF不许引外部库():
    """页面必须离线可用：PDF 由浏览器生成。

    钉的是**加载机制**（`<script src>` / `import` / `require` / CDN 域名），不是禁词 ——
    这条最初写成「不许出现 jsPDF 这些字」，结果被自己注释里提到的库名绊倒
    （跟 1.0.26 模板注释那次是同一类错误：拿词表当代理，代替不了真检查）。
    """
    html = PAGE_PATH.read_text(encoding="utf-8")
    assert not re.search(r"<script[^>]*\bsrc\s*=", html, flags=re.I), "不许外部脚本"
    for pattern in (
        r"\bimport\s+[^;\n]*\bfrom\s*['\"](?:jspdf|pdfmake|html2canvas|pdf-lib)",
        r"require\(\s*['\"](?:jspdf|pdfmake|html2canvas|pdf-lib)",
        r"cdn\.(?:jsdelivr|unpkg|cdnjs)",
        r"unpkg\.com|jsdelivr\.net",
    ):
        assert not re.search(pattern, html, flags=re.I), f"匹配到外部依赖: {pattern}"


def test_页面标题跟着产物走():
    """浏览器拿 `document.title` 当「另存为 PDF」的默认文件名。"""
    html = PAGE_PATH.read_text(encoding="utf-8")
    assert "function syncTitle" in html and "document.title" in html
