"""
Phoenix score write-back annotations (API spec section 8.2).

After scoring, the eval worker writes annotations back to Phoenix so the UI shows
the judge's verdicts. Phoenix is the *convenience view*; RDS remains the results
of record, so write-back is BEST-EFFORT: failures are logged (and retried) but
NEVER fail the run.

This module is split for testability:
- ``label_for_score`` / ``build_span_annotations`` / ``build_document_annotations``
  are PURE payload builders, unit-tested with a mocked client (no live server).
- ``write_back_annotations`` performs the actual HTTP write-back via the Phoenix
  bulk span API + ``add_document_annotation``; the live path is exercised only
  behind ``@pytest.mark.phoenix_integration``.

Span-level annotations per metric use ``annotator_kind="LLM"``, the numeric
``score``, a ``label`` (``pass``/``fail`` vs the metric threshold), and the judge
rationale as ``explanation`` (bulk API). Document-level per-chunk
contextual-relevancy verdicts use ``add_document_annotation`` with a
``document_position``.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Final

from beartype import beartype

# The pass thresholds are kernel data (single source of truth); import them back.
from crucible.kernel.rag_metrics.metric_specs import DEFAULT_METRIC_THRESHOLDS

_LOGGER: Final[logging.Logger] = logging.getLogger(__name__)

_ANNOTATOR_KIND_LLM: Final[str] = "LLM"
_DEFAULT_THRESHOLD: Final[float] = 0.7
_DEFAULT_DOC_ANNOTATION_NAME: Final[str] = "contextual_relevancy"
_RETRY_BACKOFF_SECONDS: Final[float] = 0.1


@beartype
def label_for_score(score: float, *, threshold: float = _DEFAULT_THRESHOLD) -> str:
    """
    Return the pass/fail label for a score versus a threshold.

    Args:
        score: The numeric metric score.
        threshold: The pass threshold (score >= threshold -> ``pass``).

    Returns:
        ``"pass"`` if ``score >= threshold`` else ``"fail"``.

    """
    return "pass" if score >= threshold else "fail"


@beartype
def build_span_annotations(
    *,
    span_id: str,
    scores: dict[str, float],
    reasoning: dict[str, str] | None = None,
    thresholds: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """
    Build the bulk span-annotation payloads, one per metric.

    Args:
        span_id: The Phoenix span id to annotate.
        scores: ``metric_name -> score`` mapping.
        reasoning: Optional ``metric_name -> judge rationale`` for ``explanation``.
        thresholds: Optional per-metric thresholds (defaults to the kernel's
            ``DEFAULT_METRIC_THRESHOLDS`` then ``0.7``).

    Returns:
        A list of ``SpanAnnotationData``-shaped dicts for the bulk API.

    """
    reasoning = reasoning or {}
    thresholds = thresholds or {}
    payloads: list[dict[str, Any]] = []
    for metric_name, score in scores.items():
        threshold = thresholds.get(
            metric_name, DEFAULT_METRIC_THRESHOLDS.get(metric_name, _DEFAULT_THRESHOLD)
        )
        payloads.append(
            {
                "name": metric_name,
                "span_id": span_id,
                "annotator_kind": _ANNOTATOR_KIND_LLM,
                "result": {
                    "score": score,
                    "label": label_for_score(score, threshold=threshold),
                    "explanation": reasoning.get(metric_name),
                },
            }
        )
    return payloads


@beartype
def build_document_annotations(
    *,
    span_id: str,
    chunk_verdicts: list[dict[str, Any]],
    annotation_name: str = _DEFAULT_DOC_ANNOTATION_NAME,
    threshold: float = _DEFAULT_THRESHOLD,
) -> list[dict[str, Any]]:
    """
    Build per-chunk document-annotation payloads with ``document_position``.

    Args:
        span_id: The retrieval span id whose documents are annotated.
        chunk_verdicts: Per-chunk verdicts in rank order; each is a dict with at
            least ``score`` and optional ``explanation``.
        annotation_name: Annotation name (default contextual-relevancy).
        threshold: Pass threshold for the per-chunk label.

    Returns:
        A list of ``add_document_annotation`` kwargs dicts (one per chunk).

    """
    docs: list[dict[str, Any]] = []
    for position, verdict in enumerate(chunk_verdicts):
        score = float(verdict.get("score", 0.0))
        docs.append(
            {
                "span_id": span_id,
                "document_position": position,
                "annotation_name": annotation_name,
                "annotator_kind": _ANNOTATOR_KIND_LLM,
                "label": label_for_score(score, threshold=threshold),
                "score": score,
                "explanation": verdict.get("explanation"),
            }
        )
    return docs


def _retry_best_effort(fn: Any, *, what: str, max_retries: int) -> None:
    """Call ``fn`` with bounded retries; log and swallow every failure."""
    attempt = 0
    while attempt <= max_retries:
        try:
            _ = fn()
            return
        except Exception as exc:  # noqa: BLE001 -- best-effort; never fail the run
            attempt += 1
            _LOGGER.warning(
                "[phoenix-annotations] %s write-back failed (attempt %d/%d): %s",
                what,
                attempt,
                max_retries + 1,
                exc,
            )
            if attempt > max_retries:
                _LOGGER.error("[phoenix-annotations] giving up on %s write-back", what)
                return
            # WARN: brief backoff on a best-effort retry; never blocks the run long.
            time.sleep(_RETRY_BACKOFF_SECONDS)


@beartype
def write_back_annotations(
    *,
    client: Any,
    span_id: str,
    scores: dict[str, float],
    reasoning: dict[str, str] | None = None,
    chunk_verdicts: list[dict[str, Any]] | None = None,
    thresholds: dict[str, float] | None = None,
    document_annotation_name: str = _DEFAULT_DOC_ANNOTATION_NAME,
    max_retries: int = 2,
) -> None:
    """
    Write span- and document-level annotations back to Phoenix (best-effort).

    Span-level annotations are written via the bulk API
    (``client.spans.log_span_annotations``); per-chunk contextual-relevancy
    verdicts via ``client.spans.add_document_annotation`` with
    ``document_position``. Every write is retried up to ``max_retries`` times and
    any failure is logged but NEVER raised (Phoenix is the convenience view).

    Args:
        client: A Phoenix client (real or mocked).
        span_id: The span id to annotate.
        scores: ``metric_name -> score`` mapping for the span-level annotations.
        reasoning: Optional ``metric_name -> judge rationale``.
        chunk_verdicts: Optional per-chunk relevancy verdicts in rank order.
        thresholds: Optional per-metric pass thresholds.
        document_annotation_name: Annotation name for the per-chunk verdicts.
        max_retries: Best-effort retry count per write call.

    """
    span_payloads = build_span_annotations(
        span_id=span_id, scores=scores, reasoning=reasoning, thresholds=thresholds
    )
    if span_payloads:
        _retry_best_effort(
            lambda: client.spans.log_span_annotations(span_annotations=span_payloads),
            what="span-level",
            max_retries=max_retries,
        )

    for doc in build_document_annotations(
        span_id=span_id,
        chunk_verdicts=chunk_verdicts or [],
        annotation_name=document_annotation_name,
    ):
        _retry_best_effort(
            lambda doc=doc: client.spans.add_document_annotation(**doc),
            what="document-level",
            max_retries=max_retries,
        )
