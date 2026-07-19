"""The ingestion pipeline as a phase-emitting generator (one source of truth).

`run_ingest` drives the exact same linear pipeline the `/ingest` handler used to
run inline — fetch → parse → normalize → hash → chunk → max-chunks guard →
dedup check → embed → index → prune — but yields a `PhaseEvent` at each natural
boundary and a final `ResultEvent`, so BOTH entry points share it:

  - `POST /ingest` drains the generator, ignores the phase events, and returns
    the terminal result (behaviour byte-identical to before).
  - `POST /ingest/stream` forwards each event to the client as SSE for live
    per-phase progress (the embedding phase reports `current`/`total`).

Error mapping lives HERE, once: every typed pipeline failure is raised as a
`PipelineError(status_code, detail)`, so `/ingest` re-raises it as an
`HTTPException` and `/ingest/stream` emits it as a terminal `error` event —
neither route duplicates the status-code logic.

The four caller-facing phases map to the pipeline steps as: parsing =
fetch+parse, chunking = normalize+hash+chunk+dedup-check, embedding = the
per-chunk Titan loop, indexing = bulk index + prune-stale.
"""

from collections.abc import Iterator
from dataclasses import dataclass

from opensearchpy.exceptions import OpenSearchException

from app.config import get_settings
from app.pipeline.chunk import chunk_text
from app.pipeline.embed import EmbeddingUpstreamError, embed_texts_iter
from app.pipeline.fetch import InvalidSourceError, SourceNotFoundError, fetch_bytes
from app.pipeline.hash_dedup import (
    content_sha256,
    derive_doc_id,
    is_complete_duplicate,
)
from app.pipeline.index import BulkIndexError, index_chunks, prune_stale_chunks
from app.pipeline.normalize import normalize
from app.pipeline.parse import NoExtractableTextError, extract_text
from app.schemas.ingest import IngestRequest

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


@dataclass(frozen=True)
class PhaseEvent:
    """One progress checkpoint. `current`/`total` are set only for embedding."""

    phase: str  # "parsing" | "chunking" | "embedding" | "indexing"
    current: int | None = None
    total: int | None = None


@dataclass(frozen=True)
class ResultEvent:
    """The terminal success event — the same fields as `IngestResponse`."""

    doc_id: str
    sha256: str
    chunks_indexed: int
    skipped: bool


class PipelineError(Exception):
    """A typed pipeline failure carrying its HTTP-equivalent status + detail.

    `detail` is a plain message string, or the same structured dict the old
    handler returned for upstream/bulk failures (`{"message": ...}` /
    `{"message": ..., "failures": ...}`).
    """

    def __init__(self, status_code: int, detail: object):
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


def run_ingest(request: IngestRequest) -> Iterator[PhaseEvent | ResultEvent]:
    """Run the pipeline, yielding a `PhaseEvent` per stage then a `ResultEvent`.

    Raises `PipelineError` on any typed failure (the single place statuses are
    assigned). A complete-duplicate short-circuits to a `skipped` result after
    the chunking phase, before any embedding call — exactly as before.
    """
    # --- parsing: fetch bytes, then extract text (pdf via pypdf, md UTF-8) ---
    yield PhaseEvent("parsing")
    try:
        raw_bytes = fetch_bytes(request.source)
    except InvalidSourceError as exc:
        raise PipelineError(400, str(exc)) from exc
    except SourceNotFoundError as exc:
        raise PipelineError(404, str(exc)) from exc
    try:
        raw_text = extract_text(request.source, raw_bytes)
    except NoExtractableTextError as exc:
        raise PipelineError(422, str(exc)) from exc
    except InvalidSourceError as exc:
        raise PipelineError(400, str(exc)) from exc

    # --- chunking: normalize → sha256 → doc_id → chunk → max-chunks guard ---
    yield PhaseEvent("chunking")
    text = normalize(raw_text)
    sha256 = content_sha256(text)
    doc_id = request.doc_id or derive_doc_id(request.source)
    chunks = chunk_text(text)

    settings = get_settings()
    if len(chunks) > settings.max_chunks_per_doc:
        raise PipelineError(
            400,
            f"Document produces {len(chunks)} chunks, which exceeds the "
            f"limit of {settings.max_chunks_per_doc} (max_chunks_per_doc).",
        )

    # Dedup check runs in the chunking phase (needs the local expected count);
    # an identical prior ingest short-circuits to a skip, no AWS embed calls.
    try:
        if is_complete_duplicate(
            doc_id, sha256, expected_chunk_count=len(chunks), index=request.index
        ):
            yield ResultEvent(
                doc_id=doc_id, sha256=sha256, chunks_indexed=0, skipped=True
            )
            return
    except OpenSearchException as exc:
        raise PipelineError(502, {"message": _INDEX_UPSTREAM_MESSAGE}) from exc

    # --- embedding: strictly sequential Titan calls, one PhaseEvent per chunk ---
    total = len(chunks)
    yield PhaseEvent("embedding", current=0, total=total)
    vectors: list[list[float]] = []
    try:
        for done, vector in enumerate(embed_texts_iter(chunks), start=1):
            vectors.append(vector)
            yield PhaseEvent("embedding", current=done, total=total)
    except EmbeddingUpstreamError as exc:
        raise PipelineError(502, {"message": _EMBED_UPSTREAM_MESSAGE}) from exc

    # --- indexing: bulk index (overwrite same _ids) then prune stale sha256 ---
    yield PhaseEvent("indexing")
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
        raise PipelineError(
            502, {"message": _BULK_FAILURE_MESSAGE, "failures": exc.failures}
        ) from exc
    except OpenSearchException as exc:
        raise PipelineError(502, {"message": _INDEX_UPSTREAM_MESSAGE}) from exc

    try:
        prune_stale_chunks(doc_id, keep_sha256=sha256, index=request.index)
    except OpenSearchException as exc:
        raise PipelineError(502, {"message": _INDEX_UPSTREAM_MESSAGE}) from exc

    yield ResultEvent(
        doc_id=doc_id, sha256=sha256, chunks_indexed=chunks_indexed, skipped=False
    )
