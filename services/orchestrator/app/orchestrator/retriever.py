"""
Retrieval stage: hybrid BM25 + k-NN retrieval per the BYO index contract v2.

Typed PURE functions: client INSTANCES arrive as arguments — this module
never imports boto3 or opensearch-py (``stages-pure`` import-linter
contract) and never reads the environment (grep-gate). Client duck types are
described with :class:`typing.Protocol` so no ``app.clients`` import is
needed either.

Embedding and search are DISTINCT steps (:func:`embed_query`, then
:func:`retrieve`) so the router can time and span them separately.

Division of inputs: ``top_k`` comes ONLY from the resolved pinned config;
index/pipeline/field NAMES are Settings-derived location facts; the search
pipeline is bound explicitly PER REQUEST as a query parameter (never
``index.search.default_pipeline``); ``_source`` excludes the vector field
(never haul 1024 floats per hit back over the wire).

Hit mapping (pure): ``chunk_id`` = hit ``_id`` verbatim
(``{doc_id}:{chunk_idx}``), ``rank`` = list position, ``score`` = raw
``_score`` pass-through, plus ``doc_id`` and text.
"""

from __future__ import annotations

from typing import Any, Protocol

from app.schemas.pipeline_config import PipelineConfig


class QueryEmbedder(Protocol):
    """Duck type of the injected Bedrock embedding client."""

    def embed_query(self, text: str, *, model_id: str) -> list[float]:
        """Embed one query text; returns the embedding vector."""
        ...


class HybridSearchClient(Protocol):
    """Duck type of the injected read-only OpenSearch client."""

    def search(self, body: dict[str, Any], *, search_pipeline: str) -> dict[str, Any]:
        """Run one search with the pipeline bound per request."""
        ...


class SearchLocationSettings(Protocol):
    """
    Location facts consumed by retrieval (BYO contract): names of things.

    Structurally matches ``app.config.Settings`` — a Protocol so this stage
    module needs no ``app.config`` import.
    """

    opensearch_pipeline: str
    opensearch_text_field: str
    opensearch_vector_field: str


def embed_query(
    question: str,
    config: PipelineConfig,
    *,
    embedder: QueryEmbedder,
) -> list[float]:
    """
    Embed the question with the config-pinned embedding model.

    A distinct step from :func:`retrieve` so the router can time/span
    ``embedding`` and ``retrieval`` separately.
    """
    return embedder.embed_query(question, model_id=config.embedder.model_id)


def build_query_body(
    question: str,
    query_vector: list[float],
    *,
    top_k: int,
    text_field: str,
    vector_field: str,
) -> dict[str, Any]:
    """
    Build the hybrid BM25 + k-NN query body (pure).

    Score fusion happens in the search pipeline bound per request at search
    time; ``_source`` excludes the vector field.
    """
    return {
        "size": top_k,
        "_source": {"excludes": [vector_field]},
        "query": {
            "hybrid": {
                "queries": [
                    {"match": {text_field: question}},
                    {"knn": {vector_field: {"vector": query_vector, "k": top_k}}},
                ]
            }
        },
    }


def hits_to_retrieved_chunks(
    hits: list[dict[str, Any]],
    *,
    text_field: str,
) -> list[dict[str, Any]]:
    """
    PURE mapping: OpenSearch hits → schema v1.1.0 ``retrieved_chunks``.

    Per hit (in given order): ``chunk_id`` = ``_id`` verbatim, ``rank`` =
    0-based position, ``score`` = raw ``_score`` pass-through (ordering-only
    meaningful), ``doc_id`` and the configured text field from ``_source``.
    """
    return [
        {
            "chunk_id": hit["_id"],
            "rank": rank,
            "score": hit["_score"],
            "doc_id": hit["_source"]["doc_id"],
            "text": hit["_source"][text_field],
        }
        for rank, hit in enumerate(hits)
    ]


def retrieve(
    question: str,
    query_vector: list[float],
    config: PipelineConfig,
    settings: SearchLocationSettings,
    *,
    search_client: HybridSearchClient,
) -> list[dict[str, Any]]:
    """
    Run the hybrid search and map hits to ranked ``retrieved_chunks``.

    Args:
        question: The (possibly rewritten) user question for the BM25 leg.
        query_vector: Titan V2 query embedding from :func:`embed_query`.
        config: Resolved pinned config — supplies ``top_k`` (behavior).
        settings: Location facts — pipeline and text/vector field names.
        search_client: Injected read-only OpenSearch client instance.

    Returns:
        Ranked chunk dicts per the schema mapping above.

    """
    body = build_query_body(
        question,
        query_vector,
        top_k=config.retrieval.top_k,
        text_field=settings.opensearch_text_field,
        vector_field=settings.opensearch_vector_field,
    )
    response = search_client.search(body, search_pipeline=settings.opensearch_pipeline)
    return hits_to_retrieved_chunks(
        response["hits"]["hits"], text_field=settings.opensearch_text_field
    )
