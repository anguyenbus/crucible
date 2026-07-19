"""Task Group 6: BFF per-project index lifecycle + PDF-capable upload routing.

Focused tests (per task 6.1): provision on project-create + `index_name`
exposure; markdown AND PDF uploads route to `/ingest` with the `index` target;
PDF is now accepted; drop-on-delete calls ingestion's delete endpoint. The
ingestion HTTP calls (ingest + index lifecycle) and S3 are mocked; the firewall
stays green (no OpenSearch client, no sibling Python imports).
"""

from app import ingest_client
from app.api import documents as documents_api


def _capture_ingest(monkeypatch):
    """Capture the `index` passed to ingestion; return a recorder dict."""
    captured: dict = {}

    def _fake(source, ingestion_url, index=None, *, on_phase):
        captured["source"] = source
        captured["index"] = index
        return ingest_client.IngestResult("d", "s", 3, False)

    monkeypatch.setattr(documents_api.ingest_client, "ingest_stream", _fake)
    return captured


def test_project_create_provisions_index_and_exposes_index_name(client, mock_index_client):
    created = client.post("/projects", json={"name": "Matter A"})

    assert created.status_code == 201
    body = created.json()
    project_id = body["id"]
    # index_name is persisted + exposed as proj-{id}.
    assert body["index_name"] == f"proj-{project_id}"
    # Ingestion's provision endpoint was called for that index.
    assert mock_index_client.provision_calls == [f"proj-{project_id}"]

    # The exposed index_name round-trips on the detail record too.
    detail = client.get(f"/projects/{project_id}").json()
    assert detail["index_name"] == f"proj-{project_id}"


def test_markdown_upload_routes_to_ingest_with_the_index_target(client, monkeypatch):
    project_id = client.post("/projects", json={"name": "P"}).json()["id"]
    captured = _capture_ingest(monkeypatch)

    response = client.post(
        f"/projects/{project_id}/documents",
        files={"file": ("doc.md", b"# Title\n\nBody.", "text/markdown")},
    )

    assert response.status_code == 202
    assert captured["index"] == f"proj-{project_id}"


def test_pdf_upload_is_accepted_and_routes_with_the_index_target(client, monkeypatch):
    project_id = client.post("/projects", json={"name": "P"}).json()["id"]
    captured = _capture_ingest(monkeypatch)

    # A .pdf upload is now accepted (Phase-2 markdown-only validation extended).
    response = client.post(
        f"/projects/{project_id}/documents",
        files={"file": ("report.pdf", b"%PDF-1.4 fake", "application/pdf")},
    )

    assert response.status_code == 202
    # The background job (TestClient runs it inline) drove the row to indexed.
    doc_id = response.json()["id"]
    docs = client.get(f"/projects/{project_id}/documents").json()
    assert next(d for d in docs if d["id"] == doc_id)["status"] == "indexed"
    assert captured["index"] == f"proj-{project_id}"


def test_unsupported_type_still_rejected_before_storing(client, monkeypatch):
    project_id = client.post("/projects", json={"name": "P"}).json()["id"]

    def _boom(*args, **kwargs):
        raise AssertionError("must not reach ingestion for a rejected upload")

    monkeypatch.setattr(documents_api.ingest_client, "ingest_stream", _boom)

    response = client.post(
        f"/projects/{project_id}/documents",
        files={"file": ("evil.exe", b"binary", "application/octet-stream")},
    )
    assert response.status_code == 400


def test_project_delete_drops_the_index_via_ingestion(client, mock_index_client):
    project_id = client.post("/projects", json={"name": "P"}).json()["id"]

    deleted = client.delete(f"/projects/{project_id}")

    assert deleted.status_code == 204
    # Ingestion's delete endpoint was called for the project's index.
    assert mock_index_client.delete_calls == [f"proj-{project_id}"]
    # And the project is gone.
    assert client.get(f"/projects/{project_id}").status_code == 404


def test_provision_failure_on_create_rolls_back_and_returns_502(client, monkeypatch):
    """Gap-fill: a failed provision leaves NO orphan project and surfaces a 502."""
    from app import index_client

    def _boom(index_name, ingestion_url):
        raise index_client.IndexServiceError(502, "ingestion unreachable")

    monkeypatch.setattr(index_client, "provision", _boom)

    response = client.post("/projects", json={"name": "Doomed"})

    assert response.status_code == 502
    # No orphan project row survived the failed provision.
    assert client.get("/projects").json() == []


def test_delete_index_failure_returns_502_and_keeps_metadata_retry_safe(client, monkeypatch):
    """Gap-fill: index-drop failure surfaces 502 and does NOT delete metadata."""
    from app import index_client

    project_id = client.post("/projects", json={"name": "Keep"}).json()["id"]

    def _boom(index_name, ingestion_url):
        raise index_client.IndexServiceError(502, "ingestion unreachable")

    monkeypatch.setattr(index_client, "delete", _boom)

    response = client.delete(f"/projects/{project_id}")

    assert response.status_code == 502
    # The project metadata is intact (the delete is retry-safe, not half-done).
    assert client.get(f"/projects/{project_id}").status_code == 200


def test_document_delete_removes_chunks_via_ingestion_then_the_row(client, monkeypatch, mock_index_client):
    """Deleting a document deletes its chunks from the project index, then the row."""
    project_id = client.post("/projects", json={"name": "P"}).json()["id"]
    _capture_ingest(monkeypatch)  # upload gets ingest_doc_id "d"
    doc_id = client.post(
        f"/projects/{project_id}/documents",
        files={"file": ("doc.md", b"# T\n\nBody.", "text/markdown")},
    ).json()["id"]

    response = client.delete(f"/projects/{project_id}/documents/{doc_id}")

    assert response.status_code == 204
    # Chunk cleanup targeted the project index + the ingestion doc_id.
    assert mock_index_client.delete_document_calls == [(f"proj-{project_id}", "d")]
    # The row is gone.
    assert client.get(f"/projects/{project_id}/documents") .json() == []


def test_document_delete_gated_on_chunk_cleanup_failure_502_keeps_row(client, monkeypatch, mock_index_client):
    """If chunk cleanup fails, the delete is a 502 and the row is NOT dropped."""
    project_id = client.post("/projects", json={"name": "P"}).json()["id"]
    _capture_ingest(monkeypatch)
    doc_id = client.post(
        f"/projects/{project_id}/documents",
        files={"file": ("doc.md", b"# T\n\nBody.", "text/markdown")},
    ).json()["id"]

    mock_index_client.delete_document_fail_status = 502

    response = client.delete(f"/projects/{project_id}/documents/{doc_id}")

    assert response.status_code == 502
    # The document survives — a "deleted" doc never lingers as retrievable chunks.
    assert len(client.get(f"/projects/{project_id}/documents").json()) == 1
