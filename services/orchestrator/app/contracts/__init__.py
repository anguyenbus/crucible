"""
Packaged JSON Schema contracts for the orchestrator service.

This package holds the orchestrator's MIRROR of eval's canonical
``rag_query_output.schema.json``
(``services/eval/app/contracts/rag_query_output.schema.json``). Direction of
truth: eval's copy is CANONICAL; this one is a byte-for-byte mirror kept in
sync by a byte-equality test — copy from eval's, never edit locally.

It exists as an explicit package so validation can resolve the schema via
``importlib.resources.files("app.contracts")`` — the run-from-anywhere anchor
that works in BOTH the source tree and an installed wheel (no CWD-relative
paths, no path dependency on eval).
"""
