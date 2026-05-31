"""Regression checking against baseline results."""

import json
from pathlib import Path


def check_regression(
    current_results: Path, baseline_path: Path, threshold: float = 0.05
) -> None:
    """
    Compare current results against baseline and check for regressions.

    Fails if any blocker-severity metric has regressed beyond threshold.

    Args:
        current_results: Path to JSON file with current results.
        baseline_path: Path to JSON file with baseline results.
        threshold: Regression threshold (default 5% relative decrease).

    Raises:
        RuntimeError: If blocker regression is detected.
        FileNotFoundError: If either file doesn't exist.

    """
    # Load files
    if not current_results.exists():
        raise FileNotFoundError(f"Current results not found: {current_results}")
    if not baseline_path.exists():
        raise FileNotFoundError(f"Baseline not found: {baseline_path}")

    with open(current_results) as f:
        current = json.load(f)
    with open(baseline_path) as f:
        baseline = json.load(f)

    # Check for regressions
    regressions = []

    for metric_name, current_data in current.get("metrics", {}).items():
        if metric_name not in baseline.get("metrics", {}):
            continue

        baseline_data = baseline["metrics"][metric_name]

        # Only check blocker severity
        if baseline_data.get("severity") != "blocker":
            continue

        current_score = current_data.get("score", 0)
        baseline_score = baseline_data.get("score", 0)

        # Check for regression (score decreased)
        if baseline_score > 0:
            relative_change = (current_score - baseline_score) / baseline_score
            if relative_change < -threshold:
                regressions.append(
                    {
                        "metric": metric_name,
                        "baseline": baseline_score,
                        "current": current_score,
                        "change": relative_change * 100,
                    }
                )

    if regressions:
        msg = "Regression detected:\n"
        for reg in regressions:
            metric = reg["metric"]
            baseline = reg["baseline"]
            current = reg["current"]
            change = reg["change"]
            msg += f"  - {metric}: {baseline:.3f} -> {current:.3f} ({change:.1f}%)\n"
        raise RuntimeError(msg)
