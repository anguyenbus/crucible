"""``POST /analyze`` — non-RAG facts extraction + summary (Bedrock mocked).

The route reuses the app.state client seam, so these run with the shared
``client`` fixture's ``MockBedrockClient`` and never touch AWS or OpenSearch
(the analyze path never calls search — proven here by a search client that
would raise if touched).
"""

from __future__ import annotations

import json

from app.clients.bedrock import GenerationResult
from botocore.exceptions import ClientError


def _json_generation(summary: str, facts: list[str]) -> GenerationResult:
    return GenerationResult(
        text=json.dumps({"summary": summary, "facts": facts}),
        model_id="au.anthropic.claude-sonnet-4-6",
        input_tokens=100,
        output_tokens=20,
        stop_reason="end_turn",
    )


def test_analyze_both_returns_parsed_summary_and_facts(client, mock_bedrock):
    mock_bedrock.generation_result = _json_generation(
        "A lease between Acme and Beta.", ["Term is 5 years.", "Rent is $1,000/month."]
    )

    response = client.post("/analyze", json={"text": "Some document text.", "mode": "both"})

    assert response.status_code == 200
    body = response.json()
    assert body["summary"] == "A lease between Acme and Beta."
    assert body["facts"] == ["Term is 5 years.", "Rent is $1,000/month."]
    assert body["model_id"] == "au.anthropic.claude-sonnet-4-6"
    assert body["truncated"] is False

    # Extraction is deterministic (temperature 0.0) and the doc text is in the prompt.
    call = mock_bedrock.generate_calls[0]
    assert call["temperature"] == 0.0
    assert "Some document text." in call["prompt"]


def test_analyze_defaults_to_both_mode(client, mock_bedrock):
    mock_bedrock.generation_result = _json_generation("s", ["f"])
    body = client.post("/analyze", json={"text": "t"}).json()
    assert body["summary"] == "s"
    assert body["facts"] == ["f"]


def test_analyze_facts_mode_returns_facts_only(client, mock_bedrock):
    # Even if the model echoes a summary, facts mode surfaces facts only.
    mock_bedrock.generation_result = _json_generation("ignored", ["a", "b"])

    body = client.post("/analyze", json={"text": "t", "mode": "facts"}).json()

    assert body["facts"] == ["a", "b"]
    assert body["summary"] == ""


def test_analyze_summary_mode_takes_prose_verbatim(client, mock_bedrock):
    mock_bedrock.generation_result = GenerationResult(
        text="  This lease runs five years at $1,000/month.  ",
        model_id="m",
        input_tokens=1,
        output_tokens=1,
        stop_reason="end_turn",
    )

    body = client.post("/analyze", json={"text": "t", "mode": "summary"}).json()

    assert body["summary"] == "This lease runs five years at $1,000/month."
    assert body["facts"] == []


def test_analyze_caps_facts_at_max_facts(client, mock_bedrock):
    mock_bedrock.generation_result = _json_generation(
        "s", [f"fact {i}" for i in range(10)]
    )

    body = client.post("/analyze", json={"text": "t", "max_facts": 3}).json()

    assert body["facts"] == ["fact 0", "fact 1", "fact 2"]


def test_analyze_truncates_long_text_and_reports_it(client, mock_bedrock):
    mock_bedrock.generation_result = _json_generation("s", [])
    long_text = "x" * 60_050  # over the 60_000-char analysis cap

    body = client.post("/analyze", json={"text": long_text}).json()

    assert body["truncated"] is True
    # The prompt carries exactly the 60_000-char slice, never the full 60_050.
    prompt = mock_bedrock.generate_calls[0]["prompt"]
    assert "x" * 60_000 in prompt
    assert "x" * 60_001 not in prompt


def test_analyze_non_json_reply_degrades_to_summary_no_facts(client, mock_bedrock):
    mock_bedrock.generation_result = GenerationResult(
        text="I could not produce JSON, but here is prose.",
        model_id="m",
        input_tokens=1,
        output_tokens=1,
        stop_reason="end_turn",
    )

    body = client.post("/analyze", json={"text": "t"}).json()

    assert body["summary"] == "I could not produce JSON, but here is prose."
    assert body["facts"] == []


def test_analyze_tolerates_prose_wrapped_json(client, mock_bedrock):
    mock_bedrock.generation_result = GenerationResult(
        text='Sure! ```json\n{"summary": "wrapped", "facts": ["f1"]}\n``` done',
        model_id="m",
        input_tokens=1,
        output_tokens=1,
        stop_reason="end_turn",
    )

    body = client.post("/analyze", json={"text": "t"}).json()

    assert body["summary"] == "wrapped"
    assert body["facts"] == ["f1"]


def test_analyze_empty_text_is_422(client):
    response = client.post("/analyze", json={"text": ""})
    assert response.status_code == 422


def test_analyze_never_touches_opensearch(client, mock_bedrock, mock_search):
    mock_bedrock.generation_result = _json_generation("s", [])
    client.post("/analyze", json={"text": "t"})
    assert mock_search.search_calls == []


def test_analyze_bedrock_client_error_maps_to_502(client, mock_bedrock):
    mock_bedrock.generate_error = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "InvokeModel"
    )

    response = client.post("/analyze", json={"text": "t"})

    assert response.status_code == 502
    assert response.json()["dependency"] == "bedrock"
