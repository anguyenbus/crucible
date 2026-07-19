"""Task Group 6 (gap-fill): ingestion HTTP-boundary error mapping.

Exercises `ingest_client.ingest` (blocking) and `ingest_client.ingest_stream`
(SSE) against crafted httpx responses (no live service) so the typed-error
matrix and the per-phase framing are validated at the actual HTTP boundary, not
only via the mocked client used by the upload-bridge tests.
"""

import json

import httpx
import pytest

from app import ingest_client


def _response(status_code, json_body):
    request = httpx.Request("POST", "http://ingestion.test/ingest")
    return httpx.Response(status_code, json=json_body, request=request)


def test_success_maps_to_ingest_result(monkeypatch):
    monkeypatch.setattr(
        ingest_client.httpx,
        "post",
        lambda *a, **k: _response(
            200, {"doc_id": "d", "sha256": "s", "chunks_indexed": 3, "skipped": False}
        ),
    )
    result = ingest_client.ingest("s3://b/k.md", "http://ingestion.test")
    assert (result.doc_id, result.chunks_indexed, result.skipped) == ("d", 3, False)


@pytest.mark.parametrize("code", [400, 404])
def test_string_detail_errors_map_with_code(monkeypatch, code):
    monkeypatch.setattr(
        ingest_client.httpx,
        "post",
        lambda *a, **k: _response(code, {"detail": "bad source"}),
    )
    with pytest.raises(ingest_client.IngestionError) as exc:
        ingest_client.ingest("bad", "http://ingestion.test")
    assert exc.value.status_code == code
    assert exc.value.message == "bad source"


def test_502_dict_detail_extracts_message(monkeypatch):
    monkeypatch.setattr(
        ingest_client.httpx,
        "post",
        lambda *a, **k: _response(
            502, {"detail": {"message": "The search index is temporarily unavailable."}}
        ),
    )
    with pytest.raises(ingest_client.IngestionError) as exc:
        ingest_client.ingest("s3://b/k.md", "http://ingestion.test")
    assert exc.value.status_code == 502
    assert exc.value.message == "The search index is temporarily unavailable."


def test_network_error_normalizes_to_502(monkeypatch):
    def _raise(*args, **kwargs):
        raise httpx.ConnectError(
            "Connection refused", request=httpx.Request("POST", "http://ingestion.test/ingest")
        )

    monkeypatch.setattr(ingest_client.httpx, "post", _raise)
    with pytest.raises(ingest_client.IngestionError) as exc:
        ingest_client.ingest("s3://b/k.md", "http://ingestion.test")
    assert exc.value.status_code == 502
    assert "unreachable" in exc.value.message


# --- ingest_stream (SSE) ----------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code, lines=(), json_body=None, text=""):
        self.status_code = status_code
        self._lines = list(lines)
        self._json = json_body
        self._text = text

    def read(self):  # materialize the body (no-op for the fake)
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

    monkeypatch.setattr(ingest_client.httpx, "stream", _stream)


def _frame(payload: dict) -> str:
    return f"data: {json.dumps(payload)}"


def test_stream_emits_phases_then_returns_done_result(monkeypatch):
    lines = [
        _frame({"phase": "parsing"}),
        "",
        _frame({"phase": "chunking"}),
        "",
        _frame({"phase": "embedding", "current": 1, "total": 2}),
        "",
        _frame({"phase": "embedding", "current": 2, "total": 2}),
        "",
        _frame(
            {
                "phase": "done",
                "doc_id": "d",
                "sha256": "s",
                "chunks_indexed": 2,
                "skipped": False,
            }
        ),
        "",
    ]
    _install_stream(monkeypatch, response=_FakeResponse(200, lines=lines))

    seen: list[dict] = []
    result = ingest_client.ingest_stream(
        "s3://b/k.md", "http://ingestion.test", index="proj-x", on_phase=seen.append
    )

    assert (result.doc_id, result.chunks_indexed, result.skipped) == ("d", 2, False)
    # Every non-terminal phase reached the callback, in order.
    assert [e["phase"] for e in seen] == ["parsing", "chunking", "embedding", "embedding"]
    assert seen[-1] == {"phase": "embedding", "current": 2, "total": 2}


def test_stream_error_frame_raises_typed_error(monkeypatch):
    lines = [
        _frame({"phase": "parsing"}),
        "",
        _frame({"phase": "error", "status": 404, "detail": "no such object"}),
        "",
    ]
    _install_stream(monkeypatch, response=_FakeResponse(200, lines=lines))

    with pytest.raises(ingest_client.IngestionError) as exc:
        ingest_client.ingest_stream(
            "s3://b/missing.md", "http://ingestion.test", on_phase=lambda e: None
        )
    assert exc.value.status_code == 404
    assert exc.value.message == "no such object"


def test_stream_error_frame_flattens_dict_detail(monkeypatch):
    lines = [
        _frame(
            {
                "phase": "error",
                "status": 502,
                "detail": {"message": "The search index is temporarily unavailable."},
            }
        ),
        "",
    ]
    _install_stream(monkeypatch, response=_FakeResponse(200, lines=lines))

    with pytest.raises(ingest_client.IngestionError) as exc:
        ingest_client.ingest_stream(
            "s3://b/k.md", "http://ingestion.test", on_phase=lambda e: None
        )
    assert exc.value.status_code == 502
    assert exc.value.message == "The search index is temporarily unavailable."


def test_stream_non_200_status_line_raises(monkeypatch):
    _install_stream(
        monkeypatch,
        response=_FakeResponse(400, json_body={"detail": "bad source"}),
    )

    with pytest.raises(ingest_client.IngestionError) as exc:
        ingest_client.ingest_stream(
            "bad", "http://ingestion.test", on_phase=lambda e: None
        )
    assert exc.value.status_code == 400
    assert exc.value.message == "bad source"


def test_stream_network_error_normalizes_to_502(monkeypatch):
    _install_stream(
        monkeypatch,
        raise_exc=httpx.ConnectError(
            "refused", request=httpx.Request("POST", "http://ingestion.test/ingest/stream")
        ),
    )

    with pytest.raises(ingest_client.IngestionError) as exc:
        ingest_client.ingest_stream(
            "s3://b/k.md", "http://ingestion.test", on_phase=lambda e: None
        )
    assert exc.value.status_code == 502
    assert "unreachable" in exc.value.message


def test_stream_idle_timeout_maps_to_504_stalled(monkeypatch):
    _install_stream(
        monkeypatch,
        raise_exc=httpx.ReadTimeout(
            "timed out",
            request=httpx.Request("POST", "http://ingestion.test/ingest/stream"),
        ),
    )

    with pytest.raises(ingest_client.IngestionError) as exc:
        ingest_client.ingest_stream(
            "s3://b/k.md", "http://ingestion.test", on_phase=lambda e: None
        )
    # A hang (not a crash) is distinguished from unreachable, and is terminal.
    assert exc.value.status_code == 504
    assert "stalled" in exc.value.message


def test_stream_without_terminal_event_is_502(monkeypatch):
    lines = [_frame({"phase": "parsing"}), ""]  # never a done/error frame
    _install_stream(monkeypatch, response=_FakeResponse(200, lines=lines))

    with pytest.raises(ingest_client.IngestionError) as exc:
        ingest_client.ingest_stream(
            "s3://b/k.md", "http://ingestion.test", on_phase=lambda e: None
        )
    assert exc.value.status_code == 502
    assert "terminal" in exc.value.message
