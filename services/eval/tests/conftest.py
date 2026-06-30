"""
Shared pytest fixtures + collection hooks.

Auto-skips any test marked ``@pytest.mark.phoenix_integration`` when a live
Phoenix endpoint is not reachable, so unmarked/local runs never error on a
missing server. The default suite is invoked with ``-m "not phoenix_integration"``
and stays fully hermetic; the live write-back tests only run when both the marker
is selected AND ``PHOENIX_ENDPOINT`` resolves to a reachable host.
"""

from __future__ import annotations

import os
import socket
from urllib.parse import urlparse

import pytest

_DEFAULT_PHOENIX_ENDPOINT = "http://localhost:6006"
_PROBE_TIMEOUT_SECONDS = 0.5


def _phoenix_endpoint_reachable() -> bool:
    """Return True if a TCP connection to PHOENIX_ENDPOINT succeeds quickly."""
    endpoint = os.environ.get("PHOENIX_ENDPOINT", _DEFAULT_PHOENIX_ENDPOINT)
    parsed = urlparse(endpoint if "://" in endpoint else f"http://{endpoint}")
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=_PROBE_TIMEOUT_SECONDS):
            return True
    except OSError:
        return False


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip phoenix_integration tests when no live Phoenix endpoint is reachable."""
    if _phoenix_endpoint_reachable():
        return
    skip = pytest.mark.skip(reason="PHOENIX_ENDPOINT unreachable; skipping live Phoenix tests")
    for item in items:
        if "phoenix_integration" in item.keywords:
            item.add_marker(skip)
