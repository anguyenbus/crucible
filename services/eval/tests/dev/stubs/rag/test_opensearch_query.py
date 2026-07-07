"""Tests for the OpenSearch retriever (mocked clients; no live/network calls)."""

from unittest.mock import MagicMock

import pytest

from app.kernel.validation.schema_validator import SchemaValidationError
from dev.stubs.rag import opensearch_query as osq_export  # noqa: F401 (lazy-export smoke)
from dev.stubs.rag.schema_conformance import validate_rag_output

import dev.stubs.rag.opensearch_query as osq


def _hit(_id: str, score: float, source: dict) -> dict:
    """Build an OpenSearch hit dict in the response shape."""
    return {"_index": "legal-rag-bench", "_id": _id, "_score": score, "_source": source}


def _wrap_output(chunks: list[dict]) -> dict:
    """Wrap retrieved_chunks into a minimal full rag_query_output payload."""
    return {
        "schema_version": "1.1.0",
        "system_version": {"pipeline_version": osq.PIPELINE_VERSION},
        "query": {"query_id": "opensearch_abc12345", "text": "What is a lease?"},
        "answer": {"text": "An answer.", "citations": []},
        "retrieved_chunks": chunks,
    }


# ---------------------------------------------------------------------------
# Pure hit -> schema-dict mapping (the app/ promotion seam)
# ---------------------------------------------------------------------------


def test_mapping_without_char_span_is_schema_valid():
    """POC-shaped hits (no char_span, chunk_id derived from _id) validate v1.1.0."""
    hits = [
        _hit("PSG_1:0", 0.87, {"doc_id": "PSG_1", "content": "lease text", "chunk_index": 0}),
        _hit("PSG_2:1", 0.42, {"doc_id": "PSG_2", "content": "other text", "chunk_index": 1}),
    ]

    chunks = osq.hits_to_retrieved_chunks(hits, text_field="content")

    assert [c["chunk_id"] for c in chunks] == ["PSG_1:0", "PSG_2:1"]
    assert all("char_span" not in c for c in chunks)
    assert chunks[0]["doc_id"] == "PSG_1"
    assert chunks[0]["text"] == "lease text"
    # Full-output validation via validate_rag_output (schema v1.1.0).
    validate_rag_output(_wrap_output(chunks))


def test_mapping_includes_char_span_only_when_present():
    """char_span passes through from _source when the index provides offsets."""
    hits = [
        _hit("DOC_A:0", 0.9, {"doc_id": "DOC_A", "text": "spanful", "char_span": [0, 7]}),
        _hit("DOC_A:1", 0.5, {"doc_id": "DOC_A", "text": "spanless"}),
    ]

    chunks = osq.hits_to_retrieved_chunks(hits, text_field="text")

    assert chunks[0]["char_span"] == [0, 7]
    assert "char_span" not in chunks[1]
    validate_rag_output(_wrap_output(chunks))


def test_mapping_preserves_order_and_raw_scores():
    """Rank follows hit order; raw backend _score passes through unchanged."""
    scores = [0.93, 0.51, 0.07]
    hits = [
        _hit(f"D:{i}", score, {"doc_id": "D", "text": f"t{i}"})
        for i, score in enumerate(scores)
    ]

    chunks = osq.hits_to_retrieved_chunks(hits, text_field="text")

    assert [c["rank"] for c in chunks] == [0, 1, 2]
    assert [c["score"] for c in chunks] == scores


# ---------------------------------------------------------------------------
# Hybrid query-body construction + env-only field-name config
# ---------------------------------------------------------------------------


def test_query_body_default_field_names(monkeypatch):
    """Defaults: BM25 on 'text', k-NN on 'embedding'; _source excludes the vector."""
    monkeypatch.setenv("EVAL_OPENSEARCH_ENDPOINT", "https://example.invalid")
    monkeypatch.setenv("EVAL_OPENSEARCH_INDEX", "my-index")
    monkeypatch.delenv("EVAL_OPENSEARCH_PIPELINE", raising=False)
    monkeypatch.delenv("EVAL_OPENSEARCH_TEXT_FIELD", raising=False)
    monkeypatch.delenv("EVAL_OPENSEARCH_VECTOR_FIELD", raising=False)

    config = osq.load_config()
    assert config.pipeline == "hybrid-search-pipeline"

    vector = [0.1, 0.2, 0.3]
    body = osq.build_query_body(
        question="what is gst?",
        query_vector=vector,
        top_k=5,
        text_field=config.text_field,
        vector_field=config.vector_field,
    )

    assert body["size"] == 5
    assert body["_source"] == {"excludes": ["embedding"]}
    queries = body["query"]["hybrid"]["queries"]
    assert queries[0] == {"match": {"text": "what is gst?"}}
    assert queries[1] == {"knn": {"embedding": {"vector": vector, "k": 5}}}


def test_query_body_honors_env_field_overrides(monkeypatch):
    """POC override: content/content_vector via EVAL_OPENSEARCH_*_FIELD env vars."""
    monkeypatch.setenv("EVAL_OPENSEARCH_ENDPOINT", "https://example.invalid")
    monkeypatch.setenv("EVAL_OPENSEARCH_INDEX", "legal-rag-bench")
    monkeypatch.setenv("EVAL_OPENSEARCH_TEXT_FIELD", "content")
    monkeypatch.setenv("EVAL_OPENSEARCH_VECTOR_FIELD", "content_vector")

    config = osq.load_config()
    body = osq.build_query_body(
        question="q",
        query_vector=[0.5],
        top_k=3,
        text_field=config.text_field,
        vector_field=config.vector_field,
    )

    assert body["_source"] == {"excludes": ["content_vector"]}
    queries = body["query"]["hybrid"]["queries"]
    assert queries[0] == {"match": {"content": "q"}}
    assert queries[1] == {"knn": {"content_vector": {"vector": [0.5], "k": 3}}}


def test_query_body_pure_knn_debug_mode():
    """mode='knn' builds a bare k-NN body (no hybrid clause; pipeline bypassed)."""
    body = osq.build_query_body(
        question="q",
        query_vector=[0.5],
        top_k=2,
        text_field="text",
        vector_field="embedding",
        mode="knn",
    )

    assert "hybrid" not in body["query"]
    assert body["query"] == {"knn": {"embedding": {"vector": [0.5], "k": 2}}}


# ---------------------------------------------------------------------------
# _meta guard: loud on mismatch, no-op when absent
# ---------------------------------------------------------------------------


def test_meta_absent_is_noop():
    """An index without a _meta block (the POC index) verifies as a no-op."""
    mapping = {"legal-rag-bench": {"mappings": {"properties": {"doc_id": {"type": "keyword"}}}}}

    assert (
        osq.verify_index_meta(
            mapping,
            embedder_model="amazon.titan-embed-text-v2:0",
            embedder_dims=1024,
        )
        is None
    )


def test_meta_embedder_mismatch_fails_loudly():
    """A _meta block with a different embedder model raises a loud ValueError."""
    mapping = {
        "byo-index": {
            "mappings": {
                "_meta": {"embedder": "sentence-transformers/all-MiniLM-L6-v2", "dims": 384},
                "properties": {},
            }
        }
    }

    with pytest.raises(ValueError, match="[Ee]mbedder mismatch"):
        osq.verify_index_meta(
            mapping,
            embedder_model="amazon.titan-embed-text-v2:0",
            embedder_dims=1024,
        )


def test_meta_dims_mismatch_fails_loudly():
    """A _meta block with matching model but wrong dims raises a loud ValueError."""
    mapping = {
        "byo-index": {
            "mappings": {
                "_meta": {"embedder": "amazon.titan-embed-text-v2:0", "dims": 512},
                "properties": {},
            }
        }
    }

    with pytest.raises(ValueError, match="dimension mismatch"):
        osq.verify_index_meta(
            mapping,
            embedder_model="amazon.titan-embed-text-v2:0",
            embedder_dims=1024,
        )


# ---------------------------------------------------------------------------
# End-to-end query() with mocked client / embedder / generator
# ---------------------------------------------------------------------------


class _FakeEmbedder:
    """Titan-shaped fake: exposes the identity attrs the _meta guard reads."""

    _model_id = "amazon.titan-embed-text-v2:0"
    _dimensions = 1024

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3] for _ in texts]


def test_query_end_to_end_mocked(monkeypatch):
    """query() assembles a schema-valid v1.1.0 output with the pipeline bound per request."""
    monkeypatch.setenv("EVAL_OPENSEARCH_ENDPOINT", "https://example.invalid")
    monkeypatch.setenv("EVAL_OPENSEARCH_INDEX", "legal-rag-bench")
    monkeypatch.setenv("EVAL_OPENSEARCH_TEXT_FIELD", "content")
    monkeypatch.setenv("EVAL_OPENSEARCH_VECTOR_FIELD", "content_vector")
    monkeypatch.delenv("EVAL_OPENSEARCH_PIPELINE", raising=False)

    client = MagicMock()
    client.indices.get_mapping.return_value = {
        "legal-rag-bench": {"mappings": {"properties": {}}}
    }
    client.search.return_value = {
        "hits": {
            "hits": [
                _hit("PSG_9:0", 0.77, {"doc_id": "PSG_9", "content": "the gst rate is 10%"}),
                _hit("PSG_3:2", 0.31, {"doc_id": "PSG_3", "content": "unrelated passage"}),
            ]
        }
    }
    monkeypatch.setattr(osq, "create_client", lambda endpoint: client)
    # Reset the module-level client cache so the mocked client is picked up.
    monkeypatch.setattr(osq, "_cached_client", None)
    monkeypatch.setattr(osq, "_cached_client_key", None)

    generator = MagicMock()
    generator.generate.return_value = {"text": "GST is 10%.", "answer_supported": True}
    generator._model = "au.anthropic.claude-sonnet-4-6"
    monkeypatch.setattr(osq, "_get_generator", lambda: generator)

    output = osq.query("What is the GST rate?", corpus_dir=None, top_k=2, embedder=_FakeEmbedder())

    # The pipeline is bound explicitly per request (query param, never index default).
    _, search_kwargs = client.search.call_args
    assert search_kwargs["index"] == "legal-rag-bench"
    assert search_kwargs["params"] == {"search_pipeline": "hybrid-search-pipeline"}
    # The mapping was fetched once at client init (the _meta guard ran).
    client.indices.get_mapping.assert_called_once_with(index="legal-rag-bench")

    assert output["schema_version"] == "1.1.0"
    assert output["query"]["query_id"].startswith("opensearch_")
    assert [c["chunk_id"] for c in output["retrieved_chunks"]] == ["PSG_9:0", "PSG_3:2"]
    assert set(output["timings_ms"]) == {"retrieval", "generation", "total"}
    validate_rag_output(output)


# ---------------------------------------------------------------------------
# Schema v1.1.0 relaxation
# ---------------------------------------------------------------------------


def test_schema_v110_chunk_without_char_span_validates():
    """v1.1.0: a chunk lacking char_span is valid (char_span is now OPTIONAL)."""
    chunk = {"chunk_id": "D:0", "rank": 0, "score": 0.5, "doc_id": "D", "text": "t"}

    validate_rag_output(_wrap_output([chunk]))


def test_schema_v110_unknown_chunk_property_still_rejected():
    """additionalProperties stays false: an unknown chunk key fails validation."""
    chunk = {
        "chunk_id": "D:0",
        "rank": 0,
        "score": 0.5,
        "doc_id": "D",
        "text": "t",
        "embedding": [0.1, 0.2],  # vector must never leak into eval output
    }

    with pytest.raises(SchemaValidationError):
        validate_rag_output(_wrap_output([chunk]))


def test_schema_v110_old_version_string_rejected():
    """The schema_version const is bumped: '1.0.0' payloads no longer validate."""
    output = _wrap_output([])
    output["schema_version"] = "1.0.0"

    with pytest.raises(SchemaValidationError):
        validate_rag_output(output)


# ---------------------------------------------------------------------------
# _embedder_identity: unknown identity must NOT be assumed to be Titan
# ---------------------------------------------------------------------------


def test_embedder_identity_known_for_titan_like_embedder():
    """An embedder exposing _model_id/_dimensions reports its real identity."""

    class _TitanLike:
        _model_id = "amazon.titan-embed-text-v2:0"
        _dimensions = 1024

    assert osq._embedder_identity(_TitanLike()) == ("amazon.titan-embed-text-v2:0", 1024)


def test_embedder_identity_unknown_returns_none():
    """A foreign embedder without identity attrs is UNKNOWN (None), never
    assumed Titan — assuming defeated the _meta guard for exactly the
    mismatched embedders it exists to catch."""

    class _ForeignEmbedder:  # e.g. SentenceTransformersEmbedder shape
        _model_name = "sentence-transformers/all-MiniLM-L6-v2"

    assert osq._embedder_identity(_ForeignEmbedder()) is None
    assert osq._embedder_identity(object()) is None
