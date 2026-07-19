"""Task Group 6: POST /ingest — orchestration, dedup, update semantics, errors."""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.pipeline import run as run_module
from app.pipeline.fetch import InvalidSourceError, SourceNotFoundError
from app.pipeline.hash_dedup import content_sha256, derive_doc_id, is_complete_duplicate
from app.pipeline.index import BulkIndexError, prune_stale_chunks
from app.pipeline.normalize import normalize

SOURCE = "s3://atlas-demo-shared-s3-docs/guides/setup.md"
MARKDOWN = "# Title\n\nSome body text about hybrid search.\n"
EXPECTED_SHA = content_sha256(normalize(MARKDOWN))
EXPECTED_DOC_ID = derive_doc_id(SOURCE)


@pytest.fixture
def client():
    return TestClient(create_app())


@pytest.fixture
def pipeline_calls(monkeypatch):
    """Replace every pipeline stage with a recording fake (happy-path behavior)."""
    calls: list[tuple] = []

    def fake_fetch(source):
        calls.append(("fetch", source))
        return MARKDOWN.encode("utf-8")

    def fake_dedup(doc_id, sha256, expected_chunk_count, index=None):
        calls.append(("dedup", doc_id, sha256, expected_chunk_count, index))
        return False

    def fake_embed_iter(chunks):
        chunks = list(chunks)
        calls.append(("embed", chunks))
        for _ in chunks:
            yield [0.1] * 1024

    def fake_index(chunks, vectors, *, doc_id, source_uri, sha256, index=None):
        calls.append(("index", doc_id, sha256, index))
        return len(chunks)

    def fake_prune(doc_id, *, keep_sha256, index=None):
        calls.append(("prune", doc_id, keep_sha256, index))
        return 0

    monkeypatch.setattr(run_module, "fetch_bytes", fake_fetch)
    monkeypatch.setattr(run_module, "is_complete_duplicate", fake_dedup)
    monkeypatch.setattr(run_module, "embed_texts_iter", fake_embed_iter)
    monkeypatch.setattr(run_module, "index_chunks", fake_index)
    monkeypatch.setattr(run_module, "prune_stale_chunks", fake_prune)
    return calls


class FakeCountClient:
    def __init__(self, count: int):
        self.count_value = count
        self.count_calls: list[dict] = []

    def count(self, index=None, body=None):
        self.count_calls.append({"index": index, "body": body})
        return {"count": self.count_value}


class FakeDeleteClient:
    def __init__(self):
        self.delete_calls: list[dict] = []

    def delete_by_query(self, index=None, body=None, **kwargs):
        self.delete_calls.append({"index": index, "body": body, **kwargs})
        return {"deleted": 2}


def test_happy_path_returns_ids_counts_and_not_skipped(client, pipeline_calls):
    response = client.post("/ingest", json={"source": SOURCE})

    assert response.status_code == 200
    assert response.json() == {
        "doc_id": EXPECTED_DOC_ID,
        "sha256": EXPECTED_SHA,
        "chunks_indexed": 1,  # short doc -> single chunk
        "skipped": False,
    }
    # Dedup check received the locally computed expected chunk count.
    assert ("dedup", EXPECTED_DOC_ID, EXPECTED_SHA, 1, None) in pipeline_calls


def test_dedup_skip_requires_both_sha_match_and_complete_chunk_count():
    doc_id, sha = "a" * 16, "b" * 64

    # Complete prior ingest: existing count == expected -> duplicate (skip).
    complete = FakeCountClient(count=3)
    assert is_complete_duplicate(doc_id, sha, 3, client=complete) is True
    filters = complete.count_calls[0]["body"]["query"]["bool"]["filter"]
    assert {"term": {"doc_id": doc_id}} in filters
    assert {"term": {"sha256": sha}} in filters

    # Partial prior run: sha matches but count differs -> full re-ingest.
    assert is_complete_duplicate(doc_id, sha, 3, client=FakeCountClient(2)) is False


def test_unchanged_complete_doc_is_skipped_without_embedding(
    client, pipeline_calls, monkeypatch
):
    monkeypatch.setattr(
        run_module, "is_complete_duplicate", lambda *args, **kwargs: True
    )

    response = client.post("/ingest", json={"source": SOURCE})

    assert response.status_code == 200
    assert response.json() == {
        "doc_id": EXPECTED_DOC_ID,
        "sha256": EXPECTED_SHA,
        "chunks_indexed": 0,
        "skipped": True,
    }
    stages = [call[0] for call in pipeline_calls]
    assert "embed" not in stages
    assert "index" not in stages
    assert "prune" not in stages


def test_changed_content_indexes_new_chunks_before_pruning_stale(
    client, pipeline_calls
):
    response = client.post("/ingest", json={"source": SOURCE})

    assert response.status_code == 200
    stages = [call[0] for call in pipeline_calls]
    assert stages.index("index") < stages.index("prune")
    assert ("prune", EXPECTED_DOC_ID, EXPECTED_SHA, None) in pipeline_calls


def test_prune_deletes_only_same_doc_chunks_with_stale_sha():
    fake = FakeDeleteClient()

    deleted = prune_stale_chunks("doc1", keep_sha256="newsha", client=fake)

    assert deleted == 2
    query = fake.delete_calls[0]["body"]["query"]["bool"]
    assert query["filter"] == [{"term": {"doc_id": "doc1"}}]
    assert query["must_not"] == [{"term": {"sha256": "newsha"}}]


def test_invalid_source_returns_400_and_missing_source_returns_404(
    client, pipeline_calls, monkeypatch
):
    def raise_invalid(source):
        raise InvalidSourceError("unsupported source")

    def raise_missing(source):
        raise SourceNotFoundError("no such object")

    monkeypatch.setattr(run_module, "fetch_bytes", raise_invalid)
    assert client.post("/ingest", json={"source": "ftp://x"}).status_code == 400

    monkeypatch.setattr(run_module, "fetch_bytes", raise_missing)
    assert client.post("/ingest", json={"source": "s3://b/missing.md"}).status_code == 404


def test_over_max_chunks_returns_400_before_any_embedding(
    client, pipeline_calls, monkeypatch
):
    monkeypatch.setattr(run_module.get_settings(), "max_chunks_per_doc", 0)

    response = client.post("/ingest", json={"source": SOURCE})

    assert response.status_code == 400
    assert "max_chunks_per_doc" in response.json()["detail"]
    stages = [call[0] for call in pipeline_calls]
    assert "embed" not in stages
    assert "index" not in stages


def test_bulk_failure_returns_502_with_per_chunk_details_and_no_prune(
    client, pipeline_calls, monkeypatch
):
    failures = [
        {"_id": f"{EXPECTED_DOC_ID}:0", "status": 429, "error": {"type": "rejected"}}
    ]

    def raise_bulk_error(chunks, vectors, **kwargs):
        raise BulkIndexError("1 of 1 chunks failed to index", failures)

    monkeypatch.setattr(run_module, "index_chunks", raise_bulk_error)

    response = client.post("/ingest", json={"source": SOURCE})

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["failures"] == failures
    assert "retry" in detail["message"]  # safe, actionable message
    # NEVER prune when indexing did not succeed.
    assert "prune" not in [call[0] for call in pipeline_calls]
