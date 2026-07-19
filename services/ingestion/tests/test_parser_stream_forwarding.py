"""Task Group 6 (ingestion side): per-page forwarding through `/ingest/stream`.

Lean suite — `parser_client.parse_stream` is MOCKED (and, for the client-boundary
tests, `httpx.stream` is faked with crafted frames), so nothing here imports
docling/torch (the ingestion venv/CI stay torch-free) and nothing touches a live
parser. Two concerns, focused coverage only (per task 6.1):

  - `run_ingest`'s `parsing` phase RE-EMITS one `PhaseEvent("parsing", current=i,
    total=N)` per parser `page` frame (the nested stream), on top of the leading
    bare `parsing` phase — the `.md`-local path stays a single bare `parsing`.
  - the `parse_stream` HTTP client maps the boundary failures: a stream that ends
    without a terminal frame → 502; an idle stall → 504; a network fault → 502.

The parser's `page` frames are a POST-HOC manifest (a burst after the opaque
parse); the mock reproduces that shape (all page frames, then the `done`).
"""

import json

import httpx
import pytest

from app.pipeline import parser_client as parser_client_module
from app.pipeline import run as run_module
from app.pipeline.parser_client import ParseResult, ParserError, parse_stream
from app.pipeline.run import PhaseEvent, ResultEvent, run_ingest
from app.schemas.ingest import IngestRequest

PARSED_MARKDOWN = ("# Report\n\n" + ("hybrid search over the corpus " * 400)).strip()


def _parse_result(markdown: str = PARSED_MARKDOWN) -> ParseResult:
    return ParseResult(
        markdown=markdown,
        confidence={"document": 0.9, "pages": []},
        page_count=3,
        page_routes=[{"route": "docling-kept"}, {"route": "textract"}, {"route": "textract"}],
        call_counts={"vlm": 0, "textract": 2},
        warnings=[],
    )


@pytest.fixture
def mock_downstream(monkeypatch):
    """Mock fetch + gate + embed + OpenSearch write/prune/dedup (no AWS)."""

    def fake_embed_iter(chunks):
        for _ in chunks:
            yield [0.1] * 1024

    def fake_index(chunks, vectors, *, doc_id, source_uri, sha256, index=None, **kwargs):
        return len(chunks)

    monkeypatch.setattr(run_module, "fetch_bytes", lambda source: b"%PDF-1.7 raw bytes")
    monkeypatch.setattr(run_module, "find_complete_raw_duplicate", lambda *a, **k: None)
    monkeypatch.setattr(run_module, "is_complete_duplicate", lambda *a, **k: False)
    monkeypatch.setattr(run_module, "embed_texts_iter", fake_embed_iter)
    monkeypatch.setattr(run_module, "index_chunks", fake_index)
    monkeypatch.setattr(run_module, "prune_stale_chunks", lambda *a, **k: 0)


# --------------------------------------------------------------------------- #
# run_ingest re-emits one `parsing` frame per parser page
# --------------------------------------------------------------------------- #
def test_run_ingest_reemits_parsing_frame_per_page(mock_downstream, monkeypatch):
    """A 3-page parser stream → three `parsing` current/total frames (1..3 of 3)."""

    def fake_parse_stream(data, filename, *, parser_url, on_phase):
        # The parser's POST-HOC page-frame burst (mirrors the real endpoint).
        for i in range(1, 4):
            on_phase({"phase": "page", "current": i, "total": 3, "route": "textract"})
        return _parse_result()

    monkeypatch.setattr(parser_client_module, "parse_stream", fake_parse_stream)

    events = list(run_ingest(IngestRequest(source="s3://bucket/report.pdf")))

    parsing = [e for e in events if isinstance(e, PhaseEvent) and e.phase == "parsing"]
    # The leading bare `parsing` (no counters) then one per page (current/total).
    assert parsing[0].current is None and parsing[0].total is None
    paged = [e for e in parsing if e.current is not None]
    assert [(e.current, e.total) for e in paged] == [(1, 3), (2, 3), (3, 3)]

    # The per-page frames precede chunking, and the run still completes normally.
    phases = [e.phase for e in events if isinstance(e, PhaseEvent)]
    assert phases.index("parsing") < phases.index("chunking")
    result = events[-1]
    assert isinstance(result, ResultEvent) and result.skipped is False


def test_md_local_path_stays_single_bare_parsing_phase(mock_downstream, monkeypatch):
    """The `.md`-local path never streams — a single bare `parsing`, no page frames."""

    def explode(*args, **kwargs):
        raise AssertionError("markdown must not round-trip through parse_stream")

    monkeypatch.setattr(parser_client_module, "parse_stream", explode)
    monkeypatch.setattr(
        run_module, "fetch_bytes", lambda source: PARSED_MARKDOWN.encode("utf-8")
    )

    events = list(run_ingest(IngestRequest(source="s3://bucket/guide.md")))

    parsing = [e for e in events if isinstance(e, PhaseEvent) and e.phase == "parsing"]
    assert len(parsing) == 1
    assert parsing[0].current is None and parsing[0].total is None


# --------------------------------------------------------------------------- #
# parse_stream HTTP-client boundary error mapping
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(self, status_code, lines=(), json_body=None, text=""):
        self.status_code = status_code
        self._lines = list(lines)
        self._json = json_body
        self._text = text

    def read(self):
        return b""

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json

    @property
    def text(self):
        return self._text

    def iter_lines(self):
        yield from self._lines


class _FakeStreamCtx:
    def __init__(self, response):
        self._response = response

    def __enter__(self):
        return self._response

    def __exit__(self, *args):
        return False


def _install_stream(monkeypatch, *, response=None, raise_exc=None):
    def _stream(method, url, **kwargs):
        if raise_exc is not None:
            raise raise_exc
        return _FakeStreamCtx(response)

    monkeypatch.setattr(parser_client_module.httpx, "stream", _stream)


def _frame(payload: dict) -> str:
    return f"data: {json.dumps(payload)}"


def _call(on_phase=lambda e: None):
    return parse_stream(
        b"bytes", "doc.pdf", parser_url="http://parser:8000", on_phase=on_phase
    )


def test_parse_stream_pages_then_done_returns_result(monkeypatch):
    lines = [
        _frame({"phase": "parsing"}),
        "",
        _frame({"phase": "page", "current": 1, "total": 2, "route": "textract"}),
        "",
        _frame({"phase": "page", "current": 2, "total": 2, "route": "docling-kept"}),
        "",
        _frame(
            {
                "phase": "done",
                "markdown": "# md",
                "confidence": {"document": 0.9},
                "page_count": 2,
                "page_routes": [{"route": "textract"}, {"route": "docling-kept"}],
                "call_counts": {"textract": 1},
                "warnings": [],
            }
        ),
        "",
    ]
    _install_stream(monkeypatch, response=_FakeResponse(200, lines=lines))

    seen: list[dict] = []
    result = _call(on_phase=seen.append)

    assert result.markdown == "# md" and result.page_count == 2
    # Every NON-terminal frame reached the callback (the leading `parsing` and both
    # `page` frames); the terminal `done` did NOT — it became the returned result.
    assert [e["phase"] for e in seen] == ["parsing", "page", "page"]
    page_frames = [e for e in seen if e["phase"] == "page"]
    assert [(e["current"], e["total"]) for e in page_frames] == [(1, 2), (2, 2)]


def test_parse_stream_error_frame_raises_typed_error(monkeypatch):
    lines = [
        _frame({"phase": "page", "current": 1, "total": 1, "route": "textract"}),
        "",
        _frame({"phase": "error", "status": 502, "detail": "escalation unreachable"}),
        "",
    ]
    _install_stream(monkeypatch, response=_FakeResponse(200, lines=lines))

    with pytest.raises(ParserError) as exc:
        _call()
    assert exc.value.status_code == 502
    assert exc.value.message == "escalation unreachable"


def test_parse_stream_without_terminal_event_is_502(monkeypatch):
    lines = [_frame({"phase": "page", "current": 1, "total": 1}), ""]  # no done/error
    _install_stream(monkeypatch, response=_FakeResponse(200, lines=lines))

    with pytest.raises(ParserError) as exc:
        _call()
    assert exc.value.status_code == 502
    assert "terminal" in exc.value.message


def test_parse_stream_idle_stall_maps_to_504(monkeypatch):
    _install_stream(
        monkeypatch,
        raise_exc=httpx.ReadTimeout(
            "timed out", request=httpx.Request("POST", "http://parser:8000/parse/stream")
        ),
    )

    with pytest.raises(ParserError) as exc:
        _call()
    assert exc.value.status_code == 504
    assert "stalled" in exc.value.message


def test_parse_stream_network_fault_maps_to_502(monkeypatch):
    _install_stream(
        monkeypatch,
        raise_exc=httpx.ConnectError(
            "refused", request=httpx.Request("POST", "http://parser:8000/parse/stream")
        ),
    )

    with pytest.raises(ParserError) as exc:
        _call()
    assert exc.value.status_code == 502
    assert "unreachable" in exc.value.message


def test_parse_stream_non_200_status_line_raises(monkeypatch):
    _install_stream(
        monkeypatch, response=_FakeResponse(413, json_body={"detail": "too many pages"})
    )

    with pytest.raises(ParserError) as exc:
        _call()
    assert exc.value.status_code == 413
    assert exc.value.message == "too many pages"
