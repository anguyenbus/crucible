# genai-backend Repo Tree

```
genai-backend/
  services/
    api-wrapper/
      app/
        main.py                       # FastAPI app entry point
        routers/                      # Frontend-facing API routes
          chat.py
          ingestion.py
          eval.py
          admin.py
        middleware/                   # Auth context, tracing, correlation IDs
        clients/                      # HTTP clients for orchestrator/ingestion/eval/policy
        schemas/                      # Pydantic request/response models
        config.py                     # Service config
      tests/
      Dockerfile
      pyproject.toml

    orchestrator/
      app/
        main.py
        routers/                      # Orchestrator API endpoints
        orchestrator/
          policy_router.py            # Direct/RAG/tool routing
          query_rewrite.py            # Query rewrite / expansion
          retriever.py                # OpenSearch retrieval client
          reranker.py                 # Reranking integration
          context_assembler.py        # Context assembly/token budgeting
          prompt_builder.py           # Prompt construction
          guardrails.py               # Input/output guardrail handling
          citation_builder.py         # Citation construction/validation
        clients/                      # OpenSearch, Redis, ML gateway, policy clients
        schemas/
        config.py
      tests/
      Dockerfile
      pyproject.toml

    ingestion/
      app/
        main.py                       # Optional ingestion control API
        api/                          # Register document, status, reindex, delete
        workers/                      # SQS worker entry points
        pipeline/
          fetch.py                    # Fetch from S3/raw archive
          hash_dedup.py               # Raw SHA-256 exact dedup
          parse.py                    # Parser/Docling/Textract adapter
          normalize.py                # Normalize extracted content
          redact.py                   # Optional redaction
          enrich.py                   # Metadata enrichment
          chunk.py                    # Chunking
          embed.py                    # Embedding calls
          index.py                    # OpenSearch indexing
        state/                        # Document registry/case mapping persistence
        clients/
        schemas/
        config.py
      tests/
      Dockerfile
      pyproject.toml

    eval/
      app/
        main.py
        api/                          # Eval trigger/status/result APIs
        runners/                      # Eval execution flow
        ragas/                        # RAGAS adapters
        metrics/                      # Metric aggregation/custom checks
        mlflow/                       # MLflow logging
        datasets/                     # Eval set handling
        clients/
        schemas/
        config.py
      tests/
      Dockerfile
      pyproject.toml

  libs/
    auth/                             # JWT/principal utilities
    policy/                           # Policy service client/common models
    audit/                            # Audit event schema/writer
    observability/                    # OpenTelemetry/logging helpers
    schemas/                          # Shared Pydantic types
    clients/                          # Shared HTTP client utilities
    errors/                           # Common exception/error response model

  contracts/
    openapi/
      api-wrapper-openapi.json        # Exported frontend-facing OpenAPI spec
      orchestrator-openapi.json       # Internal service spec if needed
      ingestion-openapi.json
      eval-openapi.json

  scripts/
    export-openapi.py                 # Generates OpenAPI specs from FastAPI
    detect-changed-services.sh        # Build only changed services
    smoke-test.sh

  tests/
    integration/                      # Cross-service integration tests
    contract/                         # API contract tests

  .gitlab-ci.yml                      # Backend CI/CD
  README.md
```