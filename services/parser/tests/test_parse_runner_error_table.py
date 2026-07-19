"""Task Group 2: the error-translation table over crafted `parse_to_markdown`.

These tests drive `app.parse_runner.run_parse` — the single source of truth for
the status table — against CRAFTED `parse_to_markdown` outputs (the vendored
entrypoint is patched, never invoked), so they run WITHOUT docling/torch installed.
That is the whole point of the lazy-import indirection: patching
`app.parse_runner.parse_to_markdown` replaces the entrypoint before it is ever
called, so no heavy import happens.

One test per row of the Q7 table (plus the two 413 sub-cases). Exhaustive per-format
coverage is deliberately skipped.
"""

import pytest

from app import parse_runner
from app.parse_runner import PipelineError, ResultEvent, run_parse


def _drain(raw_bytes: bytes = b"%PDF-1.4 crafted", filename: str = "doc.pdf"):
    """Run the generator to its terminal event, returning the `ResultEvent`.

    Re-raises a `PipelineError` so a test can assert on `.status`.
    """
    result = None
    for event in run_parse(raw_bytes, filename):
        if isinstance(event, ResultEvent):
            result = event
    return result


def _patch_parse(monkeypatch, output: dict) -> None:
    """Patch the lazy entrypoint so no docling import ever happens."""
    monkeypatch.setattr(parse_runner, "parse_to_markdown", lambda _path: output)


def _route(route: str, reason=None, n_chars: int = 100, page_index: int = 0) -> dict:
    return {
        "page_index": page_index,
        "route": route,
        "reason": reason,
        "n_chars": n_chars,
    }


def _result(markdown="", page_routes=None, warnings=None, call_counts=None) -> dict:
    return {
        "markdown": markdown,
        "page_routes": page_routes or [],
        "warnings": warnings or [],
        "confidence": {"document": 0.9, "pages": []},
        "call_counts": call_counts or {"vlm": 0, "textract": 0},
    }


# --- Row 1: non-empty markdown → 200 (warnings ride along) ------------------
def test_non_empty_markdown_returns_200_with_warnings(monkeypatch):
    _patch_parse(
        monkeypatch,
        _result(
            markdown="# Title\n\nBody text.",
            page_routes=[
                _route("docling-kept", n_chars=10, page_index=0),
                _route("textract-fallback-docling", reason="low_coverage", n_chars=8, page_index=1),
            ],
            warnings=[{"code": "vlm_fallback_docling", "message": "Textract error on page 1", "scope": "page"}],
        ),
    )

    result = _drain()

    assert isinstance(result, ResultEvent)
    assert result.markdown == "# Title\n\nBody text."
    assert result.page_count == 2
    # Partial degradation is ALWAYS loud: the N/M fallback summary rides along.
    messages = [w["message"] for w in result.warnings]
    assert any("1/2 pages fell back to Docling" in m for m in messages)
    # Confidence is present but NEVER gated on.
    assert result.confidence["document"] == 0.9


# --- Row 2: empty markdown OR every page errored → 422 ----------------------
def test_empty_markdown_returns_422(monkeypatch):
    _patch_parse(
        monkeypatch,
        _result(
            markdown="",
            page_routes=[_route("docling-kept", n_chars=0)],
        ),
    )

    with pytest.raises(PipelineError) as exc:
        _drain()

    assert exc.value.status == 422


def test_every_page_errored_returns_422(monkeypatch):
    # Every page fell back with NO usable Docling content (hard pages, permanent
    # error) → empty markdown → 422 (not retryable — nothing to retry against).
    _patch_parse(
        monkeypatch,
        _result(
            markdown="",
            page_routes=[
                _route("textract-fallback-docling", reason="page_unparseable", n_chars=0, page_index=0),
                _route("textract-fallback-docling", reason="page_unparseable", n_chars=0, page_index=1),
            ],
        ),
    )

    with pytest.raises(PipelineError) as exc:
        _drain()

    assert exc.value.status == 422


# --- Row 3: oversized (page cap OR size cap) → 413 --------------------------
def test_over_page_cap_returns_413(monkeypatch):
    # More page_routes than PARSER_MAX_PAGES (100 default) → 413 from the result.
    _patch_parse(
        monkeypatch,
        _result(
            markdown="content",
            page_routes=[_route("docling-kept", page_index=i) for i in range(101)],
        ),
    )

    with pytest.raises(PipelineError) as exc:
        _drain()

    assert exc.value.status == 413


def test_over_size_cap_returns_413_before_parse(monkeypatch):
    # Input-size cap is enforced pre-parse: parse_to_markdown must never be called.
    def _boom(_path):
        raise AssertionError("parse_to_markdown must not run for an oversized input")

    monkeypatch.setattr(parse_runner, "parse_to_markdown", _boom)
    oversized = b"x" * (51 * 1024 * 1024)  # 51 MB > 50 MB cap

    with pytest.raises(PipelineError) as exc:
        _drain(raw_bytes=oversized, filename="big.pdf")

    assert exc.value.status == 413


# --- Row 4: unsupported / unknown type → 400 --------------------------------
def test_unsupported_type_returns_400(monkeypatch):
    _patch_parse(
        monkeypatch,
        _result(
            markdown="",
            page_routes=[],
            warnings=[{"code": "unsupported_type", "message": "Unsupported file type: .xyz", "scope": "document"}],
        ),
    )

    with pytest.raises(PipelineError) as exc:
        _drain(filename="mystery.xyz")

    assert exc.value.status == 400


# --- Row 5: ALL escalation calls failed TRANSIENTLY, majority → 502 ---------
def test_all_transient_escalation_failures_majority_returns_502(monkeypatch):
    # 2 of 3 pages fell back with reason="throttled" (the transient marker), no
    # engine success, no hard fallback → all-transient AND page-count majority.
    _patch_parse(
        monkeypatch,
        _result(
            markdown="salvaged docling text",
            page_routes=[
                _route("textract-fallback-docling", reason="throttled", n_chars=5, page_index=0),
                _route("textract-fallback-docling", reason="throttled", n_chars=5, page_index=1),
                _route("docling-kept", reason=None, n_chars=5, page_index=2),
            ],
        ),
    )

    with pytest.raises(PipelineError) as exc:
        _drain()

    assert exc.value.status == 502


# --- Row 6: escalation unreachable but Docling usable → 200 -----------------
def test_transient_escalation_minority_but_docling_usable_returns_200(monkeypatch):
    # Escalation was down on 1 of 3 pages (transient), but Docling carried the
    # majority — do NOT fail the doc; ship 200 with a loud fallback warning.
    _patch_parse(
        monkeypatch,
        _result(
            markdown="mostly clean docling markdown",
            page_routes=[
                _route("docling-kept", reason=None, n_chars=50, page_index=0),
                _route("docling-kept", reason=None, n_chars=50, page_index=1),
                _route("textract-fallback-docling", reason="throttled", n_chars=5, page_index=2),
            ],
        ),
    )

    result = _drain()

    assert isinstance(result, ResultEvent)
    assert result.page_count == 3
    messages = [w["message"] for w in result.warnings]
    assert any("1/3 pages fell back to Docling" in m for m in messages)
