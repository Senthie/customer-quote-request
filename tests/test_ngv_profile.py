"""NGV 客户档案回归测试。

分两层：

1. **合成用例**（一定会跑）—— 锁住 section_pairs 策略的机制、数量写法、档案结构。
   NGV 表单最坑的两件事都在这层：
     * 同一标签在不同分区含义不同（content 的 paper ≠ cover 的 paper）
     * 同一分区里标签会重复（封面纸 / 护封纸）
   这两件事靠「按行位置对齐」解决，任何把位置改成「按标签名匹配」的改动都会在这里红。

2. **真实样本回归**（语料库在时才跑）—— 用 4 份真实邮件附件 + 随包 ERP 导出，
   把「哪些字段该等于什么」逐条钉住。语料库在 customer-files/samples/ngv/
   （真实客户数据，被 .gitignore 排除，只在本机/CI 有样本时生效）。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from customer_profile import load_profile
from extract_fields import CustomerData, extract_from_workbook, scan_values
from main import map_fields, parse_single_quantity, _de_number

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLES = REPO_ROOT / "customer-files" / "samples" / "ngv"

NGV = load_profile("ngv")


# ── 档案结构 ────────────────────────────────────────────────────────────
def test_档案已校准且只用分区策略():
    assert NGV.status == "calibrated"
    assert NGV.rule("layout.strategies") == ["section_pairs"], (
        "NGV 表里 paper/printing/finishing 在 B 列反复出现，"
        "加上平铺策略( auto_pairs )会抓出一堆错值"
    )
    assert NGV.validate() == []


def test_分区表覆盖表单的十个分区():
    names = [(s.get("names") or ["?"])[0] for s in NGV.sections]
    assert names == ["enquiry", "schedule", "quantity", "versions", "content",
                     "cover", "finishing", "specials", "packaging", "notes"]


def test_分区声明里没有重复标签位置():
    """同一分区同一位置不能声明两次，否则前一处被静默吃掉。"""
    for section in NGV.sections:
        entries = section.get("entries") or []
        keys = [e.get("key") for e in entries if e.get("key")]
        assert len(keys) == len(set(keys)), f"{section.get('names')} 的 key 有重复"


# ── section_pairs 机制 ──────────────────────────────────────────────────
# 复刻 NGV 表单的关键形状：A 列分区、B 列标签、C 列起值，
# 且 content 与 cover 下各有两个 `paper`。
_FORM = [
    ["enquiry\nAnfrage", "My Sample Book", "", "", ""],
    ["quantity\nAuflage", "512580cps", "", "", ""],
    ["content", "extent\nUmfang", "36", "pp", "+ Cover"],
    ["Inhalt", "paper\nPapier", "120", "gsm", "woodfree white offset paper"],
    ["", "printing\nDruck", "4x4", "col.", ""],
    ["ends", "paper\nPapier", "", "", ""],              # 环衬纸（空）
    ["Vor-/Nachsatz", "printing\nDruck", "", "", ""],
    ["", "boards\nMGP", "", "", ""],
    ["cover\nUmschlag", "paper\nPapier", "230", "gsm art card C1S (FSC Mix)", ""],
    ["", "printing\nDruck", "4x0", "col.", ""],
    ["", "finishing\nVeredelung", "UV varnish", "", ""],
    ["jacket\nSchutzumschlag", "paper\nPapier", "", "", ""],   # 护封纸（空）
    ["", "printing\nDruck", "", "", ""],
    ["finishing\nVerarbeitung", "Bindung", "perfect binding", "210", "mm side"],
    ["", "back\nRücken", "square", "", ""],
]


def _extract(rows):
    from extract_fields import _extract_sections
    return _extract_sections(rows, NGV)


def test_分区位置对齐_同标签在不同分区含义不同():
    values, _, _, _ = _extract(_FORM)
    # content 的 paper = 内文纸；cover 的 paper = 封面纸；两处不能串
    assert values["text_material"].startswith("120 gsm woodfree")
    assert values["cover_material"].startswith("230 gsm art card C1S")


def test_分区位置对齐_同分区内重复标签按行序区分():
    values, _, _, _ = _extract(_FORM)
    # content 下 paper 出现两次：内文纸有值、环衬纸为空 → 环衬不该被填成内文的值
    assert "ends_material" not in values
    # cover 下 paper 出现两次：封面纸有值、护封纸为空 → 护封同样不该被串
    assert "jacket_material" not in values


def test_分区位置对齐_空值行不会让后面错位():
    """按序「消费」实现会在空行处错位（实测把联系人写进 notes），位置对齐不会。"""
    rows = _FORM + [
        ["", "padding\nWattierung", "", "", ""],
        ["notes\nBemerkungen", "注意：内文语言中立", "", "", ""],
        ["", "Ebers, Lea", "", "", ""],                 # 联系人
        ["", "35", "", "", ""],                         # 页脚数字（明确忽略）
    ]
    values, _, _, _ = _extract(rows)
    assert values["notes"] == "注意：内文语言中立"
    assert values["contact"] == "Ebers, Lea"


def test_分区值写法的行_B列自身即值():
    values, _, _, _ = _extract(_FORM)
    assert values["product_name"] == "My Sample Book"
    assert values["quantity"] == "512580cps"


def test_标签行断言不符时打WARN且记录为未识别():
    rows = [["enquiry\nAnfrage", "Book", "", ""],
            ["content", "extent\nUmfang", "36", "pp"]]
    broken = [rows[0], [rows[1][0], "something else", *rows[1][2:]]]
    values, _, _, unmatched = _extract(broken)

    assert "extent" not in values           # 结构不符 → 不硬取值
    assert "something else" in unmatched    # 记下来供校准


def test_A列的干扰文字不切换分区():
    """ends / jacket 不是分区，是行内子说明 —— 声明的分区名之外一律忽略。"""
    values, _, _, _ = _extract(_FORM)
    # 若 ends 被当成新分区，content 的行序会错位，text_printing_spec 会丢
    assert values["text_printing_spec"].startswith("4x4")


# ── 数量写法（NGV 是德式 + cps 后缀 + 单一总数）──────────────────────────
@pytest.mark.parametrize("text,expected", [
    ("512580cps", 512580),
    ("4 x 17.500", 70000),          # 德式千分位：17.500 = 17500
    ("4 x 17,5", 70),               # 逗号才是小数点
    ("193820", 193820),
    ("  70000 pcs ", 70000),
])
def test_单一数量解析(text, expected):
    assert parse_single_quantity(text) == expected


@pytest.mark.parametrize("text", ["0", "", "n/a", "待定"])
def test_取不到数量时返回None(text):
    assert parse_single_quantity(text) is None


def test_德式数字千分位判定():
    assert _de_number("17.500") == 17500
    assert _de_number("1.234.567") == 1234567
    assert _de_number("7.5") == 7.5
    assert _de_number("7,5") == 7.5


# ── 正则扫描 ────────────────────────────────────────────────────────────
def test_扫描_我方编号与FSC():
    blocks = ["Ebers, Lea", "PR-00093270", "230 gsm art card C1S (FSC Mix)", "x"]
    got = scan_values(blocks, NGV)
    assert got["our_ref"] == "PR-00093270"
    assert got["fsc"] == "FSC Mix"


def test_扫描_香港单号取自文件名():
    """附件文件名里的「香港单号 CU…」→ 规范键 khddh（落 KHDDH，不是 KHDH）。

    2026-09-24：此前它叫 khdh 并被写进 KHDH（客户单号），而模板第 43 键
    KHDDH（中文标签就是「香港单号」）永远是空的 —— 4 份权威 ERP 导出
    显示香港单号该进 KHDDH。
    """
    got = scan_values(["香港单号 CU26072301 A_100x155_Postkarten.xlsx"], NGV)
    assert got["khddh"] == "CU26072301"
    assert "khdh" not in got


def test_扫描_装订边要带单位才认():
    """精度要求：孤零零一个数字不能被当成装订边，必须带 `mm side` 才算。"""
    assert "binding_side" not in scan_values(["perfect binding", "210"], NGV)
    assert scan_values(["perfect binding 210 mm side"], NGV)["binding_side"] == "210"
    # 真实表里 `210` 与 `mm side` 分处相邻两格（haystack 按换行拼，\\s 可跨行）
    assert scan_values(["210", "mm side"], NGV)["binding_side"] == "210"


def test_扫描_装订方向原文只进CPMS不映射ERP():
    """`perfect binding at the top` → 扫出 `at the top`，**只给 CPMS 用**。

    业务口径（2026-10-20，实单 CU26070802 / Ostermalblock）：
    装订边位置（顶/底/左/右）和 ERP 的「长/短书脊」是两个维度，
    方向原文不许拿去反推 Drop_SJFX。
    """
    got = scan_values(["softcover, perfect binding at the top, square back"], NGV)
    assert got["binding_direction"] == "at the top"
    # 没有方向词的写法不许凭空造
    assert "binding_direction" not in scan_values(["perfect binding"], NGV)


def test_尺寸_按书写顺序落CD_KD不再比较长短():
    """业务 2026-10-21 改口径：**默认顺序 = 长 x 宽 x 高，按客户书写顺序落 CD/KD/GD，
    不比较长短边**（取代 2026-10-20 的「长边进 CD」）。

    依据：LG26090401 真实录入 `240 x 330` → CD=240 / KD=330。
    所以 148 x 210 → CD=148 / KD=210（旧口径会给 210/148）。
    """
    got = _map({"size": "148 x 210 mm portrait", "binding": "perfect binding"})
    assert (got["CD"], got["KD"]) == (148, 210), "客户先写 148 就落 CD=148"

    got = _map({"size": "280 x 210 mm landscape", "binding": "perfect binding"})
    assert (got["CD"], got["KD"]) == (280, 210)

    # 真实录入复核：240x330 → 240/330
    got = _map({"size": "240 x 330 mm portrait", "binding": "board book"})
    assert (got["CD"], got["KD"]) == (240, 330)


def test_书脊方向_由装订边位置推出长短():
    """`at the top` + 竖版 → 短书脊（业务真单就是【短书脊】）；判不出来才留空。

    1.0.35 立过一条「顶/底/左/右 反推不出长/短」的规矩 —— 那是建立在
    「CD/KD 顺序不确定」之上的。2026-10-20 拍板 CD=长/KD=宽 之后，
    方向 + 版式就是几何换算，不再需要猜：
      * 上/下边（top/head/bottom）→ 书脊沿**宽**边；
      * 左/右边（side/left/right）→ 书脊沿**长**边。
    （2026-10-21 起 CD/KD 按书写顺序落，148 x 210 → CD=148 宽边 / KD=210 长边。）
    """
    got = _map({
        "size": "148 x 210 mm portrait",
        "binding": "softcover, perfect binding at the top, square back",
        "binding_direction": "at the top",
    })
    assert (got["CD"], got["KD"]) == (148, 210)
    assert got["Drop_SJFX"] == "短书脊", "装订在顶部 → 书脊在 148 短边"

    got = _map({"size": "148 x 210 mm portrait", "binding": "perfect binding at the side",
                "binding_direction": "at the side"})
    assert got["Drop_SJFX"] == "长书脊", "装订在侧边 → 书脊在 210 长边"

    # 连方向都没有：留空（不猜）
    got = _map({"size": "148 x 210 mm portrait", "binding": "perfect binding"})
    assert not got["Drop_SJFX"]


def test_board_book落普通BB书():
    """LG26090401（2026-10-21 真实录入 CPLB=普通BB书）：附件写 `board book binding`。

    旧 map 没有 board book 这条 → 落成兜底 32.00;其他。
    """
    got = _map({"size": "240 x 330 mm portrait",
                "binding": "board book",
                "binding_instructions": "board book binding"})
    assert got["CPLB"].endswith("普通BB书"), got["CPLB"]


def test_书脊方向_给了装订边仍按装订边判():
    """有 `N mm side` 时判据不变：装订边 ≥ 较长边 → 长书脊，否则短书脊。"""
    got = _map({"size": "145 x 210 mm portrait", "binding_side": "210"})
    assert got["Drop_SJFX"] == "长书脊"
    got = _map({"size": "145 x 210 mm portrait", "binding_side": "145"})
    assert got["Drop_SJFX"] == "短书脊"


def test_扫描_连字符FSC写法要分开判真混用与同值():
    """`FSC-Mix` 与 `FSC Mix` 是**同一个值**，`FSC-Mix` 与 `FSC Recycled` 才是混用。

    两个真实单把这个坑踩全了：
      * CU26051601-01：同一份附件里 `FSC mix` / `(FSC Mix)` / `(FSC-Mix)` 混着写
        → 旧实现按字面判等 → 误当成 2 个取值 → FSCMC 被错留空；
      * CU26070802：纸张 `FSC-Mix`、备注 `FSC Recycled` → 只认空格分隔的正则
        把 `FSC-Mix` 漏掉 → 错落成 `FSC Recycled`。
    """
    # 同值（大小写 + 连字符差异）→ 正常落值
    got = scan_values(["art card C1S (FSC Mix)", "sticker paper (FSC-Mix)",
                       "text paper (FSC mix)"], NGV)
    assert got["fsc"].lower().replace("-", " ") == "fsc mix"

    # 真混用 → 留空 + 记账
    notes: list = []
    got = scan_values(["cover (FSC-Mix)", "noted: accept FSC Recycled"], NGV, notes=notes)
    assert "fsc" not in got, "FSC-Mix 与 FSC Recycled 是真混用 → 不唯一就留空"
    assert notes and "不唯一就留空" in notes[0]


def test_扫描_不唯一就留空并记账():
    """`unique: true` —— 同一份文件里扫出多个不同取值时不落值（业务 2026-09-24 拍板）。

    依据：CU26072301 的附件同时写着 FSC Recycled（盒底）/ FSC Mix（盒盖纸+卡片）/
    FSC 100%（竹底座），而权威 ERP 导出里 FSCMC 是**空的**。取「第一个命中」等于
    在造错值，所以宁可不填。
    """
    notes: list = []
    # 单值 → 正常落值
    got = scan_values(["230 gsm art card C1S (FSC Mix)"], NGV, notes=notes)
    assert got["fsc"] == "FSC Mix" and notes == []

    # 多值 → 不落值 + 记账（不能静默）
    got = scan_values([
        "board 1200 gsm grey board (FSC Recycled)",
        "cover 300 gsm art card (FSC Mix)",
        "base bamboo (FSC 100%)",
    ], NGV, notes=notes)
    assert "fsc" not in got, "多材质混用 FSC → 留空"
    assert notes and "fsc" in notes[0] and "不唯一就留空" in notes[0]

    # 同一个值重复出现多次不算「不唯一」
    got = scan_values(["FSC Mix", "FSC Mix", "FSC Mix"], NGV, notes=notes)
    assert got["fsc"] == "FSC Mix"


# ── 2026-09-24 第二批业务口径（YYS / PXH / 配件 / 修改记录）──────────────
def _map(values: dict, profile=None):
    """用 NGV 档案把一组规范键映射成 59 字段（不落盘、不打印）。

    profile 可传变体（如改过 rules.cpmc.cn_name 的副本）以测「档案开关改了就生效」。
    """
    template = json.loads(
        (REPO_ROOT / "assets" / "add_customer_quote_request.json").read_text(encoding="utf-8"))

    class _Quiet:
        def info(self, *a, **k): pass
        def warn(self, *a, **k): pass
        def error(self, *a, **k): pass
        def field_mapping(self, *a, **k): pass

    return map_fields(CustomerData(values=values), template, profile or NGV, _Quiet())


def test_款数语言数只看邮件最新一封():
    """LG26090401（2026-10-21 真实录入 TZS=10 / YYS=10）：
    附件是德文 Titel+Mengen 表，款数/语言数只在正文，且引用区里还有旧单的 6/4。
    """
    from main import versions_from_email

    body = ("There are 10 titles in 10 languages based on standard language change:\n"
            "\nFrom: kevinlai\n6 titles 4 languages\n")
    titles, langs = versions_from_email(NGV, {"subject": "A00142 Wimmelbücher (Winter)",
                                             "body": body})
    assert (titles, langs) == (10, 10), "新封是 10/10，旧封的 6/4 不许串味"


def test_没有邮件上下文时款数语言数返回None():
    from main import versions_from_email
    assert versions_from_email(NGV, None) == (None, None)


def test_数量写法_带总数时以总数为准():
    """`3 x 7000 cps. = 21.000 cps`（PR-00091345 实测）必须算 21000，不能算 3。

    旧实现只认整串 `^N x M$`，这种带「= 总数」的写法匹配失败后退回「取第一个数字」，
    静默落成 3（真值 21000）—— 数量错了整单报价都错，所以三种写法都要钉住。
    """
    assert parse_single_quantity("3 x 7000 cps. = 21.000 cps") == 21000
    assert parse_single_quantity("4 x 17.500") == 70000
    assert parse_single_quantity("512580cps") == 512580
    assert parse_single_quantity("0") is None

    # 英文千分位也要认（17,500 = 17500）
    assert parse_single_quantity("4 x 17,500") == 70000
    # 纯文本里多个数字、没有可识别写法 → 宁可留空，不许悄悄取第一个
    assert parse_single_quantity("ca. 3 Stück, Lieferung 2027") is None


def test_数量写法_乘法与总数不一致要告警并取总数():
    """客户自己写了总数就以总数为准，但差异必须打出来（不静默选一个）。"""
    warnings: list = []

    class _Rec:
        def info(self, *a, **k): pass
        def warn(self, msg, *a, **k): warnings.append(str(msg))

    assert parse_single_quantity("3 x 7000 cps. = 20.000 cps", _Rec()) == 20000
    assert warnings and "不一致" in warnings[0]


def test_决策_YYS提取不到就落0():
    """业务 2026-09-24：「提取不到就为 0」—— 邮件可能后补而附件没更新，0 是待核对信号。

    三种情况必须分得清：
      `12 titles, 5 languages` → 5
      `4 titles, 1 language`   → 留空（单语言不落库）
      `8 titles`（没有语言数）  → 0（不许留模板默认 1，也不许留空）
    """
    assert _map({"versions": "12 titles, 5 languages"})["YYS"] == 5
    assert _map({"versions": "4 titles, 1 language"})["YYS"] == ""
    assert _map({"versions": "8 titles"})["YYS"] == 0, "提取不到 → 0"
    assert _map({})["YYS"] == 0, "客户文件里根本没有语言数 → 0"


def test_extras与纯标签分开_CPMS素材不再被标题污染():
    """NGV 的栏目标题（`TPS\nFormat`、`back\nRücken`…）不许落进 extras_found。

    实测（PR-00091345）：以前这些标题塞在 `extras:` 里，于是 extras_found（「CPMS 素材」）
    里出现一堆 `TPS\nFormat`，看起来像漏了信息，实际只是标题格、不含值。
    现在它们在 `ignored_labels:` 里 —— 既不算素材，也不算「认不出的标签」。
    """
    assert NGV.extras == [], "NGV 表单没有「非 ERP 附加信息」栏位，extras 应为空"
    assert NGV.is_ignored_label("TPS\nFormat")
    assert NGV.is_ignored_label("back\nRücken")
    assert not NGV.is_ignored_label("230 gsm art card C1S"), "含值的不算纯标签"
    assert not NGV.is_known_extra("TPS\nFormat"), "纯标题不是「附加信息」"

    """PXH 模板默认是 1，档案写 '' 才真的能清掉（业务 2026-09-24 拍板留空）。"""
    tpl = json.loads(
        (REPO_ROOT / "assets" / "add_customer_quote_request.json").read_text(encoding="utf-8"))
    assert tpl["PXH"] == 1, "模板默认仍是 1 —— 所以档案必须显式留空才有效果"
    assert _map({})["PXH"] == "", "PXH 必须留空（显式留空，不是沿用模板默认）"


def test_决策_配件与修改记录推不出就留空():
    """SFYPJ / Remark1 / Remark3 一律留空 —— 不再写死「否」，也不猜（业务 2026-09-24）。

    CU26072301 的导出是 `是` / `自厂购`（该单确实有配件），
    我们写死「否」是错的；Remark3 我们产出的就是第一版，不存在修改记录。
    """
    r = _map({})
    assert r["SFYPJ"] == "" and r["Remark1"] == "", "配件推不出就留空"
    assert r["Remark3"] == "", "第一版报价没有修改记录"
    assert r["BZ"] == "美元" and r["CPDL"] == "书版", "这两个业务要求我们填"


# ── 真实样本回归（语料库存在时才跑）──────────────────────────────────
def _load_manifest():
    manifest = SAMPLES / "manifest.json"
    if not manifest.exists():
        return None
    with open(manifest, encoding="utf-8") as f:
        return json.load(f)


MANIFEST = _load_manifest()
needs_samples = pytest.mark.skipif(
    MANIFEST is None,
    reason="缺少 NGV 校准语料库 customer-files/samples/ngv/（真实客户数据，不入库）",
)


def _run(attachment: str) -> dict:
    """跑一遍映射（不落盘），返回 59 字段结果。"""
    profile = load_profile("ngv")
    data = extract_from_workbook(str(SAMPLES / attachment), profile, filename=attachment)
    template_path = REPO_ROOT / "assets" / "add_customer_quote_request.json"
    with open(template_path, encoding="utf-8") as f:
        template = json.load(f)

    class _Quiet:
        def info(self, *a, **k): pass
        def warn(self, *a, **k): pass
        def error(self, *a, **k): pass
        def field_mapping(self, *a, **k): pass

    return map_fields(data, template, profile, _Quiet()) | {"__extract__": data}


SET_ORDERS = {"CU26061701", "CU26072301"}   # 这两单的 CPLB 顶层是「套装」


@needs_samples
@pytest.mark.parametrize("order", ["CU26051601-01", "CU26061701", "CU26072301", "CU26072302"])
def test_真实样本_关键字段(order):
    pair = next(p for p in MANIFEST["pairs"] if p["order"] == order)
    result = _run(pair["attachment"])

    # 附件 → 规范键（校准的核心产出）
    # 2026-10-20 重排后：装订方式先行、冲形状/PAD 细分（见 ngv.yaml cplb.map 的长注释）。
    #   CU26051601-01 贴纸骑马钉 → 24.03；CU26061701/01 套装 → 29.04；
    #   CU26072302 的 specials 里写着 `open pad binding at long side (210 mm)` → 23.06 ✓
    #   （当初 23.06 就是从这单反查来的；错的是把它泛化成了「perfect binding 一律 23.06」）
    assert result["CPLB"].split(";")[0] in ("24.03", "23.06", "23.01", "29.04")
    assert result["GD"] == 0, "NGV 的 ERP 导出里 GD 恒为 0（rules.thickness.enabled=false）"
    if order in SET_ORDERS:
        # 业务决定（2026-09-24）：套装（盒子）录系统尺寸一律写 0/0/0，
        # 即使附件给了盒内单件的 TPS（160x104 / 100x155）也不采用。
        assert (result["CD"], result["KD"], result["GD"]) == (0, 0, 0)
        assert result["Drop_SJFX"] == "", "套装没有书脊方向（rules.spine.skip_tops）"
    else:
        assert result["CD"] and result["KD"], "非套装必须把 TPS 尺寸解析出来"
    assert result["ZDFS"] in ("骑马钉", "胶装/PAD书", "套装")
    # FSC 类别：**不唯一就留空**（业务 2026-09-24 拍板）。
    # CU26072301 同一份附件里同时出现 FSC Recycled（盒底）/ FSC Mix（盒盖纸+卡片）/
    # FSC 100%（竹底座），权威 ERP 导出里 FSCMC 是空的 —— 扫到第一个就填是错的。
    # 只有一个 FSC 取值的那几单仍然照填。
    if order == "CU26072301":
        assert result["FSCMC"] == "", "多材质混用 FSC → 不唯一就留空"
        assert any("fsc" in n for n in result["__extract__"].scan_notes), \
            "留空的原因必须记在 scan_notes 里，不能静默"
    else:
        assert result["FSCMC"] == "FSC Mix"
    assert result["DWMC"] in ("本", "套")
    assert result["Remark4"], "NGV 的检测清单是客户常量，必须带上"
    assert result["CPMS"] == "", "CPMS 只能由 agent 手写，脚本不许生成"
    # 业务决定（2026-09-24）：JHRQ / WCRQ 都不落库，ZBLX 默认留空
    assert result["JHRQ"] == "" and result["WCRQ"] == "", "交货/下单日期不落库"
    assert result["ZBLX"] == "", "纸板类型不由我们产出（ERP/业务侧填）"
    # 业务决定（2026-09-24 第二批）：PXH 显式留空（模板默认 1 必须清掉）；
    # SFYPJ / Remark1 推不出就留空（写死「否」是错的 —— CU26072301 有配件）。
    assert result["PXH"] == "", "排序号显式留空（模板默认 1 必须被清掉）"
    assert result["SFYPJ"] == "" and result["Remark1"] == "", "配件推不出就留空"
    assert result["Remark3"] == "", "第一版报价没有修改记录 → 留空"
    assert result["CPDL"] == "书版" and result["BZ"] == "美元", "业务 2026-09-24：我们填"


@needs_samples
def test_真实样本_数量与款数语言数():
    r1 = _run(next(p for p in MANIFEST["pairs"] if p["order"] == "CU26061701")["attachment"])
    assert r1["CPSL1"] == 512580 and r1["TZS"] == 12 and r1["YYS"] == 5

    r2 = _run(next(p for p in MANIFEST["pairs"] if p["order"] == "CU26072302")["attachment"])
    assert r2["CPSL1"] == 70000          # 4 x 17.500
    # 附件写 `4 titles, 1 language` → 单语言**不落库**（业务决定 2026-09-24，
    # 依据：两单单语言的 ERP 记录里 YYS 都是空的）
    assert r2["TZS"] == 4 and r2["YYS"] == ""

    r3 = _run(next(p for p in MANIFEST["pairs"] if p["order"] == "CU26072301")["attachment"])
    assert r3["TZS"] == 4 and r3["YYS"] == ""

    # 附件没有语言数 → **落 0**（业务 2026-09-24 拍板：「提取不到就为 0，因为客户的
    # 邮件可能后面补充了某些内容，但是附件没有更新」）。0 是「待业务核对」的信号：
    # 这一单 ERP 里是 3，只能来自邮件/历史单，附件推不出来。
    r4 = _run(next(p for p in MANIFEST["pairs"] if p["order"] == "CU26051601-01")["attachment"])
    assert r4["TZS"] == 8
    assert r4["YYS"] == 0, "附件没有语言数 → 落 0（不许留模板默认 1，也不许留空）"


@needs_samples
def test_真实样本_香港单号落KHDDH而不是KHDH():
    """香港单号 → KHDDH；KHDH（客户单号）在报价阶段是空的。

    2026-09-24 修正：这两个键只差一个字母，此前把附件里的「香港单号」
    写进了 KHDH，KHDDH 永远是空。4 份权威 ERP 导出：KHDDH=单号、KHDH 空。
    """
    for order in ("CU26051601-01", "CU26072301", "CU26072302"):
        pair = next(p for p in MANIFEST["pairs"] if p["order"] == order)
        result = _run(pair["attachment"])
        assert result["KHDDH"] == order, f"{order}: 香港单号必须落到 KHDDH"
        assert result["KHDH"] == "", f"{order}: 客户单号附件里没有 → 必须留空"

    # ⚠️ CU26061701 的附件文件名与表内都没有单号 → 两个都空，
    #    必须由邮件标题/正文补（不能拿 our_ref PR-00081126 冒充）。
    o2 = next(p for p in MANIFEST["pairs"] if p["order"] == "CU26061701")["attachment"]
    result2 = _run(o2)
    assert result2["KHDDH"] == "" and result2["KHDH"] == "", (
        "这份附件里没有单号；不能拿 our_ref(PR-00081126) 冒充"
    )


# ── 单号分流（2026-09-24）────────────────────────────────────────────
def test_两组单号规则各归各位():
    """香港单号→KHDDH，客户单号→KHDH，两组规则互不顶替、互不误捕。"""
    from ingest_email import extract_numbers

    numbers, sources = extract_numbers(NGV, "NGV enquiry CU26072301 Postkarten in Box", "")
    assert numbers.get("KHDDH") == "CU26072301"
    assert "KHDH" not in numbers, "CU 号是香港单号，不许写进客户单号 KHDH"
    assert "标题" in sources["KHDDH"]

    # 客户自己的 PO 号才进 KHDH
    numbers2, _ = extract_numbers(NGV, "NGV enquiry - PO no.: AB-12345", "")
    assert numbers2.get("KHDH") == "AB-12345"


def test_客户单号正则不许被单词前缀误捕():
    """`Postkarten` 里的 `Po` 曾被当成 PO 号（抓出 `stkarten`）。"""
    from ingest_email import extract_numbers

    numbers, _ = extract_numbers(NGV, "Postkarten in Box mit Aufsteller", "")
    assert "KHDH" not in numbers


def test_档案单号规则声明了目标字段():
    rules = {target for _, _, target in NGV.number_rules()}
    assert rules == {"KHDH", "KHDDH"}
    # 客户单号明确不写 attachment_keys（附件里那个是香港单号）
    khdh_cfg = dict((k, c) for k, c, _ in NGV.number_rules())["khdh"]
    assert not khdh_cfg.get("attachment_keys")


@needs_samples
def test_真实样本_产出与模板键完全一致():
    """前 59 键与 ERP 模板完全一致（键集合 + 顺序），其后只追加声明的内部字段。"""
    from internal_fields import load_internal_fields

    template_path = REPO_ROOT / "assets" / "add_customer_quote_request.json"
    with open(template_path, encoding="utf-8") as f:
        template_keys = list(json.load(f).keys())
    internal_keys = [str(f["key"]) for f in load_internal_fields()]
    for pair in MANIFEST["pairs"]:
        result = _run(pair["attachment"])
        keys = [k for k in result if not k.startswith("_")]
        assert keys == template_keys, f"{pair['order']} 的 ERP 键顺序/数量与模板不一致"
        # 内部字段在 step6 才追加（map_fields 只产出 59 键 + 下划线键）
        assert internal_keys == ["ID", "CPMC_ZH", "Reminder", "version"]


# ── 端到端：客户识别必须看正文 ──────────────────────────────────────────
@needs_samples
def test_识别必须看正文_文件名撞车也不许判错(tmp_path):
    """回归（2026-09-24 真 bug）：NGV 附件文件名里含 "Colour"，命中了 igl 的
    文件名关键词（10 分），只凭文件名的识别就把整单判成了 igl —— 而正文里
    NGV 的特征（enquiry / Angebotsfrist / VK-Verpackung，500 分）压根没被看过。
    后果是整单字段全错：CPLB 落到「32.00;其他」、erp_fixed 一个没上、
    日期还按公共默认算了 +10 天。

    现在表格类文件一律把**正文**一起送进识别，文件名只当加分项。
    """
    pair = next(p for p in MANIFEST["pairs"] if p["order"] == "CU26061701")
    attachment = pair["attachment"]
    # 前提：这个文件名确实会撞上 igl 的 filename_keywords（含 "colour"）
    assert "colour" in attachment.lower()

    workdir = tmp_path / "e2e"
    pending = workdir / "pending"
    pending.mkdir(parents=True)
    target = pending / attachment
    shutil.copy2(SAMPLES / attachment, target)

    run = subprocess.run(
        ["uv", "run", "./scripts/main.py", str(target),
         "--processed-dir", str(workdir / "processed")],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=300,
    )
    assert run.returncode == 0, f"main.py 失败:\n{run.stdout}\n{run.stderr}"
    assert "选定客户档案: ngv" in run.stdout, (
        "文件名撞车时仍须按正文识别到 ngv；判成 igl 就是回退到 2026-09-24 的 bug\n"
        + "\n".join(line for line in run.stdout.splitlines() if "档案" in line or "识别" in line)
    )
    assert "识别得分偏低" not in run.stdout

    erp_files = [p for p in (workdir / "processed").glob("*/*.json")
                 if p.name.startswith("ERP_")]
    assert len(erp_files) == 1, erp_files
    result = json.loads(erp_files[0].read_text(encoding="utf-8"))
    # 这些值只有用 ngv 档案才会出现（判成 igl 时全都不对）
    assert result["CPLB"].split(";")[0] == "29.04"
    assert result["SHFS"] == "FOB(NGV)"
    assert result["DWMC"] == "套"
    assert (result["CD"], result["KD"], result["GD"]) == (0, 0, 0)
    assert result["JHRQ"] == "" and result["WCRQ"] == ""

@needs_samples
def test_真实样本_extras_found不再混入纯标签():
    """真实样本上再确认一次：纯标签格不进 extras_found（CPMS 素材）。"""
    result = _run(next(p for p in MANIFEST["pairs"]
                       if p["order"] == "CU26072302")["attachment"])
    found = result["__extract__"].extras_found
    for junk in ("TPS\nFormat", "back\nRücken", "notes\nBemerkungen"):
        assert junk not in found, f"{junk!r} 是纯标签格，不该出现在 CPMS 素材里"


# ══ 2026-09-24 第三批业务口径 ════════════════════════════════════════════
# 业务逐条拍板的四条：
#   ① CPMC 的业务员 = KM（不再留【业务员待填】）
#   ② KHDH / KHDDH 默认就是空（香港单号扫不到不算缺陷）
#   ③ CPSX 取不到就显示「无」
#   ④ 超过报价截止日期必须提醒业务

def test_决策_CPMC是产品名称_业务员落YWY():
    """业务 2026-10-19（实单 CU26070802）更正：

    * `CPMC` **就是产品名称**，由 agent 从客户文件译出 —— 不带业务员、品牌（Aldi/KiK）、
      类别、FSC。脚本不会翻译 → 两侧都写占位符，交付前必须被替换。
    * 业务员代号 **KM 落 `YWY`**，不再作 CPMC 前缀（此前 `KM/Aldi …` 是错的）。
    * Wire-O → CPLB 26.07 普通YO书（真实 ERP 反查过）。
    """
    values = {"product_name": "Basteln&Gestalten Diamantsticker Nageldesign",
              "fsc": "FSC Mix", "binding_instructions": "Wire-O Binding"}
    r = _map(values)
    assert r["CPLB"] == "26.07;普通YO书", r["CPLB"]
    # 产品名由 agent 翻译 → 脚本给可见占位符，不带任何前缀
    assert r["CPMC"] == "【中文品名待定】", r["CPMC"]
    assert r["_cpmc_zh"] == "【中文品名待定】", r["_cpmc_zh"]
    # 业务员落 YWY
    assert r["YWY"] == "KM", r["YWY"]

    # 译好之后（= 改产物里的两个字段）：CPMC 是纯中文品名，不触发「含中文」告警
    import dataclasses

    filled = dataclasses.replace(
        NGV, rules={**NGV.rules,
                    "cpmc": {**NGV.rules["cpmc"], "template": "钻石贴纸美甲设计",
                             "zh_template": "钻石贴纸美甲设计"}})
    r2 = _map(values, profile=filled)
    assert r2["CPMC"] == "钻石贴纸美甲设计"
    assert r2["_cpmc_zh"] == "钻石贴纸美甲设计"


def test_决策_产品英文名落ECPMC():
    """业务 2026-10-20：`CPMS` 有了、`ECPMC`（产品英文名）却空着 —— 必须落客户原文英文品名。

    ERP 里 `CPMC` / `ECPMC` 是两个独立字段（`txtCPMC` 产品名称 / `txtECPMC` 产品英文名）。
    NGV 的 CPMC 现在是中文译名，英文名没有别的地方去 → `ECPMC`。
    口径：**照抄客户文件原文**，不翻译、不拼类别、不带 FSC。
    """
    r = _map({"product_name": "Ostermalblock", "fsc": "FSC Mix"})
    assert r["ECPMC"] == "Ostermalblock", r["ECPMC"]
    assert r["CPMC"] == "【中文品名待定】"      # 中文仍由 agent 译，两者互不影响

    # 没声明 rules.ecpmc 的客户不产出该字段（不动 igl 基线）
    import dataclasses
    no_ecpmc = dataclasses.replace(
        NGV, rules={k: v for k, v in NGV.rules.items() if k != "ecpmc"})
    assert _map({"product_name": "Ostermalblock"}, profile=no_ecpmc)["ECPMC"] == ""


def test_决策_未开allow_chinese的客户_CPMC中文要报警():
    """默认口径仍然有效：没声明 `cpmc.allow_chinese` 的客户，CPMC 里不许有中文。"""
    import dataclasses

    bad = dataclasses.replace(
        NGV, rules={**NGV.rules,
                    "cpmc": {**NGV.rules["cpmc"], "allow_chinese": False,
                             "template": "中文品名 {product_name}"}})
    warnings: list = []

    class _Rec:
        def info(self, *a, **k): pass
        def warn(self, msg, *a, **k): warnings.append(str(msg))
        def error(self, *a, **k): pass
        def field_mapping(self, *a, **k): pass

    template = json.loads(
        (REPO_ROOT / "assets" / "add_customer_quote_request.json").read_text(encoding="utf-8"))
    map_fields(CustomerData(values={"product_name": "Sticker Book"}), template, bad, _Rec())
    assert any("CPMC 里出现了中文" in w for w in warnings), warnings


def test_决策_无落JSON_null():
    """业务 2026-09-24：「CPSX 或者其他值的『无』使用 json 里的 null 填充」。

    链路：档案 `rules.cpsx.when_missing: 无` → `rules.null_values: ["无"]` 把「无」翻成
    **JSON null**（不是字符串「无」）。这样最干净：ERP 自身导出里空值就是 null，
    null 属空值、不触发下拉枚举校验 —— 于是 1.0.21 的 `enum_exceptions` 声明也随之删掉了。

    三档必须分得清：
      推不出来 → null（客户口径「无」）
      能推出来 → 中文枚举（不受影响）
      其他客户  → 公共默认仍是「留空 + WARN」，不会跟着变成 null
    """
    assert NGV.rule("cpsx.when_missing") == "无"
    assert NGV.rule("null_values") == ["无"], "「无」→ null 的口径在档案里（可关）"

    assert _map({})["CPSX"] is None, "取不到年龄段 → 档案给「无」→ 落 JSON null"
    assert _map({"target_age_group": "6 years"})["CPSX"] == "三岁以上"
    assert _map({"target_age_group": "3+", "safety_checks": "Y"})["CPSX"] == "3岁以上（要寄第三方测试）"

    # 只有**整格恰好等于**「无」才转换：长文本/别的写法不受影响
    assert _map({"fsc": "无"})["FSCMC"] is None, "整格「无」→ null（业务原话：「或者其他值的」）"
    assert _map({"fsc": "无特殊要求"})["FSCMC"] == "无特殊要求", "不是整格匹配 → 原样保留"
    assert _map({"notes": "客户无需提供刀模，具体要求见附件"})["CPMS"] == "", "CPMS 永远留空"
    # 客户常量（erp_fixed）也不受影响
    assert _map({})["Remark4"], "Remark4 是 NGV 的固定检测清单，不该被 null 转换动到"

    # 公共默认层不许跟着改：null_values 是全局口径（各客户可自行覆盖成 []）
    from customer_profile import load_profile

    assert load_profile("generic").rule("null_values") == ["无"], (
        "「无 → null」是全局口径，写在 _defaults.yaml"
    )

    # NGV 不再需要「口径例外」声明（null 不触发枚举校验）
    assert NGV.rule("enum_exceptions", []) == []


def test_决策_KHDDH默认空不算缺陷():
    """业务 2026-09-24：香港单号默认就是空 —— 扫不到不算缺陷，不报假 error。

    附件文件名里带「香港单号 CU…」时 rules.scan 扫得到就落库；
    PR-00091345 那种只有 PR 号的单子，KHDDH 空是**预期结果**。
    """
    from erp_schema import validate_result

    assert "KHDDH" in NGV.rule("required_empty")
    assert "KHDH" in NGV.rule("required_empty")

    # `--check --customer ngv` 与管道用的是同一套豁免
    kwargs = {"allow_empty": NGV.rule("required_empty")}
    result = _map({})
    issues = validate_result(result, **kwargs)
    assert [i for i in issues if i["level"] == "error"] == [], (
        "KHDH/KHDDH/Drop_SJFX 空 + CPSX=null（业务口径「无」→ null）都属正常状态，不该报 error"
    )
    assert result["CPSX"] is None, "前提：CPSX 已是 JSON null（空值），不是字符串「无」"

    # 不顺带放宽：字典里没有的**取值**仍要报错
    assert [i for i in validate_result({"CPSX": "五岁以上"}, **kwargs)
            if i["level"] == "error"], "胡乱填的取值必须继续报 error"


def test_报价截止日期_过期判据与提醒(monkeypatch):
    """业务 2026-09-24：「超过了报价截止日期，你记得提醒」。

    判据必须由**基准日期**驱动，不能写死 —— 用 --today / _TODAY 覆盖才可复现
    （否则今天没过期、明天过期了，测试自己就漂了）。
    """
    from datetime import date

    import main as main_mod
    from main import check_offer_deadline

    warnings: list = []

    class _Rec:
        def info(self, *a, **k): pass
        def warn(self, msg, *a, **k): warnings.append(str(msg))

    monkeypatch.setattr(main_mod, "_TODAY", date(2026, 9, 24))

    info = check_offer_deadline(
        CustomerData(values={"offer_deadline": "2026-06-26 00:00:00"}), NGV, _Rec())
    assert info["deadline"] == "2026-06-26" and info["expired"] is True
    assert info["days"] == 90, "逾期天数要算出来（提醒里要报）"
    assert any("已过期" in w for w in warnings), "过期必须打 WARN（不许只记 info）"

    # 截止日期就是今天 → 不算过期，但提醒语气也要对
    info2 = check_offer_deadline(
        CustomerData(values={"offer_deadline": "2026-09-24 00:00:00"}), NGV, _Rec())
    assert info2["expired"] is False and info2["days"] == 0

    # 还没到期 → 不算过期
    info3 = check_offer_deadline(
        CustomerData(values={"offer_deadline": "2026-10-01 00:00:00"}), NGV, _Rec())
    assert info3["expired"] is False and info3["days"] == -7

    # 没取到 → None（不是「假装没过期」）
    assert check_offer_deadline(CustomerData(values={}), NGV, _Rec()) is None


@needs_samples
@pytest.mark.parametrize("order,expected", [
    ("CU26051601-01", "2026-05-26"),
    ("CU26061701", "2026-06-17"),
    ("CU26072301", "2026-07-27"),
    ("CU26072302", "2026-07-27"),
])
def test_真实样本_报价截止日期必须抽出来(order, expected):
    """`offer deadline / Angebotsfrist` 那一行是**有值的**（5/5 份样本都有）。

    回归 2026-09-24 的真 bug：它曾被当成「纯标题」塞进 ignored_labels →
    值被静默丢掉（extracted.json 里根本没有 offer_deadline），
    「过期提醒」也就无从谈起。
    """
    pair = next(p for p in MANIFEST["pairs"] if p["order"] == order)
    data = extract_from_workbook(str(SAMPLES / pair["attachment"]), NGV,
                                 filename=pair["attachment"])
    assert data.get("offer_deadline"), f"{order}: 截止日期被静默丢了"
    assert str(data.get("offer_deadline")).startswith(expected)
    assert not NGV.is_ignored_label("offer deadline\nAngebotsfrist"), (
        "有值标签不许登记成「纯标题」—— 那会静默丢值"
    )


@needs_samples
def test_端到端_过期提醒要出现在产物与stdout里(tmp_path):
    """跑真 main.py：`--today` 在截止日期之后就出提醒，之前就不出。

    提醒要同时落在两处（业务 2026-09-24 要求）：stdout 末尾的醒目块 +
    validation.json 的 reminders（机器可读，供 cron/后续处理）。
    """
    pair = next(p for p in MANIFEST["pairs"] if p["order"] == "CU26051601-01")
    attachment = pair["attachment"]

    def _run_once(day: str):
        workdir = tmp_path / day
        pending = workdir / "pending"
        pending.mkdir(parents=True)
        target = pending / attachment
        shutil.copy2(SAMPLES / attachment, target)
        run = subprocess.run(
            ["uv", "run", "./scripts/main.py", str(target), "--customer", "ngv",
             "--today", day, "--processed-dir", str(workdir / "processed")],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=300,
        )
        assert run.returncode == 0, f"main.py 失败:\n{run.stdout}\n{run.stderr}"
        vfiles = list((workdir / "processed").glob("*/*.validation.json"))
        assert len(vfiles) == 1, vfiles
        return run.stdout, json.loads(vfiles[0].read_text(encoding="utf-8"))

    # ① 基准日在截止日期（2026-05-26）之后 → 必须提醒
    out, report = _run_once("2026-09-24")
    assert "提醒业务" in out and "已过期" in out, out
    assert report["reminders"], report
    assert report["reminders"][0]["kind"] == "offer_deadline_expired"
    assert report["reminders"][0]["deadline"] == "2026-05-26"
    assert report["reminders"][0]["days_over"] > 0
    assert report["summary"]["error"] == 0, report["errors"]

    # ② 基准日在截止日期之前 → 不许误报（证明判据由日期驱动，不是写死的）
    out2, report2 = _run_once("2026-05-01")
    assert "提醒业务" not in out2, out2
    assert report2["reminders"] == [], report2





def test_CPLB_冲形状胶装不许判成PAD头书():
    """真 bug（CU26070802 / 真单 BJXQ260708007）：整书冲形状的无锁线胶装 = 23.03。

    旧规则 `perfect\\s*binding|open\\s*pad|pad\\s*bind → 23.06` 三个错叠在一起：
      ① perfect binding 只是**装订方式**，不是产品形态（PAD头书）；
      ② `at the top` 的 top 被当成 PAD 的「头」（其实是装订边位置）；
      ③ 整书冲形状（shape die-cutting egg form）根本没参与判定。
    真单的 ERP 类别就是「冲形状无线胶装书」。（尺寸顺序 2026-10-21 改为按书写顺序落库，
    故这里是 148*210；类别/书脊结论不受影响。）
    """
    got = _map({
        "size": "148 x 210 mm portrait",
        "binding": ("softcover, perfect binding at the top, square back, "
                    "shape die-cutting egg form"),
        "binding_direction": "at the top",
        "cover_material": "230 gsm artcard C1S (FSC-Mix)",
        "text_material": "100 gsm offset paper (FSC-Mix)",
    })
    assert got["CPLB"] == "23.03;冲形状无线胶装书", got["CPLB"]
    assert got["ZDFS"] == "胶装/PAD书"
    assert (got["CD"], got["KD"]) == (148, 210)
    assert got["Drop_SJFX"] == "短书脊"


def test_CPLB_无线胶装但没冲形状落基础节点():
    """没冲形状、没有 PAD 线索 → 23.01 普通无线胶装书（不许猜 PAD头书）。

    PAD头书（23.05/23.06）要「封面大于内文 / 露 PAD 头 / 二次上胶装」这类结构证据，
    客户文件常常只写 perfect binding —— 脚本落基础类，交业务改（宁可粗，不可错）。
    """
    got = _map({
        "size": "148 x 210 mm portrait",
        "binding": "softcover, perfect binding, square back",
        "binding_direction": "at the top",
    })
    assert got["CPLB"] == "23.01;无线胶装书", got["CPLB"]
