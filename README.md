# Crucible — RAG Evaluation & Replay Testing Framework

Crucible evaluates RAG (Retrieval-Augmented Generation) systems with DeepEval
LLM-judge metrics and Arize Phoenix observability, and compares candidate
deployments against a baseline via production-traffic replay. Its internals are
shaped as three strictly-layered packages — `kernel` (pure), `service`
(infra-coupled), `local` (demo/CLI) — so the codebase can migrate into the
`genai-backend/services/eval/app/` monorepo as a directory copy plus a single
mechanical import rewrite, a property proven continuously by a rehearsal script.

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

The package is split into three layers with a one-way dependency rule. The split
mirrors the monorepo target one-to-one: `kernel/` → `app/kernel/`, each `service/`
child → `app/<child>/`, and `local/` never migrates.

```
src/crucible/
├── kernel/              # PURE — copies verbatim to app/kernel/ (no infra, no env, no network)
│   ├── rag_metrics/     #   DeepEvalEvaluator (metrics injected), sample transform,
│   │                    #   metric_specs (au.* defaults, assert_distinct), telemetry opt-out
│   ├── replay_stats/    #   comparison.py — Wilcoxon + Cliff's Delta (scipy mandatory)
│   ├── validation/      #   schema_validator — importlib.resources default + injectable schema_path
│   └── interfaces.py    #   RagAdapter + JudgeProvider / RateLimiter / ClaimStore protocols
├── service/             # INFRA-COUPLED — each child copies to app/<child>/
│   ├── deepeval/        #   bedrock_provider (env/boto/dotenv/judge resolution), embeddings
│   ├── phoenix/         #   adapter, experiments, evaluators, replay_client, annotations (§8.2)
│   ├── datasets/        #   legal_rag_bench + gst_legal_rag loaders, resolve.py (single gst_ dispatch)
│   ├── metrics/         #   regression_check, csv_writer
│   ├── runners/         #   run_golden_set / run_phoenix_native / run_replay → RunResult
│   └── config.py        #   YAML + env config (load_dotenv only behind from_dotenv=True)
├── local/               # NEVER migrates — quarantined behind the `demo` extra
│   ├── cli/             #   thin shells: eval-rag / eval-replay / generate-spans / crucible check
│   └── stubs/           #   demo RAG (ChromaDB + sentence-transformers), Zvec service, span generator
└── contracts/           # single-sourced JSON schemas (the importlib.resources anchor)
```

**The one-way dependency rule (enforced, not aspirational):**

- **`kernel/`** may import only stdlib, `pydantic`, `jsonschema`, `scipy`,
  `deepeval`, `beartype`. It must **never** import `boto3`, `phoenix`/`arize`,
  `dotenv`, `crucible.service`, `crucible.local`, nor read/write `os.environ`
  (except one allowlisted telemetry line).
- **`service/`** may import `crucible.kernel.*` (absolute) and third-party infra.
  It must **never** import `crucible.local`.
- **`local/`** may import anything; nothing in `kernel`/`service` imports it.

These are enforced by `import-linter` (`kernel-pure`, `service-not-local`,
`nothing-imports-local`) and a kernel grep-gate pre-commit hook — see
[Development](#development).

## Installation

Crucible uses [`uv`](https://docs.astral.sh/uv/) with the `uv_build` backend and
targets **Python ≥ 3.12**.

```bash
git clone <repo-url>
cd crucible

# Everything (all extras + dev tools) — simplest for local work:
uv sync --all-extras --dev
```

The core install (`pip install crucible`, no extras) is deliberately
**kernel-grade** — `pydantic`, `jsonschema`, `pyyaml`, `deepeval==4.0.5`, `scipy`,
`polars`, `beartype` — and pulls in no ChromaDB / sentence-transformers.
Everything else lives behind extras you opt into:

```bash
# Run the demo stub RAG locally with the AWS Bedrock judge, traced to Phoenix:
uv sync --extra demo --extra bedrock --extra phoenix
```

> The demo stub (`--rag stub-local`) needs the **`demo`** extra (ChromaDB +
> sentence-transformers for retrieval). The **`bedrock`** extra (`boto3` +
> `aiobotocore`) is required for the Bedrock judge; **`phoenix`** adds the Arize
> Phoenix client for tracing/experiments.

## Configuration

Crucible runs the RAG generator and the DeepEval judge on **AWS Bedrock**
using AU-geographic inference profiles (`au.*`), so Australian legal/PII data
stays in `ap-southeast-2`. Bedrock is the only supported provider.

Configuration is read from the environment (a git-ignored `.env` is loaded by the
CLI shells and by `service.config.load_config(..., from_dotenv=True)`). The
**judge model must differ from the generator model** — a same-model collision or a
provider/model mismatch fails loud.

**AWS Bedrock:**

```bash
CRUCIBLE_GENERATOR_PROVIDER=bedrock
CRUCIBLE_GENERATOR_MODEL=au.anthropic.claude-sonnet-4-6
CRUCIBLE_JUDGE_PROVIDER=bedrock
CRUCIBLE_JUDGE_MODEL=au.anthropic.claude-opus-4-6   # must differ from generator
AWS_REGION=ap-southeast-2                            # credential chain only — no keys in .env
PHOENIX_ENDPOINT=http://localhost:6006
```

> **Bedrock inference profiles vs. bare model IDs.** The `au.*` defaults are
> cross-region inference profiles. If your AWS org blocks them via an SCP, every
> `au.*`/`apac.*`/`global.*` ID returns `AccessDenied`; use a **bare on-demand
> model ID** pinned to an AU region instead (e.g.
> `anthropic.claude-3-5-sonnet-20241022-v2:0` in `ap-southeast-2` — single-region,
> stays in-country, loses only cross-region failover). Verify what an account can
> actually invoke with `uv run --extra bedrock crucible check bedrock`.

For a Bedrock run, preflight credentials / region / model-access before a full
eval:

```bash
uv run --extra bedrock crucible check bedrock
```

## Running an Evaluation

The RAG eval ingests a dataset's corpus into the local ChromaDB stub, generates
answers, scores them with the four DeepEval LLM-judge metrics, traces to Phoenix,
and writes CSV/Parquet results under `results/eval_rag/<timestamp>/`.

```bash
# 1. Start Phoenix (REQUIRED — eval-rag runs the Phoenix-native experiment flow):
docker-compose -f docker-compose.yml -f docker-compose.observability.yml up -d

# 2. Preflight that Phoenix is reachable before running an eval:
uv run crucible check phoenix

# 3. First run ingests the corpus into ChromaDB, then evaluates a slice:
uv run eval-rag --slice gst_pico --rag stub-local --force-reingest

# 4. Subsequent runs reuse the persisted collection (data/chromadb/) — omit --force-reingest:
uv run eval-rag --slice gst_nano --rag stub-local
```

> **`--force-reingest` re-embeds the entire corpus on every query** (a known stub
> limitation). Use it only to (re)build the collection the first time; drop it for
> normal runs so retrieval reuses `data/chromadb/`.

`eval-rag` runs the Phoenix-native **Datasets & Experiments** flow — the only
Phoenix path. It uploads the slice as a Phoenix dataset, runs a Phoenix experiment
with the four DeepEval metrics as evaluators (judge on AWS Bedrock), and writes the
canonical CSV / Parquet / JSON artifacts (`*_score` / `*_label` / `*_verdicts` +
`app_cost_usd` / `judge_cost_usd` / `total_cost_usd`) under
`results/eval_rag/<timestamp>/`. A running Phoenix server is REQUIRED;
`crucible check phoenix` is the fail-fast preflight.

Other console scripts: `uv run eval-replay ...` (replay comparison),
`uv run generate-spans ...` (demo span generator), and the `crucible` command group
(`crucible check bedrock|phoenix|config`).

## Viewing Results in Phoenix

Phoenix runs locally via docker-compose (`docker-compose.observability.yml`). Open
the UI:

```
http://localhost:6006
```

- **Scored experiment** → **Datasets** → open the slice dataset (e.g.
  `gst-legal-rag-gst_pico`) → the experiment row shows per-row faithfulness /
  context_precision / context_recall / answer_relevancy with a click-to-inspect
  view per question.

To check Phoenix from the shell instead of the browser:

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:6006          # liveness
curl -s http://localhost:6006/graphql -H 'content-type: application/json' \
  -d '{"query":"{ projects { edges { node { name traceCount } } } }"}'   # projects + counts
```

## Datasets, Slices & Metrics

| Dataset | Source | Slices |
|---|---|---|
| Legal RAG Bench | HuggingFace `isaacus/legal-rag-bench` | `pico` (2), `nano` (10), `full` (100) |
| GST Legal RAG | vendored JSONL (`data/rag/gst_legal_rag/`, 5,263 passages) | `gst_pico` (2), `gst_nano` (10), `gst_mini` (20), `gst_full` (76) |

Slice routing is prefix-based and consolidated in `service/datasets/resolve.py`:
`gst_*` → GST loader; any other slice → Legal RAG Bench.

The four DeepEval LLM-judge metrics: **Faithfulness** (hallucination detection),
**Contextual Precision** (signal-to-noise in retrieved context), **Contextual
Recall** (coverage of relevant passages), **Answer Relevancy** (directness).
Replay comparison uses the **Wilcoxon signed-rank test** + **Cliff's Delta**
(`kernel/replay_stats/comparison.py`; scipy is a mandatory core dependency — no
silent fallback).

## Dependencies & Extras

| Group | Contents | Purpose |
|---|---|---|
| core (always) | `pydantic`, `jsonschema`, `pyyaml`, `deepeval==4.0.5`, `scipy`, `polars`, `beartype` | kernel-grade; `pip install crucible` is import-clean |
| `bedrock` | `boto3`, `aiobotocore` | default Bedrock generator + judge (`AmazonBedrockModel` needs `aiobotocore`) |
| `phoenix` | `arize-phoenix`, `openinference-semantic-conventions` | native Datasets & Experiments flow |
| `replay` | `fastapi`, `uvicorn`, `click`, `aiohttp` | replay testing + the Zvec stub service |
| `demo` | `chromadb`, `sentence-transformers`, `datasets`, `huggingface-hub`, `rich`, `python-dotenv` | the local stub RAG; **never migrates** |
| `dev` (group) | `pytest`, `pytest-cov`, `ruff`, `icontract`, `import-linter` | development tooling |

```bash
uv sync --all-extras --dev      # everything
uv sync --extra demo --extra bedrock --extra phoenix    # local stub + Bedrock judge + tracing
uv sync --extra bedrock         # minimal Bedrock judge
```

## Design Invariants & Guardrails

Each is backed by a test or a CI gate, not a code-review hope:

- **`deepeval==4.0.5` exact pin.** Judge prompts are version-dependent, so a bump is
  a deliberate **re-baseline event**, never a routine update. Enforced by
  `tests/test_dependency_contract.py` (exact pin + matches `uv.lock`).
- **Judge ≠ generator.** A pure `assert_distinct(judge_id, generator_id)` in
  `kernel/rag_metrics/metric_specs.py` refuses same-model self-grading.
- **AU data residency.** Bedrock defaults are `au.*` inference profiles
  (`ap-southeast-2`), single-sourced in `metric_specs.py`.
- **Telemetry opt-out before deepeval imports.** `DEEPEVAL_TELEMETRY_OPT_OUT="YES"`
  is set at exactly two allowlisted sites (`kernel/rag_metrics/__init__.py`,
  `service/deepeval/__init__.py`), each before its package imports `deepeval` —
  verified by subprocess regression tests.
- **No import-time side effects.** `import crucible` mutates zero `os.environ`;
  `load_dotenv()` is confined to the CLI shells and `service.config` behind
  `from_dotenv=True`.
- **Kernel purity.** `import-linter` + a grep-gate forbid infra imports and
  `os.environ` access under `kernel/`; `tests/kernel/` passes in a zero-extras venv.
- **Single-source contracts.** The four JSON schemas live in `src/crucible/contracts/`
  (each with a `schema_version`); the validator resolves them by logical name via
  `importlib.resources` (run-from-anywhere) or an injectable `schema_path`.
- **Claims/idempotency seam.** Runners accept an optional two-phase `ClaimStore`
  protocol (`claim(repro_key)` → `finalize(repro_key, result_uri)`) so the monorepo's
  idempotency layer plugs in without reshaping the library signatures.

## Migration Readiness

The migration into `genai-backend/services/eval/app/` is **exactly** a directory
copy plus these import rewrites — nothing else moves:

| Rewrite | Effect |
|---|---|
| `crucible.kernel` → `app.kernel` | `kernel/` copies whole to `app/kernel/` |
| `crucible.service.` → `app.` | service children flatten into `app/` (`service/config.py` → `app/config.py`; there is no `app/service/`) |
| `crucible.contracts` → `app.contracts` | `contracts/` copies to `app/contracts/`; the validator resolves it via `importlib.resources` |
| `from crucible[.service] import` → `from app import` | defensive bare top-level forms |

After the rewrite, **zero dotted `crucible.` references** may survive (the proper
noun "Crucible" in prose is fine — the assertion is dotted).

`scripts/rehearse_migration.sh` proves this continuously: it copies the three
buckets into a fresh `app/` under `services/eval/` (the committed scaffold), applies
the rewrites, asserts zero `crucible.`, runs `uv sync --frozen` against the scaffold's
lockfile, import-smokes every `app.*` module, and runs the mirrored test suite. The
generated `app/` packages + copied tests are gitignored (reproduced on demand from
`src/`); only the scaffold is committed. **Green = the migration is proven by
construction.**

```bash
bash scripts/rehearse_migration.sh            # rehearse into services/eval (the scaffold)
bash scripts/rehearse_migration.sh <DEST>     # rehearse into a real destination on migration day
```

It is idempotent and self-contained. It is a **required pre-merge gate** (run on
demand; not a pre-commit hook — the venv build is slow and there is no CI). On
migration day, run it against the real `genai-backend/services/eval/` and wire the
monorepo's repo-level contracts via the injectable `schema_path`; carry along the
repo-root `eval_config.yaml` (its config-shape test self-skips when absent).

## Development

```bash
uv run pytest                       # full suite (set CRUCIBLE_GENERATOR_MODEL for hermetic runs)
uv run ruff check --fix && uv run ruff format
uv run lint-imports                 # kernel-pure / service-not-local / nothing-imports-local
bash scripts/check_kernel_clean.sh  # tests/kernel/ green in a zero-extras venv
bash scripts/check_wheel_contents.sh  # the 4 schemas ship in the built wheel
bash scripts/check_kernel_grep_gate.sh  # no forbidden code under kernel/
pre-commit install
```

Phoenix integration tests are marked `phoenix_integration` and skipped by default
(they need a reachable `PHOENIX_ENDPOINT`); run them with `-m phoenix_integration`.

> Hermetic note: the unit suite mocks all LLM/Bedrock/Phoenix calls. Set
> `CRUCIBLE_GENERATOR_MODEL=au.anthropic.claude-sonnet-4-6` (or any non-default) when a stale `.env` would
> otherwise trip the renamed-var guard. A single ChromaDB test-ordering flake
> (`test_chromadb_collection_exists`) passes in isolation.

## Docker

```bash
docker-compose build
docker-compose -f docker-compose.yml -f docker-compose.observability.yml up -d
docker-compose run crucible eval-rag --slice gst_pico --rag stub-local
```

## Documentation

- **Migrating to the monorepo:** [docs/MIGRATION.md](docs/MIGRATION.md) — the tested runbook for moving into `genai-backend/services/eval/`
- Guides: [RAG evaluation](docs/guides/rag-evaluation.md), [replay testing](docs/guides/replay-testing.md), [GST corpus generation](docs/guides/gst-corpus-generation.md)
- Refactor / migration plan: `docs/eval_final/9-refactor-plan.md`
- Evaluation service design + API: `docs/eval_final/2-evaluation-design.md`, `docs/eval_final/7-api.md`
- Per-phase specs & verification reports: `agent-os/specs/*/`
