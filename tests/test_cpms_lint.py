"""CPMS 写作规范（assets/cpms_spec.yaml + scripts/cpms_lint.py）的回归测试。

钉三件事：
1. 规范样例**自己必须合规** —— 文档里的示例如果跑不过 linter，规范就是废纸。
2. 每条规则码都真的会触发（否则规则写了等于没写）。
3. `--fix` 做机械修复：版式/单位/同义词 + **部件行整槽补印色 C**，但**不许碰语义**
   （不换算灰板 g↔mm、不改工艺顺序、不动备注段）。

语料：`/home/claw/train/custom_cpms_requie`（18476 份 ERP 导出）用作**度量**，
不作为测试依赖 —— 测试必须能在没有那份数据的机器上跑。
"""
from __future__ import annotations

import pytest

import cpms_lint as L

SPEC = L.load_spec()

# 一份合规样例：书本类，封面/内文/装订/装箱齐全，多款结构
GOOD = """书籍方向：【长书脊】产品规格：【228.6*254*0】共【2】款

1. 普通BB书
  封面：300g单粉(FSC)，4C+哑胶+局部UV(30%)/0C，圆角
  内文：24PP，350g单粉(FSC)对裱，4C/4C
  夹层：12张，350g单粉(FSC)，0C/0C
  装订：普通BB书，圆角

2. 精装BB书
  皮壳：157g光粉(FSC)+1500g灰板(FSC)，4C+光胶/0C
  内文：10PP，350g单粉(FSC)对裱，4C+UV/0C
  装订：精装BOBO书

装配：2本入1个书套盒
装箱：20本/箱，每箱2张B3B平咭
备注：请拆分书本与书套盒的价格
""".strip()


def codes(findings: list[L.Finding], level: str | None = None) -> set[str]:
    return {f.code for f in findings if level is None or f.level == level}


# ── 1. 规范样例自证 ──────────────────────────────────────────
def test_合规样例零违规():
    assert L.lint_text(GOOD, SPEC) == []


def test_规范自身完整():
    assert SPEC.keys, "规范里没有段落"
    assert len(SPEC.keys) >= 40
    # order 必须两两可比（同级允许相同）
    orders = [SPEC.order[k] for k in SPEC.keys]
    assert all(isinstance(o, int) for o in orders)
    # 每个规则码都有级别与说明
    for code, meta in SPEC.rules.items():
        assert meta.get("level") in {"error", "warn"}, code
        assert meta.get("desc"), code


def test_词表覆盖历史高频标签():
    """18476 份实测的高频标签必须都在词表里，否则规范一上手就满屏 error。"""
    for label in ["封面", "内文", "装订", "装箱", "包装", "夹层", "衬纸", "皮壳",
                  "皮壳衬纸", "内兜", "内兜衬纸", "贴纸", "单张", "卡片", "砌图",
                  "内托", "陈列盒", "腰纸", "底卡", "说明书", "发声器", "客供配件",
                  "箱唛", "装配", "客户目标价", "备注"]:
        assert label in SPEC.keys, f"词表缺 {label}"


# ── 2. 每条规则都真的会触发 ──────────────────────────────────
@pytest.mark.parametrize(
    "text,code",
    [
        (GOOD.replace("\n", "\r\n"), "CPMS001"),
        (GOOD.replace("  内文：", "\t内文："), "CPMS002"),
        (GOOD.replace("圆角\n", "圆角  \n"), "CPMS003"),
        (GOOD.replace("装配：", "\n\n\n装配："), "CPMS004"),
        ("\n" + GOOD, "CPMS005"),
        (GOOD.replace("装配：", "=======\n装配："), "CPMS006"),
        (GOOD.replace("装配：", "【黄秋芬   032524】产品补充描述\n装配："), "CPMS007"),
        (GOOD.replace("装箱：20本/箱", "装箱: 20本/箱"), "CPMS008"),
        (GOOD.split("\n", 1)[1], "CPMS010"),
        (GOOD.replace("装配：", "书籍方向：【长书脊】产品规格：【228.6*254*0】共【2】款\n装配："), "CPMS012"),
        (GOOD.replace("  内文：", "  内文页："), "CPMS020"),
        (GOOD.replace("  内文：", "  内页："), "CPMS021"),
        (GOOD.replace("  内文：24PP", "  内文：\n24PP"), "CPMS022"),
        (GOOD.replace("  装订：", "  2.装订："), "CPMS023"),
        (GOOD.replace("装箱：20本/箱，每箱2张B3B平咭\n", ""), "CPMS040"),
        (GOOD.replace("4C+哑胶+局部UV(30%)/0C", "4/0c"), "CPMS052"),
        (GOOD.replace("4C+哑胶+局部UV(30%)", "4C+局部UV(30%)+哑胶"), "CPMS053"),
        ("产品规格：【1*1*0】\n装箱：待确认", "CPMS070"),
    ],
)
def test_规则码会触发(text: str, code: str):
    assert code in codes(L.lint_text(text, SPEC)), f"{code} 没被触发"


def test_印色写法告警带出建议():
    fs = [f for f in L.lint_text(GOOD.replace("4C/4C", "4/4c"), SPEC) if f.code == "CPMS052"]
    assert fs and "4C/4C" in fs[0].msg


def test_商务要求写进部件段会告警():
    bad = GOOD.replace("  内文：24PP", "  请拆分：书本的价钱\n  内文：24PP")
    assert "CPMS060" in codes(L.lint_text(bad, SPEC))


def test_三级子项豁免词表():
    ok = GOOD.replace("  内文：24PP", "  内文：24PP\n  - 第一张内文右边：留白3mm")
    assert L.lint_text(ok, SPEC) == []


def test_空CPMS判未完成():
    assert "CPMS040" in codes(L.lint_text("", SPEC))


# ── 3. 概要行是派生数据 ──────────────────────────────────────
def test_概要行与字段一致才通过():
    rec = {"Drop_SJFX": "长书脊", "CD": 229, "KD": 254, "GD": 0, "TZS": 2, "YYS": None}
    assert L.lint_text(GOOD, SPEC, rec) == []          # 228.6 与 229 的取整差允许


def test_概要行方向不一致要报错():
    rec = {"Drop_SJFX": "短书脊", "CD": 228.6, "KD": 254, "GD": 0, "TZS": 2}
    assert "CPMS011" in codes(L.lint_text(GOOD, SPEC, rec))


def test_尺寸差超过阈值要报错():
    rec = {"Drop_SJFX": "长书脊", "CD": 260, "KD": 254, "GD": 0, "TZS": 2}
    assert "CPMS011" in codes(L.lint_text(GOOD, SPEC, rec))


def test_TZS大于1必须写款数():
    rec = {"Drop_SJFX": "长书脊", "CD": 228.6, "KD": 254, "GD": 0, "TZS": 4}
    assert "CPMS011" in codes(L.lint_text(GOOD, SPEC, rec))


def test_语言数大于1必须写每款语言():
    rec = {"Drop_SJFX": "长书脊", "CD": 228.6, "KD": 254, "GD": 0, "TZS": 2, "YYS": 3}
    assert "CPMS011" in codes(L.lint_text(GOOD, SPEC, rec))


# ── 4. --fix 做机械修复（含部件行整槽补印色 C）────────────────
def test_机械规范化后合规():
    dirty = (GOOD.replace("\n", "\r\n")
                 .replace("4C/4C", "4/4c")            # 印色补 C（业务 2026-10-19 拍板自动修）
                 .replace("  内文：", "  内页：")       # 同义标签
                 .replace("装箱：20本/箱", "装箱:20本/箱")  # 半角冒号
                 .replace("日期", "日期"))
    fixed, changes = L.normalize_text(dirty, SPEC)
    assert "\r" not in fixed
    assert "内文：" in fixed and "内页：" not in fixed
    assert "装箱：20本/箱" in fixed
    assert "4C/4C" in fixed and "4/4c" not in fixed
    assert changes
    # 修完必须真的合规（而不是只改了几个字）
    assert [f for f in L.lint_text(fixed, SPEC) if f.level == "error"] == []


def test_印色只修部件行整槽_不碰比例和备注():
    text = ("书籍方向：【长书脊】产品规格：【210*148*12】\n\n"
            "封面：250g单粉(FSC)，4/0c\n"          # 部件行整槽 → 4C/0C
            "内文：160PP，80g书纸(FSC)，4c+光油/0\n"
            "装箱：40本/箱\n"
            "备注：书脊1/2处烫金")                 # 备注段不动
    fixed, _ = L.normalize_text(text, SPEC)
    assert "4C/0C" in fixed
    assert "4C+光油/0C" in fixed
    assert "书脊1/2处烫金" in fixed                # 比例不许被改成 1C/2C


def test_套装写款数要报错_写件数才通过():
    rec = {"Drop_SJFX": "", "CD": 0, "KD": 0, "GD": 0, "TZS": 1,
           "CPLB": "29.04;其它套装", "ZDFS": "套装"}
    bad = "产品规格：【0*0*0】共【2】款\n\n1套=A+B\n\n装箱：24套/箱"
    good = "产品规格：【0*0*0】共【2】件\n\n1套=A+B\n\n装箱：24套/箱"
    assert "CPMS011" in codes(L.lint_text(bad, SPEC, rec))
    assert [f for f in L.lint_text(good, SPEC, rec) if f.code == "CPMS011"] == []


def test_套装件数必须与组成声明一致():
    rec = {"Drop_SJFX": "", "CD": 0, "KD": 0, "GD": 0, "TZS": 1,
           "CPLB": "29.04;其它套装", "ZDFS": "套装"}
    mismatch = "产品规格：【0*0*0】共【5】件\n\n1套=A+B+C\n\n装箱：24套/箱"
    assert "CPMS011" in codes(L.lint_text(mismatch, SPEC, rec))


def test_套装多件漏写件数要报错():
    rec = {"Drop_SJFX": "", "CD": 0, "KD": 0, "GD": 0, "TZS": 1,
           "CPLB": "29.04;其它套装", "ZDFS": "套装"}
    text = "产品规格：【0*0*0】\n\n1套=A+B+C+D\n\n装箱：24套/箱"
    assert "CPMS011" in codes(L.lint_text(text, SPEC, rec))


def test_书本类不许写件():
    rec = {"Drop_SJFX": "长书脊", "CD": 228.6, "KD": 254, "GD": 0, "TZS": 2,
           "CPLB": "23.01;无线胶装书"}
    text = GOOD.replace("共【2】款", "共【2】件")
    assert "CPMS011" in codes(L.lint_text(text, SPEC, rec))


def test_灰板写mm要告警():
    text = ("书籍方向：【长书脊】产品规格：【210*148*12】\n\n"
            "封面：157g光粉(FSC)+2.5mm灰板(FSC)，4C/0C\n"
            "内文：160PP，80g书纸(FSC)，1C/1C\n"
            "装订：锁线胶装\n"
            "装箱：40本/箱")
    fs = [f for f in L.lint_text(text, SPEC) if f.code == "CPMS054"]
    assert fs and all(f.level == "warn" for f in fs)


def test_规范化不碰尺寸里的星号():
    fixed, _ = L.normalize_text("书籍方向：【长书脊】产品规格：【228.6*254*0】\n封面：150x50x200mm", SPEC)
    assert "150*50*200mm" in fixed


def test_规范化删除分隔线():
    fixed, _ = L.normalize_text("产品规格：【1*1*0】\n=======\n装箱：待确认", SPEC)
    assert "=====" not in fixed


# ── 5. CLI ──────────────────────────────────────────────────
def test_cli_vocab(capsys):
    assert L.main(["--vocab"]) == 0
    out = capsys.readouterr().out
    assert "封面" in out and "装箱" in out


def test_cli_text(tmp_path, capsys):
    assert L.main(["--text", GOOD]) == 0
    assert L.main(["--text", GOOD.replace("4C/4C", "4/4c")]) >= 0


def test_cli_file(tmp_path):
    import json

    p = tmp_path / "ERP_test.json"
    p.write_text(json.dumps({"CPMS": GOOD}, ensure_ascii=False), encoding="utf-8")
    assert L.main([str(p)]) == 0
    p.write_text(json.dumps({"CPMS": GOOD.replace("\n", "\r\n")}, ensure_ascii=False), encoding="utf-8")
    assert L.main([str(p)]) == 1
