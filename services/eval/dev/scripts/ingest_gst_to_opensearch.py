"""
Ingest the GST Legal RAG corpus into an AWS OpenSearch k-NN index.

Reads the vendored ``documents.jsonl``, chunks each passage with the existing
``FixedChunker`` (512 chars / 0 overlap), embeds chunks with Bedrock Titan
Text Embeddings V2, and bulk-indexes them into a lucene k-NN index with a
hybrid (BM25 + vector) search pipeline.

Index contract ("bring your own index")
----------------------------------------
Externally provided indexes work against the retriever as long as they
conform to this contract; only the three env vars below are needed.

NOTE: the NORMATIVE contract is v2 — see ``docs/byo-index-contract.md`` at
the repo root (and the retriever docstring in
``dev/stubs/rag/opensearch_query.py``). This script is the FALLBACK /
BYO-reference implementation only (NOT the POC ingest path — the ingestion
service owns the POC index): the index it builds conforms to v2 using the
DEFAULT field names (``text``/``embedding``), which is why only three env
vars are needed here. Under v2 the field names are configurable via
``EVAL_OPENSEARCH_TEXT_FIELD``/``EVAL_OPENSEARCH_VECTOR_FIELD``, and
``_meta``/``char_span``/``chunk_id`` are OPTIONAL — this script emits all
three; the POC index has none. Known limitation, documented not fixed: the
bulk 500-chunk batches trigger 429s on t3.small.search.

- Default index name: ``gst-legal-rag-titan1024-512-0``
- Fields: ``doc_id`` (keyword), ``chunk_id`` (keyword), ``char_span``
  (``[start, end)`` character offsets), ``text`` (analyzed text for BM25),
  ``embedding`` (``knn_vector``).
- k-NN params: 1024 dims, ``lucene`` engine, ``cosinesimil`` space, HNSW
  with library defaults ``m=16`` / ``ef_construction=100``.
- ``_meta`` block: embedder model (``amazon.titan-embed-text-v2:0``), dims
  (1024), chunker (fixed, chunk_size 512, overlap 0). The retriever verifies
  the embedder model when ``_meta`` is present.
- Document ``_id``: deterministic, ``_id == chunk_id == "{doc_id}:{chunk_idx}"``
  so reruns upsert instead of duplicating.
- Search pipeline: ``hybrid-minmax-mean`` (``min_max`` normalization +
  ``arithmetic_mean`` combination), created idempotently at setup and passed
  explicitly at query time via the ``search_pipeline`` query parameter.
- Score semantics: backend-native ``_score`` pass-through (hybrid =
  pipeline-normalized fused values); only ordering is meaningful.
- Config surface (env-only in Python; CloudFormation awareness lives in the
  Makefile): ``EVAL_OPENSEARCH_ENDPOINT``, ``EVAL_OPENSEARCH_INDEX``,
  ``EVAL_OPENSEARCH_PIPELINE``.

Lifecycle: ``make opensearch-up`` -> ``make opensearch-ingest`` -> eval runs
-> ``make opensearch-down`` (spend back to zero).

The script FAILS if the index already exists; pass ``--recreate`` (or
``RECREATE=1`` via the Makefile) to delete and rebuild. Auth is SigV4 via the
boto3 credential chain (instance role) against service ``es``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Final

from dev.stubs.rag.bedrock_embedder import (
    DEFAULT_EMBEDDING_DIM,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_REGION,
    BedrockTitanEmbedder,
)
from dev.stubs.rag.chunker import FixedChunker

DEFAULT_INDEX_NAME: Final[str] = "gst-legal-rag-titan1024-512-0"
PIPELINE_NAME: Final[str] = "hybrid-minmax-mean"
CHUNK_SIZE: Final[int] = 512
CHUNK_OVERLAP: Final[int] = 0
BULK_BATCH_SIZE: Final[int] = 500
DOCUMENTS_PATH: Final[Path] = Path("dev/fixtures/data/rag/gst_legal_rag/documents.jsonl")


class IndexExistsError(RuntimeError):
    """Raised when the target index exists and --recreate was not given."""


def build_index_body() -> dict:
    """
    Build the index creation body per the canary-validated contract.

    Single-node POC domain: 1 shard, 0 replicas. ``index.knn: true`` enables
    the k-NN plugin; the ``_meta`` block lets the retriever verify that the
    query-side embedder matches the index.
    """
    return {
        "settings": {
            "index": {
                "knn": True,
                "number_of_shards": 1,
                "number_of_replicas": 0,
            }
        },
        "mappings": {
            "_meta": {
                "embedder": DEFAULT_EMBEDDING_MODEL,
                "dims": DEFAULT_EMBEDDING_DIM,
                "chunker": {
                    "strategy": "fixed",
                    "chunk_size": CHUNK_SIZE,
                    "chunk_overlap": CHUNK_OVERLAP,
                },
            },
            "properties": {
                "doc_id": {"type": "keyword"},
                "chunk_id": {"type": "keyword"},
                "char_span": {"type": "integer"},
                "text": {"type": "text"},
                "embedding": {
                    "type": "knn_vector",
                    "dimension": DEFAULT_EMBEDDING_DIM,
                    "method": {
                        "name": "hnsw",
                        "engine": "lucene",
                        "space_type": "cosinesimil",
                        "parameters": {"m": 16, "ef_construction": 100},
                    },
                },
            },
        },
    }


def build_pipeline_body() -> dict:
    """Build the hybrid search-pipeline body (min_max + arithmetic_mean)."""
    return {
        "description": (
            "Hybrid BM25+kNN score fusion: min_max normalization, "
            "arithmetic_mean combination."
        ),
        "phase_results_processors": [
            {
                "normalization-processor": {
                    "normalization": {"technique": "min_max"},
                    "combination": {"technique": "arithmetic_mean"},
                }
            }
        ],
    }


def make_chunk_id(doc_id: str, chunk_idx: int) -> str:
    """Deterministic chunk/document id: ``{doc_id}:{chunk_idx}``."""
    return f"{doc_id}:{chunk_idx}"


def load_documents(documents_path: Path) -> list[dict]:
    """
    Load passages from documents.jsonl.

    Strict JSONL parsing, with ONE tolerated defect: a final line that is not
    newline-terminated and fails to parse is treated as an EOF-truncated tail
    record (the vendored fixture on this host is truncated at 512 KiB) and
    skipped with a loud warning. Any other bad line raises.

    Args:
        documents_path: Path to documents.jsonl.

    Returns:
        List of passage dicts with at least ``id`` and ``text``.

    Raises:
        FileNotFoundError: If documents.jsonl does not exist.
        ValueError: If an interior line is invalid JSON.

    """
    if not documents_path.exists():
        raise FileNotFoundError(f"documents.jsonl not found: {documents_path}")

    raw = documents_path.read_text(encoding="utf-8")
    lines = raw.split("\n")
    file_ends_with_newline = raw.endswith("\n")

    documents: list[dict] = []
    for line_no, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped:
            continue

        try:
            doc = json.loads(stripped)
        except json.JSONDecodeError as exc:
            is_final_line = line_no == len(lines)
            if is_final_line and not file_ends_with_newline:
                print(
                    f"WARNING: skipping EOF-truncated final record at line {line_no} of "
                    f"{documents_path} (file is not newline-terminated; the vendored corpus "
                    f"is truncated). Ingesting the {len(documents)} intact passages.",
                    file=sys.stderr,
                )
                break
            raise ValueError(f"Invalid JSON at line {line_no} of {documents_path}: {exc}") from exc

        if not doc.get("id") or not doc.get("text"):
            raise ValueError(f"Record at line {line_no} of {documents_path} lacks id/text")
        documents.append(doc)

    return documents


def chunk_documents(documents: list[dict], chunker: FixedChunker) -> list[dict]:
    """
    Chunk passages and assign contract chunk ids.

    The FixedChunker's own chunk_id format is replaced with the index
    contract's deterministic ``{doc_id}:{chunk_idx}``.

    Args:
        documents: Passage dicts with ``id`` and ``text``.
        chunker: FixedChunker instance (512/0).

    Returns:
        Chunk dicts with ``chunk_id``, ``doc_id``, ``text``, ``char_span``.

    """
    chunks: list[dict] = []
    for doc in documents:
        for chunk_idx, chunk in enumerate(chunker.chunk(doc_id=doc["id"], text=doc["text"])):
            chunks.append(
                {
                    "chunk_id": make_chunk_id(doc["id"], chunk_idx),
                    "doc_id": doc["id"],
                    "text": chunk["text"],
                    "char_span": chunk["char_span"],
                }
            )
    return chunks


def build_bulk_actions(
    chunks: list[dict],
    embeddings: list[list[float]],
    index_name: str,
) -> list[dict]:
    """
    Build opensearchpy.helpers.bulk actions with deterministic ``_id``s.

    Args:
        chunks: Chunk dicts from :func:`chunk_documents`.
        embeddings: One vector per chunk, same order.
        index_name: Target index.

    Returns:
        Bulk action dicts (``_id == chunk_id`` so reruns upsert).

    """
    return [
        {
            "_index": index_name,
            "_id": chunk["chunk_id"],
            "_source": {
                "doc_id": chunk["doc_id"],
                "chunk_id": chunk["chunk_id"],
                "char_span": chunk["char_span"],
                "text": chunk["text"],
                "embedding": embedding,
            },
        }
        for chunk, embedding in zip(chunks, embeddings, strict=True)
    ]


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


def ensure_index(client: Any, index_name: str, recreate: bool) -> None:
    """
    Create the index; fail if it exists unless ``recreate`` is set.

    Args:
        client: OpenSearch client.
        index_name: Target index name.
        recreate: Delete an existing index before creating.

    Raises:
        IndexExistsError: If the index exists and ``recreate`` is False.

    """
    if client.indices.exists(index=index_name):
        if not recreate:
            raise IndexExistsError(
                f"Index '{index_name}' already exists. Rerun with --recreate "
                f"(RECREATE=1 via make) to delete and rebuild it."
            )
        print(f"Deleting existing index '{index_name}' (--recreate)")
        client.indices.delete(index=index_name)

    print(f"Creating index '{index_name}'")
    client.indices.create(index=index_name, body=build_index_body())


def ensure_pipeline(client: Any, pipeline_name: str) -> None:
    """Create/overwrite the hybrid search pipeline (PUT is idempotent)."""
    print(f"Ensuring search pipeline '{pipeline_name}'")
    client.transport.perform_request(
        "PUT",
        f"/_search/pipeline/{pipeline_name}",
        body=build_pipeline_body(),
    )


def main(argv: list[str] | None = None) -> int:
    """Run the ingest; return a process exit code."""
    parser = argparse.ArgumentParser(description="Ingest GST corpus into OpenSearch")
    parser.add_argument(
        "--recreate",
        action="store_true",
        help="Delete and rebuild the index if it already exists",
    )
    args = parser.parse_args(argv)

    endpoint = os.getenv("EVAL_OPENSEARCH_ENDPOINT")
    if not endpoint:
        print("ERROR: EVAL_OPENSEARCH_ENDPOINT is not set", file=sys.stderr)
        return 1
    index_name = os.getenv("EVAL_OPENSEARCH_INDEX", DEFAULT_INDEX_NAME)

    started = time.perf_counter()
    client = create_client(endpoint)

    try:
        ensure_index(client, index_name, recreate=args.recreate)
    except IndexExistsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    ensure_pipeline(client, PIPELINE_NAME)

    documents = load_documents(DOCUMENTS_PATH)
    chunks = chunk_documents(documents, FixedChunker(CHUNK_SIZE, CHUNK_OVERLAP))
    print(f"Loaded {len(documents)} passages -> {len(chunks)} chunks")

    from opensearchpy import helpers

    embedder = BedrockTitanEmbedder()
    indexed_total = 0
    for batch_start in range(0, len(chunks), BULK_BATCH_SIZE):
        batch = chunks[batch_start : batch_start + BULK_BATCH_SIZE]
        embeddings = embedder.embed([chunk["text"] for chunk in batch])
        # helpers.bulk raises on the first item error (fail-fast, no retries).
        success_count, _ = helpers.bulk(
            client,
            build_bulk_actions(batch, embeddings, index_name),
            chunk_size=BULK_BATCH_SIZE,
        )
        indexed_total += success_count
        print(f"Indexed {indexed_total}/{len(chunks)} chunks")

    client.indices.refresh(index=index_name)
    doc_count = client.count(index=index_name)["count"]
    elapsed = time.perf_counter() - started

    if doc_count != len(chunks):
        print(
            f"ERROR: doc-count verification failed: index has {doc_count} docs, "
            f"expected {len(chunks)}",
            file=sys.stderr,
        )
        return 1

    print(
        f"OK: {len(documents)} passages, {len(chunks)} chunks indexed and verified in "
        f"'{index_name}' (pipeline '{PIPELINE_NAME}') in {elapsed:.1f}s"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
