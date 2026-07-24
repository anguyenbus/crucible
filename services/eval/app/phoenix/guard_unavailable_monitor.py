"""
Guard-unavailable WATCH — alarms the ``nemo-output-guard-unavailable-v1`` window.

**Repoints the retired fail-open watch (item 9a).** The output lane USED to fail
**OPEN**: on a pod-unreachable (Mode B) it delivered the answer UNGUARDED with a
``nemo-output-fail-open-v1`` advisory, and this module counted those unguarded
deliveries. Guardrail spec ruling 0.3 (2026-07-24) changed the output lane to
fail **CLOSED**: a pod-unreachable now SUPPRESSES the unadjudicated answer to an
honest 200 refusal carrying ``nemo-output-guard-unavailable-v1`` (+ ``Retry-After``).
Fail-open no longer exists on any lane, so the old signal is dead; THIS is the
live one. The purpose shifts with it — from a SAFETY gap (answers reached the
officer unguarded) to an AVAILABILITY signal (the output guard is unreachable and
officers are being REFUSED). Same root cause (pod down on the output path),
opposite user-facing consequence.

The writer half is asserted next to the code that owns it, in
``services/orchestrator/tests/test_fail_open_alarm_signal.py`` /
``test_gate_mode_b.py``: a Mode-B refusal stamps ``guardrail.rule_id =
nemo-output-guard-unavailable-v1`` on the ``guardrail_output`` span and into
``guardrail_decisions[]``. This module is the READER half — and the two rule-id
constants must not drift.

**Placement decision (recorded, not defaulted).** The watch is a Phoenix-side
scheduled check in the EVAL service, not an orchestrator-side circuit breaker.
The full reasoning is in the spec folder's
``implementation/fail-open-monitor-decision.md``; the short form is that the
decision records ALREADY land in Phoenix (the rule id is on the
``guardrail_output`` span), so this needs no orchestrator change, no new
request-path state, and cannot itself add a failure mode to the answering path it
is watching. (The orchestrator ALSO trips a circuit breaker + alarm on sustained
unavailability at the request boundary — Group 2; this scheduled read is the
independent, audit-side confirmation, not the primary signal.)

Design (mirrors ``app.phoenix.guardrail_ab`` — dependency-injected shell):

  * The Phoenix span source is INJECTED (``client.spans`` satisfies
    :class:`SpanSource`). This module never builds a Phoenix client, never
    imports ``phoenix`` at module scope, and never touches AWS — so it imports
    and unit-tests hermetically against a fake source.
  * The counting half is PURE (:func:`guard_unavailable_span_ids`,
    :func:`guard_unavailable_decisions`), so the same rule id can be counted from
    Phoenix spans OR straight from a response envelope's
    ``guardrail_decisions[]`` without a second implementation.
  * The notification surface is an injected callable
    (:data:`GuardUnavailableMonitor.notify`), defaulting to :func:`log_alarm`.
    Swapping in a pager / webhook is a constructor argument, not a rewrite.

**One notification per window.** A pod outage is sustained by nature: a monitor
that re-notified on every scheduled check would emit an alert storm and be muted
by its recipient, which is exactly the silence this item exists to remove. The
alarm is therefore re-notified at most once per ``notify_interval`` (defaulting
to the window length) while the condition persists;
:meth:`GuardUnavailableMonitor.check` still RETURNS the alarm on every check so a
caller can render current state.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from beartype import beartype
from beartype.typing import Callable, Protocol, runtime_checkable

log = logging.getLogger(__name__)

# The LOUD rule id the orchestrator stamps on a fail-CLOSED, pod-unreachable
# REFUSAL — distinct from the routine advisory `nemo-output-flag-v1` (pod DID
# answer, non-blocking flag). Defined in
# `services/orchestrator/app/orchestrator/guardrails.py`
# (`_NEMO_OUTPUT_GUARD_UNAVAILABLE_RULE_ID`); this constant is the reader half of
# that contract and the two must not drift.
GUARD_UNAVAILABLE_RULE_ID: Final[str] = "nemo-output-guard-unavailable-v1"

# Where the orchestrator's spans land (`app.observability.PHOENIX_PROJECT`).
ORCHESTRATOR_PROJECT: Final[str] = "orchestrator"

# The span attribute the rule id rides on (`set_guardrail_output_attributes`).
RULE_ID_ATTRIBUTE: Final[str] = "guardrail.rule_id"

# Envelope key carrying the same id in `guardrail_decisions[]`.
DECISION_RULE_ID_KEY: Final[str] = "rule_id"

# Default window / threshold. One guard-unavailable is one REFUSED answer, but a
# single transient hiccup is not an incident and paging on it trains the
# recipient to ignore the page. THREE refusals inside FIFTEEN minutes is a pod
# that is down rather than flapping, which is the condition the output-lane
# fail-closed policy actually cares about. Both are constructor arguments —
# tighten them once a real outage rate is observed.
DEFAULT_WINDOW: Final[timedelta] = timedelta(minutes=15)
DEFAULT_THRESHOLD: Final[int] = 3

# Cap on spans fetched per check. The count is therefore "at least N" once the
# cap is hit — which is well past any threshold worth alarming on.
DEFAULT_MAX_SPANS: Final[int] = 1000


@runtime_checkable
class SpanSource(Protocol):
    """
    The Phoenix span-read seam (structurally satisfied by ``client.spans``).

    Declared as a Protocol so the monitor unit-tests against a fake without a
    Phoenix server and without importing ``phoenix`` at module scope.
    """

    def get_spans(
        self,
        *,
        project_identifier: str,
        start_time: datetime | None = ...,
        end_time: datetime | None = ...,
        attributes: Mapping[str, Any] | None = ...,
        limit: int = ...,
    ) -> Sequence[Mapping[str, Any]]:
        """Return spans in ``[start_time, end_time)`` matching ``attributes``."""
        ...


@dataclass(frozen=True, slots=True)
class GuardUnavailableAlarm:
    """
    One tripped guard-unavailable window (plain data).

    Attributes:
        window_start: Inclusive lower bound of the observation window (UTC).
        window_end: Exclusive upper bound — the check's ``now`` (UTC).
        count: Guard-unavailable refusals counted in the window (capped by
            ``max_spans``).
        threshold: The count at or above which the alarm trips.
        span_ids: Phoenix span ids of the counted occurrences — the evidence
            trail an on-call owner opens next.
        notified: Whether THIS check delivered a notification, or suppressed it
            as a duplicate of an earlier one in the same window.

    """

    window_start: datetime
    window_end: datetime
    count: int
    threshold: int
    span_ids: tuple[str, ...] = ()
    notified: bool = True

    @property
    def message(self) -> str:
        """The operator-facing alarm line: what happened, how bad, where to look."""
        return (
            f"GUARDRAIL OUTPUT UNAVAILABLE: {self.count} answer(s) REFUSED "
            f"because the output guard could not adjudicate "
            f"(rule {GUARD_UNAVAILABLE_RULE_ID}, threshold {self.threshold}) between "
            f"{self.window_start.isoformat()} and {self.window_end.isoformat()}. "
            "The guardrail pod is unreachable on the output/facts path; the "
            "output lane fails CLOSED by policy (ruling 0.3), so officers are "
            "being REFUSED rather than served unguarded. Check the pod's health "
            "and /readyz."
        )


@beartype
def log_alarm(alarm: GuardUnavailableAlarm) -> None:
    """Default notification surface: one CRITICAL log line per notified alarm."""
    log.critical("%s", alarm.message)


@beartype
def guard_unavailable_span_ids(spans: Iterable[Mapping[str, Any]]) -> tuple[str, ...]:
    """
    Span ids of the guard-unavailable refusals in ``spans`` (pure).

    Re-filters on ``guardrail.rule_id`` locally rather than trusting the
    server-side attribute filter, so a Phoenix build that ignores the filter
    inflates no count. The routine advisory ``nemo-output-flag-v1`` is NOT
    counted: the pod answered, so the answer was guarded — not an outage.
    """
    return tuple(
        str(span.get("id", ""))
        for span in spans
        if dict(span.get("attributes") or {}).get(RULE_ID_ATTRIBUTE) == GUARD_UNAVAILABLE_RULE_ID
    )


@beartype
def guard_unavailable_decisions(
    decisions: Iterable[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    """
    Return the ``guardrail_decisions[]`` entries marking a guard-unavailable REFUSAL.

    The envelope-side counterpart of :func:`guard_unavailable_span_ids`. A
    guard-unavailable refusal carries ``decision == "guard-unavailable"`` while a
    routine advisory carries ``decision == "flag"``, but the rule id is the
    precise, decision-vocab-stable discriminator, so we count on it.
    """
    return tuple(
        decision
        for decision in decisions
        if decision.get(DECISION_RULE_ID_KEY) == GUARD_UNAVAILABLE_RULE_ID
    )


class GuardUnavailableMonitor:
    """
    Windowed watch over ``nemo-output-guard-unavailable-v1``, read from Phoenix spans.

    One :meth:`check` = one scheduled read of the trailing window. Trips when
    the occurrence count reaches ``threshold``; notifies at most once per
    ``notify_interval`` while the condition persists.
    """

    @beartype
    def __init__(
        self,
        spans: SpanSource,
        *,
        notify: Callable[[GuardUnavailableAlarm], None] = log_alarm,
        project: str = ORCHESTRATOR_PROJECT,
        window: timedelta = DEFAULT_WINDOW,
        threshold: int = DEFAULT_THRESHOLD,
        notify_interval: timedelta | None = None,
        max_spans: int = DEFAULT_MAX_SPANS,
    ) -> None:
        """
        Build the watch.

        Args:
            spans: The injected Phoenix span source (``client.spans``).
            notify: The notification surface, called once per notified alarm.
            project: Phoenix project the orchestrator's spans land in.
            window: Trailing observation window.
            threshold: Occurrences in the window at or above which the alarm trips.
            notify_interval: Minimum gap between notifications while the alarm
                stays tripped. Defaults to ``window`` — one page per window.
            max_spans: Per-check fetch cap.

        Raises:
            ValueError: On a non-positive threshold, window, or fetch cap — a
                misconfigured watch must fail at construction, never silently
                watch nothing.

        """
        if threshold < 1:
            raise ValueError(f"threshold must be >= 1, got {threshold}")
        if window <= timedelta(0):
            raise ValueError(f"window must be positive, got {window}")
        if max_spans < 1:
            raise ValueError(f"max_spans must be >= 1, got {max_spans}")
        self._spans = spans
        self._notify = notify
        self._project = project
        self._window = window
        self._threshold = threshold
        self._notify_interval = window if notify_interval is None else notify_interval
        self._max_spans = max_spans
        self._last_notified_at: datetime | None = None

    @beartype
    def check(self, now: datetime | None = None) -> GuardUnavailableAlarm | None:
        """
        Run one scheduled check over the trailing window.

        Args:
            now: The window's exclusive upper bound; defaults to the current UTC
                time. Must be timezone-aware (Phoenix span times are UTC).

        Returns:
            A :class:`GuardUnavailableAlarm` when the count reached the threshold
            — with ``notified`` recording whether this check actually paged — or
            ``None`` when the window is below threshold.

        Raises:
            ValueError: If ``now`` is naive. Comparing a naive clock against
                UTC span times silently mis-windows the count.

        """
        moment = datetime.now(UTC) if now is None else now
        if moment.tzinfo is None:
            raise ValueError("`now` must be timezone-aware (Phoenix span times are UTC)")
        window_start = moment - self._window

        fetched = self._spans.get_spans(
            project_identifier=self._project,
            start_time=window_start,
            end_time=moment,
            attributes={RULE_ID_ATTRIBUTE: GUARD_UNAVAILABLE_RULE_ID},
            limit=self._max_spans,
        )
        span_ids = guard_unavailable_span_ids(fetched)
        if len(span_ids) < self._threshold:
            return None

        notified = (
            self._last_notified_at is None
            or moment - self._last_notified_at >= self._notify_interval
        )
        alarm = GuardUnavailableAlarm(
            window_start=window_start,
            window_end=moment,
            count=len(span_ids),
            threshold=self._threshold,
            span_ids=span_ids,
            notified=notified,
        )
        if notified:
            self._last_notified_at = moment
            self._notify(alarm)
        return alarm
