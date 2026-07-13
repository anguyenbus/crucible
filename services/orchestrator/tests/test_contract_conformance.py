"""Contract conformance (the spec's named deliverable) plus strategic gap tests.

The conformance test validates the envelope's ``result`` VERBATIM against the
packaged eval rag_query_output v1.1.0 schema — NO key stripping anywhere
(strip-before-validate would neuter the schema's additionalProperties:false
drift detection). Phase 2: responses come from the live chain over MOCKED
clients (the designed evolution — the Phase 1 comment said this test flips
to ``live``). Gap tests cover citation internal consistency, the
config_sha256 provenance echo, envelope-level extension containment, and the
extensible (non-enum) guardrail vocabulary in OpenAPI.
"""

from app.main import app
from app.schemas.contract_validation import load_packaged_schema, validate_result

VALID_REQUEST = {
    "question": "Is the supply of legal services to a non-resident GST-free?",
    "query_id": "q-conformance-001",
    "pipeline_config": "legal-rag-default-1.1.0",
    "metadata": {"category": "gst"},
}


def _post_query(client) -> dict:
    response = client.post("/query", json=VALID_REQUEST)
    assert response.status_code == 200
    return response.json()


def test_query_result_validates_verbatim_against_packaged_schema(client):
    """The spec's named conformance test: result validates as-is, live marker present."""
    body = _post_query(client)

    # The one-line eval-adapter unwrap: response["result"] ALONE is the
    # schema-valid rag_query_output v1.1.0 payload — validated VERBATIM
    # (no key stripping anywhere) via the packaged-schema loader.
    validate_result(body["result"])

    # Phase 2 flipped the Phase 1 "stub" assertion, as designed: every
    # response comes from the real pipeline and is marked live.
    assert body["generation_mode"] == "live"


def test_citation_claim_span_and_chunk_ids_are_internally_consistent(client):
    """Citations cross-reference retrieved_chunks[] and index into answer.text."""
    result = _post_query(client)["result"]

    answer_text = result["answer"]["text"]
    citations = result["answer"]["citations"]
    assert citations, "the mocked-generation answer must carry at least one citation"

    retrieved_ids = {chunk["chunk_id"] for chunk in result["retrieved_chunks"]}
    for citation in citations:
        # claim_span is [start, end) into answer.text and must select real text.
        start, end = citation["claim_span"]
        assert 0 <= start < end <= len(answer_text)
        assert answer_text[start:end].strip()
        # Every cited chunk_id must exist in retrieved_chunks[].
        assert set(citation["chunk_ids"]) <= retrieved_ids

    # Contract-realistic id semantics per BYO index contract v2:
    # chunk_id == "{doc_id}:{chunk_idx}".
    for chunk in result["retrieved_chunks"]:
        doc_id, _, chunk_idx = chunk["chunk_id"].rpartition(":")
        assert doc_id == chunk["doc_id"]
        assert chunk_idx.isdigit()


def test_config_sha256_echo_equals_truncated_manifest_hash(client):
    """The response echoes the truncated manifest hash of the config that ran."""
    from app.config import CONFIG_SHA256_LENGTH, load_config_manifest

    system_version = _post_query(client)["result"]["system_version"]
    manifest = load_config_manifest()
    full_hash = manifest["legal-rag-default-1.1.0.yaml"]
    assert system_version["config_sha256"] == full_hash[:CONFIG_SHA256_LENGTH]


def test_orchestrator_extensions_live_only_at_envelope_top_level(client):
    """result carries no extensions beyond the open system_version echo slot."""
    body = _post_query(client)

    # The envelope top level is the ONLY home for orchestrator extensions.
    assert set(body.keys()) == {"result", "guardrail_decisions", "generation_mode"}

    schema = load_packaged_schema()
    # result's top level holds schema-declared keys only (trace and
    # timings_ms are DECLARED optional slots, not extensions).
    assert set(body["result"].keys()) <= set(schema["properties"].keys())

    # system_version is the one deliberately-open object; the extensions
    # riding there are the config_sha256 hash plus the Q8 provenance echo
    # (resolved index name + endpoint host).
    declared = set(schema["properties"]["system_version"]["properties"].keys())
    extras = set(body["result"]["system_version"].keys()) - declared
    assert extras == {"config_sha256", "opensearch_index", "opensearch_host"}


def test_openapi_documents_extensible_guardrail_decision_vocabulary():
    """OpenAPI carries GuardrailDecision fields with non-enum stage/decision."""
    openapi = app.openapi()
    decision_schema = openapi["components"]["schemas"]["GuardrailDecision"]

    assert set(decision_schema["properties"].keys()) >= {
        "stage",
        "decision",
        "rule_id",
        "category",
        "rationale",
    }
    for field in ("stage", "decision"):
        prop = decision_schema["properties"][field]
        # EXTENSIBLE open vocabulary: plain string, no closed enum/const, with
        # the documented value set in the description (oasdiff-gate friendly).
        assert prop["type"] == "string"
        assert "enum" not in prop
        assert "const" not in prop
        assert "EXTENSIBLE" in prop["description"]
