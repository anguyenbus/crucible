"""Task Group 5: bulk indexing — deterministic _ids, refresh, failure surfacing."""

import pytest

from app.pipeline.index import BulkIndexError, index_chunks

DOC_ID = "abc123def4567890"
CHUNKS = ["first chunk text", "second chunk text"]
VECTORS = [[0.1] * 1024, [0.2] * 1024]


class FakeOpenSearch:
    def __init__(self, response: dict):
        self.response = response
        self.bulk_calls: list[dict] = []

    def bulk(self, body=None, refresh=None, **kwargs) -> dict:
        self.bulk_calls.append({"body": body, "refresh": refresh})
        return self.response


def test_bulk_actions_use_deterministic_ids_and_wait_for_refresh():
    client = FakeOpenSearch({"errors": False, "items": []})

    indexed = index_chunks(
        CHUNKS,
        VECTORS,
        doc_id=DOC_ID,
        source_uri="s3://bucket/doc.md",
        sha256="feed" * 16,
        client=client,
    )

    assert indexed == 2
    call = client.bulk_calls[0]
    assert call["refresh"] == "wait_for"
    actions = call["body"]
    assert actions[0]["index"]["_id"] == f"{DOC_ID}:0"
    assert actions[2]["index"]["_id"] == f"{DOC_ID}:1"
    doc = actions[1]
    assert doc["content"] == CHUNKS[0]
    assert doc["content_vector"] == VECTORS[0]
    assert doc["doc_id"] == DOC_ID
    assert doc["source_uri"] == "s3://bucket/doc.md"
    assert doc["sha256"] == "feed" * 16
    assert doc["chunk_index"] == 0
    assert doc["created_at"]


def test_per_item_bulk_failures_are_collected_and_raised_with_details():
    client = FakeOpenSearch(
        {
            "errors": True,
            "items": [
                {"index": {"_id": f"{DOC_ID}:0", "status": 201}},
                {
                    "index": {
                        "_id": f"{DOC_ID}:1",
                        "status": 429,
                        "error": {"type": "circuit_breaking_exception", "reason": "too much load"},
                    }
                },
            ],
        }
    )

    with pytest.raises(BulkIndexError) as exc_info:
        index_chunks(
            CHUNKS,
            VECTORS,
            doc_id=DOC_ID,
            source_uri="s3://bucket/doc.md",
            sha256="feed" * 16,
            client=client,
        )

    failures = exc_info.value.failures
    assert failures == [
        {
            "_id": f"{DOC_ID}:1",
            "status": 429,
            "error": {"type": "circuit_breaking_exception", "reason": "too much load"},
        }
    ]
