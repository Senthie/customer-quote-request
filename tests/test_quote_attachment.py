"""quote_attachment.py 的离线测试（不联网：FakeClient 顶替 ERP）。"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import quote_attachment as qa  # noqa: E402


def _make_jpeg(path: Path, size=(2400, 3000), color=(200, 40, 40)) -> None:
    from PIL import Image
    Image.new("RGB", size, color).save(path, "JPEG", quality=95)


class FakeClient:
    """记录 tools/call 的假客户端；existing 决定 list 时返回哪些附件。

    支持分片协议：{upload_key, chunk_index, total_chunks, chunk_base64}，
    chunk_base64 为空 = 探针（只回已收分片）。
    """

    def __init__(self, existing: list[str] | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.existing = existing or []
        self.fail_upload = False
        self.uploads: dict[str, dict] = {}      # upload_key → {"total", "parts", "name"}
        self.blobs: dict[str, bytes] = {}       # 附件名 → 内容（供 download 回读）

    def _chunk_state(self, key: str) -> dict:
        u = self.uploads.get(key) or {"total": 0, "parts": {}}
        recv = sorted(u["parts"])
        total = u["total"]
        return {"ok": False, "status": "chunk_pending", "complete": False,
                "chunks": [{"upload_key": key, "total": total, "received": recv,
                            "missing": [i for i in range(total) if i not in u["parts"]],
                            "complete": total > 0 and len(recv) == total}]}

    def call_tool(self, name: str, args: dict) -> dict:
        self.calls.append((name, args))
        if name != "erp_attachment":
            return {}
        if args.get("delete"):
            self.existing = [n for n in self.existing if n != args["delete"]]
            return {"ok": True, "status": "deleted", "deleted": {"名称": args["delete"]}}
        if args.get("download"):
            nm = args["download"]
            import base64 as _b64
            if nm not in self.blobs:
                return {"ok": False, "raw": "no such attachment"}
            return {"名称": nm, "size": len(self.blobs[nm]),
                    "content_base64": _b64.b64encode(self.blobs[nm]).decode()}
        files = args.get("files")
        if files:
            f = files[0]
            key = f.get("upload_key")
            if key is None:      # 旧式单次上传（兼容）
                if self.fail_upload:
                    return {"ok": False, "errors": ["boom"], "raw": "HTTP 413"}
                names = [x.get("name") or x.get("filename") for x in files]
                self.existing.extend(n for n in names if n)
                return {"ok": True, "verified": True, "count": len(self.existing),
                        "uploaded": files, "missing": [], "errors": [],
                        "files": [{"名称": n} for n in self.existing]}
            import base64 as _b64
            part = _b64.b64decode(f.get("chunk_base64") or "")
            if not part:          # 探针
                return self._chunk_state(key)
            u = self.uploads.setdefault(key, {"total": f.get("total_chunks") or 1,
                                              "parts": {}, "name": f.get("name")})
            u["parts"][f.get("chunk_index") or 0] = part
            if len(u["parts"]) < u["total"]:
                return self._chunk_state(key)
            if self.fail_upload:
                return {"ok": False, "verified": False, "errors": ["boom"], "raw": "HTTP 413"}
            blob = b"".join(u["parts"][i] for i in range(u["total"]))
            nm = u["name"] or f.get("filename")
            self.blobs[nm] = blob
            self.existing.append(nm)
            return {"ok": True, "verified": True, "size_ok": True, "count": len(self.existing),
                    "missing": [], "errors": [], "files": [{"名称": n} for n in self.existing]}
        return {"count": len(self.existing), "files": [{"名称": n} for n in self.existing]}


@pytest.fixture()
def unit(tmp_path: Path) -> Path:
    d = tmp_path / "pending" / "5"
    d.mkdir(parents=True)
    _make_jpeg(d / "IMG_1.jpeg")
    _make_jpeg(d / "IMG_2.jpeg", color=(40, 120, 200))
    (d / "body.txt").write_text("询价正文", encoding="utf-8")
    (d / "IMG_1.jpeg.vlm.json").write_text('{"doc_type":"cover_art"}', encoding="utf-8")
    return d


def test_collect_skips_internal_and_zip(unit: Path):
    (unit / "_attachment").mkdir()
    (unit / "old.zip").write_bytes(b"PK\x03\x04")
    names = [p.name for p in qa.collect_files(unit)]
    assert "body.txt" in names and "IMG_1.jpeg" in names
    assert "old.zip" not in names  # 不把自己打过包再塞回去


def test_build_zip_shrinks_images_and_counts(unit: Path):
    zp, stats = qa.build_zip(unit, unit.parent / "_attachment", "BJXQ_TEST")
    assert zp.is_file() and zp.stat().st_size == stats["size"]
    assert stats["files"] == 4 and stats["images"] == 2 and stats["others"] == 2
    assert stats["sha256"] and stats["base64_size"] > stats["size"]
    with zp.open("rb") as fh:
        assert fh.read(2) == b"PK"
    # 原图 2400x3000 压到长边 ≤1600 之后必须明显变小
    import zipfile
    with zipfile.ZipFile(zp) as z:
        assert z.read("5/body.txt").decode() == "询价正文"
        assert all(i.file_size < 400_000 for i in z.infolist() if i.filename.endswith(".jpeg"))


def test_attach_uploads_when_missing(unit: Path):
    c = FakeClient()
    r = qa.attach_unit(unit, record_id="RID", bdbh="BJXQ1", client=c, confirm=True)
    assert r["status"] == "attached" and r["verified"] is True
    chunks = [a for n, a in c.calls if n == "erp_attachment" and a.get("files")
              and a["files"][0].get("upload_key") and a["files"][0].get("chunk_base64")]
    assert chunks, "应该走分片上传"
    f = chunks[0]["files"][0]
    assert f["name"] == "询价原始件（BJXQ1）" and f["chunk_base64"]


def test_attach_is_idempotent_by_name_prefix(unit: Path):
    c = FakeClient(existing=["询价原始件（旧名字）"])
    r = qa.attach_unit(unit, record_id="RID", bdbh="BJXQ1", client=c, confirm=True)
    assert r["status"] == "skipped"
    assert not [a for n, a in c.calls if n == "erp_attachment" and a.get("files")]


def test_attach_dry_run_does_not_upload(unit: Path):
    c = FakeClient()
    r = qa.attach_unit(unit, record_id="RID", bdbh="BJXQ1", client=c, confirm=False)
    assert r["status"] == "dry_run" and r["size"] > 0
    assert not [a for n, a in c.calls if a.get("files")]


def test_attach_force_replaces(unit: Path):
    c = FakeClient(existing=["询价原始件（BJXQ1）"])
    r = qa.attach_unit(unit, record_id="RID", bdbh="BJXQ1", client=c, confirm=True, force=True)
    assert r["status"] == "attached"
    assert any(a.get("delete") == "询价原始件（BJXQ1）" for n, a in c.calls)


def test_attach_reports_failure(unit: Path):
    c = FakeClient()
    c.fail_upload = True
    r = qa.attach_unit(unit, record_id="RID", bdbh="BJXQ1", client=c, confirm=True)
    assert r["status"] == "failed" and r["errors"]


def test_backfill_scans_receipts(tmp_path: Path, monkeypatch, unit: Path):
    # 把 CUSTOMER_FILES 指到临时树上
    root = unit.parent.parent          # .../pending 的父
    processed = root / "processed"; processed.mkdir()
    out = processed / "hash1"; out.mkdir()
    (out / "ERP_x.erp-push.json").write_text(json.dumps({
        "status": "created",
        "results": [{"status": "created", "bdbh": "BJXQ9",
                     "erp_response": {"record_id": "RID9", "bdbh": "BJXQ9"}}],
    }), encoding="utf-8")
    monkeypatch.setattr(qa, "CUSTOMER_FILES", root)

    c = FakeClient()
    r = qa.backfill(c, confirm=True)
    assert r["scanned"] == 1 and r["attached"] == 1
    assert any(a.get("files") for n, a in c.calls if n == "erp_attachment")
