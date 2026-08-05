"""
Task Group 1: the parity gate's CONTRACT is ``secrets:`` only.

These are CONTRACT assertions, not a re-test of the harness (that lives in
``test_guardrail_ab.py``). They exist to close the ordering trap: the pod's
``pii:`` detector table is being retired, and if the parity gate still carried
``pii_high`` / ``pii_low`` as contract classes it would score the pod against a
capability it was deliberately told to stop having — producing a meaningless
number, or a green-looking report that quietly measured a withdrawal.

Hermetic: pure functions over hand-built rows. No Phoenix server, no AWS, no pod.
"""

from __future__ import annotations

from app.phoenix.guardrail_ab import (
    BENIGN_CLASS,
    CONTRACT_CLASSES,
    DETERMINISTIC_CLASSES,
    WITHDRAWN_CLASSES,
    ABRow,
    GuardOutcome,
    evaluate_gate,
    per_class_confusion,
)


def _row(query_id: str, attack_class: str, expected: str, verdict: str) -> ABRow:
    """One recorded row whose arm produced ``verdict`` for a labelled expectation."""
    return ABRow(
        query_id=query_id,
        is_benign=attack_class == BENIGN_CLASS,
        outcome=GuardOutcome(
            blocked=verdict == "block", flag=verdict == "flag", latency_ms=1.0
        ),
        attack_class=attack_class,
        expected=expected,
    )


def test_deterministic_contract_is_secrets_only():
    """``pii_high`` / ``pii_low`` are absent from the CONTRACT classes."""
    assert DETERMINISTIC_CLASSES == ("secrets",)
    assert WITHDRAWN_CLASSES == ("pii_high", "pii_low")
    for withdrawn in WITHDRAWN_CLASSES:
        assert withdrawn not in DETERMINISTIC_CLASSES
        assert withdrawn not in CONTRACT_CLASSES
    # The rest of the contract is untouched — this is a subtraction, not a rewrite.
    assert "secrets" in CONTRACT_CLASSES and BENIGN_CLASS in CONTRACT_CLASSES


def test_gate_neither_passes_nor_fails_on_a_pii_row():
    """A PII-labelled row cannot contribute to the gate verdict in either direction."""
    # Arm B gets EVERY pii row wrong (a block where a flag was expected, an allow
    # where a block was expected). Under the old contract that was a hard FAIL.
    pii_rows_b = [
        _row("ph1", "pii_high", "block", "allow"),
        _row("pl1", "pii_low", "flag", "block"),
    ]
    pii_rows_a = [
        _row("ph1", "pii_high", "block", "block"),
        _row("pl1", "pii_low", "flag", "flag"),
    ]
    baseline = [_row("s1", "secrets", "block", "block")]

    report = evaluate_gate(baseline + pii_rows_a, baseline + pii_rows_b)

    assert {check.name for check in report.checks}.isdisjoint(WITHDRAWN_CLASSES)
    # The withdrawn rows cannot FAIL the gate...
    assert report.passed
    # ...and they cannot PASS it either: with only PII rows present there is no
    # bar at all, so the gate raises no check on them.
    pii_only = evaluate_gate(pii_rows_a, pii_rows_b)
    assert pii_only.checks == ()


def test_per_class_confusion_tolerates_pii_rows_observationally():
    """PII rows still report without crashing — observational, never bar-bearing."""
    rows = [
        _row("s1", "secrets", "block", "block"),
        _row("pl1", "pii_low", "flag", "block"),
        _row("pl2", "pii_low", "flag", "flag"),
    ]

    confusion = per_class_confusion(rows)

    assert set(confusion) == {"secrets", "pii_low"}
    assert confusion["pii_low"].num_rows == 2
    assert confusion["pii_low"].bar_bearing is False
    assert confusion["secrets"].bar_bearing is True
