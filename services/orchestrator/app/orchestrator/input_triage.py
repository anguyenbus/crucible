"""
Group 4 input-triage pure stage: the UNCONDITIONAL shadow triage observation.

The Haiku triage rail (``ATTACK`` / ``OFFTOPIC`` / ``OK``) ships SHADOW-first: on
the guarded lane EVERY question is triaged (not only pre-filter hits), the verdict
is RECORDED + persisted via the Group 2 :func:`record_verdict`, and it does NOT
block/redirect real traffic. The still-enforcing ``self_check_input`` lane is
UNTOUCHED — the triage runs as a SEPARATE, rail-selected pod call
(``/check/input/triage``), so the two never interfere.

Purity: like ``app.orchestrator.guardrails`` / ``guard_policy`` this is a PURE
stage module. It imports ONLY stdlib + the sibling pure modules + the envelope
schema — NO transport / NeMo / httpx / boto3 / OTel (``stages-pure`` +
``check_stages_grep_gate.sh`` stay green). The pod client is INJECTED, described
by a :class:`typing.Protocol` (structural typing), and the verdict sink is
injected too, so the audit trail lives OUTSIDE the answering path.

Three officer-visible outcomes wired here, each gated behind the Group 2 mode
flag so they do NOT act in Group 4 (verdict recorded only):

- ``ATTACK`` → block (an honest 200 ``GuardrailTripwire`` refusal) — ENFORCING only.
- ``OFFTOPIC`` → redirect (the ``redirect`` open-vocab decision; generic,
  non-accusatory, does not name the matter) — ENFORCING only.
- ``OK`` → allow (recorded).

The enforcing branches exist for the Group 8 flip; with the default
``GuardModes`` (all SHADOW) they never fire. The layered fail policy is honoured:
an ``unavailable`` adjudication maps to the ATTACK-lane ``guard-unavailable``
decision (fail CLOSED when ATTACK is enforcing, via
:func:`attack_guard_unavailable_tripwire` with ``Retry-After``); a topicality
miss (OFFTOPIC) is never a security event.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Protocol

from app.orchestrator.guard_policy import (
    GuardMode,
    GuardModes,
    VerdictClass,
    VerdictSink,
    attack_guard_unavailable_tripwire,
    guard_unavailable_decision,
    record_verdict,
    redirect_decision,
)
from app.orchestrator.guardrails import REFUSAL_TEXT, GuardrailTripwire
from app.schemas.envelope import GuardrailDecision

_INPUT_STAGE: Final[str] = "input"
_NEMO_CATEGORY: Final[str] = "nemo"

# Distinct rule ids so a triage verdict is machine-distinguishable in shadow
# telemetry from the enforcing self_check_input block (``nemo-input-block-v1``) —
# alarm on the rule id, not on rationale prose.
TRIAGE_ATTACK_BLOCK_RULE_ID: Final[str] = "nemo-input-triage-attack-v1"
TRIAGE_ALLOW_RULE_ID: Final[str] = "nemo-input-triage-ok-v1"

_TRIAGE_ATTACK_RATIONALE: Final[str] = (
    "Input triage classified this turn as an ATTACK (prompt-injection / "
    "system-prompt-leakage / jailbreak, incl. injection embedded in pasted "
    "content)."
)
_TRIAGE_ALLOW_RATIONALE: Final[str] = (
    "Input triage classified this turn as OK (on-topic case question, not an "
    "attack)."
)

# The generic redirect wording (does NOT name the matter — avoids adding context
# surface). Distinct from the block ``REFUSAL_TEXT``: a redirect is not an
# accusation and never blocks on the same footing as an ATTACK.
REDIRECT_TEXT: Final[str] = (
    "I can only help with questions about the case you're working on. "
    "Let's get back to the matter at hand."
)

# Group 7 cutover (2026-07-24, product-owner sign-off + G6 parity, zero ATTACK
# regression): the triage ATTACK class becomes the ACTIVE input adjudicator —
# ENFORCING — atomically with retiring ``self_check_input`` to PRODUCTION SHADOW.
# The swap is ATOMIC: the input lane never loses its security block, because
# self_check_input's enforcement transfers to the strictly-stronger, UNCONDITIONAL
# triage in the SAME change (triage covers every question, not only the old
# pre-filter-gated subset).
#
# OFFTOPIC → ENFORCING (Group 8.5, 2026-07-24 team decision). An off-topic question
# on the GUARDED config now receives the generic :data:`REDIRECT_TEXT` instead of an
# answer. This is a PRODUCT-UX behaviour (the assistant answers only case questions),
# not a security control; the team decided to enable it AND to expose an on/off
# comparison via the webui Guardrails toggle (guarded 1.8.0 vs unguarded 1.2.0 — the
# triage only runs on the guarded lane, so OFF answers everything). OFFTOPIC still
# fails toward OK on an unparseable verdict, and the redirect is a soft, non-accusatory
# steer, never a security refusal. OUTPUT is a separate lane, unchanged.
# Fail policy is preserved by ``run_input_triage``: an ATTACK adjudication (or any
# unavailable/timed-out pod call) fails CLOSED (guard-unavailable + Retry-After).
CUTOVER_MODES: Final[GuardModes] = GuardModes(
    attack=GuardMode.ENFORCING, offtopic=GuardMode.ENFORCING
)


class NemoTriageVerdict(Protocol):
    """
    Structural shape of the pod's plain-data TRIAGE verdict.

    Matches ``app.clients.nemo_guard.NemoTriageVerdict`` WITHOUT importing it, so
    the stage stays free of any httpx/nemoguardrails-carrying module
    (``stages-pure``). ``verdict`` is one of ``attack`` / ``offtopic`` / ``ok``;
    ``unavailable`` marks an infrastructure failure (the pod could not adjudicate).
    """

    verdict: str
    unavailable: bool
    rationale: str | None
    model_id: str | None
    input_tokens: int | None
    output_tokens: int | None


class NemoTriageClient(Protocol):
    """Structural type of the injected pod client's triage entry point."""

    def check_input_triage(self, question: str) -> NemoTriageVerdict:
        """Triage ONE RAW user turn via the pod's ``/check/input/triage`` rail."""
        ...


@dataclass(frozen=True)
class TriageObservation:
    """
    The recorded outcome of a SHADOW triage observation (no traffic effect).

    ``verdict_class`` is the flippable class this verdict belongs to
    (``ATTACK`` / ``OFFTOPIC``) or ``None`` for an ``ok`` allow. ``mode`` is the
    mode the class ran in (``SHADOW`` in Group 4). The pod-stamped ``model_id`` +
    token counts ride onto the shadow ``guardrail_input`` span so the triage
    call's cost renders in Phoenix.
    """

    decision: GuardrailDecision
    verdict_class: VerdictClass | None
    mode: GuardMode
    model_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class _UnavailableVerdict:
    """
    A synthetic ``unavailable`` triage verdict for a pod-call FAILURE.

    ``run_input_triage`` translates ANY exception from the injected client (a
    transport failure, a timeout, a mangled response) into this — the pod could
    not adjudicate, which is UNAVAILABLE. It is the SAME condition as a
    pod-signalled ``unavailable=True`` and is routed through the identical fail
    policy: fail CLOSED when ATTACK is ENFORCING (attacker-correlated timeouts
    must not fail open — arXiv 2606.14517), recorded when SHADOW. Keeping the
    translation HERE (broad ``except``, no transport import) preserves the
    ``stages-pure`` contract while making the atomic Group 7 swap fail-safe.
    """

    verdict: str = "ok"
    unavailable: bool = True
    rationale: str | None = None
    model_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


def _attack_block_decision(rationale: str | None) -> GuardrailDecision:
    """The ATTACK triage decision: a ``block`` (honest 200 refusal path when enforcing)."""
    return GuardrailDecision(
        stage=_INPUT_STAGE,
        decision="block",
        category=_NEMO_CATEGORY,
        rule_id=TRIAGE_ATTACK_BLOCK_RULE_ID,
        rationale=rationale or _TRIAGE_ATTACK_RATIONALE,
    )


def _allow_decision() -> GuardrailDecision:
    """The OK triage decision: an ``allow`` (recorded for the shadow audit trail)."""
    return GuardrailDecision(
        stage=_INPUT_STAGE,
        decision="allow",
        category=_NEMO_CATEGORY,
        rule_id=TRIAGE_ALLOW_RULE_ID,
        rationale=_TRIAGE_ALLOW_RATIONALE,
    )


def map_triage_verdict(
    verdict: NemoTriageVerdict,
) -> tuple[GuardrailDecision, VerdictClass | None]:
    """
    Map a pod triage verdict onto a :class:`GuardrailDecision` + its flippable class.

    - ``unavailable`` → the ATTACK-lane ``guard-unavailable`` decision (the pod
      could not adjudicate; ATTACK fails CLOSED when enforcing).
    - ``attack`` → a ``block`` decision (ATTACK class).
    - ``offtopic`` → the ``redirect`` decision (OFFTOPIC class).
    - ``ok`` (or anything else — fail toward OK) → an ``allow`` decision, no class.
    """
    if verdict.unavailable:
        return guard_unavailable_decision(), VerdictClass.ATTACK
    value = (verdict.verdict or "ok").strip().lower()
    if value == "attack":
        return _attack_block_decision(verdict.rationale), VerdictClass.ATTACK
    if value == "offtopic":
        return redirect_decision(), VerdictClass.OFFTOPIC
    return _allow_decision(), None


def answer_text_for_decision(decision: GuardrailDecision) -> str:
    """
    Derive the officer-visible answer text FROM the decision (task 4.5).

    A ``redirect`` renders the generic :data:`REDIRECT_TEXT`; every other
    refusal (``block`` / ``guard-unavailable``) renders the canned
    :data:`REFUSAL_TEXT`. The text is decision-derived, NOT a single constant, so
    a redirect fails gracefully rather than reading like a security refusal.
    """
    if decision.decision == "redirect":
        return REDIRECT_TEXT
    return REFUSAL_TEXT


def run_input_triage(
    client: NemoTriageClient,
    question: str,
    *,
    sink: VerdictSink,
    interaction_id: str,
    modes: GuardModes = GuardModes(),
) -> TriageObservation:
    """
    UNCONDITIONAL triage over the RAW question; record the verdict (SHADOW default).

    Runs on EVERY question on the guarded lane (Requirement 1's "every input").
    The RAW ``question`` is forwarded (NORMALIZE-ONCE canonical text — invisible/
    BIDI already handled by the Group 1 floor — but NEVER a rewritten query: the
    adversarial payload reaches the judge as written). The verdict is projected to
    a :class:`GuardrailDecision` and persisted via :func:`record_verdict` OUTSIDE
    the answering path.

    In SHADOW (the Group 2 default for every LLM class) this only RECORDS — it
    does not block/redirect. A class flipped to ENFORCING (Group 8) makes the
    officer-visible action fire: ``ATTACK`` → block (or fail CLOSED with
    ``Retry-After`` on an unavailable adjudication), ``OFFTOPIC`` → redirect. Both
    enforcing actions raise :class:`GuardrailTripwire`; the router derives the
    answer text from the decision.
    """
    try:
        verdict: NemoTriageVerdict = client.check_input_triage(question)
    except Exception:  # noqa: BLE001 — a pod-call failure is UNAVAILABLE; the layered fail policy owns it
        # The pod could not adjudicate (transport failure / timeout). Treat it as
        # UNAVAILABLE so ATTACK fails CLOSED when enforcing (never open on the exact
        # input that timed out — arXiv 2606.14517) and is recorded when shadow.
        verdict = _UnavailableVerdict()
    decision, verdict_class = map_triage_verdict(verdict)
    enforcing = verdict_class is not None and modes.is_enforcing(verdict_class)
    mode = GuardMode.ENFORCING if enforcing else GuardMode.SHADOW

    # Persist the verdict OUTSIDE the answering path (audit trail, countable by
    # rule id) — this happens in shadow AND enforcing.
    record_verdict(sink, interaction_id=interaction_id, decision=decision, mode=mode)

    if enforcing:
        # Group 8 territory — never reached with the default all-SHADOW modes.
        if verdict.unavailable:
            # ATTACK adjudication unavailable → fail CLOSED (honest 200 +
            # Retry-After); attacker-correlated timeouts must not fail open.
            raise attack_guard_unavailable_tripwire()
        raise GuardrailTripwire(
            decision,
            model_id=verdict.model_id,
            input_tokens=verdict.input_tokens,
            output_tokens=verdict.output_tokens,
        )

    return TriageObservation(
        decision=decision,
        verdict_class=verdict_class,
        mode=mode,
        model_id=verdict.model_id,
        input_tokens=verdict.input_tokens,
        output_tokens=verdict.output_tokens,
    )
