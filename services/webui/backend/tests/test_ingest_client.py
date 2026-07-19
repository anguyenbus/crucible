"""Task Group 6 (gap-fill): ingestion HTTP-boundary error mapping.

Exercises `ingest_client.ingest` against crafted httpx responses (no live
service) so the typed-error matrix is validated at the actual HTTP boundary,
not only via the mocked client used by the upload-bridge tests.
"""

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
