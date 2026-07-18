"""
Live-arm builders + pre-pass capture for the guardrail parity A/B (dev/-only).

The A/B LIBRARY (``app.phoenix.guardrail_ab``) is complete and arm-agnostic: it
runs two :class:`~app.phoenix.guardrail_ab.GuardArm`s over a corpus and records
a Phoenix dataset + one experiment per arm. This module supplies the missing live
pieces the driver (``scripts/guardrail_parity_gate.py``) wires together:

  * :func:`load_legal_corpus` — load the committed ~30-row labelled question corpus
    (mostly benign criminal-law questions + a handful engineered to try to elicit a
    policy-violating / ungrounded ANSWER) into :class:`CorpusItem`s. The rows are
    QUESTIONS, never crafted answers — if the generator self-refuses, that refusal
    IS the honest data (the corpus is NOT engineered to make NeMo win).
  * :func:`capture_arm` — the END-TO-END PRE-PASS: POST ``/query`` ONCE per question
    on a config pin (arm A = ``1.4.0`` in-house guard, arm B = nemo-all ``1.8.0``)
    and read each config's REAL generated answer + retrieved chunks + guard decision
    from the response envelope (``guardrail_decisions`` + ``timings_ms``; token
    telemetry is span-only on the current ``/query`` envelope, so it is captured
    "as available" and 0 otherwise). NO crafted answers are ever injected.
  * :func:`build_in_house_arm` / :func:`build_nemo_arm` — wrap a captured pre-pass
    into a REPLAY :class:`GuardArm`: its ``check`` returns the captured
    :class:`~app.phoenix.guardrail_ab.GuardOutcome` for the question, so the harness
    records exactly the decisions the live pod produced without re-billing Bedrock.

One-way dependency rule (honoured): this ``dev/`` module MAY import ``app.*`` (the
harness types) and drives the orchestrator over HTTP ONLY — nothing under
``services/orchestrator`` is imported here, and nothing in ``app`` imports ``dev``.

The HTTP transport mirrors ``dev/stubs/rag/orchestrator_query.py`` (env-driven
``ORCHESTRATOR_URL``; a non-200 raises), but the pre-pass keeps the FULL envelope
(``guardrail_decisions`` is a top-level sibling of ``result``) rather than unwrapping
to ``result`` alone.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import httpx
from app.phoenix.guardrail_ab import (
    BENIGN_CLASS,
    VERDICT_ALLOW,
    CorpusItem,
    GuardOutcome,
)

# Env surface (mirrors dev/stubs/rag/orchestrator_query.py; read at call time).
ENV_URL: Final[str] = "ORCHESTRATOR_URL"
DEFAULT_URL: Final[str] = "http://localhost:8000"

# One live /query embeds, retrieves and generates; Bedrock throttle-retries can
# stretch a single call well past httpx's default (same rationale as the sibling
# orchestrator_query adapter).
REQUEST_TIMEOUT_S: Final[float] = 300.0

# The committed labelled A/B QUESTION corpus (kept SEPARATE from the crafted
# capability corpus that lives inline in the guardrail capability script).
DEFAULT_CORPUS_PATH: Final[Path] = (
    Path(__file__).parent / "fixtures" / "data" / "guardrail_ab" / "legal_questions.jsonl"
)


class OrchestratorQueryError(RuntimeError):
    """A non-200 response from the orchestrator's ``POST /query`` (pre-pass)."""

    def __init__(self, status_code: int, body: str) -> None:
        """Build the message from the HTTP status code and response body."""
        super().__init__(
            f"Orchestrator POST /query failed with HTTP {status_code}: {body} "
            "— refusing to score a failed pre-pass query."
        )
        self.status_code = status_code
        self.body = body


def load_legal_corpus(path: Path | None = None) -> list[CorpusItem]:
    """
    Load the committed labelled A/B question corpus into :class:`CorpusItem`s.

    Each JSONL row carries ``query_id``, ``question``, an ``is_benign`` label, and
    the Phase-2 gate labels ``attack_class`` (:data:`~app.phoenix.guardrail_ab.BENIGN_CLASS`
    or an attack class) + ``expected`` (the verdict the row SHOULD receive
    end-to-end). ``answer`` / ``chunks`` are left EMPTY here — they are filled by
    the pre-pass (:func:`capture_arm`) from each config's REAL ``/query`` envelope,
    never crafted.

    This is the QUESTION lane of the parity set: benign + jailbreak/prompt-leak +
    policy + grounding, i.e. the classes whose verdict needs a live Bedrock rail.
    The deterministic classes (secrets / high-sev PII / low-sev PII) are the ANSWER
    lane and are scored against the pod's real detectors in the pod's own gate
    suite — a `/query` pre-pass cannot make a legal generator emit an SSN on demand,
    and crafting one would be exactly the engineered corpus this harness refuses.

    Args:
        path: Corpus JSONL path (defaults to the committed dev fixture).

    Returns:
        The corpus items in file order.

    """
    corpus_path = path or DEFAULT_CORPUS_PATH
    items: list[CorpusItem] = []
    for line in corpus_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        items.append(
            CorpusItem(
                query_id=str(row["query_id"]),
                question=str(row["question"]),
                is_benign=bool(row.get("is_benign", True)),
                attack_class=str(row.get("attack_class", BENIGN_CLASS)),
                expected=str(row.get("expected", VERDICT_ALLOW)),
            )
        )
    return items


@dataclass(frozen=True, slots=True)
class CapturedArm:
    """
    One config's PRE-PASS capture: the REAL per-question guard outcomes + answers.

    Attributes:
        config_ref: The pipeline config the pre-pass exercised.
        outcomes: Captured :class:`GuardOutcome` keyed by question text.
        answers: The REAL generated answer keyed by question text.
        chunks: The retrieved grounding chunk texts keyed by question text.

    """

    config_ref: str
    outcomes: dict[str, GuardOutcome]
    answers: dict[str, str]
    chunks: dict[str, tuple[str, ...]]


def _post_query(
    question: str,
    config_ref: str,
    *,
    base_url: str,
    transport: httpx.BaseTransport | None,
) -> dict[str, Any]:
    """POST one ``/query`` and return the FULL envelope (non-200 raises)."""
    payload = {"question": question, "pipeline_config": config_ref}
    with httpx.Client(base_url=base_url, timeout=REQUEST_TIMEOUT_S, transport=transport) as client:
        response = client.post("/query", json=payload)
    if response.status_code != 200:
        raise OrchestratorQueryError(response.status_code, response.text)
    return response.json()


def _guard_latency_ms(timings_ms: dict[str, Any]) -> float:
    """
    Sum every guard-lane latency in ``timings_ms`` (keys starting ``guardrail``).

    Honest, arm-agnostic total guard round-trip latency: the in-house arm bills a
    ``guardrail`` (input) + ``guardrail_output`` (regex) lane; the NeMo arm bills
    ``guardrail_nemo_output``. Non-guard timings (embedding/retrieval/generation)
    are excluded.
    """
    total = 0.0
    for key, value in timings_ms.items():
        if str(key).startswith("guardrail"):
            try:
                total += float(value)
            except (TypeError, ValueError):
                continue
    return total


def outcome_from_envelope(envelope: dict[str, Any]) -> GuardOutcome:
    """
    Build a :class:`GuardOutcome` from ONE ``/query`` envelope (pure).

    ``blocked`` / ``flag`` come from ``guardrail_decisions``; ``latency_ms`` from
    the summed guard lanes in ``result.timings_ms``. Per-call token telemetry is
    span-only on the current ``/query`` envelope, so it is read "as available" from
    an optional top-level ``guard_telemetry`` slot (0 / "" otherwise) — the
    envelope-only pre-pass never fabricates a token count it did not observe.
    """
    result = envelope.get("result") or {}
    decisions = envelope.get("guardrail_decisions") or []
    blocked = any(d.get("decision") == "block" for d in decisions)
    flag = any(d.get("decision") == "flag" for d in decisions)
    timings = result.get("timings_ms") or {}
    telem = envelope.get("guard_telemetry") or {}
    return GuardOutcome(
        blocked=blocked,
        latency_ms=_guard_latency_ms(timings),
        input_tokens=int(telem.get("input_tokens", 0)),
        output_tokens=int(telem.get("output_tokens", 0)),
        model_id=str(telem.get("model_id", "")),
        flag=flag,
    )


def _answer_and_chunks(envelope: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
    """Pull the REAL generated answer text + retrieved chunk texts from an envelope."""
    result = envelope.get("result") or {}
    answer = str((result.get("answer") or {}).get("text", ""))
    chunks = tuple(
        str(chunk.get("text", "")) for chunk in (result.get("retrieved_chunks") or [])
    )
    return answer, chunks


def capture_arm(
    config_ref: str,
    corpus: list[CorpusItem],
    *,
    base_url: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> CapturedArm:
    """
    Run the END-TO-END PRE-PASS for one config over the corpus (real answers).

    POSTs ``/query`` ONCE per question on ``config_ref`` and captures each REAL
    generated answer + retrieved chunks + guard decision from the envelope. No
    crafted answers are ever supplied — the arm judges the model's own output.

    Args:
        config_ref: The pipeline config pin to run the pre-pass on.
        corpus: The labelled question corpus (from :func:`load_legal_corpus`).
        base_url: Orchestrator base URL (defaults to ``ORCHESTRATOR_URL`` / localhost).
        transport: Injectable httpx transport for tests (mocked HTTP only).

    Returns:
        A :class:`CapturedArm` with per-question outcomes + real answers + chunks.

    """
    url = (base_url or os.getenv(ENV_URL, DEFAULT_URL)).rstrip("/")
    outcomes: dict[str, GuardOutcome] = {}
    answers: dict[str, str] = {}
    chunks: dict[str, tuple[str, ...]] = {}
    for item in corpus:
        envelope = _post_query(item.question, config_ref, base_url=url, transport=transport)
        outcomes[item.question] = outcome_from_envelope(envelope)
        answers[item.question], chunks[item.question] = _answer_and_chunks(envelope)
    return CapturedArm(config_ref=config_ref, outcomes=outcomes, answers=answers, chunks=chunks)


@dataclass(frozen=True, slots=True)
class _ReplayArm:
    """
    A :class:`GuardArm` that REPLAYS a config's captured pre-pass outcomes.

    The harness calls ``check(question, answer, chunks)`` per corpus item; this arm
    ignores the passed answer/chunks and returns the :class:`GuardOutcome` captured
    for that question during the pre-pass — so the recorded experiment reflects the
    REAL live pod decision without re-billing Bedrock. A question with no captured
    outcome (should not happen for a corpus captured in the same run) records a
    non-blocking error row rather than raising.
    """

    config_ref: str
    _outcomes: dict[str, GuardOutcome]

    def check(self, *, question: str, answer: str, chunks: tuple[str, ...]) -> GuardOutcome:
        """Replay the captured outcome for ``question`` (answer/chunks unused)."""
        _ = (answer, chunks)  # documented: the replay keys off the question only
        outcome = self._outcomes.get(question)
        if outcome is None:
            return GuardOutcome(blocked=False, latency_ms=0.0, error="no captured outcome")
        return outcome


def build_in_house_arm(config_ref: str, captured: CapturedArm) -> _ReplayArm:
    """
    Build the IN-HOUSE (arm A, ``1.4.0``) replay arm from its pre-pass capture.

    Arm A exercises the shipped deterministic in-house input/output guard via
    ``/query`` on the in-house config pin; this wraps that capture into a replay
    :class:`GuardArm`.
    """
    return _ReplayArm(config_ref=config_ref, _outcomes=dict(captured.outcomes))


def build_nemo_arm(config_ref: str, captured: CapturedArm) -> _ReplayArm:
    """
    Build the NeMo (arm B, nemo-all ``1.8.0``) replay arm from its pre-pass capture.

    Arm B exercises the out-of-process nemo-all lane (input + output + facts) via
    ``/query`` on the nemo-all config pin (``1.8.0``); this wraps that capture into
    a replay :class:`GuardArm`. Structurally identical to :func:`build_in_house_arm`
    — the harness is arm-agnostic; the only difference is WHICH config each captured
    against (the experiment provenance).
    """
    return _ReplayArm(config_ref=config_ref, _outcomes=dict(captured.outcomes))


def corpus_with_captured_answers(
    corpus: list[CorpusItem], captured: CapturedArm
) -> list[CorpusItem]:
    """
    Return the corpus with each item's REAL captured answer + chunks filled in.

    The Phoenix dataset records ONE answer per row; we record the arm-under-scrutiny
    (NeMo) capture so the dataset shows the answer the grounding/policy rails judged.
    Both arms still replay their OWN captured outcomes via their replay arm.
    """
    filled: list[CorpusItem] = []
    for item in corpus:
        filled.append(
            CorpusItem(
                query_id=item.query_id,
                question=item.question,
                answer=captured.answers.get(item.question, ""),
                chunks=captured.chunks.get(item.question, ()),
                is_benign=item.is_benign,
                attack_class=item.attack_class,
                expected=item.expected,
            )
        )
    return filled
