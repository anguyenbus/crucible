"""History-aware stage tests (chainlit-chat-ui Task Group 7): rewrite + prompt.

Both stages stay PURE functions of typed inputs + config pins — deterministic
by decision, no paid call, no infra imports, no env reads. Focused checks only
(per task 7.1) — exhaustive budget-boundary permutations are intentionally
skipped.
"""

from __future__ import annotations

import pytest
from app.clients import AppClients
from app.config import resolve_pipeline_config
from app.main import app as main_app
from app.orchestrator.prompt_builder import build_prompt
from app.orchestrator.query_rewrite import rewrite
from app.schemas.pipeline_config import HistoryPins
from app.schemas.query import HistoryTurn
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from tests.conftest import FIXTURE_SETTINGS

PINS = HistoryPins(
    rewrite_window_user_turns=3,
    rewrite_char_budget=1_500,
    prompt_window_turns=6,
    prompt_char_budget=6_000,
)


def _turn(role: str, text: str) -> HistoryTurn:
    return HistoryTurn(role=role, text=text)


def test_rewrite_windows_recent_user_turns_newest_first_rendered_oldest_first():
    """Only USER turns are selected (newest-first), rendered oldest-first as a prefix."""
    history = [
        _turn("user", "u1 oldest"),
        _turn("assistant", "a1 never selected"),
        _turn("user", "u2"),
        _turn("assistant", "a2 never selected"),
        _turn("user", "u3"),
        _turn("user", "u4 newest"),
    ]

    rewritten = rewrite("current question?", history, PINS)

    # Window of 3 user turns: u4/u3/u2 selected newest-first, u1 falls out;
    # rendered oldest-first with the current question last.
    assert rewritten == "u2\nu3\nu4 newest\ncurrent question?"
    assert "a1" not in rewritten
    assert "u1" not in rewritten


def test_rewrite_char_budget_drops_whole_turns_oldest_first():
    """A turn that would overflow the budget is dropped WHOLE, with all older turns."""
    history = [
        _turn("user", "o" * 30),  # oldest: would overflow, dropped whole
        _turn("user", "m" * 10),
        _turn("user", "n" * 10),  # newest
    ]
    pins = HistoryPins(
        rewrite_window_user_turns=3,
        rewrite_char_budget=25,
        prompt_window_turns=6,
        prompt_char_budget=6_000,
    )

    rewritten = rewrite("q?", history, pins)

    assert rewritten == "m" * 10 + "\n" + "n" * 10 + "\nq?"

    # A single newest turn over the whole budget → nothing fits → identity.
    oversized = [_turn("user", "x" * 26)]
    assert rewrite("q?", oversized, pins) == "q?"


def test_rewrite_with_empty_or_absent_history_is_identity():
    """Empty/absent history → identity on the question (single-turn parity)."""
    question = "What is the applicable rate?"
    assert rewrite(question) is question
    assert rewrite(question, None, PINS) is question
    assert rewrite(question, [], PINS) is question


def test_prompt_history_block_renders_chronological_lines_above_context():
    """User:/Assistant: lines, chronological, in a delimited section ABOVE context."""
    template = resolve_pipeline_config("legal-rag-default-1.2.0").config.prompt_template.text
    history = [
        _turn("user", "first question"),
        _turn("assistant", "first answer [gst-act-1999:1]"),
        _turn("user", "second question"),
    ]

    prompt = build_prompt(
        template,
        context="[d:0]: some legal text",
        question="third question?",
        history=history,
        pins=PINS,
    )

    block = (
        "Conversation so far:\n"
        "User: first question\n"
        "Assistant: first answer [gst-act-1999:1]\n"
        "User: second question\n"
        "\n"
        "Context:"
    )
    assert block in prompt
    # The delimited history section sits ABOVE the context and question.
    assert prompt.index("Conversation so far:") < prompt.index("Context:")
    assert prompt.index("Context:") < prompt.index("Question: third question?")


def test_prompt_history_char_budget_drops_whole_turns_oldest_first():
    """Whole-turn tail truncation: oldest turns fall out first under the budget."""
    template = resolve_pipeline_config("legal-rag-default-1.2.0").config.prompt_template.text
    history = [
        _turn("user", "oldest " + "o" * 100),
        _turn("assistant", "middle answer"),
        _turn("user", "newest question"),
    ]
    # Budget fits the two newest rendered lines but not the oldest turn.
    pins = HistoryPins(
        rewrite_window_user_turns=3,
        rewrite_char_budget=1_500,
        prompt_window_turns=6,
        prompt_char_budget=len("Assistant: middle answer") + len("User: newest question"),
    )

    prompt = build_prompt(
        template,
        context="ctx",
        question="q?",
        history=history,
        pins=pins,
    )

    assert "Assistant: middle answer\nUser: newest question\n\nContext:" in prompt
    assert "oldest" not in prompt


def test_prompt_without_history_is_equivalent_to_the_1_1_0_prompt():
    """Empty history renders NO block; the 1.2.0 prompt equals 1.1.0's prompt."""
    template_110 = resolve_pipeline_config("legal-rag-default-1.1.0").config.prompt_template.text
    template_120 = resolve_pipeline_config("legal-rag-default-1.2.0").config.prompt_template.text
    context = "[d:0]: some legal text"
    question = "When is a supply GST-free?"

    prompt_110 = build_prompt(template_110, context=context, question=question)
    for history in (None, []):
        prompt_120 = build_prompt(
            template_120, context=context, question=question, history=history, pins=PINS
        )
        assert "Conversation so far:" not in prompt_120
        # Byte-equality here is stronger than the required semantic equivalence.
        assert prompt_120 == prompt_110


def test_replace_rendering_keeps_literal_braces_in_user_text():
    """Literal braces in history/context/question survive {history} substitution."""
    template = resolve_pipeline_config("legal-rag-default-1.2.0").config.prompt_template.text
    history = [_turn("user", "what does {the trust deed} say?")]

    prompt = build_prompt(
        template,
        context="[d:0]: clause {7.2} applies",
        question="and {schedule 3}?",
        history=history,
        pins=PINS,
    )

    assert "User: what does {the trust deed} say?" in prompt
    assert "clause {7.2} applies" in prompt
    assert "Question: and {schedule 3}?" in prompt
    # The template's literal citation-instruction braces survive too.
    assert "[doc_id:chunk_idx]" in prompt
    assert "{history}" not in prompt
    assert "{context}" not in prompt
    assert "{question}" not in prompt


def test_reserved_placeholder_tokens_in_content_are_not_re_substituted():
    """A RESERVED token ({context}/{question}) inside history/chunk text survives.

    Single-pass substitution must not re-scan inserted values: a prior turn (or
    a retrieved chunk) that literally contains ``{context}``/``{question}`` must
    NOT have the real context/question spliced into it.
    """
    template = "{history}Context:\n{context}\n\nQuestion: {question}"
    history = [_turn("user", "earlier I quoted {context} and {question} verbatim")]

    prompt = build_prompt(
        template,
        context="RETRIEVED_CONTEXT",
        question="the current question",
        history=history,
        pins=PINS,
    )

    # The reserved tokens inside the history line stay literal — not replaced.
    assert "earlier I quoted {context} and {question} verbatim" in prompt
    # The real placeholders (in the template) WERE substituted exactly once.
    assert "Context:\nRETRIEVED_CONTEXT" in prompt
    assert "Question: the current question" in prompt
    # The retrieved context appears exactly once (not also spliced into history).
    assert prompt.count("RETRIEVED_CONTEXT") == 1


@pytest.fixture
def traced_client(mock_bedrock, mock_search):
    """TestClient with mocked clients AND a real tracer over an in-memory exporter."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    main_app.state.settings = FIXTURE_SETTINGS
    main_app.state.clients = AppClients(bedrock=mock_bedrock, search=mock_search)
    main_app.state.tracer = provider.get_tracer("test-tracer")
    try:
        yield TestClient(main_app), exporter
    finally:
        del main_app.state.settings
        del main_app.state.clients
        del main_app.state.tracer


def test_retrieval_span_input_value_shows_the_rewritten_query(traced_client, mock_bedrock):
    """With history, the retrieval span's INPUT_VALUE is the REWRITTEN query."""
    client, exporter = traced_client
    request = {
        "question": "Does it also apply to digital services?",
        "pipeline_config": "legal-rag-default-1.2.0",
        "history": [
            {"role": "user", "text": "Is a supply to a non-resident GST-free?"},
            {"role": "assistant", "text": "Yes, under section 38-190 [gst-act-1999:0]."},
        ],
    }

    response = client.post("/query", json=request)
    assert response.status_code == 200

    rewritten = "Is a supply to a non-resident GST-free?\nDoes it also apply to digital services?"
    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert spans["retrieval"].attributes["input.value"] == rewritten
    # The rewritten query is what was embedded and retrieved against...
    assert mock_bedrock.embed_calls[-1]["text"] == rewritten
    # ...while the prompt carries the history BLOCK plus the CURRENT question
    # (never the history-prefixed retrieval query), and the result echoes the
    # current question verbatim.
    prompt = mock_bedrock.generate_calls[-1]["prompt"]
    assert "Conversation so far:\nUser: Is a supply to a non-resident GST-free?" in prompt
    assert "Question: Does it also apply to digital services?" in prompt
    assert response.json()["result"]["query"]["text"] == request["question"]
