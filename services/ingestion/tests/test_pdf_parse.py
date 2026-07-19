"""Parse stage: non-`.md` documents route through the parser service.

MAINTENANCE (Task Group 3): this file previously exercised the in-process pypdf
`extract_pdf_text` path. That seam is gone — non-`.md` documents now round-trip
through the parser service over HTTP, so pypdf is no longer an ingestion dependency
and the ingestion venv stays torch-free. The patches were moved from
`extract_pdf_text` / pypdf onto the new `parser_client` seam, mirroring how prior
work re-pointed patch targets when logic moved out of ingestion.

MAINTENANCE (Task Group 6): `run_ingest`'s parsing phase now consumes the STREAMING
client (`parser_client.parse_stream`) so `/ingest/stream` forwards per-page
progress, so the `/ingest` integration tests below patch `parse_stream` — the same
re-pointing pattern. The DRAIN seam (`extract_text` → `parser_client.parse`) is
unchanged, and the unit test that exercises it directly still patches `parse`.

The three behaviours asserted are unchanged in spirit:
  - a non-`.md` source parses to markdown then chunks + indexes,
  - the markdown path is UNCHANGED (REQUIRED regression, still fully local),
  - a no-extractable-content document fails honestly with a typed 422.

Exhaustive per-format PDF-structure permutations are skipped (the parser owns
format breadth in its heavy suite).
"""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.pipeline import parser_client as parser_client_module
from app.pipeline import run as run_module
from app.pipeline.parse import NoExtractableTextError, extract_text
from app.pipeline.parser_client import ParseResult, ParserError

PARSED_MARKDOWN = ("# Report\n\n" + ("Hello RAG hybrid search corpus " * 200)).strip()
MARKDOWN = "# Title\n\nSome body text about hybrid search.\n"


def _parse_result(markdown: str) -> ParseResult:
    return ParseResult(
        markdown=markdown,
        confidence={},
        page_count=1,
        page_routes=[{"route": "docling"}],
        call_counts={},
        warnings=[],
    )


@pytest.fixture
def client():
    return TestClient(create_app())


@pytest.fixture
def mock_downstream(monkeypatch):
    """Mock embed + OpenSearch write/prune/dedup (no network)."""
    calls: list[tuple] = []

    def fake_embed_iter(chunks):
        chunks = list(chunks)
        calls.append(("embed", chunks))
        for _ in chunks:
            yield [0.1] * 1024

    def fake_index(chunks, vectors, *, doc_id, source_uri, sha256, index=None, **kwargs):
        calls.append(("index", doc_id, len(chunks), index))
        return len(chunks)

    monkeypatch.setattr(run_module, "find_complete_raw_duplicate", lambda *a, **k: None)
    monkeypatch.setattr(run_module, "is_complete_duplicate", lambda *a, **k: False)
    monkeypatch.setattr(run_module, "embed_texts_iter", fake_embed_iter)
    monkeypatch.setattr(run_module, "index_chunks", fake_index)
    monkeypatch.setattr(run_module, "prune_stale_chunks", lambda *a, **k: 0)
    return calls


def test_pdf_source_parses_to_markdown_then_chunks_and_indexes(
    client, mock_downstream, monkeypatch
):
    """A `.pdf` source round-trips through the parser and yields indexed chunks."""
    monkeypatch.setattr(run_module, "fetch_bytes", lambda source: b"%PDF-1.7 bytes")
    monkeypatch.setattr(
        parser_client_module,
        "parse_stream",
        lambda *a, **k: _parse_result(PARSED_MARKDOWN),
    )

    response = client.post(
        "/ingest", json={"source": "s3://bucket/report.pdf", "index": "proj-x"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["skipped"] is False
    assert body["chunks_indexed"] >= 1
    assert any(call[0] == "embed" for call in mock_downstream)
    index_calls = [c for c in mock_downstream if c[0] == "index"]
    assert index_calls and index_calls[0][3] == "proj-x"


def test_markdown_path_is_unchanged(client, mock_downstream, monkeypatch):
    """REQUIRED regression: a `.md` source still UTF-8-decodes locally and indexes."""
    monkeypatch.setattr(
        run_module, "fetch_bytes", lambda source: MARKDOWN.encode("utf-8")
    )
    # If markdown ever round-tripped to the parser, this would blow up.
    def _fail(*a, **k):
        pytest.fail("markdown must not hit the parser")

    monkeypatch.setattr(parser_client_module, "parse", _fail)
    monkeypatch.setattr(parser_client_module, "parse_stream", _fail)

    response = client.post("/ingest", json={"source": "s3://bucket/guide.md"})

    assert response.status_code == 200
    assert response.json()["chunks_indexed"] >= 1
    assert any(call[0] == "embed" for call in mock_downstream)


def test_no_extractable_content_fails_with_typed_422(client, mock_downstream, monkeypatch):
    """A no-extractable-content document returns a typed 422 — never a silent index."""
    monkeypatch.setattr(run_module, "fetch_bytes", lambda source: b"scanned bytes")
    monkeypatch.setattr(
        parser_client_module,
        "parse_stream",
        lambda *a, **k: (_ for _ in ()).throw(ParserError(422, "no extractable content")),
    )

    response = client.post("/ingest", json={"source": "s3://bucket/scanned.pdf"})

    assert response.status_code == 422
    assert not any(call[0] in ("embed", "index") for call in mock_downstream)


def test_extract_text_dispatches_md_local_and_maps_parser_422(monkeypatch):
    """Unit: `.md` decodes locally; a parser 422 remaps to NoExtractableTextError."""
    assert extract_text("x.md", MARKDOWN.encode("utf-8")) == MARKDOWN

    monkeypatch.setattr(
        parser_client_module,
        "parse",
        lambda *a, **k: (_ for _ in ()).throw(ParserError(422, "empty markdown")),
    )
    with pytest.raises(NoExtractableTextError):
        extract_text("x.pdf", b"bytes")
