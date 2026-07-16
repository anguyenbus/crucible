# LLM Selection & Continuous Evaluation Framework

*An engineering process for keeping a GenAI application high-performing, cost-effective, and safe — written with the skeptic's hat on — followed by an honest audit of how much of it Crucible actually does.*

**Audited** 2026-07-16 · **Branch** `dev_opensearch_orchestrator_guardrails` · **Stage** POC, no production · **Basis** code, not intent

Part I is vendor-neutral process and applies to any GenAI application. Part II is the audit of this repo, cited to code. Where a doc and the code disagree, the code wins.

---

## Status at a glance

| Phase | Status | The one-line truth |
|---|---|---|
| 1 — Requirements as contracts | **PARTIAL** | Specs exist in `docs/eval_final/`; thresholds are self-declared placeholders |
| 2 — Hard-constraint gate | **STRONG** | Residency genuinely enforced; no written exclusion record |
| 3 — Evaluation suite | **PARTIAL** | Golden set real (76 q); adversarial set **absent**; judge **uncalibrated** and **uncached** |
| 4 — Offline bake-off | **PARTIAL** | One chunking comparison at n=10, honestly reported as inconclusive |
| 5 — Safety track | **PARTIAL** | Guards ship and run; red-team **absent**; FP-rate harness never executed |
| 6 — CI/CD gate | **ABSENT** | No CI exists; `regression_check` is orphaned and structurally cannot fire |
| 7 — Production loop | **ABSENT** | No production; tracing thorough but dev-only and unsampled |

Detail in Part II. The self-score against the anti-pattern checklist is in section 13.

---

# Part I — The framework

## 0. The honest premise: why unit tests don't transfer

Before adapting the unit-test paradigm, be honest about where it breaks. A unit test asserts `f(x) == y`. An LLM gives you *f(x) ≈ y, usually, with p ≈ 0.9, and the definition of "≈" is itself contested*. Three properties of software testing quietly disappear:

**Determinism.** Even at temperature 0, outputs can vary across runs, model versions, and hardware. A "test" that passed yesterday can fail today with no code change on your side, because the provider silently updated the model or your prompt hit a different sampling path.

**Binary correctness.** Most LLM outputs are graded, not pass/fail. "Is this summary faithful?" has degrees. Forcing it into a boolean throws away the signal you actually need.

**Locality of failure.** When a unit test fails, the stack trace points somewhere. When a RAG answer is wrong, the fault could be in chunking, embedding, retrieval, reranking, the prompt, the model, or the question itself. Evaluation must be **layered** so failures localize.

The adaptation, therefore, is not "write unit tests for prompts." It is: **treat evaluation datasets as versioned test suites, metrics as statistical assertions with thresholds, and the whole thing as a CI/CD gate plus a production monitoring loop that continuously feeds new failures back into the suite.** Everything below builds that.

One more uncomfortable truth to state up front: **your evaluation is only as good as your dataset, and your dataset is always smaller, cleaner, and less adversarial than production traffic.** The process below is designed around that gap, not in denial of it.

## 1. Phase 1 — Define requirements as testable contracts

Skip this and every later phase degenerates into vibes. For each use case, write a one-page spec that a new engineer could evaluate against:

| Dimension | Contract example (make yours concrete) |
|---|---|
| Task definition | "Answer employee policy questions grounded in the HR knowledge base" |
| Quality bar | ≥ 90% answers rated correct-and-faithful on the golden set |
| Hallucination tolerance | Faithfulness violations < 2%; zero fabricated citations |
| Latency | p95 ≤ 3 s end-to-end (retrieval + generation) |
| Cost ceiling | ≤ $X per 1,000 queries at projected volume |
| Safety | Zero PII leakage; harmful-content rate < 0.1%; resists top-20 injection patterns |
| Compliance / residency | All inference in ap-southeast-2, or documented sign-off for cross-region routing |
| Refusal behavior | Answers "I don't know" when context is absent, rather than guessing |

The adversarial question to ask here: **"How would we know if this system was failing?"** If the team can't answer that in measurable terms, the spec isn't done. Also define the *failure taxonomy* now — hallucination, retrieval miss, refusal-when-shouldn't, answer-when-shouldn't, format violation, injection success, latency breach — because every later phase tags failures against it.

## 2. Phase 2 — Gate on hard constraints before evaluating anything

Cheap filters first. Kill candidates that fail non-negotiables so you never waste evaluation budget on them:

1. **Region & residency** — available in your required region (for us: ap-southeast-2), or via an inference profile whose routing your compliance owner has actually approved. Cross-region profiles can route prompts offshore; treat that as a compliance decision, not a config detail.
2. **Modality & context window** — can it ingest what you need (docs, images) and hold your retrieved context plus prompt with headroom?
3. **Lifecycle status** — do not standardize on a model already marked legacy or with a published end-of-life.
4. **Quota & throughput reality** — default quotas at your volume, or a provisioned-throughput cost you can stomach.
5. **Feature compatibility** — tool use, guardrails integration, structured output, whatever the architecture requires.

Output of this phase: a shortlist of 2–4 candidates and a written record of what was excluded and why. That record is what makes the decision defensible later.

## 3. Phase 3 — Build the evaluation suite (your "test suite")

This is the highest-leverage engineering artifact in the whole process. Budget real time for it.

### 3.1 The golden dataset

100–300 examples minimum for a meaningful signal, drawn from *real or realistic* user queries, each with ground truth (expected answer and, for RAG, expected source passages). Version it in git like code. Anything under ~50 examples produces differences that are mostly noise — be suspicious of any "Model A beat Model B by 3%" claim on a tiny set; without a significance check it's a coin flip wearing a lab coat.

### 3.2 The adversarial dataset

This is where the skeptic's hat matters. Deliberately include: ambiguous questions, questions whose answer is *not* in the knowledge base (to test refusal), near-duplicate documents that stress retrieval, prompt-injection attempts embedded in documents and in user input, PII-bait queries, out-of-scope requests, multilingual or typo-ridden inputs, and questions where the plausible-sounding wrong answer is more fluent than the right one. If your eval set contains only well-formed questions with clean answers, you are testing the demo, not the product.

### 3.3 Metric assertions

Pair every metric with a threshold so it behaves like an assertion, and split metrics into two classes:

*Programmatic checks (cheap, deterministic — your true unit tests):* output parses as valid JSON/schema, required fields present, length limits, no banned strings, citation IDs actually exist in retrieved context, latency and token counts within bounds. Run these on every single call, in CI and in production. They are the only part of the stack that genuinely behaves like classic unit tests — exploit that.

*Model-graded checks (LLM-as-a-judge — powerful, but a measuring instrument with its own error bars):* correctness, completeness, faithfulness/groundedness, context relevance and coverage (retrieval-side), helpfulness, harmfulness.

### 3.4 Calibrate the judge — do not skip this

LLM judges have documented biases: they prefer longer and more confident answers, favor outputs stylistically similar to their own (self-preference), and exhibit position bias in pairwise comparisons. Before trusting judge scores, have humans label 50–100 examples and measure judge–human agreement. If agreement is poor, fix the rubric or change the judge before running any model comparison — otherwise every downstream number is built on sand. Pin the judge model and rubric version; a judge upgrade mid-project invalidates score comparability. And never use the candidate model as its own judge.

### 3.5 Name the noise before trying to remove it

A score moved two points. The model improved, the judge drifted, the references shifted, or some combination — and you cannot tell which without naming the kind of noise you are looking at. There are two sources, and they need different fixes:

- **Aleatoric** — the example itself is ambiguous, or has several valid outputs. No judge improvement resolves this; it is a property of the task.
- **Epistemic** — the judge lacks the knowledge, evidence, or capability to rate correctly. This is fixable, and it is the more actionable of the two.

They co-occur. An answer may depend on a user preference nobody stated (aleatoric) while the judge lacks the domain knowledge to verify it anyway (epistemic). Conflating them produces wrong conclusions — methods that fail to separate the two misclassify high-entropy responses as hallucinations.

The consequence that catches teams out: **more samples fix sampling noise, not judge noise.** If your effect size is two points and your judge drifts one point run-to-run on identical inputs, growing the set from 10 examples to 100 does not rescue the comparison. Airbnb measured roughly 1% judge drift across runs on the same dataset against a real signal of 1–3% — most of what they observed was noise, and not the kind more data resolves.

So the bar for "meaningful" is not a threshold, it is **survival under perturbation**: rotate the judge, version the metric, re-stratify the sample, and see whether the conclusion holds. A difference that passes a significance test but flips under a judge swap is not a difference worth shipping on. Formal significance testing complements this; it does not replace it.

### 3.6 Make the harness deterministic before you trust it

The instinctive responses to a noisy judge are probabilistic — sample and majority-vote, or model the noise Bayesian-style. Both disappoint. Majority voting converges toward the judge's central tendency, which is not the same thing as accuracy. Bayesian treatment needs a centralized store for priors and per-sample posteriors across runs, which is the same infrastructure as a cache, minus the reproducibility a cache gives you for free.

The cheaper move is to stop asking the judge the same question twice. Cache on two axes:

- **References** — keyed by sample identifier and reference-generation config. Skip this axis entirely if your references are static, hand-written ground truth; it earns its keep only when references are LLM-generated and would otherwise regenerate as different strings on every run.
- **Judge verdicts** — keyed by sample, model output, judge config, and metric.

Identical inputs then return cached results, so evaluation becomes deterministic, cheaper, and comparable across runs — which is precisely what 3.5 needs in order to tell judge drift apart from a real change. Key at the experiment level rather than the infrastructure level and partial progress becomes durable: a job that dies at example 8,000 resumes from the cache, and each new candidate or metric runs against existing cached outputs for free.

Note that temperature 0 is not determinism. It reduces sampling variance within a call; it does not make two runs of the same evaluation return the same verdicts, and it does not stop you paying for both.

The framing worth internalizing: when candidates share a base model, most of their outputs are identical strings, and much of the noise a team fights is manufactured by its own testing process rather than emitted by the model.

### 3.7 Evaluate the layers separately — then evaluate the whole path

For RAG: run retrieval-only evaluation first (context relevance, coverage) to tune chunking, embeddings, top-k, and reranking; then end-to-end retrieve-and-generate evaluation. If you only measure end-to-end, a generation-model comparison is confounded by retrieval noise and you'll attribute retrieval failures to the wrong component.

But layered evaluation is only half of it, and the other half is the half teams skip: **component-level confidence creates false assurance at the seams.** ML components carry no formal specifications, so they resist the compositional reasoning that makes ordinary software testable — their interactions can only be observed empirically, not derived. You can validate preprocessing, the model, and the serving path individually and still be surprised in production, because nothing exercised the combined path under realistic conditions.

The fix is unexotic: a small set of representative inputs run through the entire production path, with quality and tail latency measured on the combined configuration. Selection is what matters — traffic-weighted sampling across the highest-volume segments, deliberate over-representation of the tail (locales, input modalities, historically incident-prone patterns), and prior incidents seeded in explicitly as regression cases. Small enough to run on every release candidate; broad enough that seam bugs surface before deployment.

Watch especially for **mocks at the seam**. A component test that fakes the object on the other side of a boundary cannot detect that the boundary is broken — it will pass, permanently and confidently, while the real path returns nothing.

## 4. Phase 4 — Offline model selection (the bake-off)

Now, and only now, compare candidates:

1. Run every shortlisted model against the *same* golden + adversarial suite with the *same* judge and rubric.
2. **Run each configuration 3–5 times** and report mean and variance. Non-determinism means a single run is an anecdote. If Model A beats Model B by less than the run-to-run variance, they are tied — say so. Be clear about what repetition buys: it characterizes sampling noise, but it will not rescue a comparison whose effect size is smaller than the judge's own drift (3.5). Caching removes that drift at the source (3.6); judge rotation tests whether the result survives it.
3. Score per failure-taxonomy category, not just in aggregate. A model that is 2 points better on average but hallucinates citations twice as often may be the worse choice for your risk profile.
4. Compute **cost per correct answer**, not cost per token: `(price per query) / (accuracy)`. A model at half the price but 70% accuracy vs. 92% is not "half the cost" once you account for the failures a human must catch — and the ones nobody catches.
5. Measure latency at realistic concurrency, not a single warm request.
6. Sanity-check for benchmark leakage: if a model's score on your suite looks too good, verify your eval questions weren't derived from public docs the model memorized. Contaminated evals flatter models that will disappoint in production.

Decision rule to pre-commit before seeing results (prevents post-hoc rationalization): e.g., "cheapest model that clears every quality and safety threshold wins; ties broken by latency." Document the winner, the evidence, the losers, and the margins.

**The multi-model answer is often correct.** The honest outcome of a bake-off is frequently *routing*, not a single winner: a small fast model for classification and simple lookups, a stronger model for complex reasoning. Evaluate the router as its own component if you go this way — a bad router combines the cost of the big model with the quality of the small one.

## 5. Phase 5 — Safety evaluation as a first-class track

Safety cannot be a row in the quality spreadsheet; it has different failure economics (one bad output can matter more than a thousand good ones).

Run a dedicated red-team pass on the winning configuration: direct and indirect prompt injection (including instructions hidden inside retrieved documents — the RAG-specific attack most teams forget), jailbreak patterns, PII extraction attempts, harmful-content elicitation, off-brand/off-scope manipulation, and data-exfiltration prompts if the app has tool access. Test **with guardrails in place** and record both the block rate on attacks and the false-positive rate on legitimate traffic — a guardrail that blocks 5% of real users is a product defect, not a safety win.

Safety thresholds are gates, not scores: any critical finding (PII leak, successful injection leading to action) blocks release regardless of how good the quality numbers are.

## 6. Phase 6 — CI/CD integration: evaluation as a merge gate

Here the unit-test paradigm returns in adapted form:

- **On every prompt/config/model change:** run the programmatic checks plus a fast eval subset (~30–50 examples) as a required CI check. Fails below threshold → merge blocked. Keep it fast enough that engineers don't route around it.
- **Nightly / pre-release:** full golden + adversarial suite, multiple runs, compared against a stored baseline. Any metric regressing beyond its noise band opens a ticket automatically.
- **On provider model updates:** treat a model version bump exactly like a dependency upgrade — full suite before adoption. Never let a provider's silent upgrade reach production unevaluated; pin model versions where the platform allows it.
- **Baseline discipline:** baselines are updated deliberately (a reviewed PR that says "we accept the new numbers"), never automatically. Auto-ratcheting baselines are how regressions get normalized.

Everything is versioned together: prompt, model ID, parameters, retrieval config, eval dataset version, judge version. A score is meaningless if you can't reproduce the exact configuration that produced it.

## 7. Phase 7 — Production: the loop that makes it "continuous"

Offline evaluation predicts; production reveals. Close the loop:

**Online monitoring.** Log (with appropriate privacy handling) inputs, retrieved context, outputs, latency, token cost, guardrail triggers, and user feedback signals (thumbs, rephrases, escalations to a human, abandonment). Run cheap programmatic checks on 100% of traffic and LLM-judge scoring on a sampled slice (1–10%, budget-dependent) — judging everything at full production volume can cost as much as serving it, so sample deliberately.

**Drift and canary discipline.** Watch for distribution shift in query types, retrieval scores, and judge scores over time. Roll out any model/prompt change as a canary or shadow deployment: shadow-run the new config on real traffic, compare judged outputs against the incumbent, promote only when it wins outside the noise band.

**The regression flywheel — the single most important habit in this document.** Every production failure (user complaint, judge flag, red-team finding, support escalation) gets triaged against the failure taxonomy and, if valid, **added to the adversarial dataset as a permanent regression test.** This is the direct adaptation of "every bug gets a test." Six months in, your eval suite reflects your real failure surface instead of your launch-day imagination — that accumulated suite is the asset that makes the whole process compound.

**Scheduled re-selection.** Quarterly (or on major model releases), rerun Phase 4 on the current suite. The model market moves fast enough that last quarter's winner is a hypothesis, not a fact. Because your suite has been accumulating real failures, each re-selection is *harder* to pass than the last — which is exactly what you want.

## 8. Anti-patterns — the adversarial checklist

Fail the process review if any of these are true. Crucible is scored against this list in section 13.

| # | Anti-pattern | Why it's fatal |
|---|---|---|
| 01 | Eval set under 50 examples, conclusions without variance | That's noise with a narrative. |
| 02 | Judge never calibrated against humans | You've automated an opinion, not a measurement. |
| 03 | Candidate model judging itself | Self-preference bias makes this structurally unsound. |
| 04 | Only aggregate scores reported | Averages hide the 3% catastrophic-failure mode that ends up in an incident review. |
| 05 | Optimizing the metric, not the outcome | Goodhart. If faithfulness is judged by citation presence, models learn to decorate answers with citations. |
| 06 | Eval set leaked into prompts or fine-tuning | You're testing memorization. Keep a held-out set nobody tunes against. |
| 07 | Demo-driven selection | Ten cherry-picked prompts in a console is procurement theater. |
| 08 | Safety as a footnote | No dedicated adversarial pass = untested attack surface in production. |
| 09 | Cost measured per token instead of per correct answer | The cheap model's failures are a cost; put them on the books. |
| 10 | No re-evaluation trigger | A selection decision without an expiry condition silently becomes dogma. |
| 11 | Unpinned model versions | Your baseline is drifting under you and your CI numbers mean nothing. |
| 12 | Eval spend unbudgeted | If unplanned, teams quietly stop evaluating — the most expensive saving available. |
| 13 | Task ambiguity and judge error collapsed into one number | They need different fixes. A low score on an unanswerable question is not a model defect, and no judge upgrade resolves it. |
| 14 | Components validated, the seam never exercised | ML components carry no specs; their interactions are only observable empirically. Mocking the far side of a boundary hides breaks in the boundary. |
| 15 | Regenerating what could have been cached | You are manufacturing your own noise and then measuring it. |

## 9. Minimal viable version (if resources are tight)

If the full framework is too heavy to start: (1) a 100-example golden set in git, (2) programmatic schema/latency checks on every call, (3) one calibrated LLM-judge metric — faithfulness for RAG — as a CI gate, (4) a rule that every production failure becomes a test case, (5) a quarterly bake-off. That's perhaps two weeks of setup and it already puts you ahead of most teams, because the flywheel in (4) does the compounding for you.

---

# Part II — Implementation status in Crucible

*The audit. Claims here are cited to code, not to intent.*

## 10. The honest baseline

**Crucible is a POC on a feature branch, not a system in production.** There is no CI, no deployment, no Kubernetes manifests, and no real traffic. The only AWS artifact is a `t3.small.search` OpenSearch domain that `make opensearch-down` deletes to stop billing (`infra/opensearch-poc/template.yaml:2-6`). This matters for reading the rest of Part II: Phases 6 and 7 are not "behind schedule," they are **not applicable yet** — but the design decisions being made now are what determine whether they're cheap or expensive later.

The shape of the gap is consistent and worth naming up front: **the reproducibility engineering is genuinely deep; the execution layer beneath it is thin.** Configs are hash-pinned and verified on every resolution, prompt bytes live inside the config hash, and the judge≠generator invariant is enforced at runtime. But nothing runs on a schedule, no gate blocks anything, and the two measurements the framework treats as non-negotiable — judge calibration and adversarial testing — have never been performed.

## 11. Phase by phase

### Phase 1 — Requirements: PARTIAL

The spec exists but disclaims itself. `docs/eval_final/7-api.md:64-67` marks both the judge model and the metric thresholds as explicit placeholders: *"the `0.80 / 0.85 / 0.70` values in examples are illustrative. Real thresholds come from calibrating against the golden set, not from this doc."* Shipped reality is a uniform 0.7 across all four metrics (`services/eval/app/kernel/rag_metrics/metric_specs.py:60-65`), described in-comment as "conservative and uniform pending re-baseline."

**The failure taxonomy section 1 calls for does not exist in code** — nothing tags failures by category, so no later phase can score against it.

### Phase 2 — Hard constraints: STRONG

This is the strongest-implemented phase, and it was implemented for the right reason. Models are pinned to `au.*` geographic inference profiles rather than `apac.*` specifically because "apac routes across the broad APAC region, a residency regression" (`metric_specs.py:23-26`). Region resolution **fails loud with no `us-east-1` fallback** (`services/eval/app/deepeval/bedrock_provider.py:127-151`) — exactly the "compliance decision, not a config detail" posture section 2 demands.

What's missing is section 2's actual deliverable: **there is no written record of which candidates were excluded and why.** The decision is defensible in code and undocumented in prose.

### Phase 3 — The suite: PARTIAL

Split verdict, and the split is the point.

**3.1 Golden dataset — real but under the bar.** GST Australian tax legislation, 76 questions at `--slice gst_full`, sliceable to 2/10/20 (`services/eval/app/datasets/gst_legal_rag.py:26-29`), plus HuggingFace `isaacus/legal-rag-bench`. Versioned in git as JSONL. But **76 is below the 100–300 that section 3.1 calls for**, and every decision recorded so far was made at n=10. Also: the loader docstring and `README.md:220` both claim **5,263 passages; the vendored corpus has 467** (`documents.jsonl`) — an 11× overclaim that would mislead anyone sizing a run.

**3.2 Adversarial dataset — ABSENT.** Not thin: absent. There is no attack corpus, no injection dataset, no PII-bait set, no refusal-test set — a `find` for any data file matching `inject|jailbreak|pii|redteam|adversar` returns **zero files**. The entire labelled benign/attack corpus in this repo is **four synthetic rows in a test fixture** (`services/eval/tests/phoenix/test_guardrail_ab.py:91-94`). By section 3.2's own standard, we are testing the demo.

**3.3 Metric assertions — half-wired.** The four model-graded metrics are real and run. The *programmatic* checks that section 3.3 calls "your true unit tests" are where it thins out:

- **`validate_result()` is never called on the live response path.** `routers/query.py` doesn't invoke it, and the envelope types the payload as `dict[str, Any]` that FastAPI won't schema-check (`app/schemas/envelope.py:78`). It gates tests and a demo script only.
- Citation verification exists and is genuinely good — unknown markers are dropped and counted, surfaced as `citation.dropped_unknown_marker_count` (`app/orchestrator/citation_builder.py:59-74`) — but it is **observability, not a gate**: a hallucinated citation lowers a count and fails nothing.
- Latency and token-budget assertions do not exist.

**3.4 Judge calibration — ABSENT.** The framework says "do not skip this." It was skipped. **Zero human-labelled examples exist**; judge–human agreement has never been measured. Every quality number in this repo therefore rests on an uncalibrated instrument. The judge is at least correctly *pinned* (Sonnet 4.5, `metric_specs.py:32`) and structurally cannot self-grade, so this is fixable without invalidating the harness — but until it's done, section 3.4's verdict stands: we have automated an opinion, not a measurement.

**3.5 Noise taxonomy — ABSENT, and it is blocking a live decision.** Nothing in the repo separates judge drift from task ambiguity. This is not academic: the chunking comparison found that only 4 of 10 questions diverge on recall, and that *"every divergence is a single judge-verdict flip"* (`docs/chunking-strategy-comparison.md:188`). Those 4 are judge-sensitive examples by the definition in 3.5 — and the doc's prescribed remedy, `--slice full`, grows the **sample**, which addresses sampling noise. The observed failure mode is **verdict flips**, which it does not address. Running 76 questions may well return the same null at 7.6× the cost. Nobody has checked, because the check requires distinguishing the two noise sources and no mechanism does.

**3.6 Deterministic harness — SPECCED, NEVER BUILT.** `docs/eval_final/2-evaluation-design.md:50` describes exactly the right thing: a verdict cache keyed by `repro_key` (pinned `prompt_template_version` plus dated judge model ID). Of that design block, **only temperature-0 shipped** (`metric_specs.py:46`) — and temperature 0 is not determinism. There is no cache anywhere in `services/eval/`, so every run re-bills every judge call and re-rolls every verdict; two runs of the same config are not strictly comparable, and a run that dies partway resumes from nothing.

One point genuinely in Crucible's favour: **references are static.** The GST loader yields a fixed `reference_answer` from vendored JSONL (`gst_legal_rag.py:81`), so the reference axis of the 3.6 cache is unnecessary. Crucible needs half the cache — the verdict axis only.

**3.7 Layered evaluation — structurally possible, not yet exploited; seams untested.** `context_precision` and `context_recall` are retrieval-side, so the layers *are* separable, and `docs/chunking-strategy-comparison.md:66` correctly held retrieval fixed while varying chunking. No retrieval-only tuning pass has been run.

The seam half is worse. `make opensearch-smoke` runs **one hardcoded query** end-to-end, and its own comment says it "replaces any pytest live test" — a seam suite of size 1. Two seam defects are already on the books and both match the pattern 3.7 warns about:

- **The cost export** (Finding 1) — the test mocks the far side of the boundary, so it passes while the boundary is broken.
- **The field-name mismatch** — the orchestrator defaults to `text`/`embedding` while the ingestion service writes `content`/`content_vector`. Both sides' tests pass in isolation. Any index built by the ingestion service raises `KeyError` in `hits_to_retrieved_chunks` unless `ORCHESTRATOR_OPENSEARCH_TEXT_FIELD`/`_VECTOR_FIELD` are overridden. The defaults match the pre-existing `legal-rag-bench` index, not service-ingested ones — a seam that only breaks in the combination nobody exercises.

### Phase 4 — The bake-off: PARTIAL

One comparison exists and it is honest. `docs/chunking-strategy-comparison.md` ran three chunking arms (whole-passage / recursive / fixed 256-50) with retrieval held fixed, and concluded (`:188`): *"At this sample size: no strategy separates from the others… every divergence is a single judge-verdict flip"* — and named its own remedy (`:201`): *"A decision needs `--slice full`."*

**That is section 4.2 discipline applied correctly, and it should be said plainly: the team reported a tie rather than manufacturing a winner.**

What's absent: no *model* bake-off has been run (only chunking), and **section 4.4's cost-per-correct-answer is currently impossible to compute** — see section 12.

### Phase 5 — Safety: PARTIAL

Two guard lanes ship and genuinely run. Input is a regex pre-filter (prompt-leak, jailbreak, unicode evasion including BIDI overrides) escalating to a Bedrock Haiku classifier **only on a hit**, so cost on benign traffic ≈ 0 (`app/orchestrator/guardrails.py:583-699`). Output is pure regex — secrets block, PII redacts or flags — with three deliberate false-positive suppressors motivated by the legal corpus: citation-marker skipping, a Luhn gate so exhibit numbers aren't masked as credit cards, and separator requirements so statute numbers aren't read as SSNs (`guardrails.py:702-793`). Input fails **safe**; output fails **open**. This is careful work.

What section 5 asks for and does not exist:

- **No red team has been run.** PyRIT and deepteam are vendored clones under gitignored `references/` with **zero first-party imports**; `.pyrit-venv` has `pyrit 0.14.0` installed and is referenced by nothing in the repo.
- **Block rate and FP rate have never been measured.** The harness that measures exactly those four things is written and tested (`services/eval/app/phoenix/guardrail_ab.py:220-266`), but its driver **unconditionally refuses to run** (`services/eval/scripts/guardrail_shadow_ab.py:56-62`, `raise SystemExit`), the arms it needs (`build_in_house_arm`, `build_nemo_arm`, `load_legal_corpus`) exist only inside a docstring, and no result artifact exists on disk. This is deliberate — it's user-owned and gated on a live pod — but the framework's point stands: **the NeMo lane's go/no-go depends on a measurement that has not occurred.**
- **No indirect injection test.** Nothing tests instructions hidden inside *retrieved chunks* — precisely the attack section 5 flags as "the RAG-specific attack most teams forget." Unit tests cover one exemplar per rule (33 tests, with genuine FP cases), which is rule verification, not attack-surface measurement.
- The NeMo pod itself has **never run live**: `image_digest: "pending-ci-build"` (`legal-rag-default-1.5.0.yaml:113`), no compose or infra entry, `ORCHESTRATOR_NEMO_GUARD_URL` unset so the client resolves to `None`.

### Phase 6 — CI/CD gate: ABSENT

There is **no CI, and there never has been** (`git log --all` over `.github` and `.gitlab-ci.yml` returns zero commits). The project states this itself, at `README.md:303`: *"pre-commit is the only gate (no CI)."*

Pre-commit is real — nine hooks covering ruff, import-linter, pytest for eval and orchestrator, grep-gates, and OpenAPI drift plus oasdiff breaking-change classification. But it is **bypassable with `git commit --no-verify`** and covers neither `ingestion`, `guardrail`, nor `demo_ui`: **98 test functions gated by nothing.** No mypy. No coverage threshold. No `make test`.

The regression gate section 6 is built around **cannot fire**: `check_regression` (`services/eval/app/metrics/regression_check.py:44-45`) reads `metrics[*].severity == "blocker"`, but the pipeline emits `metrics_avg` with no `severity` field — so even if it were called, every metric would `continue` and no regression could ever trip. It has **zero callers**, and the project's own docs mark it orphaned.

**The counterweight.** Section 6's *other* demand — "everything is versioned together" — is the best-implemented idea in the repo. Released configs are immutable and hash-verified on **every resolution**, raising `ConfigIntegrityError` on drift (`app/config.py:175-186`, manifest at `app/configs/manifest.sha256`). **Prompt template text is inlined as a block scalar specifically so `config_sha256` covers every prompt byte** (`app/schemas/pipeline_config.py:13-15`) — identical config hash therefore *literally* means identical behaviour. `deepeval==4.0.5` is pinned exactly, with the comment that a bump is "a deliberate re-baseline EVENT, never a routine dependabot merge." This is section 6's reproducibility clause done better than most production teams manage — it is simply not attached to anything that runs.

### Phase 7 — Production loop: ABSENT

Not applicable yet, with one caveat worth recording now. Span instrumentation is thorough and kind-correct (OpenInference conventions; RETRIEVER/EMBEDDING/LLM/CHAIN spans; the Chainlit UI injects `traceparent` so each turn is one joined trace). But it exports to a `localhost` Phoenix on SQLite with an unpinned `:latest` tag, and **sampling is explicitly absent** (`app/observability.py:134`: *"no sampling logic is added here"*) — always-on.

Section 7's 1–10% judge sampling, drift detection, canary/shadow on real traffic, feedback capture, and annotation write-back are all design prose in `docs/eval_final/`, not code.

## 12. Three findings that change what the numbers mean

### Finding 1 — Cost tracking does not work, and the test says it does

**BROKEN.** `app_cost_usd` and `total_cost_usd` are **`NaN` in every committed run**, and `judge_cost_usd` is **always `0.0`**. There is no pricing table anywhere in the repo — cost is read off external objects that don't carry it. `experiments.py:365` reads `cost` and `:408-411` reads `latency` from Phoenix's `ExperimentRun`, which has neither field (it exposes `start_time`/`end_time`). DeepEval's `evaluation_cost` stays `None` because `AmazonBedrockModel` is constructed without `cost_per_*_token` and the configured judges have no price in its registry.

**The unit test guarding these columns fabricates a dict carrying `"cost": 0.0012` and `"latency": 1500` keys the real object doesn't have** (`test_experiments.py:324-325`) — so it passes green while the live path returns nothing.

Consequence: section 4.4's cost-per-correct-answer and section 8's "cost per token is the wrong unit" cannot currently be honoured. Anyone reading `total_cost_usd` in a CSV is reading an empty column, not a cheap result.

### Finding 2 — A "skipped" result scores 0.0 and is averaged in

**DISTORTING.** Missing context or answer returns `score: 0.0, label: "skipped"` (`app/phoenix/evaluators.py:161-168`), and the summary filters on `error`, not on the label (`experiments.py:483-490`). A retrieval miss is therefore indistinguishable from a genuine quality-zero and silently drags the mean — the exact "averages hide the failure mode" pathology of anti-pattern 04, one layer down.

It is also the noise-conflation error of 3.5 in miniature, and worth seeing that way: a retrieval miss (a pipeline failure, fixable, epistemic) and a quality-zero (a generation failure, a different fix entirely) are collapsed into one number, which the harness then averages and reports as if it measured a single thing. This is noise manufactured by the testing process, not noise observed in the model — the cheapest possible class of bug to fix, and currently silent.

### Finding 3 — `.env.example` ships a judge the policy forbids

**TRAP, not an active fault.** `.env.example:54` sets `EVAL_JUDGE_MODEL=au.anthropic.claude-opus-4-6`, against a stated policy of "sonnet/haiku only, never opus" — Opus deprecates the `temperature` param the judge always sends, so it fails at Bedrock with `ValidationException`. Env beats YAML and `load_dotenv()` runs at import, so `cp .env.example .env` yields a broken judge; the `assert_distinct` guard only checks judge≠generator and lets it through.

The live `.env` is correct (Sonnet 4.5), so **nothing is broken today** — this is a trap for the next person.

## 13. Self-score against the section 8 checklist

The framework says: fail the review if any of these are true. Scored honestly.

| # | Anti-pattern | Verdict | Evidence |
|---|---|---|---|
| 01 | Eval set < 50, conclusions without variance | **PARTIAL** | 76 available, but decisions taken at n=10; mitigated by the chunking doc reporting the tie honestly |
| 02 | Judge never calibrated against humans | **FAIL** | Zero human labels exist |
| 03 | Candidate model judging itself | **PASS** | Hard invariant, runtime-enforced (`bedrock_provider.py:76`) |
| 04 | Only aggregate scores reported | **PASS** | Per-query CSV with per-metric verdicts and reasons |
| 05 | Optimizing the metric, not the outcome | **UNTESTED** | No rubric rotation, no human audit of high scorers |
| 06 | Eval set leaked into training | **RISK** | Both datasets are public — see below |
| 07 | Demo-driven selection | **PARTIAL** | `demo_ui/TEST_QUESTIONS.md` probes are human-run and unrecorded |
| 08 | Safety as a footnote | **FAIL** | No red-team pass; FP rate never measured |
| 09 | Cost per token, not per correct answer | **FAIL** | Cost columns are `NaN`; can't compute either unit |
| 10 | No re-evaluation trigger | **FAIL** | Nothing scheduled; no expiry on any decision |
| 11 | Unpinned model versions | **PASS** | The strongest area in the repo |
| 12 | Eval spend unbudgeted | **UNKNOWN** | `max_concurrent` is resolved and never consumed (`bedrock_provider.py:342-353`) |
| 13 | Task ambiguity and judge error collapsed | **FAIL** | No separation attempted. Finding 2 does it literally: a retrieval miss scores 0.0 alongside a quality-zero |
| 14 | Components validated, seam never exercised | **FAIL** | `opensearch-smoke` is one query; the cost seam is mocked; the field-name seam breaks only in combination |
| 15 | Regenerating what could have been cached | **PARTIAL** | References are static (good, and half the problem avoided); no verdict cache, so every run re-rolls and re-bills |

**On #06, which nobody has looked at:** both datasets are **public**. `isaacus/legal-rag-bench` is a HuggingFace dataset, and the GST corpus is generated from the ATO Legal Database (`docs/guides/gst-corpus-generation.md:11`). Frontier models plausibly memorized both. Section 4.6 warns that contaminated evals flatter models that will disappoint in production — and a faithfulness score of **0.9875** on `gst_nano` is exactly the kind of number that should invite suspicion rather than celebration. Until a held-out set of questions written against private or post-cutoff documents exists, high scores here are not evidence of retrieval quality; they may be evidence of recall.

## 14. What section 9's "minimal viable version" would cost from here

Mapping the five-item MVP onto current state, in dependency order. The ordering is not arbitrary: steps 2 and 3 are what make step 5 worth running at all, and an earlier draft of this plan had step 5 second — before it was clear that more samples do not fix judge noise.

| # | Step | Addresses | Why this order |
|---|---|---|---|
| 1 | **Calibrate the judge** — 50–100 human labels against existing GST questions, measure agreement | 3.4, anti-pattern 02 | Nothing else in Part II can be trusted until this exists, and it needs no new infrastructure |
| 2 | **Build the verdict cache** — key on (sample, model output, judge config, metric); the `repro_key` already specced in `eval_final` | 3.6, anti-pattern 15 | A multiplier, not a step. It makes step 1 re-runnable, step 3 affordable, step 5 resumable, and it is the precondition for telling drift from signal |
| 3 | **Judge-sensitivity probe** — rerun the 4 divergent chunking questions 5× each, and again under a rotated judge | 3.5, anti-pattern 13 | ~20 judge calls. Tells you whether step 5 will separate anything *before* you pay for step 5. `EVAL_JUDGE_MODEL` swaps Sonnet 4.5 → Haiku 4.5 within the existing sonnet/haiku-only policy |
| 4 | **Fix the cost path, or delete the columns** | Finding 1, anti-patterns 09 & 14 | An always-`NaN` column that a green test defends is worse than an absent one |
| 5 | **Run the full slice** (`--slice full`), with **judge rotation as the acceptance condition** rather than a threshold | 3.1, 3.5, Phase 4 | Conditional on step 3. 76 questions is one run away and still below the 100 the framework asks for, so consider growing the set at the same time. A result that flips under judge swap is not a result |
| 6 | **Build the adversarial set** | 3.2, anti-pattern 08 | The largest genuine gap. Also unblocks the NeMo A/B: `guardrail_ab.py` is finished and waiting on a labelled corpus and three functions that exist only in a docstring |
| 7 | **Attach one gate to something that runs** — one nightly job on the full slice vs. a stored baseline | Phase 6, anti-pattern 10 | Not full CI. `check_regression` needs its schema reconciled with `metrics_avg` before it can be that gate |

Step 6 is also what makes the **regression flywheel (Phase 7)** possible at all — there is currently no artifact for a production failure to be *added to*, which is why section 7's "single most important habit" has no landing site in this repo yet.

**A cheap seam suite is missing from this list deliberately.** Section 3.7 calls for a small representative set through the whole path, and Crucible has the raw material for it — `opensearch-smoke` (one query), plus two known seam defects that would seed it honestly. But 3.7 says to weight it by real traffic and seed it with real incidents, and Crucible has neither. Build it when there is traffic to weight by; until then, growing it past a handful of hand-picked cases is guesswork dressed as coverage.

---

## Appendix: what Crucible actually uses

An earlier draft of this document mapped the framework to **Bedrock-native** tooling. Crucible chose differently on two rows; the table below reflects what is actually built.

| Framework component | What Crucible uses | Notes |
|---|---|---|
| Offline bake-off | Phoenix + DeepEval, self-hosted | Not Bedrock Evaluations. Portable; judge pinned to Bedrock Sonnet 4.5 in ap-southeast-2 |
| RAG-layer evaluation | Four DeepEval metrics via Phoenix experiments | Faithfulness, context precision, context recall, answer relevancy — all at a uniform 0.7 threshold |
| Programmatic checks | Own harness — pre-commit only | No managed substitute; ordinary software testing, and it is not attached to CI |
| Safety guardrails | In-house regex + Bedrock Haiku classifier | Not Bedrock Guardrails. A NeMo Guardrails pod is staged behind an A/B that has never run |
| Ranking / reranking | OpenSearch hybrid BM25 + Titan k-NN | Fused server-side: min-max normalization, arithmetic mean, 0.5/0.5 weights, top-k 8 |
| Judge calibration, flywheel, canaries | Nothing yet | The parts no vendor sells — and the parts that matter most |

**On ranking.** There is no ranking *model*. Reranking is parked, and `services/orchestrator/app/orchestrator/reranker.py` is a typed identity function holding the seam open at its natural position in the chain. Ranking today is an untuned hybrid-fusion default that has never been compared against any alternative. The Bedrock Rerank API remains unavailable in ap-southeast-2 as of mid-2026, so adopting it would mean a cross-region call that must clear the same residency gate as everything else in Phase 2.

---

## Sources

Sections 3.5 (noise taxonomy), 3.6 (deterministic harness), and the seams half of 3.7 are adapted from Baharak Saberidokht, *"From weeks to a day: how we made LLM evaluation fast enough to iterate on"* (Airbnb engineering, 2026). The measured figures quoted in 3.5 — ~1% judge drift against a 1–3% real signal, ~75% of LLM-generated references differing across labeling runs — are Airbnb's, on Airbnb's workload. They are cited here as an existence proof that the noise floor can exceed the signal, not as constants to design against. Measure your own.

That article frames the work as four layers. Three are folded into this document. **Layer 3 — micro-LoRA adapters for same-day model patching — is deliberately omitted**, because Crucible trains nothing: it calls hosted Bedrock models, so there is no adapter, no rank, and no GPU hour to spend. The discipline underneath that layer does translate, and is worth stating in Crucible's own terms: *bounded, scoped mutation, validated behind two gates, canary-deployed with automatic rollback.* **Crucible's config semver is its adapter.** A 1.4.0 → 1.5.0 change is scoped, hash-pinned, and `services/orchestrator/docs/guardrails.md` already claims one-line rollback via config-ref swap. What is missing is the two gates and the canary — which is precisely the NeMo A/B that has never run.

The underlying research is worth reading directly rather than through either summary:

- Abbasi Yadkori et al. (NeurIPS 2024) — separating epistemic from aleatoric uncertainty; conflating them misclassifies high-entropy responses as hallucinations. The basis of 3.5.
- Sculley et al. (NeurIPS 2015) — *Hidden Technical Debt in Machine Learning Systems*, and the CACE principle: changing anything changes everything. The basis of 3.7's seams argument, and a decade old — which is the point.
- Kästner et al. (2021) — ML components resist compositional reasoning; their interactions can only be observed empirically.
- Ling et al. (2024) — epistemic uncertainty is the more actionable signal; aleatoric reflects properties no judge improvement resolves.

The transferable thesis, and the reason this material was folded in rather than linked: LLM pipelines are not categorically new. They are pipelines with a nondeterministic component in the middle, which makes the seams harder to reason about and more important to test. The leverage sits in ordinary systems engineering applied where the new failure modes actually live — which, for Crucible today, is the harness, not the model.
