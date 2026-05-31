"""Replay testing for crucible."""

from crucible.replay.candidate_config import (
    CandidateConfig,
    CandidateSpec,
    UpstreamNotes,
    load_candidate_config,
)
from crucible.replay.comparison import (
    ComparisonResult,
    DEFAULT_ALPHA,
    DEFAULT_EFFECT_SIZE_THRESHOLD,
    paired_comparison,
)
from crucible.replay.http_client import HTTPClient
from crucible.replay.phoenix_client import DEFAULT_ENDPOINT, DEFAULT_PROJECT_NAME, PhoenixClient
from crucible.replay.tasks import BaselineTask, CandidateTask

__all__ = [
    # Phoenix client
    "PhoenixClient",
    "DEFAULT_ENDPOINT",
    "DEFAULT_PROJECT_NAME",
    # Comparison
    "ComparisonResult",
    "paired_comparison",
    "DEFAULT_ALPHA",
    "DEFAULT_EFFECT_SIZE_THRESHOLD",
    # HTTP client
    "HTTPClient",
    # Candidate config
    "CandidateConfig",
    "CandidateSpec",
    "UpstreamNotes",
    "load_candidate_config",
    # Tasks
    "CandidateTask",
    "BaselineTask",
]
