"""Unit tests for the guard-unavailable watch (item 9a, task 6.1) — fully MOCKED.

Repoints the retired fail-open watch: the output lane now fails CLOSED (guardrail
spec ruling 0.3), so a pod-unreachable REFUSES the answer with
``nemo-output-guard-unavailable-v1`` rather than delivering it unguarded. This
suite pins the reader half against a fake Phoenix span source (no Phoenix server,
no orchestrator, no pod, no AWS), so it is hermetic and fast. It pins the four
behaviours that decide whether the watch is worth having:

  * a window that reaches the threshold TRIPS, carrying its evidence;
  * a window below the threshold does NOT (the negative case — a watch that
    alarms on everything is muted by its recipient and is therefore silence);
  * a SUSTAINED outage notifies ONCE per window, not once per scheduled check
    (an alert storm is the same silence by a different route); and
  * the routine advisory ``nemo-output-flag-v1`` is NOT counted — the pod
    ANSWERED there, so the answer was guarded and it is not an outage.

The Mode-B WRITER half — that a pod-unreachable refusal actually STAMPS
``nemo-output-guard-unavailable-v1`` on the ``guardrail_output`` span and into
``guardrail_decisions[]`` — is asserted next to the code that owns it, in
``services/orchestrator/tests/test_fail_open_alarm_signal.py`` and
``test_gate_mode_b.py``. The envelope shape it produces is replayed here as the
reader-side counterpart.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.phoenix.guard_unavailable_monitor import (
    GUARD_UNAVAILABLE_RULE_ID,
    ORCHESTRATOR_PROJECT,
    RULE_ID_ATTRIBUTE,
    GuardUnavailableMonitor,
    guard_unavailable_decisions,
)

# The routine advisory: the pod DID answer and flagged. Never a guard-unavailable.
ADVISORY_RULE_ID = "nemo-output-flag-v1"

NOW = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)


def _span(
    span_id: str, *, minutes_ago: float, rule_id: str = GUARD_UNAVAILABLE_RULE_ID
) -> dict[str, Any]:
    """One Phoenix-shaped ``guardrail_output`` span carrying a guard rule id."""
    return {
        "id": span_id,
        "name": "guardrail_output",
        "span_kind": "CHAIN",
        "start_time": NOW - timedelta(minutes=minutes_ago),
        "attributes": {
            "guardrail.stage": "output",
            "guardrail.decision": "guard-unavailable",
            "guardrail.category": "nemo",
            RULE_ID_ATTRIBUTE: rule_id,
        },
    }


class _FakeSpans:
    """
    Injected Phoenix span source: window-aware replay of canned spans.

    Honours ``start_time`` / ``end_time`` so the WINDOW is genuinely exercised
    (a monitor that fetched everything and called it a window would pass a fake
    that ignored the bounds), and records each query for assertion.
    """

    def __init__(self, spans: list[dict[str, Any]]) -> None:
        self._spans = spans
        self.calls: list[dict[str, Any]] = []

    def get_spans(
        self,
        *,
        project_identifier: str,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        attributes: dict[str, Any] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        self.calls.append(
            {
                "project_identifier": project_identifier,
                "start_time": start_time,
                "end_time": end_time,
                "attributes": attributes,
                "limit": limit,
            }
        )
        selected = [
            span
            for span in self._spans
            if (start_time is None or span["start_time"] >= start_time)
            and (end_time is None or span["start_time"] < end_time)
        ]
        return selected[:limit]


def test_a_window_at_the_threshold_trips_the_alarm_with_its_evidence():
    """Three guard-unavailable refusals inside the window ⇒ ALARM, notified once."""
    spans = _FakeSpans(
        [_span(f"span-{i}", minutes_ago=minutes) for i, minutes in enumerate((1, 5, 12))]
    )
    fired = []
    monitor = GuardUnavailableMonitor(
        spans, notify=fired.append, threshold=3, window=timedelta(minutes=15)
    )

    alarm = monitor.check(now=NOW)

    assert alarm is not None
    assert alarm.count == 3
    assert alarm.threshold == 3
    assert alarm.notified is True
    assert alarm.span_ids == ("span-0", "span-1", "span-2")
    assert alarm.window_start == NOW - timedelta(minutes=15)
    assert alarm.window_end == NOW
    # The notification names the rule and says plainly what happened.
    assert len(fired) == 1
    assert GUARD_UNAVAILABLE_RULE_ID in fired[0].message
    assert "REFUSED" in fired[0].message
    # It asked Phoenix the right question: orchestrator project, this rule, this window.
    assert spans.calls[0]["project_identifier"] == ORCHESTRATOR_PROJECT
    assert spans.calls[0]["attributes"] == {RULE_ID_ATTRIBUTE: GUARD_UNAVAILABLE_RULE_ID}
    assert spans.calls[0]["start_time"] == NOW - timedelta(minutes=15)


def test_a_window_below_the_threshold_does_not_alarm():
    """Two in-window occurrences (a third fell OUT of the window) ⇒ no alarm."""
    spans = _FakeSpans(
        [
            _span("recent-a", minutes_ago=2),
            _span("recent-b", minutes_ago=9),
            # Older than the 15-minute window — must not be counted.
            _span("stale", minutes_ago=40),
        ]
    )
    fired = []
    monitor = GuardUnavailableMonitor(
        spans, notify=fired.append, threshold=3, window=timedelta(minutes=15)
    )

    assert monitor.check(now=NOW) is None
    assert fired == []


def test_the_routine_advisory_is_not_counted_as_a_guard_unavailable():
    """``nemo-output-flag-v1`` means the pod ANSWERED — the answer was guarded."""
    spans = _FakeSpans(
        [_span(f"advisory-{i}", minutes_ago=i + 1, rule_id=ADVISORY_RULE_ID) for i in range(6)]
    )
    fired = []
    monitor = GuardUnavailableMonitor(
        spans, notify=fired.append, threshold=3, window=timedelta(minutes=15)
    )

    assert monitor.check(now=NOW) is None
    assert fired == []


def test_a_sustained_outage_notifies_once_per_window_not_once_per_check():
    """
    A pod outage is sustained by nature; one page per window, not per check.

    Twelve one-minute checks over a window that stays tripped must produce ONE
    notification, and every check must still REPORT the alarm (so a dashboard
    renders current state) with ``notified`` recording the suppression.
    """
    # An outage that keeps refusing every 30 seconds, from 15 minutes before the
    # first check to well past the last one.
    spans = _FakeSpans([_span(f"outage-{i}", minutes_ago=15 - 0.5 * i) for i in range(70)])
    fired = []
    monitor = GuardUnavailableMonitor(
        spans,
        notify=fired.append,
        threshold=3,
        window=timedelta(minutes=15),
        notify_interval=timedelta(minutes=15),
    )

    alarms = [monitor.check(now=NOW + timedelta(minutes=minute)) for minute in range(12)]

    assert all(alarm is not None for alarm in alarms)
    assert len(fired) == 1, "a sustained outage must not produce an alert storm"
    assert alarms[0].notified is True
    assert [alarm.notified for alarm in alarms[1:]] == [False] * 11

    # Once the notify interval has elapsed and the condition PERSISTS, it pages
    # again — suppression is a rate limit, not a mute.
    later = monitor.check(now=NOW + timedelta(minutes=15))
    assert later is not None and later.notified is True
    assert len(fired) == 2


def test_the_mode_b_guard_unavailable_decision_is_countable_in_the_envelope():
    """
    The envelope-side counterpart: ``guardrail_decisions[]`` is countable too.

    This is the exact decision shape a Mode-B (pod unreachable) fail-CLOSED
    response carries — asserted against the real orchestrator in
    ``services/orchestrator/tests/test_gate_mode_b.py``. A guard-unavailable
    refusal (``decision == "guard-unavailable"``) and a routine advisory
    (``decision == "flag"``) differ, but the rule id is the precise,
    decision-vocab-stable discriminator, so we count on it.
    """
    decisions = [
        {
            "stage": "output",
            "decision": "guard-unavailable",
            "category": "nemo",
            "rule_id": GUARD_UNAVAILABLE_RULE_ID,
            "rationale": (
                "NeMo guardrail pod unavailable on the output/facts path — failing closed"
            ),
        },
        {
            "stage": "output",
            "decision": "flag",
            "category": "nemo",
            "rule_id": ADVISORY_RULE_ID,
            "rationale": "NeMo output/facts advisory",
        },
    ]

    refused = guard_unavailable_decisions(decisions)

    assert len(refused) == 1
    assert refused[0]["rule_id"] == GUARD_UNAVAILABLE_RULE_ID


def test_a_misconfigured_watch_fails_at_construction():
    """A watch built to watch nothing must fail loudly, not watch nothing quietly."""
    spans = _FakeSpans([])
    with pytest.raises(ValueError):
        GuardUnavailableMonitor(spans, threshold=0)
    with pytest.raises(ValueError):
        GuardUnavailableMonitor(spans, window=timedelta(0))
