"""Document-detail read endpoints: file serve + extracted-text proxy.

The `/file` route streams the stored bytes back inline with the right
Content-Type; the `/text` route proxies ingestion's chunk endpoint (mocked). A
failed doc with no `ingest_doc_id` returns empty text WITHOUT calling ingestion;
an unknown document is a 404; a genuinely missing stored file is a typed 404
(not a 500). Ingestion HTTP + S3 are mocked — the suite stays hermetic.
"""

import pytest

from app import index_client, storage
from app.api import documents as documents_api


@pytest.fixture
def project_id(client):
    return client.post("/projects", json={"name": "Detail"}).json()["id"]


def _upload_indexed(client, monkeypatch, project_id, *, name="doc.md", body=b"# Hi"):
    """Upload a doc whose mocked ingest succeeds (so it has an ingest_doc_id)."""

    def _fake(source, ingestion_url, index=None, *, on_phase):
        from app import ingest_client

        return ingest_client.IngestResult(
            doc_id="doc-1", sha256="s", chunks_indexed=2, skipped=False
        )

    monkeypatch.setattr(documents_api.ingest_client, "ingest_stream", _fake)
    return client.post(
        f"/projects/{project_id}/documents",
        files={"file": (name, body, "text/markdown")},
    ).json()


def test_file_serves_bytes_with_content_type_and_inline_disposition(
    client, project_id, monkeypatch
):
    body = b"# Title\n\nSome markdown."
    doc = _upload_indexed(client, monkeypatch, project_id, name="note.md", body=body)

    response = client.get(f"/projects/{project_id}/documents/{doc['id']}/file")

    assert response.status_code == 200
    assert response.content == body
    assert response.headers["content-type"].startswith("text/markdown")
    assert response.headers["content-disposition"] == 'inline; filename="note.md"'


def test_file_pdf_content_type(client, project_id, monkeypatch):
    doc = _upload_indexed(
        client, monkeypatch, project_id, name="report.pdf", body=b"%PDF-1.4 x"
    )

    response = client.get(f"/projects/{project_id}/documents/{doc['id']}/file")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/pdf")
    assert 'filename="report.pdf"' in response.headers["content-disposition"]


def test_file_unknown_document_is_404(client, project_id):
    assert (
        client.get(f"/projects/{project_id}/documents/nope/file").status_code == 404
    )


def test_file_missing_stored_bytes_is_typed_404_not_500(
    client, project_id, monkeypatch
):
    doc = _upload_indexed(client, monkeypatch, project_id)

    def _boom(storage_uri):
        raise storage.StoredObjectMissingError("gone")

    monkeypatch.setattr(documents_api.storage, "read_stored", _boom)

    response = client.get(f"/projects/{project_id}/documents/{doc['id']}/file")
    assert response.status_code == 404


def test_text_proxies_ingestion_chunks(client, project_id, monkeypatch):
    doc = _upload_indexed(client, monkeypatch, project_id)

    captured = {}

    def _fake_chunks(index_name, doc_id, ingestion_url):
        captured["index_name"] = index_name
        captured["doc_id"] = doc_id
        return {
            "index": index_name,
            "doc_id": doc_id,
            "chunk_count": 2,
            "text": "one\n\ntwo",
            "chunks": [
                {"chunk_index": 0, "content": "one"},
                {"chunk_index": 1, "content": "two"},
            ],
        }

    monkeypatch.setattr(
        documents_api.index_client, "get_document_chunks", _fake_chunks
    )

    body = client.get(f"/projects/{project_id}/documents/{doc['id']}/text").json()

    assert body["chunk_count"] == 2
    assert body["text"] == "one\n\ntwo"
    assert captured["index_name"] == f"proj-{project_id}"
    assert captured["doc_id"] == "doc-1"


def test_text_for_failed_doc_returns_empty_without_calling_ingestion(
    client, project_id, monkeypatch
):
    """A doc that never indexed (no ingest_doc_id) → empty, no ingestion call."""
    from app import ingest_client

    def _fail(source, ingestion_url, index=None, *, on_phase):
        raise ingest_client.IngestionError(502, "Bedrock upstream failure")

    monkeypatch.setattr(documents_api.ingest_client, "ingest_stream", _fail)
    queued = client.post(
        f"/projects/{project_id}/documents",
        files={"file": ("bad.md", b"# x", "text/markdown")},
    ).json()
    # The background job (run inline by TestClient) drove it to failed.
    docs = client.get(f"/projects/{project_id}/documents").json()
    doc = next(d for d in docs if d["id"] == queued["id"])
    assert doc["status"] == "failed"
    assert doc["ingest_doc_id"] is None

    def _boom(*args, **kwargs):
        raise AssertionError("must not call ingestion for a doc with no ingest_doc_id")

    monkeypatch.setattr(documents_api.index_client, "get_document_chunks", _boom)

    body = client.get(f"/projects/{project_id}/documents/{doc['id']}/text").json()
    assert body == {"chunk_count": 0, "text": "", "chunks": []}


def test_text_surfaces_ingestion_error_status(client, project_id, monkeypatch):
    doc = _upload_indexed(client, monkeypatch, project_id)

    def _boom(index_name, doc_id, ingestion_url):
        raise index_client.IndexServiceError(502, "ingestion unreachable")

    monkeypatch.setattr(documents_api.index_client, "get_document_chunks", _boom)

    response = client.get(f"/projects/{project_id}/documents/{doc['id']}/text")
    assert response.status_code == 502


def test_text_unknown_document_is_404(client, project_id):
    assert (
        client.get(f"/projects/{project_id}/documents/nope/text").status_code == 404
    )
