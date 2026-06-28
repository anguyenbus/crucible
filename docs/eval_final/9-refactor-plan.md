# Crucible Refactor Plan v2 — Shape It for the `services/eval/` Migration

**Branch:** `bedrock` → refactor branch `restructure/eval-shape`
**Objective:** refactor crucible *in place* so the later move into `genai-backend/services/eval/app/` is a directory copy plus one mechanical, machine-verified import rewrite — no redesign, no untangling, no surprises on migration day.
**Success criterion (binding):** a CI job (`rehearse-migration`) copies crucible into a **skeletal but real** `services/eval/` destination on every PR, runs `uv sync --frozen` against the destination's own lockfile, asserts zero surviving `crucible.*` references, import-smokes every module, and runs the test suite. Green = the migration is continuously proven, not merely planned.

**What changed from v1 (review against the live `bedrock` branch and the `deepeval==4.0.5` wheel):**

| # | v1 said | v2 says | Why |
|---|---|---|---|
| 1 | Telemetry opt-out lives only in `service/deepeval/__init__.py`; "the service imports deepeval only through this package" | Opt-out fires on **both** import paths — the **kernel** also imports deepeval (`evaluator`/`samples`). Single allowlisted env write in `kernel/rag_metrics/__init__.py` + the service one; two subprocess tests; egress backstop noted (deepeval ships `sentry-sdk` + `posthog`) | v1's invariant was structurally false: its own Phase-2 clean-venv kernel test would import deepeval with telemetry on |
| 2 | "openai leaves core" | openai leaves **direct** deps only; it returns transitively via `deepeval==4.0.5` (verified wheel metadata: hard-requires `openai`, `python-dotenv`, `sentry-sdk`, `posthog`, `pytest`+plugins). Contract test scoped to direct deps; runtime `test_no_openai_guard` stays the real enforcement | An install-time assertion against the lockfile can never pass |
| 3 | Schema packaging deferred to Phase 5; "mechanical move" | `rag_adapter.py:90` hardcodes `Path("src/crucible/contracts/…")` (CWD-relative). Fixed in **Phase 2** when the validator moves: injectable schema path, `importlib.resources` default | The copy is not mechanical until this lands; the backend swap (next row) detonates it |
| 4 | hatchling implicit | **hatchling will not be used.** Backend swap to `uv_build` is a Phase-0 commit; rehearsal validates packaging via `uv sync` | Team decision; hatchling's free inclusion of `src/crucible/contracts/*.json` disappears with it |
| 5 | `claim_check: Callable[[str], CachedScore \| None]` | Two-phase **claim/finalize** protocol | A read-only lookup can't express claim-before-Bedrock-call + finalize-after-write; v1's shape guaranteed a monorepo reshape |
| 6 | Rehearsal: PYTHONPATH a `/tmp` namespace package; sed two rules | Rehearse into a real skeleton with `app/__init__.py`, per-service `uv.lock`, fixtures (`contracts/`, `eval_config.yaml`); sed hardened + **post-sed zero-reference assertion** + recursive import-smoke | v1's sed missed `from crucible.service import X` and string-literal module paths; untested modules (phoenix clients, replay tasks) could pass green with broken imports |
| 7 | `comparison.py`, `schema_validator` move as-is | **Tests written first** (currently zero coverage on both); scipy promoted to core **and the silent sign-test fallback deleted** (`comparison.py:189-194` silently swaps Wilcoxon for a sign test when scipy is missing — a live correctness bug) | "Kernel" status requires tests; the rehearsal's guarantee is only as strong as the suite |
| 8 | Pin-collision risk flagged to platform owner | **Resolved: per-service `uv.lock`.** Risk row deleted; replaced by lockfile review discipline (transitive re-locks can move judge behaviour even with the `==` pin intact) | Decision made |
| 9 | Monorepo layout "fixed" | Confirmed: `services/eval/app/` `ragas/`/`mlflow/` entries are **placeholders, replaced wholesale** by this layout (team uses DeepEval + Phoenix; mlflow and ragas will not be used). Recorded here and to be noted in the monorepo tree doc | Kills the tribal-knowledge trap |

Scope note: this plan is **service-level only**. Integration with `genai-backend/libs/*` is explicitly out of scope and is monorepo work.

---

## 0. The one idea everything below serves

The monorepo target layout (placeholders replaced):

```
services/eval/app/{ kernel/ · deepeval/ · phoenix/ · datasets/ · metrics/ · runners/ · schemas/ · config.py }
```

crucible's internal layout is refactored to mirror it one-to-one, under two top-level subpackages with a strictly one-way dependency:

```
crucible.kernel.*    →  copies to  app/kernel/*      (pure; no infra, no env reads, no network)
crucible.service.*   →  copies to  app/<dir>/*       (deepeval/, phoenix/, datasets/, metrics/, runners/, config)
```

Everything that will **not** migrate (demo stubs, CLI shells, local docker-compose) is quarantined under `crucible.local` where the copy never touches it. The import rewrite is one rule, **verified after the fact rather than trusted**:

```
crucible.kernel.X   →  app.kernel.X
crucible.service.X  →  app.X
…then: assert no string "crucible." survives anywhere in the copied tree.
```

Two import disciplines make the rewrite mostly unnecessary: (1) **within** a subpackage, relative imports only; (2) **across** the kernel/service boundary, absolute `crucible.kernel.…` imports, service-side only. Both are enforced by the import linter; the post-sed assertion (§4 Phase 6) catches anything the disciplines or the sed miss — including string-literal module paths in tests.

---

## 1. Phase 0 — Lock decisions before touching code

| # | Decision | Status / default | Why locked first |
|---|---|---|---|
| 0.0 | **Destination confirmed.** `services/eval/app/` is replaced wholesale by this layout; `ragas/` and `mlflow/` in the tree doc are placeholders and **will not be used**. | **Decided.** Add a one-line note to the monorepo tree doc. | Two sources of truth must not disagree on the destination of a rehearsed migration. |
| 0.1 | **Python version.** crucible pins `>=3.13` (Dockerfile: `python:3.13-slim`); confirm the monorepo `services/*` toolchain minor. | align crucible to the monorepo's exact minor | The one thing that can hard-block the copy. |
| 0.2 | **`deepeval==4.0.5` exact pin** carries into `services/eval/pyproject.toml` verbatim, with the "bump = re-baseline event" rule and the dependency-contract test. | yes, verbatim | Judge-drift protection. Per-service `uv.lock` (decided) means no cross-service pin conflict is possible — but see the lockfile-discipline invariant (§7.8): `uv lock --upgrade` can move deepeval's *transitives* (openai, tenacity, …) without touching the pin. |
| 0.3 | **Lint/format parity.** Copy the monorepo ruff/format settings into crucible now; land as one isolated formatting commit before any structural change. | adopt monorepo config | Otherwise migration day buries the real diff under a reformat. |
| 0.4 | **`beartype` decorators** stay or go in the monorepo. | stay (small dep, real guards) | Stripping later is invasive. |
| 0.5 | **Claims/idempotency seam.** Runners accept a two-phase protocol, not a lookup: | locked as the protocol below | A `(key) -> CachedScore \| None` getter cannot express *claim atomically before the Bedrock call, finalize after the durable write* — the actual idempotency model (DynamoDB conditional claim → write → finalize). Locking a getter now guarantees a signature reshape later, the exact thing this decision exists to prevent. |
| 0.6 | **Build backend: hatchling out** (team decision). Swap to **`uv_build`** in the same Phase-0 window as 0.3, its own commit. | `uv_build` | Backends differ in which non-Python files ship: hatchling was including `src/crucible/contracts/*.json` for free; after the swap, the CWD-relative path hack at `rag_adapter.py:90` would be the only thing finding schemas — and only from repo root. Swapping early means the rehearsal validates the *real* packaging behaviour from day one (verify the json data files are declared/included under `uv_build`, and that `uv build` + install-from-wheel still passes the kernel tests). |
| 0.7 | **Telemetry opt-out placement** (resolves the kernel/deepeval contradiction — see §4 Phase 4). | the dual-site carve-out below | The kernel imports deepeval (evaluator/samples). An opt-out living only service-side leaves every direct kernel import — including this plan's own clean-venv kernel test — running deepeval with telemetry on. |

**0.5 — the locked protocol** (kernel `interfaces.py`; implemented in the monorepo, no-op default in crucible):

```python
class ClaimStore(Protocol):
    def claim(self, repro_key: str) -> Claimed | CachedResult:
        """Atomically claim repro_key. CachedResult => already scored, skip the Bedrock call."""
    def finalize(self, repro_key: str, result_uri: str) -> None:
        """Mark the claim complete after the durable write."""
```

**0.7 — the locked telemetry rule:** `DEEPEVAL_TELEMETRY_OPT_OUT="YES"` is written in exactly **two** places, each immediately before that package's first `import deepeval`: `kernel/rag_metrics/__init__.py` (the kernel's only deepeval importer) and `service/deepeval/__init__.py`. The kernel-purity grep-gate carries a single-file allowlist for the former. Each site gets a subprocess regression test (§4 Phase 4). **Backstop:** `deepeval==4.0.5` also vendors `sentry-sdk` and `posthog`, which the env var does not govern — in the monorepo, the service's egress policy (no outbound to Sentry/PostHog endpoints) is the hard guarantee; the env var is defence-in-depth, not the perimeter. Record this in the service's deployment notes.

**Exit criteria:** decisions recorded at the top of crucible's README; ruff-config commit and `uv_build` commit landed (separately) before any structural change.

---

## 2. Target end-state layout of crucible

```
crucible/
├── pyproject.toml                  # uv_build backend; re-cut deps (§4 Phase 5); console scripts = thin wrappers
├── uv.lock                         # per-service lock model; review-flagged on change (§7.8)
├── contracts/                      # SINGLE source of schemas (vendored src/ copy deleted in Phase 5)
│   ├── eval_questions.schema.json
│   ├── legal_rag_bench_query_output.schema.json   # ← was missing from v1's inventory
│   ├── parser_output.schema.json                  # ← exists ONLY here today (not in src/ copy)
│   └── rag_query_output.schema.json
├── src/crucible/
│   ├── __init__.py                 # re-exports + __version__ only; NO os.environ writes (moved — §4 Phase 4)
│   │
│   ├── kernel/                     # ══ copies verbatim to app/kernel/ ══
│   │   ├── rag_metrics/
│   │   │   ├── __init__.py         #   sets DEEPEVAL_TELEMETRY_OPT_OUT *before* importing deepeval
│   │   │   │                       #   (the kernel's only env write — allowlisted; decision 0.7)
│   │   │   ├── evaluator.py        #   from adapters/deepeval_adapter.py: DeepEvalEvaluator,
│   │   │   ├── samples.py          #     compute_metrics(+timing), transform_to_deepeval_sample
│   │   │   └── metric_specs.py     #   pure half of metrics/deepeval_config: metric registry,
│   │   │                           #     thresholds, judge≠generator invariant, Bedrock/au.* defaults
│   │   │                           #     as the SINGLE source of default constants — the adapter's own
│   │   │                           #     stale openai/gpt-4o-mini defaults (deepeval_adapter.py:17-20)
│   │   │                           #     are DELETED, not moved (§4 Phase 2.4)
│   │   ├── replay_stats/
│   │   │   └── comparison.py       #   Wilcoxon, Cliff's Delta, ComparisonResult — scipy import made
│   │   │                           #     top-level and mandatory; the silent sign-test fallback
│   │   │                           #     (lines ~189-194) is DELETED (§4 Phase 2.2)
│   │   ├── validation/
│   │   │   └── schema_validator.py #   schemas resolved via injectable path; default =
│   │   │                           #     importlib.resources into packaged contracts (§4 Phase 2.3).
│   │   │                           #     The CWD-relative Path("src/crucible/contracts/…") at
│   │   │                           #     rag_adapter.py:90 is eliminated here, not in Phase 5
│   │   └── interfaces.py           #   RagAdapter (stub default REMOVED — callable required),
│   │                               #   JudgeProvider, RateLimiter/BudgetGate, ClaimStore (0.5)
│   │
│   ├── service/                    # ══ each child copies to app/<child>/ ══
│   │   ├── deepeval/
│   │   │   ├── __init__.py         #   second telemetry opt-out site (0.7), before import deepeval
│   │   │   └── bedrock_provider.py #   impure half of metrics/deepeval_config: env reads, dotenv,
│   │   │                           #     boto/session wiring, model resolution, concurrency env
│   │   ├── phoenix/
│   │   │   ├── adapter.py          #   from observability/phoenix_adapter + config
│   │   │   ├── evaluators.py       #   from experiments/deepeval_evaluators
│   │   │   ├── experiments.py      #   from experiments/runner + experiments/datasets
│   │   │   ├── replay_client.py    #   from replay/phoenix_client
│   │   │   └── annotations.py      #   NEW: span/document annotation write-back (API spec §8.2);
│   │   │                           #     integration-tested against docker-compose Phoenix, behind
│   │   │                           #     a pytest marker so the rehearsal stays runnable (§4 Phase 6)
│   │   ├── datasets/               #   from datasets/; cache dirs become config-driven, not
│   │   │                           #     Path("data/…") CWD-relative defaults
│   │   ├── metrics/                #   from reporting/ (regression_check, csv_writer; html → local/cli)
│   │   ├── runners/                #   LIBRARY functions, not __main__ shells:
│   │   │   ├── golden_set.py       #     run_golden_set(dataset, adapter, provider, config,
│   │   │   │                       #                    claims: ClaimStore | None = None) -> RunResult
│   │   │   └── replay.py           #     from run_replay_eval + replay/{tasks,candidate_config,http_client}
│   │   └── config.py               #   yaml + env expansion — the only service-side env entrypoint
│   │                               #     besides deepeval/__init__; dotenv behind from_dotenv=True
│   │
│   └── local/                      # ══ NEVER copied — quarantined ══
│       ├── cli/                    #   crucible / eval-rag / eval-replay / generate-spans shells +
│       │                           #     html_summary; each ≤ ~30 lines; builds stub adapter explicitly
│       └── stubs/                  #   demo RAG (chromadb, sentence-transformers, fake service,
│                                   #     span generator), 3,372 LOC — `demo` extra only
└── tests/
    ├── kernel/                     # mirrors kernel — runs with ZERO extras; includes NEW tests for
    │                               #   comparison.py and schema_validator (currently zero coverage)
    ├── service/                    # needs bedrock/phoenix extras; Phoenix-integration behind marker
    └── local/                      # CLI + stub tests — needs demo extra
```

**Layout rules (enforced by linter + grep-gate, not aspiration):**
1. `kernel/` may import: stdlib, `pydantic`, `jsonschema`, `scipy`, `deepeval`, `beartype`. It may **not** import `boto3`, `phoenix`/`arize`, `dotenv`, `crucible.service`, `crucible.local`, **read** `os.environ`, or **write** it — except the single allowlisted telemetry line in `kernel/rag_metrics/__init__.py` (0.7).
2. `service/` may import `kernel` (absolute) and third-party infra; never `crucible.local`.
3. Nothing imports `local`. (Today **four** call sites do — verified: `adapters/rag_adapter.py:55`, `runners/run_rag_eval.py:95`, `cli/check.py:91`, `cli/check.py:349`.)
4. Within each top-level subpackage: relative imports only. Across: absolute only.
5. **No CWD-relative resource paths anywhere in `kernel/` or `service/`** — grep-gate on `Path("src/`, `Path("contracts/`, `Path("data/`. Resource locations are injected or resolved via `importlib.resources`/config. (CLI defaults like `Path("eval_config.yaml")` are fine *inside `local/cli`* only.)

---

## 3. Current → refactored → monorepo mapping (full inventory, corrected)

| Current location (`bedrock` branch) | Refactored location | Migrates to | Notes |
|---|---|---|---|
| `replay/comparison.py` | `kernel/replay_stats/comparison.py` | `app/kernel/replay_stats/` | scipy import becomes top-level (core dep); **delete the sign-test fallback**; **write unit tests first** — none exist today |
| `adapters/deepeval_adapter.py` | `kernel/rag_metrics/{evaluator,samples}.py` | `app/kernel/rag_metrics/` | split; invert the line-166 reach-in (`create_deepeval_metrics` import) — metrics are passed in; **delete the file's own stale defaults** (`DEFAULT_LLM_PROVIDER="openai"`, `DEFAULT_JUDGE_MODEL="gpt-4o-mini"`, lines 17-20) — `metric_specs.py` is the single source and it is Bedrock/`au.*` |
| `metrics/deepeval_config.py` (444 LOC) | split: constants/invariant → `kernel/rag_metrics/metric_specs.py`; env+boto wiring → `service/deepeval/bedrock_provider.py` | kernel + `app/deepeval/` | the most delicate task; import-time `os.environ` write + `load_dotenv()` (lines 42-45) relocate per Phase 4 |
| `adapters/rag_adapter.py` | `kernel/interfaces.py` + `kernel/validation/` usage | `app/kernel/` | **delete the stub default at line 55** (`query_callable` required → bare `RagAdapter()` becomes `TypeError`; **all existing call sites and tests constructing it bare must be updated in the same PR** — quantify in Phase 1); **replace the hardcoded schema path at line 90** with the injected/`importlib.resources` mechanism |
| `adapters/schema_validator.py` | `kernel/validation/schema_validator.py` | `app/kernel/validation/` | gains the injectable-path API; **gains tests** (none today) |
| `adapters/embeddings.py` | `service/deepeval/` | `app/deepeval/` | keep bedrock path, drop openai path |
| `experiments/{runner,datasets,deepeval_evaluators}.py` | `service/phoenix/{experiments,evaluators}.py` | `app/phoenix/` | Phoenix-coupled by design |
| `observability/*` | `service/phoenix/adapter.py` | `app/phoenix/` | `PHOENIX_ENDPOINT` env read stays service-side |
| `replay/{phoenix_client,http_client,candidate_config,tasks}.py` | `service/phoenix/replay_client.py` + `service/runners/replay.py` | `app/phoenix/` + `app/runners/replay.py` | kept working (Appendix-A machinery) |
| `datasets/*` | `service/datasets/` | `app/datasets/` | HF-token env read stays; `Path("data/…")` cache defaults become config-driven |
| `reporting/{regression_check,csv_writer}.py` | `service/metrics/` | `app/metrics/` | `html_summary.py` → `local/cli/` |
| `runners/run_rag_eval.py` (542 LOC) | logic → `service/runners/golden_set.py`; shell → `local/cli/` | `app/runners/golden_set.py` | strip dotenv/env side effects from the library path |
| `runners/run_replay_eval.py`, `generate_spans.py` | same split | `app/runners/replay.py` / dropped | span generator is demo → `local/stubs/` |
| `cli/*` (incl. `check.py`, 374 LOC) | `local/cli/` | **does not migrate** | thin wrappers after extraction |
| `stubs/*` (3,372 LOC) | `local/stubs/` | **does not migrate** | `demo` extra only |
| `src/crucible/contracts/*.json` (3 files — `parser_output` was never vendored) | **deleted**; top-level `contracts/` (4 files) is the sole source, packaged under `uv_build` | `genai-backend/contracts/schemas/` (new sibling of `contracts/openapi/` — confirm the dir with the platform owner before migration day) | kills the dual-copy drift risk; today the duplicated 3 are byte-identical (verified) |
| `config.py` | `service/config.py` | `app/config.py` | merge target for monorepo pydantic-settings later |
| `tests/*` | mirrored `tests/{kernel,service,local}` | `services/eval/tests/` | `test_dependency_contract.py` **re-anchored** (walks up to the nearest `pyproject.toml` instead of `parents[2]`) so the identical file guards the pin in crucible, in the rehearsal, and in the monorepo; same fix for `test_eval_config_yaml_judge.py`'s `parents[2]/eval_config.yaml` |

---

## 4. Phased execution (each phase = its own PR, green CI throughout)

### Phase 1 — Quarantine (`local/`), cut the stub leaks
1. `git mv` `stubs/` → `local/stubs/`, `cli/` → `local/cli/`.
2. Fix the four stub-leak call sites (verified list in §2 rule 3). `RagAdapter` loses its stub default: bare construction raises `TypeError` with a message pointing at the demo extra. **Update every bare `RagAdapter()` construction in src and tests in this same PR** — this is a breaking API change inside the repo and must not straddle PRs.
3. Console scripts keep working: each CLI builds the stub adapter explicitly and injects it.
4. Add **import-linter** with three contracts (kernel-pure — activates Phase 2; service-not-local; nothing-imports-local) as a required CI step:

```toml
[tool.importlinter]
root_package = "crucible"
[[tool.importlinter.contracts]]
name = "nothing imports local (stubs/cli)"
type = "forbidden"
source_modules = ["crucible.kernel", "crucible.service"]
forbidden_modules = ["crucible.local"]
```

**Acceptance:** linter green; `pip install crucible` (no extras) imports cleanly with no chromadb/sentence-transformers present; CLIs run with `[demo]`.

### Phase 2 — Carve `kernel/` (test-first where coverage is zero)
1. **Write the missing tests before promotion:** unit tests for `comparison.py` (Wilcoxon path, Cliff's Delta values against known fixtures, tie/zero-delta handling) and `schema_validator.py` (valid/invalid/missing-field/unknown-major-version). These modules currently have **zero** coverage; the rehearsal's guarantee is only as strong as the suite, so "kernel" status is earned by tests, not by `git mv`.
2. Move `comparison.py`; make the scipy import top-level and unconditional; **delete the sign-test fallback** (today a broken env silently swaps the statistical method — that is a results-correctness bug, fixed here regardless of the migration). scipy enters core deps (Phase 5 formalises).
3. Move `schema_validator.py` into `kernel/validation/` with the **injectable schema path** API: `validate(payload, schema=..., schema_dir: Path | None = None)`, default resolving via `importlib.resources` into the packaged `contracts/`. Eliminate `rag_adapter.py:90`'s `Path("src/crucible/contracts/…")` in the same PR. Add the no-CWD-path grep-gate (§2 rule 5).
4. Create `interfaces.py` (`RagAdapter`, `JudgeProvider`, `RateLimiter`, `ClaimStore` per 0.5). Split `deepeval_adapter.py` into `evaluator.py`/`samples.py`; invert the line-166 dependency — `DeepEvalEvaluator` receives constructed metric objects (or a `JudgeProvider`), never imports config. **Delete the adapter's own default constants** (openai/gpt-4o-mini) rather than moving them; `metric_specs.py` is the single source and carries the Bedrock/`au.*` defaults and the judge≠generator invariant as a pure function (`assert_distinct(judge_id, generator_id)`) with its unit test.
5. `kernel/rag_metrics/__init__.py` sets the telemetry opt-out before its `import deepeval` (decision 0.7) — this is the kernel's **only** env write, allowlisted in the grep-gate.
6. Activate the kernel-purity import-linter contract **plus** the grep-gate: `os.environ`, `load_dotenv`, `boto3`, `phoenix` forbidden under `src/crucible/kernel/`, with the single-line 0.7 allowlist.

**Acceptance:** `tests/kernel/` (now including the new comparison/validator tests) passes in a venv with only core deps — no `bedrock`, `phoenix`, or `demo` extras; grep-gate and linter green.

### Phase 3 — Shape `service/` to the app layout
1. Create `service/{deepeval,phoenix,datasets,metrics,runners}` per §2/§3; mechanical `git mv` + relative-import fixes.
2. Extract runner logic out of `__main__` shells into plain functions: `run_golden_set(dataset, adapter, provider, config, claims: ClaimStore | None = None) -> RunResult` (0.5 protocol — claim before judge calls when provided, finalize after result write; `None` = no idempotency layer, crucible-standalone behaviour).
3. Make `service/datasets` cache directories config-driven (kill the `Path("data/…")` module constants as defaults reachable from library code; CLI may still pass them).
4. Write the one genuinely new module, `service/phoenix/annotations.py` (span + document annotation write-back, bulk API), integration-tested against the repo's docker-compose Phoenix **behind a `phoenix_integration` pytest marker** so suites that lack a Phoenix server (the rehearsal, plain CI) skip it explicitly rather than fail.
5. Carry forward the AU-residency checklist item: confirm the dated `au.*` model strings for ap-southeast-2 against current Bedrock inference-profile availability before this phase merges (invariant §7.4).

**Acceptance:** CLIs ≤ ~30-line wrappers; `tests/service/` green (Phoenix-marked tests run in the compose job, skip elsewhere); annotation write-back integration test green against local Phoenix.

### Phase 4 — Kill import-time side effects (with the corrected invariant)
Inventory (verified): env writes/`load_dotenv()` at import time in `__init__.py:34`, `metrics/deepeval_config.py:42-45`, and per-runner repeats (`run_rag_eval.py:26-36`, `run_replay_eval.py:19-24`, `generate_spans.py:20-24`).

1. The telemetry opt-out fires at the **two** 0.7 sites only — `kernel/rag_metrics/__init__.py` and `service/deepeval/__init__.py` — each before its own first `import deepeval`. The package root `crucible/__init__.py` writes nothing.
2. **Two subprocess regression tests**, one per import path: fresh interpreter, assert `"deepeval" not in sys.modules`, then (a) `import crucible.kernel.rag_metrics` and (b) `import crucible.service.deepeval`; assert the env var is set in both cases before deepeval landed in `sys.modules`. (v1 tested only the service path — the kernel path is the one its own Phase-2 acceptance exercises.)
3. `load_dotenv()` moves to the legitimate entrypoints only: `local/cli/*`, and `service/config.py` behind an explicit `from_dotenv=True`. Library import of any crucible module mutates nothing beyond the 0.7 sites.
4. Note for the record (not crucible's to fix): the env var governs DeepEval analytics; deepeval also vendors `sentry-sdk`/`posthog`. The monorepo deployment's egress policy is the hard telemetry perimeter (0.7 backstop).

**Acceptance:** `python -c "import crucible"` performs zero env writes (tested); both subprocess tests green.

### Phase 5 — Dependencies, packaging, contracts (uv_build)
Re-cut `pyproject.toml` (backend already `uv_build` since Phase 0.6):

```toml
[build-system]
requires = ["uv_build"]
build-backend = "uv_build"

[project]
requires-python = ">=3.13"          # per 0.1 — align to monorepo minor
dependencies = [                    # = what services/eval inherits, verbatim
  "pydantic>=2", "jsonschema>=4", "pyyaml",
  "deepeval==4.0.5",                # exact pin + re-baseline-event comment: unchanged
  "scipy",                          # kernel/replay_stats — promoted from the replay extra; no fallback
  "polars>=1.41.0", "beartype",
]
[project.optional-dependencies]
bedrock = ["boto3>=1.0.0", "aiobotocore>=2.0.0"]
phoenix = ["arize-phoenix>=4.0.0", "openinference-semantic-conventions>=0.1.0"]
replay  = ["fastapi>=0.100.0", "uvicorn>=0.23.0", "click>=8.0.0", "aiohttp"]
demo    = ["chromadb", "sentence-transformers>=5.5.0", "openai>=1.0.0",
           "huggingface-hub", "datasets", "rich", "python-dotenv"]
```

Deliberate moves and their honest limits:
- chromadb / sentence-transformers / openai / datasets / rich / dotenv leave **direct** core; scipy enters core. **Transitive reality:** `deepeval==4.0.5` hard-requires `openai`, `python-dotenv`, `rich`, `sentry-sdk`, `posthog`, and the pytest plugin family — these land in every install regardless. The dependency-contract test therefore asserts **direct** dependencies only; the runtime `test_no_openai_guard` (already a hard CI gate) remains the actual "no OpenAI path in prod" enforcement; the image bloat/supply-chain surface is acknowledged and owned by the deepeval pin decision, not hidden.
- Verify `uv_build` packages `contracts/*.json` as data files (explicit include config) and that an install **from the built wheel** — not just an editable/path install — passes the kernel tests, exercising the `importlib.resources` path for schemas. Then delete `src/crucible/contracts/` (the 3 vendored duplicates; byte-identical today, verified). Top-level `contracts/` (4 schemas, `schema_version` present in all 4 — verified) is the sole source and later moves untouched to `genai-backend/contracts/schemas/`.
- **Re-anchor `test_dependency_contract.py`** (nearest-`pyproject.toml` discovery) and extend it: deepeval pin exact + matches `uv.lock`; chromadb/openai absent from **direct** core deps; bedrock extra declares boto3. Same re-anchor for `test_eval_config_yaml_judge.py`.
- **Lockfile discipline:** add `uv.lock` to CODEOWNERS (or a CI annotation on change). Rationale in §7.8.

**Acceptance:** extended contract test green; clean-venv kernel run repeated; wheel-install kernel run green.

### Phase 6 — The migration rehearsal (the binding guarantee, hardened)
Two pieces: a **skeletal destination** and a **rehearsal script that proves more than imports**.

**6a. Build the skeleton now** (one-time, small): `genai-backend/services/eval/` — or, until monorepo write access exists, a `skeleton/services/eval/` directory inside crucible mirroring it — containing: `app/__init__.py` (a real package, not an implicit namespace — matching how every other `services/*` app is laid out), `pyproject.toml` in house style (crucible's deps verbatim + `bedrock`/`phoenix` extras promoted per the §5 runbook; `doc-bench` pin commented until wired), a generated per-service `uv.lock`, and `tests/`. This turns the rehearsal from "simulate a guessed destination" into "copy into the real one."

**6b. `scripts/rehearse_migration.sh`** — run in CI on every PR:

```bash
#!/usr/bin/env bash
set -euo pipefail
DEST="${1:-skeleton/services/eval}"          # migration day: the real services/eval checkout

# 1. Copy into the skeleton (app/__init__.py and pyproject/uv.lock already exist there)
rm -rf "$DEST/app/kernel"; cp -r src/crucible/kernel "$DEST/app/kernel"
for d in src/crucible/service/*/; do
  rm -rf "$DEST/app/$(basename "$d")"; cp -r "$d" "$DEST/app/$(basename "$d")"
done
cp src/crucible/service/config.py "$DEST/app/config.py"
cp -r tests/kernel tests/service "$DEST/tests/"
cp -r contracts "$DEST/contracts"            # fixtures the v1 script forgot
cp eval_config.yaml "$DEST/eval_config.yaml"

# 2. The mechanical rewrite — hardened
grep -rl "crucible\." "$DEST/app" "$DEST/tests" | xargs -r sed -i \
  -e 's/crucible\.kernel/app.kernel/g' \
  -e 's/crucible\.service\./app./g' \
  -e 's/from crucible\.service import/from app import/g' \
  -e 's/from crucible import/from app import/g'

# 3. THE ASSERTION THAT MAKES THE SED TRUSTWORTHY:
#    no reference to the old package may survive — catches every pattern the
#    rules above miss, including string-literal module paths in tests.
if grep -rn "crucible" "$DEST/app" "$DEST/tests"; then
  echo "FATAL: surviving crucible.* references after rewrite"; exit 1
fi

# 4. Resolve against the destination's OWN lockfile (validates deps + uv_build packaging)
( cd "$DEST" && uv sync --frozen --extra bedrock --extra phoenix )

# 5. Import-smoke EVERY module — broken imports in untested modules
#    (phoenix clients, replay tasks) cannot pass silently.
( cd "$DEST" && uv run python - <<'EOF'
import importlib, pkgutil, app
failures = []
for m in pkgutil.walk_packages(app.__path__, "app."):
    try: importlib.import_module(m.name)
    except Exception as e: failures.append((m.name, repr(e)))
assert not failures, f"import-smoke failures: {failures}"
EOF
)

# 6. Run the mirrored tests in the destination venv (Phoenix-integration tests skip via marker)
( cd "$DEST" && uv run pytest tests -q -m "not phoenix_integration" )
```

**Acceptance:** job green and **required**. From this point, any PR that complicates the migration — a missed import pattern, a dep that doesn't resolve under the destination lockfile, a packaging regression under `uv_build`, a module that breaks under the rename — fails CI the day it's written. The honest statement of the guarantee: green proves resolution, packaging, import-health of **every** module, and behaviour of every **tested** path — which is why Phase 2's new tests for the previously-uncovered kernel modules are a prerequisite, not a nice-to-have.

---

## 5. Migration-day runbook

1. Run `scripts/rehearse_migration.sh services/eval` against the real monorepo checkout (same script, real destination — the skeleton *is* the destination, so this step is the rehearsal minus `/skeleton`).
2. `services/eval/pyproject.toml` already carries crucible's deps (skeleton, 6a); add the `doc-bench==Y` pin; `uv lock` and commit the per-service lockfile.
3. `tests/{kernel,service}` already copied by the script; re-anchored `test_dependency_contract.py` now guards the pin against the *service's* pyproject + lock with zero edits.
4. Move `contracts/*.schema.json` → `genai-backend/contracts/schemas/` (no edits; confirm the new sibling dir next to `contracts/openapi/` with the platform owner **before** this day — it's a one-line ask).
5. Port the import-linter contracts and the rehearsal-derived checks into the monorepo's **GitLab** CI (`.gitlab-ci.yml`) — rule text identical, root package renamed; this is a CI-system port, budget an hour, not a copy-paste.
6. Archive the crucible repo with a tombstone README pointing at `services/eval/`.

Net-new work that begins only after the copy (unchanged): `api/`, `state/` (the `ClaimStore` implementation over DynamoDB + run registry), `spans/` (S3 span-store reader + sampler), `deepeval/token_bucket.py` (implements the kernel `RateLimiter` protocol), `runners/online_monitor.py`, `parse_run.py`.

---

## 6. Deliberately NOT built in crucible (and why)

| Not built | Why it stays out |
|---|---|
| Token bucket, `ClaimStore` implementation, REST API, S3 span reader | Infra-bound (Redis/RDS/DynamoDB/ALB/IRSA); building against local fakes would be thrown away. Crucible defines the **protocols** (`RateLimiter`, `ClaimStore` — now two-phase per 0.5) so they slot in without touching migrated code. |
| `parse_run` / comparator integration | doc-bench's job (external pinned wheel); wiring is monorepo work. |
| Renaming the package to `app` inside crucible | Breaks standalone installability during the transition window; the rehearsed, *asserted* rewrite is cheaper than a broken interim repo. |
| MLflow logging | **Confirmed not a requirement** — the monorepo tree's `mlflow/` entry is a placeholder; the team's stack is DeepEval + Phoenix. Recorded so nobody re-derives a phantom requirement from the tree doc. |
| `libs/*` integration | Out of scope by decision — service-level only; revisit in the monorepo. |

---

## 7. Invariants that must survive every phase (each backed by a test or gate)

1. **`deepeval==4.0.5` exact pin** — re-anchored `test_dependency_contract.py`: pin exact, matches `uv.lock`, **direct** core deps exclude chromadb/openai.
2. **Judge ≠ generator** — pure kernel function in `metric_specs.py` + unit test.
3. **Telemetry opt-out before deepeval import on BOTH import paths** — two subprocess tests (kernel path and service path; Phase 4.2). Egress policy is the production perimeter (0.7 backstop).
4. **AU residency defaults (`au.*`, never `apac.*`), single-sourced** — asserted on `metric_specs.py`; the adapter's stale openai defaults are deleted, not relocated (Phase 2.4); dated `au.*` strings re-confirmed for ap-southeast-2 in Phase 3.
5. **Kernel purity** — import-linter + grep-gate (with the single 0.7 allowlist line) + clean-venv kernel run + wheel-install kernel run.
6. **No silent statistics degradation** — scipy is a mandatory top-level import in `comparison.py`; the sign-test fallback is deleted; test asserts the Wilcoxon path (a missing scipy is an ImportError, never a different test statistic).
7. **No CWD-relative resource paths in kernel/service** — grep-gate (§2 rule 5); schema resolution via injectable path / `importlib.resources`, covered by the wheel-install test.
8. **Lockfile changes are deliberate events** — `uv.lock` under CODEOWNERS/CI-flag: with per-service locks, `uv lock --upgrade` can move deepeval's transitives (openai, tenacity, …) and shift judge behaviour while the `==4.0.5` pin sits untouched. A re-lock is reviewed like a pin bump, scaled down.
9. **Rehearsal green** — including the post-sed zero-`crucible.*` assertion, `uv sync --frozen` against the destination lock, and the all-modules import smoke. Required job.

---

## 8. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Big-bang restructure breaks history/review | Six small PRs in dependency order; `git mv` where it's a move. **Honesty note:** the `deepeval_config.py` and `deepeval_adapter.py` splits are content surgery — blame dies there and that's accepted; the lint-config and `uv_build` commits are isolated first so structural diffs stay readable. |
| `uv_build` packages differently than hatchling did (data files, wheel contents) | Swapped in Phase 0 so every subsequent phase and the rehearsal's `uv sync` validate the real backend; Phase 5 adds an install-from-wheel kernel test. |
| A transitive bump shifts judge behaviour despite the pin | Invariant §7.8 (lockfile review discipline) + `test_no_openai_guard` re-run on any lock change. |
| `metric_specs` / `bedrock_provider` split leaves a hidden env read in the kernel | Grep-gate makes it a CI failure, not a review hope; the only sanctioned write is the allowlisted 0.7 line. |
| Phoenix annotation API shape differs in the monorepo's Phoenix version | Pin the `arize-phoenix` minimum in the extra to the platform's version; the marker-gated compose test uses the same pin. |
| Rehearsal red/flaky because service tests need live deps | `phoenix_integration` marker (skip in rehearsal, run in the compose job); bedrock tests already hermetic (subclassed `AmazonBedrockModel`, no network — existing pattern in `test_no_openai_guard`). |
| Someone adds a convenient stub import "temporarily" | `nothing-imports-local` contract fails the PR. |
| `RagAdapter()` breakage straddles PRs | All bare constructions updated inside the Phase-1 PR; the `TypeError` message names the fix. |
| `contracts/schemas/` dir doesn't exist in the monorepo | One-line confirmation with the platform owner scheduled before migration day (runbook §5.4), not discovered on it. |

*(Removed from v1: the deepeval-pin cross-service collision risk — dead by the per-service `uv.lock` decision.)*

---

## 9. Definition of done

- [ ] All six phases merged; required CI jobs: lint, import-linter, kernel clean-venv tests, kernel wheel-install tests, service tests (+ Phoenix compose job), **rehearse-migration** (post-sed assertion + `uv sync --frozen` + import-smoke + suite).
- [ ] `pip install crucible` → zero env mutations on import of the root package; the two 0.7 sites tested by subprocess; no demo deps in direct core; kernel importable standalone and from the built wheel.
- [ ] Four stub-leak sites eliminated; `RagAdapter` stub default gone and every call site updated.
- [ ] One schema source (top-level `contracts/`, 4 files, `schema_version` in every file); vendored `src/` copy deleted; packaged under `uv_build` and resolved via injectable path / `importlib.resources` — no CWD-relative resource paths anywhere in kernel/service.
- [ ] `comparison.py` and `schema_validator.py` have unit tests; the scipy sign-test fallback is deleted.
- [ ] `ClaimStore` two-phase protocol in `kernel/interfaces.py`; runners accept it.
- [ ] `uv.lock` under review discipline (CODEOWNERS/CI flag).
- [ ] §7 invariant tests all green.
- [ ] README updated: layout rules, migration runbook (§5), Phase-0 decision log (0.0–0.7), and the placeholder note for the monorepo tree doc.