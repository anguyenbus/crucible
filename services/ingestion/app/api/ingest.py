"""POST /ingest — synchronous ingestion pipeline endpoint.

Flow: fetch → normalize → sha256 → chunk (pure local tiktoken) →
max-chunks guard → dedup check → embed → bulk index (refresh=wait_for) →
prune stale chunks. Chunking runs before the dedup check because the check
needs the locally computed expected chunk count; it is cheap and makes no
AWS calls. The `max_chunks_per_doc` guard rejects oversized documents with a
clear 400 BEFORE any embedding call is made.

The OPTIONAL `index` on the request (project-scoped chat) targets a specific
OpenSearch index for the dedup count, write, AND prune. ABSENT ⇒ the
service-default single index (`settings.index_name`), byte-identical to today.

Error contract:
  400 — invalid/unsupported source string; document exceeds max_chunks_per_doc
  404 — S3 object or local file does not exist
  502 — Bedrock or OpenSearch upstream failure: safe, non-technical message,
        plus per-chunk `_bulk` failure details when a bulk write fails

There is no partial-failure recovery beyond reporting: deterministic chunk
`_id`s mean a retry simply overwrites the same documents (self-healing), so
no rollback logic exists anywhere.
"""

from fastapi import APIRouter, HTTPException
from opensearchpy.exceptions import OpenSearchException

from app.config import get_settings
from app.pipeline.chunk import chunk_text
from app.pipeline.embed import EmbeddingUpstreamError, embed_texts
from app.pipeline.fetch import (
    InvalidSourceError,
    SourceNotFoundError,
    fetch_bytes,
)
from app.pipeline.hash_dedup import content_sha256, derive_doc_id, is_complete_duplicate
from app.pipeline.index import BulkIndexError, index_chunks, prune_stale_chunks
from app.pipeline.normalize import normalize
from app.pipeline.parse import NoExtractableTextError, extract_text
from app.schemas.ingest import IngestRequest, IngestResponse

router = APIRouter()

_EMBED_UPSTREAM_MESSAGE = (
    "The embedding service is temporarily unavailable; please retry the ingest."
)
_INDEX_UPSTREAM_MESSAGE = (
    "The search index is temporarily unavailable; please retry the ingest."
)
_BULK_FAILURE_MESSAGE = (
    "Some chunks could not be indexed; please retry the ingest "
    "(a retry safely overwrites the same chunk IDs)."
)


@router.post("/ingest", response_model=IngestResponse)
def ingest(request: IngestRequest) -> IngestResponse:
    try:
        raw_bytes = fetch_bytes(request.source)
    except InvalidSourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SourceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    # Parse stage: dispatch by extension (.pdf via pypdf, .md/.markdown UTF-8).
    # A .pdf with no extractable native text (scanned / image-only) fails
    # honestly with a typed 422 — never a silent empty index.
    try:
        raw_text = extract_text(request.source, raw_bytes)
    except NoExtractableTextError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except InvalidSourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    text = normalize(raw_text)
    sha256 = content_sha256(text)
    doc_id = request.doc_id or derive_doc_id(request.source)
    chunks = chunk_text(text)

    settings = get_settings()
    if len(chunks) > settings.max_chunks_per_doc:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Document produces {len(chunks)} chunks, which exceeds the "
                f"limit of {settings.max_chunks_per_doc} (max_chunks_per_doc)."
            ),
        )

    try:
        if is_complete_duplicate(
            doc_id, sha256, expected_chunk_count=len(chunks), index=request.index
        ):
            return IngestResponse(
                doc_id=doc_id, sha256=sha256, chunks_indexed=0, skipped=True
            )
    except OpenSearchException as exc:
        raise _upstream_index_error() from exc

    try:
        vectors = embed_texts(chunks)
    except EmbeddingUpstreamError as exc:
        raise HTTPException(
            status_code=502, detail={"message": _EMBED_UPSTREAM_MESSAGE}
        ) from exc

    # Index-then-prune: the new version overwrites the same deterministic
    # `_id`s first; stale-sha256 leftovers are removed only after indexing
    # succeeds. NEVER delete before indexing — worst case must be
    # stale-but-searchable content, never a data-loss window.
    try:
        chunks_indexed = index_chunks(
            chunks,
            vectors,
            doc_id=doc_id,
            source_uri=request.source,
            sha256=sha256,
            index=request.index,
        )
    except BulkIndexError as exc:
        raise HTTPException(
            status_code=502,
            detail={"message": _BULK_FAILURE_MESSAGE, "failures": exc.failures},
        ) from exc
    except OpenSearchException as exc:
        raise _upstream_index_error() from exc

    try:
        prune_stale_chunks(doc_id, keep_sha256=sha256, index=request.index)
    except OpenSearchException as exc:
        raise _upstream_index_error() from exc

    return IngestResponse(
        doc_id=doc_id, sha256=sha256, chunks_indexed=chunks_indexed, skipped=False
    )


def _upstream_index_error() -> HTTPException:
    return HTTPException(status_code=502, detail={"message": _INDEX_UPSTREAM_MESSAGE})
