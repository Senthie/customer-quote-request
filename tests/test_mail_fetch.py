"""收信入口（`scripts/mail_fetch.py`）。

不连真邮箱：`FakeClient` 顶替 himalaya，验的是收信层自己那几条容易出事的规矩：

* 系统通知（企业微信登录提醒）**不进**流水线，但要记指纹，别每轮白扫
* 同一封信第二轮不能再落一次盘（指纹预筛 + Message-ID 兜底两条都要管用）
* 空轮次**一个字都不打印**（cron 每 5 分钟跑，不能骚扰）
* `--dry-run` 不写文件、不写台账
* `--mark-baseline` 之后历史邮件不入队，新信照样能进
"""
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import mail_fetch as M  # noqa: E402

CFG = {
    "account": "sora",
    "folder": "INBOX",
    "page_size": 50,
    "max_per_run": 5,
    "skip_senders": ["10000@exmail.weixin.qq.com", "mailer-daemon@"],
    "skip_subjects": ["企业微信邮箱登录提醒"],
}


def env(id, subject, addr="kevinlai@ghouseproducts.com", date="2026-11-04 15:34+08:00",
        name="kevinlai"):
    return M.Envelope(id=str(id), subject=subject, sender_addr=addr,
                      sender_name=name, date=date)


class FakeClient:
    """只实现收信层用到的那两个方法。"""

    def __init__(self, envelopes, msgids=None, fail_on=()):
        self.envelopes = envelopes
        self.msgids = msgids or {}
        self.fail_on = set(fail_on)
        self.exported = []

    def list_envelopes(self, page_size=50):
        return list(self.envelopes)

    def export_full(self, envelope, dest):
        if envelope.id in self.fail_on:
            raise M.HimalayaError("boom")
        self.exported.append(envelope.id)
        msgid = self.msgids.get(envelope.id, f"<{envelope.id}@test>")
        body = (f"Message-ID: {msgid}\r\n"
                f"Subject: {envelope.subject}\r\n"
                f"From: {envelope.sender}\r\n"
                f"Content-Type: text/plain; charset=utf-8\r\n\r\n"
                f"hello from {envelope.id}\r\n")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(body, encoding="utf-8")
        return dest


@pytest.fixture
def dirs(tmp_path):
    pending = tmp_path / "pending"
    pending.mkdir()
    state = tmp_path / ".mail_fetch_state.json"
    return pending, state


def run(client, dirs, cfg=None, **kw):
    pending, state = dirs
    return M.fetch(client, pending, state, cfg or CFG, **kw)


# ── 过滤 ──────────────────────────────────────────────────────────────

def test_系统通知不进流水线但记指纹(dirs):
    pending, state = dirs
    client = FakeClient([
        env(169, "企业微信邮箱登录提醒", addr="10000@exmail.weixin.qq.com", name="邮件助手"),
        env(168, "NGV-报价搜索5个"),
    ])
    result = run(client, dirs, logger=lambda _m: None)

    assert [i["id"] for i in result["ingested"]] == ["168"]
    assert [p.name for p in pending.iterdir()] == ["20261104_NGV-报价搜索5个.eml"]
    assert "169" not in client.exported          # 被过滤的信连下载都不该发生
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert any("10000@exmail.weixin.qq.com" in fp for fp in saved["fingerprints"])


def test_主题命中跳过名单也不进(dirs):
    pending, _ = dirs
    client = FakeClient([env(1, "Re: 自动回复 / Out of Office",
                             addr="someone@customer.com")])
    cfg = dict(CFG, skip_subjects=["Out of Office"])
    result = run(client, dirs, cfg=cfg, logger=lambda _m: None)
    assert result["ingested"] == []
    assert client.exported == []


def test_腾讯企业邮转发通知不进流水线():
    """2026-11-17 真实事故：`marketing@wafai.net` 的「已设置将邮件转发至您的邮箱」
    是腾讯企业邮的**系统通知**，正文只有一句通知 + 腾讯页面链接，无附件、
    无任何产品字段 → 抽出来是 `profile=generic` / `values={}` 的空单
    （`831ab1c2…`，`CPMC=32.00;其他`、`CPSL1..10=0`）。

    用**真实** `assets/mail.yaml` 校验（不是测试里另造的 CFG）—— 配置改坏了这里会红。
    同时钉住边界：**同一个发件人**真发来的询价不能被顺手拦掉。
    """
    cfg = M.load_config(M.CONFIG_PATH)
    notice = env(900, "Mark Lin已设置将邮件转发至您的邮箱",
                 addr="marketing@wafai.net", name="Mark Lin")
    assert "转发" in M.skip_reason(notice, cfg)

    enquiry = env(901, "Preisanfrage Malbücher 2027",
                  addr="marketing@wafai.net", name="Mark Lin")
    assert M.skip_reason(enquiry, cfg) == ""


# ── 去重 ──────────────────────────────────────────────────────────────

def test_第二轮不再重复落盘(dirs):
    pending, state = dirs
    envelopes = [env(168, "NGV-报价搜索5个")]
    first = run(FakeClient(envelopes), dirs, logger=lambda _m: None)
    assert len(first["ingested"]) == 1

    second_client = FakeClient(envelopes)
    second = run(second_client, dirs, logger=lambda _m: None)
    assert second["ingested"] == []
    assert second_client.exported == []          # 指纹命中，连下载都省了
    assert len(list(pending.iterdir())) == 1
    assert state.exists()


def test_指纹变了但_Message_ID_相同也不重复(dirs):
    """邮编箱那边改了标题/日期（转发、重新投递）时，靠 Message-ID 兜底。"""
    pending, _ = dirs
    run(FakeClient([env(168, "NGV-报价搜索5个")],
                   msgids={"168": "<same@x>"}), dirs, logger=lambda _m: None)

    # 同一封信，换个信封 id / 日期 / 标题前缀
    moved = [env(999, "Fwd: NGV-报价搜索5个", date="2026-11-05 09:00+08:00")]
    result = run(FakeClient(moved, msgids={"999": "<same@x>"}),
                 dirs, logger=lambda _m: None)

    assert result["ingested"] == []
    assert [p.name for p in pending.iterdir()] == ["20261104_NGV-报价搜索5个.eml"], \
        "重复的那封必须被删掉，不能留在 pending 里等着被抽两次"


# ── 限额 / 干跑 ────────────────────────────────────────────────────────

def test_单轮最多落盘上限(dirs):
    envelopes = [env(i, f"询价 {i}") for i in range(1, 8)]
    result = run(FakeClient(envelopes), dirs, cfg=dict(CFG, max_per_run=3),
                 logger=lambda _m: None)
    assert len(result["ingested"]) == 3
    assert any("上限" in s["reason"] for s in result["skipped"])


def test_dry_run_不写文件不写台账(dirs):
    pending, state = dirs
    client = FakeClient([env(168, "NGV-报价搜索5个")])
    result = run(client, dirs, dry_run=True, logger=lambda _m: None)
    assert [i["id"] for i in result["ingested"]] == ["168"]
    assert client.exported == []
    assert list(pending.iterdir()) == []
    assert not state.exists()


def test_没有新信时结果为空(dirs):
    """cron 靠「结果为空」保持安静，这条别退化。"""
    result = run(FakeClient([]), dirs, logger=lambda _m: None)
    assert result == {"ingested": [], "skipped": [], "errors": [], "eml": []}


def test_单封失败不影响其余(dirs):
    envelopes = [env(1, "询价 A"), env(2, "询价 B")]
    result = run(FakeClient(envelopes, fail_on={"1"}), dirs, logger=lambda _m: None)
    assert [i["id"] for i in result["ingested"]] == ["2"]
    assert [e["id"] for e in result["errors"]] == ["1"]


# ── 基线 ──────────────────────────────────────────────────────────────

def test_基线之后历史邮件不入队新信照进(dirs):
    pending, state = dirs
    old = [env(165, "Re:客户询价Ai录入进展报告"), env(167, "NGV 客户 - 询价资料")]

    baseline_client = FakeClient(old)
    assert M.mark_baseline(baseline_client, state, CFG, logger=lambda _m: None) == 2
    assert baseline_client.exported == []
    assert list(pending.iterdir()) == []

    # 基线之后：老信还在邮箱里，但不该再被拉
    after = run(FakeClient(old), dirs, logger=lambda _m: None)
    assert after["ingested"] == []

    # 新来的信照常进
    fresh = run(FakeClient(old + [env(170, "NGV-新询价")]), dirs, logger=lambda _m: None)
    assert [i["id"] for i in fresh["ingested"]] == ["170"]


# ── 命名 / 配置 ───────────────────────────────────────────────────────

def test_文件名清掉非法字符并截断(dirs):
    pending, _ = dirs
    long_subject = 'NGV/报价: "Lidl" <测试>*?' + "长" * 80
    result = run(FakeClient([env(1, long_subject)]), dirs, logger=lambda _m: None)
    name = Path(result["eml"][0]).name
    assert not any(c in name for c in '\\/:*?"<>|')
    assert len(name) <= len("20261104_") + 60 + len(".eml")
    assert name.startswith("20261104_")


def test_同名附件自动加序号(dirs):
    pending, _ = dirs
    assert M.unique_path(pending, "a.eml").name == "a.eml"
    (pending / "a.eml").write_text("x", encoding="utf-8")
    assert M.unique_path(pending, "a.eml").name == "a_2.eml"


def test_配置缺文件时用默认值(tmp_path):
    cfg = M.load_config(tmp_path / "nope.yaml")
    assert cfg["folder"] == "INBOX"
    assert cfg["max_per_run"] == 5
    assert "10000@exmail.weixin.qq.com" in cfg["skip_senders"]


def test_配置文件覆盖默认键(tmp_path):
    path = tmp_path / "mail.yaml"
    path.write_text("folder: Archive\nmax_per_run: 2\n", encoding="utf-8")
    cfg = M.load_config(path)
    assert cfg["folder"] == "Archive"
    assert cfg["max_per_run"] == 2
    assert cfg["page_size"] == 50              # 没写的键保留默认


# ── himalaya 客户端 ───────────────────────────────────────────────────

def test_himalaya_找不到二进制时报错():
    client = M.HimalayaClient(binary="/nonexistent/himalaya")
    with pytest.raises(M.HimalayaError, match="找不到 himalaya"):
        client.list_envelopes()


def test_account_参数放在子命令后面():
    """himalaya 1.2 的 --account 是子命令级选项，放全局位置会直接报错。"""
    client = M.HimalayaClient(binary="himalaya", account="sora")
    cmd = client._cmd(["envelope", "list", "--folder", "INBOX"])
    assert cmd[1:3] == ["envelope", "list"], "子命令必须在最前面（第 0 位是可执行文件）"
    assert cmd.index("--account") > cmd.index("list"), "account 不能出现在子命令之前"
    assert cmd[cmd.index("--account") + 1] == "sora"


def test_不配账户时不带_account_参数():
    client = M.HimalayaClient(binary="himalaya", account="")
    assert "--account" not in client._cmd(["envelope", "list"])


# ── 与扫描器的接线 ────────────────────────────────────────────────────

def test_定时脚本先收信再扫描():
    script = (SCRIPTS / "scan_pending.sh").read_text(encoding="utf-8")
    assert "mail_fetch.py" in script, "cron 入口必须接上收信，否则要人手动拉"
    assert script.index("mail_fetch.py") < script.index("scan_pending.py"), \
        "必须**先**收信再扫描，否则新邮件要等下一个 5 分钟周期"
    assert "2>>/tmp/cqr_scan.stderr" in script, "收信失败不能污染要发给人看的那段话"
