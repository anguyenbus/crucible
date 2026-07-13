"""Schema single-sourcing tests: byte-equality mirror + packaged loading.

Direction of truth: eval's ``rag_query_output.schema.json`` is CANONICAL; the
orchestrator's ``app/contracts/`` copy is a byte-for-byte mirror.
"""

import json
from pathlib import Path

import pytest

SERVICE_ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATOR_SCHEMA = SERVICE_ROOT / "app" / "contracts" / "rag_query_output.schema.json"
# Eval's canonical copy, resolved relative to this test file's repo location —
# deliberately NOT an import/package dependency on eval.
EVAL_CANONICAL_SCHEMA = (
    SERVICE_ROOT.parent / "eval" / "app" / "contracts" / "rag_query_output.schema.json"
)


def test_schema_mirror_is_byte_identical_to_evals_canonical_copy():
    """Byte-equality (NOT semantic-JSON) sync test against eval's canonical schema."""
    if not EVAL_CANONICAL_SCHEMA.is_file():
        pytest.fail(
            f"Eval's canonical schema was not found at {EVAL_CANONICAL_SCHEMA} - "
            "services/eval is not present as a sibling in this checkout, so the "
            "byte-equality sync test cannot run. Direction of truth is unchanged: "
            "eval's rag_query_output.schema.json is CANONICAL and the orchestrator "
            "mirror must be copied from it, never edited locally: "
            "cp services/eval/app/contracts/rag_query_output.schema.json "
            "services/orchestrator/app/contracts/"
        )
    assert ORCHESTRATOR_SCHEMA.read_bytes() == EVAL_CANONICAL_SCHEMA.read_bytes(), (
        "app/contracts/rag_query_output.schema.json has drifted from eval's "
        "CANONICAL copy at services/eval/app/contracts/rag_query_output.schema.json "
        "— copy from eval's, never edit locally: "
        "cp services/eval/app/contracts/rag_query_output.schema.json "
        "services/orchestrator/app/contracts/"
    )


def test_packaged_schema_loads_via_importlib_resources_with_no_cwd_dependence(
    monkeypatch, tmp_path
):
    """The loader resolves the packaged schema regardless of working directory."""
    monkeypatch.chdir(tmp_path)

    from app.schemas.contract_validation import load_packaged_schema

    schema = load_packaged_schema("rag_query_output")
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["properties"]["schema_version"]["const"] == "1.1.0"
    # Sanity: the loaded content matches the packaged file on disk.
    assert schema == json.loads(ORCHESTRATOR_SCHEMA.read_text(encoding="utf-8"))
