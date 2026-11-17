"""2026-11-17 引擎侧五项修复的回归。

背景：11-17 凌晨那批 10 封真实邮件的产出全是 `profile=generic`、`CD/KD/GD=0/0/0`、
`TZS=1`、`YWY` 空、`CPSL` 全 0，cpms_todo 每轮催 5 条。逐条定位出**引擎**的五个缺陷
（不是文案问题，agent 侧写 CPMS 捂不住）：

1. **收信不看方向**：本公司外发的回信（`Re: PO16528 …` / `Re: AW: Lidl …`）被当成询价
   收进来 → 抽出空单。10 封里 4 封是外发。（`mail_fetch.outbound_reason`）
2. **转发件识别不出客户**：同事 `Fw:` 转发的询价，`.txt` 抬头没有 `From:`，
   真正的客户域名只在正文引文里 → 旧实现只比对 sender，一律掉 generic。
   （`customer_profile.detect_score` 新增「正文含客户域名」20 分）
3. **尺寸丢在 `Format:` / `TPS:` 行里**：`size` 只认 Size/尺寸 标签 → CD/KD 落 0。
   （`main.size_from_text` 兜底 + `_defaults.fields.size` 补 TPS 等别名）
4. **`YWY` 只在 ngv.yaml 声明**：它是客户无关的生成方标记（必填 `KM`），
   未建档客户/igl 的产物 YWY 全空 → 落进公共 `_defaults.yaml` 的 erp_fixed。
5. **审计把客户料号拆成数字**：`37SI` / `38BA` 被 `TOKEN_RE` 回溯成 `3` →
   误报「无出处」。（`cpms_content_audit` 的 TOKEN_RE / CODE_DIGIT_RE）
"""
from __future__ import annotations

import sys
from email.message import EmailMessage
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import cpms_content_audit as A  # noqa: E402
import customer_profile as C  # noqa: E402
import mail_fetch as M  # noqa: E402
import main as MAIN  # noqa: E402


# ── 1. 收信方向：外发邮件不进流水线（内部转发必须保留）───────────────────

def _write_eml(path: Path, frm: str, to: str, cc: str = "", subject: str = "x",
               body: str = "hello") -> Path:
    msg = EmailMessage()
    msg["From"] = frm
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    msg["Message-ID"] = f"<{abs(hash((frm, to, subject)))}@test>"
    msg.set_content(body)
    path.write_bytes(bytes(msg))
    return path


CFG = {"self_domains": ["ghouseproducts.com", "wafai.net"]}


def test_外发回信被识别出来(tmp_path):
    """我方 → 客户（To 在外部）= 外发，不是询价。"""
    p = _write_eml(tmp_path / "out.eml", "Demo Chen <demochen@ghouseproducts.com>",
                   "eckenberger@vemag-medien.de", subject="Re: PO16528 Bodendisplay")
    reason = M.outbound_reason(p, CFG)
    assert reason, "外发邮件必须被识别"
    assert "vemag-medien.de" in reason


def test_抄送里有外部收件人也算外发(tmp_path):
    p = _write_eml(tmp_path / "out2.eml", "doriali@ghouseproducts.com",
                   "marklin@ghouseproducts.com", cc="rusek@vemag-medien.de")
    assert M.outbound_reason(p, CFG)


def test_内部转发必须保留(tmp_path):
    """同事把客户询价转给业务 —— 发件人也是我方域名，但收件人全在自家域内，
    这是**真询价**，不许砍（否则半个流水线停摆）。"""
    p = _write_eml(tmp_path / "fw.eml", "Tinrry <tinrry@ghouseproducts.com>",
                   "Mark Lin <marklin@ghouseproducts.com>", subject="Fw: WG: Anfrage")
    assert M.outbound_reason(p, CFG) == ""


def test_跨自家域名转发也算内部(tmp_path):
    p = _write_eml(tmp_path / "fw2.eml", "Tony Ren <tonyren@ghouseproducts.com>",
                   "marklin <marklin@wafai.net>", subject="Fw: STF Flashcards")
    assert M.outbound_reason(p, CFG) == ""


def test_客户来信永远放行(tmp_path):
    p = _write_eml(tmp_path / "in.eml", "Kylie <kylie@priddybooks.com>",
                   "marklin@ghouseproducts.com", subject="New estimate request")
    assert M.outbound_reason(p, CFG) == ""


def test_未配置self_domains时保持旧行为(tmp_path):
    p = _write_eml(tmp_path / "out3.eml", "a@ghouseproducts.com", "x@customer.com")
    assert M.outbound_reason(p, {}) == ""
    assert M.outbound_reason(p, {"self_domains": []}) == ""


def test_子域名算自家域名(tmp_path):
    p = _write_eml(tmp_path / "sub.eml", "a@mail.ghouseproducts.com",
                   "b@ghouseproducts.com")
    assert M.outbound_reason(p, CFG) == ""


# ── 2. 转发件：正文里的客户域名参与识别 ──────────────────────────────

def test_正文域名能认出转发件的客户():
    ngv = C.load_profile("ngv")
    body = ["Von: Eckenberger, Katrin <eckenberger@vemag-medien.de>\nBetreff: Bodendisplay"]
    score, reasons = ngv.detect_score(body, filename="Fw_Anfrage",
                                      sender="Tinrry <tinrry@ghouseproducts.com>",
                                      subject="Fw: Anfrage")
    assert score >= 20
    assert any("正文含客户域名" in r for r in reasons), reasons


def test_发件人域名分值高于正文引用():
    """正文引用只是旁证：发件人 30 分 > 正文 20 分，别抢了别家 100 分的标签命中。"""
    ngv = C.load_profile("ngv")
    as_sender, _ = ngv.detect_score([], filename="", sender="a@vemag-medien.de")
    as_body, _ = ngv.detect_score(["a@vemag-medien.de"], filename="", sender="")
    assert as_sender == 30
    assert as_body == 20


# ── 3. 尺寸：`Format:` / `TPS:` 兜底 + 别名 ────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("140x198mm", "140 x 198 mm"),
    ("300mm x 220mm - trimmed. 300mm(h) x 220mm(w)", "300 x 220 mm"),
    ("215(w) x 280(h) - portrait", "215 x 280 mm"),
    ("20 x 25,3 cm", "200 x 253 mm"),
])
def test_从自由文本里捞尺寸(text, expected):
    assert MAIN.size_from_text(text) == expected


@pytest.mark.parametrize("text", ["", None, "PB", "Hardback", "Portrait", "4 x 17.500"])
def test_不含尺寸的写法不许触发(text):
    assert MAIN.size_from_text(text) == ""


def test_默认档案认得TPS标签():
    generic = C.load_profile("generic")
    aliases = [a.lower() for a in generic.fields.get("size", [])]
    assert "tps" in aliases
    assert "trim size" in aliases


# ── 4. YWY 是客户无关的生成方标记 ──────────────────────────────────────

def test_公共档案带YWY_KM():
    generic = C.load_profile("generic")
    assert generic.erp_fixed.get("YWY") == "KM"


def test_各客户档案解析后都有YWY_KM():
    for cid in C.list_profiles():
        profile = C.load_profile(cid)
        assert profile.erp_fixed.get("YWY") == "KM", f"{cid} 没有 YWY=KM"


# ── 5. 审计：客户料号不许被拆成数字 ────────────────────────────────────

def test_数字开头的料号不再被回溯成单个数字():
    assert A.tokens(A.normalize("37SI")) == []
    assert A.codes("37SI") == ["37SI"]
    assert A.codes("38BA") == ["38BA"]


def test_单位后缀不算料号():
    assert A.codes("140gsm 300mm 16sheets 64pp") == []


def test_客户原码不再误报无出处():
    src = "Artikel 37SI und 38BA, 140 gsm Offset, 300 mm"
    cpms = "内文用纸：140g书写纸\n配件：37SI 与 38BA 各 1 个"
    r = A.audit(cpms, src)
    assert r["untraced_codes"] == []
    assert not any(t["num"] == "3" for t in r["untraced"]), r["untraced"]


def test_字母开头的料号仍然认():
    assert A.codes("PR-00091345") == ["PR-00091345"]
    assert A.codes("CU26082501") == ["CU26082501"]


# ── 6. 装订占位值必须在 CPMS 里标明（2026-11-17 森西）────────────────────
def _placeholder_record() -> dict:
    return {
        "question_answers": [{
            "id": "Q_ZDFS", "field": "ZDFS",
            "question": ("装订方式无法判断（客户原文未给出可识别的装订方式）→ "
                         "已按 ERP 第一个合法值「胶装/PAD书」落库，需业务自行判断"),
            "answer": "", "status": "open", "blocking": False, "source": "pipeline",
            "asked_at": "2026-11-17", "answered_at": None,
        }],
        "CPLB": "23.01;无线胶装书", "CD": 140, "KD": 198, "GD": 0,
        "TZS": 4, "CPSL1": 4000, "DWMC": "本", "CPMS": "",
    }


def test_装订占位值未在CPMS标明会被抓():
    import cpms_lint as L

    spec = L.load_spec()
    cpms = "产品规格：【140*198*0】\n装订：锁线胶装\n"
    codes = {f.code for f in L.lint_text(cpms, spec, _placeholder_record())}
    assert "CPMS081" in codes


def test_装订占位值已标明就不报():
    import cpms_lint as L

    spec = L.load_spec()
    cpms = ("产品规格：【140*198*0】\n装订：锁线胶装\n"
            "备注：装订方式无法判断值，需业务自行判断\n")
    codes = {f.code for f in L.lint_text(cpms, spec, _placeholder_record())}
    assert "CPMS081" not in codes


def test_没有装订占位项时不报CPMS081():
    import cpms_lint as L

    spec = L.load_spec()
    record = _placeholder_record()
    record["question_answers"] = []          # 装订有证据 / 没这项
    cpms = "产品规格：【140*198*0】\n装订：锁线胶装\n"
    codes = {f.code for f in L.lint_text(cpms, spec, record)}
    assert "CPMS081" not in codes
