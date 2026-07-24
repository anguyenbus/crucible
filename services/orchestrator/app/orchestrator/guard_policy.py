"""
Shared guard operational surface: persistence, mode flag, timeout/breaker, fail policy.

Group 2 of the guardrail input/output validation spec — "instrument BEFORE
enforce." This module is the shared surface every later LLM verdict (the Haiku
triage rail, the ratified output lane) is built on: it must land before any LLM
verdict ships in shadow, because a shadow verdict that is not recorded is
useless.

Purity: like ``app.orchestrator.guardrails`` this is a PURE stage module. It
imports ONLY stdlib + ``app.schemas.envelope`` + ``app.orchestrator.guardrails``
— NO transport / NeMo / httpx / boto3 / opentelemetry (the ``stages-pure``
import-linter contract + ``check_stages_grep_gate.sh`` stay green). The verdict
sink, the alarm callback, and the clock are all INJECTED (structural
``Protocol`` / callables), so the audit trail lives OUTSIDE the answering code
path (the independent-enforcement rationale) and the breaker is deterministically
testable with no wall clock.

THE LAYERED FAIL POLICY (the security-researched centrepiece — represented
exactly):

  1. Deterministic floor (``guardrails.check_input_malformed`` +
     ``check_input_prefilter_floor``) — ALWAYS enforcing, pod-independent. NOT
     governed by the mode flag here. This floor is what breaks the DoS
     "attacker-wins-either-way" dilemma, which only holds for a SINGLE LLM gate.
  2. LLM ATTACK adjudication unavailable / timeout → fail CLOSED for THAT
     request: an honest 200 refusal with a distinct ``guard-unavailable`` rule id
     and ``Retry-After``. NOT fail-open — guard timeouts are attacker-correlated
     (reasoning-extension DoS, arXiv 2606.14517 *From Shield to Target*, 27.7x
     token amplification on Claude Haiku; arXiv 2410.02916 *Double-Edged Sword* /
     FP-DoS). Failing open on the exact input that timed out hands the attacker
     the engineered bypass and leaves an evidentiary gap (DeepInspect AI-gateway
     audit-trail guidance; NIST / regulated fail-secure guidance).
  3. LLM OFFTOPIC (topicality) unavailable → fail OPEN to ``OK``: a topicality
     miss is not a security event; blocking a real case question is worse.
  4. Circuit breaker + alarm on SUSTAINED pod unavailability (infra-down or
     attacker-induced load — both page-worthy). Fail-closed must be a monitored,
     bounded, rare degraded mode with a documented recovery path (half-open
     probe), never a silent lockout.

Enforcement of these policies through an UNCONDITIONAL input lane is Group 4 —
this module builds and unit-tests the primitives; it does not yet wire them into
the router.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum
from typing import Final, Protocol

from app.orchestrator.guardrails import GuardrailTripwire
from app.schemas.envelope import GuardrailDecision

_INPUT_STAGE: Final[str] = "input"
_NEMO_CATEGORY: Final[str] = "nemo"

# --- Bounded pod-call timeout (Q6) ----------------------------------------
# A critical-path dependency MUST have defined behavior on timeout. Provisionally
# ~2-3x the measured p95 (triage p50 ~595 ms): "sequential, bounded, defined
# failure behavior," never "sequential, unbounded." A timeout FEEDS the fail
# policy below (a timed-out ATTACK adjudication fails CLOSED).
GUARD_POD_TIMEOUT_SECONDS: Final[float] = 2.0

# --- Retry-After on a fail-CLOSED guard-unavailable refusal ---------------
# Short: a transient Bedrock throttle clears in seconds, so the honest 200
# refusal is survivable and retryable (the guard runs PRE-retrieval, so it costs
# one query, not the officer's work).
GUARD_UNAVAILABLE_RETRY_AFTER_SECONDS: Final[int] = 5

# --- Distinct machine-readable rule ids (alarm on the id, not on prose) ----
INPUT_GUARD_UNAVAILABLE_RULE_ID: Final[str] = "nemo-input-guard-unavailable-v1"
OFFTOPIC_REDIRECT_RULE_ID: Final[str] = "nemo-input-offtopic-redirect-v1"
OFFTOPIC_FAIL_OPEN_RULE_ID: Final[str] = "nemo-input-offtopic-fail-open-v1"

_GUARD_UNAVAILABLE_RATIONALE: Final[str] = (
    "Guard pod could not adjudicate the ATTACK decision (timeout/unavailable) — "
    "failing CLOSED for this request (attacker-correlated timeouts must not "
    "fail open; arXiv 2606.14517). Retryable."
)
_REDIRECT_RATIONALE: Final[str] = (
    "Question is off-topic for this case — redirected back to the matter "
    "(generic, non-accusatory; does not name the matter)."
)
_OFFTOPIC_FAIL_OPEN_RATIONALE: Final[str] = (
    "Guard pod could not adjudicate the OFFTOPIC (topicality) decision — failing "
    "OPEN to OK (a topicality miss is not a security event; the ATTACK regex "
    "floor still guards)."
)


# ==========================================================================
# Shadow / enforcing mode flag — per verdict class
# ==========================================================================


class VerdictClass(Enum):
    """
    The LLM verdict classes that flip from shadow to enforcing INDEPENDENTLY.

    Deliberately does NOT include the deterministic floor (malformed / promoted
    pre-filter): that floor is ALWAYS enforcing and is never governed by the mode
    flag — "shadow" applies ONLY to the new LLM layer and never disables
    pre-existing protection.
    """

    ATTACK = "attack"
    OFFTOPIC = "offtopic"
    OUTPUT = "output"


class GuardMode(Enum):
    """Whether a class blocks/redirects real traffic (ENFORCING) or only records it (SHADOW)."""

    SHADOW = "shadow"
    ENFORCING = "enforcing"


@dataclass(frozen=True)
class GuardModes:
    """
    Per-verdict-class shadow/enforcing modes (the new LLM layer ships in SHADOW).

    Every LLM class defaults to :data:`GuardMode.SHADOW` so the flip to the
    guarded default (Group 3) records verdicts on the envelope WITHOUT blocking
    real traffic; each class is flipped to enforcing later (Group 8), gated on a
    measured false-positive rate reviewed by a named owner. The deterministic
    floor is NOT represented here — it is always enforcing.
    """

    attack: GuardMode = GuardMode.SHADOW
    offtopic: GuardMode = GuardMode.SHADOW
    output: GuardMode = GuardMode.SHADOW

    def is_enforcing(self, verdict_class: VerdictClass) -> bool:
        """True iff the given LLM verdict class currently blocks/redirects real traffic."""
        return self._mode(verdict_class) is GuardMode.ENFORCING

    def _mode(self, verdict_class: VerdictClass) -> GuardMode:
        return {
            VerdictClass.ATTACK: self.attack,
            VerdictClass.OFFTOPIC: self.offtopic,
            VerdictClass.OUTPUT: self.output,
        }[verdict_class]

    def with_mode(self, verdict_class: VerdictClass, mode: GuardMode) -> GuardModes:
        """Return a copy with ONE class flipped — the other classes are unchanged."""
        field = {
            VerdictClass.ATTACK: "attack",
            VerdictClass.OFFTOPIC: "offtopic",
            VerdictClass.OUTPUT: "output",
        }[verdict_class]
        return replace(self, **{field: mode})


# ==========================================================================
# Verdict persistence — OUTSIDE the answering code path
# ==========================================================================


@dataclass(frozen=True)
class VerdictRecord:
    """
    One persisted guard verdict, keyed to the interaction/trace id.

    The audit trail lives OUTSIDE the answering code path (independent
    enforcement): the answering code emits a :class:`GuardrailDecision`; a
    :class:`VerdictSink` records this projection of it. Each record carries its
    OWN machine-readable ``rule_id`` so fail-open / fail-closed windows are
    countable and alarms fire on the rule id, not on rationale prose.
    """

    interaction_id: str
    stage: str
    decision: str
    rule_id: str | None
    category: str | None
    mode: str
    rationale: str | None = None


class VerdictSink(Protocol):
    """Structural type of the injected audit sink (Phoenix span/DB/log — INJECTED, not imported)."""

    def record(self, record: VerdictRecord) -> None:
        """Persist one verdict record keyed to its interaction/trace id."""
        ...


def record_verdict(
    sink: VerdictSink,
    *,
    interaction_id: str,
    decision: GuardrailDecision,
    mode: GuardMode,
) -> VerdictRecord:
    """
    Project a decision into a :class:`VerdictRecord` and persist it via the sink.

    Records allow / block / flag / redirect / fail-open / guard-unavailable
    verdicts identically — the machine-readable ``rule_id`` is what distinguishes
    them downstream. Returns the record for the caller to also attach to a span.
    """
    record = VerdictRecord(
        interaction_id=interaction_id,
        stage=decision.stage,
        decision=decision.decision,
        rule_id=decision.rule_id,
        category=decision.category,
        mode=mode.value,
        rationale=decision.rationale,
    )
    sink.record(record)
    return record


# ==========================================================================
# Layered fail policy — decision/tripwire builders
# ==========================================================================


def guard_unavailable_decision() -> GuardrailDecision:
    """The ATTACK-lane fail-CLOSED decision: a ``guard-unavailable`` refusal (distinct rule id)."""
    return GuardrailDecision(
        stage=_INPUT_STAGE,
        decision="guard-unavailable",
        category=_NEMO_CATEGORY,
        rule_id=INPUT_GUARD_UNAVAILABLE_RULE_ID,
        rationale=_GUARD_UNAVAILABLE_RATIONALE,
    )


def attack_guard_unavailable_tripwire() -> GuardrailTripwire:
    """
    ATTACK adjudication unavailable/timeout → fail CLOSED for THIS request.

    An honest 200 refusal (a :class:`GuardrailTripwire`, NEVER a 5xx) carrying the
    distinct ``guard-unavailable`` rule id and a ``Retry-After``. NOT fail-open:
    guard timeouts are attacker-correlated (reasoning-extension DoS, arXiv
    2606.14517), so failing open on the exact input that timed out would hand the
    attacker the engineered bypass and leave an evidentiary gap.
    """
    return GuardrailTripwire(
        guard_unavailable_decision(),
        retry_after=GUARD_UNAVAILABLE_RETRY_AFTER_SECONDS,
    )


def offtopic_fail_open_decision() -> GuardrailDecision:
    """
    OFFTOPIC (topicality) adjudication unavailable → fail OPEN to ``OK``.

    A NON-blocking decision (recorded for audit so the fail-open window is
    countable by rule id), never a tripwire: a topicality miss is not a security
    event, and blocking a real case question is worse. The ATTACK regex floor
    still guards behind it.
    """
    return GuardrailDecision(
        stage=_INPUT_STAGE,
        decision="flag",
        category=_NEMO_CATEGORY,
        rule_id=OFFTOPIC_FAIL_OPEN_RULE_ID,
        rationale=_OFFTOPIC_FAIL_OPEN_RATIONALE,
    )


def redirect_decision() -> GuardrailDecision:
    """
    The OFFTOPIC redirect decision (new ``redirect`` open-vocab value).

    A redirect is NOT a block: a false OFFTOPIC on a real case question must fail
    gracefully (annoy, not lose the officer's query), so it never blocks on the
    same footing as ATTACK. Generic, non-accusatory wording that does not name
    the matter (avoids adding context surface).
    """
    return GuardrailDecision(
        stage=_INPUT_STAGE,
        decision="redirect",
        category=_NEMO_CATEGORY,
        rule_id=OFFTOPIC_REDIRECT_RULE_ID,
        rationale=_REDIRECT_RATIONALE,
    )


# ==========================================================================
# Circuit breaker — sustained unavailability is a page-worthy incident
# ==========================================================================

import time as _time  # noqa: E402 - stdlib only; kept local to the breaker


class CircuitBreaker:
    """
    A minimal, deterministic circuit breaker for the pod call.

    Consecutive failures crossing ``failure_threshold`` TRIP the breaker and fire
    the injected ``alarm`` ONCE (sustained unavailability — infra-down or
    attacker-induced load — is a page-worthy incident). The trip is a MONITORED,
    BOUNDED, rare degraded mode with a documented recovery path: after
    ``reset_timeout_s`` the breaker half-opens (a probe is allowed — never a
    silent lockout), and a success on the probe fully closes it and re-arms the
    alarm. The ``clock`` is injected so the breaker is testable with no wall time.
    """

    def __init__(
        self,
        *,
        failure_threshold: int,
        reset_timeout_s: float,
        alarm: Callable[[str], None],
        clock: Callable[[], float] = _time.monotonic,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._reset_timeout_s = reset_timeout_s
        self._alarm = alarm
        self._clock = clock
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._alarmed = False

    def record_success(self) -> None:
        """A success (incl. a half-open probe) fully closes the breaker and re-arms the alarm."""
        self._consecutive_failures = 0
        self._opened_at = None
        self._alarmed = False

    def record_failure(self) -> None:
        """Count a failure; trip + page ONCE when sustained unavailability crosses the threshold."""
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._failure_threshold and self._opened_at is None:
            self._opened_at = self._clock()
            if not self._alarmed:
                self._alarm(
                    "guard pod SUSTAINED unavailability — circuit breaker OPEN "
                    f"({self._consecutive_failures} consecutive failures); page on-call"
                )
                self._alarmed = True

    def is_open(self) -> bool:
        """True while tripped AND inside the bounded reset window (else half-open)."""
        if self._opened_at is None:
            return False
        # Bounded degraded mode with a recovery path: after the reset window a
        # probe is allowed (half-open) rather than a silent, permanent lockout.
        if self._clock() - self._opened_at >= self._reset_timeout_s:
            return False
        return True
