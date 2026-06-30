"""
HTTP client wrapper for candidate RAG services.

This module provides HTTPClient class that wraps requests library with
retry logic, timeout enforcement, and header propagation for calling
candidate RAG services.
"""

from __future__ import annotations

import logging
import time

from beartype import beartype
from beartype.typing import Any, Final

from crucible.service.runners.candidate_config import CandidateConfig

# Constants
HEALTH_CHECK_TIMEOUT: Final[int] = 5
HEALTH_CHECK_RETRIES: Final[int] = 3
HEALTH_CHECK_RETRY_DELAY: Final[float] = 1.0
_SUPPORTED_CONTRACTS: Final[tuple[str, ...]] = ("1.0", "1.1")

logger = logging.getLogger(__name__)


@beartype
def _parse_contract_version(version: str) -> tuple[int, int]:
    """
    Parse semantic version string into (major, minor) tuple.

    Args:
        version: Semantic version string (e.g., "1.0", "1.1").

    Returns:
        Tuple of (major, minor) version numbers.

    Raises:
        ValueError: If version string is invalid.

    """
    parts = version.split(".")
    if len(parts) < 2:
        raise ValueError(f"Invalid contract version: {version}")
    return int(parts[0]), int(parts[1])


@beartype
class HTTPClient:
    """
    HTTP client for calling candidate RAG services.

    Wraps requests library with exponential backoff retry, timeout enforcement,
    and header propagation for version tracking and distributed tracing.

    Attributes:
        config: Candidate configuration with service URL and settings.
        health_check_enabled: Whether to check /health before queries.

    """

    def __init__(
        self,
        config: CandidateConfig,
        health_check_enabled: bool = True,
    ) -> None:
        """
        Initialize HTTP client.

        Args:
            config: Candidate configuration.
            health_check_enabled: Whether to check /health before first query.

        Raises:
            RuntimeError: If contract version is not supported.

        """
        self.config = config
        self.health_check_enabled = health_check_enabled
        self._health_checked = False

        # Validate contract version
        self._validate_contract_version()

    def _validate_contract_version(self) -> None:
        """
        Validate contract version against supported versions.

        Raises:
            RuntimeError: If major version is not supported.

        """
        contract_version = self.config.candidate.contract_version

        try:
            major, minor = _parse_contract_version(contract_version)
        except ValueError as e:
            raise RuntimeError(f"Invalid contract version '{contract_version}': {e}") from e

        # Check if any supported version has matching major version
        supported = False
        for supported_ver in _SUPPORTED_CONTRACTS:
            supported_major, _ = _parse_contract_version(supported_ver)
            if major == supported_major:
                supported = True
                break

        if not supported:
            supported_majors = [v.split(".")[0] for v in _SUPPORTED_CONTRACTS]
            raise RuntimeError(
                f"Unsupported contract version: {contract_version}. "
                f"Supported major versions: {supported_majors}"
            )

        # Log warning if minor version differs
        if contract_version not in _SUPPORTED_CONTRACTS:
            logger.warning(
                f"Contract version {contract_version} not explicitly tested. "
                f"Tested versions: {_SUPPORTED_CONTRACTS}"
            )

    def query(self, payload: dict[str, Any]) -> dict[str, Any]:
        """
        Execute query against candidate service with retry logic.

        Args:
            payload: Query payload (question, top_k, etc.).

        Returns:
            Response JSON from candidate service.

        Raises:
            RuntimeError: If all retries are exhausted.

        """
        import requests

        # Health check before first query if enabled
        if self.health_check_enabled and not self._health_checked:
            self._ensure_healthy()
            self._health_checked = True

        last_error = None
        for attempt in range(self.config.candidate.max_retries + 1):
            try:
                headers = self._build_headers()
                response = requests.post(
                    self.config.candidate.service_url,
                    json=payload,
                    timeout=self.config.candidate.timeout_seconds,
                    headers=headers,
                )
                response.raise_for_status()
                return response.json()

            except requests.RequestException as e:
                last_error = e
                if attempt < self.config.candidate.max_retries:
                    # Exponential backoff
                    time.sleep(2**attempt)
                    continue

        raise RuntimeError(
            f"Failed to call candidate {self.config.name} at "
            f"{self.config.candidate.service_url}: {last_error}"
        ) from last_error

    def _build_headers(self) -> dict[str, str]:
        """
        Build HTTP headers for request.

        Returns:
            Dictionary of HTTP headers.

        """
        headers = {
            "Content-Type": "application/json",
            "X-Candidate-Version": self.config.candidate.service_version,
            "X-Contract-Version": self.config.candidate.contract_version,
        }

        # Add traceparent if available (for distributed tracing)
        traceparent = self._get_traceparent()
        if traceparent:
            headers["traceparent"] = traceparent

        return headers

    def _get_traceparent(self) -> str | None:
        """
        Get W3C traceparent header from current trace context.

        Returns:
            traceparent header value or None if no active trace.

        """
        try:
            from opentelemetry import trace

            current_span = trace.get_current_span()
            if current_span and current_span.is_recording():
                span_context = current_span.get_span_context()
                if span_context.is_valid:
                    # Format: version-trace_id-span_id-trace_flags
                    return (
                        f"00-{span_context.trace_id:032x}-"
                        f"{span_context.span_id:016x}-"
                        f"0{span_context.trace_flags:01x}"
                    )
        except Exception:
            # OpenTelemetry not available or not initialized
            pass

        return None

    def check_health(self) -> bool:
        """
        Check if candidate service is healthy.

        Makes up to HEALTH_CHECK_RETRIES attempts to call /health endpoint.

        Returns:
            True if service is healthy, False otherwise.

        """
        import requests

        health_url = self.config.candidate.service_url.replace("/query", "/health")

        for attempt in range(HEALTH_CHECK_RETRIES):
            try:
                response = requests.get(
                    health_url,
                    timeout=HEALTH_CHECK_TIMEOUT,
                )
                if response.status_code == 200:
                    return True
            except requests.RequestException:
                if attempt < HEALTH_CHECK_RETRIES - 1:
                    time.sleep(HEALTH_CHECK_RETRY_DELAY)
                    continue

        return False

    def _ensure_healthy(self) -> None:
        """
        Ensure service is healthy, raising error if not.

        Raises:
            RuntimeError: If service health check fails.

        """
        if not self.check_health():
            raise RuntimeError(
                f"Service {self.config.name} at {self.config.candidate.service_url} "
                f"is not healthy after {HEALTH_CHECK_RETRIES} attempts"
            )


__all__ = ["HTTPClient", "_SUPPORTED_CONTRACTS"]
