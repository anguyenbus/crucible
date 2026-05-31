"""
Crucible: RAG evaluation and replay testing framework.

This package provides:
- Dataset loaders for Legal RAG Bench
- DeepEval metrics for RAG quality (faithfulness, contextual precision/recall, answer relevance)
- ChromaDB and Zvec vector backends for retrieval
- Phoenix observability integration with OpenInference
- Replay testing for production traffic validation
- CLI entry points for running evaluations and serving candidate services

Typical usage:
    uv run crucible eval-rag --slice pico --rag stub-local --top_k 5
    uv run crucible generate-spans --limit 10
    uv run crucible eval-replay --candidate-spec configs/candidates/zvec.yaml --production-baseline
    uv run crucible serve --config configs/candidates/zvec.yaml --port 8082
    uv run crucible check phoenix
    uv run crucible check config
"""

import os

# ====================================================================
# SECURITY: DISABLE THIRD-PARTY TELEMETRY (executed on package import)
# ====================================================================
# DO NOT REMOVE OR MODIFY. Applies to ALL uses of this package.
#
# This disables DeepEval telemetry (analytics, usage stats) globally.
# Setting it here ensures it's applied before any DeepEval code runs.
#
# Why: Privacy, security, compliance, cost. See .env.example for details.
# Reference: https://docs.confident-ai.com/docs/telemetry-opt-out
# ====================================================================
os.environ["DEEPEVAL_TELEMETRY_OPT_OUT"] = "YES"

__version__ = "0.1.0"
