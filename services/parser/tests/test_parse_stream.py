"""Task Group 6 (parser side): the `POST /parse/stream` SSE contract.

Heavy suite, but `parse_to_markdown` is PATCHED with crafted outputs (the vendored
entrypoint is never invoked), so these run WITHOUT docling/torch — the same
lazy-import trick the error-table tests use.

The contract asserted here (mirrors `services/ingestion/tests/test_ingest_stream.py`):
  - HTTP status is 200 for the WHOLE stream (a mid-stream failure is a terminal
    `error` frame, NEVER a status change after the headers);
  - exactly one `page` frame per page, each `{"phase": "page", "current": i,
    "total": N, "route": ...}`, with `current` running 1..N;
  - then EXACTLY ONE terminal frame — `{"phase": "done", ...result}` on success or
    `{"phase": "error", "status": N, "detail": ...}` on a typed failure.

The `page` frames are a POST-HOC per-page manifest (see `parse_runner.PhaseEvent`),
which is exactly why the drain result is unaffected — asserted in the last test.
"""

import json

import pytest
from fastapi.testclient import TestClient

from app import parse_runner
from app.main import create_app


@pytest.fixture
def client():
    return TestClient(create_app())


def _patch_parse(monkeypatch, output: dict) -> None:
    """Patch the lazy entrypoint so no docling import ever happens."""
    monkeypatch.setattr(parse_runner, "parse_to_markdown", lambda _path: output)


def _route(route: str, page_index: int = 0, n_chars: int = 100) -> dict:
    return {"page_index": page_index, "route": route, "reason": None, "n_chars": n_chars}


def _result(markdown="", page_routes=None, warnings=None) -> dict:
    return {
        "markdown": markdown,
        "page_routes": page_routes or [],
        "warnings": warnings or [],
        "confidence": {"document": 0.9, "pages": []},
        "call_counts": {"vlm": 0, "textract": 0},
    }


def _events(response) -> list[dict]:
    """Parse the SSE body into the list of decoded `data:` JSON payloads."""
    return [
        json.loads(line[len("data:") :].strip())
        for line in response.text.splitlines()
        if line.startswith("data:")
    ]


def _post_stream(client, filename="doc.pdf", body=b"%PDF-1.4 crafted"):
    return client.post(
        "/parse/stream",
        content=body,
        params={"filename": filename},
        headers={"content-type": "application/octet-stream"},
    )


def test_stream_emits_one_page_frame_per_page_then_done(client, monkeypatch):
    """Three pages → three `page` frames (current 1..3, with routes) then one done."""
    _patch_parse(
        monkeypatch,
        _result(
            markdown="# Title\n\nBody across three pages.",
            page_routes=[
                _route("docling-kept", page_index=0),
                _route("textract", page_index=1),
                _route("textract-fallback-docling", page_index=2),
            ],
        ),
    )

    response = _post_stream(client)

    # 200 for the whole stream; the body is SSE.
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _events(response)

    page_frames = [e for e in events if e.get("phase") == "page"]
    assert [e["current"] for e in page_frames] == [1, 2, 3]
    assert all(e["total"] == 3 for e in page_frames)
    assert [e["route"] for e in page_frames] == [
        "docling-kept",
        "textract",
        "textract-fallback-docling",
    ]

    # Exactly one terminal frame, and it is `done` carrying the full result shape.
    terminals = [e for e in events if e.get("phase") in ("done", "error")]
    assert len(terminals) == 1
    done = terminals[0]
    assert done["phase"] == "done"
    assert done["markdown"] == "# Title\n\nBody across three pages."
    assert done["page_count"] == 3
    assert {"confidence", "page_routes", "call_counts", "warnings"} <= done.keys()
    # The terminal frame is LAST.
    assert events[-1]["phase"] == "done"


def test_stream_midstream_failure_is_terminal_error_frame_not_status_change(
    client, monkeypatch
):
    """Empty markdown → the failure rides as a terminal `error` frame; status is 200."""
    _patch_parse(
        monkeypatch,
        _result(markdown="", page_routes=[_route("docling-kept", n_chars=0)]),
    )

    response = _post_stream(client)

    # The stream itself is a 200; the 422 rides inside the terminal event.
    assert response.status_code == 200
    events = _events(response)
    error = events[-1]
    assert error["phase"] == "error"
    assert error["status"] == 422
    assert "no extractable content" in str(error["detail"]).lower()
    # No `done` frame accompanies an error — exactly one terminal frame.
    terminals = [e for e in events if e.get("phase") in ("done", "error")]
    assert len(terminals) == 1


def test_stream_oversized_is_terminal_error_413_still_http_200(client, monkeypatch):
    """A pre-parse 413 (size cap) is a terminal error frame, not a 413 status line."""

    def _boom(_path):
        raise AssertionError("parse_to_markdown must not run for an oversized input")

    monkeypatch.setattr(parse_runner, "parse_to_markdown", _boom)
    oversized = b"x" * (51 * 1024 * 1024)  # 51 MB > 50 MB cap

    response = _post_stream(client, filename="big.pdf", body=oversized)

    assert response.status_code == 200
    error = _events(response)[-1]
    assert error["phase"] == "error"
    assert error["status"] == 413


def test_stream_and_drain_agree_on_result(client, monkeypatch):
    """The stream's `done` frame equals the drain body — page frames don't leak in.

    This is the severability guarantee at the endpoint: adding per-page frames to
    the shared generator does NOT change the drain `POST /parse` result shape.
    """
    output = _result(
        markdown="# Same\n\nContent.",
        page_routes=[_route("docling-kept", page_index=0), _route("textract", page_index=1)],
    )
    _patch_parse(monkeypatch, output)

    drain = client.post(
        "/parse",
        content=b"%PDF-1.4 crafted",
        params={"filename": "doc.pdf"},
        headers={"content-type": "application/octet-stream"},
    )
    assert drain.status_code == 200
    drain_body = drain.json()

    stream_done = [e for e in _events(_post_stream(client)) if e.get("phase") == "done"][0]
    # The `done` frame is the drain body plus the `phase` marker.
    assert {k: v for k, v in stream_done.items() if k != "phase"} == drain_body
