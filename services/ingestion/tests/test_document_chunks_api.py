"""Document-chunks read endpoint (OpenSearch mocked).

Focused tests: chunks come back in `chunk_index` order with the blank-line
`text` join; a missing index / no-hit doc is an honest 200 empty (NOT a 500);
an invalid index name is a 400 raised BEFORE any OpenSearch call.
"""

import pytest
from fastapi.testclient import TestClient
from opensearchpy.exceptions import NotFoundError

from app.api import document_chunks as document_chunks_module
from app.main import create_app


class FakeOpenSearch:
    """Returns canned search hits; a missing index raises NotFoundError."""

    def __init__(self, hits=None, *, index_missing=False):
        self._hits = hits or []
        self._index_missing = index_missing
        self.searched: list[dict] = []
        self.deleted_queries: list[dict] = []

    def search(self, index, body):
        self.searched.append({"index": index, "body": body})
        if self._index_missing:
            raise NotFoundError(404, "index_not_found_exception", {})
        return {"hits": {"hits": self._hits}}

    def delete_by_query(self, index, body, **kwargs):
        self.deleted_queries.append({"index": index, "body": body})
        if self._index_missing:
            raise NotFoundError(404, "index_not_found_exception", {})
        return {"deleted": len(self._hits)}


def _hit(chunk_index, content):
    return {
        "_id": f"doc-1:{chunk_index}",
        "_source": {
            "chunk_index": chunk_index,
            "content": content,
            "created_at": "2026-07-19T02:40:55Z",
        },
    }


@pytest.fixture
def client():
    return TestClient(create_app())


def _install(monkeypatch, fake):
    monkeypatch.setattr(document_chunks_module, "get_opensearch_client", lambda: fake)


def test_returns_ordered_chunks_and_blank_line_joined_text(client, monkeypatch):
    # The fake returns hits already ordered by chunk_index (OpenSearch sort).
    fake = FakeOpenSearch(
        hits=[_hit(0, "first chunk"), _hit(1, "second chunk"), _hit(2, "third")]
    )
    _install(monkeypatch, fake)

    response = client.get("/indices/proj-abc123/documents/doc-1/chunks")

    assert response.status_code == 200
    body = response.json()
    assert body["index"] == "proj-abc123"
    assert body["doc_id"] == "doc-1"
    assert body["chunk_count"] == 3
    assert body["text"] == "first chunk\n\nsecond chunk\n\nthird"
    assert [c["chunk_index"] for c in body["chunks"]] == [0, 1, 2]
    assert [c["id"] for c in body["chunks"]] == ["doc-1:0", "doc-1:1", "doc-1:2"]
    assert body["embedding_dims"] == 1024
    assert all(c["created_at"] == "2026-07-19T02:40:55Z" for c in body["chunks"])

    # The query filtered by doc_id and sorted by chunk_index asc.
    sent = fake.searched[0]["body"]
    assert sent["query"] == {"term": {"doc_id": "doc-1"}}
    assert sent["sort"] == [{"chunk_index": "asc"}]


def test_no_hits_returns_honest_empty_200(client, monkeypatch):
    fake = FakeOpenSearch(hits=[])
    _install(monkeypatch, fake)

    body = client.get("/indices/proj-abc123/documents/missing/chunks").json()

    assert body == {
        "index": "proj-abc123",
        "doc_id": "missing",
        "chunk_count": 0,
        "text": "",
        "chunks": [],
        "embedding_dims": 1024,
    }


def test_missing_index_is_empty_200_not_500(client, monkeypatch):
    fake = FakeOpenSearch(index_missing=True)
    _install(monkeypatch, fake)

    response = client.get("/indices/proj-neverindexed/documents/d/chunks")

    assert response.status_code == 200
    assert response.json()["chunk_count"] == 0


def test_invalid_index_name_returns_400_before_any_opensearch_call(client, monkeypatch):
    fake = FakeOpenSearch(hits=[_hit(0, "x")])
    _install(monkeypatch, fake)

    # Uppercase is not an acceptable OpenSearch index name.
    response = client.get("/indices/Proj-ABC/documents/d/chunks")

    assert response.status_code == 400
    # No OpenSearch query happened.
    assert fake.searched == []


def test_delete_document_chunks_deletes_by_query(client, monkeypatch):
    fake = FakeOpenSearch(hits=[_hit(0, "a"), _hit(1, "b")])
    _install(monkeypatch, fake)

    body = client.delete("/indices/proj-abc123/documents/doc-1").json()

    assert body == {"index": "proj-abc123", "doc_id": "doc-1", "deleted": 2}
    q = fake.deleted_queries[0]["body"]["query"]
    assert q == {"term": {"doc_id": "doc-1"}}


def test_delete_document_chunks_missing_index_is_200_zero(client, monkeypatch):
    _install(monkeypatch, FakeOpenSearch(index_missing=True))

    response = client.delete("/indices/proj-neverindexed/documents/d")

    assert response.status_code == 200
    assert response.json()["deleted"] == 0


def test_delete_document_chunks_invalid_index_name_400(client, monkeypatch):
    fake = FakeOpenSearch()
    _install(monkeypatch, fake)

    response = client.delete("/indices/Proj-ABC/documents/d")

    assert response.status_code == 400
    assert fake.deleted_queries == []
