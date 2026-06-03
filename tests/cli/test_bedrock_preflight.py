"""
Tests for the Bedrock startup preflight (Task Group 6).

Covers crucible.cli.check.bedrock_preflight:
- it runs (makes the cheap call) only when the resolved provider is bedrock,
  and is a no-op when provider!=bedrock,
- it passes (no raise) on a successful 1-token invoke_model call,
- it fails fast and loud with DISTINCT, actionable messages per failure class:
    * missing/incomplete creds (NoCredentialsError / PartialCredentialsError),
    * access-not-granted (ClientError code AccessDeniedException),
    * model-not-in-region (ClientError code ValidationException),
- an unset region fails loud before any network call (ValueError from
  _resolve_region).

botocore Stubber validates the request against the real bedrock-runtime service
model. The preflight lazily does `import boto3` inside the function, so we patch
boto3.client at the boto3 module (patch-at-import-site).
"""

import io
import json
from unittest import mock

import boto3
import pytest
from botocore.exceptions import ClientError, NoCredentialsError
from botocore.stub import Stubber

from crucible.cli.check import BedrockPreflightError, bedrock_preflight

_REGION = "ap-southeast-2"
_BEDROCK_ENV = {
    "AWS_REGION": _REGION,
    "CRUCIBLE_GENERATOR_PROVIDER": "bedrock",
    "CRUCIBLE_GENERATOR_MODEL": "au.anthropic.claude-sonnet-4-6",
}


def _ok_body() -> dict:
    """A stubbed Anthropic invoke_model response (contentType is required)."""
    payload = json.dumps({"content": [{"text": "ok"}]}).encode()
    return {"body": io.BytesIO(payload), "contentType": "application/json"}


def test_preflight_noop_when_provider_not_bedrock():
    """provider=openai -> the preflight makes NO bedrock call and does not raise."""
    env = {
        "CRUCIBLE_GENERATOR_PROVIDER": "openai",
        "CRUCIBLE_GENERATOR_MODEL": "gpt-4o-mini",
    }

    def fail_client(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("boto3.client must not be called for openai provider")

    with mock.patch.dict("os.environ", env, clear=True):
        with mock.patch.object(boto3, "client", fail_client):
            bedrock_preflight()  # no raise, no client construction


def test_preflight_passes_on_successful_cheap_call():
    """A successful 1-token invoke_model call -> no raise."""
    real_client = boto3.client("bedrock-runtime", region_name=_REGION)
    stubber = Stubber(real_client)
    stubber.add_response("invoke_model", _ok_body())

    with mock.patch.dict("os.environ", _BEDROCK_ENV, clear=True):
        with stubber, mock.patch.object(boto3, "client", lambda *a, **k: real_client):
            bedrock_preflight()  # no raise


def test_preflight_cheap_call_requests_single_token():
    """The preflight body requests exactly one output token (a cheap call)."""
    real_client = boto3.client("bedrock-runtime", region_name=_REGION)
    stubber = Stubber(real_client)
    captured = {}
    original_invoke = real_client.invoke_model

    def recording_invoke(**kwargs):
        captured["modelId"] = kwargs["modelId"]
        captured["body"] = json.loads(kwargs["body"])
        return original_invoke(**kwargs)

    stubber.add_response("invoke_model", _ok_body())

    with mock.patch.dict("os.environ", _BEDROCK_ENV, clear=True):
        with stubber, mock.patch.object(boto3, "client", lambda *a, **k: real_client):
            with mock.patch.object(real_client, "invoke_model", recording_invoke):
                bedrock_preflight()

    assert captured["modelId"] == _BEDROCK_ENV["CRUCIBLE_GENERATOR_MODEL"]
    assert captured["body"]["max_tokens"] == 1


def test_preflight_missing_creds_message():
    """NoCredentialsError -> distinct missing-creds message; original chained."""
    real_client = boto3.client("bedrock-runtime", region_name=_REGION)

    def raise_no_creds(**kwargs):
        raise NoCredentialsError()

    with mock.patch.dict("os.environ", _BEDROCK_ENV, clear=True):
        with mock.patch.object(boto3, "client", lambda *a, **k: real_client):
            with mock.patch.object(real_client, "invoke_model", raise_no_creds):
                with pytest.raises(BedrockPreflightError) as exc_info:
                    bedrock_preflight()

    msg = str(exc_info.value)
    assert "credentials are missing" in msg
    assert isinstance(exc_info.value.__cause__, NoCredentialsError)


def test_preflight_access_not_granted_message():
    """AccessDeniedException -> distinct access-not-granted message; code chained."""
    real_client = boto3.client("bedrock-runtime", region_name=_REGION)
    stubber = Stubber(real_client)
    stubber.add_client_error("invoke_model", service_error_code="AccessDeniedException")

    with mock.patch.dict("os.environ", _BEDROCK_ENV, clear=True):
        with stubber, mock.patch.object(boto3, "client", lambda *a, **k: real_client):
            with pytest.raises(BedrockPreflightError) as exc_info:
                bedrock_preflight()

    msg = str(exc_info.value)
    assert "access" in msg.lower() and "not" in msg.lower()
    cause = exc_info.value.__cause__
    assert isinstance(cause, ClientError)
    assert cause.response["Error"]["Code"] == "AccessDeniedException"


def test_preflight_model_not_in_region_message():
    """ValidationException -> distinct model-not-in-region message; code chained."""
    real_client = boto3.client("bedrock-runtime", region_name=_REGION)
    stubber = Stubber(real_client)
    stubber.add_client_error("invoke_model", service_error_code="ValidationException")

    with mock.patch.dict("os.environ", _BEDROCK_ENV, clear=True):
        with stubber, mock.patch.object(boto3, "client", lambda *a, **k: real_client):
            with pytest.raises(BedrockPreflightError) as exc_info:
                bedrock_preflight()

    msg = str(exc_info.value)
    assert "not available" in msg
    cause = exc_info.value.__cause__
    assert isinstance(cause, ClientError)
    assert cause.response["Error"]["Code"] == "ValidationException"


def test_preflight_region_unset_fails_loud_before_network():
    """Region unset -> ValueError from _resolve_region, before any boto3 call."""
    env = {
        "CRUCIBLE_GENERATOR_PROVIDER": "bedrock",
        "CRUCIBLE_GENERATOR_MODEL": "au.anthropic.claude-sonnet-4-6",
    }

    def fail_client(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("boto3.client must not be called when region is unset")

    with mock.patch.dict("os.environ", env, clear=True):
        with mock.patch.object(boto3, "client", fail_client):
            with pytest.raises(ValueError) as exc_info:
                bedrock_preflight()

    assert "region" in str(exc_info.value).lower()
