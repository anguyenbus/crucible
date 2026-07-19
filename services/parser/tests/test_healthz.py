"""Task Group 1: GET /healthz liveness endpoint (mirrors ingestion)."""

from fastapi.testclient import TestClient

from app.main import create_app


def test_healthz_returns_200():
    client = TestClient(create_app())

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
