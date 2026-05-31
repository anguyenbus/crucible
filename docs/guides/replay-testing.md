# Replay Testing Guide

This guide shows how to use crucible replay testing to validate candidate services against production baselines.

## Prerequisites

1. Install dependencies:
   ```bash
   uv sync --all-extras --dev
   ```

2. Ensure Phoenix is running with baseline traces:
   ```bash
   docker-compose -f docker-compose.yml -f docker-compose.observability.yml up
   ```

## Quick Start

### Generate baseline spans

```bash
uv run crucible generate-spans --limit 10 --slice nano
```

### Evaluate candidate service

```bash
uv run crucible eval-replay \
    --candidate-spec configs/candidates/zvec.yaml \
    --production-baseline default
```

## Options

- `--candidate-spec`: Path to candidate service specification YAML
- `--production-baseline`: Phoenix project name for production traces
- `--output-dir`: Custom output directory for comparison results
- `--config`: Path to eval_config.yaml (default: eval_config.yaml)

## Candidate Service Configuration

Create a YAML file defining your candidate service:

```yaml
# configs/candidates/zvec.yaml
name: zvec-candidate
endpoint: http://localhost:8082
headers:
  Content-Type: application/json
timeout: 30
```

## Serve Candidate Service

To serve a candidate service locally:

```bash
uv run crucible serve --config configs/candidates/zvec.yaml --port 8082
```

## Statistical Tests

Replay testing performs statistical comparisons:
- Wilcoxon signed-rank test for paired differences
- Cliff's Delta for effect size
- Pass/fail based on significance thresholds

## Results

Results include:
- Statistical comparison summary
- Per-query differences
- Significance test results
- Effect size measurements
