r"""
Phoenix-native PARITY GATE driver — in-house ``1.4.0`` vs nemo-all ``1.8.0``.

The Phase-2 gate that authorizes cutover. This is a thin SHELL over the parity
library (``app.phoenix.guardrail_ab`` + ``dev.guardrail_ab_arms``) — there is
deliberately no parallel A/B lane. It:

  1. loads the committed labelled QUESTION-lane parity set (>=200 benign legal
     queries + >=20-30 per attack class);
  2. runs the END-TO-END PRE-PASS — ``POST /query`` once per (arm, question) on
     each config pin — capturing each config's REAL generated answer + chunks +
     guard decision. NO crafted answers are ever injected;
  3. records a dataset + one experiment per arm IN Phoenix (Phoenix-native: a CSV
     alone does not count); and
  4. prints the per-class confusion and applies the HARD acceptance bars, exiting
     NON-ZERO if the gate is RED.

What this run does and does not cover
-------------------------------------
It scores the LLM classes (jailbreak/prompt-leak, policy, grounding) and the
benign slice — the verdicts that need a live Bedrock rail, and therefore the ones
that cannot be asserted in CI.

It does NOT score the DETERMINISTIC classes (secrets / high-sev PII / low-sev
PII). Those are the ANSWER lane: a `/query` pre-pass cannot make a legal
generator emit an SSN on demand, and crafting an answer to force it would be the
engineered corpus this harness refuses. They are instead asserted END-TO-END
THROUGH THE POD — real detectors, real route, real short-circuit ordering — in
``services/guardrail/tests/test_gate_parity_and_mode_a.py``, which is
deterministic and runs in CI because the deterministic rail is pure regex. Both
failure-injection modes are likewise CI-asserted next to the code that owns each
fail policy (Mode A at the pod boundary; Mode B in the orchestrator pure stage).

The gate is GREEN only when this run AND those suites are green.

This driver never flips a default — it only produces the evidence. Cutover is
Task Group 4 and is the USER's go/no-go.

Run from the repo root once the pod + orchestrator + Phoenix are up (needs live
Bedrock: the pre-pass generates a real answer per question, per arm):

    set -a; . .env; set +a
    PHOENIX_ENDPOINT=http://localhost:6006 \
    ORCHESTRATOR_URL=http://localhost:8000 \
    services/eval/.venv/bin/python services/eval/scripts/guardrail_parity_gate.py
"""

from __future__ import annotations

import os
import sys

from app.phoenix.guardrail_ab import (
    IN_HOUSE_CONFIG_REF,
    NEMO_ALL_CONFIG_REF,
    PARITY_DATASET_NAME,
    ClassConfusion,
    GateReport,
    _print_metrics,
    run_shadow_ab,
)


def _print_confusion(label: str, confusion: dict[str, ClassConfusion]) -> None:
    """Print one arm's per-class confusion (the gate's required report)."""
    print(f"  arm {label}:")
    header = (
        f"    {'class':24s} {'n':>4s} {'exp':>6s} "
        f"{'block':>6s} {'flag':>5s} {'allow':>6s} {'match':>7s}"
    )
    print(header)
    for name in sorted(confusion):
        c = confusion[name]
        print(
            f"    {c.attack_class:24s} {c.num_rows:4d} {c.expected:>6s} "
            f"{c.blocked:6d} {c.flagged:5d} {c.allowed:6d} {c.match_rate:7.3f}"
        )


def _print_gate(gate: GateReport) -> None:
    """Print every acceptance bar's verdict + the overall gate result."""
    print("\nPer-class confusion:")
    _print_confusion(IN_HOUSE_CONFIG_REF, gate.confusion_a)
    _print_confusion(NEMO_ALL_CONFIG_REF, gate.confusion_b)

    print("\nAcceptance bars (HARD — not measure-and-review):")
    for check in gate.checks:
        status = "PASS" if check.passed else "FAIL"
        print(f"  [{status}] {check.bar:14s} {check.name:24s} {check.detail}")


def main() -> int:
    """
    Run the live parity gate end-to-end (USER-owned; real Bedrock + Phoenix).

    Returns:
        0 when every acceptance bar passed, 1 otherwise (so CI / a human can gate
        on the exit code rather than on reading the prose).

    """
    # Local (dev/-only) imports: the arm builders + pre-pass drive the
    # orchestrator over HTTP; kept inside main so importing this module never
    # touches a live pod / AWS / Phoenix.
    from app.phoenix.experiments import create_phoenix_client
    from dev.guardrail_ab_arms import (
        build_in_house_arm,
        build_nemo_arm,
        capture_arm,
        corpus_with_captured_answers,
        load_legal_corpus,
    )

    endpoint = os.environ.get("PHOENIX_ENDPOINT", "http://localhost:6006")
    client = create_phoenix_client(endpoint)

    corpus = load_legal_corpus()
    benign = sum(1 for item in corpus if item.is_benign)
    print(
        f"Loaded {len(corpus)} labelled parity questions ({benign} benign, "
        f"{len(corpus) - benign} attack)."
    )

    print(f"Pre-pass: capturing real answers on {IN_HOUSE_CONFIG_REF} (arm A, in-house)…")
    captured_a = capture_arm(IN_HOUSE_CONFIG_REF, corpus)
    print(f"Pre-pass: capturing real answers on {NEMO_ALL_CONFIG_REF} (arm B, nemo-all)…")
    captured_b = capture_arm(NEMO_ALL_CONFIG_REF, corpus)

    arm_a = build_in_house_arm(IN_HOUSE_CONFIG_REF, captured_a)
    arm_b = build_nemo_arm(NEMO_ALL_CONFIG_REF, captured_b)

    # The dataset records the arm-under-scrutiny (nemo-all) captured answers; each
    # arm still replays its OWN captured outcomes.
    dataset_corpus = corpus_with_captured_answers(corpus, captured_b)

    result = run_shadow_ab(
        client=client,
        arm_a=arm_a,
        arm_b=arm_b,
        corpus=dataset_corpus,
        dataset_name=PARITY_DATASET_NAME,
        experiment_prefix="guardrail-parity-gate",
    )

    print("\nRecorded IN Phoenix (dataset + one experiment per arm):")
    print(f"  dataset      : {result.dataset}")
    print(f"  experiment A : {result.experiment_a}")
    print(f"  experiment B : {result.experiment_b}")

    print("\nFour review metrics per arm (measure-and-review):")
    _print_metrics(result.metrics_a)
    _print_metrics(result.metrics_b)

    gate = result.gate
    assert gate is not None  # run_shadow_ab always reports the gate
    _print_gate(gate)

    if gate.passed:
        print(
            "\nGATE: GREEN — LLM-class recall match-or-beat 1.4.0 with no worse "
            "benign FP/over-refusal.\nThis authorizes cutover ONLY together with "
            "the CI-asserted deterministic parity + Mode A/B failure-injection "
            "suites. The flip itself remains the USER's go/no-go."
        )
        return 0

    print(
        "\nGATE: RED — at least one acceptance bar failed (see FAIL rows above). "
        "Do NOT cut over."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
