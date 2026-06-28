"""Test-first (RED) coverage for the kernel comparison stats.

These tests target the FUTURE kernel path
``crucible.kernel.replay_stats.comparison`` (Commit 3 moves the module there).
Until that move lands they fail at import/collection time. Fixtures are
deterministic with hand-verified expected values (NO hypothesis).

Cliff's Delta is computed by hand:
- candidate strictly greater than baseline on every pair -> delta == 1.0.
- candidate equal to baseline on every pair -> delta == 0.0 (zero-delta / tie).
- partial-overlap fixture cand=[3,4,5] vs base=[1,2,6]: greater=6, less=3,
  total=9 -> (6 - 3) / 9 == 1/3.

Wilcoxon expected statistics were produced with scipy.stats.wilcoxon directly:
- cand=[0.6,0.7,0.8,0.9,1.0] vs base=[0.5,0.6,0.7,0.8,0.9] -> (0.0, 0.0625).
"""

from __future__ import annotations

import math

from crucible.kernel.replay_stats.comparison import (
    DEFAULT_ALPHA,
    DEFAULT_EFFECT_SIZE_THRESHOLD,
    ComparisonResult,
    paired_comparison,
)


def test_wilcoxon_path_matches_known_fixture() -> None:
    """The Wilcoxon statistic/p-value match scipy on a known fixture."""
    candidate = [0.6, 0.7, 0.8, 0.9, 1.0]
    baseline = [0.5, 0.6, 0.7, 0.8, 0.9]

    result = paired_comparison(candidate, baseline)

    assert isinstance(result, ComparisonResult)
    # Verified directly against scipy.stats.wilcoxon.
    assert math.isclose(result.statistic, 0.0, abs_tol=1e-9)
    assert math.isclose(result.p_value, 0.0625, abs_tol=1e-9)


def test_cliffs_delta_all_greater_is_one() -> None:
    """Candidate strictly greater on every pair yields Cliff's Delta == 1.0."""
    candidate = [1.0, 1.0, 1.0]
    baseline = [0.0, 0.0, 0.0]

    result = paired_comparison(candidate, baseline)

    assert math.isclose(result.effect_size, 1.0, abs_tol=1e-9)
    assert result.winner == "candidate"


def test_cliffs_delta_partial_overlap_is_one_third() -> None:
    """Partial-overlap fixture yields the hand-computed delta of 1/3."""
    candidate = [3.0, 4.0, 5.0]
    baseline = [1.0, 2.0, 6.0]

    result = paired_comparison(candidate, baseline)

    assert math.isclose(result.effect_size, 1.0 / 3.0, abs_tol=1e-9)


def test_zero_delta_tie_when_scores_equal() -> None:
    """Identical scores produce zero effect size and a 'tie' winner."""
    candidate = [0.5, 0.5, 0.5]
    baseline = [0.5, 0.5, 0.5]

    result = paired_comparison(candidate, baseline)

    assert math.isclose(result.effect_size, 0.0, abs_tol=1e-9)
    assert result.winner == "tie"
    # A zero/negligible effect cannot pass the practical-significance gate.
    assert result.pass_fail is False


def test_constants_have_expected_defaults() -> None:
    """The kernel re-exposes the documented default thresholds."""
    assert DEFAULT_ALPHA == 0.05
    assert DEFAULT_EFFECT_SIZE_THRESHOLD == 0.15
