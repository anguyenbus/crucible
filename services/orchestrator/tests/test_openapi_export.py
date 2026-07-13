"""
Tests for the committed OpenAPI contract (Task Group 6).

The exported document at ``services/orchestrator/openapi.json`` is the gated
wire contract: Tier 1 of the pre-commit gate re-exports it and fails on drift.
These tests are the same tripwire under plain pytest (no pre-commit needed).
"""

import json

from scripts.export_openapi import OPENAPI_PATH, render_openapi_document


def test_export_is_byte_identical_to_committed_openapi_json():
    """Re-exporting must reproduce the committed openapi.json byte-for-byte."""
    assert OPENAPI_PATH.is_file(), (
        "services/orchestrator/openapi.json is not committed - "
        "run `make orchestrator-openapi` from the repo root and commit the result"
    )
    assert OPENAPI_PATH.read_bytes() == render_openapi_document().encode("utf-8"), (
        "Committed openapi.json is stale (the app's API surface changed) - "
        "run `make orchestrator-openapi` and commit the re-exported file"
    )


def test_exported_document_contains_the_phase_1_endpoints():
    """The exported contract documents POST /query, GET /healthz, GET /readyz."""
    document = json.loads(render_openapi_document())
    assert "post" in document["paths"]["/query"]
    assert "get" in document["paths"]["/healthz"]
    assert "get" in document["paths"]["/readyz"]


def test_exported_document_pins_the_dependency_error_surface():
    """The contract documents 502/503 with the dependency field + Retry-After."""
    document = json.loads(render_openapi_document())

    query_responses = document["paths"]["/query"]["post"]["responses"]
    for status in ("502", "503"):
        ref = query_responses[status]["content"]["application/json"]["schema"]["$ref"]
        assert ref == "#/components/schemas/DependencyErrorResponse"
    assert "Retry-After" in query_responses["503"]["headers"]

    dependency_schema = document["components"]["schemas"]["DependencyErrorResponse"]
    assert dependency_schema["properties"]["dependency"]["enum"] == ["opensearch", "bedrock"]

    # readyz honestly states the reachability/credentials-not-invoke semantic.
    readyz_doc = document["paths"]["/readyz"]["get"]["description"]
    assert "NOT verify Bedrock invoke permission" in readyz_doc

    # generation_mode's enum still carries both values (oasdiff non-breakage).
    envelope = document["components"]["schemas"]["QueryResponse"]
    assert envelope["properties"]["generation_mode"]["enum"] == ["stub", "live"]


def test_exported_document_contains_no_python_only_named_groups():
    """OpenAPI patterns must be ECMA-262-safe: no Python-only (?P<...> groups."""
    assert "(?P<" not in render_openapi_document(), (
        "The exported OpenAPI document contains a Python-only (?P<...>) named "
        "group - OpenAPI 'pattern' fields must be ECMA-262 regexes; use plain "
        "capture groups instead."
    )
