"""
Phase-2 GATE (pod side): deterministic verdict parity + Mode-A failure injection.

This is half of the gate that authorizes cutover to the nemo-all ``1.8.0`` config
(the other half — Mode B, the transport policy — lives in the orchestrator's pure
stage, so it is asserted there). Everything here is DETERMINISTIC and runs in CI:
the deterministic rail is pure regex, and Mode A is a MOCKED Bedrock failure, so
no AWS is touched.

**Deterministic verdict parity** is asserted END-TO-END THROUGH THE POD: every row
of the labelled answer-lane parity set is driven through the REAL ``POST
/check/output`` route, the REAL ``config/detectors.yml``, the REAL rail ordering
and the REAL ``CheckResponse`` serialization, and the resulting HTTP verdict is
compared against the row's label.

It is deliberately NOT asserted as "the regex matched the same string as the
orchestrator's regex". The patterns were ported VERBATIM, so a regex-vs-regex
comparison is tautological — it would pass even if the rail were never registered,
the verdict never serialized, or the short-circuit ordered after the paid rails.
Those (wiring / serialization / short-circuit ordering) are the bugs that can
actually exist here, and only a full-pipeline verdict catches them.

**Mode A** (pod UP, the Bedrock/LLM rail fails — throttle / malformed / the
stop-seq class we actually lived) is the core resilience payoff:

  * a secrets answer STILL BLOCKS — the deterministic rail is pure regex with no
    model, so it cannot be taken down by a Bedrock outage, and it runs FIRST; and
  * ``self_check_output`` / ``self_check_facts`` fail OPEN + flag — a flaky paid
    rail must never nuke a valid legal answer.

Note what Mode A proves about ordering: on a blocking row ``generate`` is never
called AT ALL, so the block cannot be an artifact of the LLM rail's failure — it
is the deterministic rail short-circuiting ahead of it.

**The parity CONTRACT is ``secrets:`` only** (item 6): ``pii_high`` / ``pii_low``
were withdrawn along with the pod's ``pii:`` table. Their rows are retained in
the fixture and still driven through the pod — not as a parity score, but as the
withdrawal regression, proving those answers are now delivered clean rather than
blocked or flagged.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.helpers import FakeRes

# The labelled ANSWER-lane parity set (deterministic classes). The QUESTION-lane
# slice — benign + jailbreak/prompt-leak + policy + grounding — is scored by the
# Phoenix A/B harness instead, because those verdicts need a live Bedrock rail.
PARITY_SET = Path(__file__).parent / "fixtures" / "parity" / "detector_answers.jsonl"

# CONTRACT class -> the verdict every row in it must receive end-to-end.
EXPECTED_BY_CLASS = {
    "secrets": "block",
    "benign": "allow",
}

# Classes WITHDRAWN from the contract with the `pii:` table. Their fixture rows
# are kept and scored as the withdrawal REGRESSION (every one must now deliver
# clean), never as a parity number.
WITHDRAWN_CLASSES = ("pii_high", "pii_low")


# Secret-shaped samples are ASSEMBLED HERE at runtime from fragments, so neither
# the committed fixture nor this file contains a contiguous, scanner-matching
# literal (GitHub push-protection would otherwise block the repo). Each assembled
# value still matches the pod's detector regex (config/detectors.yml); the exact
# bytes are irrelevant — the test only asserts a secrets answer BLOCKS. The fixture
# stores ``{{SECRET:<kind>}}`` placeholders that ``_inject_secrets`` swaps in before
# the answer reaches the detector.
def _s(*parts: str) -> str:
    return "".join(parts)


_SECRET_SAMPLES: dict[str, str] = {
    "anthropic": _s("sk-", "ant-", "A" * 44),
    "aws": _s("AKIA", "A" * 16),
    "openai": _s("sk-", "proj-", "A" * 44),
    "google": _s("AIza", "A" * 35),
    "github": _s("ghp", "_", "A" * 36),
    "slack": _s("xox", "b-", "1" * 13, "-", "2" * 13, "-", "A" * 24),
    "hf": _s("hf", "_", "A" * 34),
    "privatekey": _s("-----BEGIN ", "RSA ", "PRIVATE KEY-----"),
    "jwt": _s("ey", "Jx", ".", "ey", "Jy", ".", "sig"),
    "conn": _s("postgres", "://u:", "p@h:5432/d"),
}


def _inject_secrets(answer: str) -> str:
    """Swap ``{{SECRET:<kind>}}`` placeholders for their runtime-assembled tokens."""
    for kind, sample in _SECRET_SAMPLES.items():
        answer = answer.replace("{{SECRET:" + kind + "}}", sample)
    return answer


def load_parity_rows() -> list[dict[str, Any]]:
    """Load the labelled answer-lane parity set (placeholders → runtime secrets)."""
    rows = [
        json.loads(line)
        for line in PARITY_SET.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    for row in rows:
        row["answer"] = _inject_secrets(row["answer"])
    return rows


ALL_ROWS = load_parity_rows()
PARITY_ROWS = [r for r in ALL_ROWS if r["attack_class"] in EXPECTED_BY_CLASS]
WITHDRAWN_ROWS = [r for r in ALL_ROWS if r["attack_class"] in WITHDRAWN_CLASSES]


class RaisingRails:
    """
    A pod-UP rails whose ``generate`` ALWAYS fails — the Mode-A injection.

    Models the real failure class: Bedrock throttling / a malformed response
    surfaces to ``nemo_runtime`` as a raise from ``generate``. ``calls`` records
    every invocation so a test can prove the paid rail was never even reached.
    """

    def __init__(self, error: Exception) -> None:
        self._error = error
        self.calls: list[dict[str, Any]] = []

    def generate(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        raise self._error


class CleanRails:
    """A rails whose ``generate`` returns a clean (non-blocking) result."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def generate(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return FakeRes(response=[{"role": "assistant", "content": "ok"}])


def _throttle() -> Exception:
    """A Bedrock throttling error shaped like the real botocore ClientError."""
    return RuntimeError(
        "An error occurred (ThrottlingException) when calling the "
        "InvokeModel operation: Too many requests"
    )


def _verdict_of(body: dict[str, Any]) -> str:
    """
    Map a ``/check/output`` response body to the END-TO-END verdict vocabulary.

    ``unsafe`` dominates: a blocked answer is never delivered. Otherwise the
    answer IS delivered, and a deterministic detection makes it a ``flag``.
    """
    if body["unsafe"]:
        return "block"
    if body["detections"]:
        return "flag"
    return "allow"


def _post(client: Any, answer: str) -> dict[str, Any]:
    """POST one answer through the REAL /check/output route."""
    response = client.post(
        "/check/output", json={"answer": answer, "chunks": ["c"], "check_facts": True}
    )
    # An honest 200 verdict, never a 5xx — a guard block is a refusal, not an error.
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize(
    "row", PARITY_ROWS, ids=[row["query_id"] for row in PARITY_ROWS]
)
def test_deterministic_verdict_parity_end_to_end_through_the_pod(make_client, row):
    """Every labelled parity row's END-TO-END pod verdict matches its label."""
    rails = CleanRails()
    client = make_client(rails)

    body = _post(client, row["answer"])

    assert _verdict_of(body) == row["expected"] == EXPECTED_BY_CLASS[row["attack_class"]]
    # Attribution is ALWAYS recorded for a firing rule — including on a block that
    # short-circuited the paid rails.
    if row["expected"] in {"block", "flag"}:
        assert body["detections"], "a firing rule must record its attribution"
    else:
        assert body["detections"] == []


def test_parity_set_covers_every_contract_class_at_the_required_size():
    """The scored parity set meets the gate's per-class minimum (>=20-30 per class)."""
    counts: dict[str, int] = {}
    for row in PARITY_ROWS:
        counts[row["attack_class"]] = counts.get(row["attack_class"], 0) + 1

    assert set(counts) == set(EXPECTED_BY_CLASS)
    for attack_class, count in counts.items():
        assert count >= 20, f"{attack_class} has only {count} rows (gate floor is 20)"


@pytest.mark.parametrize(
    "row", WITHDRAWN_ROWS, ids=[row["query_id"] for row in WITHDRAWN_ROWS]
)
def test_withdrawn_pii_rows_are_now_delivered_clean_through_the_pod(make_client, row):
    """Item-6 regression: every withdrawn PII row now ALLOWS with no detections."""
    client = make_client(CleanRails())

    body = _post(client, row["answer"])

    assert _verdict_of(body) == "allow"
    assert body["detections"] == []


@pytest.mark.parametrize("attack_class", ["secrets"])
def test_mode_a_deterministic_rail_still_blocks_when_the_bedrock_rail_fails(
    make_client, attack_class
):
    """
    Mode A: the deterministic rail STILL BLOCKS every secrets row when the paid
    Bedrock rail is failing — pure regex, no model, ordered FIRST.
    """
    rails = RaisingRails(_throttle())
    client = make_client(rails)

    rows = [r for r in PARITY_ROWS if r["attack_class"] == attack_class]
    for row in rows:
        body = _post(client, row["answer"])

        assert body["unsafe"] is True, f"{row['query_id']} must block under Mode A"
        assert body["detections"], "the deterministic verdict is recorded even here"
        # NOT a fail-open: this is a real deterministic verdict.
        assert body["flag"] is False

    # The paid rail was never reached on ANY row: the block is the deterministic
    # rail short-circuiting FIRST, not a side effect of the LLM rail's failure.
    assert rails.calls == []


def test_mode_a_policy_and_facts_fail_open_with_a_flag_when_the_bedrock_rail_fails(
    make_client,
):
    """Mode A: a clean answer + a failing paid rail ⇒ fail OPEN + flag, never a block."""
    rails = RaisingRails(_throttle())
    client = make_client(rails)

    body = _post(client, "Theft requires an intention to permanently deprive.")

    assert body["unsafe"] is False, "a flaky guard must never nuke a valid answer"
    assert body["flag"] is True, "the fail-open window must be advertised"
    assert body["detections"] == []
    # The paid rail WAS attempted (nothing short-circuited it) and it failed.
    assert len(rails.calls) == 1


def test_mode_a_contact_details_are_delivered_unflagged_on_the_fail_open_path(
    make_client,
):
    """
    Mode A + item 6: an answer carrying contact details no longer trips ANY
    deterministic rule, so it does not short-circuit — it reaches the failing paid
    rail and is delivered with the advisory fail-open flag and NO detections.
    """
    rails = RaisingRails(_throttle())
    client = make_client(rails)

    body = _post(client, "Call the registry on 0412 345 678 or email clerk@example.gov.au.")

    assert body["unsafe"] is False, "contact details must never block"
    # The only flag here is the fail-open advisory, not a deterministic detection.
    assert body["flag"] is True
    assert body["detections"] == []
    # It rode the same pass as the LLM rails rather than short-circuiting them.
    assert len(rails.calls) == 1
