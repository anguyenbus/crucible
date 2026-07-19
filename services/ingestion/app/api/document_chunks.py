"""Read a single document's indexed chunks/text (document-detail view).

`GET /indices/{index_name}/documents/{doc_id}/chunks` returns the ACTUAL chunk
`content` ingestion stored for one `doc_id`, in `chunk_index` order, plus those
chunks joined into a single `text` field. The BFF calls this over HTTP (it holds
no OpenSearch client) to render the extracted-text pane of the document detail.

Error contract (mirrors indices.py):
  400 — the caller-supplied index name is not an acceptable OpenSearch name
        (raised BEFORE any OpenSearch call)
  502 — OpenSearch upstream failure (safe, non-technical message)
A MISSING index or a doc with no chunks is NOT an error: it returns 200 with
`chunk_count: 0`, empty `chunks`, and empty `text` (honest empty state).
"""

from fastapi import APIRouter, HTTPException
from opensearchpy.exceptions import NotFoundError, OpenSearchException

from app.clients.index_admin import InvalidIndexNameError, validate_index_name
from app.clients.opensearch import get_opensearch_client
from app.config import get_settings
from app.schemas.document_chunks import (
    DeleteDocumentChunksResponse,
    DocumentChunk,
    DocumentChunksResponse,
)

router = APIRouter()

_UPSTREAM_MESSAGE = "The search index is temporarily unavailable; please retry."

# Cap the chunks fetched for one document. Ingestion bounds a document at
# max_chunks_per_doc=100, so 1000 is generous headroom with no unbounded scan.
_MAX_CHUNKS = 1000


@router.get(
    "/indices/{index_name}/documents/{doc_id}/chunks",
    response_model=DocumentChunksResponse,
)
def get_document_chunks(index_name: str, doc_id: str) -> DocumentChunksResponse:
    """Return one document's chunks in order plus the blank-line-joined text."""
    try:
        validate_index_name(index_name)
    except InvalidIndexNameError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # embedding_dims is the vector length every chunk carries; read from config
    # (constant per index), NOT by fetching each chunk's 1024-float vector.
    embedding_dims = get_settings().embed_dimensions

    body = {
        "size": _MAX_CHUNKS,
        "query": {"term": {"doc_id": doc_id}},
        "sort": [{"chunk_index": "asc"}],
        "_source": ["chunk_index", "content", "created_at"],
    }
    try:
        response = get_opensearch_client().search(index=index_name, body=body)
    except NotFoundError:
        # Index not created yet / never ingested — an honest empty result.
        return DocumentChunksResponse(
            index=index_name,
            doc_id=doc_id,
            chunk_count=0,
            text="",
            chunks=[],
            embedding_dims=embedding_dims,
        )
    except OpenSearchException as exc:
        raise HTTPException(
            status_code=502, detail={"message": _UPSTREAM_MESSAGE}
        ) from exc

    chunks = [
        DocumentChunk(
            id=hit["_id"],
            chunk_index=hit["_source"]["chunk_index"],
            content=hit["_source"]["content"],
            created_at=hit["_source"].get("created_at"),
        )
        for hit in response.get("hits", {}).get("hits", [])
    ]
    text = "\n\n".join(chunk.content for chunk in chunks)
    return DocumentChunksResponse(
        index=index_name,
        doc_id=doc_id,
        chunk_count=len(chunks),
        text=text,
        chunks=chunks,
        embedding_dims=embedding_dims,
    )


@router.delete(
    "/indices/{index_name}/documents/{doc_id}",
    response_model=DeleteDocumentChunksResponse,
)
def delete_document_chunks(index_name: str, doc_id: str) -> DeleteDocumentChunksResponse:
    """Delete ALL chunks of `doc_id` from `index_name` (delete-by-query).

    Idempotent: a missing index or already-absent document → `deleted == 0`, 200.
    Called by the BFF when a document is deleted so the project index stays in
    sync with the document list (chunks no longer retrievable / citable).
    """
    try:
        validate_index_name(index_name)
    except InvalidIndexNameError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        response = get_opensearch_client().delete_by_query(
            index=index_name,
            body={"query": {"term": {"doc_id": doc_id}}},
            conflicts="proceed",
            refresh=True,
        )
    except NotFoundError:
        # Index never created / already gone — nothing to delete.
        return DeleteDocumentChunksResponse(index=index_name, doc_id=doc_id, deleted=0)
    except OpenSearchException as exc:
        raise HTTPException(
            status_code=502, detail={"message": _UPSTREAM_MESSAGE}
        ) from exc

    return DeleteDocumentChunksResponse(
        index=index_name, doc_id=doc_id, deleted=response.get("deleted", 0)
    )
