"""
Item 6: the ``pii:`` detector table is RETIRED — ``secrets:`` is the whole table.

Three things are asserted here, and the third is the one that keeps the other two
honest:

1.  **The shape.** ``config/detectors.yml`` parses to a mapping whose ONLY
    top-level key is ``secrets:``, and a reintroduced table fails FAST rather
    than silently no-opping.
2.  **The withdrawal, on concrete values.** An answer carrying the Australian
    financial identifiers this ACCOUNTING product must legitimately SEE — an ABN,
    a TFN, a BSB + account number, a mobile number and an auditor's email — scans
    CLEAN and is delivered by ``/check/output`` with an EMPTY ``detections``
    array, while a credential still blocks deterministically INCLUDING on the
    Mode-A path (the paid Bedrock rail failing).
3.  **The retained machinery.** ``_CompiledRule.blocks``, the ``validator:``
    seam, ``_VALIDATORS`` and ``_luhn_ok`` survive the deletion as the documented
    extension seam for the Phase-3 numeric-provenance rail and for any
    compliance-approved PII return. They are compiled from a SYNTHETIC fixture
    table here, because retained-but-untested detector machinery is exactly how a
    detector silently no-ops.

Every value below is a test / publicly-published value — never real personal
data. The ABN ``51 824 753 556`` is the published ABN of a Commonwealth entity;
the TFN, BSB, account and mobile numbers are the standard fictitious test values.

Fully offline: pure regex + a recording LLM double. No AWS, no live pod.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from app import nemo_runtime
from app.detectors import DetectorConfigError, load_detectors
from tests.helpers import TEST_MODEL_ID, FakeRails, benign_res

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
_DETECTORS_YML = _CONFIG_DIR / "detectors.yml"

# An answer of exactly the kind this product exists to produce: an accountant's
# compliance finding quoting the client's own financial identifiers.
AU_IDENTIFIER_ANSWER = (
    "The entity trades under ABN 51 824 753 556 and the return quotes TFN "
    "123 456 782. Remittances were paid to BSB 062-000 account 12345678, and "
    "the engagement contact is auditor@example.com.au on 0412 345 678."
)

# A fake AWS access key id: 'AKIA' + 16 upper-alnum characters.
AWS_KEY = "AKIA0123456789ABCDEF"


class _RaisingRails:
    """A pod-UP rails whose ``generate`` ALWAYS fails — the Mode-A injection."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def generate(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        raise RuntimeError(
            "An error occurred (ThrottlingException) when calling the "
            "InvokeModel operation: Too many requests"
        )


def _detectors():
    """The shipping deterministic detector (config/detectors.yml), compiled."""
    return load_detectors(_CONFIG_DIR)


def _write_table(tmp_path: Path, body: str) -> Path:
    """Write a synthetic detectors.yml fixture table and return its directory."""
    (tmp_path / "detectors.yml").write_text(body, encoding="utf-8")
    return tmp_path


# --------------------------------------------------------------------------- #
# 1. The shape of the shipping table
# --------------------------------------------------------------------------- #


def test_detectors_yml_only_top_level_key_is_secrets():
    """The shipping table has ONE table: `secrets:`. `pii:` is gone entirely."""
    raw = yaml.safe_load(_DETECTORS_YML.read_text(encoding="utf-8"))

    assert isinstance(raw, dict)
    assert set(raw) == {"secrets"}
    assert len(raw["secrets"]) == 10
    # No shipping entry uses the retained seam, so every shipping rule blocks.
    assert all("blocks" not in entry and "validator" not in entry for entry in raw["secrets"])


def test_a_reintroduced_pii_table_fails_fast_instead_of_no_opping(tmp_path):
    """An unrecognised top-level table is a hard config error, never a silent skip."""
    config_dir = _write_table(
        tmp_path,
        "secrets:\n"
        "  - {pattern: 'AKIA[0-9A-Z]{16}', label: aws_access_key}\n"
        "pii:\n"
        "  - {pattern: '\\d{3}-\\d{2}-\\d{4}', label: ssn, severity: high}\n",
    )
    with pytest.raises(DetectorConfigError, match="unsupported top-level key"):
        load_detectors(config_dir)


# --------------------------------------------------------------------------- #
# 2. The withdrawal, on concrete values (and the credential no-regression)
# --------------------------------------------------------------------------- #


def test_australian_financial_identifiers_scan_clean_and_are_delivered(make_client):
    """ABN / TFN / BSB+account / mobile / email scan CLEAN and are delivered."""
    result = _detectors().scan(AU_IDENTIFIER_ANSWER)

    assert result.blocked is False
    assert result.detections == ()
    assert result.verdict == "clean"

    # End-to-end: /check/output allows it with an EMPTY detections array.
    client = make_client(FakeRails(benign_res()))
    response = client.post(
        "/check/output", json={"answer": AU_IDENTIFIER_ANSWER, "chunks": ["e"]}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["unsafe"] is False
    assert body["flag"] is False
    assert body["detections"] == []


def test_aws_access_key_still_blocks_including_on_the_mode_a_path(make_client):
    """A credential blocks deterministically — and keeps blocking under throttling."""
    answer = f"For access export AWS_ACCESS_KEY_ID={AWS_KEY} before running."

    result = _detectors().scan(answer)
    assert result.blocked is True
    assert result.rationale == "aws_access_key"

    # Mode A: the paid Bedrock rail is failing. The pure-regex tier must survive.
    rails = _RaisingRails()
    verdict = nemo_runtime.check_output(
        rails, answer, chunks=["e"], model_id=TEST_MODEL_ID, detectors=_detectors()
    )
    assert verdict.unsafe is True
    assert verdict.rationale == "aws_access_key"
    # NOT a fail-open, and the paid rail was never even reached (short-circuit).
    assert verdict.flag is False
    assert rails.calls == []


# --------------------------------------------------------------------------- #
# 3. The RETAINED extension seam, genuinely exercised
# --------------------------------------------------------------------------- #


def test_retained_seam_compiles_a_non_blocking_rule_and_yields_a_flag_verdict(tmp_path):
    """`blocks: false` compiles to an advisory rule — the only route to `flag`."""
    config_dir = _write_table(
        tmp_path,
        "secrets:\n"
        "  - {pattern: 'AKIA[0-9A-Z]{16}', label: aws_access_key}\n"
        "  - {pattern: 'ADVISORY-[0-9]{4}', label: advisory_marker, blocks: false}\n",
    )
    detectors = load_detectors(config_dir)

    advisory = detectors.scan("Ledger note ADVISORY-1234 was raised.")
    assert advisory.blocked is False
    assert advisory.rationale is None
    assert advisory.verdict == "flag"
    assert {d.label: d.count for d in advisory.detections} == {"advisory_marker": 1}

    # A blocking rule co-occurring with the advisory one still dominates, and the
    # advisory detection is still recorded on the short-circuited verdict.
    both = detectors.scan(f"ADVISORY-1234 and key {AWS_KEY}")
    assert both.blocked is True
    assert both.rationale == "aws_access_key"
    assert {d.label for d in both.detections} == {"aws_access_key", "advisory_marker"}


def test_retained_seam_compiles_a_validator_gated_rule_using_luhn(tmp_path):
    """`validator: luhn` resolves through `_VALIDATORS` and gates the match."""
    config_dir = _write_table(
        tmp_path,
        "secrets:\n"
        "  - pattern: '\\b(?:\\d{4}[- ]?){3}\\d{4}\\b'\n"
        "    label: luhn_gated\n"
        "    validator: luhn\n",
    )
    detectors = load_detectors(config_dir)

    # 4111 1111 1111 1111 is a Luhn-VALID test card -> the validator passes.
    passes = detectors.scan("value 4111 1111 1111 1111 present")
    assert passes.blocked is True
    assert passes.rationale == "luhn_gated"

    # 1234 5678 1234 5678 is 16 digits but Luhn-INVALID -> the validator rejects
    # it, so the rule does not fire at all.
    rejected = detectors.scan("Exhibit reference 1234 5678 1234 5678 is enclosed.")
    assert rejected.blocked is False
    assert rejected.detections == ()

    # An unknown validator name is a hard config error, never an ungated rule.
    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    _write_table(
        bad_dir,
        "secrets:\n  - {pattern: 'X{3}', label: nope, validator: not_a_validator}\n",
    )
    with pytest.raises(DetectorConfigError, match="unknown validator"):
        load_detectors(bad_dir)
