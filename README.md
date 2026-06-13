# Crucible: RAG Evaluation and Replay Testing Framework

Crucible is a standalone evaluation framework for RAG (Retrieval-Augmented Generation) systems and replay testing with Phoenix observability integration. It provides deterministic metrics for RAG quality and enables validation of model deployments through production traffic replay.

## Phase 0 Decision Log (Refactor → `genai-backend/services/eval/app/`)

Crucible is being prepared for a mechanical migration into the monorepo at
`genai-backend/services/eval/app/`. Phase 0 lands prerequisite build-tooling and
documentation commits with **zero structural or behavioral code changes** so that
later structural diffs stay readable. The decisions below are locked here (per the
refactor plan §1); items marked **DEFERRED** are recorded as outstanding, not
falsely closed.

| # | Decision | Status |
|---|----------|--------|
| 0.1 | **Python minor.** `requires-python` **lowered `>=3.13` → `>=3.12`** for migration safety: targeting the lower bound maximizes compatibility with the monorepo `services/*` toolchain (hedges against it running 3.12, which a `>=3.13` floor would hard-block). Dockerfile `python:3.12-slim` (3 stages), pre-commit `python3.12`, ruff `target-version = "py312"`. No upper bound added (no `<3.14`). A 3.12/3.13-only-syntax grep (PEP 695 type aliases/generics, `@override`, `itertools.batched`, version gates) found none, and the full suite was run on **CPython 3.12.11** (120 passed; the 1 ChromaDB ordering flake passes in isolation) — code verified to run on 3.12, not merely asserted. | **Lowered to >=3.12 (verified on 3.12)** |
| 0.2 | **`deepeval==4.0.5` exact pin = re-baseline event rule.** The exact pin carries into the monorepo verbatim. A bump can shift the LLM-judge's metric prompts and reintroduce judge drift, so any change is a deliberate, documented **re-baseline event**, never a routine dependabot/`uv lock --upgrade` merge. The `==` must not be loosened to `>=`/`~=`. Per-service `uv.lock` plus the dependency-contract test enforce it. | **Locked** |
| 0.3 | **Lint/format parity.** The current `[tool.ruff]` config is **frozen verbatim as the candidate parity target** (`target-version = "py313"`, `line-length = 100`, `select = [E,F,I,B,UP,D]`, `ignore = [D203,D212]`, `tests/**/*.py = [D]`). ruff is pinned to one identical exact version (`==0.15.15`) in both `pyproject.toml` dev deps and `.pre-commit-config.yaml` (`rev: v0.15.15`) for deterministic local/CI formatting. **black dropped** — `ruff format` is the sole formatter (`ruff format` reimplements black; running both is redundant and can disagree); black was never invoked by any tooling, only stale docs. **True monorepo parity is DEFERRED** (the monorepo ruff/Python config is not available in this tree): the migration-day reformat risk is recorded as **OUTSTANDING**, not closed. | **Frozen (parity DEFERRED)** |
| 0.4 | **`beartype` stays.** Small dependency, real runtime guards; stripping later is invasive. Carries into the monorepo. | **Locked (stays)** |
| 0.5 | **Claims/idempotency seam.** Runners accept a **two-phase `ClaimStore` protocol** (`claim(repro_key) -> Claimed \| CachedResult` then `finalize(repro_key, result_uri)`), not a `(key) -> CachedScore \| None` getter. A getter cannot express "claim atomically before the Bedrock call, finalize after the durable write" (the DynamoDB conditional-claim → write → finalize model). The protocol is locked now; the implementation lands in the monorepo (no-op default in crucible). | **Protocol locked; impl DEFERRED** |
| 0.0 | **Build backend.** Swapped `hatchling → uv_build` (`requires = ["uv_build>=0.9,<0.10"]`) in its own isolated commit. uv_build packages the in-package `src/crucible/contracts/*.json` into the wheel; an explicit `[tool.uv.build-backend].source-include` declaration makes the 3 schema JSONs a non-regressing guarantee for the wheel and sdist. Verified by actually building (`scripts/check_wheel_contents.sh`). uv.lock re-lock is build-system-only — no deepeval-transitive drift (`openai`/`tenacity`/`posthog`/`sentry-sdk` unchanged). Packages the in-package `src/crucible/contracts/` ONLY; the top-level `contracts/` is Phase 5. | **Swapped** |

### Known Issue — schema resolution was CWD-relative (Finding A, CLOSED in Phase 2/5)

Shipping the schema JSONs in the wheel is **necessary but NOT sufficient** for a
runnable-on-install package. Two call sites still resolve the RAG output schema via
**CWD-relative** `Path(...)` rather than `importlib.resources`:

- `src/crucible/adapters/rag_adapter.py:90` — `Path("src/crucible/contracts/rag_query_output.schema.json")`
- `src/crucible/stubs/rag/schema_conformance.py:37` — `Path("contracts/rag_query_output.schema.json")`

Therefore `pip install crucible` + run-from-anywhere is **already broken regardless
of the build backend**. Phase 0 does **not** claim a runnable-on-install wheel. These
paths are **behavioral** and are **NOT touched in Phase 0**; the fix (injectable path
defaulting to `importlib.resources`) is deferred to **Phase 2/5**.

> **Update (Phase 2/5):** RESOLVED. Phase 2 added the injectable validator defaulting to `importlib.resources.files("crucible.contracts")`; Phase 5 single-sourced the four schemas in-package and switched the last CWD-relative caller (`local/stubs/rag/schema_conformance.py`) to logical-name resolution. Run-from-anywhere now works for both the source tree and an installed wheel. See "Phase 5 Migration Note" below.


## Phase 3 Verification Note — `au.*` inference profiles (verification-only)

Phase 3 (shape `service/` to the app layout) is a structured relocation: it MOVES
files into `crucible.service.{deepeval,phoenix,datasets,metrics,runners}` +
`service/config.py`, extracts the runner logic into injectable library functions
(`service/runners/golden_set.run_golden_set -> RunResult`,
`run_phoenix_native`, `service/runners/replay.run_replay`), adds the new
`service/phoenix/annotations.py` (API spec §8.2 score write-back), and activates
the `service-not-local` import-linter contract for real (proven non-vacuous via
inject-and-revert). No behavioral change to dotenv/telemetry side-effects this
phase (Phase 4 owns that).

As part of Phase 3 the dated `au.*` (AU-geographic, `ap-southeast-2`) Bedrock
inference-profile strings in
`src/crucible/kernel/rag_metrics/metric_specs.py`
(`DEFAULT_JUDGE_MODEL = "au.anthropic.claude-opus-4-6"`,
`DEFAULT_GENERATOR_MODEL = "au.anthropic.claude-sonnet-4-6"`) were **reviewed and
re-confirmed with NO behavioral change**. The kernel `metric_specs` module remains
the single source of truth for these constants and the judge≠generator invariant.
Any actual constant change is a **separate follow-up** that touches
`kernel/rag_metrics/metric_specs.py` (not service code) — this phase is
verification-only.


## Phase 5 Migration Note — contracts single-sourced in `src/crucible/contracts/`

Phase 5 closes Finding A and re-cuts the dependency shape. The schema contracts
are now **single-sourced inside the package** at `src/crucible/contracts/` (all
four canonical schemas: `parser_output`, `rag_query_output`, `eval_questions`,
`legal_rag_bench_query_output`; each carries `schema_version`). The top-level
`contracts/` duplicate was **deleted**.

**Deviation from plan §5 (intentional, Finding A).** Plan §5 says "delete
`src/crucible/contracts/`; keep top-level `contracts/` as the source." Phase 5 does
the **opposite**, deliberately: Phase 2 (which post-dates the plan) shipped the
kernel `schema_validator` resolving schemas via
`importlib.resources.files("crucible.contracts")` — an **in-package** anchor that is
the only mechanism working run-from-anywhere in BOTH the source tree
(`PYTHONPATH=src`) and an installed wheel. A non-package top-level `contracts/` is
not reachable as `crucible.contracts`, so reverting would re-break the
run-from-anywhere guarantee. Both prior CWD-relative call sites (the Phase-1
`rag_adapter`, now `crucible.kernel`, and `local/stubs/rag/schema_conformance.py`)
resolve by logical name instead — Finding A is closed.

**Migration-day runbook.** The step "move `contracts/*` →
`genai-backend/contracts/schemas/`" now sources from `src/crucible/contracts/`. The
monorepo wires its repo-level contracts via the **injectable** `schema_path` /
`schema_dir` the Phase-2 validator already supports; the `importlib.resources`
default is the crucible-local convenience for a self-contained wheel.

**Dependency reshape.** Core `[project.dependencies]` is now the kernel-grade seven
(`pydantic`, `jsonschema`, `pyyaml`, `deepeval==4.0.5`, `scipy`, `polars`,
`beartype`), so `pip install crucible` (no extras) is import-clean. Demo-only deps
(`chromadb`, `sentence-transformers`, `openai`, `datasets`, `huggingface-hub`,
`rich`, `python-dotenv`) live in the new `demo` extra; `observability` was renamed
to `phoenix`; `replay` was slimmed; `bedrock` is unchanged. The `deepeval==4.0.5`
pin and its judge-critical transitives (`openai`/`tenacity`/`posthog`/`sentry-sdk`)
are unchanged in `uv.lock`.


## Phase 6 — The Migration Rehearsal (binding guarantee)

Phase 6 proves — continuously and mechanically — that migrating crucible into the
monorepo is a **directory copy plus a single mechanical import rewrite**. A committed
skeleton destination (`skeleton/services/eval/`) plus an on-demand rehearsal script
(`scripts/rehearse_migration.sh`) copy `kernel/`, the `service/` children, and
`contracts/` into a fresh `app/` package, rewrite imports, assert zero surviving
`crucible.` references, resolve against the skeleton's own lockfile, import-smoke every
module, and run the mirrored tests. **Green = the migration is proven by construction.**

### Layout rules

The migration is exactly these five import/layout rules — nothing else moves:

1. **`crucible.kernel` → `app.kernel`.** `kernel/` copies whole to `app/kernel/`.
2. **`crucible.service.` → `app.`** (trailing dot). The service children
   (`deepeval`, `phoenix`, `datasets`, `metrics`, `runners`) **flatten into `app/`**:
   `crucible.service.deepeval` → `app.deepeval`, `crucible.service.config` →
   `app.config`. There is **no `app/service/` package** — `service/__init__.py` is the
   one thing deliberately **not** copied; `service/config.py` becomes `app/config.py`.
3. **`crucible.contracts` → `app.contracts`.** `contracts/` copies to `app/contracts/`;
   the skeleton's `[tool.uv.build-backend].source-include` ships `app/contracts/*.json`,
   so the kernel validator (`_CONTRACTS_PACKAGE = "app.contracts"`) resolves the four
   schemas via `importlib.resources` in the skeleton venv.
4. **`from crucible.service import` / `from crucible import` → `from app import`**
   (defensive bare top-level forms). The sibling statement form `import crucible` →
   `import app` (the top-level package's monorepo analog).

   > The rule ORDER matters: the dotted rules run **before** the flatten rule so
   > `crucible.kernel` is never mangled by `crucible.service. → app.`.

5. **No surviving `crucible.` reference.** After the rewrite, **zero** dotted
   `crucible.` and **no** `import crucible` / `from crucible ` statement may remain. The
   genericize pass (Phase 6 Commit 1) removed the only non-rewritable references
   (`crucible.local` docstrings/comments/messages); the proper noun "Crucible" and
   slash-paths (`crucible/...`) in prose are allowed because the assertion is **dotted**.

### Migration-day runbook

The actual migration is "run the same script against the real checkout", not an
improvised guess:

1. Land all changes on `main` with the rehearsal green (`bash scripts/rehearse_migration.sh`).
2. In the monorepo, create the destination service dir (e.g. `genai-backend/services/eval/`)
   seeded from `skeleton/services/eval/` (its `pyproject.toml`, `uv.lock`, `app/__init__.py`).
3. Run `scripts/rehearse_migration.sh <path-to-destination>` against that real
   destination. It copies the three buckets + `config.py` + the mirrored tests, applies
   the four rewrite rules, asserts zero `crucible.`, `uv sync --frozen`s, import-smokes
   every `app.*` module, and runs the suite.
4. Wire the monorepo's repo-level contracts via the **injectable** `schema_path` /
   `schema_dir` the Phase-2 validator already supports (the `importlib.resources`
   default is the crucible-local convenience for a self-contained wheel).
5. Carry along the repo-root runtime config (`eval_config.yaml`) the service reads; the
   config-shape test (`test_eval_config_yaml_judge.py`) skips itself when that file is
   absent and exercises it once present.

### How to run the rehearsal

```bash
bash scripts/rehearse_migration.sh            # defaults to skeleton/services/eval
bash scripts/rehearse_migration.sh <DEST>     # rehearse against a custom destination
```

It is **idempotent** (`rm -rf`s the generated subdirs first) and **self-contained**
(sets `CRUCIBLE_GENERATOR_MODEL=gpt-4o` internally). It is a **required pre-merge gate**
alongside `scripts/check_kernel_clean.sh`, `scripts/check_wheel_contents.sh`, the kernel
grep-gate, and `uv run lint-imports`. It is **not** wired into pre-commit: there is no CI
and the venv build is slow, so run it on demand before merging.


## Features

- **RAG Evaluation**: Evaluate RAG systems on Legal RAG Bench and GST Legal RAG with DeepEval LLM-judge metrics
  - Faithfulness (hallucination detection)
  - Contextual Precision (signal-to-noise in retrieved contexts)
  - Contextual Recall (coverage of relevant information)
  - Answer Relevancy (directness of response)

- **Replay Testing**: Replay production traffic against candidate services
  - Statistical comparison with baseline
  - Wilcoxon signed-rank test
  - Cliff's Delta effect size

- **Phoenix Observability**: Full OpenInference trace integration
  - CHAIN, RETRIEVER, LLM, EVALUATOR span kinds
  - Proper parent-child span hierarchy
  - Parquet fallback when Phoenix unavailable

- **Vector Backends**:
  - ChromaDB (embedded PersistentClient mode)
  - Zvec (for replay candidate service)

- **CLI Interface**:
  - Runner entrypoints (console scripts, invoked via `uv run <name>`):
    - `eval-rag` - Run RAG evaluation
    - `generate-spans` - Generate spans for replay
    - `eval-replay` - Run replay evaluation
  - `crucible` command group:
    - `crucible check bedrock` - Bedrock startup preflight (credentials / region / model access)
    - `crucible check phoenix` - Check Phoenix connectivity
    - `crucible check config` - Show configuration

## Quickstart

### Installation

```bash
# Clone repository
git clone <repo-url>
cd crucible

# Install everything (includes the `bedrock` extra needed for the default provider)
uv sync --all-extras --dev

# ...or a minimal Bedrock-only setup (boto3 + aiobotocore for the judge):
uv sync --extra bedrock

# Activate environment
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
```

> The default LLM provider is AWS Bedrock, so the `bedrock` extra
> (`boto3` + `aiobotocore`) must be installed. DeepEval's `AmazonBedrockModel`
> judge requires `aiobotocore` specifically. Run the runners with the extra
> available, e.g. `uv run --extra bedrock eval-rag ...`.

### Configuration

Crucible defaults to **AWS Bedrock** for both the RAG generator and the
DeepEval LLM-judge, using AU-geographic inference profiles (`au.*`) so
Australian legal/PII data stays in-country. OpenAI/GPT remains fully supported
as an opt-in for local testing.

Copy the example env file and edit it for your environment:

```bash
cp .env.example .env
```

`.env` is git-ignored. See `.env.example` for the full, documented list of
variables; the essentials are:

- **Bedrock defaults (no config needed for the common case):**
  - `CRUCIBLE_GENERATOR_PROVIDER=bedrock`, `CRUCIBLE_GENERATOR_MODEL=au.anthropic.claude-sonnet-4-6`
  - `CRUCIBLE_JUDGE_PROVIDER=bedrock`, `CRUCIBLE_JUDGE_MODEL=au.anthropic.claude-opus-4-6`
    (the judge model must differ from the generator model — no self-grading)
- **AWS region & profile (credential chain only — no access keys):**
  - `AWS_REGION` must match the inference-profile geography (e.g. `ap-southeast-2`
    for `au.*` profiles); optionally set `AWS_PROFILE`. The standard AWS
    credential chain handles authentication — do not put access keys in `.env`.
- **OpenAI opt-in (local testing without AWS):** set `OPENAI_API_KEY` and flip
  the provider/model overrides, e.g.
  `CRUCIBLE_JUDGE_PROVIDER=openai` + `CRUCIBLE_JUDGE_MODEL=gpt-4o`
  (and the matching `CRUCIBLE_GENERATOR_*` for the generator). A provider/model
  mismatch fails loud.

> **Inference profiles vs. bare model IDs.** The `au.*` defaults are
> cross-region inference profiles. Some AWS orgs block inference profiles via an
> SCP — every `au.*`/`apac.*`/`global.*` ID then returns `AccessDenied`. In that
> case use **bare on-demand model IDs** (e.g.
> `CRUCIBLE_GENERATOR_MODEL=anthropic.claude-3-haiku-20240307-v1:0`,
> `CRUCIBLE_JUDGE_MODEL=anthropic.claude-3-5-sonnet-20241022-v2:0`) pinned to an
> AU region. A bare ID is single-region: in `ap-southeast-2` (Sydney) it stays
> in-country — you only lose cross-region failover. Verify what an account can
> actually invoke with `crucible check bedrock`.

Run a fast preflight before a full eval to fail early on missing credentials, an
unset/mismatched region, or model access that isn't granted:

```bash
uv run --extra bedrock crucible check bedrock
```

### Basic Usage

```bash
# With .env configured (Bedrock defaults), just run a slice:
uv run --extra bedrock eval-rag --slice pico --rag stub-local      # Legal RAG Bench
uv run --extra bedrock eval-rag --slice gst_pico --rag stub-local  # GST Legal RAG

# Phoenix tracing is OPTIONAL and auto-attaches when a Phoenix server is
# reachable at PHOENIX_ENDPOINT (it degrades gracefully when it isn't — no flag
# to disable). To view traces, start Phoenix first:
docker-compose -f docker-compose.yml -f docker-compose.observability.yml up -d
export PHOENIX_ENDPOINT=http://localhost:6006
uv run --extra bedrock eval-rag --slice nano --rag stub-local

# Use Phoenix's native experiment API instead of span tracing:
uv run --extra bedrock eval-rag --slice nano --rag stub-local --phoenix-native
```

Available slices: `pico` (2), `nano` (10), `full` (100) for Legal RAG Bench;
`gst_pico` (2), `gst_nano` (10), `gst_mini` (20), `gst_full` (76) for GST Legal RAG.

### Dependency Groups

- Base (always installed): RAG evaluation core — `deepeval` (pinned), `sentence-transformers`, `chromadb`, `datasets`, `openai`
- `bedrock`: AWS Bedrock provider — `boto3`, `aiobotocore` (required for the default Bedrock generator and judge)
- `observability`: Phoenix tracing — `arize-phoenix`, `openinference-instrumentation-openai`
- `replay`: Replay testing — `arize-phoenix`, `fastapi`, `uvicorn`, `aiohttp`, `scipy`
- `dev` (dependency group): development tools — `pytest`, `pytest-cov`, `ruff`, `icontract`

Install specific extras:
```bash
uv sync --all-extras --dev   # Everything (incl. bedrock + dev tools)
uv sync --extra bedrock      # Base + Bedrock provider (minimal eval setup)
uv sync --extra observability  # Base + Phoenix tracing
uv sync --extra replay         # Base + replay testing
```

> `deepeval` is pinned to an exact version (`==4.0.5`). Judge semantics are
> version-dependent, so any bump is a deliberate re-baseline event — see the
> spec's upgrade runbook, not a routine dependency update.

## Documentation

- [RAG Evaluation Guide](docs/guides/rag-evaluation.md)
- [Replay Testing Guide](docs/guides/replay-testing.md)
- [Architecture Decisions](docs/adr/001-architecture-decisions.md)

## Docker Deployment

```bash
# Build image
docker-compose build

# Run with Phoenix observability
docker-compose -f docker-compose.yml -f docker-compose.observability.yml up

# Run evaluation in container
docker-compose run crucible eval-rag --slice pico --rag stub-local
```

## Development

```bash
# Run tests
uv run pytest

# Run linting
uv run ruff check --fix
uv run ruff format

# Install pre-commit hooks
pre-commit install
```

## License

[Specify your license here]
