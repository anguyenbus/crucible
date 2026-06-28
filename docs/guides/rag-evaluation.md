# RAG Evaluation Guide

This guide shows how to use crucible to evaluate RAG systems on the Legal RAG Bench dataset.

## Prerequisites

1. Install dependencies:
   ```bash
   uv sync --all-extras --dev
   ```

2. Configure AWS Bedrock access for the DeepEval judge (credential chain only):
   ```bash
   export AWS_REGION=ap-southeast-2   # must match your inference-profile geography
   # AWS credentials come from the standard chain (env / profile / role)
   ```

3. Set up HuggingFace token (required for Legal RAG Bench dataset):
   ```bash
   export HF_TOKEN=your-token-here
   # or create ~/.huggingface/token with your token
   ```

## Quick Start

### Evaluate with pico slice (2 queries)

```bash
uv run crucible eval-rag --slice pico --rag stub-local
```

### Evaluate with nano slice (10 queries)

```bash
uv run crucible eval-rag --slice nano --rag stub-local
```

### Evaluate with full dataset (100 queries)

```bash
uv run crucible eval-rag --slice full --rag stub-local
```

## Options

- `--slice`: Dataset slice (`pico`, `nano`, `full`)
- `--rag`: RAG system to use (`stub-local` for ChromaDB-backed reference)
- `--top-k`: Number of chunks to retrieve (default: 5)
- `--force-reingest`: Force re-ingestion of corpus into ChromaDB
- `--output-dir`: Custom output directory for results
- `--config`: Path to eval_config.yaml (default: eval_config.yaml)

## With Phoenix Observability

To enable Phoenix tracing:

1. Start Phoenix:
   ```bash
   docker-compose -f docker-compose.yml -f docker-compose.observability.yml up
   ```

2. Run evaluation with Phoenix:
   ```bash
   export PHOENIX_ENDPOINT=http://localhost:6006
   uv run crucible eval-rag --slice pico --rag stub-local --enable-phoenix
   ```

3. View traces at http://localhost:6006

## Results

Results are saved to `results/eval_rag/TIMESTAMP/results.csv` with columns:
- query_id
- question
- gold_answer
- generated_answer
- relevant_passage_retrieved
- faithfulness_score
- context_precision_score
- context_recall_score
- answer_relevancy_score
- judge_verdict
- total_ms
- error

## Verification

After running evaluation, verify:
1. CSV file was created in output directory
2. All queries show either PASS or NEEDS_REVIEW verdict
3. Error column is empty for successful evaluations
4. Phoenix traces show complete span hierarchy (if enabled)
