"""Task Group 1: scaffold health/ready + config (TestClient, no live services)."""

import httpx

from app import health
from app.config import Settings


def test_healthz_makes_no_dependency_calls(client, monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("liveness must not call any dependency")

    monkeypatch.setattr(health.httpx, "get", _boom)

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_green_when_ingestion_up(client, monkeypatch):
    def _ok(url, **kwargs):
        return httpx.Response(200, request=httpx.Request("GET", url))

    monkeypatch.setattr(health.httpx, "get", _ok)

    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_readyz_surfaces_real_dependency_error_when_down(client, monkeypatch):
    def _refused(url, **kwargs):
        raise httpx.ConnectError("Connection refused", request=httpx.Request("GET", url))

    monkeypatch.setattr(health.httpx, "get", _refused)

    response = client.get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert "Connection refused" in body["detail"]


def test_settings_load_webui_prefix_env_over_default(monkeypatch):
    monkeypatch.setenv("WEBUI_INGESTION_URL", "http://ingest.example:9000")
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    settings = Settings()

    assert settings.ingestion_url == "http://ingest.example:9000"
    assert settings.orchestrator_url == "http://localhost:8000"  # untouched default
    assert settings.aws_region == "us-east-1"
