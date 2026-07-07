"""Phase 0 check: hybrid search pipeline support on OpenSearch 2.19.

Verifies, in order:
  1. A named search pipeline with a min-max `normalization-processor`
     (arithmetic_mean combination, explicit weights) can be created and read
     back on the eval-poc domain (OpenSearch 2.19).
  2. A hybrid query (knn + match) executes through the NAMED pipeline
     (`?search_pipeline=<name>`) and returns normalized, scored hits.
  3. A TEMPORARY (inline) search pipeline defined inside the search request
     body works — required later so POST /search can honor per-request
     knn_weight/keyword_weight (a named pipeline has fixed weights).

All verification resources are temporary (index `phase0-check-hybrid-tmp`,
pipeline `phase0-check-minmax-pipeline`) and deleted afterwards; nothing
named for real use is touched.

Exits 0 on success, 1 on failure.

Usage:
    uv run scripts/check_search_pipeline.py
"""

import sys
import time

from _common import get_opensearch_client, print_result

TMP_INDEX = "phase0-check-hybrid-tmp"
TMP_PIPELINE = "phase0-check-minmax-pipeline"
VECTOR_DIM = 4

PIPELINE_BODY = {
    "description": "Phase 0 verification: min-max normalization for hybrid search",
    "phase_results_processors": [
        {
            "normalization-processor": {
                "normalization": {"technique": "min_max"},
                "combination": {
                    "technique": "arithmetic_mean",
                    "parameters": {"weights": [0.5, 0.5]},
                },
            }
        }
    ],
}

INDEX_BODY = {
    "settings": {
        "index": {"knn": True, "number_of_shards": 1, "number_of_replicas": 0}
    },
    "mappings": {
        "properties": {
            "content": {"type": "text"},
            "content_vector": {
                "type": "knn_vector",
                "dimension": VECTOR_DIM,
                "method": {
                    "name": "hnsw",
                    "space_type": "cosinesimil",
                    "engine": "faiss",
                },
            },
        }
    },
}

SAMPLE_DOCS = [
    {"content": "hybrid search combines vectors and keywords", "content_vector": [0.9, 0.1, 0.1, 0.1]},
    {"content": "markdown ingestion pipeline for opensearch", "content_vector": [0.1, 0.9, 0.1, 0.1]},
    {"content": "titan embeddings power semantic retrieval", "content_vector": [0.1, 0.1, 0.9, 0.1]},
]

HYBRID_QUERY = {
    "hybrid": {
        "queries": [
            {"knn": {"content_vector": {"vector": [0.8, 0.2, 0.1, 0.1], "k": 3}}},
            {"match": {"content": "hybrid search"}},
        ]
    }
}


def create_named_pipeline(client) -> bool:
    print(f"[1/3] create named min-max normalization pipeline '{TMP_PIPELINE}'")
    try:
        client.transport.perform_request(
            "PUT", f"/_search/pipeline/{TMP_PIPELINE}", body=PIPELINE_BODY
        )
        readback = client.transport.perform_request(
            "GET", f"/_search/pipeline/{TMP_PIPELINE}"
        )
    except Exception as exc:
        print(f"  FAIL: {exc}")
        return False
    if TMP_PIPELINE not in readback:
        print(f"  FAIL: pipeline not present on read-back: {readback}")
        return False
    print("  OK: pipeline created and read back")
    return True


def seed_temp_index(client) -> None:
    client.indices.create(index=TMP_INDEX, body=INDEX_BODY)
    for i, doc in enumerate(SAMPLE_DOCS):
        client.index(index=TMP_INDEX, id=str(i), body=doc)
    client.indices.refresh(index=TMP_INDEX)


def assert_scored_hits(response: dict, label: str) -> bool:
    hits = response.get("hits", {}).get("hits", [])
    if not hits:
        print(f"  FAIL: {label} returned no hits")
        return False
    scores = [h.get("_score") for h in hits]
    if any(s is None for s in scores):
        print(f"  FAIL: {label} returned unscored hits")
        return False
    print(f"  OK: {label} returned {len(hits)} scored hits "
          f"(top score {scores[0]:.4f})")
    return True


def check_named_pipeline_search(client) -> bool:
    print("[2/3] hybrid query through the NAMED pipeline")
    try:
        response = client.search(
            index=TMP_INDEX,
            body={"query": HYBRID_QUERY, "size": 3},
            params={"search_pipeline": TMP_PIPELINE},
        )
    except Exception as exc:
        print(f"  FAIL: {exc}")
        return False
    return assert_scored_hits(response, "named-pipeline search")


def check_inline_pipeline_search(client) -> bool:
    print("[3/3] hybrid query with a TEMPORARY (inline) pipeline in the "
          "request body (per-request weights)")
    inline_pipeline = {
        "description": "temporary per-request weights",
        "phase_results_processors": [
            {
                "normalization-processor": {
                    "normalization": {"technique": "min_max"},
                    "combination": {
                        "technique": "arithmetic_mean",
                        "parameters": {"weights": [0.7, 0.3]},
                    },
                }
            }
        ],
    }
    try:
        response = client.search(
            index=TMP_INDEX,
            body={
                "query": HYBRID_QUERY,
                "size": 3,
                "search_pipeline": inline_pipeline,
            },
        )
    except Exception as exc:
        print(f"  FAIL: {exc}")
        return False
    return assert_scored_hits(response, "inline-pipeline search")


def cleanup(client) -> None:
    for method, path in (
        ("DELETE", f"/{TMP_INDEX}"),
        ("DELETE", f"/_search/pipeline/{TMP_PIPELINE}"),
    ):
        try:
            client.transport.perform_request(method, path)
        except Exception:
            pass  # temp resource may not exist if an earlier step failed


def main() -> int:
    client = get_opensearch_client()
    passed = False
    try:
        passed = create_named_pipeline(client)
        if passed:
            seed_temp_index(client)
            time.sleep(1)  # let the tiny temp index settle before querying
            passed = check_named_pipeline_search(client)
            passed = check_inline_pipeline_search(client) and passed
    except Exception as exc:
        print(f"  FAIL: {exc}")
        passed = False
    finally:
        cleanup(client)
    return print_result(passed, "check_search_pipeline")


if __name__ == "__main__":
    sys.exit(main())
