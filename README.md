# Crucible: RAG Evaluation and Replay Testing Framework

Crucible is a standalone evaluation framework for RAG (Retrieval-Augmented Generation) systems and replay testing with Phoenix observability integration. It provides deterministic metrics for RAG quality and enables validation of model deployments through production traffic replay.

## Features

- **RAG Evaluation**: Evaluate RAG systems on Legal RAG Bench with DeepEval LLM-judge metrics
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
  - `crucible eval-rag` - Run RAG evaluation
  - `crucible generate-spans` - Generate spans for replay
  - `crucible eval-replay` - Run replay evaluation
  - `crucible serve` - Serve candidate service
  - `crucible check phoenix` - Check Phoenix connectivity
  - `crucible check config` - Show configuration

## Quickstart

### Installation

```bash
# Clone repository
git clone <repo-url>
cd crucible

# Install with uv
uv sync --all-extras --dev

# Activate environment
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
```

### Basic Usage

```bash
# Set up API keys
export OPENAI_API_KEY=your-key-here
export HF_TOKEN=your-token-here

# Run RAG evaluation (pico slice = 2 queries)
uv run crucible eval-rag --slice pico --rag stub-local

# Run with Phoenix observability
docker-compose -f docker-compose.yml -f docker-compose.observability.yml up -d
export PHOENIX_ENDPOINT=http://localhost:6006
uv run crucible eval-rag --slice nano --rag stub-local --enable-phoenix
```

### Dependency Groups

- `core`: Base RAG evaluation (deepeval, sentence-transformers, chromadb, datasets)
- `observability`: Phoenix tracing (arize-phoenix, openinference-instrumentation-openai)
- `replay`: Replay testing (fastapi, uvicorn, aiohttp, scipy)
- `dev`: Development tools (pytest, pytest-cov, ruff, icontract)

Install specific groups:
```bash
uv sync --all-extras  # Everything
uv sync --extra observability  # Core + Phoenix
uv sync --extra replay  # Everything except dev
```

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
