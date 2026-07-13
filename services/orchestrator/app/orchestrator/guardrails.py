"""
Input/output guardrail handling stage (system-prompt-leakage input guard).

Layer 1 of the guard: a config-gated, deterministic prompt-leak/injection
pre-filter → an INJECTED Bedrock Haiku classifier (called ONLY on a pre-filter
hit) → a structured allow/block decision. When it blocks it raises
:class:`GuardrailTripwire` carrying a :class:`GuardrailDecision`; the router
turns that into a 200 canned refusal (NOT an error — a tripwire is a
successful, honest refusal and must never reach the 5xx handlers).

Purity: this stage is a PURE function of its typed inputs and the injected
classifier — it imports NO infra library (boto3/opensearch/OTel). The injected
classifier is described with a :class:`typing.Protocol` (structural typing) so
NO ``app.clients`` import is needed (the ``stages-pure`` import-linter contract
and ``check_stages_grep_gate.sh`` stay green; the client is injected exactly
like ``bedrock``/``search`` are into the other stages).

``check_output`` stays a parked typed identity in this slice — the output
guard (roadmap Phase 3 item 10) is out of scope.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Protocol

from app.schemas.envelope import GuardrailDecision
from app.schemas.pipeline_config import GuardrailsPin

# The single canned refusal the Chainlit UI renders verbatim on a block.
REFUSAL_TEXT: Final[str] = "I'm sorry, but I can't help with that request."

# Guard identity for the prompt-leak/injection class (this slice's only class).
_INPUT_STAGE: Final[str] = "input"
_BLOCK_CATEGORY: Final[str] = "prompt_leak"
_BLOCK_RULE_ID: Final[str] = "prompt-leak-v1"
_FAIL_SAFE_RATIONALE: Final[str] = (
    "guard classifier unavailable on a pre-filter-flagged input — failing safe"
)
_DEFAULT_BLOCK_RATIONALE: Final[str] = "system-prompt extraction attempt"


class ClassifierVerdict(Protocol):
    """Structural shape of the injected classifier's plain-data verdict."""

    unsafe: bool
    rationale: str | None
    input_tokens: int | None
    output_tokens: int | None


@dataclass(frozen=True)
class GuardInputResult:
    """
    Outcome of the input guard: the (unchanged) question + classifier-call telemetry.

    ``model_id``/``input_tokens``/``output_tokens`` are populated ONLY when the
    classifier actually ran (a pre-filter hit classified SAFE); a gate-off or
    pre-filter-miss result carries just the question. The router attaches the
    telemetry to the ``guardrail_input`` LLM span so the Haiku guard call is
    visible in Phoenix (model id, token counts, latency) on a SAFE allow — not
    only on a block.
    """

    question: str
    model_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


class GuardClassifier(Protocol):
    """
    Structural type of the injected Bedrock Haiku guard classifier.

    Matches ``app.clients.guardrail.GuardClassifier`` WITHOUT importing it — the
    stage stays free of any boto3-carrying module (``stages-pure``). The client
    is injected by the router exactly like ``bedrock``/``search``.
    """

    def classify(self, question: str, *, model_id: str) -> ClassifierVerdict:
        """Classify one user turn for system-prompt-leakage / injection."""
        ...


# Deterministic pre-filter: a curated pattern set for the prompt-leak /
# prompt-injection extraction class. A MISS returns the question unchanged with
# NO paid classifier call (cost ≈ 0 on normal traffic); a HIT is a *candidate*
# that the Haiku classifier then confirms or clears.
_PREFILTER_PATTERNS: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"system prompt",
        r"your (instructions|prompt|rules|guidelines)",
        r"ignore (all |the )?(previous|prior|above|preceding)",
        r"disregard .*(instruction|prompt|rule)",
        r"repeat .*(above|verbatim|prompt|instruction)",
        r"reveal .*(prompt|instruction|rule)",
        r"(print|show|output|display) .*(prompt|instruction|the text above)",
        r"(what|repeat) .*(text|words) above",
        r"initial (instructions|prompt)",
    )
)


class GuardrailTripwire(Exception):
    """
    A successful, honest refusal — NOT an error.

    Raised by :func:`check_input` when the input guard blocks; carries the
    :class:`GuardrailDecision` the router attaches to the refusal envelope's
    ``guardrail_decisions[]`` and records as a guardrail span. It must NEVER be
    routed through the app-level 5xx exception handlers (the router catches it
    specifically and returns a 200 canned refusal).
    """

    def __init__(
        self,
        decision: GuardrailDecision,
        *,
        model_id: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> None:
        """
        Carry the block decision + guard-call telemetry to the router.

        The router records both on the ``guardrail_input`` LLM span; ``model_id``
        and tokens are ``None`` on a fail-safe block (no verdict).
        """
        super().__init__(decision.rationale or REFUSAL_TEXT)
        self.decision = decision
        self.model_id = model_id
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class GuardMisconfiguredError(RuntimeError):
    """
    The guard is ENABLED but its classifier client / model id was never injected.

    A deploy/wiring error, NOT a per-request classifier failure — raised LOUDLY
    (it propagates to a 500, distinct from :class:`GuardrailTripwire`) instead of
    being masked as a fail-safe refusal, so a whole class of queries is never
    silently blocked by a misconfiguration. In the real build path the classifier
    is ALWAYS constructed (``build_app_clients`` makes no paid call), so this
    signals a genuine wiring bug rather than any expected runtime condition.
    """


def _block_decision(rationale: str) -> GuardrailDecision:
    """Build the structured ``block`` decision for the prompt-leak class."""
    return GuardrailDecision(
        stage=_INPUT_STAGE,
        decision="block",
        category=_BLOCK_CATEGORY,
        rule_id=_BLOCK_RULE_ID,
        rationale=rationale,
    )


def _prefilter_hit(question: str) -> bool:
    """Return True when any curated prompt-leak/injection pattern matches."""
    return any(pattern.search(question) for pattern in _PREFILTER_PATTERNS)


def prefilter_hit(question: str, pins: GuardrailsPin) -> bool:
    """
    Report whether the guard is enabled AND a curated pattern matches.

    A True result means the classifier WILL run for this question. The router
    uses this to open the ``guardrail_input`` LLM span only around an actual
    classifier call (so a benign pre-filter miss adds no span), then calls
    :func:`check_input` as usual. ``check_input`` re-checks the pre-filter itself,
    so it stays fully self-contained (callable without the router's gate).
    """
    return pins.enabled and _prefilter_hit(question)


def check_input(
    question: str,
    *,
    pins: GuardrailsPin,
    classifier: GuardClassifier | None,
) -> GuardInputResult:
    """
    Config-gated system-prompt-leakage input guard (allows with telemetry, or blocks).

    Args:
        question: The user turn (the current question, pre-rewrite).
        pins: The resolved config's ``guardrails`` block. ``enabled=False`` ⇒
            a typed IDENTITY: return the ``question`` unchanged, NO classifier
            call (the eval/1.0.0/1.1.0/1.2.0 path — behavior identical to today).
        classifier: The injected Bedrock Haiku guard classifier (or ``None``).

    Returns:
        A :class:`GuardInputResult` carrying the unchanged ``question`` when
        allowed (gate off, pre-filter miss, or pre-filter hit classified SAFE);
        the classifier-call telemetry (model id + token counts) is populated
        only when the classifier actually ran.

    Raises:
        GuardrailTripwire: On a pre-filter hit that the classifier flags UNSAFE,
            OR any classifier error on that (already suspicious) input — fail
            SAFE. Carries the guard call's telemetry for the span. Normal traffic
            never calls the classifier, so an outage can neither refuse benign
            queries nor add cost to them.
        GuardMisconfiguredError: Guard enabled but no classifier/model wired.

    """
    if not pins.enabled:
        return GuardInputResult(question=question)
    if not _prefilter_hit(question):
        return GuardInputResult(question=question)

    # Misconfiguration (guard enabled but no classifier client / model id wired)
    # is a DEPLOY error, NOT a per-request classifier failure. Raise it LOUDLY
    # (→ 500) so a wiring bug can never masquerade as a silent refusal of every
    # flagged query. Deliberately OUTSIDE the fail-safe try below.
    if classifier is None or not pins.classifier_model_id:
        raise GuardMisconfiguredError(
            "guardrails.enabled is true but no classifier client / model id was "
            "injected — the input guard cannot run."
        )

    # Suspicious candidate: the classifier confirms or clears it. A RUNTIME
    # classifier failure (throttle, ClientError, unparseable verdict) on this
    # already-flagged input fails SAFE (BLOCK) — no verdict, so no token counts.
    try:
        verdict = classifier.classify(question, model_id=pins.classifier_model_id)
    except Exception as exc:  # noqa: BLE001 — fail-safe: runtime classifier failure ⇒ block
        raise GuardrailTripwire(
            _block_decision(_FAIL_SAFE_RATIONALE), model_id=pins.classifier_model_id
        ) from exc

    if verdict.unsafe:
        raise GuardrailTripwire(
            _block_decision(verdict.rationale or _DEFAULT_BLOCK_RATIONALE),
            model_id=pins.classifier_model_id,
            input_tokens=verdict.input_tokens,
            output_tokens=verdict.output_tokens,
        )
    return GuardInputResult(
        question=question,
        model_id=pins.classifier_model_id,
        input_tokens=verdict.input_tokens,
        output_tokens=verdict.output_tokens,
    )


def check_output(answer_text: str) -> str:
    """Typed identity on the output answer text (output guard parked, item 10)."""
    return answer_text
