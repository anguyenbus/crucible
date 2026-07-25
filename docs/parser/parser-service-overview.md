# Parser Service — overview

**Status:** as-built · **Scope:** what the parser service is, the pipeline it runs, and the guarantees
it makes. **Companion:** [parser-topology.md](parser-topology.md) (how ingestion calls it and where the
parser / guardrail split falls) · contract [parser-openapi.yaml](parser-openapi.yaml) (internal, east-west).

---

## 1. What it is

A small, **out-of-process** service that turns **document bytes into one clean, RAG-ready Markdown
string** plus routing telemetry. It runs **Docling on CPU** with **per-page escalation to AWS Textract by
default** (a Bedrock Claude VLM is selectable via env). Ingestion calls it over HTTP and **never imports
it**; the parser owns the heavy `docling` + `torch` + `rapidocr` stack so ingestion's venv and CI stay
torch-free.

| Property | Value |
|---|---|
| Framework | FastAPI, Python **3.11** (the reference's proven docling/torch/rapidocr wheel resolution — deliberately NOT monorepo-3.12) |
| Extraction | **Docling** (layout + table models) on CPU, a parser-owned per-page Markdown renderer, a two-layer quality gate, and per-page escalation |
| Escalation engine | **AWS Textract** (`analyze_document`, `LAYOUT`+`TABLES`) by default; **Bedrock Claude VLM** (`au.anthropic.claude-sonnet-4-6`) selectable via `PARSER_ESCALATION_ENGINE=vlm` |
| S3-IAM-free | Receives raw bytes over the wire, writes them to a **context-managed tempfile** for Docling's `Path` API (no disk leak on crash). Fetch ownership + source-error mapping stay in ingestion |
| Never-raises core | The vendored `parse_to_markdown` **never raises** — every failure lands in `warnings[]` / `page_routes`; a thin runner translates that into the HTTP status table |
| Offline runtime | The Docker image **bakes** the Docling + rapidocr models at build time, so the pod serves with networking disabled (`HF_HUB_OFFLINE=1`) |
| Internal-only | East-west, **never ingress-exposed**, no app-level auth — an exposed parser billing Textract per page is a cost-bomb / DoS. In compose it publishes **no host port** |

The pod is deliberately **thin and stateless**: it owns no data, holds no tenant state, and is a pure
function of (its input bytes + filename + the `PARSER_*` config). All persistence — the index, the
raw-bytes dedup gate, the chunk metadata — lives in the **caller** (ingestion).

---

## 2. The pipeline

`parse_to_markdown(path)` (the vendored `parser_service.markdown_pipeline`) runs a **format-routed,
per-page-gated** pipeline. Format is classified by extension first, then MIME:

| Kind | Path |
|---|---|
| **PDF** | Docling once → group items into elements per 0-based page → render **each page** with the parser-owned `render_markdown` → **per-page quality gate** → keep Docling or escalate that page |
| **Image** (PNG/JPEG/TIFF/WebP) | Gated **one-page** path: Docling OCR → gate → escalate only when the gate fires (or Docling extracted nothing). NOT always-VLM |
| **DOCX / XLSX / HTML** | Whole-document `export_to_markdown()`, **gate skipped**, one logical page (flowing office/HTML formats have no reliable page geometry to gate per-page) |
| **Unknown** | Empty markdown + an `unsupported_type` warning → the runner maps this to **400** |

### 2.1 The parser-owned renderer (Route B)

The PDF/image paths do **not** use Docling's native `export_to_markdown`. They group Docling items into
elements per page and render each page with the parser's own `render_markdown`, which scores **~0.05 NID
higher** than Docling's dialect against verbatim-text gold on DP-Bench and OmniDocBench. Pages are joined
in page order with a plain `\n\n` — **no inline page markers** (they would inject tokens absent from
marker-free gold); page provenance lives entirely in `page_routes`.

### 2.2 The two-layer quality gate

Each PDF/image page is triaged by `quality_gate.evaluate_page` to decide **keep vs. promote**:

- **Layer 1 — Docling's own `ConfidenceReport`** (free; already computed). A `poor`/`fair` low- or
  mean-grade promotes the page.
- **Coverage check** — promotes when Docling extracted **< 30%** of the page's embedded text-layer
  tokens (catches silent under-extraction that still grades high-confidence — e.g. tables of contents).
- **Layer 2 — heuristic safety net** on the extracted text: garbled-token ratio, mean word length,
  content-token (dict) hit rate, repeated-character runs, printable-ASCII ratio. Catches
  confidently-wrong OCR. Number-dense pages (financial tables) are treated as valid content, not garble.

A page that fails any layer is **promoted to the escalation engine**; otherwise Docling's rendering is
kept. Tables are always escalated on the legacy `parse()` path (VLM table crops); on the markdown path
they ride the page decision.

### 2.3 Escalation (Textract default, VLM selectable)

A promoted page is rendered to a PNG (`pypdfium2`, DPI-bounded to a megapixel budget) and sent to the
selected engine. **Both engines return the same element-JSON shape**, so the downstream
`render_markdown` conversion is reused unchanged. On engine garbage (`{"error": …}`, non-list/empty
`elements`, or whitespace-only markdown) the page **falls back to the Docling rendering**. Every fallback
is **loud** in `warnings` (`N/M pages fell back to Docling`), never silent.

| Route (in `page_routes`) | Meaning |
|---|---|
| `docling-kept` | Gate kept Docling's page markdown |
| `textract` / `vlm` | The engine's markdown replaced the page |
| `textract-fallback-docling` / `vlm-fallback-docling` | Engine was called but returned nothing usable → gate-flagged Docling shipped (a `reason: "throttled"` marks an exhausted **transient** failure) |
| `textract-rejected-kept-docling` / `vlm-rejected-kept-docling` | Arbitration (opt-in) kept a clean Docling render over a successful-but-low-quality engine render |

Both escalation clients (`textract_client`, `vlm_client`) **never raise** — they return `{"error": …}`,
retry only transient throttles/5xx with bounded exponential backoff (`retry.py`), and tag an exhausted
transient failure with `error_kind="throttled"`. Successful-call counts are tracked per worker thread
(`threading.local`) so `call_counts` is race-free under concurrency.

### 2.4 Why Textract is the default

Textract is **deterministic**: the same page yields the same text, so escalated documents dedup
predictably against ingestion's raw-bytes gate. The VLM is **non-deterministic** (best-effort dedup). The
honest tradeoff: per the reference benchmark, Textract loses on **scanned forms** by ~2× to the VLM
(structure/layout fidelity). Flip `PARSER_ESCALATION_ENGINE=vlm` for a form-heavy corpus, knowing you
trade away deterministic dedup.

---

## 3. The error-translation seam (the HTTP status table)

`parse_to_markdown` never raises; `app/parse_runner.py` is the **single source of truth** that reconciles
that never-raises contract with ingestion's *refuse-to-index-nothing* contract. Ordering matters (the
document-level 413/400 guards run before the content-level 502/422 checks):

| Result | HTTP |
|---|---|
| Non-empty markdown | **200** (with any partial-degradation warnings riding along) |
| Empty markdown **or** every page errored (no usable content) | **422** — refuse to index nothing |
| Over the page cap **or** input-size cap | **413** |
| Unsupported / unknown type | **400** |
| **All** escalation calls failed **transiently** AND those pages are the **majority** of the document | **502** — retryable |
| Escalation unreachable but Docling produced usable markdown (failed pages a minority) | **200** |

The 502 "majority of content" is measured by **page count** — the honest proxy, since a transiently
failed page ships only whatever Docling salvaged (often little), so shipped `n_chars` under-represents
what the failure lost. This is the one best-effort part of the table; the transient-vs-hard
discrimination itself is a real signal (`reason == "throttled"`).

---

## 4. Confidence — advisory only, never gates, never hashed

`parse_to_markdown` emits a per-page and a content-weighted document `confidence` derived **entirely**
from `page_routes` (the route + the gate's quality-signal booleans). It is a **transparent,
routing-derived heuristic, NOT a gold-calibrated probability**.

- It **NEVER gates** parsing, routing, the quality gate, the promote/keep decision, or the shipped
  markdown.
- It is **NEVER part of any hash** (ingestion keys dedup on normalized text, not confidence).
- Pages below `LOW_CONFIDENCE_THRESHOLD` (0.65, a derived midpoint between the highest untrusted and
  lowest trusted tier — never a magic float that equals a tier) surface an **additive, advisory**
  `low_confidence_page` warning for operator review.

---

## 5. The endpoints

| Endpoint | Purpose |
|---|---|
| `POST /parse` | **Synchronous drain**: bytes + filename → `{markdown, confidence, page_count, page_routes, call_counts, warnings}`. Mirrors ingestion's `/ingest` drain shape |
| `POST /parse/stream` | **SSE**: a leading `parsing` frame, one `page` frame per page, then exactly one terminal `done`/`error` frame. HTTP is **200 for the whole stream** — a mid-stream failure arrives as a terminal `error` frame, never a status change after the headers |
| `GET /healthz` | Liveness (mirrors ingestion) |

**Streaming honesty (load-bearing).** The `page` frames are a **POST-HOC per-page manifest, not live
mid-parse progress.** `parse_to_markdown` is a single synchronous call and the vendored pipeline exposes
no page-level callback, so all `page` frames arrive in a burst **after** the opaque Docling parse
completes, derived by iterating the resulting `page_routes`. No sleeps, no interpolation, no fabricated
increments — the real long-pole (Docling) stays a single opaque span.

Input on both routes: raw bytes as **either** `multipart/form-data` (a `file` part) **or** a raw
`application/octet-stream` body, plus a `filename` (query param / the multipart part's filename /
`X-Filename` header). The extension drives format dispatch, so it must be preserved.

---

## 6. Concurrency and caps

- **One in-flight parse per pod** (`PARSER_MAX_CONCURRENT_PARSES=1`) — `torch`/`docling` saturate the
  cores. A single shared semaphore bounds concurrency across **both** endpoints; the blocking parse runs
  in a threadpool so the event loop stays free. **Scale horizontally by replicas**, not per-pod
  concurrency.
- **Page cap × concurrency IS the POC spend bound.** `PARSER_MAX_PAGES=100` (over → 413),
  `PARSER_MAX_INPUT_MB=50` (over → 413, stat-based before any read).
- **`PARSER_BUDGET_USD` is disabled by default** — a miscalibrated dollar meter that silently degrades
  quality is worse than none. When set it feeds an **advisory** cost summary only; it NEVER gates. The
  Textract price ($0.019/page) is **provisional** — verifying the live `ap-southeast-2` LAYOUT+TABLES
  price is an ops follow-up, not a ship blocker.
- Idle timeout (`PARSER_IDLE_TIMEOUT_SECONDS=120`) bounds the **gap between per-page frames**, NOT a
  wall-clock total — a legitimate 100-page parse runs ~100–190 min. Aligns with the caller's idle bound.

---

## 7. Invariants

- **Internal-only / east-west** — never ingress-exposed, no app-level auth; network isolation (compose
  network here, VPC + security groups in prod) is the *entire* control. In compose it publishes **no host
  port** (`expose:` only). Do not add a `ports:` mapping.
- **S3-IAM-free** — bytes over the wire, never a fetch. Fetch + the 404/400 source-error mapping stay in
  ingestion.
- **Never-raises core** — `parse_to_markdown` captures every failure in `warnings[]`; the runner is the
  only place HTTP statuses are assigned.
- **Confidence never gates and is never hashed** — it is advisory routing telemetry.
- **Offline runtime** — models baked at build time; `HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1` at run.
  Verified by a network-blocked offline-startup smoke test (not merely "a model file was copied").
- **AWS via the standard boto3 credential chain** — never creds in config. Textract needs
  `textract:AnalyzeDocument`; VLM escalation additionally needs Bedrock `InvokeModel`.
- **Dependency firewall** — ingestion reaches the parser HTTP-only over `INGESTION_PARSER_URL` and never
  imports `services.parser`, keeping ingestion torch-free.

---

## 8. Current state

| Path / control | State |
|---|---|
| PDF per-page gated markdown (Route B) | **As-built** |
| Image gated one-page path | **As-built** (README notes an always-VLM variant is a deferred cost call) |
| DOCX / XLSX / HTML whole-doc path | **As-built** |
| Textract escalation (default) | **As-built**, deterministic |
| VLM (Bedrock Claude) escalation | **As-built**, selectable via `PARSER_ESCALATION_ENGINE=vlm` |
| `POST /parse` (drain) + `GET /healthz` | **As-built** |
| `POST /parse/stream` (SSE) | **As-built** (per-page manifest; severable Task Group 6) |
| `PARSER_SCAN_FASTPATH` (all-scanned Docling skip) | Built, **opt-in, default OFF** — the first perf/cost knob to flip once the corpus is confirmed scan-heavy |
| `PARSER_ESCALATION_ARBITRATION` (keep-better-of Docling/engine) | Built, **opt-in, default OFF** (byte-identical to baseline when off) |
| Visual-hiding provenance (text-layer-vs-OCR diff) | **NOT built** — the parser is its natural owner (it has the render layer); a future extension can carry per-span provenance alongside each chunk. See the topology doc's parser / guardrail split |

**Not covered here (by design):** the guardrail's ingest-time `/check/chunks` corpus-poisoning scan sees
the *text* the parser produced; **visual hiding** (zero-size / off-canvas / colour-hidden / metadata-only
text) is destroyed at parse and is the parser's job, not the guardrail's. The two halves are separate and
composable — see [parser-topology.md](parser-topology.md) §4.
