"""Task Group 7: POST /search — hybrid retrieval, weight handling, mapping."""

import pytest
from fastapi.testclient import TestClient

from app.api import search as search_module
from app.main import create_app

QUERY_VECTOR = [0.5] * 1024

HITS = [
    {
        "_score": 0.91,
        "_source": {
            "doc_id": "d1",
            "source_uri": "s3://bucket/a.md",
            "chunk_index": 0,
            "content": "alpha chunk",
        },
    },
    {
        "_score": 0.42,
        "_source": {
            "doc_id": "d2",
            "source_uri": "/docs/b.md",
            "chunk_index": 3,
            "content": "beta chunk",
        },
    },
]


class FakeOpenSearch:
    def __init__(self, hits):
        self.hits = hits
        self.search_calls: list[dict] = []

    def search(self, index=None, body=None, params=None):
        self.search_calls.append({"index": index, "body": body, "params": params})
        return {"hits": {"hits": self.hits}}


@pytest.fixture
def client():
    return TestClient(create_app())


def make_fake_search(monkeypatch, hits) -> FakeOpenSearch:
    fake = FakeOpenSearch(hits)
    monkeypatch.setattr(search_module, "get_opensearch_client", lambda: fake)
    monkeypatch.setattr(search_module, "embed_text", lambda query: QUERY_VECTOR)
    return fake


def test_default_weights_use_named_hybrid_search_pipeline(client, monkeypatch):
    fake = make_fake_search(monkeypatch, HITS)

    response = client.post("/search", json={"query": "hybrid search"})

    assert response.status_code == 200
    call = fake.search_calls[0]
    assert call["params"] == {"search_pipeline": "hybrid-search-pipeline"}
    assert "search_pipeline" not in call["body"]
    # Hybrid query: knn on content_vector first, BM25 match second, size top_k.
    assert call["body"]["size"] == 5
    knn_query, match_query = call["body"]["query"]["hybrid"]["queries"]
    assert knn_query["knn"]["content_vector"]["vector"] == QUERY_VECTOR
    assert match_query["match"]["content"] == "hybrid search"


def test_non_default_weights_send_inline_pipeline_in_request_body(client, monkeypatch):
    fake = make_fake_search(monkeypatch, HITS)

    response = client.post(
        "/search",
        json={"query": "hybrid", "top_k": 2, "knn_weight": 0.7, "keyword_weight": 0.3},
    )

    assert response.status_code == 200
    call = fake.search_calls[0]
    assert call["params"] is None  # named pipeline not referenced
    inline = call["body"]["search_pipeline"]
    processor = inline["phase_results_processors"][0]["normalization-processor"]
    assert processor["normalization"] == {"technique": "min_max"}
    assert processor["combination"]["parameters"]["weights"] == [0.7, 0.3]
    assert call["body"]["size"] == 2


def test_no_matches_returns_200_with_empty_results(client, monkeypatch):
    make_fake_search(monkeypatch, hits=[])

    response = client.post("/search", json={"query": "nothing indexed yet"})

    assert response.status_code == 200
    assert response.json() == {"results": []}


def test_results_map_hit_fields_to_response_rows(client, monkeypatch):
    make_fake_search(monkeypatch, HITS)

    response = client.post("/search", json={"query": "hybrid"})

    assert response.status_code == 200
    assert response.json()["results"] == [
        {
            "doc_id": "d1",
            "source_uri": "s3://bucket/a.md",
            "chunk_index": 0,
            "content": "alpha chunk",
            "score": 0.91,
        },
        {
            "doc_id": "d2",
            "source_uri": "/docs/b.md",
            "chunk_index": 3,
            "content": "beta chunk",
            "score": 0.42,
        },
    ]
