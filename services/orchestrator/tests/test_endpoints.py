"""Endpoint tests: live ``POST /query`` (mocked clients) with REAL config resolution.

Phase 2 evolution of the Phase 1 canned-path tests: the canned payload moved
into ``tests/conftest.py`` as mock-client fixture data, and every assertion
that said ``stub`` now says ``live``. Health-probe tests live in
``tests/test_health.py``.
"""

VALID_REQUEST = {
    "question": "Is the supply of legal services to a non-resident GST-free?",
    "query_id": "q-001",
    "pipeline_config": "legal-rag-default-1.0.0",
}


def test_query_returns_200_with_real_config_resolution(client):
    """Config resolution is REAL: pipeline_version and config_sha256 come from the resolver."""
    from app.config import resolve_pipeline_config

    response = client.post("/query", json=VALID_REQUEST)
    assert response.status_code == 200

    resolved = resolve_pipeline_config("legal-rag-default-1.0.0")
    system_version = response.json()["result"]["system_version"]
    assert system_version["pipeline_version"] == "1.0.0"
    assert system_version["config_sha256"] == resolved.config_sha256


def test_query_echoes_metadata_verbatim(client):
    """Request metadata comes back untouched in result.query.metadata."""
    metadata = {"category": "gst", "nested": {"expected_answer_type": "boolean"}, "n": 3}
    response = client.post("/query", json={**VALID_REQUEST, "metadata": metadata})
    assert response.status_code == 200
    assert response.json()["result"]["query"]["metadata"] == metadata


def test_query_envelope_has_live_marker_and_empty_guardrail_decisions(client):
    """The envelope carries generation_mode='live' and a required-but-empty guardrail list."""
    response = client.post("/query", json=VALID_REQUEST)
    assert response.status_code == 200

    body = response.json()
    assert body["generation_mode"] == "live"
    assert body["guardrail_decisions"] == []


def test_query_unknown_config_returns_404_and_malformed_ref_returns_422(client):
    """Unknown-but-well-formed ref → 404 with a clear body; malformed ref → 422."""
    unknown = client.post(
        "/query", json={**VALID_REQUEST, "pipeline_config": "no-such-config-9.9.9"}
    )
    assert unknown.status_code == 404
    assert "no-such-config-9.9.9" in unknown.json()["detail"]

    malformed = client.post("/query", json={**VALID_REQUEST, "pipeline_config": "not_a_ref"})
    assert malformed.status_code == 422


def test_query_echoes_empty_string_query_id_verbatim(client):
    """query_id="" is a PROVIDED value: echoed verbatim, never replaced by a UUID."""
    response = client.post("/query", json={**VALID_REQUEST, "query_id": ""})
    assert response.status_code == 200
    assert response.json()["result"]["query"]["query_id"] == ""


def test_config_integrity_failure_returns_500_with_clean_json_body(client, monkeypatch):
    """ConfigIntegrityError from the resolver → app-level 500 handler, clean body."""
    import app.routers.query
    from app.config import ConfigIntegrityError

    def raise_integrity(ref):
        raise ConfigIntegrityError(
            f"Packaged config '{ref}.yaml' does not match the committed manifest "
            f"— released configs are immutable."
        )

    monkeypatch.setattr(app.routers.query, "resolve_pipeline_config", raise_integrity)

    response = client.post("/query", json=VALID_REQUEST)
    assert response.status_code == 500
    assert "legal-rag-default-1.0.0" in response.json()["detail"]


def test_resolver_detected_malformed_ref_emits_documented_422_list_shape(client, monkeypatch):
    """Defense-in-depth path: the handler emits FastAPI's field-level list shape."""
    import app.routers.query
    from app.config import MalformedConfigRefError

    def raise_malformed(ref):
        raise MalformedConfigRefError(f"Config reference {ref!r} is not fully qualified.")

    monkeypatch.setattr(app.routers.query, "resolve_pipeline_config", raise_malformed)

    response = client.post("/query", json=VALID_REQUEST)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, list)
    assert detail[0]["loc"] == ["body", "pipeline_config"]
    assert detail[0]["type"] == "string_pattern_mismatch"
    assert "not fully qualified" in detail[0]["msg"]
