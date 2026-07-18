"""Demo-UI guard slice tests (output-guardrail Task Group 8).

Two additive changes over the prior slice:

1. Config bump: the default pinned config ref moves to
   ``legal-rag-default-1.4.0`` (the output PII/secrets guard-enabling config).
   eval's lane stays on ``legal-rag-default-1.1.0`` untouched.
2. Render note: a REDACT/FLAG output decision surfaces a concise, honest
   "N item(s) redacted / flagged" line in ``format_final_details`` — derived
   from ``guardrail_decisions`` (``transform``/``flag`` output decisions with
   per-rule counts). A canned refusal (zero citations, one ``block`` decision)
   still maps to the refusal text with NO source elements through the existing
   ``map_final_envelope``/``number_citations`` path; an envelope with empty
   ``guardrail_decisions`` renders byte-for-byte as before (no note).

The full ``guardrails_active`` decision BADGE stays a parked stub (out of scope).
Focused checks only (per task 8.1) — browser/E2E tests are skipped.
"""

from chat_ui import config
from chat_ui.render import format_final_details, map_final_envelope, number_citations

REFUSAL_TEXT = "I'm sorry, but I can't help with that request."

# The exact note fragments the render emits — asserted precisely so the tests
# do not trip on the mask token that legitimately appears in a redacted answer.
_REDACTED_NOTE = "item(s) redacted"
_FLAGGED_NOTE = "item(s) flagged"


def test_default_config_ref_is_1_4_0():
    """The UI queries with the output-guard-enabling config by default."""
    assert config.DEFAULT_PIPELINE_CONFIG == "legal-rag-default-1.4.0"
    assert config.pipeline_config_ref() == "legal-rag-default-1.4.0"


def _result_scaffold(*, answer_text, citations, chunks):
    """Shared 1.4.0-shaped ``result`` payload for the guard-slice envelopes."""
    return {
        "schema_version": "1.1.0",
        "system_version": {
            "pipeline_version": "1.4.0",
            "config_sha256": "0249750b5064" + "0" * 52,
            "generator_model": "au.anthropic.claude-sonnet-4-6",
            "embedder_model": "amazon.titan-embed-text-v2:0",
            "opensearch_index": "legal-rag-bench",
        },
        "query": {"query_id": "q-1", "text": "What is on file?"},
        "answer": {"text": answer_text, "citations": citations},
        "retrieved_chunks": chunks,
        "timings_ms": {"guardrail": 3.2, "total": 5.5},
    }


def _refusal_envelope():
    """A canned guard-block final envelope: refusal text, no citations/chunks."""
    return {
        "result": _result_scaffold(answer_text=REFUSAL_TEXT, citations=[], chunks=[]),
        "guardrail_decisions": [
            {
                "stage": "output",
                "decision": "block",
                "category": "secrets",
                "rule_id": "output-secrets-v1",
                "rationale": "aws_access_key_id",
            }
        ],
        "generation_mode": "live",
    }


def _redacted_envelope():
    """A REDACT final envelope: masked answer text + one ``transform`` decision."""
    answer = "The claimant's number is ‹redacted:ssn› per the record [c:1]."
    return {
        "result": _result_scaffold(
            answer_text=answer,
            citations=[{"chunk_ids": ["c:1"], "claim_span": [0, len(answer)]}],
            chunks=[
                {"rank": 1, "chunk_id": "c:1", "score": 0.9, "doc_id": "c", "text": "the record"}
            ],
        ),
        "guardrail_decisions": [
            {
                "stage": "output",
                "decision": "transform",
                "category": "pii",
                "rule_id": "output-pii-redact-v1",
                "rationale": "ssn=2, credit_card=1",
            }
        ],
        "generation_mode": "live",
    }


def _flagged_envelope():
    """A FLAG final envelope: answer UNCHANGED + one advisory ``flag`` decision."""
    answer = "Contact the office at clerk@example.com for the filing [c:1]."
    return {
        "result": _result_scaffold(
            answer_text=answer,
            citations=[{"chunk_ids": ["c:1"], "claim_span": [0, len(answer)]}],
            chunks=[
                {"rank": 1, "chunk_id": "c:1", "score": 0.9, "doc_id": "c", "text": "the filing"}
            ],
        ),
        "guardrail_decisions": [
            {
                "stage": "output",
                "decision": "flag",
                "category": "pii",
                "rule_id": "output-pii-flag-v1",
                "rationale": "email=1",
            }
        ],
        "generation_mode": "live",
    }


def _benign_envelope():
    """A plain allowed answer: citations, empty ``guardrail_decisions``."""
    answer = "The record is on file [c:1]."
    return {
        "result": _result_scaffold(
            answer_text=answer,
            citations=[{"chunk_ids": ["c:1"], "claim_span": [0, len(answer)]}],
            chunks=[
                {"rank": 1, "chunk_id": "c:1", "score": 0.9, "doc_id": "c", "text": "the record"}
            ],
        ),
        "guardrail_decisions": [],
        "generation_mode": "live",
    }


def test_refusal_envelope_renders_the_refusal_text_and_no_source_elements():
    render = map_final_envelope(_refusal_envelope(), phoenix_endpoint=None)

    assert render.answer_text == REFUSAL_TEXT
    assert render.has_citations is False
    assert render.sources == []
    # The decision BADGE is out of scope: guardrails_active stays a stub.
    assert render.guardrails_active is False

    numbered = number_citations(render)
    assert numbered.text == REFUSAL_TEXT  # zero citations → text unchanged
    assert numbered.sources == []  # no clickable source elements

    # A `block` decision is NOT a redaction/flag — it must not emit the note.
    details = format_final_details(render, numbered)
    assert _REDACTED_NOTE not in details
    assert _FLAGGED_NOTE not in details


def test_redacted_final_shows_masked_answer_and_redaction_note():
    render = map_final_envelope(_redacted_envelope(), phoenix_endpoint=None)

    # The mask token the orchestrator wrote is shown verbatim — never re-masked.
    assert "‹redacted:ssn›" in render.answer_text
    assert render.guardrails_active is False  # badge still a stub

    numbered = number_citations(render)  # [c:1] -> [1]
    details = format_final_details(render, numbered)
    # per-rule counts (ssn=2, credit_card=1) sum to the honest item total.
    assert f"3 {_REDACTED_NOTE}" in details
    assert _FLAGGED_NOTE not in details


def test_flagged_final_shows_flag_note_and_unchanged_answer():
    envelope = _flagged_envelope()
    original_answer = envelope["result"]["answer"]["text"]
    render = map_final_envelope(envelope, phoenix_endpoint=None)

    # A FLAG leaves the answer UNCHANGED (advisory only).
    assert render.answer_text == original_answer

    numbered = number_citations(render)
    details = format_final_details(render, numbered)
    assert f"1 {_FLAGGED_NOTE}" in details
    assert _REDACTED_NOTE not in details


def test_empty_guardrail_decisions_render_no_note():
    """No output decision ⇒ byte-for-byte the pre-guard render (no note)."""
    render = map_final_envelope(_benign_envelope(), phoenix_endpoint=None)

    numbered = number_citations(render)
    details = format_final_details(render, numbered)
    assert _REDACTED_NOTE not in details
    assert _FLAGGED_NOTE not in details
    assert "Output guardrail" not in details


# --------------------------------------------------------------------------- #
# NeMo out-of-process lane — DISTINCT attribution (category == "nemo").
#
# A NeMo advisory flag must NOT be folded into the generic "N item(s) flagged"
# count (that is the deterministic PII lane), and a NeMo block must render its
# OWN "blocked by the NeMo <rail>" line rather than looking like the shared
# secrets refusal — otherwise a reviewer cannot tell the NeMo lane acted.
# (NeMo Task Group 5.)
# --------------------------------------------------------------------------- #

_NEMO_NOTE = "NeMo guardrail"


def _nemo_flag_envelope():
    """A NeMo advisory-flag envelope: answer delivered + one nemo ``flag``."""
    answer = "Theft in Victoria requires dishonest appropriation [c:1]."
    return {
        "result": _result_scaffold(
            answer_text=answer,
            citations=[{"chunk_ids": ["c:1"], "claim_span": [0, len(answer)]}],
            chunks=[
                {"rank": 1, "chunk_id": "c:1", "score": 0.9, "doc_id": "c", "text": "the record"}
            ],
        ),
        "guardrail_decisions": [
            {
                "stage": "output",
                "decision": "flag",
                "category": "nemo",
                "rule_id": "nemo-output-flag-v1",
                "rationale": "NeMo pod unavailable on the output/facts path — failing open",
            }
        ],
        "generation_mode": "live",
    }


def _nemo_block_envelope(rationale="OutputRailException"):
    """A NeMo block envelope: answer suppressed to refusal text + a block (category nemo)."""
    return {
        "result": _result_scaffold(answer_text=REFUSAL_TEXT, citations=[], chunks=[]),
        "guardrail_decisions": [
            {
                "stage": "output",
                "decision": "block",
                "category": "nemo",
                "rule_id": "nemo-output-block-v1",
                "rationale": rationale,
            }
        ],
        "generation_mode": "live",
    }


def test_nemo_flag_renders_distinct_advisory_line_not_generic_flag_count():
    """A NeMo advisory flag surfaces a distinct NeMo line, never the PII flag count."""
    render = map_final_envelope(_nemo_flag_envelope(), phoenix_endpoint=None)
    numbered = number_citations(render)
    details = format_final_details(render, numbered)

    # Distinct, honest NeMo attribution naming the rail — NOT the generic count.
    assert _NEMO_NOTE in details
    assert "advisory flag" in details
    assert "policy/facts rail" in details
    # It is NOT lumped into the deterministic PII/secrets "N item(s) flagged" line.
    assert _FLAGGED_NOTE not in details
    assert _REDACTED_NOTE not in details


def test_nemo_block_renders_distinct_block_line_not_shared_refusal_note():
    """A NeMo policy block renders 'blocked by the NeMo policy rail', not the shared note."""
    render = map_final_envelope(_nemo_block_envelope(), phoenix_endpoint=None)
    # The answer itself is the shared refusal text (the router suppresses it)...
    assert render.answer_text == REFUSAL_TEXT

    numbered = number_citations(render)
    details = format_final_details(render, numbered)
    # ...but the details carry a DISTINCT NeMo block attribution so the reviewer
    # can tell the out-of-process NeMo lane acted (not the secrets/PII lane).
    assert _NEMO_NOTE in details
    assert "blocked by the NeMo policy rail" in details
    assert _FLAGGED_NOTE not in details
    assert _REDACTED_NOTE not in details


def test_nemo_facts_block_names_the_grounding_rail():
    """A grounding block's rationale (FactCheckRailException) names the facts rail."""
    render = map_final_envelope(
        _nemo_block_envelope(rationale="FactCheckRailException"), phoenix_endpoint=None
    )
    details = format_final_details(render, number_citations(render))
    assert "blocked by the NeMo facts (grounding) rail" in details


def test_pipeline_config_env_override_selects_1_8_0_nemo_all(monkeypatch):
    """DEMO_UI_PIPELINE_CONFIG selects the nemo-all 1.8.0 config for the grounding demo.

    The demo UI sends the config ref straight through (no allow-list), so the
    orchestrator-resolvable ``legal-rag-default-1.8.0`` is Chainlit-selectable via
    the env var — the launch path for the §6 grounding-block click-through.
    """
    monkeypatch.setenv("DEMO_UI_PIPELINE_CONFIG", "legal-rag-default-1.8.0")
    assert config.pipeline_config_ref() == "legal-rag-default-1.8.0"
