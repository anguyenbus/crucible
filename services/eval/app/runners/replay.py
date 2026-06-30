"""
Replay evaluation library (pure, dependency-injected).

Phase 3 extracts the replay-runner logic out of the ``__main__`` shell into an
injectable library function. Replay is reserved Appendix-A machinery (kept
working but not yet fully implemented); the structure-preserving move keeps the
``http_client`` / ``candidate_config`` / ``tasks`` modules separate under
``service/runners/`` and the comparison statistics in
``app.kernel.replay_stats.comparison``.

The library performs NO file I/O: the CLI shell resolves the candidate spec /
baseline and persists any comparison output.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from beartype import beartype


@dataclass(frozen=True, slots=True)
class ReplayResult:
    """
    Pure result of a replay run.

    Attributes:
        candidate_spec: Path to the candidate service spec that was replayed.
        production_baseline: Phoenix project name used as the baseline.
        implemented: Whether the full replay path executed (reserved machinery;
            currently a placeholder until Appendix-A is wired).

    """

    candidate_spec: Path
    production_baseline: str
    implemented: bool = False


@beartype
def run_replay(
    *,
    candidate_spec: Path,
    production_baseline: str = "default",
) -> ReplayResult:
    """
    Run replay evaluation comparing a candidate service against a baseline.

    Reserved Appendix-A machinery: the candidate/baseline replay + statistical
    comparison (``app.kernel.replay_stats.comparison``) is not yet fully
    wired. This library function pins the injectable signature + return shape so
    the CLI shell stays a thin wrapper.

    Args:
        candidate_spec: Path to the candidate service specification YAML.
        production_baseline: Phoenix project name for production baseline traces.

    Returns:
        A ``ReplayResult`` describing the (currently placeholder) replay run.

    """
    return ReplayResult(
        candidate_spec=candidate_spec,
        production_baseline=production_baseline,
        implemented=False,
    )
