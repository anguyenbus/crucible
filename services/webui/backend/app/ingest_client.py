"""HTTP client for the ingestion service's `POST /ingest` (blocking) and
`POST /ingest/stream` (live per-phase SSE).

The BFF consumes ingestion over its env-var URL ONLY (`WEBUI_INGESTION_URL`) --
it never imports the `services.ingestion` package.

`ingest()` BLOCKS until ingestion returns the terminal result (still used where
no progress is needed). `ingest_stream()` consumes ingestion's SSE and invokes a
per-phase callback as each `data:` frame arrives, returning the same terminal
`IngestResult` — this is what the async upload's background job uses to record
live progress. Neither call fabricates progress: phases come straight from the
service.

Ingestion's typed error surface (per `services/ingestion/README.md`) is mapped
to `IngestionError` carrying the HTTP-equivalent code and a message, so the
upload endpoint can persist a structured `failed` document status instead of
swallowing a 500:
    400 — invalid/unsupported source, or over `max_chunks_per_doc`
    404 — missing S3 object / local file
    422 — a PDF with no extractable text
    502 — Bedrock/OpenSearch upstream failure
A network fault reaching ingestion (killed/unreachable) is normalized to a 502
so a killed ingestion yields a typed, persisted, renderable failure -- never a
hang or an opaque error.
"""

import json
from collections.abc import Callable

import httpx

INGEST_TIMEOUT_SECONDS = 120.0

# The streaming call has NO overall deadline (a large document legitimately takes
# minutes) but DOES bound the idle gap BETWEEN frames: ingestion emits a frame per
# embedded chunk, so the largest healthy gap is a Bedrock throttling backoff or the
# final bulk index — comfortably under this. A truly wedged server therefore trips
# the read timeout instead of pinning a background worker + DB connection forever
# and stranding the document mid-phase; the timeout surfaces as a 502 (below), so
# the document still reaches a terminal `failed` status the poller can stop on.
INGEST_STREAM_IDLE_TIMEOUT_SECONDS = 120.0
INGEST_STREAM_TIMEOUT = httpx.Timeout(
    connect=10.0, read=INGEST_STREAM_IDLE_TIMEOUT_SECONDS, write=30.0, pool=10.0
)


class IngestResult:
    def __init__(self, doc_id: str, sha256: str, chunks_indexed: int, skipped: bool):
        self.doc_id = doc_id
        self.sha256 = sha256
        self.chunks_indexed = chunks_indexed
        self.skipped = skipped


class IngestionError(Exception):
    """A typed ingestion-domain failure carrying the HTTP-equivalent code."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _extract_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text or f"ingestion returned HTTP {response.status_code}"
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        return str(detail.get("message") or detail)
    if isinstance(detail, str):
        return detail
    return str(body)


def _detail_to_message(detail: object) -> str:
    """Flatten an SSE `error` event's `detail` (str or `{"message": ...}` dict)."""
    if isinstance(detail, dict):
        return str(detail.get("message") or detail)
    return str(detail)


def ingest_stream(
    source: str,
    ingestion_url: str,
    index: str | None = None,
    *,
    on_phase: Callable[[dict], None],
) -> IngestResult:
    """Stream `POST /ingest/stream`, invoking `on_phase` per progress frame.

    `on_phase` receives each phase event dict (e.g.
    `{"phase": "embedding", "current": 3, "total": 12}`) as it arrives. Returns
    the terminal `IngestResult` on the `done` frame, or raises `IngestionError`
    from an `error` frame (its `status`), a non-200 status line, a network
    fault (normalized to 502), or a stream that ends without a terminal frame.
    """
    url = f"{ingestion_url.rstrip('/')}/ingest/stream"
    payload: dict[str, str] = {"source": source}
    if index is not None:
        payload["index"] = index

    try:
        with httpx.stream(
            "POST", url, json=payload, timeout=INGEST_STREAM_TIMEOUT
        ) as response:
            if response.status_code != 200:
                response.read()  # materialize the body before reading it
                raise IngestionError(response.status_code, _extract_detail(response))

            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                event = json.loads(line[len("data:") :].strip())
                phase = event.get("phase")
                if phase == "done":
                    return IngestResult(
                        doc_id=event["doc_id"],
                        sha256=event["sha256"],
                        chunks_indexed=event["chunks_indexed"],
                        skipped=event["skipped"],
                    )
                if phase == "error":
                    raise IngestionError(
                        int(event.get("status", 502)),
                        _detail_to_message(event.get("detail")),
                    )
                on_phase(event)
    except httpx.TimeoutException as exc:
        raise IngestionError(
            504,
            f"ingestion stalled with no progress for "
            f"{INGEST_STREAM_IDLE_TIMEOUT_SECONDS:.0f}s at {url}: {exc}",
        ) from exc
    except httpx.HTTPError as exc:
        raise IngestionError(502, f"ingestion unreachable at {url}: {exc}") from exc

    raise IngestionError(502, "ingestion stream ended without a terminal event")


def ingest(
    source: str, ingestion_url: str, index: str | None = None
) -> IngestResult:
    """Call ingestion synchronously; raise `IngestionError` on any failure.

    `index` (project-scoped chat) targets a specific per-project OpenSearch
    index; when omitted, ingestion writes to its service-default index.
    """
    url = f"{ingestion_url.rstrip('/')}/ingest"
    payload: dict[str, str] = {"source": source}
    if index is not None:
        payload["index"] = index
    try:
        response = httpx.post(url, json=payload, timeout=INGEST_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        raise IngestionError(502, f"ingestion unreachable at {url}: {exc}") from exc

    if response.status_code == 200:
        body = response.json()
        return IngestResult(
            doc_id=body["doc_id"],
            sha256=body["sha256"],
            chunks_indexed=body["chunks_indexed"],
            skipped=body["skipped"],
        )

    raise IngestionError(response.status_code, _extract_detail(response))
