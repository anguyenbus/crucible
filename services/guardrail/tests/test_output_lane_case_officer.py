"""
Group 5 (pod side): the ratified case-officer OUTPUT lane — PII VISIBLE + verdict-only.

Two invariants of the ratified ``self check output`` lane, asserted at the pod
boundary with the real deterministic detector and a recording LLM double (no AWS):

- **5.2 PII-allow carve-out.** A realistic case-officer answer carrying an ABN +
  a BSB + account + a deposit FIGURE runs the deterministic secrets rail FIRST
  (clean — the ``pii:`` table is WITHDRAWN, item 6) and then ``self check output``
  ALLOWS it: the whole lane DELIVERS the answer with ``unsafe=False``,
  ``flag=False`` and an EMPTY ``detections`` array. The cleared officer sees every
  identifier and figure — the output rail never blocks or redacts them. (The
  live-Bedrock 11/11 measurement is the AWS-marked smoke; this pins the wiring +
  the deterministic carve-out offline.)

- **5.5 verdict-only.** On a genuine block the rationale is the exception TYPE
  (``_extract_block`` reads ``role == "exception"``), never NeMo's own refusal
  prose — the pod is verdict-only and never rewrites the answer.

Fully offline: pure-regex detector + a ``FakeRails`` double.
"""

from __future__ import annotations

from pathlib import Path

from app import nemo_runtime
from app.detectors import load_detectors
from tests.helpers import TEST_MODEL_ID, FakeRails, benign_res, block_res

_CONFIG_DIR = str(Path(__file__).resolve().parent.parent / "config")

# A case officer's compliance finding quoting the subject's own financial
# identifiers AND a deposit figure. Standard fictitious / published test values.
CASE_OFFICER_ANSWER = (
    "The subject entity trades under ABN 51 824 753 556. Remittances were paid "
    "to BSB 062-000 account 12345678, and a deposit of $4,250,000.00 was "
    "identified that is not reflected in the declared income for the period."
)

# The pod-supplied prose that must NEVER become the rationale (verdict-only).
NEMO_LEAKED_PROSE = "I am blocking this because it disclosed the taxpayer's PII."


def _detectors():
    """The shipping deterministic detector (config/detectors.yml), compiled."""
    return load_detectors(_CONFIG_DIR)


def test_case_officer_pii_answer_scans_clean_and_self_check_output_allows():
    """5.2: ABN + BSB + account + deposit figure — deterministic CLEAN then LLM ALLOW."""
    # Deterministic rail FIRST: the withdrawn PII table lets the identifiers pass.
    scan = _detectors().scan(CASE_OFFICER_ANSWER)
    assert scan.blocked is False
    assert scan.detections == ()

    # The full lane: self check output ALLOWS (a rail that would block if
    # consulted is NOT — benign_res → no exception turn), and the answer is
    # delivered with no block, no flag, no detections. PII stays VISIBLE.
    rails = FakeRails(benign_res())
    verdict = nemo_runtime.check_output(
        rails, CASE_OFFICER_ANSWER, chunks=["evidence"], model_id=TEST_MODEL_ID,
        detectors=_detectors(),
    )
    assert verdict.unsafe is False
    assert verdict.flag is False
    assert list(verdict.detections) == []
    # The paid LLM rail DID run (not short-circuited) — the carve-out is the rail
    # allowing PII, not the detector blocking then delivering.
    assert len(rails.calls) == 1


def test_output_block_rationale_is_the_exception_type_never_nemo_prose():
    """5.5: a block's rationale is the exception TYPE — NeMo's own prose never surfaces."""
    rails = FakeRails(block_res(exc_type="OutputRailException"))
    verdict = nemo_runtime.check_output(
        rails, "some answer", chunks=["e"], model_id=TEST_MODEL_ID, detectors=_detectors()
    )
    assert verdict.unsafe is True
    # Verdict-only: the terse exception type, not a rewritten answer or LLM prose.
    assert verdict.rationale == "OutputRailException"
    assert NEMO_LEAKED_PROSE not in (verdict.rationale or "")
