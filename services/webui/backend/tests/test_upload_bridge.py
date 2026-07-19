"""Task Group 4: upload -> storage -> ingest bridge (ingestion HTTP + S3 MOCKED).

The bridge is synchronous: the endpoint blocks on the mocked ingestion call and
persists the outcome. Typed ingestion errors (400/404/502) map to a PERSISTED
`failed` status and are RETURNED as the document record, never a 5xx.
"""

from pathlib import Path

import pytest

from app import ingest_client
from app.api import documents as documents_api


@pytest.fixture
def project_id(client):
    return client.post("/projects", json={"name": "Upload"}).json()["id"]


def _mock_ingest(monkeypatch, *, result=None, error=None):
    def _fake(source, ingestion_url, index=None):
        if error is not None:
            raise error
        return result

    monkeypatch.setattr(documents_api.ingest_client, "ingest", _fake)


def _md_file(name="doc.md", body=b"# Title\n\nSome content."):
    return {"file": (name, body, "text/markdown")}


def test_upload_success_persists_indexed(client, project_id, monkeypatch):
    _mock_ingest(
        monkeypatch,
        result=ingest_client.IngestResult(
            doc_id="abc123", sha256="deadbeef", chunks_indexed=4, skipped=False
        ),
    )

    response = client.post(f"/projects/{project_id}/documents", files=_md_file())

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "indexed"
    assert body["ingest_doc_id"] == "abc123"
    assert body["sha256"] == "deadbeef"
    assert body["chunks_indexed"] == 4
    assert body["skipped"] is False
    assert body["storage_uri"].endswith("doc.md")


def test_upload_dedup_skip_persists_skipped(client, project_id, monkeypatch):
    _mock_ingest(
        monkeypatch,
        result=ingest_client.IngestResult(
            doc_id="abc123", sha256="deadbeef", chunks_indexed=0, skipped=True
        ),
    )

    body = client.post(f"/projects/{project_id}/documents", files=_md_file()).json()

    assert body["status"] == "skipped"
    assert body["skipped"] is True


@pytest.mark.parametrize(
    ("code", "message"),
    [
        (400, "Document produces 250 chunks, which exceeds the limit"),
        (404, "S3 object does not exist"),
        (502, "The embedding service is temporarily unavailable"),
    ],
)
def test_upload_typed_error_maps_to_failed(
    client, project_id, monkeypatch, code, message
):
    _mock_ingest(monkeypatch, error=ingest_client.IngestionError(code, message))

    response = client.post(f"/projects/{project_id}/documents", files=_md_file())

    # Ingestion-domain failures are RETURNED as the document record (not a 5xx).
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "failed"
    assert body["failure_code"] == code
    assert message in body["failure_reason"]


def test_upload_rejects_non_markdown_before_storing(client, project_id, monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("must not reach ingestion for a rejected upload")

    monkeypatch.setattr(documents_api.ingest_client, "ingest", _boom)

    response = client.post(
        f"/projects/{project_id}/documents",
        files={"file": ("evil.exe", b"binary", "application/octet-stream")},
    )
    assert response.status_code == 400


def test_upload_rejects_oversize_before_storing(client, project_id, monkeypatch):
    monkeypatch.setattr(documents_api, "MAX_UPLOAD_BYTES", 10)

    def _boom(*args, **kwargs):
        raise AssertionError("must not reach ingestion for an oversize upload")

    monkeypatch.setattr(documents_api.ingest_client, "ingest", _boom)

    response = client.post(
        f"/projects/{project_id}/documents",
        files=_md_file(body=b"# a much longer body than ten bytes"),
    )
    assert response.status_code == 400


def test_upload_uses_local_fallback_and_records_storage_uri(
    client, project_id, monkeypatch, tmp_path
):
    """Credential-less: bytes land under WEBUI_UPLOAD_DIR, local path recorded."""
    captured = {}

    def _fake(source, ingestion_url, index=None):
        captured["source"] = source
        captured["index"] = index
        return ingest_client.IngestResult("d", "s", 1, False)

    monkeypatch.setattr(documents_api.ingest_client, "ingest", _fake)

    body = client.post(f"/projects/{project_id}/documents", files=_md_file()).json()

    storage_uri = body["storage_uri"]
    assert storage_uri == captured["source"]
    assert Path(storage_uri).is_absolute()
    assert Path(storage_uri).exists()
    assert Path(storage_uri).read_bytes() == b"# Title\n\nSome content."


def test_upload_to_unknown_project_is_404(client, monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("must not reach ingestion for an unknown project")

    monkeypatch.setattr(documents_api.ingest_client, "ingest", _boom)

    assert (
        client.post("/projects/nope/documents", files=_md_file()).status_code == 404
    )
