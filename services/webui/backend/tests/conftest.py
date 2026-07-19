"""Shared fixtures: a hermetic BFF TestClient over a temp SQLite DB.

No live services: the ingestion HTTP call and S3/boto3 are mocked in the tests
that exercise them. Per-project index lifecycle (provision on create, drop on
delete) is an HTTP call to ingestion; `mock_index_client` (autouse) stubs it to
succeed and records the calls so no test hits the network. `requires_aws` tests
auto-skip without AWS credentials (mirroring ingestion's convention).
"""

import boto3
import pytest
from fastapi.testclient import TestClient

from app import index_client
from app.config import get_settings


def _aws_credentials_available() -> bool:
    try:
        return boto3.Session().get_credentials() is not None
    except Exception:
        return False


def pytest_collection_modifyitems(config, items):
    if _aws_credentials_available():
        return
    skip_aws = pytest.mark.skip(reason="AWS credentials not available")
    for item in items:
        if "requires_aws" in item.keywords:
            item.add_marker(skip_aws)


class _IndexClientRecorder:
    """Records provision/delete calls in place of the real HTTP index client."""

    def __init__(self) -> None:
        self.provision_calls: list[str] = []
        self.delete_calls: list[str] = []
        self.delete_document_calls: list[tuple[str, str]] = []
        # When set, delete_document_chunks raises IndexServiceError(this, ...).
        self.delete_document_fail_status: int | None = None


@pytest.fixture(autouse=True)
def mock_index_client(monkeypatch) -> _IndexClientRecorder:
    """Stub ingestion's index lifecycle HTTP calls to succeed; record them.

    Every project create/delete now calls ingestion over HTTP; this keeps the
    suite hermetic. Tests that assert on the lifecycle request the recorder.
    """
    recorder = _IndexClientRecorder()

    def _provision(index_name: str, ingestion_url: str) -> bool:
        recorder.provision_calls.append(index_name)
        return True

    def _delete(index_name: str, ingestion_url: str) -> bool:
        recorder.delete_calls.append(index_name)
        return True

    def _delete_document_chunks(index_name: str, doc_id: str, ingestion_url: str) -> int:
        recorder.delete_document_calls.append((index_name, doc_id))
        if recorder.delete_document_fail_status is not None:
            raise index_client.IndexServiceError(
                recorder.delete_document_fail_status, "ingestion unreachable"
            )
        return 1

    monkeypatch.setattr(index_client, "provision", _provision)
    monkeypatch.setattr(index_client, "delete", _delete)
    monkeypatch.setattr(index_client, "delete_document_chunks", _delete_document_chunks)
    return recorder


@pytest.fixture
def settings_env(tmp_path, monkeypatch):
    """Point the BFF at a temp DB / upload dir and reset the settings cache."""
    monkeypatch.setenv("WEBUI_DB_PATH", str(tmp_path / "webui.db"))
    monkeypatch.setenv("WEBUI_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("WEBUI_UPLOAD_BUCKET", "")
    monkeypatch.setenv("WEBUI_INGESTION_URL", "http://ingestion.test")
    monkeypatch.setenv("WEBUI_CORS_ORIGINS", "http://localhost:3000")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client(settings_env):
    from app.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client
