"""HTML summary report generator."""

from pathlib import Path

import pandas as pd


def generate_summary(results_path: Path, output_path: Path) -> None:
    """
    Generate HTML summary report from evaluation results CSV.

    Aggregates pass rates per pillar and displays metric score distributions.

    Args:
        results_path: Path to CSV file with evaluation results.
        output_path: Path where HTML report should be written.

    """
    # Load results
    df = pd.read_csv(results_path)

    # Calculate pass rates
    total_count = len(df)
    pass_count = len(df[df["label"] == "pass"])
    _ = len(df[df["label"] == "fail"])  # fail_count (not used)
    _ = len(df[df["label"] == "error"])  # error_count (not used)

    pass_rate = (pass_count / total_count * 100) if total_count > 0 else 0

    # Calculate score statistics
    score_mean = df["score"].mean() if not df["score"].isna().all().all() else 0
    score_min = df["score"].min() if not df["score"].isna().all().all() else 0
    score_max = df["score"].max() if not df["score"].isna().all().all() else 0

    # Group by question_id for detailed stats
    question_stats = (
        df.groupby("question_id")
        .agg(
            {
                "score": ["mean", "min", "max", "count"],
                "label": lambda x: (x == "pass").sum(),
            }
        )
        .reset_index()
    )
    question_stats.columns = [
        "question_id",
        "score_mean",
        "score_min",
        "score_max",
        "count",
        "pass_count",
    ]

    # Determine stat class based on pass rate
    if pass_rate > 0:
        stat_class = "pass" if pass_rate >= 80 else "fail"
    else:
        stat_class = ""

    # Generate HTML
    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Evaluation Summary</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; }}
        h1 {{ color: #333; }}
        .summary {{ display: flex; gap: 20px; margin: 20px 0; }}
        .stat-box {{ background: #f5f5f5; padding: 20px; border-radius: 8px; flex: 1; }}
        .stat-value {{ font-size: 32px; font-weight: bold; }}
        .stat-label {{ color: #666; }}
        .pass {{ color: green; }}
        .fail {{ color: red; }}
        table {{ width: 100%; border-collapse: collapse; margin: 20px 0; }}
        th, td {{ padding: 12px; text-align: left; border-bottom: 1px solid #ddd; }}
        th {{ background: #f5f5f5; }}
    </style>
</head>
<body>
    <h1>Evaluation Summary Report</h1>

    <div class="summary">
        <div class="stat-box">
            <div class="stat-value {stat_class}">{pass_rate:.1f}%</div>
            <div class="stat-label">Pass Rate</div>
        </div>
        <div class="stat-box">
            <div class="stat-value">{total_count}</div>
            <div class="stat-label">Total Evaluations</div>
        </div>
        <div class="stat-box">
            <div class="stat-value">{score_mean:.2f}</div>
            <div class="stat-label">Mean Score</div>
        </div>
    </div>

    <h2>Detailed Results by Question</h2>
    <table>
        <thead>
            <tr>
                <th>Question ID</th>
                <th>Count</th>
                <th>Pass Count</th>
                <th>Mean Score</th>
                <th>Min Score</th>
                <th>Max Score</th>
            </tr>
        </thead>
        <tbody>
"""

    for _, row in question_stats.iterrows():
        html += f"""
            <tr>
                <td>{row["question_id"]}</td>
                <td>{int(row["count"])}</td>
                <td>{int(row["pass_count"])}</td>
                <td>{row["score_mean"]:.3f}</td>
                <td>{row["score_min"]:.3f}</td>
                <td>{row["score_max"]:.3f}</td>
            </tr>
"""

    html += f"""
        </tbody>
    </table>

    <h2>Score Distribution</h2>
    <p>Min Score: {score_min:.3f} | Max Score: {score_max:.3f}</p>
</body>
</html>
"""

    # Write HTML file
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html)
