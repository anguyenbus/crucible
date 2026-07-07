"""Tests for the OpenSearch ingest script (mocked clients; no network)."""

from unittest.mock import MagicMock

from dev.scripts import ingest_gst_to_opensearch as ingest
from dev.stubs.rag.chunker import FixedChunker


def test_index_body_matches_contract():
    """Index mapping body carries the canary-validated k-NN contract + _meta."""
    body = ingest.build_index_body()

    assert body["settings"]["index"]["knn"] is True

    mappings = body["mappings"]
    assert mappings["_meta"] == {
        "embedder": "amazon.titan-embed-text-v2:0",
        "dims": 1024,
        "chunker": {"strategy": "fixed", "chunk_size": 512, "chunk_overlap": 0},
    }

    properties = mappings["properties"]
    assert properties["doc_id"] == {"type": "keyword"}
    assert properties["chunk_id"] == {"type": "keyword"}
    assert properties["text"] == {"type": "text"}

    embedding = properties["embedding"]
    assert embedding["type"] == "knn_vector"
    assert embedding["dimension"] == 1024
    assert embedding["method"] == {
        "name": "hnsw",
        "engine": "lucene",
        "space_type": "cosinesimil",
        "parameters": {"m": 16, "ef_construction": 100},
    }


def test_pipeline_body_min_max_arithmetic_mean():
    """Search pipeline body fuses via min_max normalization + arithmetic_mean."""
    body = ingest.build_pipeline_body()

    processors = body["phase_results_processors"]
    assert len(processors) == 1
    normalization_processor = processors[0]["normalization-processor"]
    assert normalization_processor["normalization"] == {"technique": "min_max"}
    assert normalization_processor["combination"] == {"technique": "arithmetic_mean"}


def test_bulk_actions_use_deterministic_chunk_ids():
    """_id == chunk_id == '{doc_id}:{chunk_idx}' for every bulk action."""
    documents = [{"id": "DOC_A", "text": "x" * 1030}, {"id": "DOC_B", "text": "short text"}]
    chunks = ingest.chunk_documents(documents, FixedChunker(512, 0))

    # 512-char chunking: DOC_A (1030 chars) -> 3 chunks, DOC_B -> 1 chunk.
    assert [c["chunk_id"] for c in chunks] == ["DOC_A:0", "DOC_A:1", "DOC_A:2", "DOC_B:0"]

    embeddings = [[0.1, 0.2]] * len(chunks)
    actions = ingest.build_bulk_actions(chunks, embeddings, "test-index")

    for action, chunk in zip(actions, chunks, strict=True):
        assert action["_index"] == "test-index"
        assert action["_id"] == chunk["chunk_id"]
        assert action["_source"]["chunk_id"] == chunk["chunk_id"]
        assert action["_source"]["doc_id"] == chunk["doc_id"]


def test_main_fails_when_index_exists_without_recreate(monkeypatch):
    """Exit code is non-zero when the index exists and --recreate is absent."""
    monkeypatch.setenv("EVAL_OPENSEARCH_ENDPOINT", "https://example.invalid")
    monkeypatch.setenv("EVAL_OPENSEARCH_INDEX", "already-there")

    client = MagicMock()
    client.indices.exists.return_value = True
    monkeypatch.setattr(ingest, "create_client", lambda endpoint: client)

    exit_code = ingest.main([])

    assert exit_code != 0
    client.indices.delete.assert_not_called()
    client.indices.create.assert_not_called()
