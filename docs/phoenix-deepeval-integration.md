# Developer Guide: Phoenix ↔ DeepEval Integration

**Audience:** developers working on `src/crucible/service/phoenix/` and the `eval-rag` path.
**Status:** current as of 2026-06-28 (post Flow-A retirement, Bedrock-only).
**Supersedes:** `docs/phoenix-audit.md` (historical — it still describes the now-deleted Flow A).

---

## 1. Mental model (read this first)

Two tools, two jobs — keep them separate in your head:

- **DeepEval = the grader.** It computes the four RAG metrics (faithfulness, contextual
  precision, contextual recall, answer relevancy) using an **LLM judge**. It knows nothing about
  Phoenix.
- **Phoenix = the eval store + UI.** Its **Datasets & Experiments** feature stores a *scored
  experiment* you can browse per-question, and the run also writes local CSV/Parquet/JSON.

There is **exactly one** integration path now (internally called "Flow B"): Phoenix's native
`run_experiment` API, with DeepEval metrics wrapped as Phoenix evaluators. The old span-tracing
path ("Flow A", `PhoenixAdapter`) was **deleted** — do not look for it.

```
eval-rag  →  run_phoenix_native  →  run_phoenix_experiment
                                        ├─ create_phoenix_dataset   (upload slice to Phoenix)
                                        ├─ create_rag_task          (wrap the RAG adapter)
                                        ├─ _build_judge             (→ AmazonBedrockModel instance)
                                        ├─ create_*_evaluator × 4   (DeepEval metric → Phoenix Score)
                                        └─ client.experiments.run_experiment(...)
                                              → Phoenix Datasets & Experiments  +  export_experiment_results (CSV/Parquet/JSON)
```

Files: `service/phoenix/experiments.py` (orchestration) and `service/phoenix/evaluators.py`
(the four metric wrappers). That's the whole package now (~1,170 lines, down from ~2,020).

---

## 2. The #1 footgun — the judge MUST be a Bedrock *instance*, never a string

This is the single most important thing to understand. **DeepEval routes a bare model *string*
to its OpenAI `GPTModel`.** So if you ever pass `judge_model="au.anthropic.claude-sonnet-4-5..."`
(a string) into a DeepEval metric, DeepEval will try to construct an **OpenAI** client and fail
with `OPENAI_API_KEY is not configured` — even though your string is a Bedrock id.

The integration defends against this in two ways; **do not weaken either**:

1. **`_build_judge(judge_model)` converts the id into an `AmazonBedrockModel` instance** via
   `get_deepeval_llm(provider="bedrock", model=...)`. The evaluators receive this *instance*,
   and `FaithfulnessMetric(model=<instance>)` then uses the native Bedrock path.
2. **`judge_model` is a REQUIRED parameter** (no default) on `run_phoenix_experiment` and on all
   four `create_*_evaluator` factories. A string *default* there was the exact bug that shipped
   silently — it is gone, keep it gone.

> Rule: anything that ends up as a DeepEval metric's `model=` must be an `AmazonBedrockModel`
> instance, produced by `get_deepeval_llm(provider="bedrock", ...)`. Never a bare string.

---

## 3. Model policy: Bedrock only, sonnet/haiku, never opus

- **Bedrock only.** OpenAI/gpt-4o was removed. `provider="openai"` raises "Bedrock-only".
  `get_deepeval_llm` only builds `AmazonBedrockModel`.
- **Judge = Sonnet, Generator = Haiku — never Opus.** Opus models (e.g. `claude-opus-4-8`)
  **deprecate the `temperature` inference param** that the judge sends → Bedrock returns
  `ValidationException: temperature is deprecated for this model`. Sonnet 4.5 and Haiku 4.5 both
  accept `temperature`. Current defaults:
  - judge `au.anthropic.claude-sonnet-4-5-20250929-v1:0`
  - generator `au.anthropic.claude-haiku-4-5-20251001-v1:0` (via `.env`)
- **`judge ≠ generator`** is a hard invariant (`assert_distinct` in `kernel/rag_metrics/
  metric_specs.py`) — no same-model self-grading.
- **Region** must be set (`AWS_REGION=ap-southeast-2` for `au.*` profiles); no implicit
  `us-east-1` (residency hazard).
- **Config precedence gotcha:** the CLI runs `load_dotenv()`, and **`.env` overrides
  `eval_config.yaml`** (env > YAML). If the judge model is not what you expect, check `.env`
  first.

---

## 4. How an evaluator wrapper works (`evaluators.py`)

Each `create_*_evaluator(judge_model, embedder=None)` returns a Phoenix evaluator function
(decorated with `@create_evaluator`) that Phoenix calls once per dataset example. Inside:

1. Extract `output` (the RAG answer + retrieval_context) and `input`/`expected` from the example.
2. Build the DeepEval metric **per call** (thread-safety under Phoenix's concurrency):
   `FaithfulnessMetric(model=judge_model, include_reason=True)` — where `judge_model` is the
   **Bedrock instance** from `_build_judge`.
3. `metric.measure(test_case)` **inside `_suppress_tracing_if_available()`** — this suppresses
   the judge's own internal LLM spans so they don't pollute the experiment trace. (This is a
   private helper local to `evaluators.py`; the old `adapter.suppress_tracing_if_available` is
   gone.)
4. Return a Phoenix `Score(name, score, label, explanation, metadata=verdicts...)`.

The four wrappers differ only in which metric they build and which `LLMTestCase` fields they
populate (faithfulness ignores `expected`; recall/precision use it; etc.).

---

## 5. Running it

```bash
# Phoenix MUST be running (Flow B uploads a dataset + runs an experiment on the server).
crucible check phoenix                      # fail-fast preflight

PHOENIX_ENDPOINT=http://localhost:6006 AWS_REGION=ap-southeast-2 \
  uv run eval-rag --slice gst_pico --rag stub-local
```

There is **no `--phoenix-native` flag** — Phoenix-native is the only (default) behaviour. There
is **no offline/CSV-only mode** in the CLI anymore; a reachable Phoenix server is required.

**Output / artifacts** (`export_experiment_results`):
- In Phoenix UI → **Datasets & Experiments** → the scored experiment, browsable per question.
- On disk → `results/eval_rag/<timestamp>/experiment-*_results.{csv,parquet}` + `_summary.json`.
  Columns include per-metric `*_score`, `*_label`, `*_verdicts`, and `*_cost_usd`. This is the
  **canonical artifact** (richer than the old Flow-A `results.csv`, which is gone).

---

## 6. Testing

- **`tests/service/phoenix/test_experiments.py`** — unit tests with a **mocked Phoenix `Client`**
  + a **stub judge**. They assert the integration contract without a server or live Bedrock:
  `_build_judge` forces `provider="bedrock"` and rejects non-bedrock; the four evaluators are
  built once and passed in; `export_experiment_results` emits the expected columns. **Run these
  in CI.**
- **`tests/service/phoenix/test_experiments_live.py`** — `@pytest.mark.phoenix_integration`,
  skips cleanly unless a real Phoenix server is reachable (`PHOENIX_ENDPOINT`). Marker is
  registered in `pyproject.toml` + `tests/conftest.py`.
- **`tests/service/phoenix/test_no_flow_a.py`** — grep-gate: fails if `PhoenixAdapter`, a
  `phoenix=` parameter, or a `service.phoenix.adapter` import reappears in `src/`. Keep it green.

When writing new tests for the judge, **never let a bare model string reach a real DeepEval
metric** — use a stub judge or a mocked `get_deepeval_llm`, or you'll hit the OpenAI footgun.

---

## 7. Gotchas checklist

- [ ] Judge passed as a **string** anywhere a DeepEval metric sees it → OpenAI error. Always an
      `AmazonBedrockModel` instance via `get_deepeval_llm(provider="bedrock", ...)`.
- [ ] Opus judge model → `temperature is deprecated` Bedrock error. Use sonnet/haiku.
- [ ] `judge == generator` → `assert_distinct` raises. Keep them different.
- [ ] `.env` silently overriding `eval_config.yaml` for the judge model. Check `.env` first.
- [ ] `AWS_REGION` unset → loud failure (intentional, no us-east-1 default).
- [ ] Phoenix server down → `eval-rag` fails (by design). Run `crucible check phoenix` first.
- [ ] Adding a default value to `judge_model` "for convenience" → re-arms the footgun. Don't.

---

Still present but **orphaned** (follow-up removal candidates, not used in the live path):
`service/metrics/regression_check.py`, `service/metrics/csv_writer.py`. `run_golden_set` the
*function* is intentionally kept (it's the importable CSV/RunResult engine + the reserved replay
engine), just no longer wired to Phoenix.
