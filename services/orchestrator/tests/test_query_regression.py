"""``POST /query`` refactor-regression tests (chainlit-chat-ui Task Group 2).

Golden-capture tests written BEFORE the shared pre-generation refactor: the
first run (against the pre-refactor router) captures the golden artifacts
under ``tests/golden/``; every later run — including post-refactor — must
reproduce them byte-for-byte. Combined with the pre-generation failure-mapping
checks, this proves the streaming refactor left ``POST /query`` unchanged.

Focused checks only (per task 2.1) — exhaustive per-stage permutation tests
are intentionally skipped; the existing endpoint suite is the tripwire.
"""

from __future__ import annotations

import itertools
import json
import time
from pathlib import Path

import pytest
from app.clients import AppClients
from app.clients.errors import BedrockThrottleExhaustedError
from app.main import app as main_app
from fastapi.testclient import TestClient
from scripts.export_openapi import OPENAPI_PATH, render_openapi_document

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
GOLDEN_RESPONSE = GOLDEN_DIR / "query_response.golden.json"
GOLDEN_OPENAPI_FRAGMENT = GOLDEN_DIR / "query_openapi_fragment.golden.json"

# Fixed inputs: query_id provided (no uuid4 call) and metadata pinned, so the
# only nondeterministic response bytes are timings — removed by the fake clock.
FIXED_REQUEST = {
    "question": "Is the supply of legal services to a non-resident GST-free?",
    "query_id": "q-golden-001",
    "pipeline_config": "legal-rag-default-1.1.0",
    "metadata": {"category": "gst"},
}


def _capture_or_read(path: Path, fresh_bytes: bytes) -> bytes:
    """First run (pre-refactor) captures the golden; later runs compare."""
    if not path.is_file():
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(fresh_bytes)
    return path.read_bytes()


@pytest.fixture
def deterministic_clock(monkeypatch):
    """perf_counter → a fixed-step counter so timings_ms bytes are stable."""
    ticks = itertools.count()
    monkeypatch.setattr(time, "perf_counter", lambda: next(ticks) * 0.001)


def test_query_response_bytes_match_the_golden_capture(client, deterministic_clock):
    """REQUIRED: /query response BYTES identical pre/post the stream refactor."""
    response = client.post("/query", json=FIXED_REQUEST)
    assert response.status_code == 200

    golden = _capture_or_read(GOLDEN_RESPONSE, response.content)
    assert response.content == golden, (
        "POST /query response bytes drifted from the pre-refactor golden "
        "capture — the shared pre-generation refactor must not change /query."
    )


def test_query_openapi_fragment_matches_golden_and_committed_contract():
    """The /query OpenAPI path fragment is identical pre/post refactor."""
    fresh_fragment = (
        json.dumps(
            json.loads(render_openapi_document())["paths"]["/query"],
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")

    golden = _capture_or_read(GOLDEN_OPENAPI_FRAGMENT, fresh_fragment)
    assert fresh_fragment == golden, (
        "The /query OpenAPI fragment drifted from the pre-refactor golden — "
        "the streaming route must be purely additive."
    )

    # The committed contract artifact must carry the SAME /query fragment even
    # after the additive /query/stream re-export.
    committed_fragment = (
        json.dumps(
            json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))["paths"]["/query"],
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
    assert committed_fragment == golden


def test_config_resolution_failures_still_map_via_app_level_handlers(client):
    """404 unknown config / 422 malformed ref mapping is unchanged."""
    unknown = client.post(
        "/query", json={**FIXED_REQUEST, "pipeline_config": "no-such-config-9.9.9"}
    )
    assert unknown.status_code == 404
    assert "no-such-config-9.9.9" in unknown.json()["detail"]

    malformed = client.post("/query", json={**FIXED_REQUEST, "pipeline_config": "not_a_ref"})
    assert malformed.status_code == 422


def test_pre_generation_dependency_failures_still_map_via_app_level_handlers(
    client, mock_bedrock, mock_search
):
    """502 OpenSearch (search failure AND not-ready) and 503 embed-throttle unchanged."""
    # Embed-stage throttle exhaustion → 503 + Retry-After (dependency: bedrock).
    mock_bedrock.embed_error = BedrockThrottleExhaustedError(
        "Bedrock throttling persisted after 5 retries."
    )
    throttled = client.post("/query", json=FIXED_REQUEST)
    assert throttled.status_code == 503
    assert throttled.headers["Retry-After"].isdigit()
    assert throttled.json()["dependency"] == "bedrock"
    mock_bedrock.embed_error = None

    # OpenSearch search failure → 502 naming the dependency.
    from opensearchpy.exceptions import ConnectionError as OpenSearchConnectionError

    mock_search.search_error = OpenSearchConnectionError("N/A", "boom", Exception("boom"))
    search_failed = client.post("/query", json=FIXED_REQUEST)
    assert search_failed.status_code == 502
    assert search_failed.json()["dependency"] == "opensearch"
    mock_search.search_error = None

    # Not-ready OpenSearch client (the pre-flight guard) → 502. The client
    # fixture's state slots are restored (not deleted) so its teardown works.
    fixture_clients = main_app.state.clients
    main_app.state.clients = AppClients(
        bedrock=mock_bedrock,
        search=None,
        opensearch_unavailable_reason="OpenSearch endpoint not set.",
    )
    try:
        not_ready = TestClient(main_app).post("/query", json=FIXED_REQUEST)
    finally:
        main_app.state.clients = fixture_clients
    assert not_ready.status_code == 502
    assert not_ready.json()["dependency"] == "opensearch"
