"""Paired statistical comparison primitives (pure kernel)."""

from crucible.kernel.replay_stats.comparison import (
    DEFAULT_ALPHA,
    DEFAULT_EFFECT_SIZE_THRESHOLD,
    ComparisonResult,
    paired_comparison,
)

__all__ = [
    "ComparisonResult",
    "paired_comparison",
    "DEFAULT_ALPHA",
    "DEFAULT_EFFECT_SIZE_THRESHOLD",
]
