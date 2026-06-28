# Evaluation Service — Detailed Design (Components, Code & Data)

*The engineering layer below the Architecture Design: repo internals, code seams, GitLab CI wiring, the Postgres schema, the claims protocol, and the end-to-end write path. Everything here implements decisions recorded in the Architecture Design; nothing here re-opens them. Status markers: **[PROPOSED]** = invited for review · **[FORK]** = open decision, both options specced · **[PARKED]** = pruned with a named return trigger.*

---
## 1. Repo internals

### 1.1 `eval-rag` — two layers, one image, imports point one way

```
eval-rag/
├── pyproject.toml                  # requires-python >= 3.13; uv-managed
├── uv.lock
├── src/
│   ├── crucible/                   # ── KERNEL (pure, CLI-runnable, no infra imports)
│   │   ├── metrics/                #    DeepEval wiring: faithfulness, answer_relevancy,
│   │   │                           #    ctx_precision, ctx_recall (pinned evaluation_steps)
│   │   ├── judge/                  #    JudgeProvider protocol + score normalisation,
│   │   │                           #    QAG claim-extraction, self-consistency re-run
│   │   ├── replay/                 #    paired stats: Wilcoxon signed-rank, Cliff's Delta
│   │   ├── spans/                  #    span parsing/validation against the span schema
│   │   ├── datasets/               #    eval_questions loading (incl. adversarial flag)
│   │   └── adapters.py             #    RAGAdapter / JudgeProvider Protocols (interfaces only)
│   └── eval_service/               # ── SERVICE LAYER (depends on crucible; never the reverse)
│       ├── run.py                  #    single entrypoint: --surface adoption|online|replay|calibration
│       ├── surfaces/               #    one module per surface (thin: select items → process loop)
│       ├── adapters/
│       │   ├── bedrock_judge.py    #    JudgeProvider impl: Bedrock converse, au inference profile,
│       │   │                       #    temperature 0, judge-model ≠ generator-model enforced here
│       │   ├── opensearch_ro.py    #    read-only RAG retrieval (testing index or prod read replica)
│       │   ├── phoenix_client.py   #    experiments + per-item score logging
│       │   └── s3_evidence.py      #    content-addressed evidence writes
│       ├── claims/                 #    the spend lock (§4) — acquire / steal / finalize / release
│       ├── budget/                 #    token bucket client + per-run circuit breaker
│       ├── sampling.py             #    deterministic 5% hash sampler
│       ├── reporting/              #    comparison_reports writer; gate exit-code mapping
│       └── db/                     #    SQLAlchemy models for the 3 tables + Alembic migrations
├── contracts/                      #    vendored, versioned (PARKED hoist — see Architecture §2):
│   ├── span.schema.json            #    the serving-path recording contract
│   ├── manifest.schema.json
│   ├── eval_questions.schema.json
│   └── adoption_test_request.md    #    Hurdle-2 handoff variables (§3.3)
├── deploy/
│   ├── job-templates/              #    adoption.yaml · replay.yaml · calibration.yaml (Job)
│   ├── cron/online-nightly.yaml    #    the one CronJob (ArgoCD-synced)
│   └── rbac/                       #    launch-job ServiceAccount, eval-namespace Role
├── tools/launch-job                #    thin kubectl create+wait helper used by CI (§3.4)
├── .gitlab-ci.yml
└── importlinter.cfg                #    enforces kernel ⟂ service (below)
```

**The one-way import rule is enforced, not requested** — `import-linter` runs in CI and fails the build if the kernel ever imports the service layer:

```ini
# importlinter.cfg
[importlinter]
root_packages = crucible, eval_service

[contract:kernel-is-pure]
name = crucible never imports eval_service or infra SDKs
type = forbidden
source_modules = crucible
forbidden_modules = eval_service, boto3, kubernetes, opensearchpy, sqlalchemy
```

This is what keeps crucible CLI-runnable on a laptop (stub adapters, no AWS) while the same code judges production runs.

### 1.2 The two kernel interfaces (the seam everything plugs into)

```python
# src/crucible/adapters.py — interfaces live in the kernel; implementations in the service layer
from typing import Protocol

class RAGAdapter(Protocol):
    """Produce (retrieved_context, answer) for a query — against whatever index the run targets."""
    def query(self, question: str) -> RAGResult: ...          # RAGResult: context chunks + answer + ids/hashes

class JudgeProvider(Protocol):
    """Score one (question, context, answer[, expected]) tuple on one metric. Deterministic config."""
    model_id: str                                              # dated Bedrock model id — part of repro_key
    prompt_version: str                                        # pinned evaluation_steps version — part of repro_key
    def evaluate(self, metric: str, case: LLMTestCase) -> Verdict: ...   # Verdict: score, passed, raw, token_usage
```

Kernel ships `StubRAGAdapter` / `RecordedSpanAdapter` (replay Case A reads the span's recorded context — no retrieval at all) and a `FixtureJudge` for tests. The service layer ships `OpenSearchReadOnlyAdapter` and `BedrockJudge`. **`BedrockJudge` refuses to construct if `model_id` shares a family with the generator model recorded on the spans under test** — judge ≠ generator is a constructor-time assertion, not a convention.

### 1.3 `doc-bench` — unchanged surfaces, version-pinned contracts

doc-bench keeps its own repo, CI, and release cadence. What this design consumes:

| Surface | Form | Used by |
|---|---|---|
| wheel | `pip install doc-bench==X.Y.Z` — runs the named must-pass set in seconds, no network | local dev, pre-commit, fast CI canary (never a gate) |
| image | `doc-bench:X.Y.Z` — full benchmark, sharded | the Hurdle-1 **Indexed Job** (`JOB_COMPLETION_INDEX` selects the doc shard) |
| `parser_output.schema.json`, `results_v1.schema.json` | vendored in doc-bench, **explicit `schema_version`, published with the release** | Ingestion conforms to the pinned version; eval validates against it |

The wheel's must-pass document list is a version-pinned file (`mustpass.yaml`: doc ids + per-doc floor thresholds) — **the same file Hurdle 1 reads** to apply the per-document floor inside the full benchmark, which is how "the wheel alarms, the gate enforces" is one list, not two.

---

## 2. Execution surfaces — one image, four entrypoints

The eval-rag image is CPU-only, multi-stage, non-root, pinned by digest:

```dockerfile
# syntax=docker/dockerfile:1.7
FROM python:3.13-slim@sha256:<pin> AS builder
RUN pip install --no-cache-dir uv==0.5.*
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY src/ ./src/ 
COPY contracts/ ./contracts/
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

FROM python:3.13-slim@sha256:<pin>
RUN groupadd -r app && useradd -r -g app -u 10001 app
COPY --from=builder --chown=app:app /app /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER 10001
WORKDIR /app
ENTRYPOINT ["python", "-m", "eval_service.run"]
# surface chosen at launch: --surface adoption|online|replay|calibration
```

| `--surface` | Launched by | Reads | Writes |
|---|---|---|---|
| `adoption` | Ingestion's pipeline (Hurdle 2), via `launch-job` | testing index (RO) + `eval_questions` gold set | Phoenix experiment, S3 evidence, `eval_runs`, `comparison_reports` |
| `online` | the nightly **CronJob** | previous-day window of the S3 span store, 5% sampled at read time | Phoenix annotations on sampled spans, S3 evidence (per-customer KMS), `eval_runs` |
| `replay` | manual pipeline (`launch-job`) | recorded spans (Case A: recorded context; Case B/C: **[FORK]**, see §7) | Phoenix experiment, `comparison_reports` |
| `calibration` | scheduled pipeline (weekly) | human-labelled gold subset + adversarial trick examples | κ + self-consistency metrics → Phoenix + CloudWatch |

All four are bounded: select the item set up front, process, exit. No loop waits for new work — there is no queue to wait on.

---

## 3. GitLab CI wiring (the orchestration, concretely)

### 3.1 Hurdle 1 — `parsing-gate` in the pipeline repo's MR pipeline

```yaml
# .gitlab-ci.yml (the repo where parser code lives)
parsing-gate:
  stage: gates
  rules:
    - if: '$CI_PIPELINE_SOURCE == "merge_request_event"'
      changes:
        - parsers/**/*
        - parsing-config/**/*
  image: $ECR/eval-tools:$TOOLS_TAG          # contains tools/launch-job + kubectl
  script:
    - launch-job
        --template docbench-full             # deploy/job-templates → Indexed Job, completions=10
        --set CANDIDATE_REF=$CI_COMMIT_SHA
        --set BASELINE_REF=$BASELINE_PARSER_VERSION
        --set MUSTPASS=mustpass.yaml@$DOCBENCH_VERSION
        --wait --stream-logs
  # exit code IS the gate: 0 = pass (incl. per-doc floor), 1 = fail, 2 = infra error (retryable)
```

Routing is by `rules:changes` — *the change selects its gates*; a prompt-only MR matches a different rule set that skips `parsing-gate` and runs the RAG comparison directly (same mechanism, different template).

### 3.2 Hurdle 2 — multi-project handoff to Ingestion

```yaml
rag-gate:
  stage: gates
  needs: [parsing-gate]                       # candidacy first
  rules: [ { if: '$CI_PIPELINE_SOURCE == "merge_request_event"', changes: [ "parsers/**/*" ] } ]
  trigger:
    project: platform/ingestion-pipeline      # Ingestion's repo — they own this pipeline
    strategy: depend                          # our MR pipeline blocks on their result
  variables:                                  # == adoption_test_request v1 (contracts/)
    ADOPTION_TEST: "true"
    CANDIDATE_REF: $CI_COMMIT_SHA
    CORPUS_SCOPE: $ADOPTION_CORPUS_ID         # what they reingest
    EVAL_QUESTIONS_VERSION: $GOLD_SET_VERSION
    BASELINE_INDEX_REF: $CURRENT_PROD_PARSER_VERSION
```

Inside Ingestion's pipeline: *their* jobs build the testing index from `CANDIDATE_REF`, then a final job calls eval's `launch-job --template adoption` passing the index endpoint + embedding identity (model, dimension, space type — eval hard-errors on mismatch). `strategy: depend` propagates their pass/fail back into the MR's required checks — **the merge gate without any bespoke callback machinery**. *(Ownership of this pipeline is the [PROPOSED] split being confirmed with Ingestion.)*

### 3.3 `adoption_test_request` v1 — the handoff variables (the one new cross-team shape)

| Variable | Meaning | Validated by |
|---|---|---|
| `CANDIDATE_REF` / `BASELINE_INDEX_REF` | what's under test vs what it's compared to | eval (must differ) |
| `TESTING_INDEX_ENDPOINT`, `TESTING_INDEX_NAME` | where Ingestion built the candidate index | eval (reachability, RO creds) |
| `EMBED_MODEL_ID`, `EMBED_DIM`, `SPACE_TYPE` | index identity — must match how it was built | eval (**hard error on mismatch**) |
| `EVAL_QUESTIONS_VERSION`, `CORPUS_SCOPE` | pins the question set and corpus across both arms | eval |

### 3.4 `launch-job` — the only custom orchestration code (~80 lines)

`kubectl create -f <rendered template>` → `kubectl wait --for=condition=complete|failed --timeout=...` → stream logs → map Job status to exit code. The CI runner's ServiceAccount is bound to a Role that can **create/get/watch Jobs in the `eval` namespace only**; each Job template names its own per-surface ServiceAccount (IRSA), so the runner can launch work but holds no AWS permissions itself. *(Superseded the day the platform orchestrator provides launch/wait natively — Architecture §3b open question 1.)*

### 3.5 The nightly CronJob (the clock, not CI)

```yaml
apiVersion: batch/v1
kind: CronJob
metadata: { name: eval-online-nightly, namespace: eval }
spec:
  schedule: "0 0 * * *"                       # midnight AEST (cluster TZ)
  concurrencyPolicy: Forbid
  startingDeadlineSeconds: 3600
  suspend: false                              # flip true during incidents — a PR, since ArgoCD-synced
  jobTemplate:
    spec:
      backoffLimit: 1
      ttlSecondsAfterFinished: 86400
      template:
        spec:
          serviceAccountName: eval-online     # IRSA: S3 span store RO + evidence RW, Bedrock, Phoenix
          restartPolicy: Never
          containers:
            - name: online
              image: $ECR/eval-rag@<digest>
              args: ["--surface", "online", "--window", "previous-day"]
              resources: { requests: { cpu: "1", memory: "2Gi" }, limits: { cpu: "1", memory: "2Gi" } }
```

---

## 4. Data model **[PROPOSED — the claims table is the reviewed piece; survey in Architecture §3a]**

### 4.1 Where every kind of data lives (one row per kind — no overlaps)

| Data | Store | Key/shape |
|---|---|---|
| raw judge verdicts (evidence) | S3 | `evidence/<rk[:2]>/<repro_key>.json` — content-addressed, write-once |
| sampled production content (online) | S3 | `online-evidence/customer=<id>/dt=<date>/...` — **SSE-KMS, per-customer key** (erasure = key deletion) |
| per-item × per-metric scores | **Phoenix** | experiment runs (adoption/replay) · span annotations (online) |
| permanent score archive | S3 Parquet | `archive/scores/dt=<YYYY-MM>/part-*.parquet` — Phoenix is rebuildable from this |
| spend lock / verdict cache | Postgres `claims` | PK `repro_key` |
| run bookkeeping & provenance | Postgres `eval_runs` | GitLab pipeline ↔ Phoenix experiment join |
| gate verdicts | Postgres `comparison_reports` | PK `(run_id, metric)` |
| gold sets, snapshots, spans | S3 | `gold/<set>/<version>/` · `snapshots/<content_hash>/` · `spans/dt=<date>/` (written by serving path — the contract) |

### 4.2 The reproducibility key (what makes a paid verdict cacheable forever)

```python
def repro_key(item_id: str, input_hash: str, spec: EvalSpec) -> str:
    """Identical inputs + identical ruler ⇒ identical key ⇒ the verdict is bought once, ever."""
    material = "\x1f".join([
        item_id,
        input_hash,                  # sha256 of (question ∥ context ids+hashes ∥ answer)
        spec.eval_spec_version,      # bumping this re-baselines everything — deliberate
        spec.judge_model_id,         # dated Bedrock id, e.g. ...-20260115
        spec.judge_prompt_version,   # the pinned evaluation_steps version
        spec.decoding_params,        # "temp=0,top_p=1"
        spec.normaliser_version,
    ])
    return hashlib.sha256(material.encode()).hexdigest()
```

The baseline arm of every comparison is computed **once, ever** — every subsequent MR's `rag-gate` finds the baseline verdicts already in `claims` and pays only for the candidate arm. That cache-across-runs property is condition (3) of why `claims` exists.

### 4.3 DDL — the three control tables (full schema; this is all of it)

```sql
-- 1) claims: spend lock + verdict cache. ~99% of all rows. PK = the idempotency key.
CREATE TABLE claims (
    repro_key         TEXT PRIMARY KEY,
    status            TEXT NOT NULL CHECK (status IN ('leased','done','failed')),
    lease_owner       TEXT,                    -- pod name; NULL once done
    lease_expires_at  TIMESTAMPTZ,             -- NULL once done
    attempt           SMALLINT NOT NULL DEFAULT 1,
    result_ref        TEXT,                    -- s3://evidence/... once done
    score             DOUBLE PRECISION,        -- denormalised for cheap cache hits
    passed            BOOLEAN,
    eval_spec_version TEXT NOT NULL,           -- prune key: retire a spec ⇒ delete its claims
    cost_usd          NUMERIC(10,6) DEFAULT 0,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at       TIMESTAMPTZ
);
CREATE INDEX claims_spec_idx  ON claims (eval_spec_version);            -- pruning
CREATE INDEX claims_lease_idx ON claims (lease_expires_at) WHERE status = 'leased';  -- steal scan

-- 2) eval_runs: one row per run — the GitLab↔Phoenix provenance join.
CREATE TABLE eval_runs (
    run_id              UUID PRIMARY KEY,
    surface             TEXT NOT NULL CHECK (surface IN
                          ('docbench_full','adoption','online','replay','calibration')),
    gitlab_pipeline_id  BIGINT,                -- NULL for the CronJob-fired nightly
    gitlab_mr_iid       BIGINT,
    phoenix_experiment  TEXT,
    target_ref          TEXT NOT NULL,         -- candidate commit / parser version / window date
    baseline_ref        TEXT,
    dataset_version     TEXT,
    eval_spec_version   TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'running'
                          CHECK (status IN ('running','succeeded','failed','partial')),
    items_total         INT, items_scored INT, items_errored INT,       -- coverage, always reported
    budget_usd          NUMERIC(10,2),         -- circuit-breaker ceiling for this run
    cost_usd            NUMERIC(10,2) DEFAULT 0,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at         TIMESTAMPTZ
);
CREATE INDEX eval_runs_surface_started_idx ON eval_runs (surface, started_at DESC);  -- dead-man query

-- 3) comparison_reports: the gate verdicts — what evidence made what decision.
CREATE TABLE comparison_reports (
    run_id          UUID NOT NULL REFERENCES eval_runs(run_id),
    metric          TEXT NOT NULL,             -- faithfulness, answer_relevancy, nid, teds, ...
    n               INT NOT NULL,
    baseline_mean   DOUBLE PRECISION,
    candidate_mean  DOUBLE PRECISION,
    wilcoxon_p      DOUBLE PRECISION,          -- paired, per-item deltas; ONE-SIDED in no-regression mode
    cliffs_delta    DOUBLE PRECISION,
    mde             DOUBLE PRECISION,          -- minimum detectable effect at 80% power given n and observed σ_d
                                               -- — the gate's sensitivity, published with every verdict (§4.3b)
    floor_breached  BOOLEAN NOT NULL DEFAULT FALSE,   -- faithfulness floor / must-pass doc
    decision        TEXT NOT NULL CHECK (decision IN ('pass','fail','inconclusive')),
    policy_version  TEXT NOT NULL,             -- 'no-regression-v1' until the significance bar lands
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, metric)
);
```

Scale honesty (Architecture §3a): ~3M rows/yr, ~0.5 GB, 99% in `claims` — **no partitioning**; the only hygiene is `DELETE FROM claims WHERE eval_spec_version = :retired`.

### 4.3a If review pushes back: "you don't need these tables"

The framing answer first: **the Postgres instance already exists** (Phoenix requires it), so the choice is never "database vs no database" — it's *three tables vs three bespoke S3-JSON conventions, Athena queries to read them back, and a lock built somewhere anyway*. Tables are the smaller footprint. Then per table, in descending order of how hard we hold:

| Table | Steelmanned pushback | Why it stays | Fallback if conceded — and its named price |
|---|---|---|---|
| `claims` | "just re-run; or use S3/Redis" | A gate run is ~100 questions × 4 metrics × 3–5 Bedrock calls ≈ **1,200–2,000 paid calls per arm**; without cross-run caching the **baseline arm is re-bought on every MR, forever**; without the lease, a Spot kill at item 900 re-bills 900, and GitLab retry + Job `backoffLimit` can run the same work twice **concurrently** (double-bill + double-log). S3 conditional writes can't lease/steal; Redis eviction silently re-bills the cache half (full survey: Architecture §3a). | Per-run resume only: S3 checkpoint manifest. Keeps Spot safety; **loses baseline reuse — price = one full baseline arm per MR, recurring**. |
| `eval_runs` | "Phoenix already has runs" | The columns are deliberately the **non-Phoenix residue**: GitLab pipeline/MR ids (Phoenix doesn't know GitLab exists), `budget_usd`/`cost_usd` for the mid-run circuit breaker, coverage counts, and the dead-man probe ("no online run in 26 h") as one indexed query. | `run-summary.json` per run in S3 + CloudWatch heartbeat for the dead-man alarm. **Price: ad-hoc SQL over runs becomes Athena queries.** Livable. |
| `comparison_reports` | "GitLab already records pass/fail" | GitLab records *that* it failed; this records **the evidence** — n, paired p, Cliff's Δ, which floor breached, and `policy_version` (which decision rule was in force). For a legal-data product, "show exactly what evidence gated the parser shipped in March" must not answer with an expired CI log. | `comparison.json` per run in S3 (already written as the snapshot); the table is just the queryable index over those files. Cheapest to keep; has the compliance constituency. |

**Concession ladder** (each step has a named price; never jump): (1) all three on the existing instance — *this design* → (2) `claims` only; runs/reports to S3 JSON + Athena + heartbeat → (3) `claims` to DynamoDB (the written fallback if Phoenix's instance ever disappears) → (4) no cross-run cache, per-run S3 checkpoints — stated recurring dollar cost per MR.

### 4.3b Gate power — the MDE is published, not assumed

The honest vulnerability of "no-regression mode": the gate passes anything *not measurably worse*, so **low statistical power works in the candidate's favour** — the less sensitive the test, the easier it is to merge a real regression. The fix is to treat sensitivity as a published, engineered property of the gate, not an accident of gold-set size.

**The math** (paired test on per-item score deltas; Wilcoxon ≈ paired-t with asymptotic relative efficiency ~0.955):

```
MDE ≈ (z_α + z_power) · σ_d / √(0.955·n)
n   ≈ (z_α + z_power)² · (σ_d / δ_target)² / 0.955
```

where `σ_d` = standard deviation of per-item deltas (measured, not guessed — see procedure), one-sided α = 0.05 (no-regression cares about one direction; one-sided is legitimate here and buys power), power = 80% → `(z_α + z_power) = 2.49`.

**Worked illustration** (σ_d = 0.20 on a 0–1 metric scale — to be replaced by the measured value):

| n (gold set) | MDE @ 80% power | Reading |
|---|---|---|
| 100 | ≈ **0.051** | only regressions ≥ ~5 points are reliably caught — a 3-point faithfulness drop usually merges |
| 250 | ≈ 0.032 | |
| 650 | ≈ 0.020 | the ~2-point sensitivity a quality gate plausibly needs |

**Procedure (operational, not aspirational):**
1. **Measure σ_d free of charge:** the baseline arm's per-item scores are already cached in `claims`/the archive; the calibration surface's self-consistency re-runs (k=3) give within-judge variance. Estimate σ_d per metric from the first weeks of real runs.
2. **Publish the sensitivity:** every gate run computes and stores `mde` in `comparison_reports` and prints it in the MR verdict — *"PASS — note: this run could only detect regressions ≥ 4.8 pts on faithfulness."* A pass below the sensitivity floor is labelled honestly, never silently.
3. **Size the gold set to the MDE you need, as a budget line:** pick `δ_target` per metric (policy decision — the same conversation as the significance bar), invert the formula, and grow `eval_questions` toward that n. At ~5 judge calls/item, the marginal cost of n=650 vs n=100 is a knowable dollar figure per gate run — sensitivity is purchasable.
4. **Multiple metrics:** Benjamini–Hochberg across the metric grid (already policy) slightly raises the effective MDE; the published `mde` is computed at the post-correction α so the printed number stays honest.
5. The **faithfulness hard floor is exempt from all of this** — it's a threshold on the candidate arm's own scores, not a comparison, so it doesn't depend on detection power.

`policy_version` strings therefore carry the power posture, e.g. `no-regression-v1(one-sided α=.05, power=.80, BH)` — when the significance-bar decision lands, it replaces the policy string, and old verdicts remain interpretable under the rule that produced them.

### 4.4 The claims protocol (acquire → work → finalize, Spot-safe)

Postgres's PK conditional insert is the atomic primitive; the lease makes Spot interruption recoverable; the `done` row is the permanent verdict cache.

```sql
-- ACQUIRE (one statement, race-safe): insert fresh, or steal an expired lease. 0 rows ⇒ someone else holds it / it's done.
INSERT INTO claims (repro_key, status, lease_owner, lease_expires_at, eval_spec_version)
VALUES (:rk, 'leased', :me, now() + interval '10 minutes', :spec)
ON CONFLICT (repro_key) DO UPDATE
   SET lease_owner = EXCLUDED.lease_owner,
       lease_expires_at = EXCLUDED.lease_expires_at,
       attempt = claims.attempt + 1,
       status = 'leased'
   WHERE claims.status IN ('leased','failed') AND coalesce(claims.lease_expires_at, 'epoch') < now()
RETURNING attempt;
```

```python
def process_item(item, ctx) -> Verdict | None:
    rk = repro_key(item.id, item.input_hash, ctx.spec)

    if (hit := claims.get_done(rk)):                 # SELECT score, result_ref WHERE status='done'
        phoenix.log(ctx.run, item, hit)              # idempotent re-log; ZERO Bedrock spend
        return hit

    if not claims.acquire(rk, owner=ctx.pod):        # the SQL above
        return None                                  # owned elsewhere — skip, don't wait

    try:
        ctx.budget.charge_or_raise(estimate(item))   # circuit breaker BEFORE the spend (§5.2)
        verdict = ctx.judge.evaluate(item)           # token bucket inside the Bedrock call
        s3.put_once(evidence_key(rk), verdict.raw)   # content-addressed ⇒ idempotent
        phoenix.log(ctx.run, item, verdict)
        claims.finalize(rk, result_ref=..., score=verdict.score, cost=verdict.cost)
    except RetryableError:
        claims.release(rk)                           # or just let the lease expire
        raise
```

**Spot interruption walkthrough:** SIGTERM → handler stops taking new items and lets in-flight finish inside the 2-min Spot warning; anything mid-flight when the node dies leaves a `leased` row whose lease expires; the retried Job's worker **steals** it and re-runs — but every *completed* item before the interruption is a `done` cache hit, so an interrupted 1,000-item run resumes at item N, having paid for N, not 2N. That sentence is the whole reason the table exists.

**Write order for one item (the cross-store consistency rule):** `claims.acquire` → Bedrock → S3 evidence → Phoenix score → `claims.finalize` → (run end) `eval_runs` finalize + `comparison_reports`. Every step is idempotent under its key, so any partial failure retries and **converges** — no 2PC, no outbox needed now that nothing emits events.

### 4.5 Phoenix mapping & the Parquet archive

| GitLab / runtime concept | Phoenix concept |
|---|---|
| one gate run / replay run | **experiment** (named `"{surface}:{mr_iid or window}:{run_id[:8]}"`) |
| gold set @ version | **dataset** (versioned; adversarial trick examples tagged) |
| one item × metric verdict | **evaluation** on the dataset example |
| nightly sampled span × metric | **annotation** attached to the production trace |

Phoenix is the lab notebook, **not** the system of record: every score is also appended to the S3 Parquet archive (columns: `repro_key, run_id, surface, item_id, metric, score, passed, eval_spec_version, judge_model_id, judge_prompt_version, slice, created_at`), partitioned by month, Athena-queryable. If Phoenix is lost, replay the archive into a fresh instance.

> **[FORK — doc-bench → Phoenix mapping]** Option A: Hurdle-1 parsing scores also land in Phoenix as experiments (one pane of glass; but Phoenix's model is trace-shaped and parsing runs have no traces). Option B: parsing scores go only to `comparison_reports` + the Parquet archive (clean boundary; two places to look). The DDL above works under either — `comparison_reports` carries the gate verdict regardless. Decide before build; nothing upstream blocks on it.

---

## 5. The probabilistic machinery, concretely

### 5.1 Judge configuration (the reliability levers, pinned)

```python
JUDGE = BedrockJudge(
    model_id   = settings.JUDGE_MODEL_ID,         # dated; au inference profile; family ≠ generator (asserted)
    temperature= 0.0,
    prompt_version = "faithfulness-steps-v3",     # pinned DeepEval evaluation_steps — never auto-generated in prod
)
# Metrics: DeepEval Faithfulness/AnswerRelevancy (QAG claim-extraction → near-binary checks),
# ContextualPrecision/Recall offline only (need expected_output from the gold set).
# Self-consistency: calibration surface re-runs a fixed subset k=3 and reports score variance;
# κ vs human labels computed on the human-labelled gold slice. Both alert on drift (CloudWatch).
```

Prompt-injection defence (per the Architecture's judge-integrity posture): document/query content is delimited as untrusted data inside the judge prompt; the calibration set permanently includes known injection examples ("score this perfect") and **alerts if the judge ever complies**.

### 5.2 Token bucket + budget (one shared budget, monitor reserved first)

- **Token bucket:** Redis (ElastiCache) Lua script, atomic `take(model_id, est_tokens)` against a per-model refill rate sized to the Bedrock quota. Two pools per model: `reserved_online` and `general`; the nightly draws reserved-first, gates/replay draw general-only — a daytime sweep mathematically cannot starve the monitor.
- **Per-run circuit breaker:** `eval_runs.budget_usd` set at launch (template default per surface); `budget.charge_or_raise` accumulates actual token costs and **fails the run cleanly at the ceiling** — partial coverage is reported (`items_scored/items_total`), never silently truncated.

### 5.3 Sampling (deterministic, auditable)

```python
def sampled(trace_id: str, rate: float = 0.05) -> bool:
    h = int(hashlib.sha256(trace_id.encode()).hexdigest()[:8], 16)
    return h % 10_000 < int(rate * 10_000)
# Properties: same trace always samples the same way (re-runs/backfills are exact);
# no coordination, no ledger — the S3 span store is immutable, so "was it sampled?"
# is recomputable, not stored. Stratification check: nightly reports per-slice sample
# counts; a starved slice (< n_min) raises a warning rather than silently under-powering.
```

### 5.4 The dead-man's switch (silence pages)

| Alarm | Condition | Source |
|---|---|---|
| nightly-run-missed | no `eval_runs` row with `surface='online'` and `started_at > now()-26h` | scheduled query (Grafana/CloudWatch) |
| zero-spans-in-window | nightly ran but `items_total = 0` | the nightly itself emits a metric |
| span-store-staleness | newest object under `spans/` older than 26h | S3 metric |
| budget-breaker-tripped | any run failed with `budget_exceeded` | run exit metric |
| κ-drift / self-consistency-drift | calibration below threshold | calibration surface |

---

## 6. Security & access (per-surface least privilege)

| Identity | IAM (IRSA) | Notes |
|---|---|---|
| `eval-online` | S3 spans RO · S3 online-evidence RW (**per-customer KMS encrypt**) · Bedrock invoke (au profile) · Phoenix/Postgres | the only identity that touches production content |
| `eval-adoption` | testing-index RO creds · S3 gold RO + evidence RW · Bedrock · Postgres | no production span access |
| `docbench-full` | S3 gold RO + reports RW · Postgres (`comparison_reports`) | **no Bedrock at all** — the deterministic-world invariant, enforced in IAM |
| `eval-calibration` | gold RO · Bedrock · Postgres | |
| CI runner SA (k8s RBAC) | create/get/watch Jobs in `eval` ns only | zero AWS permissions |

Postgres access is via IAM auth tokens over TLS; no static DB passwords anywhere. Per-customer KMS keys on `online-evidence/` make erasure a key deletion (crypto-shredding) without touching the otherwise-immutable evidence store.

---

## 7. Open forks this document inherits (and what each changes here)

1. **Replay Case A/B ownership (Architecture §6).** Case A is already fully specced above (`RecordedSpanAdapter` — no index, no Ingestion involvement). If eval also owns Case B (query-param re-runs), `opensearch_ro.py` gains a parameterised query path against an index *copy Ingestion provides*; if not, the replay surface ships Case-A-only and §2's replay row loses its index column entirely. **No index-building code exists in this repo under any outcome.**
2. **doc-bench → Phoenix (§4.5).** Affects one writer module; DDL unaffected.
3. **Orchestrator (Architecture §3b).** If the platform engine has native launch/wait, `tools/launch-job` is deleted; if it's Flyte/Temporal, the `claims` migration gate fires and §4.3–4.4's table moves into the engine's native cache — the *protocol* (acquire/steal/finalize semantics, repro_key) survives the re-homing unchanged.

---

## 8. Build checklist (what exists vs what this doc adds)

| Piece | Status |
|---|---|
| crucible kernel: metrics, Bedrock judge call path, replay statistics | exists |
| pinned `evaluation_steps` prompt versions; judge≠generator assertion; self-consistency runner | **net-new** |
| service layer: surfaces, claims, budget, sampling, reporting, db | **net-new** |
| `import-linter` seam; `--surface` entrypoint; CPU image | **net-new** (small) |
| GitLab: `parsing-gate`, `rag-gate` trigger, `launch-job`, job templates, CronJob | **net-new** (mostly YAML) |
| doc-bench wheel/image + schemas | exists — needs explicit `schema_version` + `mustpass.yaml` thresholds |
| 3 tables + Alembic; Parquet archive writer; Phoenix client | **net-new** |
| token bucket Lua + budget breaker; dead-man alarms; per-customer KMS prefixes | **net-new** |

Sequencing follows Architecture §7: contract surface version-pinning → Hurdle 1 → nightly → Hurdle 2 → replay, with the Case A/B fork resolved before any Case-B code.