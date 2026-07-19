"""HTTP client for the parser service's `POST /parse` (blocking drain) and
`POST /parse/stream` (live per-page SSE).

Ingestion routes every non-`.md` document through the parser over its env-var URL
ONLY (`INGESTION_PARSER_URL`) -- it never imports the `services.parser` package
(the same firewall the BFF has to ingestion). The parser owns the heavy docling +
Textract stack; ingestion stays torch-free and mocks THIS module in its lean
suite exactly as the BFF mocks `ingest_client`.

`parse()` BLOCKS until the parser returns the terminal drain result
(`{ markdown, confidence, page_count, page_routes, call_counts, warnings }`) and
materializes it into a `ParseResult`. `parse_stream()` consumes the parser's SSE
and invokes a per-frame callback (`on_phase`) as each `data:` frame arrives,
returning the same terminal `ParseResult` on the `done` frame — this is what
`run_ingest`'s `parsing` phase uses to re-emit per-page progress through
`/ingest/stream` (the nested stream). Neither call fabricates progress: the frames
come straight from the parser (and the parser's `page` frames are themselves a
POST-HOC per-page manifest — see the parser's `parse_runner`).

The parser's typed error surface (per the parser boundary) is mapped to a
`ParserError` carrying the HTTP-equivalent code and a message, so the ingestion
pipeline can re-raise it as the equivalent `PipelineError` and reach a terminal,
renderable failure instead of an opaque 500:
    400 — unsupported / unknown document type
    413 — over the parser's page cap or input-size cap
    422 — no extractable content (empty markdown / every page errored)
    502 — the escalation engine was transiently down for the majority of pages
A network fault reaching the parser (killed / unreachable) is normalized to a 502
so a killed parser yields a typed, retryable failure -- never a hang. A WEDGED
parse that stalls with no progress trips the httpx `read=` IDLE timeout (NOT a
wall-clock total: a legitimate 100-page parse runs ~100-190 min) and surfaces as
a 504, so the document still reaches a terminal status instead of pinning a worker
forever. A stream that ends WITHOUT a terminal frame is normalized to a 502.
"""

import json
from collections.abc import Callable

import httpx

# The parse call has NO overall deadline (a large document legitimately takes many
# minutes -- 100 pages x 60-115 s/page = ~100-190 min) but DOES bound the idle gap
# with the read timeout: a healthy parser is always making progress under this, so
# only a truly WEDGED parse trips it. It surfaces as a 504 (below), keeping the page
# cap safe (a wall-clock cap would kill legitimate long parses; an idle cap kills
# only wedged ones). Aligns with the BFF's 120 s idle constant.
#
# STREAMING CAVEAT (honest): the parser's `page` frames are a POST-HOC manifest —
# they arrive in a burst AFTER the opaque docling parse completes, so the largest
# idle gap on `/parse/stream` is the docling parse itself, NOT the inter-page gap.
# The vendored pipeline exposes no page-level callback, so this idle bound cannot
# distinguish a wedged parse from a legitimately-long one DURING that opaque span;
# that limitation is inherent to not forking the vendored pipeline and is accepted
# for v1 (the page cap x concurrency is still the hard spend/latency bound).
PARSE_IDLE_TIMEOUT_SECONDS = 120.0
PARSE_TIMEOUT = httpx.Timeout(
    connect=10.0, read=PARSE_IDLE_TIMEOUT_SECONDS, write=30.0, pool=10.0
)


class ParseResult:
    """The terminal drain result of `POST /parse` (the full parser result shape)."""

    def __init__(
        self,
        markdown: str,
        confidence: dict,
        page_count: int,
        page_routes: list,
        call_counts: dict,
        warnings: list,
    ):
        self.markdown = markdown
        self.confidence = confidence
        self.page_count = page_count
        self.page_routes = page_routes
        self.call_counts = call_counts
        self.warnings = warnings


class ParserError(Exception):
    """A typed parser-domain failure carrying the HTTP-equivalent code."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _extract_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text or f"parser returned HTTP {response.status_code}"
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        return _detail_to_message(detail)
    if isinstance(detail, str):
        return detail
    return str(body)


def _detail_to_message(detail: object) -> str:
    """Flatten a `detail` payload (str or `{"message": ...}` dict) to a message."""
    if isinstance(detail, dict):
        return str(detail.get("message") or detail)
    return str(detail)


def _result_from_body(body: dict) -> ParseResult:
    """Materialize a terminal drain/`done` body into a `ParseResult`."""
    return ParseResult(
        markdown=body["markdown"],
        confidence=body["confidence"],
        page_count=body["page_count"],
        page_routes=body["page_routes"],
        call_counts=body["call_counts"],
        warnings=body["warnings"],
    )


def parse(data: bytes, filename: str, *, parser_url: str) -> ParseResult:
    """Call the parser synchronously; raise `ParserError` on any failure.

    Sends the raw document `data` as an `application/octet-stream` body with the
    `filename` as a query param (its extension drives the parser's format
    dispatch). Returns the materialized `ParseResult` on 200, or raises a typed
    `ParserError`: the parser's own 400/413/422/502 (its `detail` message), a
    network fault normalized to 502, or a wedged/idle stall normalized to 504.
    """
    url = f"{parser_url.rstrip('/')}/parse"
    try:
        response = httpx.post(
            url,
            content=data,
            params={"filename": filename},
            headers={"content-type": "application/octet-stream"},
            timeout=PARSE_TIMEOUT,
        )
    except httpx.TimeoutException as exc:
        raise ParserError(
            504,
            f"parser stalled with no progress for "
            f"{PARSE_IDLE_TIMEOUT_SECONDS:.0f}s at {url}: {exc}",
        ) from exc
    except httpx.HTTPError as exc:
        raise ParserError(502, f"parser unreachable at {url}: {exc}") from exc

    if response.status_code == 200:
        return _result_from_body(response.json())

    # Non-200: the body is already materialized by the blocking post, so reading
    # the typed detail out of it cannot itself stall or race the response stream.
    raise ParserError(response.status_code, _extract_detail(response))


def parse_stream(
    data: bytes,
    filename: str,
    *,
    parser_url: str,
    on_phase: Callable[[dict], None],
) -> ParseResult:
    """Stream `POST /parse/stream`, invoking `on_phase` per progress frame.

    Mirrors `services/webui/backend/app/ingest_client.py`'s `ingest_stream`.
    `on_phase` receives each non-terminal phase event dict (e.g. `{"phase": "page",
    "current": 3, "total": 12, "route": "textract"}`) as it arrives. Returns the
    terminal `ParseResult` on the `done` frame, or raises `ParserError` from an
    `error` frame (its `status`), a non-200 status line, a network fault
    (normalized to 502), an idle stall (the httpx `read=` timeout → 504), or a
    stream that ends without a terminal frame (→ 502).
    """
    url = f"{parser_url.rstrip('/')}/parse/stream"
    try:
        with httpx.stream(
            "POST",
            url,
            content=data,
            params={"filename": filename},
            headers={"content-type": "application/octet-stream"},
            timeout=PARSE_TIMEOUT,
        ) as response:
            if response.status_code != 200:
                response.read()  # materialize the body before reading it
                raise ParserError(response.status_code, _extract_detail(response))

            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                event = json.loads(line[len("data:") :].strip())
                phase = event.get("phase")
                if phase == "done":
                    return _result_from_body(event)
                if phase == "error":
                    raise ParserError(
                        int(event.get("status", 502)),
                        _detail_to_message(event.get("detail")),
                    )
                on_phase(event)
    except httpx.TimeoutException as exc:
        raise ParserError(
            504,
            f"parser stalled with no progress for "
            f"{PARSE_IDLE_TIMEOUT_SECONDS:.0f}s at {url}: {exc}",
        ) from exc
    except httpx.HTTPError as exc:
        raise ParserError(502, f"parser unreachable at {url}: {exc}") from exc

    raise ParserError(502, "parser stream ended without a terminal event")
