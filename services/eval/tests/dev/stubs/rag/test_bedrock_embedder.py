"""Tests for the Bedrock Titan embedder (mocked bedrock-runtime; no network)."""

import io
import json

from dev.stubs.rag.bedrock_embedder import BedrockTitanEmbedder


class FakeBedrockRuntime:
    """Records invoke_model calls and returns canned Titan responses."""

    def __init__(self):
        self.calls = []

    def invoke_model(self, **kwargs):
        self.calls.append(kwargs)
        text = json.loads(kwargs["body"])["inputText"]
        # Distinguishable vector per input so ordering is observable.
        value = 0.25 if text == "second text" else 0.5
        payload = json.dumps({"embedding": [value] * 1024})
        return {"body": io.BytesIO(payload.encode("utf-8"))}


def test_embed_builds_titan_v2_request_body():
    """InvokeModel is called with the Titan V2 body and model id."""
    fake_client = FakeBedrockRuntime()
    embedder = BedrockTitanEmbedder(max_workers=1)
    embedder._client = fake_client

    embedder.embed(["hello world"])

    assert len(fake_client.calls) == 1
    call = fake_client.calls[0]
    assert call["modelId"] == "amazon.titan-embed-text-v2:0"
    assert json.loads(call["body"]) == {
        "inputText": "hello world",
        "dimensions": 1024,
        "normalize": True,
    }


def test_embed_parses_responses_in_order():
    """embed() returns one float vector per text, preserving input order."""
    fake_client = FakeBedrockRuntime()
    embedder = BedrockTitanEmbedder(max_workers=1)
    embedder._client = fake_client

    vectors = embedder.embed(["first text", "second text"])

    assert len(vectors) == 2
    assert all(len(vector) == 1024 for vector in vectors)
    assert vectors[0][0] == 0.5
    assert vectors[1][0] == 0.25
    assert all(isinstance(value, float) for vector in vectors for value in vector)

    # Empty input short-circuits without any Bedrock call.
    assert embedder.embed([]) == []
    assert len(fake_client.calls) == 2
