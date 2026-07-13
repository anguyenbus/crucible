"""AWS client tests (Group 2): mocked boto3/transport only — NO live AWS calls.

Focused checks only (per task 2.1) — exhaustive backoff-timing and
serialization permutations are intentionally skipped.
"""

import io
import json
from pathlib import Path

import app.clients.bedrock as bedrock_module
import pytest
from app.clients.bedrock import BedrockClient
from app.clients.errors import BedrockThrottleExhaustedError, OpenSearchNotReadyError
from app.clients.opensearch import OpenSearchSearchClient, _create_raw_client
from botocore.exceptions import ClientError

TITAN_MODEL = "amazon.titan-embed-text-v2:0"
SONNET_MODEL = "au.anthropic.claude-sonnet-4-6"


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "InvokeModel")


def _invoke_response(payload: dict) -> dict:
    return {"body": io.BytesIO(json.dumps(payload).encode("utf-8"))}


class FakeBedrockRuntime:
    """Scripted invoke_model: each item is a response dict or an exception."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def invoke_model(self, **kwargs):
        self.calls.append(kwargs)
        action = self.script.pop(0)
        if isinstance(action, Exception):
            raise action
        return action


@pytest.fixture
def recorded_sleeps(monkeypatch):
    """Patch the module-level _sleep; return the recorded backoff values."""
    sleeps: list[float] = []
    monkeypatch.setattr(bedrock_module, "_sleep", sleeps.append)
    return sleeps


def test_generate_retries_only_on_throttling_then_succeeds(recorded_sleeps):
    runtime = FakeBedrockRuntime(
        [
            _client_error("ThrottlingException"),
            _client_error("ThrottlingException"),
            _invoke_response(
                {
                    "content": [{"text": "answer [d:0]."}],
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                    "stop_reason": "end_turn",
                }
            ),
        ]
    )
    client = BedrockClient(region="ap-southeast-2", runtime_client=runtime)

    result = client.generate("prompt", model_id=SONNET_MODEL, temperature=0.0, max_tokens=1024)

    assert result.text == "answer [d:0]."
    assert len(runtime.calls) == 3
    # Capped exponential backoff, no real sleeping (patched _sleep recorded).
    assert recorded_sleeps == [0.5, 1.0]


def test_throttling_exhausted_raises_throttle_exhausted_error(recorded_sleeps):
    runtime = FakeBedrockRuntime([_client_error("ThrottlingException")] * 10)
    client = BedrockClient(region="ap-southeast-2", runtime_client=runtime)

    with pytest.raises(BedrockThrottleExhaustedError):
        client.generate("prompt", model_id=SONNET_MODEL, temperature=0.0, max_tokens=1024)

    # Bounded budget: 1 initial attempt + _MAX_RETRIES retries, one sleep each.
    assert len(runtime.calls) == bedrock_module._MAX_RETRIES + 1
    assert len(recorded_sleeps) == bedrock_module._MAX_RETRIES


def test_non_throttle_client_error_is_never_retried_and_reraised_unchanged(recorded_sleeps):
    error = _client_error("AccessDeniedException")
    runtime = FakeBedrockRuntime([error])
    client = BedrockClient(region="ap-southeast-2", runtime_client=runtime)

    with pytest.raises(ClientError) as excinfo:
        client.generate("prompt", model_id=SONNET_MODEL, temperature=0.0, max_tokens=1024)

    assert excinfo.value is error  # re-raised UNCHANGED (the 502 seam)
    assert len(runtime.calls) == 1
    assert recorded_sleeps == []


def test_generate_returns_plain_data_and_clients_never_import_opentelemetry():
    runtime = FakeBedrockRuntime(
        [
            _invoke_response(
                {
                    "content": [{"text": "the answer"}],
                    "usage": {"input_tokens": 321, "output_tokens": 42},
                    "stop_reason": "end_turn",
                }
            )
        ]
    )
    client = BedrockClient(region="ap-southeast-2", runtime_client=runtime)

    result = client.generate("p", model_id=SONNET_MODEL, temperature=0.0, max_tokens=1024)

    # Plain data for the router to attach as span attributes.
    assert result.model_id == SONNET_MODEL
    assert result.input_tokens == 321
    assert result.output_tokens == 42
    assert result.stop_reason == "end_turn"
    # No module under app/clients/ touches opentelemetry (source-level check;
    # sys.modules is no longer a valid probe now that app.observability — a
    # NON-client module — legitimately imports OTel).
    clients_dir = Path(bedrock_module.__file__).resolve().parent
    for path in clients_dir.glob("*.py"):
        assert "opentelemetry" not in path.read_text(encoding="utf-8"), path


def test_embed_query_requests_titan_v2_1024_normalized():
    runtime = FakeBedrockRuntime([_invoke_response({"embedding": [0.5] * 1024})])
    client = BedrockClient(region="ap-southeast-2", runtime_client=runtime)

    vector = client.embed_query("what is GST?", model_id=TITAN_MODEL)

    assert len(vector) == 1024
    assert all(isinstance(value, float) for value in vector)
    call = runtime.calls[0]
    assert call["modelId"] == TITAN_MODEL
    body = json.loads(call["body"])
    assert body == {"inputText": "what is GST?", "dimensions": 1024, "normalize": True}


class FakeRawOpenSearch:
    """Minimal raw opensearch-py stand-in for wrapper tests."""

    def __init__(self, mapping):
        self._mapping = mapping
        self.search_calls = []

        class _Indices:
            def __init__(inner, outer):
                inner._outer = outer

            def get_mapping(inner, index):
                return inner._outer._mapping

            def exists(inner, index):
                return True

        self.indices = _Indices(self)

    def search(self, *, index, body, params):
        self.search_calls.append({"index": index, "body": body, "params": params})
        return {"hits": {"hits": []}}


def _make_client(mapping) -> tuple[OpenSearchSearchClient, FakeRawOpenSearch]:
    raw = FakeRawOpenSearch(mapping)
    client = OpenSearchSearchClient(
        endpoint="https://os.example.com",
        index="legal-rag-bench",
        embedder_model_id=TITAN_MODEL,
        embedder_dimensions=1024,
        region="ap-southeast-2",
        raw_client=raw,
    )
    return client, raw


def test_meta_guard_absent_trusts_config_present_mismatch_raises():
    # Absent _meta (the POC index shape): construction succeeds, trust config.
    client, _ = _make_client({"legal-rag-bench": {"mappings": {"properties": {}}}})
    assert client.index_exists() is True

    # Present with an embedder mismatch: not-ready error at init.
    with pytest.raises(OpenSearchNotReadyError, match="Embedder mismatch"):
        _make_client({"legal-rag-bench": {"mappings": {"_meta": {"embedder": "some-other-model"}}}})

    # Present with a dims mismatch: not-ready error at init.
    with pytest.raises(OpenSearchNotReadyError, match="dimension"):
        _make_client(
            {"legal-rag-bench": {"mappings": {"_meta": {"embedder": TITAN_MODEL, "dims": 384}}}}
        )


def test_search_binds_pipeline_explicitly_per_request():
    client, raw = _make_client({"legal-rag-bench": {"mappings": {}}})

    client.search({"size": 8}, search_pipeline="hybrid-search-pipeline")
    client.search({"size": 8}, search_pipeline="hybrid-search-pipeline")

    assert len(raw.search_calls) == 2
    for call in raw.search_calls:
        assert call["index"] == "legal-rag-bench"
        assert call["params"] == {"search_pipeline": "hybrid-search-pipeline"}


def test_opensearch_client_is_read_only_and_configures_no_retries(monkeypatch):
    # Public surface: search + index_exists only — no ingest/mutation methods.
    public = {name for name in dir(OpenSearchSearchClient) if not name.startswith("_")}
    assert public == {"search", "index_exists"}

    # Raw client construction: SigV4 shape, 60s timeout, ZERO retries.
    import boto3
    import opensearchpy

    captured: dict = {}

    class FakeSession:
        def get_credentials(self):
            return object()

    monkeypatch.setattr(boto3, "Session", FakeSession)
    monkeypatch.setattr(
        opensearchpy, "OpenSearch", lambda **kwargs: captured.update(kwargs) or object()
    )

    _create_raw_client("https://search-domain.example.com/", region="ap-southeast-2")

    assert captured["hosts"] == [{"host": "search-domain.example.com", "port": 443}]
    assert captured["timeout"] == 60
    assert captured["max_retries"] == 0
    assert captured["retry_on_timeout"] is False
    assert captured["connection_class"] is opensearchpy.Urllib3HttpConnection
