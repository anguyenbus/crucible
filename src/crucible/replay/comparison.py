"""
Comparison logic for paired statistical analysis.

This module provides statistical comparison functions for candidate
versus baseline adapter evaluation using Wilcoxon signed-rank test
and effect size calculations.
"""

from __future__ import annotations

from dataclasses import dataclass

from beartype import beartype
from beartype.typing import Final

# Constants
DEFAULT_ALPHA: Final[float] = 0.05

# Cliff's Delta effect size thresholds:
# - negligible: < 0.15
# - small: 0.15 - 0.33
# - medium: 0.33 - 0.47
# - large: >= 0.47
#
# DEFAULT_EFFECT_SIZE_THRESHOLD set to 0.15 (small) for practical significance.
# Adjust based on your use case:
# - 0.15: Detect small but meaningful improvements
# - 0.33: Only care about medium+ effects
# - 0.47: Only care about large effects
DEFAULT_EFFECT_SIZE_THRESHOLD: Final[float] = 0.15


@beartype
@dataclass(frozen=True)
class ComparisonResult:
    """
    Result of paired statistical comparison.

    Attributes:
        statistic: Test statistic value.
        p_value: P-value from statistical test.
        effect_size: Effect size (Cliff's Delta or Cohen's D).
        pass_fail: Whether the comparison passes the threshold.
        winner: Which adapter won ("candidate", "baseline", or "tie").
        candidate_error_rate: Error rate for candidate (0-1).
        baseline_error_rate: Error rate for baseline (0-1).
        error_rate_pass_fail: Whether error rate comparison passes.
        overall_pass_fail: Combined pass/fail considering both scores and errors.

    """

    statistic: float
    p_value: float
    effect_size: float
    pass_fail: bool
    winner: str
    candidate_error_rate: float = 0.0
    baseline_error_rate: float = 0.0
    error_rate_pass_fail: bool = True
    overall_pass_fail: bool = True


@beartype
def paired_comparison(
    candidate_scores: list[float],
    baseline_scores: list[float],
    alpha: float = DEFAULT_ALPHA,
    effect_size_threshold: float = DEFAULT_EFFECT_SIZE_THRESHOLD,
    candidate_errors: int = 0,
    baseline_errors: int = 0,
    total_questions: int = 0,
    max_error_rate_delta: float = 0.10,  # 10% increase in error rate is failure
) -> ComparisonResult:
    """
    Perform paired statistical comparison with error rate gating.

    Uses Wilcoxon signed-rank test for paired comparison and
    computes Cliff's Delta effect size. Also compares error rates
    to prevent survivor bias in results.

    Args:
        candidate_scores: Scores from candidate adapter.
        baseline_scores: Scores from baseline adapter.
        alpha: Significance threshold for p-value (default: 0.05).
        effect_size_threshold: Threshold for meaningful effect size.
            Default: 0.15 (small effect per Cliff's Delta).
            Options: 0.15 (small), 0.33 (medium), 0.47 (large).
        candidate_errors: Number of errors for candidate.
        baseline_errors: Number of errors for baseline.
        total_questions: Total number of questions attempted.
        max_error_rate_delta: Maximum allowed increase in error rate.
            Default: 0.10 (10% increase fails the gate).

    Returns:
        ComparisonResult with test statistics and determination.
        - pass_fail: True if p_value < alpha AND |effect_size| >= threshold
        - winner: "candidate" if effect_size > 0, "baseline" if < 0, "tie" if near 0
        - candidate_error_rate: Error rate for candidate (0-1).
        - baseline_error_rate: Error rate for baseline (0-1).
        - error_rate_pass_fail: False if candidate error rate significantly higher.
        - overall_pass_fail: Combined pass/fail (scores AND error rate).

    Raises:
        ValueError: If score lists have different lengths.

    """
    if len(candidate_scores) != len(baseline_scores):
        raise ValueError("Score lists must have the same length")

    # Calculate error rates
    successful_questions = len(candidate_scores)
    candidate_total = successful_questions + candidate_errors
    baseline_total = successful_questions + baseline_errors

    candidate_error_rate = (
        candidate_errors / candidate_total if candidate_total > 0 else 0.0
    )
    baseline_error_rate = (
        baseline_errors / baseline_total if baseline_total > 0 else 0.0
    )

    # Error rate comparison: candidate must not have significantly higher error rate
    error_rate_delta = candidate_error_rate - baseline_error_rate
    error_rate_pass_fail = error_rate_delta <= max_error_rate_delta

    if len(candidate_scores) == 0:
        return ComparisonResult(
            statistic=0.0,
            p_value=1.0,
            effect_size=0.0,
            pass_fail=False,
            winner="tie",
            candidate_error_rate=candidate_error_rate,
            baseline_error_rate=baseline_error_rate,
            error_rate_pass_fail=error_rate_pass_fail,
            overall_pass_fail=False,
        )

    # Compute Wilcoxon signed-rank test
    statistic, p_value = _wilcoxon_test(candidate_scores, baseline_scores)

    # Compute effect size (Cliff's Delta)
    effect_size = _cliffs_delta(candidate_scores, baseline_scores)

    # Determine winner based on scores
    if abs(effect_size) < effect_size_threshold:
        winner = "tie"
    elif effect_size > 0:
        winner = "candidate"
    else:
        winner = "baseline"

    # Determine pass/fail for scores
    score_pass_fail = p_value < alpha and abs(effect_size) >= effect_size_threshold

    # Overall pass/fail: both scores AND error rate must pass
    overall_pass_fail = score_pass_fail and error_rate_pass_fail

    return ComparisonResult(
        statistic=statistic,
        p_value=p_value,
        effect_size=effect_size,
        pass_fail=score_pass_fail,
        winner=winner,
        candidate_error_rate=candidate_error_rate,
        baseline_error_rate=baseline_error_rate,
        error_rate_pass_fail=error_rate_pass_fail,
        overall_pass_fail=overall_pass_fail,
    )


@beartype
def _wilcoxon_test(
    candidate_scores: list[float],
    baseline_scores: list[float],
) -> tuple[float, float]:
    """
    Perform Wilcoxon signed-rank test.

    Args:
        candidate_scores: Scores from candidate adapter.
        baseline_scores: Scores from baseline adapter.

    Returns:
        Tuple of (test_statistic, p_value).

    """
    try:
        from scipy.stats import wilcoxon

        statistic, p_value = wilcoxon(candidate_scores, baseline_scores)
        return float(statistic), float(p_value)
    except ImportError:
        # Fallback: simple sign test if scipy not available
        n = len(candidate_scores)
        n_positive = sum(
            1 for c, b in zip(candidate_scores, baseline_scores, strict=True) if c > b
        )
        n_negative = sum(
            1 for c, b in zip(candidate_scores, baseline_scores, strict=True) if c < b
        )

        # Simple approximation
        statistic = min(n_positive, n_negative)
        p_value = 2.0 ** (-abs(n_positive - n_negative) - 1) if n > 0 else 1.0

        return float(statistic), float(p_value)


@beartype
def _cliffs_delta(
    candidate_scores: list[float],
    baseline_scores: list[float],
) -> float:
    """
    Compute Cliff's Delta effect size.

    Cliff's Delta measures the degree of overlap between two distributions.
    Values range from -1 to 1, where:
    - 1 indicates all candidate scores > all baseline scores
    - -1 indicates all candidate scores < all baseline scores
    - 0 indicates complete overlap

    Args:
        candidate_scores: Scores from candidate adapter.
        baseline_scores: Scores from baseline adapter.

    Returns:
        Cliff's Delta effect size.

    """
    n1 = len(candidate_scores)
    n2 = len(baseline_scores)

    if n1 == 0 or n2 == 0:
        return 0.0

    # Count comparisons
    greater = 0
    less = 0

    for c in candidate_scores:
        for b in baseline_scores:
            if c > b:
                greater += 1
            elif c < b:
                less += 1

    # Compute Cliff's Delta
    total = n1 * n2
    delta = (greater - less) / total if total > 0 else 0.0

    return delta
