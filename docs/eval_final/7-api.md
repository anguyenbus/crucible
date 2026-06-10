# Evaluation Service — API Surface (v1)

## 1. What this service does

The service has three evaluation flows and one API. The first two evaluate the **RAG pipeline**
(retrieval + generation); the third evaluates the **document parser** that feeds it.

**Nightly online scoring (RAG).**
Every midnight, the service takes a 5% sample of the previous day's production traffic — the
questions users asked, the chunks the system retrieved, and the answers it returned — and
grades them with an LLM judge via DeepEval.
Scores are written to the results database (RDS) and also back onto the traces in Phoenix, so
quality scores appear next to the traces the team already browses.

**Golden-dataset scoring (RAG).**
On demand, the service grades a golden dataset — a set of questions with written-down expected
answers.
Having an expected answer unlocks the two metrics that need a reference to compare against:
contextual precision and contextual recall.

**Document-parser evaluation.**
On demand, the service measures how well the ingestion team's document parser extracts content
from PDFs and images. It calls the ingestion parser to parse a set of files, then scores the
parser's output against known-correct output (OmniDocBench + data-scientist golden data) using
deterministic comparison metrics — not an LLM judge.
This runs in two forms sharing one scoring library: a small wheel (11 files) run inline in
CI/CD, and a full run (593 files) triggered through the API for the data-scientist team.

**The REST API.**
The API exists so other teams can read results (dashboards, CI checks, the UI) and trigger the
runs that have no schedule behind them (golden-dataset runs, parser-evaluation runs, backfills,
ad-hoc datasets).
The API never performs evaluation work synchronously — every run is recorded and handed off to
a worker; callers poll for status and results.

---

## Assumptions

This spec inherits several conventions from the ingestion service for consistency, and uses
placeholder values in examples. Both are called out here so nothing reads as settled when it
isn't. Anything marked **[verify]** needs confirmation before build; anything marked
**[placeholder]** is an illustrative value, not a decision.

- **Shared identity provider [verify].** Assumes the eval service uses the same JWT issuer,
  JWKS, and audience as ingestion, so other teams authenticate the same way for both. If the
  eval service sits behind a different IdP, §4 changes.
- **`bu_id` claim [verify].** Carried over from ingestion's token shape; "business unit" is the
  assumed meaning (an org grouping above project). The eval service does **not** use it — all
  scoping is by `project_id`. Kept in the claims example only so the token matches ingestion's;
  remove it if eval tokens won't carry it.
- **Persistence stack [verify].** Assumes RDS (results of record), DynamoDB (idempotency / hot
  status), and S3 (datasets, artifacts), mirroring ingestion. If the eval service standardizes
  on a different store, the schemas hold but the infra/IAM sections change.
- **Parser ownership [verify].** Assumes the document parser is owned by and called from the
  ingestion service; `parse_run` depends on that service at runtime (§8.5). The invocation
  contract (direct call vs. async pipeline) is explicitly undecided — Open Question 7.
- **OmniDocBench as a registered dataset [verify].** Assumes OmniDocBench (files + ground-truth
  labels) is registered as an S3 dataset (`ds_omnidocbench@N`) like the RAG golden set, so a
  `parse_run` references it by `dataset_id`.
- **Shared comparator library [verify].** Assumes the CI wheel and the service worker use one
  versioned comparator (Open Question 9). If they're separate code today, that's a risk to fix,
  not a settled fact.
- **Judge model [placeholder].** `anthropic.claude-sonnet-4-5-v1:0` via Bedrock is used in
  examples; the actual judge model and provider are a config decision (§7.7).
- **Metric thresholds [placeholder].** The `0.80 / 0.85 / 0.70` values in examples are
  illustrative. Real thresholds come from calibrating against the golden set, not from this doc.
- **Identifiers and names [placeholder].** `ds_legal_rag_bench`, `legal-rag-prod`, `cfg_…`,
  `sa-eval-*`, ULID prefixes, the 14–30 day Phoenix retention, and the 00:00 schedule are all
  example values to be set per environment.
- **Ingestion cross-references [verify].** Mentions of "ingestion §4.4" etc. assume the
  companion ingestion design doc; section numbers should be checked against its current version.

---

## 2. Principles

1. **Async jobs only.** No endpoint performs evaluation work synchronously. `POST` endpoints
   validate, persist the run record, hand the work off to a worker, and return `202` with a
   `run_id`. Callers poll `GET /v1/eval/runs/{run_id}`. (Pattern: NeMo Evaluator / OpenAI Evals.)
2. **Schedule-triggered nightly, API-triggered on-demand.** The dominant flow (nightly scoring)
   is triggered by a schedule, not HTTP. The API exists primarily for reads and for runs that
   have no schedule behind them (golden-dataset runs, backfills, ad-hoc datasets).
3. **Execution model is a stated decision, not an assumption.** Nightly scoring runs as a
   Kubernetes CronJob that executes the work directly — a self-contained batch needs no message
   queue. A queue is introduced only where it earns its keep: the API's `POST /runs` must return
   immediately without blocking, so API-triggered runs need an asynchronous handoff to a worker.
   Whether that handoff and the nightly job share one worker path or stay separate is called out
   explicitly in §11 and the Open Questions, so reviewers decide it deliberately.
4. **One run schema.** A `target.type` discriminator (`dataset` | `model` | `parse_run`) covers
   all current lanes. (`rag_pipeline` for RAG replay is reserved but not implemented — Appendix A.)
5. **Reference-free on raw traffic; ground truth only on labeled datasets.** The API
   mechanically rejects ground-truth metrics on span sources (422). See §6.
6. **S3 is the durable tier.** Phoenix holds the working set under a retention policy;
   anything evaluation needs long-term (datasets, golden sets, artifacts) is materialized to
   S3 as immutable, content-hashed snapshots. See §8.3.
7. **Versioned path (`/v1/`).** Every path is prefixed with `/v1/` because other teams generate
   clients from this API and ship code against its request/response shapes — once they do, those
   shapes are a contract this service can't silently change. The version prefix is the escape
   hatch: backward-compatible changes (new optional field, new endpoint, new enum value) stay in
   `v1`; a breaking change (removing/renaming a field, changing a type, making an optional field
   required) goes in a new `/v2/` so existing callers keep working until they choose to migrate.
   Starting at `v1` is convention, not a prediction of a `v2` — many APIs never need one; the
   prefix is cheap insurance against being unable to evolve later. The generated `openapi.json`
   is a published artifact, and CI fails on breaking diffs (`oasdiff` in `breaking` mode) to
   enforce that breaking changes can't slip into `v1` unnoticed.
8. **Same auth story as ingestion.** JWT Bearer via the shared identity provider; `project_id`
   and scopes come from token claims, never from the request body.

---

## 3. Endpoint Summary

| Method | Path | Auth scope | Purpose |
|---|---|---|---|
| POST | `/v1/eval/runs` | `eval:write` | Submit an eval run (async) |
| GET | `/v1/eval/runs/{run_id}` | `eval:read` | Run status + result summary (poll target) |
| GET | `/v1/eval/runs/{run_id}/results` | `eval:read` | Per-item results, paginated |
| GET | `/v1/eval/runs` | `eval:read` | List/filter runs |
| POST | `/v1/eval/runs/{run_id}/cancel` | `eval:write` | Best-effort cancel of a queued/running run |
| GET | `/v1/eval/configs` · `/{config_id}` | `eval:read` | List / fetch eval configs |
| POST | `/v1/eval/configs` | `eval:admin` | Register a new config version (immutable) |
| GET | `/v1/eval/datasets` · `/{dataset_id}` | `eval:read` | List / fetch dataset registrations |
| POST | `/v1/eval/datasets` | `eval:admin` | Register/materialize a dataset version (immutable) |
| GET | `/healthz` / `/readyz` | none | K8s liveness / readiness |

Notes:
- The nightly scoring job is **not** created via this API. A Kubernetes CronJob runs at 00:00,
  creates the run record itself, and performs the scoring. Nightly runs appear in
  `GET /v1/eval/runs` with `"initiated_by": "schedule"` so consumers see one unified run history.
- `initiated_by` values: `schedule` (nightly CronJob), `api` (human/CI via this API).

---

## 4. Auth & Middleware

Identical middleware chain to ingestion-api: `JWTAuthMiddleware` → `ScopeMiddleware` →
`RequestLoggingMiddleware` (structured JSON, `X-Trace-Id` echoed).

**Claims shape** (assumes the same JWKS, issuer, and audience as ingestion — see Assumptions):

```json
{
  "sub": "user-or-service-uuid",
  "project_id": "uuid",
  "bu_id": "uuid",
  "permissions": ["eval:read", "eval:write", "eval:admin"],
  "exp": 1234567890
}
```

`bu_id` (business unit) is inherited from ingestion's token and is **not used** by the eval
service — all scoping is by `project_id`. See Assumptions.

| Scope | Grants |
|---|---|
| `eval:read` | All GET endpoints |
| `eval:write` | Submit/cancel runs (`dataset`, `model`, and `parse_run` targets) |
| `eval:admin` | Register configs and materialize datasets |

(`eval:replay` is reserved for the future RAG replay capability — Appendix A.)

`project_id` from the token scopes every read and write. Cross-project reads return `404`,
not `403` (no existence leakage). Service-to-service callers use the same JWT
client-credentials flow as ingestion; the ALB is `scheme: internal` — not reachable outside
the VPC.

---

## 5. Core Schemas

### 5.1 EvalRunRequest

```json
{
  "name": "golden-set-weekly",
  "target": { "...": "discriminated union, see 5.2" },
  "config_id": "cfg_01J...",
  "dataset_id": "ds_legal_rag_bench@1",
  "metrics": ["answer_relevancy", "faithfulness", "contextual_precision", "contextual_recall"],
  "idempotency_key": "optional-client-key",
  "labels": { "team": "search", "ticket": "EVAL-142" }
}
```

- `config_id` references a registered, immutable config (judge model + prompt version,
  thresholds, temperature=0 for comparability). Inline config is **not** accepted in v1.
- `dataset_id` uses `name@version` or opaque id. Required for `dataset` targets with an
  `s3_manifest` source; forbidden for `span_window` sources (the window *is* the input).
- `metrics` is optional; defaults come from the config. Subsetting is allowed, supersetting is not.
- `idempotency_key`: if supplied and a run with the same key + identical request hash exists,
  the API returns `200` with the existing run. Internally this maps onto the same DynamoDB
  conditional-claim mechanism the schedule/event paths use (repro key).

### 5.2 Target (discriminated union on `type`)

**`dataset`** — score existing inputs/outputs. The only target type needed for current operations.

Source kind `s3_manifest` (frozen dataset, e.g. the golden set):

```json
{
  "type": "dataset",
  "source": {
    "kind": "s3_manifest",
    "uri": "s3://eval-data/datasets/ds_legal_rag_bench/v1/_MANIFEST.json"
  }
}
```

Source kind `span_window` (production traffic from Phoenix — **online lane only**):

```json
{
  "type": "dataset",
  "source": {
    "kind": "span_window",
    "project_identifier": "legal-rag-prod",
    "from": "2026-06-10T00:00:00Z",
    "to": "2026-06-11T00:00:00Z",
    "sample_rate": 0.05,
    "strata": ["doc_type"]
  }
}
```

**Span-window rules (normative):**
- A span window is a *query*, not a frozen artifact: Phoenix spans are deletable (ungated, via
  UI and REST API), retention-purged on a CRON schedule, and PII-incident deletions can remove
  spans at any time. Therefore span-window runs are **one-shot health readings** — they are
  never paired item-by-item against another run.
- Only reference-free metrics are accepted on span sources (see §6); ground-truth metrics
  return `422`.
- Anything that must be compared across runs or kept long-term is first **materialized** into
  an immutable S3 dataset via `POST /v1/eval/datasets` (see §7.6).

**`model`** — generator-only eval against a model endpoint (Bedrock model id; the service
resolves the endpoint, callers never pass raw URLs/credentials).

```json
{
  "type": "model",
  "model_id": "anthropic.claude-sonnet-4-5-v1:0",
  "params": { "temperature": 0.0, "max_tokens": 1024 }
}
```

**`parse_run`** — evaluate the ingestion document parser. The worker pulls each file from the
dataset, calls the ingestion parser to produce parser output, then scores that output against
the dataset's ground truth with deterministic comparison metrics (§6.3). Always requires a
ground-truth dataset.

```json
{
  "type": "parse_run",
  "dataset_id": "ds_omnidocbench@1",
  "parser": { "service": "ingestion", "version": "docling-2.9.1" }
}
```

- `parser.version` names the parser build under test; the eval worker resolves the ingestion
  parser endpoint internally (callers never pass raw URLs/credentials), the same way `model`
  targets resolve a Bedrock endpoint.
- The means by which the worker obtains parser output (a direct internal call vs. ingestion's
  existing async ingest pipeline) is an **open cross-team contract** — see §8.5 and Open
  Questions. The API shape above is stable regardless of which is chosen.
- Single-version scoring is the implemented shape. Comparing two parser versions
  (baseline vs. candidate) is reserved the same way RAG replay is — see Appendix A.

(**`rag_pipeline`** — replay against a candidate RAG system — is reserved and not implemented.
See Appendix A.)

### 5.3 EvalRun (response object)

```json
{
  "run_id": "run_01JABCDEF...",
  "name": "nightly-online-2026-06-11",
  "status": "queued",
  "initiated_by": "schedule",
  "lane": "judge",
  "target": { "type": "dataset", "...": "..." },
  "config_id": "cfg_01J...",
  "dataset_id": null,
  "repro_key": "sha256:9f2c...",
  "result_counts": { "total": 0, "scored": 0, "passed": 0, "failed": 0, "errored": 0 },
  "summary": null,
  "created_at": "2026-06-11T00:00:05Z",
  "started_at": null,
  "completed_at": null,
  "links": {
    "self": "/v1/eval/runs/run_01JABCDEF",
    "results": "/v1/eval/runs/run_01JABCDEF/results"
  }
}
```

**Status lifecycle:** `queued → running → completed | failed | cancelled`.
Partial item failures surface in `result_counts.errored` while the run still completes.

`repro_key` is the server-computed reproducibility key (config version + dataset hash +
judge prompt version + model ids + code version). Two runs with equal repro keys scored
byte-identical inputs under identical methodology. Span-window runs carry a repro key for
provenance but are flagged `comparable: false` (window contents are not stable).

---

## 6. Metrics & Ground Truth

### 6.0 RAG metrics (LLM-as-judge, via DeepEval)

The RAG lanes evaluate with **DeepEval**. Metrics divide by what they require:

| Metric | Requires | Span window (nightly) | Golden dataset |
|---|---|---|---|
| `answer_relevancy` | question + answer | ✅ | ✅ |
| `faithfulness` | question + answer + retrieved chunks | ✅ | ✅ |
| `contextual_relevancy` | question + retrieved chunks | ✅ | ✅ |
| `contextual_precision` | the above **+ expected answer** | ❌ 422 | ✅ |
| `contextual_recall` | retrieved chunks **+ expected answer** | ❌ 422 | ✅ |

Rationale (plain language): precision and recall both ask *"did retrieval find what was needed
for the ideal answer?"* — judging that requires someone to have written the ideal answer down.
Raw production traffic has no such label, so DeepEval's contextual precision/recall require
`expected_output` and cannot run on spans. The reference-free trio above is the industry "RAG
triad" recommended for exactly this case. The API enforces the split: requesting a
ground-truth metric on a `span_window` source returns `422` with an explanatory message.

### 6.1 The recurring runs

**Nightly online scoring (schedule-triggered, no API call):**
- 00:00 — Kubernetes CronJob fires; the job pod creates the run record and performs the scoring
- Source: `span_window`, previous 24h, `sample_rate: 0.05`, Phoenix project `legal-rag-prod`
- Metrics: `answer_relevancy`, `faithfulness`, `contextual_relevancy`
- Outputs: results to RDS; scores + judge rationale written back to Phoenix as span
  annotations (`annotator_kind: "LLM"`); per-chunk contextual-relevancy scores written as
  document-level annotations on the retrieval span.

**Golden-set run (API- or CI-triggered):**
- `POST /v1/eval/runs` with `dataset_id: "ds_legal_rag_bench@1"` (s3_manifest source)
- Metrics: full set, including `contextual_precision` and `contextual_recall`
- Cadence: weekly, plus after any retrieval-affecting config change.

**Parser-evaluation run (CI smoke set + API full set):**
- CI smoke set (11 files): the parse-evaluation **wheel** runs inline in CI/CD — ~15 min on
  CPU, ~1 min on GPU — and fails the build on regression. Does not call this API.
- Full set (593 files): `POST /v1/eval/runs` with `target.type: "parse_run"` and
  `dataset_id: "ds_omnidocbench@1"`. The worker calls the ingestion parser per file, then
  scores. Triggered by the data-scientist team on demand.
- Both forms use the **same comparator library** (§6.3) so CI and the full run agree on what a
  good parse is.

### 6.2 Golden dataset status and growth plan

Current golden set: **`ds_legal_rag_bench@1` — 10 Legal RAG Bench questions** with labeled
expected answers, materialized to S3 with content hash.

**Honest sizing note:** 10 items is a *smoke test*, not a statistical instrument. Threshold
checks ("did any item fall below 0.7 faithfulness?") are meaningful at this size; means,
trends, and pass/fail verdicts based on averages are not — a single flaky judge call moves
the mean by 10 points. Treat `verdict` from golden-set runs as advisory until the set grows.

**Growth path (the feedback flywheel):** spans that score badly in the nightly run are triaged
via `GET /runs/{id}/results?verdict=fail`; the genuine failures get a human-written expected
answer and are promoted into the next golden-set version (`ds_legal_rag_bench@2`, `@3`, ...).
Target: ~100+ items before golden-set verdicts gate anything automatically. Each version is a
new immutable snapshot; runs pin the version they used.

### 6.3 Parser comparison metrics (deterministic, via the comparator library)

Parser evaluation does **not** use an LLM judge. It compares the parser's extracted output
against known-correct output with deterministic metrics. Parse runs therefore **always require
a ground-truth dataset** — there is no reference-free parser mode.

Metric family (exact set and thresholds to be finalized with the data-scientist team — listed
here as the shape, not a committed list):

| Dimension | Measures | Example metric |
|---|---|---|
| Text extraction | Did the parser recover the text? | normalized edit distance / character F1 |
| Tables | Cell content + structure correct? | TEDS (tree-edit-distance similarity) |
| Reading order | Content in the right sequence? | order correlation |
| Layout / regions | Blocks detected and placed? | region IoU |

Ground-truth sources:
- **OmniDocBench** — public benchmark with labeled document structure; registered as an S3
  dataset (`ds_omnidocbench@N`).
- **Data-scientist golden data** — internally prepared labels for files specific to the
  product's document mix; registered the same way and versioned.

**The comparator is one shared, versioned library.** The CI wheel and the service worker both
call it, so a "good parse" means the same thing in the 15-minute CI check and the 593-file run.
Its version is part of the run's repro key (alongside dataset version and parser version), so
two parse runs are comparable only if scored by the same comparator on the same dataset.

---

## 7. Endpoints

### 7.1 POST /v1/eval/runs

**Response — 202 Accepted:** the `EvalRun` object with `status: "queued"`.
**Response — 200 OK:** idempotent replay of an existing run (idempotency-key hit).

The handler does exactly four things, in order: validate → persist run record (RDS, single
transaction) → hand the work off to a worker → return. The handoff mechanism (a message queue
such as SQS, or a direct worker dispatch) is the §11 execution-model decision; whichever is
chosen, the record is committed to RDS **before** the handoff, so a handoff failure leaves the
run recoverable (`queued`) rather than lost — the same persist-then-dispatch ordering ingestion
uses (ingestion §4.4).

**Errors:**

| Code | Meaning |
|---|---|
| 400 | Malformed request, unknown `target.type`, missing required fields |
| 401 | Missing/invalid JWT |
| 403 | Scope missing |
| 404 | `config_id` / `dataset_id` not found in caller's project |
| 409 | `idempotency_key` reused with a *different* request hash |
| 422 | Semantically invalid: ground-truth metric on a span source; metric not in config |
| 429 | Per-project run-submission rate limit (protects Bedrock judge quota) |
| 500 | Persist or dispatch failure (no message sent if persist failed) |

### 7.2 GET /v1/eval/runs/{run_id}

Returns the `EvalRun` object. While `running`, `result_counts` and `summary` update
incrementally. `summary` on completion:

```json
{
  "metrics": {
    "faithfulness":         { "mean": 0.91, "p25": 0.84, "threshold": 0.85, "pass": true },
    "answer_relevancy":     { "mean": 0.93, "p25": 0.88, "threshold": 0.80, "pass": true },
    "contextual_relevancy": { "mean": 0.78, "p25": 0.61, "threshold": 0.70, "pass": true }
  },
  "verdict": "pass"
}
```

`verdict` is a single field, deliberately, so consuming pipelines don't reimplement threshold
logic. **Errors:** 401, 404 (not found *or* other project).

### 7.3 GET /v1/eval/runs/{run_id}/results

Per-item detail, cursor-paginated:

```
GET /v1/eval/runs/{run_id}/results?metric=faithfulness&verdict=fail&cursor=...&limit=100
```

```json
{
  "items": [
    {
      "item_id": "itm_...",
      "input_ref": { "kind": "span", "trace_id": "...", "span_id": "..." },
      "scores": { "faithfulness": 0.42 },
      "verdict": "fail",
      "judge_rationale": "Claim X not supported by retrieved context...",
      "artifacts": { "context_hashes": ["..."] }
    }
  ],
  "next_cursor": "eyJv...",
  "total": 1240
}
```

Filters: `metric`, `verdict` (`pass|fail|errored`), `min_score`, `max_score`. The
`verdict=fail` filter is the triage workhorse and the entry point of the golden-set growth
flywheel (§6.2). For span-sourced items, `input_ref` carries the Phoenix trace/span IDs so
triage can deep-link into the Phoenix UI.

### 7.4 GET /v1/eval/runs

```
GET /v1/eval/runs?initiated_by=schedule&status=completed&from=2026-06-01&cursor=...&limit=50
```

Returns abbreviated `EvalRun` objects, newest first. This is the dashboard/regression-history
endpoint. **Trend-line rule:** only group runs with equal `repro_key` *and* `comparable: true`
into the same like-for-like trend; nightly span-window runs are plotted as a health time series,
not as paired comparisons.

### 7.5 POST /v1/eval/runs/{run_id}/cancel

Best-effort: marks the run `cancelling`; workers check the flag between items. Returns `202`
with the run object. `409` if already terminal.

### 7.6 Datasets — registration and materialization

`POST /v1/eval/datasets` creates **immutable versions** — no PUT/PATCH. "Updating" means
registering version N+1; runs pin the version they were submitted with.

Two source kinds:

**Direct registration** (e.g. the golden set, prepared offline):

```json
{
  "name": "legal_rag_bench",
  "source": { "kind": "s3", "uri": "s3://eval-data/staging/legal-bench.parquet" },
  "has_ground_truth": true
}
```

**Materialization from a span window** (snapshot production traffic into a frozen dataset):

```json
{
  "name": "prod_w23_failures",
  "source": {
    "kind": "span_window",
    "project_identifier": "legal-rag-prod",
    "from": "2026-06-01T00:00:00Z",
    "to": "2026-06-08T00:00:00Z",
    "sample_rate": 0.05,
    "strata": ["doc_type"]
  },
  "has_ground_truth": false
}
```

Materialization runs the window query against Phoenix **once**, extracts the eval-relevant
fields per span (question, retrieved documents + scores, answer, model/params, trace/span IDs),
applies **PII redaction** (this is the single redaction chokepoint before user queries are
persisted long-term or shown to judge models), writes Parquet files with deterministic item
ordering to S3, and records per-file SHA-256 plus a manifest hash. The `_MANIFEST.json` is
written last (completion marker), and records the original window query for provenance, the
redaction version, item count, and hashes. The registry row in RDS stores
`{dataset_id, version, s3_prefix, content_hash, item_count, has_ground_truth}`.

The dataset hash is what makes the repro-key guarantee verifiable: anyone can re-hash the S3
objects and confirm two runs scored byte-identical inputs.

### 7.7 Configs

`POST /v1/eval/configs` — immutable versions, same discipline as datasets:

```json
{
  "name": "legal-rag-judge",
  "kind": "judge",
  "spec": {
    "library": "deepeval",
    "judge_model_id": "anthropic.claude-sonnet-4-5-v1:0",
    "judge_prompt_version": "deepeval-default+fp-2026-05-11",
    "temperature": 0.0,
    "metrics": {
      "answer_relevancy":     { "threshold": 0.80 },
      "faithfulness":         { "threshold": 0.85 },
      "contextual_relevancy": { "threshold": 0.70 },
      "contextual_precision": { "threshold": 0.70, "requires_ground_truth": true },
      "contextual_recall":    { "threshold": 0.70, "requires_ground_truth": true }
    }
  }
}
```

Note: DeepEval metric prompt templates are overridable; if we customize them, the customized
template is versioned in `judge_prompt_version` so the repro key captures it.

---

## 8. External Integrations

This service depends on two systems it does not own: **Phoenix** (the trace store, §8.1–8.4)
and the **ingestion document parser** (§8.5). Both are cross-team contracts: if either changes
its interface, a flow here breaks. They are documented so those dependencies are explicit.

### 8.0 Phoenix Integration

### 8.1 Span contract (prerequisite, cross-team)

The nightly lane can only score what instrumentation captured. Required per sampled trace,
in **OpenInference** attribute terms (Phoenix's native convention):

- Root span: `input.value` (user question), `output.value` (final answer)
- RETRIEVER span: `retrieval.documents` (document content + scores)
- LLM span: model id and generation params (for provenance in the repro key)

Traces missing required attributes are counted in `result_counts.errored` with reason
`span_contract_violation` — they are skipped, not guessed at. This contract is owned jointly
with the app team and versioned in this document.

### 8.2 Score write-back

After scoring, the worker writes annotations back to Phoenix (bulk API):
- Span-level annotations per metric: `annotator_kind: "LLM"`, score, label
  (`pass`/`fail` vs threshold), and the judge rationale as `explanation`.
- Document-level annotations for per-chunk contextual-relevancy verdicts
  (`add_document_annotation` with `document_position`).

Phoenix is the *convenience view*; RDS remains the results of record. Write-back failures are
logged and retried but never fail the run.

### 8.3 Storage tiering (normative)

- **Phoenix (Postgres) = working set.** Retention policy set at deployment
  (`PHOENIX_DEFAULT_RETENTION_POLICY_DAYS`, recommended 14–30 days; CRON enforcement during
  off-hours). Capacity math is a standing prerequisite: spans/day × retention days should stay
  in the low tens of millions — the documented failure mode is indefinite accumulation
  (community report: ~200M spans / 2TB → non-functional deployment).
- **S3 = durable tier.** Materialized datasets, golden sets, and eval artifacts. Immutability
  enforced by bucket policy (deny overwrite of existing keys); lifecycle rules transition old
  unreferenced snapshots to Glacier.
- **RDS = ledger.** Run records, scores, verdicts. Never depends on Phoenix retention.
- Flow is one-way: spans → S3 at materialization; scores → RDS and → Phoenix annotations.
  No eval read path returns to Phoenix after materialization. Phoenix can be purged, pruned,
  or down without affecting any materialized dataset or any historical result.

### 8.4 Operational notes

- Workers reach Phoenix over the in-cluster service; Phoenix API key in Secrets Manager
  alongside DB creds. No new AWS IAM (Phoenix is plain HTTP).
- Bulk span reads paginate politely (cursor-based, bounded page size); the nightly window
  read is scheduled at 00:00 deliberately (off-peak for the tracing UI).
- The eval service does not export the span firehose to S3 — only what evaluation touches.
  A 5% nightly sample of questions+contexts+answers is megabytes, not terabytes.

### 8.5 Ingestion parser integration (parse_run dependency)

A `parse_run` cannot work without calling the ingestion team's document parser. This is the one
flow with a hard runtime dependency on another team's live service.

**The contract (jointly owned with ingestion, to be finalized):**
- **Invocation [open — see Open Questions]:** how the eval worker obtains parser output is not
  yet decided. Two candidates: a direct internal call per file (synchronous request/response),
  or submission through ingestion's existing async ingest pipeline. The `parse_run` API shape is
  stable either way; only the worker's internal call path changes.
- **Output format:** ingestion's parser output schema is an input to the comparator. If the
  parser's output shape changes, the comparator must be updated in lockstep — this is a
  versioned contract, not an incidental coupling.

**Run behavior (normative):**
- **Per-file isolation.** A parser timeout or error on one file is recorded in
  `result_counts.errored` for that item with a reason; it does not fail the whole run. Same
  pattern as `span_contract_violation` in the nightly lane.
- **Concurrency limit.** A full run is 593 parser calls. The worker bounds in-flight calls
  (configurable) so a run cannot swamp a shared parser the rest of the platform relies on. The
  limit, and whether parser capacity must be reserved for large runs, is an Open Question.
- **Latency/availability are partly ingestion's.** Unlike the nightly RAG job (which only reads
  already-recorded spans and depends on nothing live), a `parse_run`'s duration and success
  depend on the parser service. Runs surface partial progress via `result_counts` while running.

**The GPU note.** The CPU-vs-GPU difference (15 min vs. 1 min on the 11-file set) is a property
of the **parser**, which is GPU-heavy — not of the eval worker, which only orchestrates and runs
the CPU-bound comparator. So eval-worker sizing is unaffected by it; full-run latency is governed
by the parser's throughput and the concurrency limit above.

---

## 9. Conventions

- **Pagination:** opaque `cursor` + `limit` (max 200) on all list endpoints. No offset paging.
- **Errors:** uniform body `{ "error": { "code": "...", "message": "...", "trace_id": "..." } }`.
- **Rate limiting:** per-project token bucket on `POST /v1/eval/runs`; `429` carries
  `Retry-After`. Protects the shared Bedrock judge quota.
- **Timestamps:** RFC 3339 UTC. **IDs:** ULID-based, prefixed (`run_`, `cfg_`, `ds_`, `itm_`).
- **OpenAPI / versioning:** the spec is generated by FastAPI from the Pydantic models and
  published as an `openapi.json` artifact consuming teams generate clients from. Versioning
  rule and the CI breaking-change gate are in Principle 7.

---

## 10. Infrastructure (summary)

Three workloads, deployed on K8s/EKS:

**eval-api** — the REST service (mirrors ingestion-api):

```yaml
# eval-api Deployment
replicas: 2                     # HPA on CPU/request rate
resources:
  requests: { cpu: 250m, memory: 256Mi }
  limits:   { cpu: 500m, memory: 512Mi }
serviceAccount: sa-eval-api     # IRSA
```

**eval-nightly** — the scheduled scoring job:

```yaml
# eval-nightly CronJob
schedule: "0 0 * * *"           # 00:00, kube scheduler — no external scheduler, no queue
concurrencyPolicy: Forbid       # skip a run if the previous one is still going
serviceAccount: sa-eval-worker  # IRSA — has Bedrock + Phoenix access
```

**eval-worker** — *conditional.* Only needed if API-triggered runs are processed off a queue
(the §11 decision). If present, it's a Deployment (or KEDA-scaled) consuming the run queue; if
API runs reuse the CronJob image as one-shot K8s Jobs instead, this workload doesn't exist.

- **Ingress (eval-api):** AWS Load Balancer Controller, ALB `scheme: internal`, TLS at the ALB.
- **IAM (sa-eval-api):** `rds-db:connect` via RDS Proxy; `dynamodb` on idempotency + run tables;
  `secretsmanager:GetSecretValue`; plus `sqs:SendMessage` on the run queue **only if** the §11
  decision is a queue. **No** Bedrock, **no** Phoenix-write — the API never touches the model
  plane or the trace store; only the worker/CronJob does.
- **IAM (sa-eval-worker):** Bedrock invoke (judge model); `rds-db:connect`; S3 read/write
  (datasets, artifacts); `secretsmanager:GetSecretValue` (incl. Phoenix API key); plus
  `sqs:ReceiveMessage`/`DeleteMessage` on the run queue **only if** the §11 decision is a queue.
- **Parser connectivity (parse_run):** the worker must reach the ingestion parser. If it's an
  in-cluster service this is network policy + service auth (no new AWS IAM); if parser output is
  obtained via ingestion's async pipeline (e.g. submitting to its queue / API), the
  corresponding grant is added. Pending the §8.5 contract decision.
- **Schedule:** the kube CronJob scheduler — no EventBridge, no SQS for the nightly trigger.
- **Probes (eval-api):** `/healthz` liveness; `/readyz` readiness (RDS Proxy reachable, and the
  run queue reachable if one is used).
- eval-api is stateless; all state in RDS, DynamoDB, S3.

---

## 11. Execution model (nightly vs. API-triggered)

Runs reach a worker by one of two trigger paths — the nightly schedule, or the API — and the
two have different execution needs. The doc treats this as an explicit decision rather than
defaulting to a queue. (This is about *how work is dispatched*, independent of the three
evaluation flows in §1; parser-eval and golden-set runs both arrive via the API path.)

**Nightly scoring needs no queue.** It is a single self-contained batch on a fixed schedule:
read a 24h span window, sample, score, write results, exit. A Kubernetes CronJob is the whole
mechanism — the kube scheduler is the trigger and the job pod is the worker. Adding a message
queue here would be ceremony: there is one producer (the schedule), one consumer (the job), and
no fan-in. This is the chosen design for the nightly flow.

**API-triggered runs need an asynchronous handoff.** `POST /runs` must return `202` immediately
and must not block on a multi-minute judge run, so the work has to be handed to something that
runs after the response is sent. That is a genuine async-worker need, independent of the
nightly job. There are two reasonable shapes:

- **Option A — one-shot K8s Jobs.** `POST /runs` creates a run record and launches a Kubernetes
  Job (reusing the nightly image with a different entrypoint). No queue, no standing worker
  pool; the cluster is the dispatcher. Simplest when API-triggered runs are infrequent
  (golden-set runs, occasional backfills). Trade-off: no built-in backpressure or queue depth
  to scale on; a burst of submissions launches a burst of Jobs.
- **Option B — a run queue + worker pool.** `POST /runs` enqueues; a standing (or KEDA-scaled)
  `eval-worker` consumes. Justified only if submission volume is high enough to need backpressure
  and queue-depth-based autoscaling, or if you want nightly + API runs to share exactly one
  worker code path and one retry/idempotency story. Trade-off: a queue and a worker deployment
  to operate, for a workload that may not need them.

**Recommendation:** start with the CronJob for nightly (settled) and **Option A** for
API-triggered runs — the smaller footprint — and adopt Option B only if measured submission
volume justifies it. Either way the API contract is unchanged: persist run record to RDS, then
hand off, then return `202`; callers poll. The choice is an internal implementation detail
behind the API, not part of the consumer-facing contract.

---

## Appendix A — Reserved comparisons (not implemented)

Two comparison flows are reserved in the schema but not built. Both are *paired comparisons*
(score two versions, decide if the candidate is better) layered on top of an implemented
single-score flow. They are documented so the reserved schema slots make sense and so the terms
aren't confused with the implemented runs.

### A.1 RAG replay (`target.type: "rag_pipeline"`)

> **Status: reserved, not implemented.** The discriminated union on `target.type` reserves
> `"rag_pipeline"` (and scope `eval:replay`) so this is non-breaking to add later. Documented so
> "replay" is not confused with nightly scoring: nightly scoring **grades answers production
> already gave**; replay **re-asks old questions to a changed RAG system that has not shipped**.

**Plain language:** before shipping a RAG change (new prompt, new top-k, new chunking), take a
frozen set of real questions, run them through both the current system and the changed system,
score both sides, and compare — so regressions are caught before users see them.

**Constraints that would apply:**
- `POST /v1/eval/runs` with `target.type: "rag_pipeline"`, requiring scope `eval:replay`.
- Input is always an immutable `dataset_id` (materialized snapshot) — never a live span window,
  because paired comparison requires byte-identical inputs on both sides.
- `change_kind` branches the cost:
  - `prompt_or_generation` — reuse recorded retrieval, regenerate answers only (cheap)
  - `query_param` — re-run retrieval against the prod index + regenerate (medium)
  - `index_affecting` — build a temporary candidate OpenSearch index (re-parse / re-chunk /
    re-embed), run both sides, tear down (expensive; `cost_class: "high"`)
- **Baseline rule:** the comparison arm is a baseline executed contemporaneously against the
  current corpus — never the historical answers recorded in spans (corpus and model drift
  would confound the comparison).
- Comparison block in `summary`: paired test (Wilcoxon) + effect size (Cliff's delta) +
  a single promotion `verdict`.
- Paired significance testing requires datasets of ~100+ items; a 10-item set cannot support it.

### A.2 Parser version comparison (extends `parse_run`)

> **Status: reserved, not implemented.** The implemented `parse_run` scores **one** parser
> version. Comparing two (baseline parser vs. candidate) is the reserved extension — the
> data-scientist team's "comparison later" need.

**Plain language:** before the ingestion team ships a parser change, run the 593-file set
through both the current parser and the candidate parser, score both against the same ground
truth, and report whether the candidate is better, worse, or a wash — per metric and per
document type.

**Constraints that would apply:**
- Same `parse_run` target with two `parser` versions (a `baseline` and a `candidate`), against
  the same immutable `dataset_id` and the same comparator version.
- Both parser versions must be reachable via the §8.5 ingestion contract.
- Comparison block in `summary`: per-metric and per-document-type deltas, so a parser change
  that helps tables but hurts reading order is visible rather than hidden in an average.

---

## Open Questions

1. **Execution model for API-triggered runs (§11):** one-shot K8s Jobs (Option A) or a run
   queue + worker pool (Option B)? Decide on measured submission volume. Nightly stays a CronJob
   either way.
2. Push notifications for consuming teams (a completion event or webhook) vs poll-only?
3. Nightly run failure alerting: who is paged if the 00:00 CronJob errors or the span-contract
   violation rate spikes?
4. Per-project rate-limit numbers for `POST /runs` — pending Bedrock judge quota figures.
5. Is `judge_rationale` consumed verbatim by the UI? If so it becomes contractual and
   judge-prompt/template changes need a deprecation note.
6. Golden-set labeling ownership: who writes expected answers for promoted failures, and what
   is the review step before an item enters `ds_legal_rag_bench@N+1`?
7. **Parser invocation contract (§8.5):** does the eval worker call the ingestion parser
   directly per file, or submit through ingestion's async ingest pipeline? Needs the ingestion
   team. Determines the worker's call path and the `sa-eval-worker` grant.
8. **Parser concurrency/capacity:** what in-flight limit keeps a 593-file run from swamping the
   shared parser, and does ingestion need to reserve parser capacity for large eval runs?
9. **Parse comparator: shared library guarantee.** Confirm the CI wheel and the service worker
   import the *same* versioned comparator (not two copies that can drift). If they're separate
   today, unifying them is a prerequisite for CI and full-run scores to be comparable.
10. Parse metric set + thresholds (§6.3) — to be finalized with the data-scientist team.