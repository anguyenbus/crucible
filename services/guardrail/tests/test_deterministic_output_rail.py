"""
Task Group 1: the pod's FIRST, deterministic (pure-regex, NO-LLM) secrets/PII
output rail — block/flag policy, Luhn gating, short-circuit attribution, the
`detections` verdict shape, and the `/readyz` fail-fast on a non-compiling
pattern. Fully offline (no AWS): the detector is pure `re`, and the LLM rail is a
recording double so we can PROVE the paid `generate` is skipped on a block.

The single live-Bedrock round-trip (input block + a secrets short-circuit end to
end through the pod) is the AWS-marked smoke in `test_bedrock_smoke.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import nemo_runtime
from app.detectors import DetectorConfigError, load_detectors
from app.settings import Settings  # noqa: F401 (used in the lifespan-driven readyz test)
from tests.helpers import TEST_MODEL_ID, FakeRails, benign_res

_CONFIG_DIR = str(Path(__file__).resolve().parent.parent / "config")


def _detectors():
    """The shipping deterministic detector (config/detectors.yml), compiled."""
    return load_detectors(_CONFIG_DIR)


def _dets(verdict):
    """Detections as a ``{label: count}`` map for terse assertions."""
    return {d.label: d.count for d in verdict.detections}


def test_secret_blocks_and_short_circuits_recording_attribution():
    """A secret → BLOCK, the paid LLM `generate` is NEVER called, verdict recorded."""
    # A rail that would ALLOW if consulted — if the block came from the LLM rail
    # this answer would be delivered. It must stay un-consulted (short-circuit).
    rails = FakeRails(benign_res())
    answer = "For access use AKIAIOSFODNN7EXAMPLE as the key."
    verdict = nemo_runtime.check_output(
        rails, answer, chunks=[], model_id=TEST_MODEL_ID, detectors=_detectors()
    )
    assert verdict.unsafe is True
    assert verdict.flag is False
    # Attribution is recorded EVEN THOUGH the LLM rails were skipped.
    assert _dets(verdict) == {"aws_access_key": 1}
    assert verdict.detections[0].category == "secrets"
    # The deterministic label is the rationale — NOT an LLM `OutputRailException`.
    assert verdict.rationale == "aws_access_key"
    # No paid LLM call was made (short-circuit) and no token telemetry.
    assert rails.calls == []
    assert verdict.input_tokens is None and verdict.output_tokens is None


def test_high_severity_pii_blocks_with_luhn_gate():
    """credit_card (valid Luhn) and ssn → BLOCK; the LLM rail is short-circuited."""
    rails = FakeRails(benign_res())
    # 4111 1111 1111 1111 is a Luhn-valid test card.
    cc = nemo_runtime.check_output(
        rails, "card 4111 1111 1111 1111", chunks=[], model_id=TEST_MODEL_ID, detectors=_detectors()
    )
    assert cc.unsafe is True and cc.rationale == "credit_card"
    assert _dets(cc) == {"credit_card": 1}
    assert rails.calls == []

    rails = FakeRails(benign_res())
    ssn = nemo_runtime.check_output(
        rails, "SSN 123-45-6789 on file", chunks=[], model_id=TEST_MODEL_ID, detectors=_detectors()
    )
    assert ssn.unsafe is True and ssn.rationale == "ssn"
    assert rails.calls == []


def test_luhn_gated_non_card_does_not_block():
    """A 16-digit run that FAILS Luhn (a reference number) does NOT trip credit_card."""
    rails = FakeRails(benign_res())
    # 1234 5678 1234 5678 is 16 digits but NOT Luhn-valid → no block, no detection.
    verdict = nemo_runtime.check_output(
        rails,
        "Exhibit reference 1234 5678 1234 5678 is enclosed.",
        chunks=[],
        model_id=TEST_MODEL_ID,
        detectors=_detectors(),
    )
    assert verdict.unsafe is False
    assert "credit_card" not in _dets(verdict)
    # Not a block → the LLM rail WAS consulted (allow verdict rides through).
    assert len(rails.calls) == 1


def test_low_severity_pii_flags_without_short_circuit():
    """email/phone → FLAG (not block): the LLM rails STILL run; detections carry counts."""
    rails = FakeRails(benign_res())
    answer = "Contact a@b.com or c@d.com, or call (555) 123-4567."
    verdict = nemo_runtime.check_output(
        rails, answer, chunks=["evidence"], model_id=TEST_MODEL_ID, detectors=_detectors()
    )
    # Low-sev PII does NOT block and does NOT short-circuit.
    assert verdict.unsafe is False
    # The LLM output rail WAS consulted (one generate call) — no short-circuit.
    assert len(rails.calls) == 1
    # `detections` shape: {category, label, count} — labels + counts, NO offsets.
    assert _dets(verdict) == {"email": 2, "phone": 1}
    by_label = {d.label: d for d in verdict.detections}
    assert by_label["email"].category == "pii" and by_label["email"].count == 2
    assert by_label["phone"].category == "pii" and by_label["phone"].count == 1
    assert not any(hasattr(d, "offset") or hasattr(d, "span") for d in verdict.detections)


def test_detections_shape_over_the_endpoint(make_client):
    """POST /check/output surfaces the typed detections (label + count) in the JSON body."""
    client = make_client(FakeRails(benign_res()))
    resp = client.post(
        "/check/output",
        json={"answer": "Reach me at a@b.com or c@d.com.", "chunks": ["e"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["unsafe"] is False
    assert body["detections"] == [{"category": "pii", "label": "email", "count": 2}]


def test_readyz_fails_fast_on_a_non_compiling_pattern(tmp_path, monkeypatch):
    """A non-compiling detector pattern → load raises, and /readyz reports 503 (never a silent no-op)."""
    # 1) The loader itself rejects a bad pattern (a broken detector must not no-op).
    bad = tmp_path / "detectors.yml"
    bad.write_text("secrets:\n  - {pattern: '[unclosed', label: broken}\npii: []\n")
    with pytest.raises(DetectorConfigError, match="failed to compile"):
        load_detectors(tmp_path)

    # 2) End-to-end: DRIVE lifespan (via `with`) with the detector-compile forced
    #    to fail — the rails engine builds fine but the detector slot stays None,
    #    so /readyz is 503 (fail-fast, never a silent no-op).
    from fastapi.testclient import TestClient

    from app.main import app
    from app.settings import Settings

    def _raise(_config_dir):
        raise DetectorConfigError("detector pattern for 'broken' failed to compile: bad")

    monkeypatch.setattr("app.main.load_detectors", _raise)
    monkeypatch.setattr("app.main.build_rails", lambda _config_dir: FakeRails(benign_res()))
    app.state.settings = Settings(model_id=TEST_MODEL_ID, region="ap-southeast-2", config_dir="config")
    try:
        with TestClient(app) as client:  # entering runs lifespan → compile fails
            resp = client.get("/readyz")
        assert resp.status_code == 503
        assert "failed to compile" in resp.json()["detail"]
    finally:
        for attr in ("settings", "rails", "detectors", "detectors_error"):
            if hasattr(app.state, attr):
                delattr(app.state, attr)
