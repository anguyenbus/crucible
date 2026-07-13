"""Health-probe tests (Group 6): real readyz semantics over mocked clients.

Focused checks only (per task 6.1) — exhaustive cache-expiry timing tests are
intentionally skipped.
"""

from app.clients import AppClients
from app.main import app as main_app
from app.routers import health as health_module
from fastapi.testclient import TestClient

from tests.conftest import FIXTURE_SETTINGS


def test_readyz_returns_200_when_both_dependencies_are_up(client, mock_bedrock, mock_search):
    """Manifest + OpenSearch index_exists + Bedrock credential chain all pass."""
    response = client.get("/readyz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["released_configs"] >= 2  # 1.0.0 and 1.1.0 are released
    # Both dependency probes actually ran (no fake checks).
    assert mock_search.index_exists_calls == 1
    assert mock_bedrock.credentials_calls == 1


def test_readyz_returns_503_naming_the_down_dependency(client, mock_bedrock, mock_search):
    """Either dependency down → 503 with a which-dependency detail."""
    mock_search.index_exists_result = False
    opensearch_down = client.get("/readyz")
    assert opensearch_down.status_code == 503
    assert "opensearch" in opensearch_down.json()["detail"]

    health_module._reset_readyz_cache()
    mock_search.index_exists_result = True
    mock_bedrock.credentials_ok = False
    bedrock_down = client.get("/readyz")
    assert bedrock_down.status_code == 503
    assert "bedrock" in bedrock_down.json()["detail"]


def test_readyz_result_is_ttl_cached(client, mock_bedrock, mock_search):
    """A second call within the TTL does not re-hit the mocked dependencies."""
    assert client.get("/readyz").status_code == 200
    assert client.get("/readyz").status_code == 200

    assert mock_search.index_exists_calls == 1
    assert mock_bedrock.credentials_calls == 1


def test_readyz_returns_503_when_meta_guard_left_search_unconstructed(mock_bedrock):
    """_meta present-but-mismatched at init renders the service not-ready."""
    main_app.state.settings = FIXTURE_SETTINGS
    main_app.state.clients = AppClients(
        bedrock=mock_bedrock,
        search=None,
        opensearch_unavailable_reason=(
            "Embedder mismatch for index 'legal-rag-bench': index _meta "
            "declares 'some-other-model'."
        ),
    )
    try:
        response = TestClient(main_app).get("/readyz")
    finally:
        del main_app.state.settings
        del main_app.state.clients

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "opensearch" in detail
    assert "Embedder mismatch" in detail


def test_healthz_stays_pure_liveness_with_no_dependency_calls(client, mock_bedrock, mock_search):
    """Liveness never touches OpenSearch or Bedrock."""
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert mock_search.index_exists_calls == 0
    assert mock_bedrock.credentials_calls == 0
