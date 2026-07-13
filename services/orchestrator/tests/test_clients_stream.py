"""Bedrock streaming-client tests (chainlit-chat-ui Task Group 1).

Mocked runtime client emitting a scripted event stream — tests never reach
AWS. Focused checks only (per task 1.1) — exhaustive event-permutation and
backoff-timing tests are intentionally skipped.
"""

from __future__ import annotations

import json
from pathlib import Path

import app.clients.bedrock as bedrock_module
import pytest
from app.clients.bedrock import BedrockClient
from app.clients.errors import BedrockThrottleExhaustedError
from botocore.exceptions import ClientError

SONNET_MODEL = "au.anthropic.claude-sonnet-4-6"


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "InvokeModelWithResponseStream")


def _stream_event(payload: dict) -> dict:
    """One bedrock event-stream item: JSON payload wrapped in chunk bytes."""
    return {"chunk": {"bytes": json.dumps(payload).encode("utf-8")}}


def _scripted_events(deltas: list[str]) -> list[dict]:
    """A full Anthropic messages event stream for the given text deltas."""
    events = [
        _stream_event({"type": "message_start", "message": {"usage": {"input_tokens": 321}}}),
        _stream_event({"type": "content_block_start", "index": 0}),
    ]
    events.extend(
        _stream_event(
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": d}}
        )
        for d in deltas
    )
    events.extend(
        [
            _stream_event({"type": "content_block_stop", "index": 0}),
            _stream_event(
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 42},
                }
            ),
            _stream_event(
                {
                    "type": "message_stop",
                    "amazon-bedrock-invocationMetrics": {
                        "inputTokenCount": 321,
                        "outputTokenCount": 42,
                    },
                }
            ),
        ]
    )
    return events


class FakeStreamingRuntime:
    """Scripted invoke_model_with_response_stream: response body or exception."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def invoke_model_with_response_stream(self, **kwargs):
        self.calls.append(kwargs)
        action = self.script.pop(0)
        if isinstance(action, Exception):
            raise action
        return {"body": action}


@pytest.fixture
def recorded_sleeps(monkeypatch):
    """Patch the module-level _sleep; return the recorded backoff values."""
    sleeps: list[float] = []
    monkeypatch.setattr(bedrock_module, "_sleep", sleeps.append)
    return sleeps


def test_generate_stream_yields_deltas_in_order_with_the_blocking_request_body():
    deltas = ["The supply ", "is GST-free ", "[gst-act-1999:0]."]
    runtime = FakeStreamingRuntime([_scripted_events(deltas)])
    client = BedrockClient(region="ap-southeast-2", runtime_client=runtime)

    stream = client.generate_stream(
        "prompt", model_id=SONNET_MODEL, temperature=0.0, max_tokens=1024
    )
    received = list(stream)

    assert received == deltas
    assert "".join(received) == "The supply is GST-free [gst-act-1999:0]."
    # The request body is the SAME Anthropic messages dialect the blocking
    # path sends (anthropic_version pinned; converse_stream rejected).
    call = runtime.calls[0]
    assert call["modelId"] == SONNET_MODEL
    assert json.loads(call["body"]) == {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 1024,
        "temperature": 0.0,
        "messages": [{"role": "user", "content": "prompt"}],
    }


def test_stream_result_exposes_token_counts_and_stop_reason_as_plain_data():
    runtime = FakeStreamingRuntime([_scripted_events(["a", "b"])])
    client = BedrockClient(region="ap-southeast-2", runtime_client=runtime)
    stream = client.generate_stream(
        "prompt", model_id=SONNET_MODEL, temperature=0.0, max_tokens=1024
    )

    # Plain data is only available AFTER the stream has been fully consumed.
    with pytest.raises(RuntimeError, match="fully consumed"):
        stream.result()

    list(stream)
    result = stream.result()
    assert result.text == "ab"
    assert result.model_id == SONNET_MODEL
    assert result.input_tokens == 321
    assert result.output_tokens == 42
    assert result.stop_reason == "end_turn"


def test_initial_call_throttling_retries_with_capped_backoff_then_exhausts(recorded_sleeps):
    # Two throttles on the INITIAL call, then a good stream: shared policy.
    runtime = FakeStreamingRuntime(
        [
            _client_error("ThrottlingException"),
            _client_error("ThrottlingException"),
            _scripted_events(["ok"]),
        ]
    )
    client = BedrockClient(region="ap-southeast-2", runtime_client=runtime)

    stream = client.generate_stream(
        "prompt", model_id=SONNET_MODEL, temperature=0.0, max_tokens=1024
    )
    assert list(stream) == ["ok"]
    assert len(runtime.calls) == 3
    assert recorded_sleeps == [0.5, 1.0]

    # Persistent throttling on the initial call exhausts the bounded budget.
    exhausted_runtime = FakeStreamingRuntime([_client_error("ThrottlingException")] * 10)
    exhausted_client = BedrockClient(region="ap-southeast-2", runtime_client=exhausted_runtime)
    with pytest.raises(BedrockThrottleExhaustedError):
        exhausted_client.generate_stream(
            "prompt", model_id=SONNET_MODEL, temperature=0.0, max_tokens=1024
        )
    assert len(exhausted_runtime.calls) == bedrock_module._MAX_RETRIES + 1


def test_mid_stream_failure_propagates_unchanged_and_is_never_retried(recorded_sleeps):
    """A throttle AFTER deltas have flowed is the mid-stream error seam."""
    error = _client_error("ThrottlingException")

    def failing_events():
        yield from _scripted_events(["partial "])[:3]  # message_start + one delta
        raise error

    runtime = FakeStreamingRuntime([failing_events()])
    client = BedrockClient(region="ap-southeast-2", runtime_client=runtime)
    stream = client.generate_stream(
        "prompt", model_id=SONNET_MODEL, temperature=0.0, max_tokens=1024
    )

    received: list[str] = []
    with pytest.raises(ClientError) as excinfo:
        for delta in stream:
            received.append(delta)

    assert excinfo.value is error  # propagated UNCHANGED — no partial-text replay
    assert received == ["partial "]
    assert len(runtime.calls) == 1  # never re-invoked
    assert recorded_sleeps == []


def test_no_module_under_app_clients_imports_opentelemetry():
    """Clients return plain data; the OTel discipline is preserved."""
    clients_dir = Path(bedrock_module.__file__).resolve().parent
    for path in clients_dir.glob("*.py"):
        assert "opentelemetry" not in path.read_text(encoding="utf-8"), path
