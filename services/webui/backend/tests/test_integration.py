"""Task Group 6 (gap-fill): cross-request persistence + readyz upstream status.

Confirms a killed-ingestion failure is PERSISTED (retrievable on a later GET,
not just returned once), and that a non-200 from ingestion's healthz reads as
not_ready.
"""

import httpx

from app import health
from app.api import documents as documents_api
from app import ingest_client


def test_failed_upload_status_is_persisted_across_requests(client, monkeypatch):
    project_id = client.post("/projects", json={"name": "P"}).json()["id"]

    def _fail(source, ingestion_url, index=None, *, on_phase):
        raise ingest_client.IngestionError(502, "ingestion unreachable at http://x/ingest")

    monkeypatch.setattr(documents_api.ingest_client, "ingest_stream", _fail)

    upload = client.post(
        f"/projects/{project_id}/documents",
        files={"file": ("k.md", b"# k", "text/markdown")},
    )
    document_id = upload.json()["id"]
    # Upload is now async: the immediate response is the accepted, queued record;
    # the background job (run inline by TestClient) persists the failure.
    assert upload.status_code == 202
    assert upload.json()["status"] == "pending"

    # A fresh GET (new request/connection) still sees the persisted failure.
    fetched = client.get(f"/projects/{project_id}/documents/{document_id}")
    body = fetched.json()
    assert body["status"] == "failed"
    assert body["failure_code"] == 502
    assert "unreachable" in body["failure_reason"]


def test_readyz_not_ready_on_non_200_upstream(client, monkeypatch):
    def _degraded(url, **kwargs):
        return httpx.Response(500, request=httpx.Request("GET", url))

    monkeypatch.setattr(health.httpx, "get", _degraded)

    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert "500" in response.json()["detail"]
