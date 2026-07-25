"""
Shared skeleton-test scaffolding: an app client over a MOCKED LLMRails.

No AWS, no live pod. The mock ``rails`` + ``Settings`` + the (real, offline)
compiled detector are pre-installed on ``app.state`` (the substitution seam) AND
``app.main.build_rails`` is patched to return the same mock, so the pod is
exercised WITHOUT ever constructing a real NeMo engine or making a Bedrock call —
regardless of whether the test transport triggers lifespan. The deterministic
detector is pure regex (``config/detectors.yml``), so pre-compiling it is free
and needs no AWS; a test that wants the ``/readyz`` fail-fast path drives lifespan
itself with ``app.main.load_detectors`` patched to raise.

The ONE exception to the no-AWS default is the opt-in live Bedrock smoke test
(``test_bedrock_smoke.py``), marked ``requires_aws``. The collection hook below
SKIPS every ``requires_aws`` test cleanly when no AWS credentials resolve, so the
credential-less default run stays green (mirrors the ingestion service's
convention).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.chunk_scan import load_chunk_scanner
from app.detectors import load_detectors
from app.main import app
from app.settings import Settings
from tests.helpers import TEST_MODEL_ID


def _aws_credentials_available() -> bool:
    """True when the ambient AWS credential chain resolves a credential set."""
    try:
        import boto3

        return boto3.Session().get_credentials() is not None
    except Exception:
        return False


def pytest_collection_modifyitems(config, items):
    """Skip ``requires_aws`` tests cleanly when no AWS credentials are present."""
    if _aws_credentials_available():
        return
    skip_aws = pytest.mark.skip(reason="AWS credentials not available")
    for item in items:
        if "requires_aws" in item.keywords:
            item.add_marker(skip_aws)


@pytest.fixture
def settings() -> Settings:
    """Pod settings with the Haiku id the pod STAMPS into every response."""
    return Settings(model_id=TEST_MODEL_ID, region="ap-southeast-2", config_dir="config")


@pytest.fixture
def make_client(monkeypatch, settings):
    """
    Factory: a ``TestClient`` whose ``app.state`` is a mocked rails + settings +
    the real (offline) compiled detector.

    ``build_rails`` is patched to return the SAME mock, so no real NeMo/Bedrock
    construction can happen even if the transport runs lifespan. The detector is
    pre-installed (pure regex, no AWS) so ``/readyz`` reflects a ready pod without
    driving lifespan.
    """
    created: list[TestClient] = []

    def _make(rails: Any) -> TestClient:
        monkeypatch.setattr("app.main.build_rails", lambda _config_dir: rails)
        app.state.settings = settings
        app.state.rails = rails
        app.state.detectors = load_detectors(settings.config_dir)
        app.state.detectors_error = None
        app.state.chunk_scanner = load_chunk_scanner(settings.config_dir)
        app.state.chunk_scanner_error = None
        client = TestClient(app)
        created.append(client)
        return client

    yield _make

    for client in created:
        client.close()
    # Clear every lifespan/seam-populated slot so state never leaks between tests
    # (a stale compiled detector would mask the /readyz fail-fast path).
    for attr in (
        "settings",
        "rails",
        "detectors",
        "detectors_error",
        "chunk_scanner",
        "chunk_scanner_error",
    ):
        if hasattr(app.state, attr):
            delattr(app.state, attr)
