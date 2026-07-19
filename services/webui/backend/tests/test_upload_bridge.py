"""upload -> storage -> ASYNC ingest bridge (ingestion SSE + S3 MOCKED).

Upload is now asynchronous: the endpoint stores the bytes, creates a
`pending`/`queued` document, schedules a background job, and returns 202 with the
queued record. The background job streams ingestion's phases into the row and
writes the terminal status. FastAPI's TestClient runs the background task
synchronously as part of the request, so a follow-up GET already sees the
terminal outcome. Typed ingestion errors (400/404/502) become a PERSISTED
`failed` status — never a 5xx.
"""

from pathlib import Path

import pytest

from app import ingest_client
from app.api import documents as documents_api


@pytest.fixture
def project_id(client):
    return client.post("/projects", json={"name": "Upload"}).json()["id"]


def _mock_stream(monkeypatch, *, result=None, error=None, phases=()):
    """Replace `ingest_client.ingest_stream` with a fake that emits `phases`
    to the job's callback, then returns `result` or raises `error`."""

    def _fake(source, ingestion_url, index=None, *, on_phase):
        for event in phases:
            on_phase(event)
        if error is not None:
            raise error
        return result

    monkeypatch.setattr(documents_api.ingest_client, "ingest_stream", _fake)


def _md_file(name="doc.md", body=b"# Title\n\nSome content."):
    return {"file": (name, body, "text/markdown")}


def _get_doc(client, project_id, document_id):
    docs = client.get(f"/projects/{project_id}/documents").json()
    return next(d for d in docs if d["id"] == document_id)


def test_upload_returns_202_queued_then_persists_indexed(client, project_id, monkeypatch):
    _mock_stream(
        monkeypatch,
        result=ingest_client.IngestResult(
            doc_id="abc123", sha256="deadbeef", chunks_indexed=4, skipped=False
        ),
    )

    response = client.post(f"/projects/{project_id}/documents", files=_md_file())

    # The immediate response is the accepted, still-queued record.
    assert response.status_code == 202
    queued = response.json()
    assert queued["status"] == "pending"
    assert queued["phase"] == "queued"

    # The background job has since driven it to a terminal, phase-cleared state.
    final = _get_doc(client, project_id, queued["id"])
    assert final["status"] == "indexed"
    assert final["ingest_doc_id"] == "abc123"
    assert final["sha256"] == "deadbeef"
    assert final["chunks_indexed"] == 4
    assert final["skipped"] is False
    assert final["phase"] is None
    assert final["storage_uri"].endswith("doc.md")


def test_upload_dedup_skip_persists_skipped(client, project_id, monkeypatch):
    _mock_stream(
        monkeypatch,
        result=ingest_client.IngestResult(
            doc_id="abc123", sha256="deadbeef", chunks_indexed=0, skipped=True
        ),
    )

    queued = client.post(f"/projects/{project_id}/documents", files=_md_file()).json()
    final = _get_doc(client, project_id, queued["id"])

    assert final["status"] == "skipped"
    assert final["skipped"] is True
    assert final["phase"] is None


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
    _mock_stream(monkeypatch, error=ingest_client.IngestionError(code, message))

    response = client.post(f"/projects/{project_id}/documents", files=_md_file())

    # The upload itself still succeeds (202); the ingestion-domain failure is
    # PERSISTED on the row, never surfaced as a 5xx on the upload.
    assert response.status_code == 202
    final = _get_doc(client, project_id, response.json()["id"])
    assert final["status"] == "failed"
    assert final["failure_code"] == code
    assert message in final["failure_reason"]
    assert final["phase"] is None


def test_upload_rejects_non_markdown_before_storing(client, project_id, monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("must not reach ingestion for a rejected upload")

    monkeypatch.setattr(documents_api.ingest_client, "ingest_stream", _boom)

    response = client.post(
        f"/projects/{project_id}/documents",
        files={"file": ("evil.exe", b"binary", "application/octet-stream")},
    )
    assert response.status_code == 400


def test_upload_rejects_oversize_before_storing(client, project_id, monkeypatch):
    monkeypatch.setattr(documents_api, "MAX_UPLOAD_BYTES", 10)

    def _boom(*args, **kwargs):
        raise AssertionError("must not reach ingestion for an oversize upload")

    monkeypatch.setattr(documents_api.ingest_client, "ingest_stream", _boom)

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

    def _fake(source, ingestion_url, index=None, *, on_phase):
        captured["source"] = source
        captured["index"] = index
        return ingest_client.IngestResult("d", "s", 1, False)

    monkeypatch.setattr(documents_api.ingest_client, "ingest_stream", _fake)

    body = client.post(f"/projects/{project_id}/documents", files=_md_file()).json()

    storage_uri = body["storage_uri"]
    assert storage_uri == captured["source"]
    assert Path(storage_uri).is_absolute()
    assert Path(storage_uri).exists()
    assert Path(storage_uri).read_bytes() == b"# Title\n\nSome content."
    # The project's per-project index is threaded through to ingestion.
    assert captured["index"] == f"proj-{project_id}"


def test_upload_to_unknown_project_is_404(client, monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("must not reach ingestion for an unknown project")

    monkeypatch.setattr(documents_api.ingest_client, "ingest_stream", _boom)

    assert (
        client.post("/projects/nope/documents", files=_md_file()).status_code == 404
    )
