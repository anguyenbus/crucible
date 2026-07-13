"""Tests for the orchestrator HTTP query callable (mocked HTTP; no live server)."""

import json
from pathlib import Path

import httpx
import pytest
from dev.stubs.rag import orchestrator_query as adapter

RESULT_PAYLOAD = {
    "schema_version": "1.1.0",
    "system_version": {"pipeline_version": "1.1.0", "config_sha256": "abc123"},
    "query": {"query_id": "q-1", "text": "What is GST-free?"},
    "answer": {"text": "It is GST-free [gst-act-1999:0].", "citations": []},
    "retrieved_chunks": [],
}

ENVELOPE = {
    "result": RESULT_PAYLOAD,
    "guardrail_decisions": [],
    "generation_mode": "live",
}


def _recording_transport(seen_requests, envelope=ENVELOPE):
    """MockTransport that records (url, body) and answers a 200 envelope."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append((str(request.url), json.loads(request.content)))
        return httpx.Response(200, json=envelope)

    return httpx.MockTransport(handler)


def test_posts_question_and_default_pipeline_config_and_unwraps_result(monkeypatch):
    """POST {question, pipeline_config} to {ORCHESTRATOR_URL}/query; return result."""
    monkeypatch.delenv(adapter.ENV_URL, raising=False)
    monkeypatch.delenv(adapter.ENV_PIPELINE_CONFIG, raising=False)
    seen = []

    result = adapter.query(
        "What is GST-free?",
        corpus_dir=Path("ignored-corpus"),  # documented stub-ism
        transport=_recording_transport(seen),
    )

    url, body = seen[0]
    assert url == "http://localhost:8000/query"
    # Exactly {question, pipeline_config}; ORCHESTRATOR_PIPELINE_CONFIG unset
    # defaults to legal-rag-default-1.1.0, and no query_id is invented.
    assert body == {
        "question": "What is GST-free?",
        "pipeline_config": "legal-rag-default-1.1.0",
    }
    # The one-line unwrap: the callable returns envelope["result"] verbatim.
    assert result == RESULT_PAYLOAD


def test_env_overrides_and_supplied_query_id_are_honored(monkeypatch):
    """ORCHESTRATOR_URL / ORCHESTRATOR_PIPELINE_CONFIG env + query_id forwarding."""
    monkeypatch.setenv(adapter.ENV_URL, "http://orchestrator.test:9999")
    monkeypatch.setenv(adapter.ENV_PIPELINE_CONFIG, "legal-rag-custom-2.0.0")
    seen = []

    adapter.query("Q?", query_id="q-42", transport=_recording_transport(seen))

    url, body = seen[0]
    assert url == "http://orchestrator.test:9999/query"
    assert body == {
        "question": "Q?",
        "pipeline_config": "legal-rag-custom-2.0.0",
        "query_id": "q-42",
    }


def test_non_200_raises_a_clear_error_naming_status_and_body(monkeypatch):
    """A failed query is NEVER scored: non-200 raises with status + body."""
    monkeypatch.delenv(adapter.ENV_URL, raising=False)
    error_body = {"detail": "Bedrock throttling persisted.", "dependency": "bedrock"}
    transport = httpx.MockTransport(lambda request: httpx.Response(503, json=error_body))

    with pytest.raises(adapter.OrchestratorQueryError, match="HTTP 503") as exc_info:
        adapter.query("Q?", transport=transport)

    assert exc_info.value.status_code == 503
    assert "bedrock" in str(exc_info.value)
