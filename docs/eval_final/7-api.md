# Evaluation Service — API Surface (v1)

> Companion to the ingestion Solution Design. Written to read as a sibling of ingestion §3 "API Surface".
> Scope: the REST control plane and the nightly scoring flow. The primary triggers (the midnight
> schedule and ingestion completion events) are **not** HTTP — they arrive via EventBridge → SQS.
> This API and the event/schedule paths are two doors into the same SQS lanes, one worker fleet,
> one idempotency mechanism, one results store.
>
> The worker evaluation library is **DeepEval**. Metrics divide into reference-free (gradeable on
> raw production traffic) and ground-truth (require a labeled golden dataset) — see §6. Replay
> (`target.type: "rag_pipeline"`) is reserved in the schema but not implemented — see Appendix A.

---

## 1. What this service does (plain language)

Every midnight, the service takes a 5% sample of yesterday's real production traffic — the
questions users actually asked, the chunks the system retrieved, and the answers it gave — and
grades them with an LLM judge (via DeepEval). Scores land in our results database and are also
written back onto the traces in Phoenix, so the team sees quality scores next to the traces
they already browse.

Separately, on demand, the service can grade a **golden dataset** — a small set of questions
where we have written down the ideal answer — which unlocks the two metrics that need a
"right answer" to compare against (contextual precision and recall).

The REST API exists so other teams can **read** these results (dashboards, CI checks, the UI)
and **trigger** the handful of runs that have no schedule or event behind them (golden-set
runs, backfills, ad-hoc datasets). The API never performs evaluation work synchronously.

---

## 2. Principles

1. **Async jobs only.** No endpoint performs evaluation work synchronously. `POST` endpoints
   validate, persist the run record, enqueue to SQS, and return `202` with a `run_id`.
   Callers poll `GET /v1/eval/runs/{run_id}`. (Pattern: NeMo Evaluator / OpenAI Evals.)
2. **Schedule/event-in, API-out.** The dominant flow (nightly scoring) is triggered by an
   EventBridge schedule, not HTTP. The API exists primarily for reads and for human/CI-initiated
   runs that have no schedule or event behind them.
3. **One run schema.** A `target.type` discriminator (`dataset` | `model`) covers all current
   lanes. (`rag_pipeline` for replay is reserved in the schema but not implemented — see Appendix A.)
4. **Reference-free on raw traffic; ground truth only on labeled datasets.** The API
   mechanically rejects ground-truth metrics on span sources (422). See §6.
5. **S3 is the durable tier.** Phoenix holds the working set under a retention policy;
   anything evaluation needs long-term (datasets, golden sets, artifacts) is materialized to
   S3 as immutable, content-hashed snapshots. See §8.3.
6. **Versioned path.** Everything under `/v1/`. The generated `openapi.json` is a published,
   CI-diffed artifact; breaking changes require `/v2`.
7. **Same auth story as ingestion.** JWT Bearer via the shared identity provider; `project_id`
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
- The nightly scoring job is **not** created via this API. EventBridge Scheduler drops a message
  onto the SQS lane at 00:00; the worker creates the run record itself. Nightly runs appear in
  `GET /v1/eval/runs` with `"initiated_by": "schedule"` so consumers see one unified run history.
- `initiated_by` values: `schedule` (nightly), `event` (ingestion completion), `api` (human/CI).

---

## 4. Auth & Middleware

Identical middleware chain to ingestion-api: `JWTAuthMiddleware` → `ScopeMiddleware` →
`RequestLoggingMiddleware` (structured JSON, `X-Trace-Id` echoed).

**Claims shape** (same JWKS, same issuer/audience as ingestion):

```json
{
  "sub": "user-or-service-uuid",
  "project_id": "uuid",
  "bu_id": "uuid",
  "permissions": ["eval:read", "eval:write", "eval:admin"],
  "exp": 1234567890
}
```

| Scope | Grants |
|---|---|
| `eval:read` | All GET endpoints |
| `eval:write` | Submit/cancel runs (`dataset` and `model` targets) |
| `eval:admin` | Register configs and materialize datasets |

(`eval:replay` is reserved for the future replay capability — Appendix A.)

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

(**`rag_pipeline`** — replay against a candidate system — is reserved and not implemented.
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

The worker evaluates with **DeepEval**. Metrics divide by what they require:

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

### 6.1 The two recurring runs

**Nightly online scoring (schedule-triggered, no API call):**
- 00:00 EventBridge → SQS → worker
- Source: `span_window`, previous 24h, `sample_rate: 0.05`, Phoenix project `legal-rag-prod`
- Metrics: `answer_relevancy`, `faithfulness`, `contextual_relevancy`
- Outputs: results to RDS; scores + judge rationale written back to Phoenix as span
  annotations (`annotator_kind: "LLM"`); per-chunk contextual-relevancy scores written as
  document-level annotations on the retrieval span.

**Golden-set run (API- or CI-triggered):**
- `POST /v1/eval/runs` with `dataset_id: "ds_legal_rag_bench@1"` (s3_manifest source)
- Metrics: full set, including `contextual_precision` and `contextual_recall`
- Cadence: weekly, plus after any retrieval-affecting config change.

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

---

## 7. Endpoints

### 7.1 POST /v1/eval/runs

**Response — 202 Accepted:** the `EvalRun` object with `status: "queued"`.
**Response — 200 OK:** idempotent replay of an existing run (idempotency-key hit).

The handler does exactly four things, in order: validate → persist run record (RDS, single
transaction) → `SendMessage` to the lane's SQS queue → return. Aurora-then-SQS ordering and
partial-dispatch recovery are identical to ingestion §4.4.

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

## 8. Phoenix Integration

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

---

## 9. Conventions

- **Pagination:** opaque `cursor` + `limit` (max 200) on all list endpoints. No offset paging.
- **Errors:** uniform body `{ "error": { "code": "...", "message": "...", "trace_id": "..." } }`.
- **Rate limiting:** per-project token bucket on `POST /v1/eval/runs`; `429` carries
  `Retry-After`. Protects the shared Bedrock judge quota.
- **Timestamps:** RFC 3339 UTC. **IDs:** ULID-based, prefixed (`run_`, `cfg_`, `ds_`, `itm_`).
- **OpenAPI:** generated by FastAPI from the Pydantic models; CI exports `openapi.json` as a
  build artifact and fails on breaking diffs (`oasdiff` in `breaking` mode). Consuming teams
  generate clients from the published artifact.

---

## 10. Infrastructure (summary)

Mirrors ingestion-api deliberately:

```yaml
# eval-api Deployment
replicas: 2                     # HPA on CPU/request rate
resources:
  requests: { cpu: 250m, memory: 256Mi }
  limits:   { cpu: 500m, memory: 512Mi }
serviceAccount: sa-eval-api     # IRSA
```

- **Ingress:** AWS Load Balancer Controller, ALB `scheme: internal`, TLS at the ALB.
- **IAM (sa-eval-api):** `sqs:SendMessage` on eval lane queues only; `rds-db:connect` via
  RDS Proxy; `dynamodb` on idempotency + run tables; `secretsmanager:GetSecretValue`.
  **No** Bedrock, **no** Phoenix-write — the API never touches the model plane; only workers do.
- **Scheduler:** EventBridge Scheduler rule at 00:00 → SQS judge lane (nightly run message).
- **Probes:** `/healthz` liveness, `/readyz` readiness (RDS Proxy + SQS reachable).
- The API is stateless; all state in RDS, DynamoDB, S3.

---

## Appendix A — Reserved: Replay (not implemented)

> **Status: reserved in the schema, not implemented.** The discriminated union on
> `target.type` reserves `"rag_pipeline"` (and the `eval:replay` scope) so this can be added
> later without a breaking change. It is documented here only so the term "replay" is not
> confused with the nightly scoring job: nightly scoring **grades answers production already
> gave**; replay **re-asks old questions to a changed system that has not shipped yet**.

**Plain language:** before shipping a change (new prompt, new top-k, new chunking strategy),
take a frozen set of real questions, run them through both the current system and the changed
system, score both sides, and compare — so regressions are caught before users see them.

**Why the schema reserves it now.** Adding a new `target.type` later is non-breaking only if
the union is already a discriminated one and clients tolerate unknown enum values — both are
true here by design. The design constraints that would apply:

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

---

## Open Questions

1. Push notifications (`eval.run.completed` EventBridge event or webhook) for consuming teams,
   vs poll-only?
2. Nightly run failure alerting: who is paged if the 00:00 run errors or the span contract
   violation rate spikes?
3. Per-project rate-limit numbers for `POST /runs` — pending Bedrock judge quota figures.
4. Is `judge_rationale` consumed verbatim by the UI? If so it becomes contractual and
   judge-prompt/template changes need a deprecation note.
5. Golden-set labeling ownership: who writes expected answers for promoted failures, and what
   is the review step before an item enters `ds_legal_rag_bench@N+1`?