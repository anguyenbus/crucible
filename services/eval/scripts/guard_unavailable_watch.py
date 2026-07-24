r"""
Guard-unavailable WATCH driver — one scheduled check of the output-guard-unavailable window.

A thin SHELL over ``app.phoenix.guard_unavailable_monitor``: it builds the Phoenix
client (the ONE place a client is constructed — the monitor itself stays injected
and hermetic), runs a single windowed check against the orchestrator's spans,
prints the outcome, and exits NON-ZERO when the alarm trips, so a cron entry / K8s
CronJob / CI step can gate on the exit code rather than on reading prose.

**Repoints the retired fail-open watch.** The output lane now fails CLOSED
(guardrail spec ruling 0.3): a pod-unreachable REFUSES the answer with
``nemo-output-guard-unavailable-v1`` rather than delivering it unguarded. This
driver watches that refusal window — a sustained output-guard outage.

This is the "scheduled check in the eval service" half of the placement decision
recorded in the spec folder's ``implementation/fail-open-monitor-decision.md``.
Being a one-shot process, the per-window notification suppression that
:class:`GuardUnavailableMonitor` provides within a long-lived process does not
carry across invocations — the SCHEDULE is what sets the notification rate here,
and the interval it is invoked on should be the window length for the same
one-page-per-window behaviour.

Run it once Phoenix is up (it needs no AWS, no pod, and no orchestrator — it
reads records that already landed):

    PHOENIX_ENDPOINT=http://localhost:6006 \
    GUARD_UNAVAILABLE_WINDOW_MINUTES=15 \
    GUARD_UNAVAILABLE_THRESHOLD=3 \
    services/eval/.venv/bin/python services/eval/scripts/guard_unavailable_watch.py

Exit codes: ``0`` window clean, ``2`` ALARM (answers refused — output guard
unavailable), ``1`` the watch itself could not run (which is NOT "clean" — an
unreadable watch is the silence this item exists to remove).
"""

from __future__ import annotations

import os
import sys
from datetime import timedelta

from app.phoenix.guard_unavailable_monitor import (
    DEFAULT_THRESHOLD,
    GUARD_UNAVAILABLE_RULE_ID,
    ORCHESTRATOR_PROJECT,
    GuardUnavailableMonitor,
)

DEFAULT_PHOENIX_ENDPOINT = "http://localhost:6006"
DEFAULT_WINDOW_MINUTES = 15

EXIT_CLEAN = 0
EXIT_WATCH_FAILED = 1
EXIT_ALARM = 2


def main() -> int:
    """
    Run one windowed guard-unavailable check and report it.

    Returns:
        ``EXIT_CLEAN`` when the window is below threshold, ``EXIT_ALARM`` when it
        trips, ``EXIT_WATCH_FAILED`` when Phoenix could not be read.

    """
    endpoint = os.environ.get("PHOENIX_ENDPOINT", DEFAULT_PHOENIX_ENDPOINT)
    project = os.environ.get("GUARD_UNAVAILABLE_PROJECT", ORCHESTRATOR_PROJECT)
    window = timedelta(
        minutes=float(os.environ.get("GUARD_UNAVAILABLE_WINDOW_MINUTES", DEFAULT_WINDOW_MINUTES))
    )
    threshold = int(os.environ.get("GUARD_UNAVAILABLE_THRESHOLD", DEFAULT_THRESHOLD))

    from phoenix.client import Client

    client = Client(base_url=endpoint)
    fired: list[str] = []
    monitor = GuardUnavailableMonitor(
        client.spans,
        notify=lambda alarm: fired.append(alarm.message),
        project=project,
        window=window,
        threshold=threshold,
    )

    print(
        f"Guard-unavailable watch — rule {GUARD_UNAVAILABLE_RULE_ID}, project {project!r}, "
        f"window {window}, threshold {threshold}, phoenix {endpoint}"
    )
    try:
        alarm = monitor.check()
    except Exception as exc:  # noqa: BLE001 — an unreadable watch must be LOUD
        print(f"WATCH FAILED: could not read Phoenix ({type(exc).__name__}: {exc})")
        return EXIT_WATCH_FAILED

    if alarm is None:
        print(
            f"CLEAN: fewer than {threshold} guard-unavailable refusals in the window. "
            "The output guard is adjudicating (or the pod is reachable)."
        )
        return EXIT_CLEAN

    for message in fired:
        print(f"ALARM: {message}")
    print("Evidence (Phoenix span ids):")
    for span_id in alarm.span_ids:
        print(f"  {span_id}")
    return EXIT_ALARM


if __name__ == "__main__":
    sys.exit(main())
