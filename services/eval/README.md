# Evaluation Service — RAG Evaluation & Replay Testing

This service evaluates RAG (Retrieval-Augmented Generation) systems with DeepEval
LLM-judge metrics and Arize Phoenix observability, and compares candidate
deployments against a baseline via production-traffic replay. It is developed
directly in the monorepo target shape under `services/eval/`: the `app/` package
migrates 1:1 into `genai-backend/services/eval/app/` as a literal `cp -r app/`
(zero import rewrites), while the sibling `dev/` package holds the local-only
ChromaDB stub + `eval-rag` CLI and never migrates.

## Table of Contents

- [Architecture](#architecture)
- [Installation](#installation)
- [Configuration](#configuration)
- [Running an Evaluation](#running-an-evaluation)
- [Viewing Results in Phoenix](#viewing-results-in-phoenix)
- [Datasets, Slices & Metrics](#datasets-slices--metrics)
- [Dependencies & Extras](#dependencies--extras)
- [Design Invariants & Guardrails](#design-invariants--guardrails)
- [Migration Readiness](#migration-readiness)
- [Development](#development)
- [Docker](#docker)
- [Documentation](#documentation)

## Architecture

`services/eval/` splits into two siblings: `app/` (the deliverable) and `dev/`
(local-only). Within `app/`, the import-pure `kernel/` is a real subpackage and the
service children are flattened (`app/deepeval/`, `app/phoenix/`, ...). `dev/` never
migrates and never ships in the wheel or Docker image.

```
services/eval/
├── app/                 # MIGRATES 1:1 → genai-backend/services/eval/app/ (cp -r app/)
│   ├── kernel/          # PURE subpackage (no infra, no env, no network)
│   │   ├── rag_metrics/ #   DeepEvalEvaluator (metrics injected), sample transform, metric_specs
│   │   ├── replay_stats/#   comparison.py — Wilcoxon + Cliff's Delta (scipy mandatory)
│   │   ├── validation/  #   schema_validator — importlib.resources anchored on app.contracts
│   │   └── interfaces.py#   RagAdapter + JudgeProvider / RateLimiter / ClaimStore protocols
│   ├── deepeval/        # ADAPTER — bedrock_provider, embeddings (forbidden to the kernel)
│   ├── phoenix/         #   experiments, evaluators
│   ├── datasets/        #   legal_rag_bench + gst_legal_rag loaders, resolve.py
│   ├── metrics/         #   regression_check, csv_writer
│   ├── runners/         #   golden_set / replay / http_client → RunResult
│   ├── config.py        #   YAML + env config (load_dotenv only behind from_dotenv=True) + Settings
│   ├── contracts/       #   single-sourced JSON schemas (the importlib.resources anchor)
│   ├── api/ clients/ schemas/ main.py worker.py   # control/data-plane skin
├── dev/                 # LOCAL-ONLY — never migrates, never packaged, never in image
│   ├── cli/             #   thin shells: run_rag_eval / eval_replay / generate_spans / check
│   ├── stubs/           #   demo RAG (ChromaDB + sentence-transformers), Zvec service, span generator
│   ├── scripts/         #   corpus-prep utilities
│   └── fixtures/        #   data/chromadb, data/rag corpus, eval_config.yaml
├── tests/               # app-tests (must NOT import dev) + tests/dev/ (local-only)
├── pyproject.toml       # name=eval, package=app, 3 import-linter contracts (no console_scripts)
├── Dockerfile           # Python 3.12; CMD uvicorn app.main:app
└── .dockerignore        # excludes dev/, tests/, caches
```

**The one-way dependency rule (enforced, not aspirational):**

- **`app/kernel/`** may import only stdlib, `pydantic`, `jsonschema`, `scipy`,
  `deepeval`, `beartype`. It must **never** import the `app.deepeval` adapter, the
  other service children, `boto3`, `phoenix`/`arize`, `dotenv`, `dev`, nor
  read/write `os.environ` (except one allowlisted telemetry line).
- **`app/*`** (the service children + the control-plane skin) may import
  `app.kernel.*` and third-party infra. It must **never** import `dev`.
- **`dev/`** may import anything (including `app.*`); nothing in `app/` imports it.

These are enforced by three `import-linter` contracts in
`services/eval/pyproject.toml` — `kernel-pure`, `app-not-dev`, `api-not-scoring`
— plus a kernel grep-gate pre-commit hook. See [Development](#development).

## Installation

The service uses [`uv`](https://docs.astral.sh/uv/) with the `uv_build` backend and
targets **Python ≥ 3.12**. All work happens inside `services/eval/`.

```bash
git clone <repo-url>
cd <repo>/services/eval

# Everything (all extras + dev tools) — simplest for local work:
uv sync --all-extras --dev
```

The core install (the `app` wheel, no extras) is deliberately **kernel-grade** —
`pydantic`, `jsonschema`, `pyyaml`, `deepeval==4.0.5`, `scipy`, `polars`,
`beartype`, plus `fastapi`/`uvicorn`/`kubernetes` for the control plane — and pulls
in no ChromaDB / sentence-transformers. The demo stack lives behind extras you opt
into:

```bash
# Run the demo stub RAG locally with the AWS Bedrock judge, traced to Phoenix:
uv sync --extra demo --extra bedrock --extra phoenix
```

> The demo stub (`--rag stub-local`) needs the **`demo`** extra (ChromaDB +
> sentence-transformers for retrieval). The **`bedrock`** extra (`boto3` +
> `aiobotocore`) is required for the Bedrock judge; **`phoenix`** adds the Arize
> Phoenix client for tracing/experiments.

## Configuration

The RAG generator and the DeepEval judge both run on **AWS Bedrock** using
AU-geographic inference profiles (`au.*`), so Australian legal/PII data stays in
`ap-southeast-2`. Bedrock is the only supported provider. The **judge model must
differ from the generator model** — a same-model collision or a provider/model
mismatch fails loud.

The judge is configured in the `judge:` block of `dev/fixtures/eval_config.yaml`
(the single source read by `app.deepeval.bedrock_provider`); the AWS region and
Phoenix endpoint come from the environment:

```bash
AWS_REGION=ap-southeast-2                 # credential chain only — no keys in .env
PHOENIX_ENDPOINT=http://localhost:6006
```

```yaml
# dev/fixtures/eval_config.yaml
judge:
  provider: bedrock
  model: au.anthropic.claude-sonnet-4-5-20250929-v1:0   # judge — differs from the Sonnet-4-6 generator
  temperature: 0.0
```

Configuration precedence is env > YAML; a git-ignored `.env` is loaded by the dev
CLI shells and by `app.config.load_config(..., from_dotenv=True)`.

> **Bedrock inference profiles vs. bare model IDs.** The `au.*` defaults are
> cross-region inference profiles. If your AWS org blocks them via an SCP, every
> `au.*`/`apac.*`/`global.*` ID returns `AccessDenied`; use a **bare on-demand
> model ID** pinned to an AU region instead (e.g.
> `anthropic.claude-3-5-sonnet-20241022-v2:0` in `ap-southeast-2` — single-region,
> stays in-country, loses only cross-region failover). Verify what an account can
> actually invoke with `python -m dev.cli check bedrock`.

Preflight credentials / region / model-access before a full eval:

```bash
uv run python -m dev.cli check bedrock
```

> Optional per-process overrides for the judge/generator provider and model are
> read from the environment — `EVAL_JUDGE_PROVIDER`, `EVAL_JUDGE_MODEL`,
> `EVAL_GENERATOR_PROVIDER`, `EVAL_GENERATOR_MODEL` — with precedence env > YAML.

## Running an Evaluation

The RAG eval ingests a dataset's corpus into the local ChromaDB stub, generates
answers, scores them with the four DeepEval LLM-judge metrics, traces to Phoenix,
and writes CSV/Parquet results under `results/eval_rag/<timestamp>/`.

The dev CLIs ship no console scripts (they live in the local-only `dev/` package),
so run them as modules from `services/eval/`:

```bash
cd services/eval

# 1. Start a Phoenix server (REQUIRED — eval-rag runs the Phoenix-native flow):
uv run --extra phoenix phoenix serve          # or your docker-compose observability stack

# 2. Preflight that Phoenix is reachable:
uv run python -m dev.cli check phoenix

# 3. First run ingests the GST corpus into ChromaDB, then evaluates a slice:
DATA_DIR="$PWD/dev/fixtures/data/rag" CACHE_DIR="$PWD/dev/fixtures/data/rag" \
  uv run python -m dev.cli.run_rag_eval \
  --slice gst_pico --rag stub-local --config dev/fixtures/eval_config.yaml --force-reingest

# 4. Subsequent runs reuse the persisted collection — omit --force-reingest:
DATA_DIR="$PWD/dev/fixtures/data/rag" CACHE_DIR="$PWD/dev/fixtures/data/rag" \
  uv run python -m dev.cli.run_rag_eval \
  --slice gst_nano --rag stub-local --config dev/fixtures/eval_config.yaml
```

> **`--force-reingest` re-embeds the entire corpus on every query** (a known stub
> limitation). Use it only to (re)build the collection the first time; drop it for
> normal runs so retrieval reuses the persisted ChromaDB collection.

`run_rag_eval` runs the Phoenix-native **Datasets & Experiments** flow — the only
Phoenix path. It uploads the slice as a Phoenix dataset, runs a Phoenix experiment
with the four DeepEval metrics as evaluators (judge on AWS Bedrock), and writes the
canonical CSV / Parquet / JSON artifacts (`*_score` / `*_label` / `*_verdicts` +
`app_cost_usd` / `judge_cost_usd` / `total_cost_usd`) under
`results/eval_rag/<timestamp>/`. A running Phoenix server is REQUIRED;
`python -m dev.cli check phoenix` is the fail-fast preflight.

Other dev CLIs: `python -m dev.cli.eval_replay ...` (replay comparison),
`python -m dev.cli.generate_spans ...` (demo span generator), and the
`python -m dev.cli check bedrock|phoenix|config` preflights.

## Viewing Results in Phoenix

Open the Phoenix UI:

```
http://localhost:6006
```

- **Scored experiment** → **Datasets** → open the slice dataset (e.g.
  `gst-legal-rag-gst_pico`) → the experiment row shows per-row faithfulness /
  context_precision / context_recall / answer_relevancy with a click-to-inspect
  view per question. (Experiments live under **Datasets**, not the **Projects /
  Traces** view.)

To check Phoenix from the shell instead of the browser:

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:6006          # liveness
curl -s http://localhost:6006/v1/datasets | python -m json.tool          # datasets + ids
```

## Datasets, Slices & Metrics

| Dataset | Source | Slices |
|---|---|---|
| Legal RAG Bench | HuggingFace `isaacus/legal-rag-bench` | `pico` (2), `nano` (10), `full` (100) |
| GST Legal RAG | vendored JSONL (`dev/fixtures/data/rag/gst_legal_rag/`, 5,263 passages) | `gst_pico` (2), `gst_nano` (10), `gst_mini` (20), `gst_full` (76) |

Slice routing is prefix-based and consolidated in `app/datasets/resolve.py`:
`gst_*` → GST loader; any other slice → Legal RAG Bench.

The four DeepEval LLM-judge metrics: **Faithfulness** (hallucination detection),
**Contextual Precision** (signal-to-noise in retrieved context), **Contextual
Recall** (coverage of relevant passages), **Answer Relevancy** (directness).
Replay comparison uses the **Wilcoxon signed-rank test** + **Cliff's Delta**
(`app/kernel/replay_stats/comparison.py`; scipy is a mandatory core dependency — no
silent fallback).

## Dependencies & Extras

| Group | Contents | Purpose |
|---|---|---|
| core (always) | `pydantic`, `jsonschema`, `pyyaml`, `deepeval==4.0.5`, `scipy`, `polars`, `beartype`, `fastapi`, `uvicorn`, `kubernetes` | kernel-grade core + the control-plane skin |
| `bedrock` | `boto3`, `aiobotocore` | default Bedrock generator + judge (`AmazonBedrockModel` needs `aiobotocore`) |
| `phoenix` | `arize-phoenix`, `openinference-semantic-conventions` | native Datasets & Experiments flow |
| `demo` | `chromadb`, `sentence-transformers`, `datasets`, `huggingface-hub`, `rich`, `python-dotenv`, `click`, `aiohttp` | the local-only `dev/` stub RAG + CLIs; **never migrates** |
| `dev` (group) | `pytest`, `import-linter` | development tooling |

```bash
uv sync --all-extras --dev      # everything
uv sync --extra demo --extra bedrock --extra phoenix    # local stub + Bedrock judge + tracing
uv sync --extra bedrock         # minimal Bedrock judge
```

## Design Invariants & Guardrails

Each is backed by a test or a pre-commit gate, not a code-review hope:

- **`deepeval==4.0.5` exact pin.** Judge prompts are version-dependent, so a bump is
  a deliberate **re-baseline event**, never a routine update. Enforced by
  `tests/test_dependency_contract.py` (exact pin + matches `uv.lock`).
- **Judge ≠ generator.** A pure `assert_distinct(judge_id, generator_id)` in
  `app/kernel/rag_metrics/metric_specs.py` refuses same-model self-grading.
- **AU data residency.** Bedrock defaults are `au.*` inference profiles
  (`ap-southeast-2`), single-sourced in `metric_specs.py`.
- **Telemetry opt-out before deepeval imports.** `DEEPEVAL_TELEMETRY_OPT_OUT="YES"`
  is set at exactly two allowlisted sites (`app/kernel/rag_metrics/__init__.py`,
  `app/deepeval/__init__.py`), each before its package imports `deepeval` —
  verified by subprocess regression tests.
- **No import-time side effects.** Importing the `app` package mutates zero
  `os.environ`; `load_dotenv()` is confined to the dev CLI shells and `app.config`
  behind `from_dotenv=True`.
- **Kernel purity.** `import-linter` + a grep-gate forbid infra imports and
  `os.environ` access under `app/kernel/`; `tests/kernel/` passes in a zero-extras
  venv.
- **Single-source contracts.** The four JSON schemas live in `app/contracts/`
  (each with a `schema_version`); the validator resolves them by logical name via
  `importlib.resources` anchored on `app.contracts` (run-from-anywhere) or an
  injectable `schema_path`.
- **Claims/idempotency seam.** Runners accept an optional two-phase `ClaimStore`
  protocol (`claim(repro_key)` → `finalize(repro_key, result_uri)`) so the monorepo's
  idempotency layer plugs in without reshaping the library signatures.

## Migration Readiness

The repo is developed directly in the monorepo target shape, so there is **no
transform to rehearse**: migrating into `genai-backend/services/eval/app/` is a
literal `cp -r app/` with **zero import rewrites**. The `app/` package is fully
self-contained — it never imports the sibling `dev/` zone (enforced by the
`app-not-dev` import-linter contract), and `dev/` is excluded from both the wheel
(`module-name = "app"`) and the Docker image (`.dockerignore`).

On migration day, copy `services/eval/app/` into `genai-backend/services/eval/app/`,
point the monorepo's repo-level contracts at the injectable `schema_path`, supply
the real RDS / Phoenix / parser endpoints via environment (see `app/config.py`),
and carry the local-only `dev/fixtures/eval_config.yaml` only if running the dev CLI.

## Development

All tooling targets `services/eval/` (run from there, or with
`uv run --project services/eval`):

```bash
cd services/eval
uv run pytest                       # full suite (app-tests + tests/dev/)
uv run ruff check --fix && uv run ruff format
uv run lint-imports                 # kernel-pure / app-not-dev / api-not-scoring
```

`pre-commit` is the only gate (no CI): it runs ruff, `lint-imports`, the kernel
grep-gate, and pytest, all scoped to `services/eval/`.

Phoenix integration tests are marked `phoenix_integration` and skipped by default
(they need a reachable `PHOENIX_ENDPOINT`); run them with `-m phoenix_integration`.

> Hermetic note: the unit suite mocks all LLM/Bedrock/Phoenix calls. A single
> ChromaDB test-ordering flake (`test_chromadb_collection_exists`) passes in
> isolation.

## Docker

The image builds from `services/eval/` on Python 3.12 and runs the FastAPI
control plane (`uvicorn app.main:app`). `.dockerignore` keeps `dev/`, `tests/`,
and caches out of the image, so ONLY `app/` ships.

```bash
docker build -t eval-service services/eval
```

## Documentation

- **Migrating to the monorepo:** [docs/MIGRATION.md](../../docs/MIGRATION.md) — the runbook for moving into `genai-backend/services/eval/`
- Guides: [RAG evaluation](../../docs/guides/rag-evaluation.md), [replay testing](../../docs/guides/replay-testing.md), [GST corpus generation](../../docs/guides/gst-corpus-generation.md)
- Refactor / migration plan: `docs/eval_final/9-refactor-plan.md`
- Evaluation service design + API: `docs/eval_final/2-evaluation-design.md`, `docs/eval_final/7-api.md`
- Per-phase specs & verification reports: `agent-os/specs/*/`
