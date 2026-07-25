# Ingest-time corpus-poisoning demo (`/check/chunks`)

Demonstrates the guardrail stopping a poisoned document **at the ingest gate**, per
chunk, and rejecting the whole document with forensic attribution — the refined
Requirement 3 behaviour (2026-07-25).

## What's here

| File | What it is |
|---|---|
| `generate_demo_pdfs.py` | Builds the two demo PDFs (ephemeral `reportlab`, no project dep). |
| `mock_investigation.pdf` | A **clean, clearly-fictional** case document. Watermarked `SAMPLE — SYNTHETIC`; it is **not** a real law-enforcement record. |
| `poisoned_complementary.pdf` | A "complementary document" seeded with many injections (DAN, IGNORE ALL PREVIOUS INSTRUCTIONS, `SYSTEM:` headers, AI-directive text, a zero-width payload). |
| `demo_check_chunks.py` | Parses → chunks → runs `/check/chunks`; prints the reject + per-chunk forensics. |

Regenerate the PDFs:
```
cd services/guardrail && uv run --with reportlab python dev/demo_corpus/generate_demo_pdfs.py
```

## Run the demo

**In-process** (no pod needed — same `app.chunk_scan` the pod runs):
```
cd services/guardrail && uv run --with pypdf python dev/demo_corpus/demo_check_chunks.py
```

**Against a live pod** (proves the HTTP contract the ingestion team consumes):
```
make guardrail-pod        # in services/webui/frontend, serves :8080
cd services/guardrail && uv run --with pypdf --with httpx \
    python dev/demo_corpus/demo_check_chunks.py --url http://127.0.0.1:8080
```

Expected: `mock_investigation.pdf → SAFE`; `poisoned_complementary.pdf → UNSAFE`,
rejected whole, with ~15 forensic findings (category, label, severity, char span,
escaped excerpt) — the "which part is not safe" alert.

## The webui demo needs one cross-team change (ingestion)

The pod endpoint (`POST /check/chunks`) is **built, tested, and pinned** — that half
is ours. Showing the rejection **through the WebUI upload flow** needs the
**ingestion service** (another team's) to call it. That wiring is the contract ask:

1. After chunking, before embed/index, `POST /check/chunks` with
   `{document_id, source_ref, chunks:[{id, ordinal, text}]}`.
2. If `safe == false`: **index nothing**, return `422 document_rejected` carrying
   `results[]` (the per-chunk forensic detections), and surface it to the BFF/UI as
   "this document is not safe to ingest — chunk N contains a prompt injection".
3. If `safe == true`: embed + index as normal.
4. If the pod returns `503` (scanner failed to compile): fail **closed** — do not
   index unscanned.

Contract: [`docs/guardrails/guardrail-openapi.yaml`](../../../../docs/guardrails/guardrail-openapi.yaml)
(`/check/chunks`, `CheckChunksRequest` / `CheckChunksResponse`). Flow + diagram:
[`docs/guardrails/guardrail-topology.md`](../../../../docs/guardrails/guardrail-topology.md) §3.

Until that lands, `demo_check_chunks.py` is the runnable proof of the gate.
