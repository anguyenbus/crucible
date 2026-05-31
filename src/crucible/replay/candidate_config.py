"""
Candidate configuration for external RAG services.

This module loads YAML configs that describe WHERE to find candidate services.
The eval harness calls these services via HTTP - it does NOT construct them.
"""

from __future__ import annotations

from pathlib import Path

# Removed beartype due to Python 3.14 import hangs
from typing import Any, Final, Self

from pydantic import BaseModel, Field, field_validator

# Constants
DEFAULT_CANDIDATES_DIR: Final[Path] = Path("configs/candidates")
DEFAULT_TIMEOUT: Final[int] = 30
DEFAULT_RETRIES: Final[int] = 2


class UpstreamNotes(BaseModel):
    """Informational metadata about the candidate service."""

    contact_team: str | None = None
    contact_slack: str | None = None
    design_doc: str | None = None
    changelog: str | None = None
    built_from_commit: str | None = None
    deployed_at: str | None = None


class CandidateSpec(BaseModel):
    """
    External RAG service specification.

    Describes WHERE to find a candidate service and HOW to call it.
    The eval harness does NOT construct the candidate - it calls this service.

    Attributes:
        service_url: HTTP endpoint where the candidate service is running.
        service_version: Version identifier for traceability.
        contract_version: API contract version (for compatibility checking).
        timeout_seconds: Request timeout.
        max_retries: Maximum retry attempts.
        top_k: Number of chunks to retrieve.

    """

    service_url: str = Field(..., description="HTTP endpoint of the candidate service")
    service_version: str = Field(..., description="Version identifier for traceability")
    contract_version: str = Field(default="1.0", description="API contract version")
    timeout_seconds: int = Field(default=DEFAULT_TIMEOUT, ge=1, le=300)
    max_retries: int = Field(default=DEFAULT_RETRIES, ge=0, le=5)
    top_k: int = Field(default=5, ge=1, le=100, description="Chunks to retrieve")

    @field_validator("service_url")
    @classmethod
    def validate_service_url(cls, v: str) -> str:
        """Validate service URL format."""
        if not v.startswith(("http://", "https://")):
            raise ValueError(
                f"service_url must start with http:// or https://, got: {v}"
            )
        return v


# beartype removed due to Python 3.14 import hangs
class CandidateConfig(BaseModel):
    """
    RAG candidate configuration loaded from YAML.

    This describes an external service to evaluate, not how to construct one.

    Attributes:
        name: Unique identifier for this candidate.
        description: What this candidate is and why it exists.
        candidate: Service specification (URL, version, etc.).
        upstream_notes: Optional metadata about the upstream team.

    """

    name: str = Field(..., description="Unique identifier")
    description: str = Field(default="", description="What this candidate is")
    candidate: CandidateSpec = Field(..., description="Service specification")
    upstream_notes: UpstreamNotes | None = Field(default=None)

    @classmethod
    def from_yaml_file(cls, path: str | Path) -> Self:
        """
        Load candidate configuration from YAML file.

        Args:
            path: Path to YAML file.

        Returns:
            CandidateConfig instance.

        Raises:
            FileNotFoundError: If file doesn't exist.
            ValueError: If YAML is invalid.

        """
        import yaml

        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Candidate config not found: {path}")

        with open(path) as f:
            data = yaml.safe_load(f)

        # Parse upstream_notes if present
        notes_data = data.pop("upstream_notes", None)
        upstream_notes = UpstreamNotes(**notes_data) if notes_data else None

        # Parse candidate spec
        candidate_data = data.pop("candidate", {})
        candidate_spec = CandidateSpec(**candidate_data)

        return cls(
            name=data["name"],
            description=data.get("description", ""),
            candidate=candidate_spec,
            upstream_notes=upstream_notes,
        )

    def call(self, payload: dict[str, Any]) -> dict[str, Any]:
        """
        Call the candidate service with the given payload.

        Args:
            payload: Request payload (question, context, etc.)

        Returns:
            Response from the candidate service.

        Raises:
            RuntimeError: If all retries are exhausted.

        """
        import time

        import requests

        last_error = None
        for attempt in range(self.candidate.max_retries + 1):
            try:
                response = requests.post(
                    self.candidate.service_url,
                    json=payload,
                    timeout=self.candidate.timeout_seconds,
                    headers={
                        "Content-Type": "application/json",
                        "X-Candidate-Version": self.candidate.service_version,
                        "X-Contract-Version": self.candidate.contract_version,
                    },
                )
                response.raise_for_status()
                return response.json()
            except requests.RequestException as e:
                last_error = e
                if attempt < self.candidate.max_retries:
                    # Exponential backoff
                    time.sleep(2**attempt)
                    continue

        raise RuntimeError(
            f"Failed to call candidate {self.name} at "
            f"{self.candidate.service_url}: {last_error}"
        ) from last_error


# beartype removed due to Python 3.14 import hangs
def load_candidate_config(path: str | Path) -> CandidateConfig:
    """
    Load candidate configuration from YAML file.

    Convenience function that also validates the file exists.

    Args:
        path: Path to YAML file.

    Returns:
        CandidateConfig instance.

    """
    return CandidateConfig.from_yaml_file(path)
