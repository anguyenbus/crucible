"""
Tests for the Bedrock generator path (Task Group 3).

Covers the boto3-client layer of dev.stubs.rag.generator._generate_bedrock:
- the client is constructed with the resolved region (no us-east-1 hardcode),
- the Anthropic Claude request body shaping is preserved,
- botocore ClientError codes (AccessDenied / ValidationException) are preserved
  and NOT flattened into a generic ValueError,
- ThrottlingException is caught separately and retried with backoff (the backoff
  sleep is patched so tests do not actually sleep), distinct from other errors.

botocore Stubber validates request params against the real bedrock-runtime
service model. The generator lazily does `import boto3` inside the method, so we
patch boto3.client at its own module (the standard patch-at-import-site approach).
"""

import io
import json
from unittest import mock

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from dev.stubs.rag import generator as gen_mod
from dev.stubs.rag.generator import LLMGenerator

_CHUNKS = [{"chunk_id": "doc1_chunk_00000", "text": "The sky is blue."}]
_REGION = "ap-southeast-2"
_MODEL = "au.anthropic.claude-sonnet-4-6"


def _make_generator():
    """Build a Bedrock-backed generator (no credentials needed for construction)."""
    with mock.patch.dict("os.environ", {"AWS_REGION": _REGION}, clear=False):
        return LLMGenerator(model=_MODEL)


def _ok_body() -> dict:
    """A stubbed Anthropic invoke_model response (contentType is required)."""
    payload = json.dumps({"content": [{"text": "It is blue."}]}).encode()
    return {"body": io.BytesIO(payload), "contentType": "application/json"}


def test_client_constructed_with_resolved_region_not_us_east_1():
    """The bedrock-runtime client gets the resolved region, never us-east-1."""
    generator = _make_generator()
    seen = {}

    real_client = boto3.client("bedrock-runtime", region_name=_REGION)
    stubber = Stubber(real_client)
    stubber.add_response("invoke_model", _ok_body())

    def fake_client(service, region_name=None, **kwargs):
        seen["service"] = service
        seen["region_name"] = region_name
        seen["kwargs"] = kwargs
        return real_client

    with mock.patch.dict("os.environ", {"AWS_REGION": _REGION}, clear=False):
        with stubber, mock.patch.object(boto3, "client", fake_client):
            generator._generate_bedrock("What colour?", _CHUNKS)

    assert seen["service"] == "bedrock-runtime"
    assert seen["region_name"] == _REGION
    assert seen["region_name"] != "us-east-1"
    # No explicit credentials passed -> the AWS credential chain engages.
    assert "aws_access_key_id" not in seen["kwargs"]
    assert "aws_secret_access_key" not in seen["kwargs"]


def test_anthropic_request_body_shaping_preserved():
    """The Anthropic Claude request body shape is preserved (Stubber-validated)."""
    generator = _make_generator()
    real_client = boto3.client("bedrock-runtime", region_name=_REGION)
    stubber = Stubber(real_client)
    captured = {}

    original_invoke = real_client.invoke_model

    def recording_invoke(**kwargs):
        captured["modelId"] = kwargs["modelId"]
        captured["body"] = json.loads(kwargs["body"])
        # Still routes through the Stubber, which validates against the
        # bedrock-runtime service model.
        return original_invoke(**kwargs)

    stubber.add_response("invoke_model", _ok_body())

    with mock.patch.dict("os.environ", {"AWS_REGION": _REGION}, clear=False):
        with stubber, mock.patch.object(boto3, "client", lambda *a, **k: real_client):
            with mock.patch.object(real_client, "invoke_model", recording_invoke):
                generator._generate_bedrock("What colour?", _CHUNKS)

    assert captured["modelId"] == _MODEL
    body = captured["body"]
    assert body["anthropic_version"] == "bedrock-2023-05-31"
    assert body["max_tokens"] == 1024
    assert body["system"]
    assert body["messages"][0]["role"] == "user"


def test_client_error_code_preserved_not_flattened_to_valueerror():
    """AccessDenied / ValidationException codes are preserved, not ValueError."""
    generator = _make_generator()

    for code in ("AccessDeniedException", "ValidationException"):
        real_client = boto3.client("bedrock-runtime", region_name=_REGION)
        stubber = Stubber(real_client)
        stubber.add_client_error("invoke_model", service_error_code=code)

        with mock.patch.dict("os.environ", {"AWS_REGION": _REGION}, clear=False):
            with stubber, mock.patch.object(boto3, "client", lambda *a, _rc=real_client, **k: _rc):
                with pytest.raises(ClientError) as exc_info:
                    generator._generate_bedrock("What colour?", _CHUNKS)

        assert exc_info.value.response["Error"]["Code"] == code
        assert not isinstance(exc_info.value, ValueError)


def test_throttling_is_retried_with_backoff_then_succeeds():
    """ThrottlingException is retried with exponential backoff, then succeeds."""
    generator = _make_generator()
    real_client = boto3.client("bedrock-runtime", region_name=_REGION)
    stubber = Stubber(real_client)
    stubber.add_client_error("invoke_model", service_error_code="ThrottlingException")
    stubber.add_client_error("invoke_model", service_error_code="ThrottlingException")
    stubber.add_response("invoke_model", _ok_body())

    sleeps: list[float] = []

    with mock.patch.dict("os.environ", {"AWS_REGION": _REGION}, clear=False):
        with (
            stubber,
            mock.patch.object(boto3, "client", lambda *a, **k: real_client),
            mock.patch.object(gen_mod, "_sleep", lambda s: sleeps.append(s)),
        ):
            result = generator._generate_bedrock("What colour?", _CHUNKS)

    assert result["text"] == "It is blue."
    # One backoff per retry; exponential growth, capped.
    assert sleeps == [0.5, 1.0]


def test_throttling_exhausts_budget_reraises_with_code_preserved():
    """Persistent throttling re-raises ThrottlingException; code preserved, no ValueError."""
    generator = _make_generator()
    real_client = boto3.client("bedrock-runtime", region_name=_REGION)
    stubber = Stubber(real_client)
    for _ in range(gen_mod._BEDROCK_MAX_RETRIES + 1):
        stubber.add_client_error("invoke_model", service_error_code="ThrottlingException")

    with mock.patch.dict("os.environ", {"AWS_REGION": _REGION}, clear=False):
        with (
            stubber,
            mock.patch.object(boto3, "client", lambda *a, **k: real_client),
            mock.patch.object(gen_mod, "_sleep", lambda s: None),
        ):
            with pytest.raises(ClientError) as exc_info:
                generator._generate_bedrock("What colour?", _CHUNKS)

    assert exc_info.value.response["Error"]["Code"] == "ThrottlingException"
    assert not isinstance(exc_info.value, ValueError)


def test_non_throttle_error_is_not_retried():
    """A non-throttle ClientError is raised on the first attempt (no backoff)."""
    generator = _make_generator()
    real_client = boto3.client("bedrock-runtime", region_name=_REGION)
    stubber = Stubber(real_client)
    # Only ONE queued error: if the code retried, the Stubber would raise an
    # unstubbed-call error instead of the AccessDenied ClientError.
    stubber.add_client_error("invoke_model", service_error_code="AccessDeniedException")

    sleeps: list[float] = []
    with mock.patch.dict("os.environ", {"AWS_REGION": _REGION}, clear=False):
        with (
            stubber,
            mock.patch.object(boto3, "client", lambda *a, **k: real_client),
            mock.patch.object(gen_mod, "_sleep", lambda s: sleeps.append(s)),
        ):
            with pytest.raises(ClientError) as exc_info:
                generator._generate_bedrock("What colour?", _CHUNKS)

    assert exc_info.value.response["Error"]["Code"] == "AccessDeniedException"
    assert sleeps == []
