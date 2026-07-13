"""Pure pipeline-stage tests (Group 3): fake client instances, zero network.

Focused checks only (per task 3.1) — exhaustive DSL and truncation
permutations are intentionally skipped.
"""

from dataclasses import dataclass

from app.config import resolve_pipeline_config
from app.orchestrator import guardrails, policy_router, query_rewrite, reranker
from app.orchestrator.citation_builder import build_citations
from app.orchestrator.context_assembler import assemble_context
from app.orchestrator.prompt_builder import build_prompt
from app.orchestrator.retriever import embed_query, hits_to_retrieved_chunks, retrieve

CONFIG_REF = "legal-rag-default-1.1.0"


@dataclass(frozen=True)
class FakeSettings:
    """Location facts only, mirroring app.config.Settings' field names."""

    opensearch_pipeline: str = "hybrid-search-pipeline"
    opensearch_text_field: str = "content"
    opensearch_vector_field: str = "content_vector"


class FakeEmbedder:
    def __init__(self):
        self.calls = []

    def embed_query(self, text, *, model_id):
        self.calls.append({"text": text, "model_id": model_id})
        return [0.1, 0.2, 0.3]


class FakeSearchClient:
    def __init__(self, hits):
        self.calls = []
        self._hits = hits

    def search(self, body, *, search_pipeline):
        self.calls.append({"body": body, "search_pipeline": search_pipeline})
        return {"hits": {"hits": self._hits}}


def _hit(doc_id: str, chunk_idx: int, score: float, text: str) -> dict:
    return {
        "_id": f"{doc_id}:{chunk_idx}",
        "_score": score,
        "_source": {"doc_id": doc_id, "content": text},
    }


def test_retriever_binds_pipeline_per_request_and_pins_come_from_the_right_places():
    config = resolve_pipeline_config(CONFIG_REF).config
    settings = FakeSettings()
    embedder = FakeEmbedder()
    search_client = FakeSearchClient([_hit("gst-act-1999", 0, 0.9, "Section 38-190 ...")])

    vector = embed_query("What is GST-free?", config, embedder=embedder)
    chunks = retrieve("What is GST-free?", vector, config, settings, search_client=search_client)

    # Embedding model comes from the config pin.
    assert embedder.calls == [
        {"text": "What is GST-free?", "model_id": "amazon.titan-embed-text-v2:0"}
    ]
    call = search_client.calls[0]
    # search_pipeline bound explicitly PER REQUEST as a query parameter.
    assert call["search_pipeline"] == "hybrid-search-pipeline"
    body = call["body"]
    # _source excludes the vector field; field names are Settings-derived.
    assert body["_source"] == {"excludes": ["content_vector"]}
    bm25_leg, knn_leg = body["query"]["hybrid"]["queries"]
    assert bm25_leg == {"match": {"content": "What is GST-free?"}}
    assert knn_leg["knn"]["content_vector"]["vector"] == vector
    # top_k comes ONLY from the resolved config (1.1.0 pins 8).
    assert body["size"] == 8
    assert knn_leg["knn"]["content_vector"]["k"] == 8
    assert len(chunks) == 1


def test_hits_map_to_retrieved_chunks_verbatim_id_rank_and_raw_score():
    hits = [
        _hit("actA", 4, 0.91, "first text"),
        _hit("actB", 0, 0.78, "second text"),
    ]

    chunks = hits_to_retrieved_chunks(hits, text_field="content")

    assert chunks == [
        {"chunk_id": "actA:4", "rank": 0, "score": 0.91, "doc_id": "actA", "text": "first text"},
        {"chunk_id": "actB:0", "rank": 1, "score": 0.78, "doc_id": "actB", "text": "second text"},
    ]


def test_context_assembler_rank_order_blocks_with_whole_chunk_tail_truncation():
    chunks = [
        {"chunk_id": "d:0", "text": "alpha alpha"},
        {"chunk_id": "d:1", "text": "beta beta"},
        {"chunk_id": "d:2", "text": "gamma gamma"},
    ]
    full = assemble_context(chunks, char_budget=10_000)
    assert full == "[d:0]: alpha alpha\n\n[d:1]: beta beta\n\n[d:2]: gamma gamma"

    # Budget fits the first two blocks but not the third: the third is
    # dropped WHOLE (no partial chunk), and nothing after it survives.
    two_blocks = "[d:0]: alpha alpha\n\n[d:1]: beta beta"
    truncated = assemble_context(chunks, char_budget=len(two_blocks))
    assert truncated == two_blocks
    assert "gamma" not in truncated

    # A chunk either fits entirely or is dropped: one char short of the
    # second block's fit drops the ENTIRE second block and everything after.
    only_first = assemble_context(chunks, char_budget=len(two_blocks) - 1)
    assert only_first == "[d:0]: alpha alpha"


def test_prompt_builder_renders_from_config_carried_template_text():
    config = resolve_pipeline_config(CONFIG_REF).config

    prompt = build_prompt(
        config.prompt_template.text,
        context="[d:0]: some legal text",
        question="When is a supply GST-free?",
    )

    assert "[d:0]: some legal text" in prompt
    assert "When is a supply GST-free?" in prompt
    assert "{context}" not in prompt
    assert "{question}" not in prompt
    # Literal braces/brackets in the template's citation instruction survive
    # (replace-based rendering, not str.format).
    assert "[doc_id:chunk_idx]" in prompt


def test_citation_builder_keeps_known_markers_drops_and_counts_unknowns():
    answer = "GST-free supply [gst-act-1999:4]. Unsupported claim [made-up-doc:9]."
    retrieved_ids = {"gst-act-1999:4", "gst-act-1999:5"}

    result = build_citations(answer, retrieved_ids)

    assert len(result.citations) == 1
    assert result.citations[0]["chunk_ids"] == ["gst-act-1999:4"]
    # Unknown-id markers are dropped from citations but COUNTED as data.
    assert result.dropped_unknown_marker_count == 1
    # Markers stay in answer.text (the builder never rewrites the answer).
    assert "[gst-act-1999:4]" in answer
    assert "[made-up-doc:9]" in answer


def test_claim_span_is_end_of_previous_marker_to_end_of_current_marker():
    first = "[actA:0]"
    second = "[actB:12]"
    answer = f"First claim {first}. Second claim {second}. Trailing text."
    first_end = answer.index(first) + len(first)
    second_end = answer.index(second) + len(second)

    result = build_citations(answer, {"actA:0", "actB:12"})

    assert result.citations == [
        {"claim_span": [0, first_end], "chunk_ids": ["actA:0"]},
        {"claim_span": [first_end, second_end], "chunk_ids": ["actB:12"]},
    ]
    assert result.dropped_unknown_marker_count == 0


def test_parked_stages_are_typed_identities_on_their_natural_types():
    question = "What is the applicable rate?"
    assert policy_router.route(question) is question
    assert query_rewrite.rewrite(question) is question

    chunks = [{"chunk_id": "d:0", "rank": 0, "score": 0.9, "doc_id": "d", "text": "t"}]
    assert reranker.rerank(chunks) is chunks

    answer = "answer text [d:0]"
    assert guardrails.check_input(question) is question
    assert guardrails.check_output(answer) is answer
