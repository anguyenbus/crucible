"""Document analyze endpoint: `POST /documents/{id}/analyze` (facts/summary).

The BFF fetches the document's indexed text from ingestion (mocked) and
forwards it to the orchestrator's `/analyze` (mocked) — it never runs an LLM
itself. `mode` is threaded through to the orchestrator; a doc with no indexed
text is an honest 422; the orchestrator's typed errors surface with their real
status. Both HTTP hops are mocked so the suite stays hermetic.
"""

import pytest

from app import analyze_client, index_client
from app.api import documents as documents_api


@pytest.fixture
def project_id(client):
    return client.post("/projects", json={"name": "Analyze"}).json()["id"]


def _upload_indexed(client, monkeypatch, project_id, *, name="doc.md", body=b"# Hi"):
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


def _mock_chunks(monkeypatch, text="one\n\ntwo"):
    def _fake_chunks(index_name, doc_id, ingestion_url):
        return {"index": index_name, "doc_id": doc_id, "chunk_count": 2, "text": text}

    monkeypatch.setattr(documents_api.index_client, "get_document_chunks", _fake_chunks)


def test_analyze_forwards_text_and_mode_and_returns_result(
    client, project_id, monkeypatch
):
    doc = _upload_indexed(client, monkeypatch, project_id)
    _mock_chunks(monkeypatch, text="the document body")

    captured = {}

    def _fake_analyze(text, orchestrator_url, *, mode="both", max_facts=12):
        captured["text"] = text
        captured["mode"] = mode
        return analyze_client.AnalyzeResult(
            summary="", facts=["a", "b"], model_id="m", truncated=False
        )

    monkeypatch.setattr(documents_api.analyze_client, "analyze_text", _fake_analyze)

    response = client.post(
        f"/projects/{project_id}/documents/{doc['id']}/analyze?mode=facts"
    )

    assert response.status_code == 200
    body = response.json()
    assert body == {"summary": "", "facts": ["a", "b"], "model_id": "m", "truncated": False}
    assert captured["text"] == "the document body"
    assert captured["mode"] == "facts"


def test_analyze_defaults_to_both_mode(client, project_id, monkeypatch):
    doc = _upload_indexed(client, monkeypatch, project_id)
    _mock_chunks(monkeypatch)

    seen = {}

    def _fake_analyze(text, orchestrator_url, *, mode="both", max_facts=12):
        seen["mode"] = mode
        return analyze_client.AnalyzeResult("s", ["f"], "m", False)

    monkeypatch.setattr(documents_api.analyze_client, "analyze_text", _fake_analyze)

    client.post(f"/projects/{project_id}/documents/{doc['id']}/analyze")
    assert seen["mode"] == "both"


def test_analyze_no_ingest_doc_id_is_422_without_calling_orchestrator(
    client, project_id, monkeypatch
):
    from app import ingest_client

    def _fail(source, ingestion_url, index=None, *, on_phase):
        raise ingest_client.IngestionError(502, "Bedrock upstream failure")

    monkeypatch.setattr(documents_api.ingest_client, "ingest_stream", _fail)
    queued = client.post(
        f"/projects/{project_id}/documents",
        files={"file": ("bad.md", b"# x", "text/markdown")},
    ).json()
    # The background job (run inline by TestClient) drove it to failed/no doc id.
    docs = client.get(f"/projects/{project_id}/documents").json()
    doc = next(d for d in docs if d["id"] == queued["id"])
    assert doc["ingest_doc_id"] is None

    def _boom(*a, **k):
        raise AssertionError("must not analyse a doc with no indexed text")

    monkeypatch.setattr(documents_api.analyze_client, "analyze_text", _boom)

    response = client.post(f"/projects/{project_id}/documents/{doc['id']}/analyze")
    assert response.status_code == 422


def test_analyze_empty_indexed_text_is_422(client, project_id, monkeypatch):
    doc = _upload_indexed(client, monkeypatch, project_id)
    _mock_chunks(monkeypatch, text="   ")  # whitespace only → nothing to analyse

    def _boom(*a, **k):
        raise AssertionError("must not analyse empty text")

    monkeypatch.setattr(documents_api.analyze_client, "analyze_text", _boom)

    response = client.post(f"/projects/{project_id}/documents/{doc['id']}/analyze")
    assert response.status_code == 422


def test_analyze_surfaces_ingestion_error_status(client, project_id, monkeypatch):
    doc = _upload_indexed(client, monkeypatch, project_id)

    def _boom(index_name, doc_id, ingestion_url):
        raise index_client.IndexServiceError(502, "ingestion unreachable")

    monkeypatch.setattr(documents_api.index_client, "get_document_chunks", _boom)

    response = client.post(f"/projects/{project_id}/documents/{doc['id']}/analyze")
    assert response.status_code == 502


def test_analyze_surfaces_orchestrator_error_status(client, project_id, monkeypatch):
    doc = _upload_indexed(client, monkeypatch, project_id)
    _mock_chunks(monkeypatch)

    def _boom(text, orchestrator_url, *, mode="both", max_facts=12):
        raise analyze_client.AnalyzeServiceError(503, "bedrock throttled")

    monkeypatch.setattr(documents_api.analyze_client, "analyze_text", _boom)

    response = client.post(f"/projects/{project_id}/documents/{doc['id']}/analyze")
    assert response.status_code == 503


def test_analyze_unknown_document_is_404(client, project_id):
    assert (
        client.post(f"/projects/{project_id}/documents/nope/analyze").status_code == 404
    )
