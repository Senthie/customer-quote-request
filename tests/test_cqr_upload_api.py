"""上传 / 排队 的 HTTP 层（`POST /api/uploads`、`GET /api/pending`）。

用 Flask 的 test_client，不真起服务；上传目录指到 tmp，绝不碰真实 pending/。
"""
import io
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from CQR_Web import upload as U  # noqa: E402
from CQR_Web.app import create_app  # noqa: E402


@pytest.fixture
def client(tmp_path):
    processed = tmp_path / "processed"
    processed.mkdir()
    pending = tmp_path / "pending"
    pending.mkdir()
    app = create_app(processed, pending)
    app.config.update(TESTING=True)
    with app.test_client() as c:
        yield c, pending


def post_files(client, files, group="one"):
    data = {"group": group,
            "files": [(io.BytesIO(payload), name) for name, payload in files]}
    return client.post("/api/uploads", data=data, content_type="multipart/form-data")


# ── 上传成功 ──────────────────────────────────────────────────────────

def test_upload_single_file_lands_in_pending(client):
    c, pending = client
    res = post_files(c, [("报价单.xlsx", b"hello")])
    assert res.status_code == 201
    body = res.get_json()
    assert body["batch"] is None
    assert body["saved"][0]["name"] == "报价单.xlsx"
    assert (pending / "报价单.xlsx").read_bytes() == b"hello"


def test_upload_multi_as_one_quote_creates_batch_dir(client):
    c, pending = client
    res = post_files(c, [("a.xlsx", b"1"), ("b.pdf", b"2")], group="one")
    assert res.status_code == 201
    body = res.get_json()
    assert body["batch"]
    assert (pending / body["batch"] / "a.xlsx").exists()
    assert (pending / body["batch"] / "b.pdf").exists()


def test_upload_multi_as_separate_quotes_stays_flat(client):
    c, pending = client
    res = post_files(c, [("a.xlsx", b"1"), ("b.xlsx", b"2")], group="each")
    assert res.get_json()["batch"] is None
    assert (pending / "a.xlsx").exists() and (pending / "b.xlsx").exists()


def test_upload_rejects_eml_with_hint(client):
    c, _ = client
    res = post_files(c, [("mail.eml", b"raw")])
    assert res.status_code == 400
    assert "ingest_email" in res.get_json()["error"]


def test_upload_rejects_when_nothing_valid(client):
    c, pending = client
    res = post_files(c, [("virus.exe", b"1")])
    assert res.status_code == 400
    assert list(pending.iterdir()) == []


def test_upload_partial_failure_reports_both_sides(client):
    c, pending = client
    res = post_files(c, [("good.xlsx", b"1"), ("bad.exe", b"2")])
    assert res.status_code == 201
    body = res.get_json()
    assert [s["name"] for s in body["saved"]] == ["good.xlsx"]
    assert body["rejected"][0]["name"] == "bad.exe"


def test_upload_without_files_is_400(client):
    c, _ = client
    res = c.post("/api/uploads", data={"group": "one"},
                 content_type="multipart/form-data")
    assert res.status_code == 400
    assert "files" in res.get_json()["error"]


def test_upload_sanitizes_traversal_name(client):
    """`../` 打头的名字落盘后必须在 pending 里，不能写到外面去。"""
    c, pending = client
    res = post_files(c, [("../../evil.xlsx", b"x")])
    assert res.status_code == 201
    assert (pending / "evil.xlsx").exists()
    assert not (pending.parent / "evil.xlsx").exists()


def test_upload_too_large_returns_json_hint(tmp_path):
    processed, pending = tmp_path / "p", tmp_path / "q"
    processed.mkdir(); pending.mkdir()
    app = create_app(processed, pending)
    app.config.update(TESTING=True)
    app.config["MAX_CONTENT_LENGTH"] = 500          # 故意设小，免得真传 64MB
    with app.test_client() as c:
        res = c.post("/api/uploads",
                     data={"files": (io.BytesIO(b"x" * 5000), "big.xlsx")},
                     content_type="multipart/form-data")
    assert res.status_code == 413
    body = res.get_json()
    assert "上限" in body["error"] and "zip" in body["error"]


# ── 排队清单 ──────────────────────────────────────────────────────────

def test_pending_endpoint_lists_uploads(client):
    c, pending = client
    post_files(c, [("a.xlsx", b"1")])
    post_files(c, [("b.xlsx", b"1"), ("c.pdf", b"2")], group="one")

    res = c.get("/api/pending")
    assert res.status_code == 200
    body = res.get_json()
    assert body["count"] == 2
    names = {it["name"] for it in body["items"]}
    assert "a.xlsx" in names
    assert any(it["kind"] == "folder" and it["n_files"] == 2 for it in body["items"])


def test_pending_endpoint_empty(client):
    c, _ = client
    body = c.get("/api/pending").get_json()
    assert body["count"] == 0 and body["items"] == []


def test_pending_dir_is_configurable(tmp_path):
    """`--pending-dir` / 测试注入 → 两个目录互不干扰。"""
    processed = tmp_path / "processed"
    processed.mkdir()
    alt = tmp_path / "别的目录"
    app = create_app(processed, alt)
    app.config.update(TESTING=True)
    with app.test_client() as c:
        c.post("/api/uploads",
               data={"files": (io.BytesIO(b"1"), "x.xlsx")},
               content_type="multipart/form-data")
    assert (alt / "x.xlsx").exists()
