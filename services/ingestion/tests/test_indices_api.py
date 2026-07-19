"""Task Group 5: index lifecycle endpoints (provision + delete), OpenSearch mocked.

Focused tests only (per task 5.1): provision creates the knn mapping + ensures
the hybrid-search pipeline; provision is idempotent; delete drops the index and
no-ops on a missing one; an invalid index name → 400 BEFORE any OpenSearch call.
Exhaustive name-permutation and pipeline-detail tests are skipped.
"""

import pytest
from fastapi.testclient import TestClient
from opensearchpy.exceptions import NotFoundError

from app.api import indices as indices_module
from app.main import create_app


class FakeOpenSearch:
    """Records index/pipeline mutations; missing pipeline raises NotFoundError."""

    def __init__(self, existing_indices=None, has_pipeline=False):
        self._existing = set(existing_indices or [])
        self._has_pipeline = has_pipeline
        self.created_bodies: dict[str, dict] = {}
        self.deleted: list[str] = []
        self.pipeline_puts: list[str] = []

        outer = self

        class _Indices:
            def exists(self, index):
                return index in outer._existing

            def create(self, index, body):
                outer.created_bodies[index] = body
                outer._existing.add(index)

            def delete(self, index):
                if index not in outer._existing:
                    raise NotFoundError(404, "index_not_found", {})
                outer._existing.discard(index)
                outer.deleted.append(index)

        class _Transport:
            def perform_request(self, method, path, body=None):
                if method == "GET":
                    if not outer._has_pipeline:
                        raise NotFoundError(404, "pipeline_not_found", {})
                    return {}
                if method == "PUT":
                    outer._has_pipeline = True
                    outer.pipeline_puts.append(path)
                    return {}

        self.indices = _Indices()
        self.transport = _Transport()


@pytest.fixture
def client():
    return TestClient(create_app())


def _install(monkeypatch, fake):
    monkeypatch.setattr(indices_module, "get_opensearch_client", lambda: fake)


def test_provision_creates_knn_mapping_and_ensures_pipeline(client, monkeypatch):
    fake = FakeOpenSearch()
    _install(monkeypatch, fake)

    response = client.post("/indices/proj-abc123")

    assert response.status_code == 200
    assert response.json() == {"index": "proj-abc123", "created": True}
    # Created with the knn_vector mapping (dim 1024).
    mapping = fake.created_bodies["proj-abc123"]["mappings"]["properties"]
    assert mapping["content_vector"]["type"] == "knn_vector"
    assert mapping["content_vector"]["dimension"] == 1024
    assert mapping["content"]["type"] == "text"
    assert mapping["doc_id"]["type"] == "keyword"
    # The named hybrid-search pipeline was ensured.
    assert any("hybrid-search-pipeline" in path for path in fake.pipeline_puts)


def test_provision_is_idempotent_for_an_existing_index(client, monkeypatch):
    fake = FakeOpenSearch(existing_indices=["proj-abc123"], has_pipeline=True)
    _install(monkeypatch, fake)

    response = client.post("/indices/proj-abc123")

    assert response.status_code == 200
    assert response.json() == {"index": "proj-abc123", "created": False}
    # No index (re)creation and no pipeline PUT — a clean no-op.
    assert fake.created_bodies == {}
    assert fake.pipeline_puts == []


def test_delete_drops_the_index_and_missing_index_is_a_benign_no_op(client, monkeypatch):
    fake = FakeOpenSearch(existing_indices=["proj-abc123"])
    _install(monkeypatch, fake)

    dropped = client.delete("/indices/proj-abc123")
    assert dropped.status_code == 200
    assert dropped.json() == {"index": "proj-abc123", "deleted": True}
    assert fake.deleted == ["proj-abc123"]

    # A second delete (index now absent) is an idempotent no-op, not an error.
    again = client.delete("/indices/proj-abc123")
    assert again.status_code == 200
    assert again.json() == {"index": "proj-abc123", "deleted": False}


def test_invalid_index_name_returns_400_before_any_opensearch_call(client, monkeypatch):
    fake = FakeOpenSearch()
    _install(monkeypatch, fake)

    # Uppercase is not an acceptable OpenSearch index name.
    response = client.post("/indices/Proj-ABC")
    assert response.status_code == 400
    # NO OpenSearch mutation happened.
    assert fake.created_bodies == {}
    assert fake.pipeline_puts == []
