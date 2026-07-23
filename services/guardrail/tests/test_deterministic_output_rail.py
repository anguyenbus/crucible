"""
The pod's FIRST, deterministic (pure-regex, NO-LLM) SECRETS output rail — block
policy, short-circuit attribution, the `detections` verdict shape, and the
`/readyz` fail-fast on a non-compiling pattern. Fully offline (no AWS): the
detector is pure `re`, and the LLM rail is a recording double so we can PROVE the
paid `generate` is skipped on a block.

Scope note (item 6): the `pii:` table is WITHDRAWN, so this file no longer
carries PII block/flag cases. The withdrawal itself — Australian financial
identifiers delivered clean — plus the retained `blocks:` / `validator:`
extension seam are asserted in `test_pii_retirement.py`.

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


def test_a_clean_answer_does_not_short_circuit_the_llm_rails():
    """No deterministic hit → no block, no detections, and the LLM rail IS consulted."""
    rails = FakeRails(benign_res())
    verdict = nemo_runtime.check_output(
        rails,
        "Exhibit reference 1234 5678 1234 5678 is enclosed; call 0412 345 678.",
        chunks=["evidence"],
        model_id=TEST_MODEL_ID,
        detectors=_detectors(),
    )
    assert verdict.unsafe is False
    assert list(verdict.detections) == []
    # Not a block → the LLM rail WAS consulted (allow verdict rides through).
    assert len(rails.calls) == 1


def test_detections_shape_over_the_endpoint(make_client):
    """POST /check/output surfaces the typed detections (label + count) in the JSON body."""
    client = make_client(FakeRails(benign_res()))
    resp = client.post(
        "/check/output",
        json={
            "answer": (
                "Rotate AKIA0123456789ABCDEF and AKIAFEDCBA9876543210 immediately."
            ),
            "chunks": ["e"],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    # An honest 200 refusal, never a 5xx; the wire shape is unchanged.
    assert body["unsafe"] is True
    assert body["detections"] == [
        {"category": "secrets", "label": "aws_access_key", "count": 2}
    ]


def test_readyz_fails_fast_on_a_non_compiling_pattern(tmp_path, monkeypatch):
    """A non-compiling detector pattern → load raises, and /readyz reports 503 (never a silent no-op)."""
    # 1) The loader itself rejects a bad pattern (a broken detector must not no-op).
    bad = tmp_path / "detectors.yml"
    bad.write_text("secrets:\n  - {pattern: '[unclosed', label: broken}\n")
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
