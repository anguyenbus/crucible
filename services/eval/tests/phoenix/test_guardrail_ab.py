"""Unit tests for the Phoenix-native guardrail parity A/B harness (MOCKED).

Covers task 5.1: the harness records a dataset + experiment IN Phoenix (not
CSV-only) and the metric aggregation computes the four review metrics — block
rate, false-positive rate on the legal corpus, latency p50/p95, and per-query
token cost — from recorded rows.

Everything here is MOCKED: the Phoenix ``client`` is a fake (no Phoenix server)
and both arms return canned :class:`GuardOutcome`s (no live pod, no AWS / Bedrock).
The harness never builds a Phoenix client and never imports ``phoenix`` at module
scope, so this suite runs hermetically WITHOUT the ``phoenix`` extra — no
``importorskip`` needed, unlike the RAG experiment tests.

One test group (the ``ExampleProxy`` replay + the live localhost experiment) DOES
exercise the real ``phoenix.client`` example shape — those tests ``importorskip``
``phoenix`` and skip cleanly where it (or a running server) is absent. They are
the regression guard for the mocked-green/live-broken defect the acceptance drill
surfaced: the replay task's parameter is named ``example``, so real Phoenix binds
it to an ``ExampleProxy`` (a ``Mapping``, NOT a ``dict``); the old task did
``isinstance(example, dict)`` and otherwise ``str(example)``, so against the real
proxy every task_run recorded ``{"error": "no recorded row"}`` while the printed
metrics (aggregated from LOCAL rows) still looked fine.
"""

from __future__ import annotations

import socket
from typing import Any
from urllib.parse import urlparse

import pytest
from app.phoenix.guardrail_ab import (
    DEFAULT_DATASET_NAME,
    IN_HOUSE_CONFIG_REF,
    NEMO_ALL_CONFIG_REF,
    ABRow,
    CorpusItem,
    GuardOutcome,
    _make_arm_task,
    aggregate_metrics,
    evaluate_gate,
    per_class_confusion,
    run_shadow_ab,
)


class _FakeArm:
    """A canned-outcome arm: returns a scripted ``GuardOutcome`` per query_id.

    The harness calls ``check`` positionally-by-keyword per corpus item; this fake
    records the calls and replays the scripted outcome, so no live guard / pod /
    AWS is touched.
    """

    def __init__(self, config_ref: str, outcomes: dict[str, GuardOutcome]) -> None:
        self.config_ref = config_ref
        self._outcomes = outcomes
        self.calls: list[dict[str, Any]] = []

    def check(
        self, *, question: str, answer: str, chunks: tuple[str, ...]
    ) -> GuardOutcome:
        self.calls.append({"question": question, "answer": answer, "chunks": chunks})
        # Script outcomes by question text (unique per corpus item below).
        return self._outcomes[question]


class _FakeDatasets:
    """Fake Phoenix ``client.datasets`` — get-or-create, records the create call."""

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    def get_dataset(self, *, dataset: str) -> Any:
        # Force the create path (no pre-existing dataset) so the test observes it.
        raise KeyError(dataset)

    def create_dataset(self, **kwargs: Any) -> dict[str, Any]:
        self.created.append(kwargs)
        return {"dataset_id": "ds-1", "name": kwargs.get("name")}


class _FakeExperiments:
    """Fake Phoenix ``client.experiments`` — records each ``run_experiment`` call."""

    def __init__(self) -> None:
        self.runs: list[dict[str, Any]] = []

    def run_experiment(self, **kwargs: Any) -> dict[str, Any]:
        self.runs.append(kwargs)
        return {
            "experiment_id": f"exp-{len(self.runs)}",
            "name": kwargs["experiment_name"],
        }


class _FakeClient:
    """A stand-in Phoenix client exposing ``datasets`` + ``experiments``."""

    def __init__(self) -> None:
        self.datasets = _FakeDatasets()
        self.experiments = _FakeExperiments()


def _corpus() -> list[CorpusItem]:
    """A tiny mixed corpus: three benign legal rows + one labelled attack row."""
    return [
        CorpusItem(query_id="q1", question="benign-1", answer="a1", is_benign=True),
        CorpusItem(query_id="q2", question="benign-2", answer="a2", is_benign=True),
        CorpusItem(query_id="q3", question="benign-3", answer="a3", is_benign=True),
        CorpusItem(query_id="q4", question="attack-1", answer="a4", is_benign=False),
    ]


# --------------------------------------------------------------------------- #
# 5.1(i): the harness records a DATASET + EXPERIMENT in Phoenix (not CSV-only)
# --------------------------------------------------------------------------- #


def test_run_shadow_ab_records_dataset_and_one_experiment_per_arm():
    """One Phoenix dataset is created and one experiment is run per arm."""
    client = _FakeClient()
    corpus = _corpus()

    # Arm A blocks nothing; arm B blocks the attack row only (both hermetic).
    outcomes_a = {
        item.question: GuardOutcome(blocked=False, latency_ms=10.0) for item in corpus
    }
    outcomes_b = {
        item.question: GuardOutcome(
            blocked=not item.is_benign,
            latency_ms=40.0,
            input_tokens=170,
            output_tokens=110,
        )
        for item in corpus
    }
    arm_a = _FakeArm(IN_HOUSE_CONFIG_REF, outcomes_a)
    arm_b = _FakeArm(NEMO_ALL_CONFIG_REF, outcomes_b)

    result = run_shadow_ab(client=client, arm_a=arm_a, arm_b=arm_b, corpus=corpus)

    # A dataset object was created IN Phoenix (not a CSV) carrying the corpus.
    assert len(client.datasets.created) == 1
    created = client.datasets.created[0]
    assert len(created["inputs"]) == len(corpus)
    assert {m["query_id"] for m in created["metadata"]} == {"q1", "q2", "q3", "q4"}
    # The stable query_id is ALSO carried on each input dict so the replay task can
    # key on the input Phoenix round-trips verbatim (the live-broken fix).
    assert {i["query_id"] for i in created["inputs"]} == {"q1", "q2", "q3", "q4"}
    assert result.dataset == {"dataset_id": "ds-1", "name": created["name"]}

    # Exactly one experiment per arm, each tagged with its config ref.
    assert len(client.experiments.runs) == 2
    run_names = {r["experiment_name"] for r in client.experiments.runs}
    assert any(IN_HOUSE_CONFIG_REF in n for n in run_names)
    assert any(NEMO_ALL_CONFIG_REF in n for n in run_names)
    assert result.experiment_a["experiment_id"] == "exp-1"
    assert result.experiment_b["experiment_id"] == "exp-2"

    # Both arms were actually shadowed over every corpus item.
    assert len(arm_a.calls) == len(corpus)
    assert len(arm_b.calls) == len(corpus)


def test_experiment_task_replays_recorded_outcomes_for_phoenix():
    """The task Phoenix runs replays each arm's recorded decision per example."""
    client = _FakeClient()
    corpus = _corpus()
    outcomes = {
        item.question: GuardOutcome(
            blocked=not item.is_benign,
            latency_ms=42.0,
            input_tokens=5,
            output_tokens=3,
        )
        for item in corpus
    }
    arm_a = _FakeArm(IN_HOUSE_CONFIG_REF, outcomes)
    arm_b = _FakeArm(NEMO_ALL_CONFIG_REF, outcomes)

    run_shadow_ab(client=client, arm_a=arm_a, arm_b=arm_b, corpus=corpus)

    # Pull the task Phoenix would call and confirm it replays the attack block —
    # first against a legacy plain-dict example (defensive back-compat), then the
    # metadata-only shape.
    task = client.experiments.runs[0]["task"]
    replayed = task({"input": {"query_id": "q4"}, "metadata": {"query_id": "q4"}})
    assert replayed["blocked"] is True
    assert replayed["is_benign"] is False
    assert replayed["input_tokens"] == 5
    # query_id living ONLY in metadata still resolves (fallback path).
    assert task({"metadata": {"query_id": "q1"}})["blocked"] is False


def _phoenix_example_proxy(*, query_id: str, question: str, is_benign: bool) -> Any:
    """Build the REAL ``phoenix.client`` example object the task is bound to.

    ``phoenix.client``'s ``_bind_task_signature`` binds a task parameter named
    ``example`` to ``ExampleProxy(v1.DatasetExample)`` — a ``Mapping``, NOT a
    ``dict``. This reconstructs the exact wrapped shape ``_upload_dataset`` yields
    on the server (input carries ``query_id``; metadata carries it too), so the
    task is exercised through the real object, not a hand-shaped dict.
    """
    types = pytest.importorskip("phoenix.client.resources.experiments.types")
    wrapped = {
        "id": f"ex-{query_id}",
        "node_id": f"node-{query_id}",
        "input": {"input": question, "query_id": query_id},
        "output": {"answer": "a"},
        "metadata": {"query_id": query_id, "is_benign": is_benign},
        "updated_at": "2026-07-17T00:00:00+00:00",
    }
    return types.ExampleProxy(wrapped)


def test_experiment_task_resolves_real_phoenix_example_proxy():
    """REGRESSION: the task resolves its row against Phoenix's real ExampleProxy.

    The old task did ``isinstance(example, dict)`` then ``str(example)``. Against
    the real ``ExampleProxy`` (a ``Mapping``, not a ``dict``) that fell through to
    the proxy's ``repr``, so EVERY live task_run recorded ``no recorded row`` — the
    exact mocked-green/live-broken defect. The prior mocked test passed a plain
    ``dict`` and never saw it. This feeds the task the REAL proxy, so it FAILS on
    the old code and PASSES on the fix.
    """
    rows = {
        "q4": ABRow(
            "q4",
            False,
            GuardOutcome(
                blocked=True, latency_ms=42.0, input_tokens=5, output_tokens=3
            ),
        ),
        "q1": ABRow(
            "q1", True, GuardOutcome(blocked=False, latency_ms=10.0)
        ),
    }
    task = _make_arm_task(rows)

    # Attack row via the REAL proxy shape → resolves the recorded BLOCK.
    proxy_attack = _phoenix_example_proxy(
        query_id="q4", question="attack-1", is_benign=False
    )
    replayed = task(proxy_attack)
    assert replayed.get("error", "") != "no recorded row"
    assert replayed["blocked"] is True
    assert replayed["is_benign"] is False
    assert replayed["input_tokens"] == 5

    # Benign row via the real proxy → resolves the recorded ALLOW.
    proxy_benign = _phoenix_example_proxy(
        query_id="q1", question="benign-1", is_benign=True
    )
    assert task(proxy_benign)["blocked"] is False


# --------------------------------------------------------------------------- #
# 5.1(iii): the STRONGEST guard — a REAL local Phoenix experiment (no AWS, no
# orchestrator, no Bedrock: the guard calls happened in the pre-pass; this only
# REPLAYS canned outcomes) records real per-row decisions in the task_runs.
# --------------------------------------------------------------------------- #


def _phoenix_base_url() -> str:
    import os

    return (
        os.environ.get("PHOENIX_COLLECTOR_ENDPOINT")
        or os.environ.get("PHOENIX_HOST")
        or "http://localhost:6006"
    )


def _phoenix_reachable(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 6006)
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def test_real_local_phoenix_experiment_records_real_decisions():
    """A REAL localhost Phoenix experiment records real per-row decisions.

    Zero AWS / Bedrock / orchestrator: ``run_shadow_ab`` REPLAYS canned
    ``GuardOutcome``s into a real ``phoenix.client`` experiment. If the task could
    not resolve its row (the live-broken bug), every task_run's output would be
    ``{"blocked": null, "error": "no recorded row"}``. We assert the recorded
    task_runs carry the REAL blocked/allow decisions instead.
    """
    pytest.importorskip("phoenix.client")
    from phoenix.client import Client

    base_url = _phoenix_base_url()
    if not _phoenix_reachable(base_url):
        pytest.skip(f"no Phoenix server reachable at {base_url}")

    client = Client(base_url=base_url)
    corpus = _corpus()
    # Unique dataset name per run so we never collide with a prior run's rows.
    import uuid

    dataset_name = f"{DEFAULT_DATASET_NAME}-pytest-{uuid.uuid4().hex[:8]}"

    outcomes_a = {
        item.question: GuardOutcome(blocked=False, latency_ms=12.0) for item in corpus
    }
    outcomes_b = {
        item.question: GuardOutcome(
            blocked=not item.is_benign,
            latency_ms=44.0,
            input_tokens=150,
            output_tokens=10,
        )
        for item in corpus
    }
    arm_a = _FakeArm(IN_HOUSE_CONFIG_REF, outcomes_a)
    arm_b = _FakeArm(NEMO_ALL_CONFIG_REF, outcomes_b)

    result = run_shadow_ab(
        client=client,
        arm_a=arm_a,
        arm_b=arm_b,
        corpus=corpus,
        dataset_name=dataset_name,
    )

    # The NeMo experiment's task_runs must carry the REAL decisions, not the
    # "no recorded row" sentinel. Read them back off the experiment object.
    outputs = _experiment_run_outputs(result.experiment_b)
    assert outputs, "expected recorded task_runs on the live experiment"
    assert all(
        (o or {}).get("error") != "no recorded row" for o in outputs
    ), f"live task_runs failed to resolve their row: {outputs}"
    # Exactly the labelled attack row blocked (block True), the benign rows allow.
    blocked_flags = sorted(bool((o or {}).get("blocked")) for o in outputs)
    assert blocked_flags == [False, False, False, True]


def _experiment_run_outputs(experiment: Any) -> list[dict[str, Any]]:
    """Extract each task_run's task output dict from a phoenix.client experiment."""
    runs = None
    for attr in ("runs", "task_runs"):
        runs = getattr(experiment, attr, None)
        if runs is None and isinstance(experiment, dict):
            runs = experiment.get(attr)
        if runs is not None:
            break
    if runs is None:
        return []
    values = runs.values() if isinstance(runs, dict) else runs
    outputs: list[dict[str, Any]] = []
    for run in values:
        out = getattr(run, "output", None)
        if out is None and isinstance(run, dict):
            out = run.get("output")
        outputs.append(out if isinstance(out, dict) else {})
    return outputs


# --------------------------------------------------------------------------- #
# 5.1(ii): metric aggregation — block rate, FP rate, latency p50/p95, token cost
# --------------------------------------------------------------------------- #


def test_aggregate_metrics_computes_the_four_review_metrics():
    """Block rate, FP rate, latency p50/p95, and per-query token cost from rows."""
    # 3 benign + 1 attack; blocks land on one benign row (a false positive) AND
    # the attack row. Latencies are chosen so p50/p95 are exact.
    rows = (
        ABRow(
            "q1",
            True,
            GuardOutcome(blocked=True, latency_ms=10.0, input_tokens=100, output_tokens=20),
        ),
        ABRow(
            "q2",
            True,
            GuardOutcome(blocked=False, latency_ms=20.0, input_tokens=0, output_tokens=0),
        ),
        ABRow(
            "q3",
            True,
            GuardOutcome(blocked=False, latency_ms=30.0, input_tokens=0, output_tokens=0),
        ),
        ABRow(
            "q4",
            False,
            GuardOutcome(blocked=True, latency_ms=40.0, input_tokens=200, output_tokens=40),
        ),
    )

    metrics = aggregate_metrics(NEMO_ALL_CONFIG_REF, rows)

    assert metrics.config_ref == NEMO_ALL_CONFIG_REF
    assert metrics.num_queries == 4
    # 2 of 4 rows blocked.
    assert metrics.block_rate == 0.5
    # 1 benign block out of 3 benign rows is the false-positive rate.
    assert abs(metrics.false_positive_rate - (1 / 3)) < 1e-9
    # Latencies [10, 20, 30, 40]: interpolated p50 = 25, p95 = 38.5.
    assert metrics.latency_p50_ms == 25.0
    assert abs(metrics.latency_p95_ms - 38.5) < 1e-9
    # Per-query token cost: totals [120, 0, 0, 240] -> mean input 75, output 15,
    # total 90.
    assert metrics.mean_input_tokens == 75.0
    assert metrics.mean_output_tokens == 15.0
    assert metrics.mean_total_tokens == 90.0


def test_run_shadow_ab_surfaces_distinct_metrics_per_arm():
    """The A/B result carries independent four-metric aggregates for each arm."""
    client = _FakeClient()
    corpus = _corpus()

    # In-house arm: zero blocks, cheap/fast. NeMo arm: blocks the attack row,
    # slower (the extra pod hop), and bills guard tokens.
    outcomes_a = {
        item.question: GuardOutcome(blocked=False, latency_ms=12.0) for item in corpus
    }
    outcomes_b = {
        item.question: GuardOutcome(
            blocked=not item.is_benign,
            latency_ms=45.0,
            input_tokens=170,
            output_tokens=110,
        )
        for item in corpus
    }
    arm_a = _FakeArm(IN_HOUSE_CONFIG_REF, outcomes_a)
    arm_b = _FakeArm(NEMO_ALL_CONFIG_REF, outcomes_b)

    result = run_shadow_ab(client=client, arm_a=arm_a, arm_b=arm_b, corpus=corpus)

    # In-house arm never blocks and has zero FP on the benign rows.
    assert result.metrics_a.config_ref == IN_HOUSE_CONFIG_REF
    assert result.metrics_a.block_rate == 0.0
    assert result.metrics_a.false_positive_rate == 0.0
    assert result.metrics_a.mean_total_tokens == 0.0

    # NeMo arm blocks only the labelled attack row: block rate 1/4, ZERO FP on
    # the benign legal corpus, and non-trivial added latency + token cost.
    assert result.metrics_b.config_ref == NEMO_ALL_CONFIG_REF
    assert result.metrics_b.block_rate == 0.25
    assert result.metrics_b.false_positive_rate == 0.0
    assert result.metrics_b.latency_p50_ms == 45.0
    assert result.metrics_b.mean_total_tokens == 280.0


# --------------------------------------------------------------------------- #
# Phase-2 GATE bars (the assertions that authorize cutover). Hermetic: the bars
# are pure functions of recorded rows.
# --------------------------------------------------------------------------- #


def _row(query_id: str, attack_class: str, expected: str, verdict: str) -> ABRow:
    """One recorded row whose arm produced `verdict` for a labelled expectation."""
    return ABRow(
        query_id=query_id,
        is_benign=attack_class == "benign",
        outcome=GuardOutcome(
            blocked=verdict == "block", flag=verdict == "flag", latency_ms=1.0
        ),
        attack_class=attack_class,
        expected=expected,
    )


def test_per_class_confusion_scores_low_sev_pii_flag_not_block():
    """A BLOCK on low-severity PII is an over-refusal, not a hit — the 3-valued
    verdict must not be collapsed to blocked/not-blocked."""
    rows = [
        _row("a", "pii_low", "flag", "flag"),  # correct: flagged + delivered
        _row("b", "pii_low", "flag", "block"),  # over-refusal
        _row("c", "pii_low", "flag", "allow"),  # missed the flag
    ]

    confusion = per_class_confusion(rows)["pii_low"]

    assert (confusion.num_rows, confusion.matched) == (3, 1)
    assert (confusion.blocked, confusion.flagged, confusion.allowed) == (1, 1, 1)
    assert confusion.match_rate == pytest.approx(1 / 3)


def test_gate_passes_when_1_8_0_matches_deterministically_and_holds_both_llm_sides():
    """The gate is GREEN on exact deterministic parity + equal recall + equal FP."""
    rows_a = [
        _row("s1", "secrets", "block", "block"),
        _row("p1", "policy", "block", "block"),
        _row("b1", "benign", "allow", "allow"),
    ]
    rows_b = [
        _row("s1", "secrets", "block", "block"),
        _row("p1", "policy", "block", "block"),
        _row("b1", "benign", "allow", "allow"),
    ]

    report = evaluate_gate(rows_a, rows_b)

    assert report.passed
    assert {check.bar for check in report.checks} == {
        "deterministic",
        "llm-recall",
        "benign-fp",
    }


def test_gate_fails_on_a_single_deterministic_verdict_mismatch():
    """Deterministic classes are all-or-nothing: ONE wrong end-to-end verdict fails."""
    rows_a = [_row("s1", "secrets", "block", "block")]
    # 1.8.0 delivers a secret — a wiring / serialization / ordering bug.
    rows_b = [_row("s1", "secrets", "block", "allow")]

    report = evaluate_gate(rows_a, rows_b)

    assert not report.passed
    deterministic = [c for c in report.checks if c.bar == "deterministic"]
    assert len(deterministic) == 1 and not deterministic[0].passed


def test_gate_fails_on_an_llm_recall_regression_even_with_perfect_benign_fp():
    """Side one of the two-sided bar: recall must match-or-beat 1.4.0."""
    rows_a = [
        _row("j1", "jailbreak_prompt_leak", "block", "block"),
        _row("j2", "jailbreak_prompt_leak", "block", "block"),
        _row("b1", "benign", "allow", "allow"),
    ]
    # 1.8.0 misses one jailbreak that 1.4.0 caught (FP is perfect on both).
    rows_b = [
        _row("j1", "jailbreak_prompt_leak", "block", "block"),
        _row("j2", "jailbreak_prompt_leak", "block", "allow"),
        _row("b1", "benign", "allow", "allow"),
    ]

    report = evaluate_gate(rows_a, rows_b)

    assert not report.passed
    recall = [c for c in report.checks if c.bar == "llm-recall"][0]
    assert not recall.passed


def test_gate_fails_on_a_benign_false_positive_regression_even_with_better_recall():
    """Side two of the two-sided bar: better recall does NOT buy a worse FP rate."""
    rows_a = [
        _row("p1", "policy", "block", "allow"),  # 1.4.0 misses it
        _row("b1", "benign", "allow", "allow"),
        _row("b2", "benign", "allow", "allow"),
    ]
    # 1.8.0 catches the policy row but starts refusing benign legal traffic.
    rows_b = [
        _row("p1", "policy", "block", "block"),
        _row("b1", "benign", "allow", "allow"),
        _row("b2", "benign", "allow", "block"),
    ]

    report = evaluate_gate(rows_a, rows_b)

    recall = [c for c in report.checks if c.bar == "llm-recall"][0]
    benign_fp = [c for c in report.checks if c.bar == "benign-fp"][0]
    assert recall.passed, "recall genuinely improved"
    assert not benign_fp.passed, "but the over-refusal regression must fail the gate"
    assert not report.passed
