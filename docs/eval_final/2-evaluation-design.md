# Evaluation Service — Design v0.2: `genai-backend` Monorepo

The evaluation service is `genai-backend/services/eval/` — a sibling of ingestion, the orchestrator, and api-wrapper, sharing the repo's `libs/`, `contracts/`, and GitLab CI. The judge stack is **DeepEval on Bedrock**; experiment tracking is **Phoenix** (MLflow optional as a summary mirror). All crucible code is reused inside this service; doc-bench remains the one external evaluation component.

---

## 1. Where eval lives: `services/eval/`

```
genai-backend/
  services/
    eval/
      app/
        main.py                     # entrypoint dispatch: nightly CronJob body, or one-shot run Job (spec §11, Option A)
        api/                        # REST API per the API spec v1 — runs, results, configs, datasets (§4)
        runners/                    # the evaluation flows
          online_monitor.py         #   nightly CronJob: 5% span-window sample, reference-free RAG triad
          golden_set.py             #   golden-dataset scoring (s3_manifest source; + ctx precision/recall)
          parse_run.py              #   parser eval: calls the ingestion parser, scores via the comparator (doc-bench)
          replay.py                 #   RESERVED — `rag_pipeline` + parser comparison (spec Appendix A)
        kernel/                     # absorbed crucible code — pure, no infra imports (§3)
          rag_metrics/              #   faithfulness, answer relevancy, ctx precision/recall orchestration
          replay_stats/             #   paired Wilcoxon, Cliff's Delta, MDE
          spans/                    #   span parsing/handling primitives
          interfaces.py             #   RAGAdapter, JudgeProvider — implemented by deepeval/ and clients/
        deepeval/                   # DeepEval adapters: judge metrics wired to Bedrock
          bedrock_provider.py       #   JudgeProvider impl: dated model id, temp-0, pinned prompt versions
          token_bucket.py           #   Redis Lua per-model bucket; online-reserved share
        metrics/                    # metric aggregation, pass rules, faithfulness floor, per-doc floors
        stats/                      # gate-facing stats surfaces (thin wrappers over kernel/replay_stats)
        datasets/                   # gold-set handling (eval_questions), mustpass.yaml consumption
        spans/                      # S3 span-store reader + deterministic hash sampler (stratified)
        state/                      # run records, results, configs/datasets registry (RDS) + idempotency
                                    #   claims (Postgres-on-Phoenix vs DynamoDB — reconciliation in §4)
        phoenix/                    # Phoenix logging — runs/experiments/per-item scores (MLflow mirror optional)
        clients/                    # opensearch (read-only), s3, bedrock, phoenix — built on libs/clients
        schemas/                    # eval-internal Pydantic models (shared shapes live in contracts/, §2)
        config.py
      tests/
      Dockerfile                    # CPU image; surfaces selected by args (one image, N entrypoints)
      pyproject.toml                # pins: doc-bench==Y (the only external eval kernel — see §3)
```

What deliberately does **not** appear, and why that's consistent with the siblings:

- **No `workers/` (no SQS consumer).** Ingestion has `workers/` because it has a queue. Eval, per the architecture decision trail (§3 note), has no queue: the S3 span store is the durable buffer, and every surface is a bounded K8s Job/CronJob launched by GitLab or the cluster clock. The monorepo changes where the code lives, not the execution model.
- **`kernel/` is a hard internal boundary, not a folder name.** It is the absorbed crucible code and must stay pure: no imports from `runners/`, `state/`, `clients/`, `api/`, or anything AWS-touching — enforced by an import linter in CI (§3). Everything else in the service may import the kernel; the kernel imports nothing back.
- **No index-building pipeline.** Eval measures; ingestion owns the testing index (invariant 5). One amendment from the API spec: `parse_run` does **call the ingestion parser at runtime** (spec §8.5) — eval still builds nothing, but this is a live cross-service dependency with per-file error isolation and a bounded concurrency limit (see §4).

**Judge stack — DeepEval on Bedrock (settled).** The kernel orchestrates DeepEval metrics with the reliability techniques the architecture doc commits to: pinned G-Eval steps, decomposed QAG verdicts, temperature-0 judging, and the verdict cache keyed by `repro_key` (which includes the pinned `prompt_template_version` and dated judge model id).

**Tracking — Phoenix (settled; MLflow optional).** Per the API spec's storage tiering (§8.3, normative): **RDS is the results of record**; Phoenix is the retention-bounded working set and convenience view — scores and judge rationale are written back as span/document annotations so quality appears next to the traces the team already browses, and write-back failures never fail a run. S3 holds the durable, immutable, content-hashed tier (materialized datasets, golden sets, artifacts). A summary-metrics mirror to MLflow can be added cheaply if org dashboards need it — an additional sink, never a substitute.

---

## 2. Contracts: `genai-backend/contracts/` grows a `schemas/` sibling

The existing `contracts/openapi/` holds HTTP surfaces. The ingestion↔eval contract is mostly **data at rest**, not HTTP, so it gets a parallel home:

```
contracts/
  openapi/
    ...                             # existing; + eval-openapi.json if the §4 API is confirmed
  schemas/
    parser_output.schema.json       # moved from doc-bench vendoring (doc-bench remains a consumer)
    results_v1.schema.json
    span.schema.json                # OpenInference field profile ingestion's serving path must emit
    manifest.schema.json            # S3 artifact/manifest layout
    eval_questions.schema.json
    adoption_test_request.schema.json   # the Hurdle-2 handoff payload (arch §8.1 — now trivial to sign)
```

With both teams in one repo, a schema change plus both consumers' updates land in **one atomic MR**, with contract tests failing loudly on mismatch. Rules carried over from the prior design, because they protect data that outlives commits:

- **`schema_version` mandatory on every artifact and span; unknown major versions rejected loudly.** Git atomicity covers code; it does not cover the years of artifacts already sitting in S3.
- **Contract tests in `tests/contract/`** validate both producers (ingestion writes) and consumers (eval reads) against these files on every MR that touches either service or the schemas — `detect-changed-services.sh` should treat a `contracts/` change as touching *all* dependent services.
- **doc-bench (external, §3) consumes `parser_output`/`results_v1` as pinned published schema files** — the coupling-direction logic is unchanged, only the hosting moved to neutral ground.

---

## 3. Kernel boundaries: crucible is absorbed, doc-bench stays external

**Crucible ceases to exist as a separate repo/package.** All of its code is reused inside `genai-backend` — the kernel (RAG metrics, judge orchestration, replay statistics, span handling, the `RAGAdapter`/`JudgeProvider` interfaces) moves into `services/eval/app/kernel/`, and what would have been its service layer *is* the rest of `services/eval/`. There is no separate wheel, no pin, no separate release cadence. doc-bench is the only evaluation component remaining outside the monorepo:

| Component | Location | Rationale |
|---|---|---|
| **crucible (all of it)** | **absorbed** into `services/eval/app/kernel/` + the service code around it | One consumer (eval), one team, one repo: a separate package would add a release/pin cycle with no second consumer to justify it. The code is reused as-is; only its packaging identity disappears. Dead weight (the OpenAI default path, chromadb/Zvec demo backends, mandatory torch) is simply not carried across. |
| **doc-bench** | **stays external** — pinned wheel + image | Deterministic, model-free, its own cadence, ships baked benchmark datasets, and is used *standalone* (local, pre-commit, fast-CI wheel canary — arch §2a) in contexts that must not require cloning a backend monorepo. Versions pinned in `services/eval/pyproject.toml` and the gate pipeline. |

**What the absorption must not destroy — the kernel/service seam.** The one-way dependency ("kernel stays pure, no infra imports; service depends on kernel, never the reverse") was the load-bearing structure, and it survives as a *package boundary inside the service*: `app/kernel/` imports nothing from `app/runners/`, `app/state/`, `app/clients/`, or `app/api/` — enforced with an import linter as a required CI check. The seam is what keeps the metrics/statistics code unit-testable without AWS and reusable if a second GenAI product ever needs it — at which point the escape hatch is hoisting `app/kernel/` into `libs/`, a mechanical move, not a redesign.

**What is consciously given up:** the kernel's standalone CLI/research identity. Local runs happen from a monorepo checkout (a dev entrypoint into `app/kernel/`) rather than a pip-installable package. Acceptable: the only users are this team, who have the checkout anyway.

*Counter-position, named for review:* fold doc-bench in too. Rejected — `libs/` and `services/` are for code shared **within this repo**, while doc-bench is shared **across contexts outside it** (laptops, pre-commit, any repo's fast CI), and its baked datasets would bloat every monorepo checkout. Wrong axis of sharing.

---

## 4. The eval API surface — settled by the API spec (v1)

The previously-parked API question is now resolved by the dedicated API document (*Evaluation Service — API Surface v1*), which is **authoritative** for endpoint shapes, auth, schemas, and conventions. This section records how that spec lands against this design: what it confirms, what it adds, and the two reconciliations it opens.

**What the spec confirms (the design's constraints hold):**

- **Async-only.** No endpoint performs evaluation synchronously; `POST /v1/eval/runs` is persist-then-dispatch, `202` + poll. The execution-model recommendation (spec §11, Option A: one-shot K8s Jobs, no queue, no standing worker pool) matches this design's bounded-Jobs model exactly — the API pod is the one always-on Deployment; everything that evaluates remains a finite Job or the nightly CronJob.
- **The schedule owns the nightly.** The CronJob creates its own run record and is not created via the API; the API exists for reads and for runs with no schedule behind them (golden-set, parser full runs, backfills). Nightly runs surface in `GET /v1/eval/runs` with `initiated_by: "schedule"` — one unified run history without HTTP becoming a second trigger path for scheduled work.
- **Gates and paired comparisons are not exposed in v1.** RAG replay (`target.type: "rag_pipeline"`, scope `eval:replay`) and parser baseline-vs-candidate comparison are **reserved, not implemented** (spec Appendix A). v1 exposes *measurement* runs only; promotion verdicts stay off the HTTP surface — consistent with invariant 1 ("no second adoption route around the merge gate"). When comparisons arrive, they come as new target types with their own scope, not as mutations of existing ones.
- **IAM split keeps the model plane off the API.** `sa-eval-api` has **no Bedrock and no Phoenix write**; only the worker/CronJob role touches the judge or the trace store. This is the same blast-radius discipline the design wanted, now concrete.

**What the spec adds beyond the earlier "thin API" sketch — accepted:**

- **Configs and datasets as first-class, immutable-versioned admin endpoints** (`eval:admin`). This is where the repro-key discipline becomes *API-enforceable* rather than conventional: `config_id` pins judge model + prompt version + thresholds; dataset materialization is the single PII-redaction chokepoint and produces the content hashes the repro key is verified against. That earns the extra surface.
- **Operational hardening:** cancel, client idempotency keys (mapped onto the same conditional-claim mechanism internally), per-project rate limiting protecting the shared Bedrock judge quota, and the `oasdiff` breaking-change gate on the published `openapi.json` — which slots into `contracts/openapi/eval-openapi.json` (§2) as the generated artifact other teams build clients from.

**Two reconciliations the spec opens (flag in review, don't paper over):**

1. **Persistence stack.** The spec assumes **RDS + DynamoDB (idempotency) + S3**, explicitly marked *[verify]*. This design's data tier put `claims` / `eval_runs` / `comparison_reports` in **Postgres riding on Phoenix's instance**, with DynamoDB as the written fallback (arch §3a). These must converge to one answer: either the spec's *[verify]* resolves to the Postgres claims table (its "DynamoDB conditional-claim" wording maps one-to-one onto the Postgres conditional insert + lease), or the architecture fallback fires and `claims` moves to DynamoDB — which the spec's RDS-Proxy/IRSA sections already assume. One decision, both documents updated together; do not ship with the two docs naming different stores for the same lock.
2. **The comparator library *is* doc-bench.** The spec's "one shared, versioned comparator" used by both the CI wheel and the 593-file service run — and its Open Question 9 ("confirm the wheel and the worker import the *same* library, not two copies that can drift") — is exactly the role §3 assigns to the external doc-bench wheel. Confirming they are the same pinned artifact closes OQ9 *by construction*: one wheel, imported in CI and by the `parse_run` worker, its version a component of the repro key.

**One amendment to §1's sibling notes:** the spec makes `parse_run` a hard runtime dependency on the **ingestion parser** (spec §8.5 — invocation contract open: direct call vs. ingestion's async pipeline). Eval still builds no index and owns no reingest, but it now *calls* an ingestion-owned service at runtime — the first live cross-service dependency, with per-file isolation and a bounded concurrency limit so a 593-file run can't swamp the shared parser.

---

## 5. Shared `libs/` adoption

Eval stops hand-rolling what the monorepo already provides — and review should check eval *actually* imports these rather than growing parallel copies:

- `libs/observability` — OTel/logging; replaces eval-local telemetry helpers. (Eval's span *consumption* for evaluation is separate from its own *emission* of operational telemetry; both should use the same OpenInference conventions, which this lib is the natural home for.)
- `libs/auth` — JWT/principal for the §4 API surface.
- `libs/schemas` / `libs/clients` / `libs/errors` — shared Pydantic types, HTTP client utilities, common error envelope.
- `libs/audit` — gate verdicts and adoption decisions are audit-worthy events; emit them through the shared writer rather than inventing an eval-local audit trail.

---

## 6. CI/CD in the monorepo (GitLab)

The promote-by-digest discipline maps onto the monorepo pipeline as follows:

- **Changed-service detection:** `scripts/detect-changed-services.sh` decides which service images build on an MR. Amend it so changes under `contracts/` rebuild + contract-test **every** service that consumes the changed schema, and changes to the pinned `doc-bench` version in `services/eval/pyproject.toml` rebuild eval.
- **Per-MR (eval-touching):** lint, type-check, unit + contract tests, the **kernel import-linter check** (§3 — `app/kernel/` must import nothing from the service layer), build `services/eval` image. The doc-bench **wheel canary** (arch §2a) runs here in seconds when parser-adjacent code moved — unchanged.
- **Gate pipelines:** unchanged from the architecture doc — `merge_request_event` pipelines launch the Hurdle-1 Indexed Job and (via ingestion's pipeline + `strategy: depend`) the Hurdle-2 adoption-test Job; required checks green ⇒ mergeable; **main is production** semantics untouched. The monorepo simplifies the cross-team handoff: ingestion's reingest pipeline and eval's gate now live in *one* `.gitlab-ci.yml` lineage instead of a cross-project trigger contract — the `adoption_test_request` schema (§2) still governs the payload so the seam stays explicit.
- **Promotion:** the exact image digest tested in the gate run is what deploys; environment differences live in overlays/values, never baked into images. Migrations (the three eval tables) run per environment ahead of rollout, expand-contract.

---

## 7. What is explicitly unchanged (so review doesn't re-litigate it)

1. **The organising boundary** — deterministic (doc-bench, hard-gate-safe) vs probabilistic (Bedrock judge: token bucket, residency, calibration, verdict cache, spend controls) — arch §1.
2. **The two-hurdle, pre-merge adoption flow**; eval measures, ingestion owns the testing index and the RAG gate; merge = adoption — arch §4, invariants §5.
3. **The data tier's principles**: idempotency claims gating every paid judge call (lease, steal, cross-run caching); a relational results-of-record; S3 as the immutable evidence locker; no queue (the spec's §11 Option A keeps it that way). The *store* for the claims is the one open reconciliation — §4 item 1.
4. **Execution model**: bounded Jobs + one CronJob on the shared EKS platform; scale-from-zero; Karpenter caveat in env 3 stands — arch §3/§3b.
5. **Non-negotiable invariants 1–5** (arch §5) verbatim — with the single softening noted in §4 (one read-mostly API pod) if that decision lands as recommended.
6. **The shoring plan** (arch §8) — and item 8.1 gets materially easier: the span-schema and `adoption_test_request` sign-offs become files in `contracts/schemas/` that ingestion approves in an MR, not cross-team document exchanges.

---

## 8. Review checklist

1. **§4 — persistence reconciliation:** one answer for the idempotency claim store — Postgres-on-Phoenix (arch §3a) or DynamoDB (API spec assumption, marked *[verify]*) — and update both documents together.
2. **§4 — comparator = doc-bench:** confirm the spec's "shared, versioned comparator" (its Open Question 9) is the pinned doc-bench wheel, imported by both the CI check and the `parse_run` worker, with its version in the repro key.
3. **§3 — kernel boundaries:** ratify crucible absorption into `services/eval/app/kernel/`, confirm the import-linter seam is a required CI check, and confirm the CLI trade-off (kernel runnable only from a monorepo checkout) is acceptable. Ratify doc-bench staying external as a pinned wheel + image, with the counter-position from §3 on the table.
4. **§2 — schema migration:** approve moving `parser_output`/`results_v1` from doc-bench vendoring into `contracts/schemas/` (doc-bench becomes a consumer at a pinned version), adding the span / manifest / `eval_questions` schemas alongside, and publishing the generated `eval-openapi.json` (with the `oasdiff` breaking-change gate) into `contracts/openapi/`.
5. **§1 — tracking:** Phoenix is the convenience view (annotations write-back, retention-bounded working set per spec §8.3); RDS is the results of record; decide whether an MLflow summary mirror is needed for org dashboards (optional, additive only).
6. **Open items inherited from the API spec:** execution model Option A vs B on measured volume (spec §11/OQ1); parser invocation contract (spec OQ7) and concurrency/capacity (OQ8); golden-set growth ownership (OQ6); parse metric set + thresholds (OQ10). Plus the reserved comparisons (spec Appendix A) — when implemented, they carry the paired-stats machinery (Wilcoxon, Cliff's delta) and the `eval:replay` scope, and remain off the v1 surface until then.