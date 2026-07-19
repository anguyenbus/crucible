# Parser Service

FastAPI microservice that parses documents (PDF digital + scanned, images,
DOCX, XLSX, HTML) into clean Markdown using the vendored `parser_service`
package: **Docling on CPU** with **per-page escalation to AWS Textract by
default** (VLM / Bedrock Claude selectable via env). Ingestion calls it
HTTP-only over `INGESTION_PARSER_URL` and NEVER imports it — the same dependency
firewall the BFF has to ingestion, which keeps ingestion's venv and CI
torch-free.

The parser is **S3-IAM-free**: it receives raw document bytes over the wire and
writes them to a context-managed tempfile for Docling's `Path` API (no disk leak
on crash). Fetch ownership and the 404/400 source-error mapping stay in
ingestion.

API surface:

| Endpoint | Purpose |
| --- | --- |
| `POST /parse` | Synchronous drain: bytes + filename → `{markdown, confidence, page_count, page_routes, call_counts, warnings}`. Mirrors ingestion's `POST /ingest` drain shape. |
| `POST /parse/stream` | SSE, one frame per page then exactly one terminal `done`/`error` frame (Task Group 6 — severable; may be deferred). |
| `GET /healthz` | Liveness check (mirrors ingestion). |

Error contract (the `parse_runner` translation seam, single source of truth):
non-empty markdown → **200** (+ loud `warnings` on any Docling fallback); empty
markdown OR every page errored → **422**; over page cap or size cap → **413**;
unsupported / unknown type → **400**; all escalation calls failed with a
TRANSIENT error AND those pages are the majority → **502** (retryable).
Escalation unreachable but Docling produced usable markdown → **200**.
Confidence is uncalibrated: it NEVER gates ingest and is NEVER part of any hash.

## Setup

Requires Python `>=3.11` (the reference's proven wheel resolution — deliberately
NOT monorepo-3.12) and [uv](https://docs.astral.sh/uv/).

```bash
cd services/parser
uv sync
```

The Docker image bakes the Docling **layout + table** and **rapidocr** OCR
models at build time so the runtime serves fully offline (`HF_HUB_OFFLINE=1`).
Verify the bake with `scripts/run_offline_smoke.sh` (a network-blocked
offline-startup smoke test, not merely "a model file was copied").

AWS access uses the standard boto3 credential chain. Textract (the default
escalation engine) needs `textract:AnalyzeDocument`; VLM escalation additionally
needs Bedrock `InvokeModel`. Region defaults to `ap-southeast-2`.

## Configuration

Service-owned settings use the `PARSER_` env prefix (per-service prefix
convention, mirroring `INGESTION_`); standard AWS vars stay bare. Defined in
`app/config.py`:

| Env var | Default | Purpose |
| --- | --- | --- |
| `PARSER_ESCALATION_ENGINE` | `textract` | Escalation engine for gate-promoted pages (`textract` or `vlm`) |
| `PARSER_VLM_MODEL` | `au.anthropic.claude-sonnet-4-6` | VLM model (used only when engine=`vlm`) |
| `PARSER_MAX_PAGES` | `100` | Page cap; over → 413. Page cap × concurrency IS the POC spend bound |
| `PARSER_MAX_INPUT_MB` | `50` | Input-size cap (MB); over → 413 |
| `PARSER_BUDGET_USD` | _(disabled)_ | Per-request advisory spend guard. DISABLED by default; NEVER gates |
| `PARSER_IDLE_TIMEOUT_SECONDS` | `120` | Idle timeout between per-page frames (NOT a wall-clock total) |
| `PARSER_SCAN_FASTPATH` | `off` | Opt-in perf/cost knob — see rollout note below |
| `PARSER_ESCALATION_ARBITRATION` | `off` | Opt-in toggle (byte-identical-to-baseline when off) |
| `AWS_REGION` | `ap-southeast-2` | Standard AWS var — deliberately unprefixed |

Concurrency is **1 in-flight parse per pod** (torch/docling saturate the cores);
scale horizontally by replicas only.

## Running the service

```bash
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

In compose the parser is reached at `http://parser:8000` on the internal
network. See the **security invariant** below before exposing it anywhere.

## Security invariant — internal-only / east-west, NEVER ingress-exposed

The parser accepts arbitrary bytes and bills Textract per page, so an exposed
instance is a **cost-bomb / DoS**. It has **no app-level auth**, which is
acceptable **ONLY because it is east-west**: network isolation (the internal
compose network here; VPC + security groups in prod) is the entire control.

Concretely, in `docker-compose.yml` the `parser` service publishes **NO host
port** — it uses `expose:` only and is reachable solely by other services on the
compose network. Do NOT add a `ports:` mapping. This is the deliberate contrast
with user-facing / east-west entry services that do publish a host port.

---

## Rollout note (ops)

> **Where this lives:** here, in the service README, rather than appended to
> `docs/parser-service-plan.md`. The README is the more discoverable home for an
> operator standing up or debugging the running service; the plan doc is the
> design record.

### One-time re-ingest of every existing PDF (expected)

Switching extraction from `pypdf` to Docling markdown **changes the extracted
bytes** for existing PDFs. The post-parse normalized-text dedup keys on that
content, so every existing PDF **re-ingests exactly once** after this rollout.
This is expected and one-time.

Be precise about why the raw-bytes gate does NOT prevent this: the pre-parse
**raw-bytes SHA-256 gate short-circuits only IDENTICAL RAW input bytes** (the
same file uploaded again). The Docling-vs-pypdf change is a change to the
*parsed output*, not the raw input, so the two are **independent** — the raw
gate cannot and does not suppress the one-time re-ingest. It only stops paying
for identical *re-uploads* (see below).

### The raw-bytes gate short-circuits identical re-uploads before any cost

Immediately after fetch — BEFORE the parser round-trip — ingestion computes
`raw_sha256` over the raw document bytes and does a cheap OpenSearch lookup keyed
on that hash (independent of `doc_id`). On a hit it skips the **entire parse +
embed cost**. `raw_sha256` is stamped onto indexed chunk metadata so the next
upload has something to look up. This is what changes the cost profile: an
identical re-upload never pays for Docling/Textract again.

### Why Textract is the default (and the honest tradeoff)

Textract is **deterministic**: the same page yields the same text, so escalated
documents dedup predictably. The VLM engine is **non-deterministic**, which
makes dedup of escalating documents best-effort. Determinism → dedup
predictability is why Textract is the default.

The honest tradeoff: per the reference benchmark, **Textract loses on scanned
FORMS by roughly 2x to the VLM** (form structure/layout fidelity). If your
corpus is form-heavy and fidelity matters more than dedup predictability, flip
`PARSER_ESCALATION_ENGINE=vlm` — knowing you trade away deterministic dedup.

### `PARSER_SCAN_FASTPATH` — the first perf/cost knob to flip

Default is **OFF for v1 parity — but "OFF knowing why."** With Textract-default
escalation, running the full Docling pass on an **all-scanned** document is
**wasted work**: Textract replaces Docling's OCR on those pages anyway. Once the
corpus is confirmed scan-heavy, `PARSER_SCAN_FASTPATH` is the **first perf/cost
knob to flip** to skip that redundant Docling pass. It stays OFF until you have
that evidence, not by reflex.

### Provisional Textract price — LOUD note

The cost summary uses a **PROVISIONAL** Textract price of **$0.019/page**. This
is advisory-only and NEVER gates parsing. **Verifying the live `ap-southeast-2`
LAYOUT+TABLES per-page price is an ops follow-up, NOT a blocker** for shipping
v1. Do not trust the dollar figures until that verification lands.

### East-west-only invariant (repeat)

The parser is **internal-only / east-west, NEVER ingress-exposed**, no app-level
auth (acceptable only because east-west). In compose it publishes no host port.
See the security-invariant section above.
