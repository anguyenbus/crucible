# Evaluation Service — Topology & Component Design

*Capstone synthesis. Three components, organised by one boundary, serving a two-hurdle adoption decision in which the service measures but never adopts. Supersedes the scattered topology notes in the prior docs; designed adversarially.*

---

## 0. The shape, in one paragraph

The Evaluation Service is **three components across two worlds**. The **deterministic world** is doc-bench (document parsing): no model, CPU, reproducible, safe as a hard gate. The **probabilistic world** is everything judge-based — RAG offline (the adoption test), online spans (the live monitor), and replay — all crucible-derived, all on Bedrock, all residency- and quota-bound. Adoption of a parser requires **two hurdles**: doc-bench is *necessary* (the parser code is sound and doesn't regress parsing), and the RAG test is *sufficient* (the answers actually improve). The service **provides both measurements and never makes the adoption decision** — Ingestion owns the testing index and the RAG gate, and because **main is production**, a change merges (goes live) only once both hurdles pass. Every topology choice below falls out of those facts.

---

## 1. The organising boundary: deterministic vs probabilistic

This is the single idea that makes the design legible. Sort every concern by which world it lives in:

| | **Deterministic world (doc-bench)** | **Probabilistic world (eval-rag: RAG-offline · online-spans · replay)** |
|---|---|---|
| What it measures | parsing quality | answer quality (faithfulness, relevancy, ctx precision/recall) |
| Model plane | **none** | **Bedrock** (au. inference profiles) |
| Compute | CPU only | CPU (judge is remote on Bedrock) |
| Reproducible? | yes → safe as a hard gate | no → monitoring signals, never a blocking CI gate |
| Needs token bucket / verdict cache / κ-calibration / residency? | **no** | **yes — all of it** |
| Cost driver | parser-run wall-time (OCR) | Bedrock TPM/RPM ($ per call) |

Why this matters: **every hard, expensive, risky thing — token bucket, residency, judge calibration, prompt-injection defence, PII erasure, the cost circuit breaker — lives entirely in the probabilistic world. doc-bench has none of it.** Keeping that boundary crisp is what lets doc-bench be a cheap deterministic gate and stops the judge's complexity from leaking into the parsing path. If a design decision blurs this line, it's wrong.

---

## 2. Repo structure (and the debate you invited)

**Position:** three repos — `contracts`, `doc-bench`, `eval-rag` — and I'd refuse to start without the first one.

- **`contracts` (shared, neutral, versioned) — non-negotiable, hoist it out now.** `parser_output`, `results_v1`, the span schema, the manifest, `eval_questions`. Today these are vendored *inside* crucible, which means ingestion would have to depend on an eval repo to learn the shape it must write — the wrong coupling direction, and it lets one team silently break the boundary. All three of ingestion, doc-bench, and eval-rag consume `contracts`; nobody owns it unilaterally. This is the prerequisite for "doc-bench is part of the service" without dragging ingestion into eval's dependency graph.

- **`doc-bench` (separate) — agreed, and correct.** It's a different *kind* of thing: deterministic, CPU, no model plane, its own cadence, shipped as a **wheel** (testability + must-pass canary — see §2a) **+ image** (full-benchmark Job). Folding it into eval-rag would couple a model-free deterministic kernel to a Bedrock service for no benefit. Keep it out.

- **`eval-rag` (the deployable) — one repo for RAG-offline + online-spans + replay. This is the debatable one; here's my argument for one, not two.**
  - *For one repo:* all three surfaces are the **same crucible kernel** (RAG metrics, judge orchestration, replay statistics, span handling, adapters) plus the **same Bedrock plumbing** (token bucket, verdict cache, residency, calibration). Splitting online-spans into its own repo duplicates the hardest-won, most safety-critical code. They differ only in *trigger and cadence* (nightly schedule vs Ingestion-triggered vs on-demand) — different **entrypoints into one image**, all bounded scale-from-zero Jobs, not different repos.
  - *The honest counter (now largely dissolved):* the earlier concern was mixing a production-critical *streaming* monitor with occasional batch surfaces in one repo. With the online monitor reworked as a **nightly batch Job** (§3), every surface shares one execution model — bounded Jobs on the shared platform — and there is no always-on component for batch churn to destabilise.
  - *Resolution:* keep one repo. The residual split trigger is narrow and named: carve online-spans out only if a future **sub-batch-interval freshness SLO** turns it back into a streaming service — not pre-emptively. Same YAGNI discipline as write-sharding: split when a real boundary forces it.

So: `contracts` ← {`doc-bench`, `eval-rag`, ingestion}; `eval-rag` pins the `doc-bench` wheel+image.

```mermaid
flowchart TB
    C["contracts (shared · versioned)<br/>parser_output · results_v1 · spans · manifest"]
    subgraph DB["doc-bench repo — DETERMINISTIC · CPU · no model plane"]
        W["wheel: testability + must-pass canary"]
        I["image: full benchmark (Job)"]
    end
    subgraph ER["eval-rag repo — PROBABILISTIC · Bedrock (the deployable)"]
        K["crucible kernel: RAG metrics · judge · replay stats · spans · adapters"]
        S["service: control plane · online-spans · adoption-test · replay · DB · IaC"]
    end
    C --> DB
    C --> ER
    C -.-> ING["Ingestion (other team)"]
    DB -->|pinned wheel + image| ER
```

### 2a. The doc-bench wheel — small, but the one signal we never compromise

The wheel surface of doc-bench is deliberately tiny, and that is exactly why it's valuable. It earns its place twice over:

1. **Testability (super lightweight).** In seconds, with no network and no cluster, it confirms a parser produces **well-formed, contract-conformant output** the eval pipeline can actually read and score. This is the cheapest signal in the whole system — runnable locally, pre-commit, or in any fast CI step — and it answers "is this parser even evaluable?" *before* anyone spends a full-benchmark run or an Ingestion reingest on it.

2. **A curated set of must-pass representative documents.** The wheel carries a small, deliberately-chosen set of representative PDFs/docs — the shapes the product cannot afford to parse worse (the critical, the common, and the historically-fragile: tables, multi-column, scanned, rotated, the specific domain forms we live on). **These are non-negotiable. There is no acceptable trade in which a candidate parser significantly regresses on them** — a candidacy signal, a mean improvement elsewhere, none of it buys back a break here. A significant regression on a wheel doc is a **smoke alarm: stop and look**, not a routine metric dip to be weighed against other gains.

**Why this matters even though the full benchmark is the formal gate.** The full benchmark's *aggregate* statistics can mask a regression on a single critical document — a candidate that lifts the mean while breaking exactly the doc you can least afford to break. The wheel pins those documents explicitly, by name, so a must-pass regression cannot hide behind a green average. It is the **per-document floor the aggregate gate structurally cannot guarantee**, and because it's so cheap it runs *everywhere the full benchmark is too heavy to* — giving an instant, ever-present canary on the documents that matter most.

**What it is not.** The wheel is **not** the formal Hurdle-1 gate (the full benchmark is) and it is **not** a gatekeeper that runs on the PR — there the full benchmark is a superset, so the wheel would be redundant. Its job is to be the fast, omnipresent alarm in every context the full benchmark is too slow for, holding the line on the documents that must never break.

---

## 3. Runtime topology on EKS/AWS

The key realisation: **orchestration is owned by GitLab CI/CD plus the cluster's clock; execution runs on the company's shared Kubernetes platform on AWS (EKS) — and nothing in eval is long-lived: every surface is a bounded, scale-from-zero workload.** MR pipelines launch the gate Jobs; a native **K8s CronJob** (midnight AEST, ArgoCD-managed) fires the nightly run; everything else is launched **by the team, on demand**. There is no bespoke control plane, **no queue, no AWS eventing into eval, and no always-on consumer**. (§3b records the execution-model decision trail and the open orchestrator questions.)

**Trigger model (pinned):** *GitLab owns change-driven and human-driven evaluation; the cluster owns the clock — all three defined in git.* Concretely: `merge_request_event` (pipeline changes, gated pre-merge), manual/cross-project runs (replay, sweeps, backfills), and the **K8s CronJob** for the nightly check (ArgoCD-synced manifest, so cadence changes are PRs). **EventBridge and the API-destination trigger were removed**: with RAG evaluation gated on PRs and bulk checks team-initiated, no machine-initiated event path from ingestion remained — so the ingestion↔eval contract shrinks to **data at rest only**: the S3 artifact/manifest layout and the span schema. No event schema to agree, version, or drift.

| Surface | Trigger | K8s primitive | Compute | Model plane |
|---|---|---|---|---|
| **Online nightly monitor** | **K8s CronJob** (midnight AEST; `concurrencyPolicy: Forbid`; ArgoCD-synced) over the previous day's window of the S3 span store | **Job** (from CronJob), Karpenter + Spot | CPU | Bedrock |
| **Ad-hoc sweeps** (e.g. bulk-onboarding parse health) | team-initiated: manual run, or a cross-project trigger from Ingestion's own onboarding pipeline | **Job**, Karpenter + Spot | CPU | — |
| **doc-bench full benchmark** (Hurdle 1) | parser MR pipeline | **Indexed Job**, Karpenter + Spot | CPU | — |
| **eval-rag adoption test** (Hurdle 2) | Ingestion pipeline (post-reingest) | **Job**, Karpenter + Spot | CPU | Bedrock |
| **replay** (retrieval-invariant) | on demand | **Job**, Karpenter + Spot | CPU | Bedrock |

## 3b. Execution model — the shared Kubernetes platform on AWS **[DECIDED — org alignment; decision trail recorded]**

> **Decision:** evaluation executes as bounded Kubernetes workloads (Jobs, one Indexed Job, one CronJob) on the **company's shared EKS platform** — the same platform Ingestion and the orchestrator run on. Eval is a small tenant of it: bursty, scale-from-zero, nothing always-on.

**Decision trail (kept honest):** an interim revision ran everything inside CI runners, on the correct observation that eval's intrinsic load is tiny — ~100 RAG queries and ~200–1,000 documents per run, I/O-bound on the Bedrock token bucket. **That analysis stands as the statement of eval's own requirements**: it sizes the resource requests and forbids eval from over-claiming platform capacity. It was superseded by organisational context that fired return-gate (c) early: **everything runs on K8s, eval will substantially reuse Ingestion and orchestrator components, and the platform's scaling is built precisely for bursty-then-nothing loads.** When the platform owns the machinery, "machinery without a workload" stops being an objection — eval consumes a shared capability instead of building a private one.

**What returns with this decision:** a Job per surface; **Indexed Job** sharding for doc-bench (the in-runner model's CI-timeout veto is moot); the nightly **CronJob** (kubelet fires it directly; `suspend: true` for incidents); per-surface **IRSA** least-privilege (cleaner than the runner-class workaround); and the thin `launch-job` helper + eval-namespace RBAC for pipeline-launched Jobs — *unless the orchestrator provides launch/wait natively (open question 1)*.

**What held through every execution-model revision** — evidence the load-bearing parts were placed at the right layer: the trigger model and the merge-blocking semantic; the data tier (claims on Phoenix's Postgres, scores in Phoenix, evidence in S3); the **spend lock** (Spot interruption is identical from inside a Job or a runner); the token bucket; the data-at-rest contract with Ingestion.

**Open questions for the platform/orchestrator team — these refine the design, they don't block it:**
1. **What is the orchestrator?** Argo Workflows / Flyte / Temporal / custom. If Flyte or Temporal, the **claims-table migration gate (§3a) fires** — claims moves into the platform's native cache and the table retires. If Argo Workflows or custom, claims stands (the memoization-granularity analysis in §3a applies).
2. **What does "reusing Ingestion" mean mechanically?** Are parse/reingest steps callable workflow units eval composes? (Intersects the measurement-provider boundary, and could finally settle the replay Case A/B fork.)
3. **Schedule and report-back:** does the nightly live as the CronJob or an orchestrator schedule, and how does an orchestrator-executed gate report pass/fail to the MR so merge-blocking survives?
4. **Hurdle-2 handoff:** stays GitLab multi-project + `strategy: depend`, or moves to orchestrator-native workflow composition if both teams share one engine.

One-liner: *"Evaluation is a small, bursty tenant of the shared platform: a container image, a handful of bounded Jobs, three tables, and a token bucket — nothing it runs is long-lived, and nothing it owns is infrastructure."*

A useful side-effect of the nightly cadence: the online run executes off-peak, so it never competes with daytime gate runs for Bedrock quota — the token bucket's online-reserved share remains as a guarantee, but in practice it's rarely contended.

> **[SQS — removed, with the conditions to bring it back.]** Earlier drafts fed the online monitor through an SQS queue consumed by a long-lived KEDA Deployment. Removed because the **S3 span store — already a hard requirement for replay — is the durable buffer**: scheduled micro-batches over a trace store are the conventional pattern for online LLM eval, a queue would be a *second delivery channel* for data already landing durably, and sampling deterministically **at read time** from the store is *more* robust under load than sampling at the edge (nothing can ever be dropped). Reintroduce SQS + a KEDA-scaled consumer only if **(a)** a pinned freshness SLO demands drift detection faster than the batch interval, or **(b)** ingestion starts emitting completion events at high frequency. Until one of those is true, the queue is by-default architecture — exactly what this design prunes.

**Shared state:** Postgres — three control tables riding on Phoenix's required instance (`claims` spend lock, slim `eval_runs` provenance, `comparison_reports` verdicts); S3 (evidence/raw/snapshots/spans + Parquet score archive); Phoenix (runs, experiments, per-item scores). Full rationale and alternatives survey in §3a. The **parser-candidate registry remains the MR itself.**

**Cross-cutting (probabilistic world only):**
- **Bedrock token bucket** — ElastiCache/Redis Lua, **per-model**, **shared by online + adoption-test + replay**, token-metered (not request-metered). Critically, the **online monitor holds a reserved share**; adoption-test and replay are preemptible against the remainder so a big experiment can't starve the production early-warning system.
- **repro-key idempotency** (Postgres `claims` conditional insert + lease — authoritative before any Bedrock spend; §3a), **VPC interface/gateway endpoints** (the in-VPC claim is false without them), **residency** (au. profiles for anything that ships spans/legal context to a model), **dead-man's switch** (scheduled-run-missed, zero-spans-in-window, and **span-store staleness** alarms — silence must page, not read as green).

```mermaid
flowchart TB
    PR["Parser MR"] --> GL["GitLab CI/CD<br/>(external — orchestrates &amp; gates)"]
    ING["Ingestion pipeline<br/>(owns reingest + testing index)"] --> GL
    EVT["Team: on-demand<br/>(replay · ad-hoc sweeps)"] --> GL
    CRON["K8s CronJob (midnight AEST)<br/>ArgoCD-synced manifest"] --> ONJ
    SERVE["Serving-path spans"] -->|OTel export| STORE[("S3 span store<br/>already required for replay")]

    subgraph EKS["Shared K8s platform on AWS (EKS) — eval = bounded Jobs only, nothing long-lived"]
        H1["doc-bench full benchmark<br/>Indexed Job · CPU · Spot"]
        H2["eval-rag adoption test<br/>Job · Bedrock"]
        ONJ["online nightly run<br/>Job · Bedrock · 5% sample at read time"]
        RP["replay<br/>Job · Bedrock"]
        TB["Bedrock token bucket<br/>(Redis · per-model · online reserved)"]
    end

    GL -->|launch + wait| H1 & H2 & RP
    STORE --> ONJ
    ONJ --- TB
    H2 --- TB
    RP --- TB
    H1 --> SOR[("Postgres (3 control tables)<br/>S3 · Phoenix")]
    H2 --> SOR
    ONJ --> SOR
    RP --> SOR
    GL -.->|merge gate: pipelines must succeed| PR
```

---

## 3a. Data tier — final shape, and the one table we must defend **[PROPOSED — subject to review]**

> **Status: open for challenge.** This is the **first workload this team has operated where redoing work costs money per item** — every judge check is paid Bedrock calls, so a retry is a billing event, not just wasted CPU. We have not run this pattern before, and we explicitly invite scrutiny of it — especially because this service is an early piece of the **AI platform this team will operate as more GenAI applications arrive**: whatever is settled here will likely be inherited.

**The tier, after consolidation** (control plane → GitLab; queue → span store; DynamoDB → Postgres; per-item results → Phoenix):

| Store | Holds | Why it's the right home |
|---|---|---|
| **S3** | artifacts, spans, raw verdicts, Parquet score archive | immutable evidence; already mandatory for replay |
| **Phoenix** (already in the stack) | runs/experiments + per-item scores (annotations) | one copy of scores, browsable next to the traces that produced them; rebuildable from the S3 archive, so still not a single point of failure |
| **Postgres** — 3 tables, riding on the instance Phoenix already requires | `claims` (spend lock), slim `eval_runs` (pipeline↔experiment provenance, coverage, cost), `comparison_reports` (gate verdicts) | the control residue no observability tool models |

Scale honesty: ~3M rows/year, ~99% of them in `claims`, ~0.5 GB — no partitioning machinery; the only hygiene is pruning claims of retired `eval_spec_version`s.

**Why `claims` must exist — four conditions, all of which hold here:** (1) redone work costs real dollars; (2) thousands of items share one run on Spot compute, so partial progress matters; (3) verdicts are cached *across* runs — the baseline is computed once, ever, and every MR reuses it; (4) no broker provides a lease — **when SQS was removed (§3 note), its visibility timeout — which *is* a claim-and-lease mechanism — went with it, and the function moved here.** Workloads missing any one of these conditions don't need this table, which is why most batch systems don't have one.

**Alternatives examined — this list is the defense; challenge any line:**

| Alternative | Why it doesn't take the job |
|---|---|
| **Phoenix** | has runs/experiments/scores (we use it for exactly those) but **no claim, lease, or conditional-write primitive in its API** — coordination isn't observability |
| **SQS visibility timeout** | was the lease; removed with the queue. Reintroducing a queue *to obtain a lease* re-adds the larger component |
| **Argo Workflows memoization** | right idea, wrong granularity (per-step, not per-item×metric); maintainers state the ConfigMap backing "will not scale to a large number of entries" |
| **Flyte / Temporal / Dagster** | real input-hash caches — implemented as tables in **their own Postgres**; adopting one swaps settled GitLab orchestration for a platform wrapper around the same table |
| **Redis** (already ours) | `SETNX`+TTL covers the *lease* half; the *cache* half must survive months, and eviction silently re-bills |
| **K8s Lease objects** | leader election for dozens of controllers in etcd — not millions of work items |
| **S3 conditional writes + Athena** | create-if-absent yes; leases, atomic steal, transactional finalize no — a hand-rolled lock manager protecting money; Athena is read-only analytics |
| **DynamoDB** | would genuinely work; not chosen because Postgres free-rides on Phoenix's mandatory instance. **Written fallback:** if that instance ever stops existing, `claims` → DynamoDB (conditional writes + native TTL), the other two tables → S3 |

**Prior art** (the pattern is standard; only the composition is ours): DeepEval's result cache (`-c` / `CacheConfig` — local-only, hence promoted to a shared store); Langfuse external eval pipelines (batched, checkpointed); Procrastinate / Oban / pg-boss / River (production Postgres claim-and-lease frameworks); Kubernetes Job docs + AWS Spot guidance (idempotent item tracking, checkpoint on SIGTERM); Stripe idempotency keys (deterministic key per paid API call). Full annotated references in the appendix.

**Platform trajectory [for discussion]:** today this is deliberately *one table inside eval*, not a service. If a second GenAI application hits the same four conditions, the candidate move is promoting `claims` into a **shared platform primitive** — an idempotency-and-budget ledger — rather than each app growing its own. We are *not* building that now (same YAGNI discipline as everywhere else in this design), but we name the trajectory so the platform review can plan for it instead of discovering it.

---

## 4. The adoption flow — two hurdles, both pre-merge; main is production

main **is** production: if a parser change is merged, it is live. So **both hurdles are pre-merge gates**, and the change stays an unmerged PR/branch — tracked by the registry — until it has earned its way in.

```mermaid
flowchart TB
    DEV["Dev changes parser (PR / branch — not merged)"] --> PRG{"HURDLE 1 — doc-bench full benchmark<br/>correctness + evaluable + no catastrophic regression"}
    PRG -->|fails floor| REJ["PR can't proceed"]
    PRG -->|passes| REG["Registry: registered as candidate<br/>'ready for ingestion test' (+ candidacy signal)"]
    REG --> RE["Ingestion: reingest the candidate into a testing index it owns"]
    RE --> H2{"HURDLE 2 — eval-rag RAG judge on the testing index<br/>do the answers improve? (faithfulness floor + significance)"}
    H2 -->|no| KEEP["Registry: rejected · PR not mergeable · keep current parser"]
    H2 -->|yes| MERGE["Mergeable → merge to main = PRODUCTION"]
    ON["Online-spans monitor"] -.->|watches post-merge quality| MERGE
```

The load-bearing reframes from the adversarial debate, now built in:
- **Hurdle 1 registers a candidate; it does not merge it.** doc-bench passing means "this parser is sound and ready for the ingestion RAG test," recorded in the registry as the eval→Ingestion handoff. It is a **correctness + evaluability + regression-floor** gate, *not* a "parsing must improve" gate — a strict improvement gate both blocks RAG-helpful changes (boilerplate strippers that lower text metrics) and waves through parsing-better/RAG-worse ones. Parsing-improvement is a *candidacy signal*, never a merge verdict.
- **Hurdle 2 is what makes it mergeable, and merging is adoption.** Ingestion reingests the candidate into a testing index it owns and runs the RAG judge there; eval supplies the RAG measurement. Only when the RAG judge passes is the PR mergeable — and because main is production, the merge *is* the adoption. Ingestion owns the testing index and the RAG gate; eval measures; the merge is the mechanical consequence of both required checks going green. There is **no separate "config flip" and no "merged-but-not-live" state**.
- **The registry is the connective tissue** between two hurdles and two owners: `registered / awaiting-RAG → RAG-passed / mergeable → merged`, or rejected at either gate. Under GitLab CI/CD this **is the MR's own state** — required checks + labels + the merge itself — so there's no bespoke registry service on day one; add a small read-model later only if you need cross-MR dashboards.
- **The two hurdles run on different corpora** (benchmark gold vs production-representative), so the parser runs twice and Hurdle 1 is a doubly-weak predictor (different metric *and* different distribution) — the standing argument for a domain gold set.

---

## 5. Non-negotiable invariants (what I'd refuse to ship without)

1. **A parsing pass must be structurally incapable of reading as an adoption signal** — separate stores, separate dashboards, separate gates; the only thing that makes a change mergeable (and main is production) is a passing Hurdle-2 RAG test on the testing index. The entire failure mode is "parsing improved, so we shipped it."
2. **doc-bench never touches Bedrock, residency, the token bucket, or calibration.** If it does, the deterministic/probabilistic boundary has been violated.
3. **The online monitor gets a reserved Bedrock quota share.** A discretionary experiment (adoption test / replay) must never be able to starve the production early-warning system.
4. **`contracts` is shared and external.** No schema the producer (ingestion) must conform to may live inside an eval repo.
5. **Eval measures; Ingestion owns the testing index and the RAG gate.** The service builds no production index and owns no reingest; adoption is the **merge to main (= production)**, gated by both hurdles. Eval never merges and never decides.

---

## 6. Open decisions I'd force before building (adversarial)

- **Replay Case A/B ownership.** Does eval own retrieval-invariant span replay (prompt/generator/judge — Case A; query-params — Case B)? Index-affecting replay (Case C) is Ingestion's reingest. If eval owns neither, **eval builds no OpenSearch index at all** and its vector-store role collapses to read-only querier — a materially simpler design. Decide this first; it's the biggest remaining fork.
- **Does the candidate registry need to be more than the MR?** By default the MR *is* the registry (checks + labels + merge), which spans both teams for free. Only build a separate read-model if you need cross-MR queries ("everything queued for the ingestion test," "candidates older than N days") — and even then it's a table fed by GitLab webhooks + RDS, not an orchestration service.
- **Domain gold set for Hurdle 1.** Public benchmarks → production parsing → production RAG is two lossy hops; a domain gold set closes the first.
- **Online cadence — PINNED: nightly (product decision).** Drift detection runs up to ~24h behind live; accepted because the pre-merge gates are the primary protection and the monitor is the backstop. The cadence is **a cron line, not architecture** — escalate to intraday micro-batches if an incident ever shows nightly is too slow, with zero design change; a queue returns only at sub-batch-interval SLOs (§3 note).
- **Orchestrator integration (§3b)** — the four questions for the platform team: which engine (decides whether the claims-table migration gate fires), what "reusing Ingestion" means mechanically (could settle replay A/B), schedule/report-back ownership, and whether the Hurdle-2 handoff moves to workflow composition. These refine the design; none block the build.
- **The claims table (§3a)** — flagged for review: first time this team holds paid-work coordination state. The alternatives survey is written precisely so reviewers can attack it line by line; the open platform question is whether this graduates to a shared idempotency/budget primitive when a second GenAI app needs it.

---

## 7. Honest build state (so the plan isn't fiction)

- **doc-bench integration, the adoption-test surface, online-spans, and replay are net-new, plus the contracts hoist.** crucible today has the RAG-judge kernel (Bedrock judge works) and the replay *statistics*, but no service surfaces, no deterministic lane, no spans pipeline, and the contracts are still vendored inside it. **Orchestration is *not* net-new — it's GitLab CI/CD** (the gates, the cross-team handoff, the merge gating), so there's nothing bespoke to build or defend there; the candidate registry is the MR's own state.
- Sequence the build so the **measurement-provider boundary and the `contracts` hoist land first** (everything else depends on them), then Hurdle 1 (doc-bench gate as a GitLab pipeline launching the Indexed Job), then the online monitor, then Hurdle 2 (the cross-team adoption-test pipeline), then replay — and resolve the §6 Case A/B fork before committing to any index-building machinery.