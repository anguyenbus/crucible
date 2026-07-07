"""Idempotent creation of the ingestion index and hybrid search pipeline.

Creates, against the eval-poc OpenSearch domain:
  1. The chunk index (default `genai-ingestion-md`, configurable via
     `INGESTION_INDEX_NAME`): `index.knn: true`, HNSW `content_vector`
     (dim 1024, cosinesimil, engine faiss with automatic nmslib fallback),
     analyzed `content`, keyword/int/date metadata fields.
  2. The named `hybrid-search-pipeline` (min-max normalization-processor,
     default 0.5/0.5 weights).

Safe to re-run: existing index/pipeline are detected and skipped cleanly
(no-op), exit 0. Exits 1 only on a real failure.

Usage:
    uv run scripts/create_index.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from opensearchpy import OpenSearch
from opensearchpy.exceptions import NotFoundError, RequestError

from app.clients.opensearch import (
    build_index_body,
    build_search_pipeline_body,
    get_opensearch_client,
)
from app.config import get_settings


def ensure_index(client: OpenSearch, index_name: str) -> None:
    if client.indices.exists(index=index_name):
        print(f"index '{index_name}' already exists — skipping (no-op)")
        return
    try:
        client.indices.create(index=index_name, body=build_index_body(engine="faiss"))
        print(f"index '{index_name}' created (knn_vector engine: faiss)")
    except RequestError as exc:
        # Domain rejected the faiss HNSW method — retry once with nmslib.
        print(f"faiss engine rejected ({exc.error}); retrying with nmslib")
        client.indices.create(index=index_name, body=build_index_body(engine="nmslib"))
        print(f"index '{index_name}' created (knn_vector engine: nmslib)")


def ensure_search_pipeline(client: OpenSearch, pipeline_name: str) -> None:
    try:
        client.transport.perform_request("GET", f"/_search/pipeline/{pipeline_name}")
        print(f"search pipeline '{pipeline_name}' already exists — skipping (no-op)")
        return
    except NotFoundError:
        pass
    client.transport.perform_request(
        "PUT",
        f"/_search/pipeline/{pipeline_name}",
        body=build_search_pipeline_body(),
    )
    print(f"search pipeline '{pipeline_name}' created (min-max, weights 0.5/0.5)")


def main() -> int:
    settings = get_settings()
    try:
        client = get_opensearch_client()
        ensure_index(client, settings.index_name)
        ensure_search_pipeline(client, settings.search_pipeline_name)
    except Exception as exc:
        print(f"FAIL: {exc}")
        return 1
    print("RESULT: OK — index and search pipeline are in place")
    return 0


if __name__ == "__main__":
    sys.exit(main())
