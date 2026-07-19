"""Parse runner — the generator + error-translation seam over `parse_to_markdown`.

Mirrors `services/ingestion/app/pipeline/run.py`'s `run_ingest`: a generator that
yields a `PhaseEvent` at each boundary and a terminal `ResultEvent`, with a
single-source-of-truth `PipelineError(status, detail)` mapping so both the drain
`POST /parse` and the SSE `POST /parse/stream` (Task Group 6) share ONE copy of
the status-code logic. `run_ingest` re-raises it as an `HTTPException`; the drain
endpoint does the same, and `/parse/stream` emits it as a terminal `error` frame.

The vendored `parser_service.markdown_pipeline.parse_to_markdown` NEVER raises —
every failure lands in the returned `warnings` / `page_routes`. This module is the
error-translation seam that reconciles that never-raises contract with ingestion's
refuse-to-index-nothing contract, encoding the Q7 error table as the one source of
truth:

    non-empty markdown                                   -> 200 (+ loud warnings)
    empty markdown OR every page errored (no content)    -> 422
    oversized (over page cap OR input-size cap)          -> 413
    unsupported / unknown type                           -> 400
    ALL escalation calls failed TRANSIENTLY and those    -> 502 (retryable)
      pages are the majority of the document
    escalation unreachable but Docling produced usable   -> 200
      markdown (the failed pages are a minority)

Partial degradation is ALWAYS loud in `warnings` (`N/M pages fell back to
Docling`), never silent. Confidence is advisory only: it NEVER gates and is NEVER
part of any hash (per the reference, it is uncalibrated).

LAZY IMPORT (load-bearing): `parse_to_markdown` is a thin module-level indirection
that imports the vendored heavy entrypoint INSIDE its body — nothing here imports
docling/torch at module load. That keeps `parse_runner` importable (so `app.main`
mounts without the multi-GB stack) and lets the error-table tests patch
`app.parse_runner.parse_to_markdown` with crafted outputs WITHOUT importing
docling. Mirrors how `services/ingestion/app/pipeline/parse.py` imports pypdf
inside `extract_pdf_text`.

PER-PAGE STREAMING (Task Group 6): after the parse completes, the runner yields one
`PhaseEvent("page", current=i, total=N, route=...)` per page for the SSE endpoint
to forward. See the `PhaseEvent` docstring for the load-bearing HONESTY note — the
page frames are a POST-HOC per-page MANIFEST derived from `page_routes`, not live
mid-parse progress (the vendored pipeline exposes no page-level callback, so there
is nothing genuine to stream during the opaque docling span). The drain `POST
/parse` ignores every `PhaseEvent`, so its result is byte-identical to before.
"""

from __future__ import annotations

import logging
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

# --- route vocabulary (from references/doc-parser/README.md) ----------------
# Engine SUCCEEDED (its markdown shipped, or arbitration kept a clean Docling
# render over a successful-but-garbled engine render — an engine call was still
# made successfully):
_ESCALATION_SUCCESS_ROUTES = frozenset({"vlm", "textract"})
_ESCALATION_REJECTED_ROUTES = frozenset(
    {"vlm-rejected-kept-docling", "textract-rejected-kept-docling"}
)
# Engine was CALLED but returned nothing usable (error / empty) → gate-flagged
# Docling shipped. A `reason == "throttled"` marks a TRANSIENT failure (network /
# throttle / timeout / 5xx) whose retries exhausted — see the note in
# `_transient_majority_error` for exactly what this signal does and does not cover.
_ESCALATION_FALLBACK_ROUTES = frozenset(
    {"vlm-fallback-docling", "textract-fallback-docling"}
)
_TRANSIENT_REASON = "throttled"


@dataclass(frozen=True)
class PhaseEvent:
    """One progress checkpoint. `current`/`total`/`route` set only for page work.

    The drain `POST /parse` emits a leading bare `parsing` phase and then (as of
    Task Group 6) one `page` phase per page. The drain endpoint IGNORES every
    `PhaseEvent` — it captures only the terminal `ResultEvent` — so the page frames
    are invisible to `/parse` and only reach the SSE `/parse/stream` consumer.

    HONESTY NOTE (load-bearing): the `page` frames are a POST-HOC per-page MANIFEST,
    not live mid-parse progress. `parse_to_markdown` is a single synchronous call
    (docling parses the whole document, then the gate/escalation runs) and the
    vendored pipeline exposes NO page-level callback, so there is no genuine
    incremental progress to forward. The `page` frames are emitted AFTER the parse
    returns by iterating the resulting `page_routes` — they report each page's final
    route in a burst once the long-pole docling parse has already completed. This is
    a faithful manifest, NOT a live progress bar: no sleeps, no interpolation, no
    fabricated increments (the repo's ethos is no invented progress). The real
    long-pole (docling) remains a single opaque span.
    """

    phase: str  # "parsing" | "page"
    current: int | None = None
    total: int | None = None
    route: str | None = None  # the page's final route, on "page" frames only


@dataclass(frozen=True)
class ResultEvent:
    """The terminal success event — the full `POST /parse` result shape."""

    markdown: str
    confidence: dict[str, Any]
    page_count: int
    page_routes: list[dict[str, Any]]
    call_counts: dict[str, int]
    warnings: list[dict[str, Any]]


class PipelineError(Exception):
    """A typed failure carrying its HTTP-equivalent status + detail.

    The single place statuses are assigned (mirrors `run_ingest`'s
    `PipelineError`). `POST /parse` re-raises it as an `HTTPException`; the SSE
    `POST /parse/stream` emits it as a terminal `error` frame.
    """

    def __init__(self, status: int, detail: object) -> None:
        super().__init__(str(detail))
        self.status = status
        self.detail = detail


def parse_to_markdown(file_path: Path) -> dict[str, Any]:
    """Thin, lazily-importing indirection to the vendored never-raises entrypoint.

    The heavy import lives INSIDE the body so importing `app.parse_runner` never
    pulls in docling/torch, and so the error-table tests can patch this symbol
    (`app.parse_runner.parse_to_markdown`) with a crafted dict without the vendored
    stack installed at all.
    """
    from parser_service.markdown_pipeline import parse_to_markdown as _impl

    return _impl(file_path)


def run_parse(raw_bytes: bytes, filename: str) -> Iterator[PhaseEvent | ResultEvent]:
    """Parse `raw_bytes` into markdown, yielding `PhaseEvent`s then a `ResultEvent`.

    Raises `PipelineError` on any typed failure (the single place statuses are
    assigned). Guardrails enforced here: the input-size cap (pre-parse, cheap) and
    the page cap (a cheap pre-parse PDF probe where possible, and — authoritatively
    — from the post-parse `page_routes`). The received bytes are written to a
    CONTEXT-MANAGED tempfile for docling's `Path` API so nothing leaks on crash;
    the parser stays S3-IAM-free (bytes over the wire, never a fetch).

    Yields, in order: a leading `PhaseEvent("parsing")`, then (POST-HOC, after the
    parse returns) one `PhaseEvent("page", current=i, total=N, route=...)` per page,
    then the terminal `ResultEvent`. See the `PhaseEvent` HONESTY note: the page
    frames are a per-page manifest, not live progress.
    """
    settings = get_settings()

    # --- guardrail: input-size cap (pre-parse, before any tempfile/parse) → 413 ---
    size_mb = len(raw_bytes) / (1024 * 1024)
    if size_mb > settings.max_input_mb:
        raise PipelineError(
            413,
            f"Input is {size_mb:.1f} MB, over the {settings.max_input_mb:.0f} MB cap.",
        )

    yield PhaseEvent("parsing")

    # Context-managed tempfile: docling needs a `Path`, and this guarantees the
    # bytes are cleaned up even if the parse crashes (no disk leak). The suffix is
    # preserved so `parse_to_markdown`'s mimetypes-based dispatch classifies it.
    suffix = Path(filename).suffix
    with tempfile.NamedTemporaryFile(suffix=suffix) as tmp:
        tmp.write(raw_bytes)
        tmp.flush()
        path = Path(tmp.name)

        # Cheap pre-parse page-cap reject for PDFs (best-effort; the authoritative
        # check is from the post-parse `page_routes` below). A non-PDF or an
        # unreadable header simply returns None and defers to the post-parse count.
        pre_pages = _pdf_page_count(path)
        if pre_pages is not None and pre_pages > settings.max_pages:
            raise PipelineError(
                413,
                f"Document has {pre_pages} pages, over the "
                f"{settings.max_pages}-page cap.",
            )

        result = parse_to_markdown(path)

    # --- error-translation table (single source of truth) --------------------
    error = _error_for(result, settings)
    if error is not None:
        raise error

    page_routes: list[dict[str, Any]] = result.get("page_routes", []) or []
    warnings = _summarize_warnings(result, page_routes)
    call_counts: dict[str, int] = result.get("call_counts", {}) or {}

    _log_advisory_cost(call_counts, settings)

    # --- per-page frames (Task Group 6): a POST-HOC per-page MANIFEST ----------
    # Emitted AFTER the parse has already completed, one per entry in the parsed
    # `page_routes`, reporting each page's index (1..N) and its final route. This
    # is NOT live mid-parse progress (the vendored pipeline exposes no page-level
    # callback — the docling long-pole is a single opaque span); it is a faithful
    # per-page manifest that arrives in a burst once parsing is done. No fabricated
    # increments. The drain `POST /parse` ignores these frames, so its result is
    # byte-identical to before this task group. See the `PhaseEvent` HONESTY note.
    total_pages = len(page_routes)
    for index, route_entry in enumerate(page_routes, start=1):
        yield PhaseEvent(
            "page",
            current=index,
            total=total_pages,
            route=route_entry.get("route"),
        )

    yield ResultEvent(
        markdown=result.get("markdown", "") or "",
        confidence=result.get("confidence", {}) or {},
        page_count=len(page_routes),
        page_routes=page_routes,
        call_counts=call_counts,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Error-translation table
# ---------------------------------------------------------------------------


def _error_for(result: dict[str, Any], settings: Settings) -> PipelineError | None:
    """Map a `parse_to_markdown` result to a `PipelineError`, or None for 200.

    The ONE source of truth for the status table. Ordering matters: the 413/400
    document-level guards run before the 502/422 content checks (an unsupported
    type produces empty markdown too, so it must be caught as 400, not 422); and
    the retryable 502 runs before the 422 empty-content check (a doc that is empty
    ONLY because escalation was transiently down should say "retry me", not
    "permanently unprocessable").
    """
    warnings: list[dict[str, Any]] = result.get("warnings", []) or []
    page_routes: list[dict[str, Any]] = result.get("page_routes", []) or []
    markdown: str = result.get("markdown", "") or ""
    codes = {w.get("code") for w in warnings if isinstance(w, dict)}

    # 413 — oversized. The vendored package's own stat-based guard fires
    # `input_too_large`; our own page cap is authoritative from the result.
    if "input_too_large" in codes:
        return PipelineError(413, _first_message(warnings, "input_too_large"))
    page_count = len(page_routes)
    if page_count > settings.max_pages:
        return PipelineError(
            413,
            f"Document produced {page_count} pages, over the "
            f"{settings.max_pages}-page cap.",
        )

    # 400 — unsupported / unknown type.
    if "unsupported_type" in codes:
        return PipelineError(400, _first_message(warnings, "unsupported_type"))

    # 502 — ALL escalation calls failed transiently AND those pages are the
    # majority of the document. Runs BEFORE the 422 empty check.
    transient_error = _transient_majority_error(page_routes, page_count)
    if transient_error is not None:
        return transient_error

    # 422 — empty markdown OR every page errored (no usable content to index).
    if not markdown.strip():
        return PipelineError(
            422,
            "Parsing produced no extractable content (empty markdown); refusing "
            "to index nothing.",
        )

    # 200 — non-empty markdown (warnings ride along in the ResultEvent).
    return None


def _transient_majority_error(
    page_routes: list[dict[str, Any]], page_count: int
) -> PipelineError | None:
    """502 iff every escalation call failed transiently AND those pages dominate.

    "ALL escalation calls failed with a transient error" = at least one page had
    an escalation call attempted, and EVERY attempted page fell back with
    `reason == "throttled"` (no engine success, no non-transient/hard fallback).
    A single engine success (`vlm`/`textract`), a kept-engine arbitration, or a
    hard/permanent fallback breaks the "all transient" precondition → not 502.

    SIGNAL PROVENANCE — what is real vs. best-effort here:
      * `route in {vlm,textract,*-rejected-kept-docling}` (an escalation call
        SUCCEEDED) and `route in *-fallback-docling` (an escalation call was made
        and returned nothing usable) are REAL, first-class route-vocabulary
        signals.
      * `reason == "throttled"` is the REAL transient marker: BOTH escalation
        clients tag every EXHAUSTED transient failure — throttle, timeout,
        connection reset, and 5xx alike (see `parser_service.retry.is_transient`)
        — with `error_kind="throttled"`, which `markdown_pipeline` records as
        `reason="throttled"`. So this covers the whole network/throttle/5xx family,
        not just literal HTTP 429.
      * LIMITATION (best-effort): the vendored result does NOT distinguish a
        transient fallback from a hard page by content weight, and a transiently-
        failed page ships only whatever Docling salvaged (often little/none, since
        the page escalated BECAUSE Docling under-served it). So the shipped
        `n_chars` UNDER-represents the content that the transient failure lost.
        "Majority of content" is therefore measured by PAGE COUNT — the honest
        proxy given the available signals — NOT by shipped `n_chars`. This is the
        only best-effort part of the 502 row; the transient-vs-hard discrimination
        itself is a real signal.
    """
    if page_count == 0:
        return None

    attempted = 0
    transient = 0
    for route_entry in page_routes:
        route = route_entry.get("route")
        if route in _ESCALATION_SUCCESS_ROUTES or route in _ESCALATION_REJECTED_ROUTES:
            attempted += 1  # an engine call succeeded — breaks "all transient"
        elif route in _ESCALATION_FALLBACK_ROUTES:
            attempted += 1
            if route_entry.get("reason") == _TRANSIENT_REASON:
                transient += 1

    if attempted == 0:
        return None  # no escalation happened at all — nothing to be down
    if transient != attempted:
        return None  # some call succeeded or failed hard — not "ALL transient"
    if transient * 2 <= page_count:
        return None  # transient failures are a minority — Docling carried the doc

    return PipelineError(
        502,
        "The escalation engine was unreachable (transient failures) on "
        f"{transient}/{page_count} pages, the majority of the document; retry.",
    )


# ---------------------------------------------------------------------------
# Warnings + advisory cost (both NEVER gate)
# ---------------------------------------------------------------------------


def _summarize_warnings(
    result: dict[str, Any], page_routes: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Return the vendored warnings plus a LOUD `N/M pages fell back to Docling`.

    Partial degradation is never silent: whenever any page fell back to Docling
    (engine errored / emptied / was down), a compact document-scope summary is
    appended so a caller sees the fallback without decoding the route vocabulary.
    """
    warnings: list[dict[str, Any]] = list(result.get("warnings", []) or [])
    fallback_pages = sum(
        1 for r in page_routes if r.get("route") in _ESCALATION_FALLBACK_ROUTES
    )
    if fallback_pages:
        warnings.append(
            {
                "code": "docling_fallback_summary",
                "message": f"{fallback_pages}/{len(page_routes)} pages fell back to Docling",
                "scope": "document",
            }
        )
    return warnings


def _log_advisory_cost(call_counts: dict[str, int], settings: Settings) -> None:
    """Log a PROVISIONAL, ADVISORY-ONLY Textract cost summary. NEVER gates.

    `PARSER_BUDGET_USD` is disabled by default; the page cap × concurrency IS the
    POC spend bound. This is a read-only estimate using the PROVISIONAL Textract
    price ($0.019/page LAYOUT+TABLES — live ap-southeast-2 verification is an ops
    follow-up); it does not touch the result and cannot fail a parse.
    """
    textract_calls = call_counts.get("textract", 0) or 0
    if textract_calls:
        estimate = textract_calls * settings.textract_price_per_page_usd
        logger.info(
            "advisory cost (PROVISIONAL, NON-GATING): %d Textract page(s) x "
            "$%.3f/page = ~$%.4f; PARSER_BUDGET_USD=%s",
            textract_calls,
            settings.textract_price_per_page_usd,
            estimate,
            settings.budget_usd,
        )


def _first_message(warnings: list[dict[str, Any]], code: str) -> str:
    """Return the first warning message for `code` (a stable client-facing detail)."""
    for warning in warnings:
        if isinstance(warning, dict) and warning.get("code") == code:
            return str(warning.get("message", code))
    return code


def _pdf_page_count(path: Path) -> int | None:
    """Best-effort pre-parse PDF page count for an early page-cap reject.

    Returns None for non-PDFs or when pypdf cannot read the header — the caller
    then relies on the authoritative post-parse `page_routes` count. Never raises.
    """
    if path.suffix.lower() != ".pdf":
        return None
    try:
        from pypdf import PdfReader

        return len(PdfReader(str(path)).pages)
    except Exception:  # noqa: BLE001 — best-effort probe; defer to post-parse count
        return None
