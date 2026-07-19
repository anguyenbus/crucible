"""POST /parse (drain) and POST /parse/stream (live per-page SSE).

Both routes run the SAME generator (`app.parse_runner.run_parse`) under the SAME
one-in-flight-per-pod semaphore:

  - `POST /parse` drains the generator, ignores the phase events, and returns only
    the terminal result — behaviour byte-identical to before Task Group 6.
  - `POST /parse/stream` forwards each event to the client as `text/event-stream`
    so a caller (ingestion's `parser_client.parse_stream`) can record per-page
    progress. Mirrors `services/ingestion/app/api/ingest.py`'s `POST /ingest/stream`
    exactly: `data: {json}` frames, one per `PhaseEvent` (a leading `parsing` frame
    then one `page` frame per page), then exactly one terminal frame —
    `{"phase": "done", ...result}` on success or
    `{"phase": "error", "status": N, "detail": ...}` on a typed failure. The HTTP
    status is 200 for the WHOLE stream, so a mid-stream failure arrives as a
    terminal `error` frame, NEVER a status change after the headers are sent.

HONESTY (load-bearing — see `parse_runner.PhaseEvent`): the `page` frames are a
POST-HOC per-page MANIFEST, not live mid-parse progress. `parse_to_markdown` is a
single synchronous call and the vendored pipeline exposes no page-level callback,
so all `page` frames arrive in a burst AFTER the opaque docling parse completes.
No fabricated incremental progress.

Input (both routes): raw document bytes as EITHER `multipart/form-data` (a `file`
part) OR a raw `application/octet-stream` body, plus a `filename` (query param, or
the multipart part's own filename, or an `X-Filename` header). The filename's
extension drives `parse_to_markdown`'s format dispatch, so it must be preserved.

Result shape (drain body / stream `done` frame): `{ markdown, confidence,
page_count, page_routes, call_counts, warnings }`.

Guardrails (see `parse_runner` for the size/page caps): concurrency is bounded to
`PARSER_MAX_CONCURRENT_PARSES` (=1) in-flight parse per pod — docling/torch
saturate the cores, so we scale by replicas, not per-pod concurrency. The blocking
parse runs in a threadpool under that semaphore so the event loop stays free; the
stream steps the same blocking generator via the threadpool under the same
semaphore, so at most one parse is ever in-flight per pod ACROSS both endpoints.

The parser is internal-only / east-west (never ingress-exposed, no app auth) — an
exposed parser billing Textract per page would be a cost-bomb / DoS.
"""

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.parse_runner import PhaseEvent, PipelineError, ResultEvent, run_parse

router = APIRouter()

# One in-flight parse per pod (NOT 2 — torch/docling saturate the cores). Scale
# horizontally by replicas. Created once at import; asyncio.Semaphore does not
# bind to a loop until first awaited. Shared by BOTH endpoints so the concurrency
# bound holds across drain + stream, not per-endpoint.
_parse_semaphore = asyncio.Semaphore(get_settings().max_concurrent_parses)

# Sentinel returned by `_next_event` when the generator is exhausted. A two-arg
# `next(..., default)` swallows StopIteration but re-raises PipelineError, which is
# exactly what the SSE loop needs to turn into a terminal `error` frame.
_STREAM_EXHAUSTED = object()


@router.post("/parse")
async def parse(request: Request, filename: str | None = None) -> dict[str, object]:
    """Drain the parse generator and return the terminal result.

    Phase events are drained and ignored; only the `ResultEvent` matters. A
    `PipelineError` becomes the same `HTTPException` (status + detail) the runner
    assigned.
    """
    raw_bytes, resolved_filename = await _read_input(request, filename)

    async with _parse_semaphore:
        try:
            return await run_in_threadpool(_drain, raw_bytes, resolved_filename)
        except PipelineError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc


@router.post("/parse/stream")
async def parse_stream(
    request: Request, filename: str | None = None
) -> StreamingResponse:
    """Run the parse and stream each event to the client as SSE.

    Emits `data: {json}` frames: one per `PhaseEvent` (a leading `{"phase":
    "parsing"}`, then `{"phase": "page", "current": i, "total": N, "route": ...}`
    per page), then exactly one terminal frame — `{"phase": "done", ...result}` on
    success or `{"phase": "error", "status": N, "detail": ...}` on a typed failure.
    """
    raw_bytes, resolved_filename = await _read_input(request, filename)
    return StreamingResponse(
        _sse_events(raw_bytes, resolved_filename),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _drain(raw_bytes: bytes, filename: str) -> dict[str, object]:
    """Run `run_parse` to completion and return the terminal result as a dict.

    Runs in a threadpool (the parse is blocking, CPU-bound docling work). Raises
    `PipelineError` on a typed failure — caught by the async caller above. Phase
    events (including the Task Group 6 `page` frames) are ignored here.
    """
    for event in run_parse(raw_bytes, filename):
        if isinstance(event, ResultEvent):
            return _result_payload(event)
    # A generator that finishes without a ResultEvent is a programming error.
    raise PipelineError(500, {"message": "parse pipeline produced no result"})


def _sse_frame(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _next_event(generator):
    """Advance the blocking `run_parse` generator one step (threadpool-safe).

    Returns the yielded event, or the `_STREAM_EXHAUSTED` sentinel on StopIteration.
    A `PipelineError` raised inside the generator propagates out unchanged (the
    two-arg `next` default only suppresses StopIteration).
    """
    return next(generator, _STREAM_EXHAUSTED)


async def _sse_events(raw_bytes: bytes, filename: str) -> AsyncIterator[str]:
    """Drive the blocking `run_parse` generator, yielding SSE frames.

    Held under the shared `_parse_semaphore` for the whole stream so at most one
    parse runs per pod across both endpoints. Each generator step runs in a
    threadpool (the docling parse is blocking, CPU-bound) so the event loop stays
    free; the heavy `parse_to_markdown` work happens inside a single step, so the
    `page` frames — and any terminal `error` — arrive right after it (a burst,
    per the POST-HOC manifest honesty note in `parse_runner.PhaseEvent`).
    """
    async with _parse_semaphore:
        generator = run_parse(raw_bytes, filename)
        try:
            while True:
                event = await run_in_threadpool(_next_event, generator)
                if event is _STREAM_EXHAUSTED:
                    break
                if isinstance(event, PhaseEvent):
                    frame: dict[str, object] = {"phase": event.phase}
                    if event.current is not None:
                        frame["current"] = event.current
                        frame["total"] = event.total
                    if event.route is not None:
                        frame["route"] = event.route
                    yield _sse_frame(frame)
                elif isinstance(event, ResultEvent):
                    yield _sse_frame({"phase": "done", **_result_payload(event)})
        except PipelineError as exc:
            yield _sse_frame(
                {"phase": "error", "status": exc.status, "detail": exc.detail}
            )


def _result_payload(event: ResultEvent) -> dict[str, object]:
    """The terminal drain/`done` result shape from a `ResultEvent`."""
    return {
        "markdown": event.markdown,
        "confidence": event.confidence,
        "page_count": event.page_count,
        "page_routes": event.page_routes,
        "call_counts": event.call_counts,
        "warnings": event.warnings,
    }


async def _read_input(request: Request, filename: str | None) -> tuple[bytes, str]:
    """Read the document bytes + filename from multipart OR an octet-stream body.

    The filename resolves in order: the explicit `filename` query param, the
    multipart part's own filename, the `X-Filename` header, then a bare fallback
    (extension-less → `parse_to_markdown` classifies as unknown → 400, which is the
    correct contract for a nameless blob).
    """
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            raise HTTPException(
                status_code=400,
                detail="multipart/form-data requires a 'file' part.",
            )
        raw_bytes = await upload.read()
        resolved = filename or upload.filename or "document"
        return raw_bytes, resolved

    raw_bytes = await request.body()
    resolved = filename or request.headers.get("x-filename") or "document"
    return raw_bytes, resolved
