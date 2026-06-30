"""
Packaged JSON Schema contracts for the eval service.

This package is the single source of truth for the canonical schema contracts
(``parser_output``, ``rag_query_output``, ``eval_questions``,
``legal_rag_bench_query_output``). It exists as an explicit package so the
kernel ``schema_validator`` can resolve schemas via
``importlib.resources.files("app.contracts")`` -- the run-from-anywhere
anchor that works in BOTH the source tree (``PYTHONPATH=src``) and an installed
wheel (Finding A). The top-level ``contracts/`` duplicate was removed in Phase 5;
the migration-day move to ``genai-backend/contracts/schemas/`` sources from here.
"""
