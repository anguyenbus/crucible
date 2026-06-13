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

# NOTE: Phase 4 removed the import-time DEEPEVAL_TELEMETRY_OPT_OUT write that
# used to live here. `import crucible` pulls NO deepeval (deepeval is not in
# sys.modules afterward), so this top-level write opened no ordering gap and
# violated the "import mutates nothing" invariant. The opt-out now lives in
# exactly two allowlisted package __init__ sites: crucible.kernel.rag_metrics
# and crucible.service.deepeval -- each set before its package imports deepeval.

__version__ = "0.1.0"
