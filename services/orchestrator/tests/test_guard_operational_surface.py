"""Group 2: verdict persistence, mode flag, bounded timeout + breaker, fail policy.

Instrument BEFORE enforce. These focused tests assert the shared operational
surface every later LLM verdict depends on:

- verdict persistence keyed to the interaction/trace id, OUTSIDE the answering
  path, each record carrying a machine-readable rule id;
- the layered fail policy centrepiece — ATTACK adjudication unavailable fails
  CLOSED (honest 200 refusal, `guard-unavailable` rule id + `Retry-After`, never
  a 5xx), OFFTOPIC unavailable fails OPEN to OK;
- the shadow/enforcing mode flag per verdict class (deterministic gates are NOT
  governed by it — they stay enforcing);
- the bounded pod-call timeout + circuit breaker that alarms on SUSTAINED
  unavailability with a documented recovery path (never a silent lockout).
"""

from __future__ import annotations

from app.orchestrator.guard_policy import (
    GUARD_POD_TIMEOUT_SECONDS,
    GUARD_UNAVAILABLE_RETRY_AFTER_SECONDS,
    INPUT_GUARD_UNAVAILABLE_RULE_ID,
    CircuitBreaker,
    GuardMode,
    GuardModes,
    VerdictClass,
    VerdictRecord,
    attack_guard_unavailable_tripwire,
    guard_unavailable_decision,
    offtopic_fail_open_decision,
    record_verdict,
    redirect_decision,
)
from app.orchestrator.guardrails import GuardrailTripwire
from app.schemas.envelope import GuardrailDecision


class FakeSink:
    """A verdict sink OUTSIDE the answering path — records, never influences it."""

    def __init__(self) -> None:
        self.records: list[VerdictRecord] = []

    def record(self, record: VerdictRecord) -> None:
        self.records.append(record)


# --------------------------------------------------------------------------
# Verdict persistence (keyed to the interaction/trace id, with a rule id)
# --------------------------------------------------------------------------


def test_every_verdict_type_is_persisted_with_its_rule_id_keyed_to_trace_id():
    sink = FakeSink()
    decisions = {
        "allow": GuardrailDecision(stage="input", decision="allow", rule_id="ok-v1"),
        "block": GuardrailDecision(stage="input", decision="block", rule_id="nemo-input-block-v1"),
        "flag": GuardrailDecision(stage="output", decision="flag", rule_id="nemo-output-flag-v1"),
        "redirect": redirect_decision(),
        "fail-open": offtopic_fail_open_decision(),
        "guard-unavailable": guard_unavailable_decision(),
    }
    for decision in decisions.values():
        record_verdict(
            sink,
            interaction_id="trace-abc",
            decision=decision,
            mode=GuardMode.SHADOW,
        )
    assert len(sink.records) == len(decisions)
    # Keyed to the interaction/trace id; each carries a machine-readable rule id.
    assert {r.interaction_id for r in sink.records} == {"trace-abc"}
    assert all(r.rule_id for r in sink.records)
    recorded_values = {r.decision for r in sink.records}
    assert {"redirect", "flag", "guard-unavailable"} <= recorded_values


# --------------------------------------------------------------------------
# Layered fail policy (the centrepiece)
# --------------------------------------------------------------------------


def test_attack_adjudication_unavailable_fails_closed_with_retry_after():
    """ATTACK timeout is attacker-correlated (arXiv 2606.14517) — fail CLOSED, not open."""
    tw = attack_guard_unavailable_tripwire()
    # An honest 200 refusal path (a GuardrailTripwire), NEVER a 5xx exception.
    assert isinstance(tw, GuardrailTripwire)
    assert not isinstance(tw, RuntimeError)  # not the GuardMisconfigured 500 class
    assert tw.decision.decision == "guard-unavailable"
    assert tw.decision.rule_id == INPUT_GUARD_UNAVAILABLE_RULE_ID
    # Retry-After present and positive (transient Bedrock throttle clears fast).
    assert tw.retry_after == GUARD_UNAVAILABLE_RETRY_AFTER_SECONDS
    assert tw.retry_after > 0


def test_offtopic_unavailable_fails_open_to_ok_never_blocks():
    """A topicality miss is not a security event; blocking a real case question is worse."""
    decision = offtopic_fail_open_decision()
    # Fail OPEN: a non-blocking decision (recorded for audit), NOT a tripwire.
    assert decision.decision != "block"
    assert decision.decision != "guard-unavailable"
    assert decision.rule_id  # countable window carries its own rule id


def test_redirect_and_guard_unavailable_are_valid_open_vocab_values():
    """Non-breaking additions to the open-vocab GuardrailDecision."""
    assert redirect_decision().decision == "redirect"
    assert guard_unavailable_decision().decision == "guard-unavailable"
    # Both machine-distinct by rule id (alarm on the rule id, not prose).
    assert redirect_decision().rule_id != guard_unavailable_decision().rule_id


# --------------------------------------------------------------------------
# Shadow/enforcing mode flag (per verdict class; deterministic gates excluded)
# --------------------------------------------------------------------------


def test_mode_flag_flips_one_class_only_and_defaults_to_shadow():
    """The NEW LLM layer ships in SHADOW; classes flip independently."""
    modes = GuardModes()  # default: every LLM class in shadow
    assert not modes.is_enforcing(VerdictClass.ATTACK)
    assert not modes.is_enforcing(VerdictClass.OFFTOPIC)
    assert not modes.is_enforcing(VerdictClass.OUTPUT)
    # Flip ONLY ATTACK to enforcing — the other classes are unchanged.
    enforced = modes.with_mode(VerdictClass.ATTACK, GuardMode.ENFORCING)
    assert enforced.is_enforcing(VerdictClass.ATTACK)
    assert not enforced.is_enforcing(VerdictClass.OFFTOPIC)
    assert not enforced.is_enforcing(VerdictClass.OUTPUT)
    # The deterministic floor is NOT a member of the flippable classes — it is
    # always enforcing and is never governed by this flag.
    assert not hasattr(VerdictClass, "MALFORMED")


# --------------------------------------------------------------------------
# Bounded timeout + circuit breaker + alarm (sustained unavailability)
# --------------------------------------------------------------------------


def test_bounded_timeout_is_a_named_constant_in_the_documented_band():
    """A critical-path dependency has DEFINED failure behavior — never unbounded."""
    assert 1.5 <= GUARD_POD_TIMEOUT_SECONDS <= 3.0


def test_breaker_trips_and_alarms_once_on_sustained_unavailability():
    clock = [0.0]
    alarms: list[str] = []
    breaker = CircuitBreaker(
        failure_threshold=3,
        reset_timeout_s=10.0,
        alarm=alarms.append,
        clock=lambda: clock[0],
    )
    # Below threshold: not tripped, no page.
    breaker.record_failure()
    breaker.record_failure()
    assert not breaker.is_open()
    assert alarms == []
    # Sustained unavailability crosses the threshold → trip + page ONCE.
    breaker.record_failure()
    assert breaker.is_open()
    assert len(alarms) == 1
    breaker.record_failure()  # still open, but no duplicate page
    assert len(alarms) == 1


def test_breaker_has_a_recovery_path_and_is_not_a_silent_lockout():
    clock = [0.0]
    alarms: list[str] = []
    breaker = CircuitBreaker(
        failure_threshold=1,
        reset_timeout_s=10.0,
        alarm=alarms.append,
        clock=lambda: clock[0],
    )
    breaker.record_failure()
    assert breaker.is_open()
    # After the bounded reset window a probe is allowed (half-open, not locked out).
    clock[0] = 10.0
    assert not breaker.is_open()
    # A success on the probe fully closes the breaker and re-arms the alarm.
    breaker.record_success()
    assert not breaker.is_open()
    breaker.record_failure()
    assert len(alarms) == 2  # a NEW sustained-unavailability window pages again
