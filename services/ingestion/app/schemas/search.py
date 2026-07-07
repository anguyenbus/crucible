"""Request/response models for POST /search (hybrid k-NN + BM25)."""

from pydantic import BaseModel


class SearchRequest(BaseModel):
    """Optional fields fall back to the service defaults in settings
    (top_k 5, knn/keyword weights 0.5/0.5)."""

    query: str
    top_k: int | None = None
    knn_weight: float | None = None
    keyword_weight: float | None = None


class SearchResult(BaseModel):
    doc_id: str
    source_uri: str
    chunk_index: int
    content: str
    score: float


class SearchResponse(BaseModel):
    results: list[SearchResult]
