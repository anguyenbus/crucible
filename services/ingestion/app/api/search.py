"""POST /search — hybrid (k-NN + BM25) retrieval endpoint.

The query is embedded through the same Titan v2 path as chunks (1024 dims),
then run as an OpenSearch `hybrid` query: a knn sub-query on `content_vector`
plus a BM25 `match` on `content`, with min-max score normalization.

Weight handling (verified live by Phase 0 `check_search_pipeline.py`):
  - Weights equal to the service defaults → the named `hybrid-search-pipeline`
    (referenced via the `search_pipeline` query parameter).
  - Per-request weights → a temporary (inline) search pipeline sent in the
    request body, because a named pipeline has fixed weights.

The sub-query order (knn first, match second) must match the [knn, keyword]
weight order used by the normalization pipeline. An empty index or a query
with no matches returns 200 with an empty `results` list — never an error.
"""

from fastapi import APIRouter, HTTPException
from opensearchpy.exceptions import NotFoundError, OpenSearchException

from app.clients.opensearch import build_search_pipeline_body, get_opensearch_client
from app.config import get_settings
from app.pipeline.embed import EmbeddingUpstreamError, embed_text
from app.schemas.search import SearchRequest, SearchResponse, SearchResult

router = APIRouter()

_EMBED_UPSTREAM_MESSAGE = (
    "The embedding service is temporarily unavailable; please retry the search."
)
_SEARCH_UPSTREAM_MESSAGE = (
    "The search index is temporarily unavailable; please retry the search."
)


@router.post("/search", response_model=SearchResponse)
def search(request: SearchRequest) -> SearchResponse:
    settings = get_settings()
    top_k = request.top_k if request.top_k is not None else settings.search_top_k
    knn_weight = (
        request.knn_weight if request.knn_weight is not None else settings.knn_weight
    )
    keyword_weight = (
        request.keyword_weight
        if request.keyword_weight is not None
        else settings.keyword_weight
    )

    try:
        query_vector = embed_text(request.query)
    except EmbeddingUpstreamError as exc:
        raise HTTPException(
            status_code=502, detail={"message": _EMBED_UPSTREAM_MESSAGE}
        ) from exc

    body = {
        "size": top_k,
        "_source": {"excludes": ["content_vector"]},
        "query": {
            "hybrid": {
                "queries": [
                    {"knn": {"content_vector": {"vector": query_vector, "k": top_k}}},
                    {"match": {"content": request.query}},
                ]
            }
        },
    }

    params = None
    if (knn_weight, keyword_weight) == (settings.knn_weight, settings.keyword_weight):
        params = {"search_pipeline": settings.search_pipeline_name}
    else:
        body["search_pipeline"] = build_search_pipeline_body(
            knn_weight, keyword_weight
        )

    try:
        response = get_opensearch_client().search(
            index=settings.index_name, body=body, params=params
        )
    except NotFoundError:
        return SearchResponse(results=[])  # index not created yet — not an error
    except OpenSearchException as exc:
        raise HTTPException(
            status_code=502, detail={"message": _SEARCH_UPSTREAM_MESSAGE}
        ) from exc

    results = [
        SearchResult(
            doc_id=hit["_source"]["doc_id"],
            source_uri=hit["_source"]["source_uri"],
            chunk_index=hit["_source"]["chunk_index"],
            content=hit["_source"]["content"],
            score=hit["_score"],
        )
        for hit in response.get("hits", {}).get("hits", [])
    ]
    return SearchResponse(results=results)
