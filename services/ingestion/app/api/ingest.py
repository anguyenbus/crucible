"""POST /ingest (synchronous) and POST /ingest/stream (live per-phase SSE).

Both routes run the SAME pipeline via `app.pipeline.run.run_ingest`, a generator
that yields a phase event at each stage (fetch → parse → normalize → sha256 →
chunk → max-chunks guard → dedup check → embed → bulk index → prune) and a final
result. Chunking runs before the dedup check because the check needs the locally
computed expected chunk count; it is cheap and makes no AWS calls. The
`max_chunks_per_doc` guard rejects oversized documents with a clear 400 BEFORE
any embedding call is made.

  - `/ingest` drains the generator and returns only the terminal `IngestResponse`
    (behaviour byte-identical to the previous inline handler).
  - `/ingest/stream` forwards every event to the client as `text/event-stream`
    so a caller (the webui BFF) can record live per-phase progress; the
    embedding phase reports `current`/`total` (chunk i of N). The HTTP status is
    200 for the whole stream, so a mid-stream failure arrives as a terminal
    `error` event (never a status change after the headers are sent).

The OPTIONAL `index` on the request (project-scoped chat) targets a specific
OpenSearch index for the dedup count, write, AND prune. ABSENT ⇒ the
service-default single index (`settings.index_name`), byte-identical to today.

Error contract (identical for both routes — `/ingest` as the HTTP status,
`/ingest/stream` as the terminal `error` event's `status`):
  400 — invalid/unsupported source string; document exceeds max_chunks_per_doc
  404 — S3 object or local file does not exist
  422 — a .pdf with no extractable native text (scanned / image-only)
  502 — Bedrock or OpenSearch upstream failure: safe, non-technical message,
        plus per-chunk `_bulk` failure details when a bulk write fails

There is no partial-failure recovery beyond reporting: deterministic chunk
`_id`s mean a retry simply overwrites the same documents (self-healing), so
no rollback logic exists anywhere.
"""

import json
from collections.abc import Iterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.pipeline.run import PhaseEvent, PipelineError, ResultEvent, run_ingest
from app.schemas.ingest import IngestRequest, IngestResponse

router = APIRouter()


@router.post("/ingest", response_model=IngestResponse)
def ingest(request: IngestRequest) -> IngestResponse:
    """Run the pipeline to completion and return the terminal result.

    Phase events are drained and ignored; only the `ResultEvent` matters. A
    `PipelineError` surfaces as the same `HTTPException` (status + detail) the
    previous inline handler raised.
    """
    try:
        for event in run_ingest(request):
            if isinstance(event, ResultEvent):
                return IngestResponse(
                    doc_id=event.doc_id,
                    sha256=event.sha256,
                    chunks_indexed=event.chunks_indexed,
                    skipped=event.skipped,
                )
    except PipelineError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    # A generator that finishes without a ResultEvent is a programming error.
    raise HTTPException(
        status_code=500, detail={"message": "ingest pipeline produced no result"}
    )


@router.post("/ingest/stream")
def ingest_stream(request: IngestRequest) -> StreamingResponse:
    """Run the pipeline and stream each phase to the client as SSE.

    Emits `data: {json}` frames: one per `PhaseEvent`
    (`{"phase": "...", "current": i, "total": n}`), then exactly one terminal
    frame — `{"phase": "done", ...result}` on success or
    `{"phase": "error", "status": N, "detail": ...}` on a typed failure.
    """
    return StreamingResponse(
        _sse_events(request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse_frame(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _sse_events(request: IngestRequest) -> Iterator[str]:
    try:
        for event in run_ingest(request):
            if isinstance(event, PhaseEvent):
                frame: dict[str, object] = {"phase": event.phase}
                if event.current is not None:
                    frame["current"] = event.current
                    frame["total"] = event.total
                yield _sse_frame(frame)
            elif isinstance(event, ResultEvent):
                yield _sse_frame(
                    {
                        "phase": "done",
                        "doc_id": event.doc_id,
                        "sha256": event.sha256,
                        "chunks_indexed": event.chunks_indexed,
                        "skipped": event.skipped,
                    }
                )
    except PipelineError as exc:
        yield _sse_frame(
            {"phase": "error", "status": exc.status_code, "detail": exc.detail}
        )
