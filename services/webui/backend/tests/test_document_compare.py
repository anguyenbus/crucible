"""Document compare endpoint: `POST /projects/{id}/compare` (contradictions).

The BFF fetches BOTH documents' indexed text from ingestion (mocked) and
forwards them to the orchestrator's `/compare` (mocked) — it never runs an LLM
itself. Comparing a document with itself is a 400; a document with no indexed
text is a 422; the orchestrator's typed errors surface with their real status.
Both HTTP hops are mocked so the suite stays hermetic.
"""

import pytest

from app import compare_client, index_client
from app.api import documents as documents_api


@pytest.fixture
def project_id(client):
    return client.post("/projects", json={"name": "Compare"}).json()["id"]


def _upload_indexed(client, monkeypatch, project_id, *, name):
    def _fake(source, ingestion_url, index=None, *, on_phase):
        from app import ingest_client

        return ingest_client.IngestResult(
            doc_id=f"ingest-{name}", sha256="s", chunks_indexed=2, skipped=False
        )

    monkeypatch.setattr(documents_api.ingest_client, "ingest_stream", _fake)
    return client.post(
        f"/projects/{project_id}/documents",
        files={"file": (name, b"# Hi", "text/markdown")},
    ).json()


def _mock_chunks(monkeypatch, text="the document body"):
    def _fake_chunks(index_name, doc_id, ingestion_url):
        return {"index": index_name, "doc_id": doc_id, "chunk_count": 2, "text": text}

    monkeypatch.setattr(documents_api.index_client, "get_document_chunks", _fake_chunks)


def _two_indexed(client, monkeypatch, project_id):
    a = _upload_indexed(client, monkeypatch, project_id, name="a.md")
    b = _upload_indexed(client, monkeypatch, project_id, name="b.md")
    return a, b


def test_compare_forwards_both_texts_and_returns_report(client, project_id, monkeypatch):
    a, b = _two_indexed(client, monkeypatch, project_id)
    _mock_chunks(monkeypatch, text="body text")

    captured = {}

    def _fake_compare(text_a, text_b, orchestrator_url, *, label_a, label_b):
        captured.update(
            text_a=text_a, text_b=text_b, label_a=label_a, label_b=label_b
        )
        return compare_client.CompareResult(
            contradictions=[
                compare_client.Contradiction(
                    type="temporal",
                    description="Different dates.",
                    quote_a="Jan 15",
                    quote_b="end of Q1",
                )
            ],
            model_id="au.anthropic.claude-sonnet-4-6",
            truncated=False,
        )

    monkeypatch.setattr(documents_api.compare_client, "compare_documents", _fake_compare)

    response = client.post(
        f"/projects/{project_id}/compare",
        json={"document_id_a": a["id"], "document_id_b": b["id"]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["document_a"] == "a.md"
    assert body["document_b"] == "b.md"
    assert body["contradictions"][0]["type"] == "temporal"
    assert body["contradictions"][0]["quote_b"] == "end of Q1"
    # Both documents' text is forwarded, labelled with their filenames.
    assert captured["text_a"] == "body text"
    assert captured["label_a"] == "a.md"
    assert captured["label_b"] == "b.md"


def test_compare_same_document_is_400_without_calling_orchestrator(
    client, project_id, monkeypatch
):
    a, _ = _two_indexed(client, monkeypatch, project_id)

    def _boom(*args, **kwargs):
        raise AssertionError("must not compare a document with itself")

    monkeypatch.setattr(documents_api.compare_client, "compare_documents", _boom)

    response = client.post(
        f"/projects/{project_id}/compare",
        json={"document_id_a": a["id"], "document_id_b": a["id"]},
    )
    assert response.status_code == 400


def test_compare_unindexed_document_is_422(client, project_id, monkeypatch):
    from app import ingest_client

    a = _upload_indexed(client, monkeypatch, project_id, name="ok.md")

    def _fail(source, ingestion_url, index=None, *, on_phase):
        raise ingest_client.IngestionError(502, "Bedrock upstream failure")

    monkeypatch.setattr(documents_api.ingest_client, "ingest_stream", _fail)
    bad = client.post(
        f"/projects/{project_id}/documents",
        files={"file": ("bad.md", b"# x", "text/markdown")},
    ).json()
    _mock_chunks(monkeypatch)

    def _boom(*args, **kwargs):
        raise AssertionError("must not compare when a document has no indexed text")

    monkeypatch.setattr(documents_api.compare_client, "compare_documents", _boom)

    response = client.post(
        f"/projects/{project_id}/compare",
        json={"document_id_a": a["id"], "document_id_b": bad["id"]},
    )
    assert response.status_code == 422


def test_compare_surfaces_orchestrator_error_status(client, project_id, monkeypatch):
    a, b = _two_indexed(client, monkeypatch, project_id)
    _mock_chunks(monkeypatch)

    def _boom(text_a, text_b, orchestrator_url, *, label_a, label_b):
        raise compare_client.CompareServiceError(503, "bedrock throttled")

    monkeypatch.setattr(documents_api.compare_client, "compare_documents", _boom)

    response = client.post(
        f"/projects/{project_id}/compare",
        json={"document_id_a": a["id"], "document_id_b": b["id"]},
    )
    assert response.status_code == 503


def test_compare_surfaces_ingestion_error_status(client, project_id, monkeypatch):
    a, b = _two_indexed(client, monkeypatch, project_id)

    def _boom(index_name, doc_id, ingestion_url):
        raise index_client.IndexServiceError(502, "ingestion unreachable")

    monkeypatch.setattr(documents_api.index_client, "get_document_chunks", _boom)

    response = client.post(
        f"/projects/{project_id}/compare",
        json={"document_id_a": a["id"], "document_id_b": b["id"]},
    )
    assert response.status_code == 502


def test_compare_unknown_document_is_404(client, project_id, monkeypatch):
    a, _ = _two_indexed(client, monkeypatch, project_id)
    response = client.post(
        f"/projects/{project_id}/compare",
        json={"document_id_a": a["id"], "document_id_b": "nope"},
    )
    assert response.status_code == 404
