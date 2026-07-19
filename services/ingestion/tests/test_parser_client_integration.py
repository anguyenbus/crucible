"""Task Group 3: ingestion routes non-`.md` through the parser over HTTP.

Lean suite — `parser_client` is MOCKED exactly as the BFF mocks `ingest_client`,
so nothing here imports docling/torch (the ingestion venv/CI stay torch-free) and
nothing touches a live parser. Focused coverage only (per task 3.1):

  - `.md` / `.markdown` still decodes locally (NO round-trip to the parser).
  - a non-`.md` source routes through the mocked parser client into the existing
    normalize → hash → chunk → embed → index chain.
  - typed error mapping: the parser's own 400 / 413 / 422 propagate with their
    status; a network fault → 502; a wedged/idle stall → 504.

SEAM NOTE (Task Group 6): `run_ingest`'s parsing phase now consumes the STREAMING
client (`parser_client.parse_stream`) so `/ingest/stream` can forward per-page
progress — so the `/ingest` integration tests below patch `parse_stream`, mirroring
how prior work re-pointed patch targets when the internal seam moved (pypdf →
`parser_client.parse` in Task Group 3). The DRAIN client (`parser_client.parse`)
and the drain seam (`extract_text` / `extract_document`) are unchanged, and their
DIRECT tests below still patch `parse`. Exhaustive per-format coverage is
deliberately skipped — the parser owns format breadth in its heavy suite.
"""

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.pipeline import parser_client as parser_client_module
from app.pipeline import run as run_module
from app.pipeline.parse import NoExtractableTextError, extract_text
from app.pipeline.parser_client import ParseResult, ParserError, parse

# Long enough to chunk into several pieces so embedding reports a real i-of-N.
PARSED_MARKDOWN = ("# Report\n\n" + ("hybrid search over the corpus " * 400)).strip()
LOCAL_MARKDOWN = "# Title\n\nSome body text about hybrid search.\n"


def _parse_result(markdown: str) -> ParseResult:
    return ParseResult(
        markdown=markdown,
        confidence={"mean": 0.9},
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
    """Mock embed + OpenSearch write/prune/dedup so the chain runs without AWS."""
    calls: list[tuple] = []

    def fake_embed_iter(chunks):
        chunks = list(chunks)
        calls.append(("embed", len(chunks)))
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


def test_markdown_decodes_locally_without_parser_roundtrip(monkeypatch):
    """`.md` / `.markdown` decode locally — the parser is NEVER called."""

    def explode(*args, **kwargs):
        raise AssertionError("parser_client must not be called for markdown")

    monkeypatch.setattr(parser_client_module, "parse", explode)
    monkeypatch.setattr(parser_client_module, "parse_stream", explode)

    assert extract_text("s3://b/guide.md", LOCAL_MARKDOWN.encode("utf-8")) == LOCAL_MARKDOWN
    assert (
        extract_text("/docs/guide.markdown", LOCAL_MARKDOWN.encode("utf-8"))
        == LOCAL_MARKDOWN
    )


def test_non_md_routes_through_parser_into_pipeline(client, mock_downstream, monkeypatch):
    """A non-`.md` source round-trips through the mocked parser into embed+index."""
    seen: dict[str, object] = {}

    def fake_parse_stream(data, filename, *, parser_url, on_phase):
        seen["data"] = data
        seen["filename"] = filename
        seen["parser_url"] = parser_url
        on_phase({"phase": "page", "current": 1, "total": 1, "route": "docling"})
        return _parse_result(PARSED_MARKDOWN)

    monkeypatch.setattr(parser_client_module, "parse_stream", fake_parse_stream)
    monkeypatch.setattr(run_module, "fetch_bytes", lambda source: b"%PDF-1.7 raw bytes")

    response = client.post(
        "/ingest", json={"source": "s3://bucket/report.pdf", "index": "proj-x"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["skipped"] is False and body["chunks_indexed"] >= 1
    # The raw bytes + the basename (extension drives parser dispatch) went over.
    assert seen["data"] == b"%PDF-1.7 raw bytes"
    assert seen["filename"] == "report.pdf"
    # The parsed markdown reached embedding + indexing into the project index.
    assert any(c[0] == "embed" for c in mock_downstream)
    index_calls = [c for c in mock_downstream if c[0] == "index"]
    assert index_calls and index_calls[0][3] == "proj-x"


def test_parser_422_maps_to_typed_422(client, mock_downstream, monkeypatch):
    """The parser's no-extractable-content 422 stays a 422 — never index nothing."""
    monkeypatch.setattr(run_module, "fetch_bytes", lambda source: b"scanned bytes")
    monkeypatch.setattr(
        parser_client_module,
        "parse_stream",
        lambda *a, **k: (_ for _ in ()).throw(ParserError(422, "no extractable content")),
    )

    response = client.post("/ingest", json={"source": "s3://bucket/scanned.pdf"})

    assert response.status_code == 422
    # Never embedded / indexed a document the parser found empty.
    assert not any(c[0] in ("embed", "index") for c in mock_downstream)


@pytest.mark.parametrize("status", [400, 413])
def test_parser_own_status_propagates(client, mock_downstream, monkeypatch, status):
    """The parser's own 400 (unsupported) / 413 (oversized) propagate unchanged."""
    monkeypatch.setattr(run_module, "fetch_bytes", lambda source: b"blob")
    monkeypatch.setattr(
        parser_client_module,
        "parse_stream",
        lambda *a, **k: (_ for _ in ()).throw(ParserError(status, f"parser said {status}")),
    )

    response = client.post("/ingest", json={"source": "s3://bucket/thing.bin"})

    assert response.status_code == status
    assert not any(c[0] in ("embed", "index") for c in mock_downstream)


def test_extract_text_maps_parser_422_to_no_extractable_text(monkeypatch):
    """Unit: a ParserError(422) is remapped to NoExtractableTextError at the seam."""
    monkeypatch.setattr(
        parser_client_module,
        "parse",
        lambda *a, **k: (_ for _ in ()).throw(ParserError(422, "empty markdown")),
    )
    with pytest.raises(NoExtractableTextError):
        extract_text("s3://b/scan.pdf", b"bytes")


def test_client_network_fault_maps_to_502(monkeypatch):
    """A network fault reaching the parser is normalized to a typed 502."""

    def raise_connect(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "post", raise_connect)

    with pytest.raises(ParserError) as exc:
        parse(b"bytes", "doc.pdf", parser_url="http://parser:8000")
    assert exc.value.status_code == 502


def test_client_idle_stall_maps_to_504(monkeypatch):
    """A wedged parse that stalls past the read (idle) timeout maps to a typed 504."""

    def raise_read_timeout(*args, **kwargs):
        raise httpx.ReadTimeout("read timed out")

    monkeypatch.setattr(httpx, "post", raise_read_timeout)

    with pytest.raises(ParserError) as exc:
        parse(b"bytes", "doc.pdf", parser_url="http://parser:8000")
    assert exc.value.status_code == 504
