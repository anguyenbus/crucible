# Crucible: RAG Evaluation and Replay Testing Framework

Crucible is a standalone evaluation framework for RAG (Retrieval-Augmented Generation) systems and replay testing with Phoenix observability integration. It provides deterministic metrics for RAG quality and enables validation of model deployments through production traffic replay.

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
