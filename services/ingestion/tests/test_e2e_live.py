"""Task Group 9: live end-to-end workflows through the real FastAPI app.

These tests exercise the full ingest → search round trip against live
Bedrock + OpenSearch (no mocks), covering the workflows the mocked unit
tests cannot: real embedding, real bulk indexing with `refresh=wait_for`,
real dedup counts, real index-then-prune, and real hybrid scoring through
both the named and inline search pipelines.

All tests are marked `requires_aws` (skipped without credentials). Each test
ingests from a unique temp-file source — so its doc_id never collides with
real documents or earlier runs — and the `cleanup_doc_ids` fixture
delete-by-queries those doc_ids from the live index afterwards, keeping
validation runs repeatable.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.clients.opensearch import get_opensearch_client
from app.config import get_settings
from app.main import create_app
from app.pipeline.chunk import chunk_text
from app.pipeline.normalize import normalize

pytestmark = pytest.mark.requires_aws


@pytest.fixture
def client():
    return TestClient(create_app())


@pytest.fixture
def cleanup_doc_ids():
    """Collects doc_ids to purge from the live index after each test."""
    doc_ids: list[str] = []
    yield doc_ids
    if doc_ids:
        get_opensearch_client().delete_by_query(
            index=get_settings().index_name,
            body={"query": {"terms": {"doc_id": doc_ids}}},
            conflicts="proceed",
            refresh=True,
        )


def _unique_marker() -> str:
    """A nonsense token that only this test's document will BM25-match."""
    return f"zzmarker{uuid.uuid4().hex[:12]}"


def _count_chunks(doc_id: str) -> int:
    return get_opensearch_client().count(
        index=get_settings().index_name,
        body={"query": {"term": {"doc_id": doc_id}}},
    )["count"]


def test_ingest_local_file_then_search_round_trip(client, cleanup_doc_ids, tmp_path):
    marker = _unique_marker()
    path = tmp_path / "roundtrip.md"
    path.write_text(
        f"# Round trip\n\nThe phrase {marker} identifies this document "
        "about hybrid retrieval validation.\n"
    )

    ingest = client.post("/ingest", json={"source": str(path)})
    assert ingest.status_code == 200
    body = ingest.json()
    cleanup_doc_ids.append(body["doc_id"])
    assert body["skipped"] is False
    assert body["chunks_indexed"] > 0

    # Chunks live under the deterministic `_id = {doc_id}:{chunk_index}`.
    assert get_opensearch_client().exists(
        index=get_settings().index_name, id=f"{body['doc_id']}:0"
    )

    # Default weights (named hybrid-search-pipeline); refresh=wait_for means
    # the just-ingested chunk is immediately searchable.
    search = client.post("/search", json={"query": marker, "top_k": 10})
    assert search.status_code == 200
    results = search.json()["results"]
    ours = [r for r in results if r["doc_id"] == body["doc_id"]]
    assert ours, f"ingested doc not in search results: {results}"
    assert marker in ours[0]["content"]
    assert ours[0]["source_uri"] == str(path)
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)  # ranked, scored chunks


def test_reingest_unchanged_file_is_skipped(client, cleanup_doc_ids, tmp_path):
    path = tmp_path / "unchanged.md"
    path.write_text(f"# Unchanged\n\nStable content {_unique_marker()}.\n")

    first = client.post("/ingest", json={"source": str(path)}).json()
    cleanup_doc_ids.append(first["doc_id"])
    assert first["skipped"] is False

    second = client.post("/ingest", json={"source": str(path)})
    assert second.status_code == 200
    assert second.json() == {
        "doc_id": first["doc_id"],
        "sha256": first["sha256"],
        "chunks_indexed": 0,
        "skipped": True,
    }


def test_changed_content_reingest_prunes_stale_and_search_reflects_new(
    client, cleanup_doc_ids, tmp_path
):
    old_marker, new_marker = _unique_marker(), _unique_marker()
    path = tmp_path / "changed.md"

    # Version 1: long enough to produce at least 2 chunks (verified locally
    # with the same chunker the service uses).
    old_text = "# Version one\n\n" + " ".join(
        f"Sentence {i} discusses {old_marker} and retrieval pipelines." for i in range(130)
    )
    old_chunk_count = len(chunk_text(normalize(old_text)))
    assert old_chunk_count >= 2, "test setup: version 1 must span multiple chunks"
    path.write_text(old_text)

    first = client.post("/ingest", json={"source": str(path)}).json()
    cleanup_doc_ids.append(first["doc_id"])
    assert first["chunks_indexed"] == old_chunk_count
    assert _count_chunks(first["doc_id"]) == old_chunk_count

    # Version 2: shorter (single chunk) — trailing stale chunks must be pruned.
    path.write_text(f"# Version two\n\nOnly {new_marker} remains after the update.\n")
    second = client.post("/ingest", json={"source": str(path)}).json()
    assert second["doc_id"] == first["doc_id"]
    assert second["skipped"] is False
    assert second["chunks_indexed"] == 1
    assert second["sha256"] != first["sha256"]

    # Index-then-prune left exactly the new version's chunks — no stale
    # sha256 leftovers, no trailing chunk indexes.
    assert _count_chunks(first["doc_id"]) == 1

    # Search reflects only the new content for this doc_id.
    new_search = client.post("/search", json={"query": new_marker, "top_k": 10})
    new_ours = [
        r for r in new_search.json()["results"] if r["doc_id"] == first["doc_id"]
    ]
    assert new_ours and new_marker in new_ours[0]["content"]

    old_search = client.post("/search", json={"query": old_marker, "top_k": 10})
    assert not any(
        old_marker in r["content"]
        for r in old_search.json()["results"]
        if r["doc_id"] == first["doc_id"]
    )


def test_search_with_custom_weights_uses_inline_pipeline_live(
    client, cleanup_doc_ids, tmp_path
):
    marker = _unique_marker()
    path = tmp_path / "weighted.md"
    path.write_text(f"# Weighted\n\nCustom weight validation via {marker}.\n")

    ingested = client.post("/ingest", json={"source": str(path)}).json()
    cleanup_doc_ids.append(ingested["doc_id"])

    # Non-default weights → temporary inline search pipeline, executed live.
    search = client.post(
        "/search",
        json={"query": marker, "top_k": 10, "knn_weight": 0.7, "keyword_weight": 0.3},
    )
    assert search.status_code == 200
    results = search.json()["results"]
    ours = [r for r in results if r["doc_id"] == ingested["doc_id"]]
    assert ours, f"ingested doc not in custom-weight results: {results}"
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)
