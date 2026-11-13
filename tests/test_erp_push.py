# -*- coding: utf-8 -*-
"""`scripts/erp_push.py` —— 流水线最后一步「推进 ERP」的回归测试（全离线）。

协议依据 `docs/erp-quote-create.md`（2026-11-12 版：明细 51 字段 = `products[0].fields`）。
这里钉住：

  1. 59 键 → 51 字段的映射（`PRODUCT_FIELD_MAP` / `QTY_SLOT_MAP` / 表头 4 个）
  2. **ERP 服务端 14 个必填**缺一个就拦下（否则 create 会留下空主表）
  3. 一条产品记录 = 一次 create（ERP 一次只收 1 条明细）
  4. `YWY=KM` 不传；`SFYPJ` 布尔 → 「是/否」；`CPSL1..10` → `qty_1..qty_10`
  5. 幂等 / 六种 ERP 响应落点 / not_writable 只剩 4 个
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import erp_push  # noqa: E402
import erp_mcp_export  # noqa: E402


def erp_payload(rec: dict, qty: int = 100):
    """单条记录 → (一次 create 的入参, 问题列表)。"""
    return erp_push.build_payload([rec], customer="NGV10", qty_override=qty)


def _rec(**over):
    """一个「14 个 ERP 必填全齐」的最小记录，单项测试只覆盖自己关心的键。"""
    rec = {
        "ID": "task-1", "KHZL": "NGV10", "YWY": "KM", "GDY": "肖也",
        "CPMC": "冬季找找图画书", "CPMC_ZH": "冬季找找图画书",
        "ECPMC": "Wimmelbücher (Winter)", "CPLB": "27.01;普通BB书",
        "CD": 240, "KD": 330, "GD": 0, "TZS": 10, "YYS": 10,
        "CPSL1": 0, "KHDDH": "LG26090401", "KHDH": "CU001",
        "CPMS": "书籍方向：【长书脊】产品规格：【240*330*0】共【10】款，每款【10】语言",
        # ↓ 14 个 ERP 必填里剩下的
        "ZDFS": "快线BB书", "BZFS": "纸箱包装", "TBLX": "不打卡板", "SHFS": "FOB(NGV)",
        "JHRQ": "2027/08/13", "WCRQ": "2027/08/06", "Remark1": "无",
        "CPSX": "三岁以上", "CPLX": "新产品", "Remark3": "无",
        "Remark4": "EN71",
        # 其余
        "FSCMC": "FSC Mix", "SFYPJ": False, "Remark": "", "CPSX_": "",
        "DWMC": "本", "CPDL": "书版", "Drop_SJFX": "", "PXH": 1,
        "YSLX": "FOB(拼柜)", "SHDQ": "FOB(NGV)", "ZXLB": "普通纸箱",
        "ZBLX": "A=B双坑纸板", "ZXFS": "平装", "DKMC": "B3B坑纸板",
        "chkBSYTB": False, "chkBSYJZX": False, "chkZDBJF": False, "chkXBJF": False,
        "BZ": "美元", "KHMBDJ": 0, "OLD_KHDDH": "", "BGMS": "", "CPMX": "",
    }
    rec.update(over)
    return rec


def _artifact(tmp_path: Path, records=None) -> Path:
    p = tmp_path / "task" / "ERP_task-1_20261111_100506.erp-import.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(records or [_rec()], ensure_ascii=False), encoding="utf-8")
    return p


class FakeClient:
    def __init__(self, responses):
        self.responses = responses if isinstance(responses, list) else [responses]
        self.calls: list[tuple[str, dict]] = []

    def initialize(self):
        return {"serverInfo": {"name": "fake"}}

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        idx = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[idx]


@pytest.fixture(autouse=True)
def _isolated_log(tmp_path, monkeypatch):
    monkeypatch.setattr(erp_push, "PUSH_LOG", tmp_path / "push_log.jsonl")
    yield


# ── 映射 ────────────────────────────────────────────────────
def test_51字段映射(tmp_path: Path):
    payload, problems = erp_push.build_payload([_rec()], customer="NGV10", qty_override=41000)
    assert problems == []
    p = payload["products"][0]
    assert p["name"] == "冬季找找图画书"
    assert p["spec"] == "240*330*0"
    assert p["qty"] == 410000   # --qty 是**单款** 41000，× 款数 10 = 落库总量 410000
    f = p["fields"]
    # 明细级（以前被我误判成「写不进」的那些，现在都能写）
    assert f["binding"] == "快线BB书"           # ZDFS
    assert f["packing"] == "纸箱包装"           # BZFS
    assert f["pallet"] == "不打卡板"            # TBLX
    assert f["delivery"] == "FOB(NGV)"          # SHFS
    assert f["delivery_date"] == "2027/08/13"   # JHRQ
    assert f["expected_date"] == "2027/08/06"   # WCRQ
    assert f["parts_desc"] == "无"              # Remark1
    assert f["modify_log"] == "无"              # Remark3
    assert f["inspection"] == "EN71"            # Remark4
    assert f["attribute"] == "三岁以上"         # CPSX
    assert f["product_type"] == "新产品"        # CPLX
    assert f["cases"] == 10                     # TZS
    assert f["languages"] == 10                 # YYS
    assert f["product_desc"].startswith("书籍方向")  # CPMS
    assert f["unit"] == "本"                    # DWMC
    assert f["product_group"] == "书版"         # CPDL
    assert f["customer_order_no"] == "CU001"    # KHDH（明细级 txtKHCPMC）
    assert f["product_name_en"] == "Wimmelbücher (Winter)"  # ECPMC
    assert f["length_mm"] == 240 and f["width_mm"] == 330 and f["height_mm"] == 0
    assert f["fsc_category"] == "FSC Mix"
    assert f["has_parts"] == "否"               # SFYPJ 布尔 → 是/否
    assert f["target_price_currency"] == "美元"
    assert f["carton_type"] == "普通纸箱" and f["board_type"] == "A=B双坑纸板"
    assert f["packing_orientation"] == "平装" and f["bottom_card"] == "B3B坑纸板"
    assert f["transport_type"] == "FOB(拼柜)"
    # ★ qty_1 不显式传：给了会盖掉 create() 对隐藏 txtCPSL 的补写
    assert "qty_1" not in f


def test_表头不传KM(tmp_path: Path):
    payload, _ = erp_push.build_payload([_rec(Remark="表头备注", CPMX="表头产品描述")],
                                        customer="NGV10", qty_override=1)
    assert payload["customer"] == "NGV10"
    assert payload["remark"] == "表头备注"
    assert payload["product_memo"] == "表头产品描述"
    assert "sales" not in payload and "merchandiser" not in payload


def test_空值不塞进fields(tmp_path: Path):
    payload, _ = erp_push.build_payload([_rec(Drop_SJFX="", OLD_KHDDH="", BGMS="")],
                                        customer="NGV10", qty_override=5)
    f = payload["products"][0]["fields"]
    assert "spine_direction" not in f and "old_hk_order_no" not in f and "extra_desc" not in f


def test_数量档位拆到qty_n(tmp_path: Path):
    """`CPSL1..10` 是**每款**档位 → 落库 `qty_n` 是**总量**（× 款数）。"""
    payload, _ = erp_push.build_payload([_rec(CPSL1=3000, CPSL2=5000, CPSL3=7000)],
                                        customer="NGV10")
    f = payload["products"][0]["fields"]
    assert "qty_1" not in f        # 第 1 档由顶层 qty 写
    assert f["qty_2"] == 50000 and f["qty_3"] == 70000
    # qty 必须等于第 1 档（总量），不是三档相加
    assert payload["products"][0]["qty"] == 30000


def test_单款数量乘款数(tmp_path: Path):
    """款数=1 时不放大；款数=10 时落库总量 = 单款 × 10。"""
    one, _ = erp_push.build_payload([_rec(TZS=1)], customer="NGV10", qty_override=3000)
    assert one["products"][0]["qty"] == 3000
    ten, _ = erp_push.build_payload([_rec(TZS=10)], customer="NGV10", qty_override=3000)
    assert ten["products"][0]["qty"] == 30000
    assert ten["products"][0]["fields"]["cases"] == 10


def test_SFYPJ字符串否不写反(tmp_path: Path):
    """产物里 `SFYPJ="否"` 是非空字符串，曾被判成 True 写成「是」。"""
    for raw, want in ((False, "否"), (True, "是"), ("否", "否"), ("是", "是")):
        payload, _ = erp_push.build_payload([_rec(SFYPJ=raw)], customer="NGV10",
                                            qty_override=1)
        assert payload["products"][0]["fields"]["has_parts"] == want, raw


# ── 模块自检 ────────────────────────────────────────────────
def test_MCP客户端还在(tmp_path: Path):
    """整理映射块时曾把 `McpHttpClient` 一起删掉（只在真连 ERP 时才炸）。

    这里钉住：客户端类与默认地址必须存在于 `erp_push` 命名空间。
    """
    assert hasattr(erp_push, "McpHttpClient") and hasattr(erp_push, "McpError")
    assert erp_push.DEFAULT_MCP_URL.startswith("https://") and erp_push.DEFAULT_MCP_URL.endswith("/mcp")


# ── 判定成败：status 优先于 bdbh ────────────────────────────
def test_带bdbh但status_failed不算成功(tmp_path: Path):
    """ERP `failed` / `partial` 也会返回 bdbh（主表已建、明细没落）。

    只看 bdbh 会把空单报成 created —— 2026-11-12 `BJXQ261112007` 就这么漏过去一次。
    """
    res = {"status": "failed", "ok": False, "bdbh": "BJXQ261112007",
           "detail_error": "产品明细行没有落库"}
    status, bdbh, err = erp_push._classify(res)
    assert status == "failed" and bdbh is None
    assert "空单" in err and "BJXQ261112007" in err


def test_真正created才报成功(tmp_path: Path):
    status, bdbh, err = erp_push._classify({"status": "created", "bdbh": "BJXQ261112009"})
    assert status == "created" and bdbh == "BJXQ261112009" and err is None


# ── 六种 ERP 响应落点 ──────────────────────────────────────
def test_产品档案不命中单独归类(tmp_path: Path):
    """档案挡住时主表已建 → 必须标成 archive_missing + 提示清空单，不能混进普通 error。"""
    res = {"raw": "Error executing tool erp_quote_create: 产品档案里找不到 '冬季找找图画书'"}
    status, bdbh, err = erp_push._classify(res)
    assert status == "archive_missing" and bdbh is None
    assert "冬季找找图画书" in err


def test_普通异常仍是error(tmp_path: Path):
    status, _, _ = erp_push._classify({"raw": "Error executing tool erp_quote_create: 会话过期"})
    assert status == "error"


# ── 服务端 pydantic schema 对齐（assets/erp_quote_create_schema.json） ──
def test_schema_tab已入库且字段数对得上(tmp_path: Path):
    """入参类型以服务端 `inputSchema` 为准，仓库里存一份当单一事实源。"""
    assert erp_mcp_export.SCHEMA_PATH.is_file(), "assets 里缺 schema 快照"
    assert len(erp_mcp_export.FIELD_TYPES) == 51
    assert erp_mcp_export.NUMERIC_FIELDS and erp_mcp_export.BOOLEAN_FIELDS
    # 所有我们映射的 MCP 字段名都必须在 schema 里
    known = set(erp_mcp_export.FIELD_TYPES)
    mapped = set(erp_mcp_export.PRODUCT_FIELD_MAP.values()) | set(
        erp_mcp_export.QTY_SLOT_MAP.values())
    assert mapped <= known, f"映射到 schema 里没有的字段：{mapped - known}"


def test_数字字段字符串会被掰成number(tmp_path: Path):
    """产物里尺寸/数量常是字符串，pydantic 要 number —— 不掰就 422 整单进不去。"""
    rec = _rec(CD="240", KD="330", GD="0", PXH="1", TZS="10", YYS="10", KHMBDJ="2.5")
    payload, problems = erp_payload(rec)
    assert problems == [], problems
    f = payload["products"][0]["fields"]
    for key, want in (("length_mm", 240), ("width_mm", 330), ("height_mm", 0),
                      ("sort_no", 1), ("cases", 10), ("languages", 10)):
        assert f[key] == want and isinstance(f[key], int), (key, f[key])
    assert f["target_price"] == 2.5 and isinstance(f["target_price"], float)


def test_布尔字段只接受布尔(tmp_path: Path):
    rec = _rec(chkBSYTB="false", chkZDBJF="是")
    payload, problems = erp_payload(rec)
    assert problems == [], problems
    f = payload["products"][0]["fields"]
    assert f["no_pallet"] is False and f["has_schedule"] is True


def test_数字字段塞不进数字就丢掉并报错(tmp_path: Path):
    """宁可少一个字段，也不要送错类型（类型错 pydantic 直接 422）。"""
    payload, problems = erp_payload(_rec(KHMBDJ="面议"))
    assert any("target_price" in p for p in problems)
    assert "target_price" not in payload["products"][0]["fields"]


def test_产品类别带上才不要求档案(tmp_path: Path):
    """协议 2026-11-12 新增：搜不到档案时给 `product_category` 即可建明细。"""
    payload, _ = erp_payload(_rec())
    assert payload["products"][0]["product_category"] == "27.01;普通BB书"
    empty, _ = erp_payload(_rec(CPLB=""))
    assert "product_category" not in empty["products"][0]


def test_validate_call能抓出类型错(tmp_path: Path):
    bad = {"customer": "NGV10", "products": [
        {"name": "x", "spec": "1*1", "qty": 10, "fields": {"cases": "10", "no_pallet": "否"}}]}
    problems = erp_mcp_export.validate_call(bad)
    assert any("cases" in p for p in problems)
    assert any("no_pallet" in p for p in problems)


# ── ERP 必填 ────────────────────────────────────────────────
# 2026-11-12 森西拍板：这 7 个客户常不给的，按业务口径兜底（见 FIELD_DEFAULTS /
# BINDING_BY_CATEGORY），**不再拦**；剩下 7 个仍然硬拦。
DEFAULTED_KEYS = ["ZDFS", "Remark1", "CPSX", "CPLX", "Remark3"]


@pytest.mark.parametrize("key", ["JHRQ", "WCRQ", "CPMS", "BZFS", "TBLX", "SHFS", "Remark4"])
def test_ERP必填缺一个就拦(key: str, tmp_path: Path):
    """14 个必填、非兜底项缺任何一个 → 不许推（否则 create 建了主表、明细被拒 = 空单）。"""
    rec = _rec(**{key: ""})
    _, problems = erp_push.build_payload([rec], customer="NGV10", qty_override=100)
    assert any("ERP 必填缺失" in p for p in problems), key


@pytest.mark.parametrize("key", DEFAULTED_KEYS)
def test_可兜底的必填缺了不拦但记进defaulted(key: str, tmp_path: Path):
    """兜底项缺了照样能推，但必须出现在 `defaulted[]` 里给业务复核。"""
    rec = _rec(**{key: ""})
    _, problems = erp_push.build_payload([rec], customer="NGV10", qty_override=100)
    assert problems == [], problems
    filled = {d["erp_field"] for d in erp_push.defaulted_fields([rec])}
    assert erp_push.PRODUCT_FIELD_MAP[key] in filled, key


def test_兜底值本身符合业务口径(tmp_path: Path):
    """CPSX→三岁以下、CPLX→新产品、Remark1/3→无、BB 类目→快线BB书。"""
    rec = _rec(ZDFS="", Remark1="", CPSX=None, CPLX="", Remark3="")
    payload, problems = erp_push.build_payload([rec], customer="NGV10", qty_override=100)
    assert problems == []
    fields = payload["products"][0]["fields"]
    assert fields["attribute"] == "三岁以下"
    assert fields["product_type"] == "新产品"
    assert fields["parts_desc"] == "无"
    assert fields["modify_log"] == "无"
    assert fields["binding"] == "快线BB书"


def test_非BB类目不瞎填装订(tmp_path: Path):
    """不是 BB 书就别替客户选装订方式 —— 让它照常拦住。"""
    rec = _rec(ZDFS="", CPLB="01.02;精装书")
    _, problems = erp_push.build_payload([rec], customer="NGV10", qty_override=100)
    assert any("binding" in p for p in problems)


def test_日期兜底只补空值(tmp_path: Path):
    """--date 只填 JHRQ/WCRQ 的空值，已有值不覆盖。"""
    rec = _rec(JHRQ="", WCRQ="")
    payload, _ = erp_push.build_payload([rec], customer="NGV10", qty_override=100,
                                        default_date="2027-03-15")
    fields = payload["products"][0]["fields"]
    assert fields["delivery_date"] == "2027-03-15"
    assert fields["expected_date"] == "2027-03-15"
    rec2 = _rec(JHRQ="2027/12/01", WCRQ="")
    payload2, _ = erp_push.build_payload([rec2], customer="NGV10", qty_override=100,
                                         default_date="2027-03-15")
    f2 = payload2["products"][0]["fields"]
    assert f2["delivery_date"] == "2027/12/01"
    assert f2["expected_date"] == "2027-03-15"


def test_必填齐全就放行(tmp_path: Path):
    _, problems = erp_push.build_payload([_rec()], customer="NGV10", qty_override=100)
    assert problems == []


# ── 一次 create 一条明细 ────────────────────────────────────
def test_三条记录拆三次create(tmp_path: Path):
    recs = [_rec(CPMC="甲书"), _rec(CPMC="乙书"), _rec(CPMC="丙书")]
    calls, problems = erp_push.build_calls(recs, customer="NGV10", qty=100)
    assert problems == []
    assert len(calls) == 3
    assert [c["products"][0]["name"] for c in calls] == ["甲书", "乙书", "丙书"]
    assert all(len(c["products"]) == 1 for c in calls)


def test_多记录问题带条号(tmp_path: Path):
    recs = [_rec(CPMC="甲书"), _rec(CPMC="【中文品名待定】")]
    _, problems = erp_push.build_calls(recs, customer="NGV10", qty=100)
    assert any(p.startswith("第2条") and "占位符" in p for p in problems)


# ── 门口三道闸 ──────────────────────────────────────────────
def test_数量未知不许推(tmp_path: Path):
    _, problems = erp_push.build_payload([_rec(CPSL1=0)], customer="NGV10")
    assert any("数量未知" in p for p in problems)


def test_CPMC占位符不许推(tmp_path: Path):
    _, problems = erp_push.build_payload([_rec(CPMC="", CPMC_ZH="【中文品名待定】")],
                                         customer="NGV10", qty_override=1)
    assert any("占位符" in p for p in problems)


def test_客户代号未知不许猜(tmp_path: Path):
    _, problems = erp_push.build_payload([_rec(KHZL="")], qty_override=1)
    assert any("客户代码未知" in p for p in problems)


def test_客户档案兜底取erp_khzl(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(erp_push, "_profile_customer_code", lambda pid: "NGVS16")
    payload, problems = erp_push.build_payload([_rec(KHZL="")], profile_id="ngv", qty_override=1)
    assert problems == []
    assert payload["customer"] == "NGVS16"


# ── 写不进的只剩 4 个 ──────────────────────────────────────
def test_not_writable只剩4个(tmp_path: Path):
    fields = erp_push.unwritable_fields([_rec(SFYPJ=True)])
    # 真实产物里 KHDQ 是空的，所以只报 CPLB + KHDDH；BDBH 由 ERP 生成也不报
    assert set(fields) == {"CPLB", "KHDDH"}
    assert fields["CPLB"] == "27.01;普通BB书"
    # 之前误判成写不进的，现在都在 fields 里了
    for k in ("CPMS", "TZS", "YYS", "ZDFS", "FSCMC", "KHDH", "ECPMC", "SFYPJ"):
        assert k not in fields


# ── 响应落点 ────────────────────────────────────────────────
def test_成功建单并幂等(tmp_path: Path):
    art = _artifact(tmp_path)
    client = FakeClient({"status": "created", "bdbh": "BJXQ261112001", "record_id": "u1"})
    rep = erp_push.push(art, client=client, customer="NGV10", qty=410000, confirm=True)
    assert rep["status"] == "created" and rep["bdbh"] == ["BJXQ261112001"]
    assert client.calls[0][1]["confirm"] is True
    again = erp_push.push(art, client=client, customer="NGV10", qty=410000, confirm=True)
    assert again["status"] == "skipped" and "重复建单" in again["reason"]


def test_干跑状态(tmp_path: Path):
    art = _artifact(tmp_path)
    client = FakeClient({"status": "needs_confirmation", "ok": True})
    rep = erp_push.push(art, client=client, customer="NGV10", qty=10, confirm=False)
    assert rep["status"] == "dry_run" and client.calls[0][1]["confirm"] is False


def test_产品档案找不到_是archive_missing(tmp_path: Path):
    """档案不命中 → 单独归类，并且报告要点出「已留下空单」。"""
    art = _artifact(tmp_path)
    client = FakeClient({"raw": "Error executing tool erp_quote_create: 产品档案里找不到 '冬季找找图画书'"})
    rep = erp_push.push(art, client=client, customer="NGV10", qty=410000, confirm=True)
    assert rep["status"] == "archive_missing"
    assert "产品档案里找不到" in rep["error"]
    assert rep["lost_header"] is True and "空单" in rep["hint"]


def test_部分成功要点出来(tmp_path: Path):
    art = _artifact(tmp_path, [_rec(CPMC="甲书"), _rec(CPMC="乙书")])
    client = FakeClient([{"status": "created", "bdbh": "BJXQ261112002"},
                         {"status": "failed", "message": "明细行没建出来"}])
    rep = erp_push.push(art, client=client, customer="NGV10", qty=10, confirm=True)
    assert rep["status"] == "partial"
    assert [r["status"] for r in rep["results"]] == ["created", "failed"]


def test_rejected带ERP原文(tmp_path: Path):
    art = _artifact(tmp_path)
    client = FakeClient({"status": "rejected", "message": "【产品数量】不能为空！"})
    rep = erp_push.push(art, client=client, customer="NGV10", qty=10, confirm=True)
    assert rep["status"] == "rejected" and "产品数量" in rep["error"]


def test_sidecar命名(tmp_path: Path):
    assert erp_push.sidecar_path(_artifact(tmp_path)).name == \
        "ERP_task-1_20261111_100506.erp-push.json"


# ── 推送时补日期：交货日期与预计下单日期可以**不是同一天** ──────────
#    背景（2026-11-13 NGV 发声故事书）：客户只给了 `Lieferung Q4 27`，
#    预计下单日期根本没给。客户档案的口径是「报价阶段这两个字段不落库」，
#    但 ERP 明细强制必填 → 只能在推送那一刻补，且不能把同一天塞进两个字段。
def test_日期兜底可按字段分别给():
    rec = _rec(JHRQ="", WCRQ="")
    calls, _ = erp_mcp_export.export_calls(
        [rec], customer="NGV10", qty=100,
        default_date={"JHRQ": "2027-12-31", "WCRQ": "2026-11-23"})
    fields = calls[0]["products"][0]["fields"]
    assert fields["delivery_date"] == "2027-12-31"
    assert fields["expected_date"] == "2026-11-23"


def test_日期兜底字符串仍然同值():
    rec = _rec(JHRQ="", WCRQ="")
    calls, _ = erp_mcp_export.export_calls([rec], customer="NGV10", qty=100,
                                           default_date="2027-03-15")
    fields = calls[0]["products"][0]["fields"]
    assert fields["delivery_date"] == "2027-03-15"
    assert fields["expected_date"] == "2027-03-15"


def test_产物已有的日期不被兜底覆盖():
    rec = _rec(JHRQ="2027/12/01", WCRQ="")
    calls, _ = erp_mcp_export.export_calls(
        [rec], customer="NGV10", qty=100,
        default_date={"JHRQ": "2027-12-31", "WCRQ": "2026-11-23"})
    fields = calls[0]["products"][0]["fields"]
    assert fields["delivery_date"] == "2027/12/01"
    assert fields["expected_date"] == "2026-11-23"
