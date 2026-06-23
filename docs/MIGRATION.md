# Migrating Crucible → `genai-backend/services/eval/`

A careful, tested runbook for moving crucible's evaluation engine into the
monorepo once `genai-backend` exists. Everything here has been exercised against
a real local Phoenix and a staged destination (`services/eval/`); the
gotchas in [§7](#7-gotchas-tested-the-things-that-actually-bite) are the ones that
actually bit, not hypotheticals.

## Table of Contents

- [0. State of play](#0-state-of-play)
- [1. Prerequisites](#1-prerequisites)
- [2. The migration in six steps](#2-the-migration-in-six-steps)
- [3. What migrates, what stays, what is net-new](#3-what-migrates-what-stays-what-is-net-new)
- [4. The four import-rewrite rules](#4-the-four-import-rewrite-rules)
- [5. Decisions to lock on migration day](#5-decisions-to-lock-on-migration-day)
- [6. Validating the migrated service](#6-validating-the-migrated-service)
- [7. Gotchas (tested)](#7-gotchas-tested-the-things-that-actually-bite)
- [8. Net-new monorepo work](#8-net-new-monorepo-work)
- [9. Tombstone crucible](#9-tombstone-crucible)
- [Appendix A. Driving the migrated service](#appendix-a-driving-the-migrated-service)

## 0. State of play

The refactor is done and proven. Crucible is split into three strictly-layered
packages so the migration is a **directory copy plus a single mechanical import
rewrite**:

- `crucible.kernel.*` → `app/kernel/*` (pure: no infra, no env, no network)
- `crucible.service.*` → `app/<child>/*` (the service children **flatten** into `app/`)
- `crucible.contracts` → `app/contracts` (the JSON-schema package; importlib.resources anchor)
- `crucible.local.*` — **never migrates** (demo stubs + CLI shells)

Two artifacts already exist in this repo and prove the migration end-to-end:

- **`scripts/rehearse_migration.sh`** — copies the three buckets into a fresh `app/`,
  applies the rewrite, asserts zero surviving `crucible.` references, resolves the
  destination lockfile, import-smokes every module, and runs the mirrored suite.
- **`services/eval/`** — the committed migration *scaffold* (`pyproject.toml`, `uv.lock`,
  `app/__init__.py`) plus worked end-to-end driver scripts under `services/eval/scripts/`.
  The rehearsal regenerates `app/` + the mirrored tests into it on demand; those generated
  trees are **gitignored** (reproduced from the current `src/`, never hand-maintained), so
  there is exactly one source of truth — `src/crucible/`.

The canonical migration method is to **re-run the rehearsal against the real
monorepo path** (always fresh from crucible's current `src/`). Run it locally with no
argument to regenerate `services/eval/app` for a preview/smoke.

## 1. Prerequisites

- `genai-backend` exists and is checked out; you know its `services/*` conventions
  (workspace membership, lockfile model, Python version, CI).
- Python ≥ 3.12 and `uv` available.
- On the crucible side, the source branch is merged/clean and **the rehearsal is
  green**: `bash scripts/rehearse_migration.sh` exits 0.
- Confirm the monorepo's Python minor matches crucible's `>=3.12` floor (a mismatch
  is the one thing that can hard-block the copy — see [§7](#7-gotchas-tested-the-things-that-actually-bite)).

## 2. The migration in six steps

### Step 1 — Land the refactor in crucible

Migrate *from* a reviewed, green source.

```bash
# in the crucible checkout
bash scripts/rehearse_migration.sh        # must be green
git push -u origin <refactor-branch>       # PR → review → merge to crucible main
```

### Step 2 — Seed the destination in the monorepo

Create the service directory and seed the *scaffold* (not the generated code) from
crucible's committed `services/eval/` scaffold:

```bash
# in the genai-backend checkout
mkdir -p services/eval
cp <crucible>/services/eval/pyproject.toml  services/eval/
cp <crucible>/services/eval/uv.lock         services/eval/
mkdir -p services/eval/app && cp <crucible>/services/eval/app/__init__.py services/eval/app/
```

Then **adapt `services/eval/pyproject.toml` to monorepo house style** — preserving
the locked bits verbatim:

- `deepeval==4.0.5` (the exact pin; bumping it is a re-baseline event, never routine)
- the seven core deps, the `bedrock` and `phoenix` extras
- `[tool.uv.build-backend]` `module-name = "app"`, `module-root = ""`,
  `source-include = ["app/contracts/*.json"]`
- the `phoenix_integration` pytest marker

Add whatever the monorepo's services carry (e.g. the `doc-bench` pinned wheel, shared
internal libs), make the service a workspace member if that's the convention, then
`uv lock` so the lockfile matches the monorepo resolver.

### Step 3 — Run the migration

From the **crucible** checkout, point the rehearsal at the real destination:

```bash
# in the crucible checkout
bash scripts/rehearse_migration.sh /abs/path/to/genai-backend/services/eval
```

This copies `kernel/`, the service children, `service/config.py`, and `contracts/`
into `services/eval/app/`, copies `tests/kernel` + `tests/service` + `tests/conftest.py`,
applies the four rewrite rules, **asserts zero surviving `crucible.` references**,
`uv sync --frozen`s, import-smokes every `app.*` module, and runs the suite. Green =
the copy is correct by construction.

### Step 4 — Commit the generated code as monorepo source

In crucible's `services/eval/` scaffold the generated `app/` + tests are gitignored
(throwaway, reproduced from `src/`). In the **real destination they are source** — drop
that ignore and commit them:

```bash
# in genai-backend/services/eval
# ensure .gitignore ignores only .venv/ caches, NOT app/ or tests/
git add app tests pyproject.toml uv.lock
```

### Step 5 — Carry the non-code dependencies

- **`eval_config.yaml`** — copy the repo-root runtime config the service reads into the
  destination. Its config-shape test (`test_eval_config_yaml_judge.py`) self-skips when
  the file is absent and exercises it once present.
- **Contracts** — see the decision in [§5](#5-decisions-to-lock-on-migration-day).

### Step 6 — Wire CI and validate

Port the gates into the monorepo CI with `root_package = "app"` (see
[§6](#6-validating-the-migrated-service)), and run an end-to-end eval against the
migrated service (see [Appendix A](#appendix-a-driving-the-migrated-service)).

## 3. What migrates, what stays, what is net-new

| Migrates (the rehearsal handles) | Stays in crucible | Net-new in the monorepo |
|---|---|---|
| `kernel/` → `app/kernel/` | `local/` (stubs + CLI shells) | `api/` (REST) |
| `service/*` → `app/*` (flattened) | `tests/local`, the `demo`/`replay` extras | `state/` — `ClaimStore` impl (DynamoDB) |
| `service/config.py` → `app/config.py` | the `eval-rag`/`eval-replay`/`generate-spans` CLIs | `spans/` — S3 span reader/sampler |
| `contracts/` → `app/contracts/` | `test_dependency_contract.py` (crucible-root anchored) | `deepeval/token_bucket.py` — `RateLimiter` impl |
| `tests/kernel` + `tests/service` + `tests/conftest.py` | the ChromaDB demo RAG | `runners/online_monitor.py`, `parse_run.py` |

The kernel **protocols** — `ClaimStore`, `RateLimiter`, `JudgeProvider` in
`app/kernel/interfaces.py` — are the seams the net-new infra plugs into, with **zero
changes to migrated code**. The monorepo drives `app.runners.golden_set.run_golden_set`
/ `run_phoenix_native` / `app.runners.replay.run_replay` from its own `api/`+runner
layer instead of crucible's `local/cli` shells.

**The eval service does not own a RAG.** It evaluates a RAG it is *given* — any
`(question, corpus_dir) -> rag_query_output` callable injected into
`app.kernel.interfaces.RagAdapter`. The ChromaDB stub stays in crucible; the monorepo
injects the real RAG service (or reads its spans from the S3 store).

## 4. The four import-rewrite rules

The rehearsal applies these in order (dotted rules first, so `crucible.kernel` is never
mangled by the `crucible.service.` flatten):

```
crucible.kernel                  → app.kernel
crucible.service.                → app.            # children flatten: crucible.service.deepeval → app.deepeval
crucible.contracts               → app.contracts
from crucible[.service] import   → from app import
import crucible                  → import app      # top-level package analog
```

After the rewrite, **zero dotted `crucible.` references** and **no `import crucible`
statement** may survive. The proper noun "Crucible" and slash-paths (`crucible/...`) in
prose are fine — the assertion is *dotted*. The only non-rewritable references
(`crucible.local` in docstrings/messages) were genericized out of migrating code in the
refactor, so nothing should leak; if something does, the rehearsal fails loud and you
have found a real cross-bucket dependency to fix — **do not weaken the assertion.**

## 5. Decisions to lock on migration day

1. **Contracts location.** The migrated service ships schemas at `app/contracts/`, which
   is the `importlib.resources` anchor (`_CONTRACTS_PACKAGE = "app.contracts"`) — this is
   proven to work run-from-anywhere. The original plan envisioned repo-level
   `genai-backend/contracts/schemas/`. **Recommendation:** keep `app/contracts/` initially
   (lowest risk, already green). If you later want repo-level sharing, point the validator
   at it via the **injectable `schema_path` / `schema_dir`** the validator already supports
   — no kernel change needed; the `importlib.resources` default is just the self-contained
   convenience.
2. **pyproject / workspace shape.** Keep the locked bits ([Step 2](#step-2--seed-the-destination-in-the-monorepo)); everything else bends to monorepo convention.
3. **CI.** Port the gates with `root_package = "app"` (see [§6](#6-validating-the-migrated-service)). Crucible has no CI; this is where the contracts become *enforced* automatically.

## 6. Validating the migrated service

Run these in `genai-backend/services/eval` after the copy:

```bash
uv sync --frozen --extra bedrock --extra phoenix
# import-linter with root_package="app" (port the 3 contracts, renamed kernel→app):
#   app-kernel-pure / app-service-... / nothing-imports-... (adapt source/forbidden modules)
uv run lint-imports
# kernel runs with zero extras (port scripts/check_kernel_clean.sh, app paths):
#   fresh venv, core deps only, pytest app tests/kernel
uv run pytest -m "not phoenix_integration"   # the mirrored suite
```

Then an **end-to-end eval** against a real RAG, traced to Phoenix — see
[Appendix A](#appendix-a-driving-the-migrated-service). Confirm:

- traces appear under **Projects** (default `run_golden_set` mode), and/or
- a scored experiment appears under **Datasets & Experiments** (`run_phoenix_native` mode).

## 7. Gotchas (tested — the things that actually bite)

1. **Python minor mismatch is the only hard-block.** Confirm the monorepo runs ≥3.12
   *before* copying. The code was lowered to a `>=3.12` floor specifically for this.
2. **The migrated service has no RAG and no `demo` deps.** Do not expect
   `chromadb`/`sentence-transformers` in `services/eval` — retrieval is the *injected*
   RAG's job. For a local smoke you install them ad-hoc into the service venv and inject
   a stub (Appendix A); production injects the real RAG service.
3. **Projects vs. Datasets & Experiments.** `run_golden_set` emits OTLP **span traces →
   Projects** tab. Only `run_phoenix_native` uploads a **dataset + experiment → Datasets &
   Experiments** tab. If "I don't see scores under Experiments", you ran the wrong mode.
4. **Phoenix project-name / global-tracer collision (demo-only).** Crucible's demo stub
   registers the **global** OpenTelemetry tracer with a hardcoded project name
   (`case-assistant-synthetic`), so when you inject it, *all* spans land under that
   project regardless of the `PhoenixAdapter` project you set. This is a demo-stub
   artifact: in the monorepo the RAG is a separate service that traces on its own, and the
   eval service writes scores back as **annotations** (`app.phoenix.annotations`, the API
   spec §8.2 path) rather than competing for the tracer — so the collision disappears.
5. **ChromaDB collection name = `corpus_dir.stem`** (in the demo stub). If you inject the
   stub for a smoke, pass `corpus_dir` whose stem matches the ingested collection
   (`data/rag/gst_legal_rag`, stem `gst_legal_rag`), or it silently re-ingests.
6. **`--force-reingest` re-embeds the whole corpus on *every query*** (demo-stub bug).
   Build the collection once, then never pass it. Irrelevant post-migration (no stub), but
   it will waste your time in a local smoke.
7. **`deepeval==4.0.5` is a hard pin.** Carry it verbatim. A bump shifts judge prompts and
   invalidates historical comparisons — treat it as a deliberate re-baseline event with its
   own runbook, never a routine `uv lock --upgrade`.
8. **Judge ≠ generator, and provider/model must agree.** The config refuses same-model
   self-grading and a provider/model mismatch (fail-loud). Local: OpenAI (`gpt-4o` judge /
   `gpt-4o-mini` generator). Prod: Bedrock `au.*` (Sydney residency). Set
   `OPENAI_API_KEY` / AWS creds in the environment, never in committed config.
9. **Telemetry opt-out ordering survives the copy** — it lives at two allowlisted sites
   (`app/kernel/rag_metrics/__init__.py`, `app/deepeval/__init__.py`) that fire before any
   `deepeval` import. Don't relocate it.

## 8. Net-new monorepo work

Built *after* the copy, on the kernel protocols (no migrated-code changes):

- `api/` — the REST surface (ALB/IRSA-bound).
- `state/` — `ClaimStore` over DynamoDB (the two-phase `claim`/`finalize` protocol).
- `spans/` — S3 span-store reader + sampler for the online/nightly flow.
- `deepeval/token_bucket.py` — `RateLimiter` implementation (Redis or in-process).
- `runners/online_monitor.py`, `parse_run.py` — wire doc-bench's comparator (external pinned wheel).

## 9. Tombstone crucible

Once `genai-backend/services/eval` is green in CI, archive crucible with a README
pointing at the monorepo service, and freeze the repo (read-only).

## Appendix A. Driving the migrated service

The eval service is a *library*, driven by injecting a RAG. The three scripts in
`services/eval/scripts/` are worked examples your monorepo `api/`+runner layer can model:

- **`smoke_phoenix.py`** — minimal: a fake (gold-context) RAG → `run_golden_set` → Phoenix
  **traces**. Proves the pipeline runs with no external RAG.
- **`eval_rag_real.py`** — a *real* RAG (injected ChromaDB stub, real retrieval + OpenAI
  generation) → `run_golden_set` → Phoenix **traces**.
- **`eval_rag_phoenix_native.py`** — the same real RAG → `run_phoenix_native` → a scored
  **dataset + experiment** under **Datasets & Experiments**.

The injection pattern (this is what the monorepo replaces the stub with — the real RAG
service or an S3 span reader):

```python
from app.kernel.interfaces import RagAdapter
from app.deepeval.bedrock_provider import create_deepeval_metrics, get_deepeval_config
from app.kernel.rag_metrics.evaluator import DeepEvalEvaluator
from app.phoenix.adapter import PhoenixAdapter, suppress_tracing_if_available
from app.runners.golden_set import run_golden_set

def my_rag(question, corpus_dir, embedder=None) -> dict:
    ...  # return a rag_query_output-schema dict (validated by RagAdapter)

adapter = RagAdapter(query_callable=my_rag)
dc = get_deepeval_config({})  # provider/model from env (CRUCIBLE_JUDGE_*)
evaluator = DeepEvalEvaluator(
    metrics=create_deepeval_metrics(
        llm_provider=dc["judge_model_provider"], judge_model=dc["judge_model"],
        temperature=dc["temperature"],
    ),
    suppress_tracing=suppress_tracing_if_available,
)
result = run_golden_set(
    dataset=dataset, adapter=adapter, evaluator=evaluator, config={},
    phoenix=PhoenixAdapter(endpoint="http://localhost:6006", project_name="eval", enabled=True),
)
```

Run the smoke against a local Phoenix from the crucible checkout:

```bash
docker-compose -f docker-compose.yml -f docker-compose.observability.yml up -d
set -a; . .env; set +a   # OPENAI_API_KEY + CRUCIBLE_JUDGE_*/GENERATOR_* + PHOENIX_ENDPOINT
GST_CORPUS_DIR=data/rag/gst_legal_rag SLICE=gst_pico \
  PYTHONPATH=$PWD/src services/eval/.venv/bin/python \
  services/eval/scripts/eval_rag_phoenix_native.py
```
