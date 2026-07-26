"""
Item 8: harness correctness for the determination-boundary calibration suite.

These are CONTRACT assertions about the measuring instrument, not a measurement:
they are deterministic, need no AWS and no Phoenix server, and they deliberately
assert the FAILING direction of both bars. A gate that has only ever been seen to
pass is not known to be a gate — the bar that matters here is the benign
false-positive rate, and a harness that could not go red would silently bless an
over-blocking prompt that deletes the product's primary function.

The ONE live row-level smoke at the bottom is marked ``requires_aws`` (the pod's
convention, mirrored into this service's conftest) and skips cleanly with no
credentials.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from app.phoenix.determination_boundary import (
    BAR_BENIGN_FP,
    BAR_MUST_BLOCK_CATCH,
    BENIGN_FALSE_POSITIVE_BAR,
    COUNSEL_SUBSTITUTE_CLASS,
    ENFORCEMENT_GUARANTEE_CLASS,
    MIN_BENIGN_ROWS,
    MIN_MUST_BLOCK_ROWS,
    MUST_BLOCK_CATCH_BAR,
    MUST_BLOCK_CLASSES,
    POD_CONFIG_VERSION,
    PREJUDGEMENT_CLASS,
    DeterminationRowError,
    ConfigVersionMismatchError,
    load_calibration_rows,
    run_calibration,
    score_calibration,
)
from app.phoenix.guardrail_ab import (
    BENIGN_CLASS,
    VERDICT_ALLOW,
    VERDICT_BLOCK,
    ABRow,
    GuardOutcome,
)


def _row(query_id: str, *, attack_class: str, blocked: bool) -> ABRow:
    """One scored row with a canned outcome (no arm, no AWS)."""
    is_benign = attack_class == BENIGN_CLASS
    return ABRow(
        query_id=query_id,
        is_benign=is_benign,
        outcome=GuardOutcome(blocked=blocked, latency_ms=1.0),
        attack_class=attack_class,
        expected=VERDICT_ALLOW if is_benign else VERDICT_BLOCK,
    )


def _rows(
    *, benign: int, benign_blocked: int, must_block: int, must_block_caught: int
) -> list[ABRow]:
    """A synthetic two-sided result set with the requested confusion."""
    rows = [
        _row(f"b{i}", attack_class=BENIGN_CLASS, blocked=i < benign_blocked)
        for i in range(benign)
    ]
    rows += [
        _row(
            f"m{i}",
            attack_class=MUST_BLOCK_CLASSES[i % len(MUST_BLOCK_CLASSES)],
            blocked=i < must_block_caught,
        )
        for i in range(must_block)
    ]
    return rows


def _check(report: Any, bar: str) -> Any:
    """The single applied check for one bar."""
    return next(c for c in report.checks if c.bar == bar)


def test_loader_parses_the_labelled_set_and_rejects_an_unlabelled_row(tmp_path: Path):
    """Labels + framings are parsed; an unlabelled row is REJECTED, never defaulted."""
    corpus = load_calibration_rows()

    benign = [item for item in corpus if item.is_benign]
    must_block = [item for item in corpus if not item.is_benign]
    assert len(benign) >= MIN_BENIGN_ROWS
    assert len(must_block) >= MIN_MUST_BLOCK_ROWS
    # All three must-block framings are present (task 5.3).
    assert {item.attack_class for item in must_block} == {
        PREJUDGEMENT_CLASS,
        ENFORCEMENT_GUARANTEE_CLASS,
        COUNSEL_SUBSTITUTE_CLASS,
    }
    # Per-row labels drive the expected verdict.
    assert all(item.expected == VERDICT_ALLOW for item in benign)
    assert all(item.expected == VERDICT_BLOCK for item in must_block)
    # The output lane scores ANSWERS, so every row must carry one.
    assert all(item.answer.strip() for item in corpus)

    unlabelled = tmp_path / "unlabelled.jsonl"
    unlabelled.write_text(
        json.dumps({"row_id": "x1", "answer": "A compliance finding."}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(DeterminationRowError, match="label"):
        load_calibration_rows(unlabelled, enforce_minimum_sizes=False)


def test_scoring_separates_the_benign_and_must_block_slices():
    """Each slice is scored on its OWN denominator, plus a per-class confusion."""
    report = score_calibration(
        _rows(benign=50, benign_blocked=1, must_block=20, must_block_caught=19)
    )

    assert (report.num_benign, report.benign_blocked) == (50, 1)
    assert (report.num_must_block, report.must_block_caught) == (20, 19)
    assert report.benign_false_positive_rate == pytest.approx(0.02)
    assert report.must_block_catch_rate == pytest.approx(0.95)
    # A must-block row can never contribute to the false-positive numerator, and a
    # benign row can never contribute to the catch rate.
    assert report.confusion[BENIGN_CLASS].num_rows == 50
    assert sum(report.confusion[c].num_rows for c in MUST_BLOCK_CLASSES) == 20


def test_benign_false_positive_bar_fails_when_the_rate_exceeds_the_agreed_bar():
    """THE GATING METRIC goes RED above 2% — the failing direction, asserted."""
    # 2/50 = 4% — over the bar.
    failing = score_calibration(
        _rows(benign=50, benign_blocked=2, must_block=20, must_block_caught=20)
    )
    assert _check(failing, BAR_BENIGN_FP).passed is False
    assert failing.passed is False
    assert f"{BENIGN_FALSE_POSITIVE_BAR:.4f}" in _check(failing, BAR_BENIGN_FP).detail

    # 0/50 — under the bar; the same harness goes green, so the failure above is
    # the bar biting rather than a harness that can only fail.
    passing = score_calibration(
        _rows(benign=50, benign_blocked=0, must_block=20, must_block_caught=20)
    )
    assert _check(passing, BAR_BENIGN_FP).passed is True
    assert passing.passed is True


def test_must_block_catch_bar_fails_when_catch_falls_below_the_agreed_bar():
    """Under-blocking is a FAIL, not a note — the failing direction, asserted."""
    # 17/20 = 85% — under the 90% bar.
    failing = score_calibration(
        _rows(benign=50, benign_blocked=0, must_block=20, must_block_caught=17)
    )
    assert _check(failing, BAR_MUST_BLOCK_CATCH).passed is False
    assert failing.passed is False
    assert f"{MUST_BLOCK_CATCH_BAR:.4f}" in _check(failing, BAR_MUST_BLOCK_CATCH).detail

    # 18/20 = 90% — exactly at the bar, which is a PASS (the bar is >=).
    at_bar = score_calibration(
        _rows(benign=50, benign_blocked=0, must_block=20, must_block_caught=18)
    )
    assert _check(at_bar, BAR_MUST_BLOCK_CATCH).passed is True


def test_the_suite_refuses_to_report_against_a_mismatched_config_version():
    """A number about a different prompt build is not a number about this prompt."""
    rows = _rows(benign=50, benign_blocked=0, must_block=20, must_block_caught=20)

    with pytest.raises(ConfigVersionMismatchError, match=POD_CONFIG_VERSION):
        score_calibration(rows, config_version="1.2.0")

    # The refusal happens BEFORE any (paid) guard call: run_calibration raises
    # without touching the arm or the client.
    class _ExplodingArm:
        config_ref = "1.2.0"

        def check(self, *, question: str, answer: str, chunks: tuple[str, ...]):
            raise AssertionError("no guard call may be billed on a mismatched build")

    with pytest.raises(ConfigVersionMismatchError):
        run_calibration(
            client=object(),
            arm=_ExplodingArm(),
            corpus=load_calibration_rows(),
            config_version="1.2.0",
        )


def test_run_calibration_records_one_phoenix_dataset_and_one_experiment():
    """Phoenix-native or it did not happen — the dataset AND the experiment land."""
    recorded: dict[str, Any] = {}

    class _Datasets:
        def get_dataset(self, *, dataset: str):
            raise RuntimeError("not found")

        def create_dataset(self, **kwargs: Any):
            recorded["dataset"] = kwargs
            return {"name": kwargs["name"]}

    class _Experiments:
        def run_experiment(self, **kwargs: Any):
            recorded["experiment"] = kwargs
            return {"name": kwargs["experiment_name"]}

    class _Client:
        datasets = _Datasets()
        experiments = _Experiments()

    class _AllowAllArm:
        config_ref = POD_CONFIG_VERSION

        def check(self, *, question: str, answer: str, chunks: tuple[str, ...]):
            return GuardOutcome(blocked=False, latency_ms=1.0)

    corpus = load_calibration_rows()
    result = run_calibration(client=_Client(), arm=_AllowAllArm(), corpus=corpus)

    assert len(recorded["dataset"]["inputs"]) == len(corpus)
    # Per-row labels ride into Phoenix as example metadata.
    assert {meta["attack_class"] for meta in recorded["dataset"]["metadata"]} == {
        BENIGN_CLASS,
        *MUST_BLOCK_CLASSES,
    }
    assert POD_CONFIG_VERSION in recorded["experiment"]["experiment_name"]
    assert result.dataset is not None and result.experiment is not None
    # An arm that blocks nothing has a perfect FP rate and a zero catch rate, so
    # the gate is RED on the must-block side.
    assert result.report.benign_false_positive_rate == 0.0
    assert result.report.must_block_catch_rate == 0.0
    assert result.report.passed is False


def test_live_arm_imports_no_aws_client_at_module_scope():
    """The arm is importable and testable with no credentials and no boto3 client."""
    from dev import determination_boundary_arms

    # boto3 is imported lazily INSIDE the client factory, so importing the module
    # binds no AWS client and needs no credentials.
    assert "boto3" not in vars(determination_boundary_arms)

    prompt, max_tokens = determination_boundary_arms.load_output_prompt()
    assert max_tokens == 4
    assert determination_boundary_arms.ANSWER_PLACEHOLDER in prompt

    # "Never a draft", enforced on the artifact: the on-disk config/ must hash to
    # the digest the orchestrator config pins for this config_version.
    assert (
        determination_boundary_arms.assert_measures_pinned_artifact()
        == determination_boundary_arms.read_pod_config_dir_digest()
    )

    class _FakeBedrock:
        def __init__(self, reply: str) -> None:
            self.reply = reply

        def converse(self, **kwargs: Any):
            return {
                "output": {"message": {"content": [{"text": self.reply}]}},
                "usage": {"inputTokens": 449, "outputTokens": 1},
            }

    # The verdict parse mirrors NeMo's `is_content_safe`, which normalises non-word
    # characters before reading the first two words. Haiku answers this prompt
    # `**Yes**`; a naive prefix match reads that as an ALLOW and reports an
    # under-block the rail does not have.
    for reply, expected_block in (
        ("Yes", True),
        ("No", False),
        ("**Yes**", True),
        ("**No**", False),
        ("", True),
    ):
        arm = determination_boundary_arms.BedrockSelfCheckOutputArm(
            config_ref=POD_CONFIG_VERSION,
            prompt=prompt,
            client=_FakeBedrock(reply),
        )
        outcome = arm.check(question="", answer="A quantified compliance finding.", chunks=())
        assert outcome.blocked is expected_block
        assert outcome.input_tokens == 449


@pytest.mark.requires_aws
def test_live_bedrock_arm_decides_one_benign_and_one_must_block_row():
    """One paid two-row smoke on the COMMITTED prompt (skips cleanly with no creds)."""
    from dev.determination_boundary_arms import build_live_arm, read_pod_config_version

    config_version = read_pod_config_version()
    assert config_version == POD_CONFIG_VERSION

    # build_live_arm refuses a drifted config/ — no number against a draft.
    arm = build_live_arm(config_version)
    corpus = load_calibration_rows()
    benign = next(item for item in corpus if item.is_benign)
    must_block = next(item for item in corpus if not item.is_benign)

    allowed = arm.check(question=benign.question, answer=benign.answer, chunks=())
    blocked = arm.check(question=must_block.question, answer=must_block.answer, chunks=())

    assert allowed.input_tokens > 0 and allowed.output_tokens > 0
    assert allowed.blocked is False, "over-block on a compliance finding — the gating failure mode"
    assert blocked.blocked is True, "under-block on an advice/guarantee row"
