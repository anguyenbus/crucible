"""SigV4-signed OpenSearch client plus index/search-pipeline body builders.

The client uses the boto3 credential chain (EC2 instance role on the POC
host, `es:ESHttp*` granted) via requests-aws4auth — no static keys. Host and
index/pipeline names come from settings.

The body builders are pure functions so provisioning (`scripts/create_index.py`)
and tests share one source of truth for the index mapping and the min-max
hybrid normalization pipeline.
"""

from functools import lru_cache

import boto3
from opensearchpy import OpenSearch, RequestsHttpConnection
from requests_aws4auth import AWS4Auth

from app.config import get_settings

# HNSW query-time beam width for the knn index (index.knn setting).
KNN_EF_SEARCH = 512


@lru_cache(maxsize=1)
def get_opensearch_client() -> OpenSearch:
    """SigV4-signed opensearch-py client for the eval-poc VPC domain."""
    settings = get_settings()
    credentials = boto3.Session(region_name=settings.aws_region).get_credentials()
    if credentials is None:
        raise RuntimeError(
            "No AWS credentials found in the boto3 credential chain "
            "(expected the EC2 instance role on this host)."
        )
    auth = AWS4Auth(
        region=settings.aws_region,
        service="es",
        refreshable_credentials=credentials,
    )
    return OpenSearch(
        hosts=[{"host": settings.opensearch_host, "port": 443}],
        http_auth=auth,
        use_ssl=True,
        verify_certs=True,
        connection_class=RequestsHttpConnection,
        timeout=30,
    )


def build_index_body(engine: str = "faiss") -> dict:
    """Index settings + mapping for the chunk index (see plan.md index design).

    `engine` is parameterized so create_index.py can fall back from faiss to
    nmslib if the domain rejects the faiss HNSW method.
    """
    settings = get_settings()
    return {
        "settings": {
            "index": {
                "knn": True,
                "knn.algo_param.ef_search": KNN_EF_SEARCH,
            }
        },
        "mappings": {
            "properties": {
                "content": {"type": "text"},
                "content_vector": {
                    "type": "knn_vector",
                    "dimension": settings.embed_dimensions,
                    "method": {
                        "name": "hnsw",
                        "space_type": "cosinesimil",
                        "engine": engine,
                    },
                },
                "doc_id": {"type": "keyword"},
                "source_uri": {"type": "keyword"},
                "sha256": {"type": "keyword"},
                "chunk_index": {"type": "integer"},
                "created_at": {"type": "date"},
            }
        },
    }


def build_search_pipeline_body(
    knn_weight: float | None = None, keyword_weight: float | None = None
) -> dict:
    """Min-max normalization-processor pipeline body for hybrid search.

    Weight order is [knn, keyword] and must match the sub-query order in the
    hybrid search request (knn first, BM25 match second). Defaults come from
    settings (0.5/0.5 — the named `hybrid-search-pipeline` carries these);
    explicit weights support the temporary inline pipeline for per-request
    weights in POST /search.
    """
    settings = get_settings()
    if knn_weight is None:
        knn_weight = settings.knn_weight
    if keyword_weight is None:
        keyword_weight = settings.keyword_weight
    return {
        "description": "Hybrid search min-max score normalization (knn + BM25)",
        "phase_results_processors": [
            {
                "normalization-processor": {
                    "normalization": {"technique": "min_max"},
                    "combination": {
                        "technique": "arithmetic_mean",
                        "parameters": {"weights": [knn_weight, keyword_weight]},
                    },
                }
            }
        ],
    }
