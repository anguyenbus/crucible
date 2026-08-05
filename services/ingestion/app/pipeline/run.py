"""The ingestion pipeline as a phase-emitting generator (one source of truth).

`run_ingest` drives the exact same linear pipeline the `/ingest` handler used to
run inline — fetch → parse → normalize → hash → chunk → max-chunks guard →
dedup check → embed → index → prune — but yields a `PhaseEvent` at each natural
boundary and a final `ResultEvent`, so BOTH entry points share it:

  - `POST /ingest` drains the generator, ignores the phase events, and returns
    the terminal result (behaviour byte-identical to before).
  - `POST /ingest/stream` forwards each event to the client as SSE for live
    per-phase progress (the parsing phase reports per-page `current`/`total`; the
    embedding phase reports per-chunk `current`/`total`).

Error mapping lives HERE, once: every typed pipeline failure is raised as a
`PipelineError(status_code, detail)`, so `/ingest` re-raises it as an
`HTTPException` and `/ingest/stream` emits it as a terminal `error` event —
neither route duplicates the status-code logic.

The four caller-facing phases map to the pipeline steps as: parsing =
fetch+parse, chunking = normalize+hash+chunk+dedup-check, embedding = the
per-chunk Titan loop, indexing = bulk index + prune-stale. The parsing phase
round-trips non-`.md` documents through the parser service over `/parse/stream`
and re-emits one `PhaseEvent("parsing", current=i, total=N)` per parser page frame
(the nested stream: ingestion consuming the parser's SSE while itself producing
SSE to the BFF). Its typed errors (400/413/422/502, plus a 504 idle stall and the
stream's 502-on-no-terminal) flow through here unchanged.

HONESTY (load-bearing): the parser's per-page frames are a POST-HOC manifest — the
vendored docling pipeline exposes no page-level callback, so every page frame
arrives in a burst AFTER the opaque parse completes. Ingestion forwards exactly
what the parser emits (no fabricated increments); the per-page `parsing` frames
are therefore a faithful per-page manifest, not a live mid-parse progress bar.
"""

import logging
from collections.abc import Iterator
from dataclasses import dataclass

from opensearchpy.exceptions import OpenSearchException

from app.config import get_settings
from app.pipeline.chunk import chunk_text
from app.pipeline.embed import EmbeddingUpstreamError, embed_texts_iter
from app.pipeline.fetch import InvalidSourceError, SourceNotFoundError, fetch_bytes
from app.pipeline.guard import GuardUnavailableError, GuardVerdict, check_chunks
from app.pipeline.hash_dedup import (
    content_sha256,
    derive_doc_id,
    find_complete_raw_duplicate,
    is_complete_duplicate,
    raw_content_sha256,
)
from app.pipeline.index import BulkIndexError, index_chunks, prune_stale_chunks
from app.pipeline.normalize import normalize
from app.pipeline.parse import NoExtractableTextError, extract_document_stream
from app.pipeline.parser_client import ParserError
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
_GUARD_UNAVAILABLE_MESSAGE = (
    "The document-safety guard is temporarily unavailable; the document was NOT "
    "indexed (fail-closed). Please retry the ingest."
)


def _rejection_detail(verdict: GuardVerdict, source: str) -> dict:
    """Build the UI-facing rejection body from the guard's forensic verdict.

    Names the offending chunk(s) and the injection labels that fired so the officer
    sees WHICH part of the document is unsafe and WHY.
    """
    unsafe = [r for r in verdict.results if r.get("verdict") == "unsafe"]
    labels: list[str] = []
    for chunk in unsafe:
        for det in chunk.get("detections", []):
            label = det.get("label")
            if label and label not in labels:
                labels.append(label)
    first = unsafe[0].get("chunk_id") if unsafe else None
    message = (
        f"Document rejected as unsafe to ingest: prompt injection detected in "
        f"{verdict.unsafe_chunk_count} of {verdict.chunk_count} chunk(s)"
        + (f" (first: {first})" if first else "")
        + ". Detected: "
        + ", ".join(labels[:12])
        + (" …" if len(labels) > 12 else "")
        + ". The document was NOT indexed."
    )
    return {
        "message": message,
        "guard": {
            "safe": verdict.safe,
            "unsafe_chunk_count": verdict.unsafe_chunk_count,
            "detection_count": verdict.detection_count,
            "source_ref": source,
            "results": unsafe,  # per-chunk forensic detail (spans + excerpts)
        },
    }


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PhaseEvent:
    """One progress checkpoint. `current`/`total` are set for parsing + embedding."""

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
    # --- parsing: fetch bytes, then extract markdown (parser SSE, md UTF-8) -----
    # A leading bare `parsing` phase covers fetch + the raw-bytes gate; the parser
    # round-trip then re-emits one `parsing` frame per page (see below).
    yield PhaseEvent("parsing")
    try:
        raw_bytes = fetch_bytes(request.source)
    except InvalidSourceError as exc:
        raise PipelineError(400, str(exc)) from exc
    except SourceNotFoundError as exc:
        raise PipelineError(404, str(exc)) from exc

    # --- pre-parse raw-bytes dedup gate (the cheapest short-circuit) -----------
    # HEADLINE: hash the raw bytes and, BEFORE the expensive parser round-trip,
    # ask OpenSearch whether a COMPLETE copy of these exact bytes is already
    # indexed (doc_id-independent). On a hit we skip the whole parse + embed +
    # index cost and return a `skipped` result indistinguishable from the
    # post-parse text-sha skip below. The gate fires ONLY on a confirmed-complete
    # prior copy (see `find_complete_raw_duplicate`), so a half-indexed prior run
    # never masks the document — it re-ingests and self-heals. The gate STILL runs
    # BEFORE the parse stream (severability of the dedup gate is preserved).
    raw_sha256 = raw_content_sha256(raw_bytes)
    doc_id = request.doc_id or derive_doc_id(request.source)
    try:
        existing_sha256 = find_complete_raw_duplicate(raw_sha256, index=request.index)
    except OpenSearchException as exc:
        raise PipelineError(502, {"message": _INDEX_UPSTREAM_MESSAGE}) from exc
    if existing_sha256 is not None:
        yield ResultEvent(
            doc_id=doc_id, sha256=existing_sha256, chunks_indexed=0, skipped=True
        )
        return

    # Consume the parser's `/parse/stream`, collecting one page frame per parser
    # `page` event. Because the parser's page frames are a POST-HOC manifest (a
    # burst after the opaque parse), they are collected during the stream read and
    # re-emitted immediately after — a faithful per-page manifest, not fabricated
    # live progress. The `.md`-local path never streams: `on_page` is never called,
    # so it stays a single bare `parsing` phase.
    page_frames: list[tuple[int | None, int | None]] = []
    try:
        extracted = extract_document_stream(
            request.source,
            raw_bytes,
            on_page=lambda current, total: page_frames.append((current, total)),
        )
    except NoExtractableTextError as exc:
        raise PipelineError(422, str(exc)) from exc
    except InvalidSourceError as exc:
        raise PipelineError(400, str(exc)) from exc
    except ParserError as exc:
        # The parser's own typed statuses (400/413/502), the idle-stall 504, and a
        # stream that ends without a terminal frame (502) propagate unchanged; the
        # 422 no-content case is remapped to NoExtractableTextError inside
        # extract_document_stream (caught above).
        raise PipelineError(exc.status_code, exc.message) from exc

    # Re-emit one `parsing` progress frame per page consumed from `/parse/stream`.
    for current, total in page_frames:
        yield PhaseEvent("parsing", current=current, total=total)

    raw_text = extracted.markdown
    provenance = extracted.provenance
    if provenance is not None:
        logger.info("parse provenance for %s: %s", request.source, provenance)

    # --- chunking: normalize → sha256 → chunk → max-chunks guard ---
    # `doc_id` was derived above (the raw-bytes gate needs it). The post-parse
    # text-sha (`content_sha256`) is a SEPARATE concern from the raw-bytes gate.
    yield PhaseEvent("chunking")
    text = normalize(raw_text)
    sha256 = content_sha256(text)
    chunks = chunk_text(text)

    settings = get_settings()
    if len(chunks) > settings.max_chunks_per_doc:
        raise PipelineError(
            400,
            f"Document produces {len(chunks)} chunks, which exceeds the "
            f"limit of {settings.max_chunks_per_doc} (max_chunks_per_doc).",
        )

    # --- ingest-time corpus-poisoning guard (guardrail pod /check/chunks) -------
    # Scan the chunks AFTER chunking and BEFORE any embed/index. ANY unsafe chunk
    # rejects the WHOLE document (422 with forensic attribution); the pod being
    # unreachable fails CLOSED (503 — never index unscanned). OFF unless
    # INGESTION_GUARDRAIL_CHECK_ENABLED=true (the dev Makefile enables it).
    if settings.guardrail_check_enabled and chunks:
        try:
            verdict = check_chunks(
                chunks,
                url=settings.guardrail_url,
                document_id=doc_id,
                source_ref=request.source,
            )
        except GuardUnavailableError as exc:
            raise PipelineError(503, {"message": _GUARD_UNAVAILABLE_MESSAGE}) from exc
        if not verdict.safe:
            raise PipelineError(422, _rejection_detail(verdict, request.source))

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
            raw_sha256=raw_sha256,
            provenance=provenance,
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
