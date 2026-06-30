# Evaluation Service — Decision Register

*The index of every significant decision: what was decided, where the full rationale lives, and what would re-open it. This is deliberately an **index, not a set of ADRs** — the rationale for each decision is written once, inline, in the Architecture Design (AD) or Detailed Design (DD); duplicating it here would create two copies that drift. Scan the table; jump to the section; attack the rationale there.*

*Doc set: **OV** = Overview · **AD** = Architecture Design · **DD** = Detailed Design. This register and those three documents supersede all earlier drafts (see §5).*

---

## 1. Decided

| # | Decision | Where the rationale lives | Re-opens if… |
|---|---|---|---|
| D1 | doc-bench stays a separate repo: deterministic, CPU, no model plane; shipped as wheel + image | AD §1–2 | never expected — the deterministic/probabilistic boundary is the design's organising idea |
| D2 | eval is repurposed into `eval-rag`: one repo, kernel/service seam, one-way imports enforced by `import-linter` | AD §2 · DD §1.1 | a sub-batch-interval freshness SLO turns online-spans back into a streaming service (narrow, named) |
| D3 | Two-hurdle adoption: parsing benchmark = necessary (registers a candidate), RAG test = sufficient (makes it mergeable); eval measures, never decides | AD §0, §4 · OV §4 | — |
| D4 | Orchestration = GitLab CI/CD + the cluster clock; **no bespoke control plane** | AD §3, §7 | see P5 (API) and F3 (platform orchestrator) |
| D5 | Execution = bounded K8s Jobs on the shared EKS platform (Indexed Job for doc-bench, CronJob for nightly); nothing always-on | AD §3b **[decision trail recorded]** | platform/orchestrator answers (F3) *refine* this; none re-open it |
| D6 | Trigger model: MR-gated + clock + manual; **no machine-initiated event path from Ingestion** | AD §3 | see P1/P2 return conditions |
| D7 | Ingestion↔eval contract = **data at rest only**: S3 artifact/manifest layout + span schema | AD §3 · DD §3.3 | P2's conditions |
| D8 | Registry = **the MR itself** (required checks + labels + merge) | AD §4, §6 | P6 |
| D9 | Judge stack: Bedrock, au inference profiles, judge ≠ generator (constructor-time assertion), temperature 0, pinned `evaluation_steps`, QAG decomposition, κ calibration + self-consistency, injection trick-examples that alert | AD §1 (evidence position, mid-2026) · DD §5.1 | judge-reliability evidence materially shifts (the §1 evidence position is dated for this reason) |
| D10 | **Delta-gating**: absolute judge scores are monitoring signals only; the judge gates exclusively on paired baseline-vs-candidate deltas | AD §1, §4 · OV §2 | — (robust at any judge reliability level) |
| D11 | Interim gate policy: **no-regression mode**, one-sided paired tests, **published MDE per verdict**, faithfulness as a hard floor exempt from power | DD §4.3b · AD §4, §8.2 | replaced (not re-opened) when the significance bar (F4) is agreed |
| D12 | Cost control: single shared Bedrock budget; per-model token bucket with a **reserved online share**; per-run circuit breaker | AD §3 · DD §5.2 · OV §2 | — |
| D13 | Online sampling: deterministic hash at read time, 5%, **no sample ledger** (recomputable over the immutable span store) | DD §5.3 | — |
| D14 | Nightly cadence — **pinned (product decision)**; a cron line, not architecture | AD §6 | an incident shows nightly is too slow → intraday micro-batches, zero design change |
| D15 | Residency: all model traffic on in-region (au) profiles; **per-customer KMS keys** on online evidence (erasure = key deletion) | OV §3 · DD §6 | — |
| D16 | Dead-man's switch: silence pages (run-missed, zero-spans, span-store staleness, budget-trip, κ-drift) | AD §3 · DD §5.4 | — |
| D17 | The wheel: testability check + **named must-pass per-document floor**; the floor is *enforced* inside Hurdle 1, the wheel *alarms* everywhere else; one `mustpass.yaml` feeds both | AD §2a · DD §1.3 | — |
| D18 | Per-surface IRSA least privilege; `docbench-full` has **no Bedrock permission** (the boundary as an IAM fact) | DD §6 | — |
| D19 | Phoenix = lab notebook, rebuildable from the S3 Parquet archive; **not** the system of record | DD §4.5 | — |
| D20 | No transactional outbox — nothing emits events; idempotent ordered writes close consistency alone | DD §4.4 | any future event emission re-introduces it |

## 2. Proposed — review explicitly invited

| # | Proposal | Where the case + alternatives live | What review decides |
|---|---|---|---|
| R1 | Data tier: S3 + Phoenix + **3 Postgres control tables riding Phoenix's instance** | AD §3a (8-row alternatives survey) · DD §4 | accept, or move down the ladder |
| R2 | The `claims` spend-lock/verdict-cache table (the one bespoke mechanism) | AD §3a · DD §4.3–4.4 · pushback defenses + **concession ladder: DD §4.3a** | which ladder rung |
| R3 | **Precondition on R1**: Phoenix's Postgres must be managed, backed-up, PITR, with a paged owner — else DynamoDB becomes the *default*, not the fallback | AD §8.3 | verify the four ops facts |
| R4 | `claims` as a future shared platform primitive (idempotency-and-budget ledger) when a second GenAI app arrives — named trajectory, not built | AD §3a | platform team awareness, no action |

## 3. Parked — pruned with named return triggers

| # | Parked | Trigger(s) to bring it back | Where |
|---|---|---|---|
| P1 | SQS + KEDA consumer for online | (a) freshness SLO < batch interval; (b) high-frequency completion events from Ingestion | AD §3 note |
| P2 | EventBridge / any machine event path | a caller exists again (none does today) | AD §3 |
| P3 | Neutral `contracts` repo (schemas stay vendored: parsing in doc-bench, span/questions in eval-rag) | first breaking change to a shared schema · Ingestion needs programmatic validation in CI · formalising `adoption_test_request` | AD §2 |
| P4 | GPU for parser runs | full-benchmark wall time exceeds the MR window *after* Indexed-Job sharding, or corpus growth does | AD §6 |
| P5 | Control-plane API | non-GitLab machine consumer that can't use a view/Athena · eval-as-a-service for a 2nd product · registry read-model outgrows webhooks→table→dashboard | AD §6 |
| P6 | Registry read-model beyond the MR | cross-MR queries needed ("all candidates awaiting ingestion test") | AD §6 |

## 4. Open — forks and confirmations (the to-do list, not the unknown list)

| # | Open item | Decides | Where |
|---|---|---|---|
| F1 | **Replay Case A/B ownership** — biggest fork; if eval owns neither, its vector role collapses to read-only querier | whether any Case-B code exists; Case A is fully specced either way | AD §6 · DD §7 |
| F2 | doc-bench results → Phoenix (one pane) vs Postgres/S3 only (clean boundary) | one writer module; DDL unaffected | AD §6 · DD §4.5 |
| F3 | Platform orchestrator: which engine; what "reusing Ingestion" means; schedule + MR report-back; Hurdle-2 handoff mechanics | possible claims migration (R2); possible F1 resolution; `launch-job` deletion | AD §3b (4 questions) |
| F4 | Significance bar + multi-metric pass rule + per-metric δ_target | replaces D11's interim policy; sizes the gold set via the MDE formula | AD §6 · DD §4.3b |
| F5 | Domain gold set (public benchmarks are two lossy hops from production RAG) | Hurdle 1's validity | AD §4, §6 |
| F6 | **Cross-team confirmations** — Ingestion gate/index ownership; main-is-production; span schema signing; `adoption_test_request` signing | converts AD §8.1 from flagged to shored; Plan B written if declined | AD §0, §4, §8.1 · OV |
| F7 | Human-label supply for κ calibration (N labels/quarter, named owner, budget line) | whether the calibration loop is real | AD §8.4 |

## 5. Superseded (for readers of earlier drafts)

The current generation (OV + AD + DD + this register) **replaces** the earlier design set. Materially superseded along the way: the FastAPI control plane (→ GitLab, D4); SQS/KEDA worker lanes and EventBridge triggering (→ D5–D7, P1–P2); the DynamoDB + partitioned-RDS results tier (→ R1's S3/Phoenix/3-tables); the GPU replay Job and CUDA image (→ P4, CPU-only); the three-stage promotion funnel (→ D3's two hurdles, with the wheel re-scoped per D17 from "must-pass CI canary" to alarm-plus-enforced-floor); the transactional outbox (→ D20); and "hoist `contracts` now" (→ P3, parked with triggers). If a statement in an older document conflicts with this register, the register wins.