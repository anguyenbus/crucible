"""
AWS OpenSearch-backed RAG query function (provider-agnostic hybrid retrieval).

NOTE: This is a local-only dev/ retriever (never imported from app/). It queries
an EXTERNALLY OWNED OpenSearch index directly — eval never builds or mutates the
index on this path (the ingestion service owns it; see the fallback ingest
script for the BYO-reference of how to build a conforming index yourself).

Index contract v2 ("bring your own index")
------------------------------------------
A conforming external index needs ONLY the env vars below.
The full normative contract doc — provider checklist, POC worked example
(legal-rag-bench), Makefile lifecycle — lives at ``docs/byo-index-contract.md``
(repo root).

REQUIRED:
- ``doc_id``: keyword field.
- One analyzed text field for BM25 — name via ``EVAL_OPENSEARCH_TEXT_FIELD``
  (default ``text``; POC index: ``content``).
- One ``knn_vector`` field — name via ``EVAL_OPENSEARCH_VECTOR_FIELD``
  (default ``embedding``; POC index: ``content_vector``).
- Embeddings: 1024-dim ``cosinesimil`` Bedrock Titan Text Embeddings V2
  (``amazon.titan-embed-text-v2:0``, normalized). The query-side embedder must
  match; the k-NN engine may be ``lucene`` or ``faiss`` (transparent at query
  time).
- Deterministic ``_id`` = ``"{doc_id}:{chunk_idx}"`` — the retriever derives
  ``chunk_id`` from it (no ``chunk_id`` field required in the index).
- A ``min_max`` normalization + ``arithmetic_mean`` combination search
  pipeline — name via ``EVAL_OPENSEARCH_PIPELINE`` (POC:
  ``hybrid-search-pipeline``); passed EXPLICITLY at query time via the
  ``search_pipeline`` query parameter, never ``index.search.default_pipeline``.

OPTIONAL:
- ``chunk_id`` field (ignored; ``chunk_id`` is derived from ``_id``).
- ``char_span`` (``[start, end)`` offsets): emitted into eval output only when
  present in ``_source`` (schema v1.1.0 makes it optional).
- mapping ``_meta`` block (embedder model / dims): VERIFIED when present (loud
  failure on embedder mismatch), no-op when absent.

Score semantics: backend-native ``_score`` pass-through (hybrid =
pipeline-normalized fused values); ordering-only meaningful — NOT comparable
with ChromaDB's ``1 - distance``.

Config surface (env-only in Python; CloudFormation awareness lives in the
Makefile): ``EVAL_OPENSEARCH_ENDPOINT`` (required), ``EVAL_OPENSEARCH_INDEX``
(required), ``EVAL_OPENSEARCH_PIPELINE``, ``EVAL_OPENSEARCH_TEXT_FIELD``,
``EVAL_OPENSEARCH_VECTOR_FIELD``; SigV4 auth via the boto3 credential chain
with region from ``AWS_REGION``.

Documented stub-ism: the ``corpus_dir`` parameter of the RagAdapter callable is
accepted and IGNORED — retrieval hits the live index, not a local corpus.
"""

from __future__ import annotations

import hashlib
import os
import time
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from dev.stubs.rag.bedrock_embedder import (
    DEFAULT_EMBEDDING_DIM,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_REGION,
    BedrockTitanEmbedder,
)
from dev.stubs.rag.citations import extract_citations
from dev.stubs.rag.generator import DEFAULT_GENERATOR_MODEL, ClaudeGenerator
from dev.stubs.rag.schema_conformance import validate_rag_output

SCHEMA_VERSION: Final[str] = "1.1.0"
PIPELINE_VERSION: Final[str] = "0.1.0-opensearch"
RETRIEVER_VERSION: Final[str] = "hybrid-bm25-knn-0.1.0"
DEFAULT_TOP_K: Final[int] = 5

# Env-var surface (env-only; the Makefile owns CloudFormation endpoint discovery).
ENV_ENDPOINT: Final[str] = "EVAL_OPENSEARCH_ENDPOINT"
ENV_INDEX: Final[str] = "EVAL_OPENSEARCH_INDEX"
ENV_PIPELINE: Final[str] = "EVAL_OPENSEARCH_PIPELINE"
ENV_TEXT_FIELD: Final[str] = "EVAL_OPENSEARCH_TEXT_FIELD"
ENV_VECTOR_FIELD: Final[str] = "EVAL_OPENSEARCH_VECTOR_FIELD"

DEFAULT_PIPELINE: Final[str] = "hybrid-search-pipeline"
DEFAULT_TEXT_FIELD: Final[str] = "text"
DEFAULT_VECTOR_FIELD: Final[str] = "embedding"

# Global cached instances (created once, reused for all queries).
_cached_embedder: Any = None
_cached_generator: Any = None
_cached_client: Any = None
_cached_client_key: tuple[str, str] | None = None


@dataclass(frozen=True)
class OpenSearchConfig:
    """Resolved env-only retriever configuration."""

    endpoint: str
    index: str
    pipeline: str
    text_field: str
    vector_field: str


def load_config() -> OpenSearchConfig:
    """
    Resolve the retriever configuration from env vars only.

    Returns:
        OpenSearchConfig with endpoint/index (required) and pipeline/field
        names (defaulted).

    Raises:
        ValueError: If EVAL_OPENSEARCH_ENDPOINT or EVAL_OPENSEARCH_INDEX is
            unset (fail loud — never guess a live endpoint).

    """
    endpoint = os.getenv(ENV_ENDPOINT)
    if not endpoint:
        raise ValueError(
            f"{ENV_ENDPOINT} is not set. Set it to the OpenSearch domain endpoint "
            "(the Makefile can resolve it from CloudFormation: `make opensearch-endpoint`)."
        )
    index = os.getenv(ENV_INDEX)
    if not index:
        raise ValueError(
            f"{ENV_INDEX} is not set. Set it to the target index name "
            "(POC: legal-rag-bench)."
        )
    return OpenSearchConfig(
        endpoint=endpoint,
        index=index,
        pipeline=os.getenv(ENV_PIPELINE, DEFAULT_PIPELINE),
        text_field=os.getenv(ENV_TEXT_FIELD, DEFAULT_TEXT_FIELD),
        vector_field=os.getenv(ENV_VECTOR_FIELD, DEFAULT_VECTOR_FIELD),
    )


def build_query_body(
    question: str,
    query_vector: list[float],
    top_k: int,
    text_field: str,
    vector_field: str,
    mode: str = "hybrid",
) -> dict[str, Any]:
    """
    Build the OpenSearch query body (the ONE query-construction function).

    ``hybrid`` (the only spec'd eval path) fuses a BM25 ``match`` on the
    configured text field with a k-NN query on the configured vector field;
    score fusion happens in the search pipeline passed as a query parameter at
    search time. ``knn`` is a pure-knn DEBUG mode that bypasses the pipeline.

    Args:
        question: Query text for the BM25 leg.
        query_vector: Query embedding for the k-NN leg.
        top_k: Number of hits (both ``size`` and k-NN ``k``).
        text_field: Analyzed text field name (BM25 leg).
        vector_field: knn_vector field name (k-NN leg).
        mode: "hybrid" (default) or "knn" (debug, bypasses the pipeline).

    Returns:
        Query body dict; ``_source`` excludes the vector field (never haul
        1024 floats per hit back over the wire).

    Raises:
        ValueError: On an unknown mode.

    """
    knn_query = {"knn": {vector_field: {"vector": query_vector, "k": top_k}}}
    if mode == "hybrid":
        query: dict[str, Any] = {
            "hybrid": {
                "queries": [
                    {"match": {text_field: question}},
                    knn_query,
                ]
            }
        }
    elif mode == "knn":
        query = knn_query
    else:
        raise ValueError(f"Unknown query mode: {mode!r} (use 'hybrid' or 'knn')")

    return {
        "size": top_k,
        "_source": {"excludes": [vector_field]},
        "query": query,
    }


def hits_to_retrieved_chunks(
    hits: list[dict[str, Any]],
    text_field: str = DEFAULT_TEXT_FIELD,
) -> list[dict[str, Any]]:
    """
    PURE mapping: OpenSearch hits -> schema v1.1.0 ``retrieved_chunks``.

    No client/IO in the signature — this is the future app/ promotion seam
    (a move, not a rewrite).

    Mapping per hit (in given order):
    - ``chunk_id``: the hit ``_id`` (contract: ``"{doc_id}:{chunk_index}"``);
      no ``chunk_id`` field is required in the index.
    - ``doc_id``: ``_source.doc_id``.
    - ``text``: the configured text field from ``_source``.
    - ``rank``: 0-based position in the hit list.
    - ``score``: raw backend ``_score`` pass-through (ordering-only meaningful).
    - ``char_span``: included ONLY when present in ``_source`` (optional since
      schema v1.1.0).

    Args:
        hits: ``response["hits"]["hits"]`` from an OpenSearch search.
        text_field: Name of the analyzed text field in ``_source``.

    Returns:
        retrieved_chunks list conforming to rag_query_output.schema.json v1.1.0.

    """
    chunks: list[dict[str, Any]] = []
    for rank, hit in enumerate(hits):
        source = hit.get("_source", {})
        chunk: dict[str, Any] = {
            "chunk_id": hit["_id"],
            "rank": rank,
            "score": hit["_score"],
            "doc_id": source["doc_id"],
            "text": source[text_field],
        }
        if "char_span" in source:
            chunk["char_span"] = source["char_span"]
        chunks.append(chunk)
    return chunks


def verify_index_meta(
    mapping_response: dict[str, Any],
    embedder_model: str,
    embedder_dims: int,
) -> None:
    """
    Verify the index mapping ``_meta`` block against the query-side embedder.

    ``_meta`` is OPTIONAL in the index contract v2: when absent (as on the
    POC ``legal-rag-bench`` index) this is a NO-OP; when present, an embedder
    model or dimension mismatch FAILS LOUDLY — silently querying an index
    embedded with a different model would produce garbage retrieval scored as
    a real result.

    Args:
        mapping_response: ``client.indices.get_mapping(index=...)`` response
            (``{index_name: {"mappings": {...}}}``).
        embedder_model: Query-side embedder model id.
        embedder_dims: Query-side embedding dimension.

    Raises:
        ValueError: On embedder model or dimension mismatch.

    """
    for index_name, index_body in mapping_response.items():
        meta = index_body.get("mappings", {}).get("_meta")
        if not meta:
            continue  # No _meta declared -> nothing to verify (no-op).

        indexed_model = meta.get("embedder")
        if indexed_model is not None and indexed_model != embedder_model:
            raise ValueError(
                f"Embedder mismatch for index '{index_name}': index _meta declares "
                f"'{indexed_model}' but the query-side embedder is '{embedder_model}'. "
                "Querying with a mismatched embedder produces garbage retrieval; "
                "fix the embedder (or the index) before evaluating."
            )

        indexed_dims = meta.get("dims")
        if indexed_dims is not None and int(indexed_dims) != int(embedder_dims):
            raise ValueError(
                f"Embedding-dimension mismatch for index '{index_name}': index _meta "
                f"declares {indexed_dims} dims but the query-side embedder produces "
                f"{embedder_dims}."
            )


def create_client(endpoint: str) -> Any:
    """
    Create an OpenSearch client with SigV4 auth (boto3 credential chain).

    Args:
        endpoint: Domain endpoint (scheme optional).

    Returns:
        opensearchpy.OpenSearch client.

    """
    import boto3
    from opensearchpy import OpenSearch, Urllib3AWSV4SignerAuth, Urllib3HttpConnection

    host = endpoint.removeprefix("https://").removeprefix("http://").rstrip("/")
    region = os.getenv("AWS_REGION", DEFAULT_REGION)
    credentials = boto3.Session().get_credentials()
    auth = Urllib3AWSV4SignerAuth(credentials, region, "es")

    return OpenSearch(
        hosts=[{"host": host, "port": 443}],
        http_auth=auth,
        use_ssl=True,
        verify_certs=True,
        connection_class=Urllib3HttpConnection,
        timeout=60,
    )


def _embedder_identity(embedder: Any) -> tuple[str, int] | None:
    """
    Best-effort (model, dims) identity of the query-side embedder.

    BedrockTitanEmbedder exposes ``_model_id``/``_dimensions``. A foreign
    embedder without them has an UNKNOWN identity -> return None so the
    ``_meta`` guard skips (with a warning) instead of comparing against
    assumed values. Assuming Titan here made the guard pass silently for
    exactly the mismatched embedders it exists to catch (e.g. a 384-dim
    MiniLM embedder reporting as Titan/1024), and falsely reject matching
    non-Titan embedders.
    """
    model = getattr(embedder, "_model_id", None)
    dims = getattr(embedder, "_dimensions", None)
    if model is None or dims is None:
        return None
    return model, int(dims)


def _get_embedder(embedder: Any = None) -> Any:
    """Return the passed-in embedder, else a cached BedrockTitanEmbedder."""
    global _cached_embedder
    if embedder is not None:
        return embedder
    if _cached_embedder is None:
        _cached_embedder = BedrockTitanEmbedder()
    return _cached_embedder


def _get_generator() -> Any:
    """Get cached generator instance (lazy load, cached after first use)."""
    global _cached_generator
    if _cached_generator is None:
        _cached_generator = ClaudeGenerator()
    return _cached_generator


def _get_client(config: OpenSearchConfig, embedder: Any) -> Any:
    """
    Get a cached OpenSearch client, running the ``_meta`` guard once at init.

    The index mapping is fetched ONCE when the client is created for a given
    (endpoint, index); ``verify_index_meta`` fails loudly on an embedder
    mismatch and is a no-op when the index declares no ``_meta``.
    """
    global _cached_client, _cached_client_key
    key = (config.endpoint, config.index)
    if _cached_client is None or _cached_client_key != key:
        client = create_client(config.endpoint)
        mapping = client.indices.get_mapping(index=config.index)
        identity = _embedder_identity(embedder)
        if identity is not None:
            model, dims = identity
            verify_index_meta(mapping, embedder_model=model, embedder_dims=dims)
        else:
            warnings.warn(
                f"Embedder {type(embedder).__name__} exposes no "
                "_model_id/_dimensions; skipping index _meta verification. "
                "Ensure its embeddings match the index (see "
                "docs/byo-index-contract.md).",
                stacklevel=2,
            )
        _cached_client = client
        _cached_client_key = key
    return _cached_client


def query(
    question: str,
    corpus_dir: Path | None = None,
    top_k: int = DEFAULT_TOP_K,
    phoenix_trace_id: str | None = None,
    embedder: Any = None,
    mode: str = "hybrid",
) -> dict[str, Any]:
    """
    Query the OpenSearch-backed RAG system (hybrid BM25 + k-NN).

    Pipeline: embed the question (Bedrock Titan V2) -> hybrid search against
    the configured index with ``search_pipeline`` passed explicitly as a query
    parameter -> generate the answer via ClaudeGenerator -> extract citations
    -> validate against rag_query_output.schema.json v1.1.0.

    Args:
        question: User question to answer.
        corpus_dir: Accepted and IGNORED (documented stub-ism — the RagAdapter
            callable contract passes it, but retrieval hits the live index).
        top_k: Number of chunks to retrieve. Default: 5.
        phoenix_trace_id: Optional Phoenix trace ID for observability.
        embedder: Optional shared embedder instance (must match the index
            embeddings: Titan V2, 1024-dim). Default: internal cached
            BedrockTitanEmbedder.
        mode: "hybrid" (default) or "knn" (pure-knn debug, bypasses the
            search pipeline).

    Returns:
        Dictionary conforming to rag_query_output.schema.json v1.1.0.

    Raises:
        ValueError: On missing env config, embedder/_meta mismatch, or an
            unknown mode.
        SchemaValidationError: If output fails schema validation.

    """
    total_start = time.perf_counter()
    _ = corpus_dir  # Documented stub-ism: accepted and ignored.

    config = load_config()
    query_hash = hashlib.md5(question.encode()).hexdigest()[:8]
    query_id = f"opensearch_{query_hash}"

    resolved_embedder = _get_embedder(embedder)
    client = _get_client(config, resolved_embedder)
    generator = _get_generator()

    # Retrieval stage (embed + search).
    retrieval_start = time.perf_counter()
    query_vector = resolved_embedder.embed([question])[0]
    body = build_query_body(
        question=question,
        query_vector=query_vector,
        top_k=top_k,
        text_field=config.text_field,
        vector_field=config.vector_field,
        mode=mode,
    )
    # The pipeline is bound EXPLICITLY per request (never the index default);
    # the pure-knn debug mode bypasses it entirely.
    params = {"search_pipeline": config.pipeline} if mode == "hybrid" else None
    response = client.search(index=config.index, body=body, params=params)
    retrieved_chunks = hits_to_retrieved_chunks(
        response["hits"]["hits"], text_field=config.text_field
    )
    retrieval_ms = (time.perf_counter() - retrieval_start) * 1000

    # Generation stage.
    generation_start = time.perf_counter()
    answer_result = generator.generate(question, retrieved_chunks)
    generation_ms = (time.perf_counter() - generation_start) * 1000

    # KNOWN LIMITATION: extract_citations' pattern only matches legacy
    # "*_chunk_N" ids, never this contract's "{doc_id}:{chunk_idx}" ids, so
    # `answer.citations` is always [] on this backend. The markers Claude was
    # prompted to emit remain visible in answer.text; no metric consumes the
    # citations field. Documented in docs/byo-index-contract.md.
    citations = extract_citations(answer_result["text"], retrieved_chunks)

    identity = _embedder_identity(resolved_embedder)
    embedder_model = (
        identity[0] if identity is not None
        else f"unknown:{type(resolved_embedder).__name__}"
    )
    output: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "system_version": {
            "pipeline_version": PIPELINE_VERSION,
            "retriever_version": RETRIEVER_VERSION,
            "embedder_model": embedder_model,
            "generator_model": getattr(generator, "_model", DEFAULT_GENERATOR_MODEL),
        },
        "query": {
            "query_id": query_id,
            "text": question,
            "metadata": {},
        },
        "answer": {
            "text": answer_result["text"],
            "answer_supported": answer_result["answer_supported"],
            "citations": citations,
        },
        "retrieved_chunks": retrieved_chunks,
        "timings_ms": {
            "retrieval": retrieval_ms,
            "generation": generation_ms,
            "total": (time.perf_counter() - total_start) * 1000,
        },
    }

    if phoenix_trace_id:
        output["trace"] = {"trace_id": phoenix_trace_id}

    validate_rag_output(output)
    return output


# Explicit alias matching the spec's naming of the RagAdapter callable.
opensearch_query = query


def _run_smoke() -> int:
    """
    Manual operational smoke check (invoked via `make opensearch-smoke`).

    Runs ONE hardcoded legal-domain query end-to-end (embed -> hybrid search
    -> generate -> schema-validate), prints the ranked hits and the answer,
    and returns a non-zero exit code on any failure. This is the live check
    that replaces any pytest live test — errors are reported verbatim, never
    swallowed.
    """
    import traceback

    question = "When can a juror be excused from serving on a jury?"
    config = load_config()
    print(f"Index: {config.index} | pipeline: {config.pipeline} "
          f"| fields: {config.text_field}/{config.vector_field}")
    print(f"Query: {question}")

    try:
        result = query(question)
    except Exception:
        traceback.print_exc()
        print("SMOKE FAIL: end-to-end query raised (see traceback above)")
        return 1

    chunks = result["retrieved_chunks"]
    print(f"\nRetrieved {len(chunks)} chunks:")
    for chunk in chunks:
        preview = " ".join(chunk["text"].split())[:100]
        print(f"  [{chunk['rank']}] score={chunk['score']:.4f} "
              f"doc_id={chunk['doc_id']} | {preview}")

    print(f"\nAnswer (supported={result['answer']['answer_supported']}):")
    print(result["answer"]["text"])

    timings = result["timings_ms"]
    print(f"\nTimings ms: retrieval={timings['retrieval']:.0f} "
          f"generation={timings['generation']:.0f} total={timings['total']:.0f}")

    # query() already ran validate_rag_output (v1.1.0) before returning;
    # reaching here with hits means the schema-valid path worked end-to-end.
    if not chunks:
        print("SMOKE FAIL: schema-valid output but zero retrieved chunks")
        return 1
    print("SMOKE PASS: schema-valid v1.1.0 hybrid result with non-empty hits")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_run_smoke())
