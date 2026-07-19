# Parser Service Plan — docling + Textract, isolated behind HTTP

Status: **design, decisions locked** (2026-07-19). Upgrades document parsing from
inline `pypdf` (ingestion) to the `references/doc-parser` hybrid (Docling on CPU,
per-page escalation to an engine). Adversarial-engineering pass; ready to turn
into an agent-os spec + tasks.

## Locked decisions

| # | Decision | Choice |
|---|----------|--------|
| 1 | Topology | **Separate `services/parser/` microservice.** Ingestion calls it HTTP-only, never imports it — same dependency-firewall relationship the BFF has to ingestion. |
| 2 | Format scope (v1) | **All reference formats:** PDF (digital + scanned), image (PNG/JPG/TIFF), DOCX, XLSX, HTML. `.md` stays ingestion's existing fast passthrough (no round-trip to parser). |
| 3 | Escalation default | **Textract** (`PARSER_ESCALATION_ENGINE=textract`). Deliberate flip from the reference's VLM default. VLM (Bedrock Claude) remains selectable via env. |

### Why separate (the case against inlining docling)

`doc-parser` pulls **torch + torchvision + transformers + docling** — a multi-GB
image, model downloads, and CPU inference at **60–115 s/page**. Ingestion today is
lean (heaviest dep `pypdf`), Python 3.13, fast cold start; its job is throughput of
`embed → index`. Inlining would (a) balloon the image ~10× and turn a seconds
cold-start into tens of seconds, (b) put a 2-hour, crash-prone CPU stage on the
same workers as the I/O-bound hot path, (c) fuse two different scaling profiles into
one replica count, and (d) force ingestion's fast test suite to carry torch. A
separate service isolates every one of those. It also matches the repo's existing
shape: HTTP-only siblings behind a firewall.

### Note on the Textract default

Per the reference's own benchmark, **neither engine dominates**: the VLM wins a
scanned *form* ~2× (grounding beats flat OCR), Textract wins a *figure-heavy* page
~3×. Textract-default trades some scanned-form fidelity for a simpler cost model (no
token estimate; one `AnalyzeDocument` LAYOUT+TABLES call per promoted page, ~$0.019/pg
*provisional*), no non-determinism, and the same instance-role creds. The engine is a
per-deploy env knob; a per-page routing policy is explicitly **out of scope** (the
reference has too few escalating docs to learn one).

---

## Service shape: `services/parser/`

Thin FastAPI over the reference package. **Do not fork the reference logic** — vendor
`parser_service` as-is (its `parse_to_markdown` is a stable, never-raises contract) and
own only the HTTP boundary + the error-translation seam.

```
services/parser/
  app/
    main.py            # create_app(): /healthz + router
    config.py          # PARSER_ env prefix; engine, DPI, max_pages, budget, timeout
    api/parse.py       # POST /parse (drain), POST /parse/stream (SSE per-page)
    parse_runner.py    # wraps parse_to_markdown → PageEvent/ResultEvent generator + error xlate
  parser_service/      # vendored reference package (pinned), NOT modified
  tests/               # heavy suite: mocks Textract/VLM, needs docling but no AWS
  pyproject.toml       # docling pinned EXACT; python >=3.11 (match reference wheels)
  Dockerfile           # bakes docling models at build (offline runtime)
```

### HTTP contract (mirrors ingestion's `/ingest` + `/ingest/stream`)

`POST /parse` — body: the raw document bytes (multipart or octet-stream) + filename.
Drains the run, returns:

```json
{ "markdown": "...", "confidence": 0.91, "page_count": 12,
  "page_routes": [{"page_index":0,"route":"docling-kept","reason":"...","n_chars":812}, ...],
  "call_counts": {"vlm":0,"textract":3}, "warnings": [] }
```

`POST /parse/stream` — same input, `text/event-stream`. One frame **per page**
(`{"phase":"page","current":i,"total":N,"route":"textract"}`), then exactly one
terminal frame: `{"phase":"done", ...result}` or
`{"phase":"error","status":N,"detail":...}`. Structurally identical to ingestion's
embedding `current`/`total` — so ingestion can forward page progress with the code
patterns it already has.

`GET /healthz` — liveness (matches ingestion).

### The error-translation seam (the #1 correctness decision)

`parse_to_markdown` **never raises** — unsupported/empty/garbage input returns empty
markdown + warnings. Ingestion's contract is the **opposite**: refuse to index
nothing (`NoExtractableTextError → 422`). `parse_runner` reconciles them at the HTTP
boundary:

| Reference outcome | Parser HTTP result |
|---|---|
| non-empty markdown | `200` + result (warnings/confidence ride along as metadata) |
| `unsupported_type` warning / unknown extension | `400` invalid source |
| empty markdown / every page errored (no usable content) | `422` "no extractable content" |
| escalation engine unreachable / AWS error after retries exhausted | `502` upstream (safe message) |

Confidence + `page_routes` are **metadata only** — never gate ingest on the confidence
float (the reference is emphatic it is *not* a calibrated probability). They are
persisted as provenance (below) so the UI can flag "low-confidence, review" without
blocking.

---

## Ingestion integration (minimal blast radius)

The current `app/pipeline/parse.py::extract_text(source, data) -> str` is the only seam
that changes. It becomes a **parser HTTP client** for the reference formats, keeping
`.md` local:

- `.md` / `.markdown` → UTF-8 decode locally, as today (no round-trip).
- everything else → `parser_client.parse(bytes, filename)` → markdown string.

The returned markdown drops straight into the existing `normalize → hash → chunk →
embed → index` chain. `normalize()` was checked: it preserves line structure, headers,
pipe tables, and code fences, so docling markdown survives it (only nested-list indent
is stripped — cosmetic for RAG).

**Phase forwarding.** `run_ingest`'s `"parsing"` phase currently emits one bare
`PhaseEvent("parsing")`. It becomes a consumer of `/parse/stream`, re-emitting
`PhaseEvent("parsing", current=i, total=N)` per page — exactly the shape `embedding`
already uses. The BFF and frontend already tolerate unknown/loose phase fields
(the forward-compat work just shipped), so page sub-counters render for free.

**Provenance.** Persist `confidence` + a compact `page_routes` summary (engine call
counts, low-confidence page indices) on the indexed document/chunks so the webui can
surface review flags. Not in the dedup hash.

### Dedup consequences (call out on rollout)

- Dedup hashes the *extracted text*. `pypdf → docling markdown` changes those bytes, so
  **every existing PDF re-ingests once** (expected, one-time).
- **VLM escalation is non-deterministic** → a scanned page's markdown can drift
  run-to-run, so its sha256 drifts and dedup is **best-effort on escalating docs**.
  Textract (the chosen default) is far more stable here, which slightly favors the
  Textract default for dedup predictability. Named, accepted.

### Ingestion test suite stays lean

Ingestion mocks `parser_client` exactly as the BFF mocks `ingest_client` — **no torch
in ingestion's venv or CI**. The parser owns the heavy suite (vendored reference tests
already mock Textract/VLM and need no AWS, but do need docling installed).

---

## Operational guardrails (adversarial must-haves)

1. **Model provisioning** — bake docling models into the image at build time and **pin
   docling exact** (the reference itself says "pin after first successful run").
   Otherwise the pod is one HuggingFace outage from all-parses-failing.
2. **Per-request page cap** (`PARSER_MAX_PAGES`) + **per-request timeout** — a 100-page
   scan is ~2h and one engine call per promoted page; refuse oversized inputs with a
   clear 400/413 rather than pinning a worker.
3. **Bounded concurrency** — one tenant's giant scan must not starve the pool.
4. **Bedrock/Textract spend guard** — the reference only has a batch-CLI `--budget-usd`;
   the service needs a per-request (and ideally per-window) ceiling that halts
   escalation and finishes Docling-only rather than running up an unbounded bill.
5. **Input-size cap** — POST-bytes seam is capped (the reference already has an
   input-size error); S3-handle passing is the scale valve for very large files later.

---

## Proposed task groups (for `/create-tasks`)

1. **Scaffold `services/parser/`** — vendor `parser_service` pinned, `pyproject.toml`,
   `Dockerfile` with model bake, `config.py` (`PARSER_` prefix, Textract default),
   `/healthz`. Heavy suite green (no AWS).
2. **HTTP boundary** — `parse_runner` generator + error-translation table; `POST /parse`
   (drain) and `POST /parse/stream` (SSE per-page); guardrails (page cap, timeout,
   size cap, concurrency). Tests against crafted reference outputs (empty→422,
   unsupported→400, engine-down→502).
3. **Ingestion `parser_client`** — new HTTP client mirroring the BFF's `ingest_client`
   (drain + stream, typed error mapping, idle timeout → 504). Swap `extract_text` to
   route non-`.md` through it. Ingestion tests mock the client; keep the suite lean.
4. **Phase forwarding + provenance** — `run_ingest` "parsing" phase consumes
   `/parse/stream`, re-emits page `current`/`total`; persist confidence/page_routes
   provenance; end-to-end through `/ingest/stream`.
5. **Compose + ops** — add `parser` to `docker-compose.yml`, wire
   `INGESTION_PARSER_URL`, budget/cap env, README. Rollout note on the one-time
   re-ingest and Textract-default rationale.

## Open risks to watch during build

- Docling wheel availability on the chosen Python (match the reference's `>=3.11`; pin
  after first successful `uv` resolve — torch wheels are the fragile part).
- Textract per-page price ($0.019) is **provisional** in the reference — verify live
  `ap-southeast-2` LAYOUT+TABLES pricing before trusting the cost summary.
- Bytes-over-the-wire twice (S3 → ingestion → parser) is fine in-cluster; revisit
  S3-handle passing only if large-file latency bites.
