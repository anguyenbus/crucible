"""Parse stage: raw document bytes → markdown, dispatched by extension.

Two routes only, decided by the source's extension:
  - `.md` / `.markdown` → local UTF-8 decode (NO round-trip to the parser).
  - anything else       → the parser service over HTTP, which returns markdown for
                          PDF / image / DOCX / XLSX / HTML via docling + Textract.

Two parser transports, both landing in the SAME `normalize → hash → chunk → embed
→ index` chain:
  - `extract_document`         → `parser_client.parse` (blocking drain). Used where
                                 no progress is needed / by the drain-seam tests.
  - `extract_document_stream`  → `parser_client.parse_stream` (SSE), forwarding one
                                 `on_page(current, total)` per parser `page` frame.
                                 Used by `run_ingest` so `/ingest/stream` re-emits
                                 per-page `parsing` progress (the nested stream).

Ingestion NEVER imports the parser package; it speaks to it HTTP-only over
`INGESTION_PARSER_URL` (the same firewall the BFF has to ingestion), so the heavy
docling / torch stack stays entirely on the parser side and ingestion's suite is
torch-free.

Error reconciliation: the parser's `parse_to_markdown` NEVER raises — its HTTP
boundary already returns a typed **422** when there is no extractable content
(empty markdown / every page errored). We map that `ParserError(422)` back onto
the SAME `NoExtractableTextError` the pipeline already catches, so ingestion's
"never index nothing" 422 contract stays intact. Every other typed parser status
(400 unsupported, 413 oversized, 502 escalation-down, 504 idle stall, plus the
stream's 502-on-no-terminal) propagates as a `ParserError` for `run_ingest` to map
once.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings
from app.pipeline import parser_client
from app.pipeline.parser_client import ParseResult, ParserError

# The advisory review threshold below which a page is deemed low-confidence.
# Kept in lock-step with the parser's `parser_service.confidence.
# LOW_CONFIDENCE_THRESHOLD` (0.65). Ingestion NEVER imports the parser package
# (torch-free firewall), so the constant is mirrored, not imported. It only
# selects which page indices land in the advisory provenance summary — it NEVER
# gates ingest and is NEVER part of any hash.
LOW_CONFIDENCE_THRESHOLD = 0.65


class NoExtractableTextError(ValueError):
    """A document carried no extractable content (scanned / image-only / empty).

    Mapped to HTTP 422 by the pipeline — an honest failure, never a silent empty
    index. Raised locally when the parser's boundary returns its own 422
    (no-extractable-content), reconciling `parse_to_markdown`'s never-raises
    contract with ingestion's refuse-to-index-nothing contract.
    """


@dataclass(frozen=True)
class ExtractResult:
    """Markdown plus the parser's advisory provenance (co-located concerns).

    `provenance` is a COMPACT summary of the parser result (see
    `summarize_provenance`) for the parser-routed path, or `None` for the
    `.md`-local path (which has no provenance — a null is stamped, never
    fabricated). Provenance is metadata-only: it never gates ingest and is never
    part of any dedup hash.
    """

    markdown: str
    provenance: dict | None


def summarize_provenance(result: ParseResult) -> dict:
    """Distil a `ParseResult` to a COMPACT, log-and-store provenance summary.

    Deliberately NOT the full `page_routes` array: just the document-level
    confidence, a route-count summary, the escalation `call_counts`, and the
    low-confidence page indices — enough to audit a parse without bloating every
    chunk. Advisory only; never gates, never hashed.
    """
    confidence = result.confidence or {}
    call_counts = result.call_counts or {}

    route_counts: dict[str, int] = {}
    for record in result.page_routes or []:
        route = record.get("route", "unknown")
        route_counts[route] = route_counts.get(route, 0) + 1

    low_confidence_pages = [
        page.get("page_index")
        for page in confidence.get("pages", []) or []
        if isinstance(page.get("confidence"), (int, float))
        and page["confidence"] < LOW_CONFIDENCE_THRESHOLD
    ]

    return {
        "confidence": confidence.get("document"),
        "route_counts": route_counts,
        "call_counts": call_counts,
        "low_confidence_pages": low_confidence_pages,
    }


def extract_document(source: str, data: bytes) -> ExtractResult:
    """Turn raw `data` bytes into markdown + provenance via the BLOCKING drain.

    `.md` / `.markdown` decode locally (provenance `None` — there is none);
    everything else round-trips through the parser's `POST /parse` (drain) and
    carries a compact provenance summary. A `ParserError(422)` (no extractable
    content) is remapped to `NoExtractableTextError`; all other typed parser
    failures propagate as-is. `run_ingest` uses `extract_document_stream` (below);
    this drain variant is kept for callers that need no progress and for the
    drain-seam unit tests.
    """
    lower = source.lower()
    if lower.endswith((".md", ".markdown")):
        return ExtractResult(markdown=data.decode("utf-8"), provenance=None)

    settings = get_settings()
    try:
        result = parser_client.parse(
            data, Path(source).name, parser_url=settings.parser_url
        )
    except ParserError as exc:
        if exc.status_code == 422:
            raise NoExtractableTextError(exc.message) from exc
        raise
    return ExtractResult(
        markdown=result.markdown, provenance=summarize_provenance(result)
    )


def extract_document_stream(
    source: str, data: bytes, *, on_page: Callable[[int | None, int | None], None]
) -> ExtractResult:
    """Turn raw `data` bytes into markdown + provenance via the SSE STREAM.

    Identical dispatch and error reconciliation to `extract_document`, but the
    non-`.md` path consumes `parser_client.parse_stream` and invokes
    `on_page(current, total)` for EACH parser `page` frame, so the caller
    (`run_ingest`) can re-emit per-page `parsing` progress through `/ingest/stream`
    — the nested stream (ingestion consuming the parser's SSE while producing its
    own SSE to the BFF).

    The `.md`-local path never calls `on_page` (there is no parser round-trip and
    no page frames — it stays a single bare `parsing` phase upstream). Because the
    parser's `page` frames are a POST-HOC manifest (a burst after the opaque parse),
    `on_page` fires in that same burst — an honest per-page manifest, not a live
    progress bar.
    """
    lower = source.lower()
    if lower.endswith((".md", ".markdown")):
        return ExtractResult(markdown=data.decode("utf-8"), provenance=None)

    settings = get_settings()

    def _forward(event: dict) -> None:
        # Only the parser's per-page `page` frames drive ingestion's re-emit; a
        # leading `parsing` frame (or any other) is progress the parser owns and is
        # not forwarded as an ingestion page count.
        if event.get("phase") == "page":
            on_page(event.get("current"), event.get("total"))

    try:
        result = parser_client.parse_stream(
            data,
            Path(source).name,
            parser_url=settings.parser_url,
            on_phase=_forward,
        )
    except ParserError as exc:
        if exc.status_code == 422:
            raise NoExtractableTextError(exc.message) from exc
        raise
    return ExtractResult(
        markdown=result.markdown, provenance=summarize_provenance(result)
    )


def extract_text(source: str, data: bytes) -> str:
    """Backwards-compatible markdown-only accessor over `extract_document`."""
    return extract_document(source, data).markdown
