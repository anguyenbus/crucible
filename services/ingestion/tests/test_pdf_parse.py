"""Task Group 4: PDF parse stage (pypdf native text) ahead of normalize/chunk.

Focused tests only (per task 4.1): a fixture PDF parses to text and flows
through the pipeline (S3 + Bedrock mocked); the markdown path is unchanged
(REQUIRED regression); a no-extractable-text PDF fails honestly with a typed
422. Exhaustive PDF-structure permutations are skipped. Fixtures were generated
with pypdf itself (`tests/fixtures/sample_text.pdf`, `sample_notext.pdf`).
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import ingest as ingest_module
from app.main import create_app
from app.pipeline.parse import NoExtractableTextError, extract_pdf_text, extract_text

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TEXT_PDF = (FIXTURES / "sample_text.pdf").read_bytes()
NOTEXT_PDF = (FIXTURES / "sample_notext.pdf").read_bytes()
MARKDOWN = "# Title\n\nSome body text about hybrid search.\n"


@pytest.fixture
def client():
    return TestClient(create_app())


@pytest.fixture
def mock_downstream(monkeypatch):
    """Mock S3 fetch + Bedrock embed + OpenSearch write/prune/dedup (no network)."""
    calls: list[tuple] = []

    def fake_dedup(doc_id, sha256, expected_chunk_count, index=None):
        return False

    def fake_embed(chunks):
        calls.append(("embed", list(chunks)))
        return [[0.1] * 1024 for _ in chunks]

    def fake_index(chunks, vectors, *, doc_id, source_uri, sha256, index=None):
        calls.append(("index", doc_id, len(chunks), index))
        return len(chunks)

    def fake_prune(doc_id, *, keep_sha256, index=None):
        return 0

    monkeypatch.setattr(ingest_module, "is_complete_duplicate", fake_dedup)
    monkeypatch.setattr(ingest_module, "embed_texts", fake_embed)
    monkeypatch.setattr(ingest_module, "index_chunks", fake_index)
    monkeypatch.setattr(ingest_module, "prune_stale_chunks", fake_prune)
    return calls


def test_pdf_source_parses_to_text_then_chunks_and_indexes(client, mock_downstream, monkeypatch):
    """A `.pdf` source is pypdf-parsed to text and yields non-empty indexed chunks."""
    monkeypatch.setattr(ingest_module, "fetch_bytes", lambda source: TEXT_PDF)

    response = client.post(
        "/ingest", json={"source": "s3://bucket/report.pdf", "index": "proj-x"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["skipped"] is False
    assert body["chunks_indexed"] >= 1
    # The extracted PDF text reached embedding + indexing (into the project index).
    assert any(call[0] == "embed" for call in mock_downstream)
    index_calls = [c for c in mock_downstream if c[0] == "index"]
    assert index_calls and index_calls[0][3] == "proj-x"


def test_markdown_path_is_unchanged(client, mock_downstream, monkeypatch):
    """REQUIRED regression: a `.md` source still UTF-8-decodes and chunks/embeds."""
    monkeypatch.setattr(
        ingest_module, "fetch_bytes", lambda source: MARKDOWN.encode("utf-8")
    )

    response = client.post("/ingest", json={"source": "s3://bucket/guide.md"})

    assert response.status_code == 200
    assert response.json()["chunks_indexed"] >= 1
    assert any(call[0] == "embed" for call in mock_downstream)


def test_scanned_pdf_with_no_text_fails_with_typed_422(client, mock_downstream, monkeypatch):
    """A no-extractable-text PDF returns a typed 422 — never a silent empty index."""
    monkeypatch.setattr(ingest_module, "fetch_bytes", lambda source: NOTEXT_PDF)

    response = client.post("/ingest", json={"source": "s3://bucket/scanned.pdf"})

    assert response.status_code == 422
    assert "no extractable text" in response.json()["detail"].lower()
    # Never embedded / indexed a document with no text.
    assert not any(call[0] in ("embed", "index") for call in mock_downstream)


def test_extract_text_dispatches_and_pdf_helper_raises_on_empty():
    """Unit-level: extract_text dispatches by extension; empty PDF raises typed error."""
    assert extract_text("x.md", MARKDOWN.encode("utf-8")) == MARKDOWN
    assert "Hello RAG" in extract_text("x.pdf", TEXT_PDF)
    with pytest.raises(NoExtractableTextError):
        extract_pdf_text(NOTEXT_PDF)
