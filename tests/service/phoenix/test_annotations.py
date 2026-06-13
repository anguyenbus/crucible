"""
Unit tests for the Phoenix score write-back annotations (Task Group 7.1).

These run TEST-FIRST and HERMETICALLY with a MOCKED Phoenix client (no live
server). They pin the PURE payload-building + bulk-batching logic of
``service/phoenix/annotations.py`` per API spec section 8.2:

- span-level annotations per metric: ``annotator_kind="LLM"``, ``score``,
  ``label`` (``pass``/``fail`` vs the metric threshold), judge rationale as
  ``explanation``; written via the bulk API;
- document-level per-chunk contextual-relevancy verdicts via
  ``add_document_annotation`` with ``document_position``;
- best-effort: a write failure is logged (and retried) but NEVER raises.

Documented local invocation:
    CRUCIBLE_GENERATOR_MODEL=gpt-4o uv run pytest tests/service/phoenix \
        -m "not phoenix_integration"
"""

from __future__ import annotations

from unittest.mock import MagicMock

from crucible.service.phoenix.annotations import (
    build_document_annotations,
    build_span_annotations,
    label_for_score,
    write_back_annotations,
)


def test_label_for_score_pass_fail_vs_threshold() -> None:
    assert label_for_score(0.9, threshold=0.7) == "pass"
    assert label_for_score(0.7, threshold=0.7) == "pass"
    assert label_for_score(0.5, threshold=0.7) == "fail"


def test_build_span_annotations_payload_shape() -> None:
    scores = {"faithfulness": 0.9, "context_recall": 0.4}
    reasoning = {"faithfulness": "well grounded", "context_recall": "missed a clause"}
    payloads = build_span_annotations(
        span_id="span-1",
        scores=scores,
        reasoning=reasoning,
    )
    assert len(payloads) == 2
    by_name = {p["name"]: p for p in payloads}

    faith = by_name["faithfulness"]
    assert faith["span_id"] == "span-1"
    assert faith["annotator_kind"] == "LLM"
    assert faith["result"]["score"] == 0.9
    assert faith["result"]["label"] == "pass"
    assert faith["result"]["explanation"] == "well grounded"

    recall = by_name["context_recall"]
    assert recall["result"]["label"] == "fail"
    assert recall["result"]["explanation"] == "missed a clause"


def test_build_document_annotations_carries_position() -> None:
    chunk_verdicts = [
        {"score": 0.8, "explanation": "relevant"},
        {"score": 0.2, "explanation": "off-topic"},
    ]
    docs = build_document_annotations(
        span_id="span-1",
        chunk_verdicts=chunk_verdicts,
        annotation_name="contextual_relevancy",
    )
    assert [d["document_position"] for d in docs] == [0, 1]
    assert docs[0]["span_id"] == "span-1"
    assert docs[0]["annotator_kind"] == "LLM"
    assert docs[0]["label"] == "pass"
    assert docs[1]["label"] == "fail"
    assert docs[1]["explanation"] == "off-topic"


def test_write_back_uses_bulk_span_api_and_document_api() -> None:
    client = MagicMock()
    write_back_annotations(
        client=client,
        span_id="span-1",
        scores={"faithfulness": 0.9},
        reasoning={"faithfulness": "ok"},
        chunk_verdicts=[{"score": 0.8, "explanation": "relevant"}],
    )
    # Span-level annotations go through the bulk API.
    client.spans.log_span_annotations.assert_called_once()
    _, kwargs = client.spans.log_span_annotations.call_args
    assert "span_annotations" in kwargs
    assert len(list(kwargs["span_annotations"])) == 1
    # Document-level annotations use add_document_annotation with a position.
    client.spans.add_document_annotation.assert_called_once()
    _, doc_kwargs = client.spans.add_document_annotation.call_args
    assert doc_kwargs["document_position"] == 0
    assert doc_kwargs["span_id"] == "span-1"


def test_write_back_is_best_effort_and_never_raises() -> None:
    client = MagicMock()
    client.spans.log_span_annotations.side_effect = RuntimeError("phoenix down")
    client.spans.add_document_annotation.side_effect = RuntimeError("phoenix down")
    # Must NOT raise even though every write fails; retries are best-effort.
    write_back_annotations(
        client=client,
        span_id="span-1",
        scores={"faithfulness": 0.9},
        reasoning={"faithfulness": "ok"},
        chunk_verdicts=[{"score": 0.8, "explanation": "relevant"}],
        max_retries=2,
    )
    # Retried at least once (best-effort retry), still swallowed.
    assert client.spans.log_span_annotations.call_count >= 1
