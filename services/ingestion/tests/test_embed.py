"""Task Group 5: Titan embedding — backoff on throttling, input guards (no AWS)."""

import io
import json

import pytest
from botocore.exceptions import ClientError

from app.pipeline import embed
from app.pipeline.chunk import count_tokens
from app.pipeline.embed import (
    TITAN_CHAR_LIMIT,
    TITAN_TOKEN_LIMIT,
    EmbeddingInputTooLargeError,
    embed_text,
)

VECTOR = [0.1] * 1024


class ThrottleThenSucceed:
    """Fake bedrock-runtime client: throttles N times, then returns a vector."""

    def __init__(self, throttle_times: int):
        self.throttle_times = throttle_times
        self.calls = 0

    def invoke_model(self, **kwargs) -> dict:
        self.calls += 1
        if self.calls <= self.throttle_times:
            raise ClientError(
                {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
                "InvokeModel",
            )
        payload = json.dumps({"embedding": VECTOR, "inputTextTokenCount": 5})
        return {"body": io.BytesIO(payload.encode("utf-8"))}


def test_throttling_triggers_exponential_backoff_with_jitter_then_succeeds(
    monkeypatch,
):
    sleeps: list[float] = []
    jitter_calls: list[tuple[float, float]] = []
    monkeypatch.setattr(embed.time, "sleep", sleeps.append)

    def fake_uniform(low: float, high: float) -> float:
        jitter_calls.append((low, high))
        return 0.0

    monkeypatch.setattr(embed.random, "uniform", fake_uniform)
    client = ThrottleThenSucceed(throttle_times=2)

    vector = embed_text("hybrid search", client=client)

    assert vector == VECTOR
    assert client.calls == 3
    # Exponential backoff (base 0.5, doubling) with jitter drawn per retry.
    assert sleeps == [0.5, 1.0]
    assert jitter_calls == [(0.0, 0.5), (0.0, 1.0)]


def test_input_over_titan_limits_trips_the_guard():
    never_called = ThrottleThenSucceed(throttle_times=0)

    # Character cap: prose hits the 50,000-char request limit first.
    with pytest.raises(EmbeddingInputTooLargeError):
        embed_text("a" * (TITAN_CHAR_LIMIT + 1), client=never_called)

    # Token cap: a token-dense input exceeds 8192 tokens within the char cap.
    dense_text = "9Zq3 " * (TITAN_CHAR_LIMIT // 5)
    assert len(dense_text) <= TITAN_CHAR_LIMIT
    assert count_tokens(dense_text) > TITAN_TOKEN_LIMIT
    with pytest.raises(EmbeddingInputTooLargeError):
        embed_text(dense_text, client=never_called)

    assert never_called.calls == 0
