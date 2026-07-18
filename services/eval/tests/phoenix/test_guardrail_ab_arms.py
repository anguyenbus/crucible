"""Unit tests for the live A/B arm builders + END-TO-END pre-pass (MOCKED).

Covers NeMo Task Group 6.1: ``load_legal_corpus`` loads the committed labelled
corpus; the pre-pass (:func:`capture_arm`) threads a REAL ``/query`` envelope
(``guardrail_decisions`` + ``timings_ms`` + token telemetry) into a
:class:`GuardOutcome`; the builders produce replay arms that REPLAY the captured
outcome (no crafted answers injected); and the whole pre-pass → ``run_shadow_ab``
flow records a Phoenix dataset + one experiment per arm on a MOCKED client.

Everything is hermetic: the orchestrator HTTP is a ``httpx.MockTransport`` (no live
pod / orchestrator / AWS) and the Phoenix ``client`` is a fake (no Phoenix server).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from app.phoenix.guardrail_ab import (
    IN_HOUSE_CONFIG_REF,
    NEMO_ALL_CONFIG_REF,
    run_shadow_ab,
)
from dev.guardrail_ab_arms import (
    build_in_house_arm,
    build_nemo_arm,
    capture_arm,
    corpus_with_captured_answers,
    load_legal_corpus,
    outcome_from_envelope,
)


def _envelope(*, answer: str, chunks: list[str], decisions: list[dict], timings: dict,
              telemetry: dict | None = None) -> dict[str, Any]:
    """Build a /query response envelope shaped like the orchestrator's 200 body."""
    envelope = {
        "result": {
            "answer": {"text": answer, "citations": []},
            "retrieved_chunks": [
                {"rank": i + 1, "chunk_id": f"c:{i}", "score": 0.9, "text": t}
                for i, t in enumerate(chunks)
            ],
            "timings_ms": timings,
            "system_version": {"pipeline_version": "x", "config_sha256": "0" * 64,
                               "opensearch_index": "legal-rag-bench"},
        },
        "guardrail_decisions": decisions,
        "generation_mode": "live",
    }
    if telemetry is not None:
        envelope["guard_telemetry"] = telemetry
    return envelope


# --------------------------------------------------------------------------- #
# 6.1(i): the committed labelled corpus loads with its class + verdict labels,
# at the Phase-2 gate's required size.
# --------------------------------------------------------------------------- #


def test_load_legal_corpus_meets_the_parity_gate_floors():
    """The committed corpus loads labelled, at the gate's per-class minimums."""
    corpus = load_legal_corpus()

    # The benign slice is the FP / over-refusal denominator: >=200 is what makes a
    # small FP regression detectable.
    benign = [c for c in corpus if c.attack_class == "benign"]
    assert len(benign) >= 200
    assert all(c.is_benign and c.expected == "allow" for c in benign)

    # >=20-30 per QUESTION-lane attack class. (The deterministic answer-lane
    # classes live in the pod's gate suite — see load_legal_corpus's docstring.)
    for attack_class in ("jailbreak_prompt_leak", "policy", "grounding"):
        rows = [c for c in corpus if c.attack_class == attack_class]
        assert len(rows) >= 20, f"{attack_class} has only {len(rows)} rows"
        assert all(not c.is_benign and c.expected == "block" for c in rows)

    # The corpus rows are QUESTIONS: answer/chunks are empty until the pre-pass.
    assert all(c.answer == "" and c.chunks == () for c in corpus)


# --------------------------------------------------------------------------- #
# 6.1(ii): the pre-pass threads a real envelope into a GuardOutcome.
# --------------------------------------------------------------------------- #


def test_outcome_from_envelope_captures_block_flag_latency_and_tokens():
    """A NeMo grounding-block envelope maps to a blocked outcome with summed latency."""
    env = _envelope(
        answer="an ungrounded claim",
        chunks=["grounding chunk"],
        decisions=[{"stage": "output", "decision": "block", "category": "nemo",
                    "rule_id": "nemo-output-block-v1", "rationale": "FactCheckRailException"}],
        timings={"generation": 900.0, "guardrail_nemo_output": 42.0},
        telemetry={"input_tokens": 170, "output_tokens": 12,
                   "model_id": "au.anthropic.claude-haiku"},
    )
    outcome = outcome_from_envelope(env)
    assert outcome.blocked is True
    assert outcome.flag is False
    assert outcome.latency_ms == 42.0  # only the guard lane, not generation
    assert (outcome.input_tokens, outcome.output_tokens) == (170, 12)
    assert outcome.model_id == "au.anthropic.claude-haiku"


def test_capture_arm_pre_pass_captures_real_answer_and_replays_no_crafted_answer():
    """capture_arm POSTs /query per question and the replay arm returns the captured outcome."""
    corpus = load_legal_corpus()[:3]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["pipeline_config"] == NEMO_ALL_CONFIG_REF
        # Block only the first question; the rest pass — REAL per-question answers.
        blocked = body["question"] == corpus[0].question
        decisions = (
            [{"stage": "output", "decision": "block", "category": "nemo",
              "rationale": "OutputRailException"}]
            if blocked else []
        )
        env = _envelope(
            answer=f"generated answer for {body['question'][:12]}",
            chunks=["retrieved chunk"],
            decisions=decisions,
            timings={"guardrail_nemo_output": 30.0},
        )
        return httpx.Response(200, json=env)

    captured = capture_arm(NEMO_ALL_CONFIG_REF, corpus, transport=httpx.MockTransport(handler))

    # The pre-pass captured a REAL generated answer + chunks per question.
    assert captured.answers[corpus[0].question].startswith("generated answer for")
    assert captured.chunks[corpus[1].question] == ("retrieved chunk",)

    # The replay arm returns the captured outcome (ignoring the passed answer) —
    # no crafted answer is ever injected into the A/B.
    arm = build_nemo_arm(NEMO_ALL_CONFIG_REF, captured)
    blocked = arm.check(question=corpus[0].question, answer="CRAFTED — ignored", chunks=())
    allowed = arm.check(question=corpus[1].question, answer="CRAFTED — ignored", chunks=())
    assert blocked.blocked is True
    assert allowed.blocked is False


# --------------------------------------------------------------------------- #
# 6.1(iii): the pre-pass feeds run_shadow_ab → Phoenix dataset + experiment/arm.
# --------------------------------------------------------------------------- #


class _FakeDatasets:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    def get_dataset(self, *, dataset: str) -> Any:
        raise KeyError(dataset)

    def create_dataset(self, **kwargs: Any) -> dict[str, Any]:
        self.created.append(kwargs)
        return {"dataset_id": "ds-1", "name": kwargs.get("name")}


class _FakeExperiments:
    def __init__(self) -> None:
        self.runs: list[dict[str, Any]] = []

    def run_experiment(self, **kwargs: Any) -> dict[str, Any]:
        self.runs.append(kwargs)
        return {"experiment_id": f"exp-{len(self.runs)}", "name": kwargs["experiment_name"]}


class _FakeClient:
    def __init__(self) -> None:
        self.datasets = _FakeDatasets()
        self.experiments = _FakeExperiments()


def test_end_to_end_pre_pass_feeds_run_shadow_ab_and_records_in_phoenix():
    """Both arms' pre-passes feed run_shadow_ab, recording a dataset + one exp/arm."""
    _all = load_legal_corpus()
    # A mixed slice: benign rows + at least one labelled should-block row.
    benign = [c for c in _all if c.is_benign][:3]
    attack = [c for c in _all if not c.is_benign][:1]
    corpus = benign + attack

    def make_handler(nemo_blocks_attack: bool):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            config = body["pipeline_config"]
            question = body["question"]
            is_benign = next(c.is_benign for c in corpus if c.question == question)
            # In-house arm never blocks; NeMo arm blocks a non-benign row only.
            blocked = config == NEMO_ALL_CONFIG_REF and nemo_blocks_attack and not is_benign
            decisions = (
                [{"stage": "output", "decision": "block", "category": "nemo",
                  "rationale": "OutputRailException"}]
                if blocked else []
            )
            latency = 40.0 if config == NEMO_ALL_CONFIG_REF else 5.0
            env = _envelope(
                answer=f"answer-{question[:8]}",
                chunks=["chunk"],
                decisions=decisions,
                timings={"guardrail_nemo_output": latency} if config == NEMO_ALL_CONFIG_REF
                else {"guardrail": latency},
                telemetry={"input_tokens": 150, "output_tokens": 10}
                if config == NEMO_ALL_CONFIG_REF else None,
            )
            return httpx.Response(200, json=env)
        return handler

    transport = httpx.MockTransport(make_handler(nemo_blocks_attack=True))
    captured_a = capture_arm(IN_HOUSE_CONFIG_REF, corpus, transport=transport)
    captured_b = capture_arm(NEMO_ALL_CONFIG_REF, corpus, transport=transport)

    arm_a = build_in_house_arm(IN_HOUSE_CONFIG_REF, captured_a)
    arm_b = build_nemo_arm(NEMO_ALL_CONFIG_REF, captured_b)
    dataset_corpus = corpus_with_captured_answers(corpus, captured_b)

    client = _FakeClient()
    result = run_shadow_ab(client=client, arm_a=arm_a, arm_b=arm_b, corpus=dataset_corpus)

    # One dataset + one experiment per arm recorded IN Phoenix.
    assert len(client.datasets.created) == 1
    assert len(client.experiments.runs) == 2
    # The dataset records the REAL captured answers (not crafted / not empty).
    assert all(o["answer"].startswith("answer-") for o in client.datasets.created[0]["outputs"])

    # In-house arm never blocks; NeMo arm blocks the labelled attack row(s) only —
    # a real end-to-end measurement, not a forced result.
    assert result.metrics_a.block_rate == 0.0
    assert result.metrics_b.block_rate > 0.0
    assert result.metrics_b.mean_total_tokens > 0.0  # NeMo bills per-answer tokens
    # NeMo blocks land on non-benign rows only → zero false positives on benign.
    assert result.metrics_b.false_positive_rate == 0.0
