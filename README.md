# Crucible — Legal RAG Platform

Crucible is a retrieval-augmented-generation platform for legal documents: upload
your documents, and the system parses, chunks, embeds, and indexes them, then lets
you **chat over them with citations**, **extract facts / summaries**, and **compare
two documents for contradictions**. Answers are generated on **AWS Bedrock** (Claude
Sonnet) with an optional NeMo guardrail lane; every retrieval is grounded in your
own indexed text. A separate evaluation service scores the RAG pipeline with
LLM-judge metrics and Arize Phoenix.

> The CodeCommit remote is still named `eval-service` — that evaluation service was
> the project's origin. It has since grown into the multi-service platform
> documented here; the eval service is now one component (`services/eval/`).

## Table of Contents

- [Architecture](#architecture)
- [Cross-cutting invariants](#cross-cutting-invariants)
- [The services](#the-services)
- [Document pipeline & features](#document-pipeline--features)
- [Running locally](#running-locally)
- [Configuration](#configuration)
- [Development & gates](#development--gates)
- [Documentation](#documentation)

## Architecture

Crucible is a set of independently-deployable services that talk to each other
**over HTTP only** — no service imports another's Python package (the "dependency
firewall"). Each owns exactly one concern.

```
 Browser ──browser-direct (CORS)──▶ BFF :8002 ──▶ ingestion :8001 ──▶ parser :8003
    │                                  │                 │                  │
    │                                  │                 └── OpenSearch + Bedrock (embed)
    │                                  │                                    └── Textract (scanned pages)
    └──server-side proxy (/api/*)──▶ orchestrator :8000 ──▶ OpenSearch (retrieval) + Bedrock (generate)
                                          │
                                          └── NeMo guardrail pod :8080  (1.8.0 guarded chat only)

 evaluation (services/eval) runs offline against the RAG pipeline, traced to Phoenix :6006
```

| Service        | Dir                       | Port | Concern                                                        |
|----------------|---------------------------|------|---------------------------------------------------------------|
| Web UI         | `services/webui/frontend` | 3000 | Next.js 15 / React 19 / Tailwind 4 — chat, projects, upload, compare |
| BFF            | `services/webui/backend`  | 8002 | Projects/documents/upload (SQLite); proxies siblings over HTTP |
| Orchestrator   | `services/orchestrator`   | 8000 | RAG query path + non-RAG `/analyze` and `/compare` on Bedrock  |
| Ingestion      | `services/ingestion`      | 8001 | Parse → chunk → embed → index into per-project OpenSearch      |
| Parser         | `services/parser`         | 8003 | Docling (CPU) + AWS Textract/Bedrock-VLM document parsing      |
| Guardrail      | `services/guardrail`      | 8080 | NeMo Guardrails pod (Bedrock Haiku) — input/output guarding    |
| Evaluation     | `services/eval`           |  —   | DeepEval LLM-judge metrics + Phoenix experiments + replay      |

## Cross-cutting invariants

These hold across every service and are backed by tests / import-linter contracts,
not convention:

- **Dependency firewall.** A service consumes its siblings **HTTP-only** and never
  imports their packages (BFF→ingestion/orchestrator, ingestion→parser, …). This
  keeps each independently deployable — the orchestrator is destined to run as its
  own K8s pod.
- **Bedrock-only, AU data residency.** Generation and judging run on AWS Bedrock
  `au.*` inference profiles in **`ap-southeast-2`** — Claude **Sonnet** for
  generation, **Haiku** for the guard classifier, Titan v2 for embeddings.
  **Never Opus or OpenAI.** Model IDs come from **pinned config artifacts**, not env.
- **Pinned pipeline configs.** The orchestrator resolves a fully-qualified
  `{name}-{semver}` ref (e.g. `legal-rag-default-1.8.0`) to a packaged YAML,
  sha256-verified against a committed manifest before use — retroactive config
  edits visibly mismatch recorded runs.
- **Honest degradation / error translation.** Downstream "never-raises" contracts
  (the parser) are translated at each boundary into truthful HTTP status — a call
  that failed is never dressed up as an empty-but-successful result.
- **Import purity.** `import-linter` contracts forbid infra imports in the pure
  cores: `stages-pure` (orchestrator pipeline stays free of boto3/opensearch/otel)
  and `kernel-pure` (eval kernel). LangChain, added for `/compare`, is confined to
  `orchestrator/app/routers` for exactly this reason.
- **Phoenix-native evaluation.** An eval run means a dataset + experiment recorded
  in Phoenix; CSV artifacts alone do not count.

## The services

### Web UI — `services/webui/frontend` (:3000)
Next.js 15 / React 19 / Tailwind 4 single-page app. Chat streams from the
orchestrator through a **same-origin proxy** (`/api/query/stream`, SSE); projects,
documents, and compare go **browser-direct** to the BFF (`NEXT_PUBLIC_BFF_URL`).
Features: project-scoped chat with citations, document upload with live ingest
progress, a document-detail view (inline PDF/markdown/image/HTML + extracted text),
on-demand **Facts / Summary**, and **Compare Documents for Contradiction**.
See [frontend/README.md](services/webui/frontend/README.md) and
[frontend/RUNNING.md](services/webui/frontend/RUNNING.md).

### BFF (backend-for-frontend) — `services/webui/backend` (:8002)
Owns projects/documents/upload over SQLite and orchestrates the sibling calls the
browser can't make cleanly. Key routes: `POST /projects/{id}/documents` (upload →
store → ingest), `GET …/{doc}/file` and `…/{doc}/text` (detail view),
`POST …/{doc}/analyze`, and `POST /projects/{id}/compare`. Talks to ingestion +
orchestrator over their env-var URLs only.
See [backend/README.md](services/webui/backend/README.md).

### Orchestrator — `services/orchestrator` (:8000)
The RAG query path plus two non-RAG endpoints, all on pinned Bedrock models:
- `POST /query` + `POST /query/stream` — hybrid BM25+kNN OpenSearch retrieval →
  context assembly → Bedrock generation → citation build, with per-stage
  OpenInference spans to Phoenix and an optional NeMo guard lane.
- `POST /analyze` — single-document summary / facts extraction.
- `POST /compare` — **two-document contradiction detection** via a LangChain
  *decompose-then-verify* pipeline (each document → atomic quoted claims → the two
  claim sets are cross-examined and classified into six contradiction types).
- `GET /healthz` / `GET /readyz`.

Raw-`boto3` throughout except `/compare`'s LangChain client. The wire contract is
committed at `services/orchestrator/openapi.json` (regenerate with
`make orchestrator-openapi`; a test fails on drift).

### Ingestion — `services/ingestion` (:8001)
Turns an uploaded document into indexed chunks: parse → chunk → embed (Titan v2) →
index into the per-project `proj-{id}` OpenSearch index. `.md`/`.markdown` decode
locally; **every other format is routed to the parser** over HTTP. `POST /ingest`
(drain) + `POST /ingest/stream` (per-phase SSE), plus index-lifecycle endpoints
(`/indices/{name}`, `…/documents/{doc}/chunks`). A raw-bytes SHA-256 gate skips
re-ingesting identical uploads. See [ingestion/README.md](services/ingestion/README.md).

### Parser — `services/parser` (:8003)
Document → markdown + provenance. **Docling** (CPU) is the default engine; individual
pages that come out low-confidence escalate to **AWS Textract** (default) or a Bedrock
**VLM**. Handles PDF (digital + scanned), images, DOCX, XLSX/XLSM, HTML. `POST /parse`
(drain) + `POST /parse/stream` (per-page SSE). **Never-raises** by contract — ingestion
translates its results into honest status. CPU-only torch keeps the image lean. First
run needs a heavy `uv sync` (`make parser-sync`). See [parser/README.md](services/parser/README.md).

### Guardrail — `services/guardrail` (:8080)
An out-of-process **NeMo Guardrails** pod on Bedrock **Haiku** (via `langchain-aws`),
providing input/output guarding for the guarded chat config (`legal-rag-default-1.8.0`).
Deliberately Bedrock-only (no OpenAI path). See [guardrail/README.md](services/guardrail/README.md)
and [docs/guardrail-consolidation-plan.md](docs/guardrail-consolidation-plan.md).

### Evaluation — `services/eval`
Scores the RAG pipeline with four DeepEval LLM-judge metrics (Faithfulness,
Contextual Precision/Recall, Answer Relevancy) as **Phoenix experiments**, and
compares candidate deployments against a baseline via production-traffic replay
(Wilcoxon + Cliff's Delta). Judge on Bedrock, **judge ≠ generator** enforced.
Full detail in [eval/README.md](services/eval/README.md).

## Document pipeline & features

```
upload ─▶ BFF stores bytes ─▶ ingestion: parse ─▶ chunk ─▶ embed ─▶ index (proj-{id})
                                   │
                                   └─ .md decoded locally; all else ─▶ parser (docling + Textract)

indexed text then powers:
  • chat        RAG over the project index, cited                  (orchestrator /query)
  • facts       per-document summary / discrete facts              (orchestrator /analyze)
  • compare     two-document contradiction table, 6 types          (orchestrator /compare)
```

The **six contradiction types** surfaced by compare: Temporal, Numerical, Authority,
Process, Policy Reversal, Specificity — each row carries the exact conflicting quote
from both documents.

## Running locally

The interactive dev workflow uses per-service `make` targets, driven from the
frontend directory. **The step-by-step guide is
[services/webui/frontend/RUNNING.md](services/webui/frontend/RUNNING.md)** — it
covers prerequisites (AWS creds, `make install`, `make parser-sync`), the
one-terminal-per-service startup order, verification, and troubleshooting.

Quick version (full stack, from `services/webui/frontend/`):

```bash
make install        # once (node_modules is on tmpfs — rerun after a reboot)
make parser-sync    # once (heavy: docling + torch)

make orchestrator   # :8000   (AWS creds)   — terminal 1
make parser         # :8003   (AWS creds)   — terminal 2
make ingestion      # :8001   (AWS creds)   — terminal 3
make bff            # :8002                  — terminal 4
make dev            # :3000                  — terminal 5
make guardrail-pod  # :8080   (only for the 1.8.0 guarded chat)

make readyz         # probe orchestrator + BFF readiness
```

Supporting infra: `docker-compose.yml` runs the containerised `eval`/`ingestion`/
`parser` services; the top-level `Makefile` manages the **OpenSearch POC** domain
(`make opensearch-up` / `opensearch-status` / `opensearch-down`) whose CloudFormation
template lives in [infra/opensearch-poc](infra/opensearch-poc). Observability
(Phoenix) is `docker-compose.observability.yml`.

## Configuration

- **AWS.** Services use the ambient credential chain in **`ap-southeast-2`** and need
  Bedrock (Sonnet + Haiku + Titan), OpenSearch (the `opensearch-poc` domain), and
  Textract (scanned pages only). The orchestrator resolves the OpenSearch endpoint
  from the CloudFormation stack automatically.
- **Pipeline config.** Behaviour (model IDs, temperature, retrieval params) is pinned
  in `services/orchestrator/app/configs/legal-rag-default-*.yaml`; the default is
  `legal-rag-default-1.8.0` (guarded). Chat can be pointed at an unguarded config
  (e.g. `1.2.0`) to skip the guard pod.
- **Env carries location facts only** (endpoints, index/field names, sibling URLs,
  Phoenix endpoint) — never model behaviour.

## Development & gates

Each service is gated independently (there is no monorepo CI; `pre-commit` and the
per-service suites are the gates):

```bash
# orchestrator
cd services/orchestrator && ./.venv/bin/python -m pytest -q && uv run lint-imports

# BFF
cd services/webui/backend && uv run pytest && uv run ruff check

# frontend  (from services/webui/frontend)
make check          # vitest + typecheck + lint + build

# eval
cd services/eval && uv run pytest && uv run lint-imports
```

Notable gates: `stages-pure` / `kernel-pure` import-linter contracts, the
orchestrator OpenAPI snapshot (`make orchestrator-openapi`), the parser heavy suite
(docling installed), and the frontend vitest/tsc/eslint trio.

## Documentation

- **Run the stack:** [services/webui/frontend/RUNNING.md](services/webui/frontend/RUNNING.md)
- **Per-service:** [orchestrator](services/orchestrator) · [ingestion](services/ingestion/README.md) · [parser](services/parser/README.md) · [guardrail](services/guardrail/README.md) · [BFF](services/webui/backend/README.md) · [frontend](services/webui/frontend/README.md) · [eval](services/eval/README.md)
- **Design docs:** [BYO index contract](docs/byo-index-contract.md), [chunking strategy](docs/chunking-strategy-comparison.md), [parser service plan](docs/parser-service-plan.md), [model selection & evaluation](docs/model-selection-and-evaluation-process.md), [guardrail consolidation plan](docs/guardrail-consolidation-plan.md), [evaluation](docs/evaluation.md), [Phoenix + DeepEval integration](docs/phoenix-deepeval-integration.md)
- **Specs & verification reports:** `agent-os/specs/*/`
