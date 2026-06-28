# Dev Workflow

*(Document begins with an empty 4-column × 5-row table — contents not legible in the photo)*

**Goal:** Structure the end to end workflow to maximize the developer velocity without sacrificing stability

## Assumptions:

1. 30 Developers working across different services (Orchestrator, Policy Router, Ingestion, Eval, Infra, Platform, shared libs, Auth, etc etc)
2. Team Leads to be assigned + a shared group which constitutes of the core Architecture team to discuss matters when there is a conflict in opinion. CODEOWNERS to be enforced
3. Need Strict Isolation rules (Read access to any repo can be provided to whole team but only a few members can modify some of the repos)
4. Branching strategy should be defined and semantic tags/releases have to be enforced
5. Local Development and Integration should be a norm to follow so that things dont break
6. No Gitops (use pull-based model as currently using helm / kustomize / kubectl)

## Repo Architectural Validations:

1. **Repo 1:** EKS Backend Infra (Mainly Terraform) — Handle all the infra part (EKS, Addons, Istio, Node groups, Karpenter, IAM) — it calls TF submodules
2. **Repo 2:**
   - Store Helm values, templates, and IRSA (IAM roles for SA) to achieve clean deployments.
   - Any Configuration change such as tweaking an env variable or scaling replicas will not trigger a heavy application rebuild
3. **Repo 3:** Modular Monolith (Gen AI Backend)
   - Grouping services
   - Along side shared library
4. CI/CD logic lives centrally in the atlas-cicd-repo: design modular CD templates that the application monorepo calls on demand

## Develop Day to Day workflow:

```
genai-backend/
  services/
    api-wrapper/
      app/
        main.py               # FastAPI app entry point
        routers/              # Frontend-facing API routes
          chat.py
          ingestion.py
          eval.py
          admin.py
        middleware/           # Auth context, tracing, correlation IDs
        clients/              # HTTP clients for orchestrator/ingestion/eval/policy
        schemas/              # Pydantic request/response models
        config.py             # Service config
      tests/
      Dockerfile
      pyproject.toml

    orchestrator/
      app/
        main.py
        routers/              # Orchestrator API endpoints
        orchestrator/
          policy_router.py    # Direct/RAG/tool routing
          query_rewrite.py    # Query rewrite / expansion
          retriever.py        # OpenSearch retrieval client
          reranker.py         # Reranking integration
          context_assembler.py # Context assembly/token budgeting
          prompt_builder.py   # Prompt construction
          guardrails.py       # Input/output guardrail handling
          citation_builder.py # Citation construction/validation
        clients/              # OpenSearch, Redis, ML gateway, policy clients
        schemas/
        config.py
      tests/
      Dockerfile
      pyproject.toml

    ingestion/
      app/
        main.py               # Optional ingestion control API
        api/                  # Register document, status, reindex, delete
        workers/              # SQS worker entry points
        pipeline/
          fetch.py            # Fetch from S3/raw archive
          hash_dedup.py       # Raw SHA-256 exact dedup
          parse.py            # Parser/Docling/Textract adapter
          normalize.py        # Normalize extracted content
          redact.py           # Optional redaction
          enrich.py           # Metadata enrichment
          chunk.py            # Chunking
          embed.py            # Embedding calls
          index.py            # OpenSearch indexing
        state/                # Document registry/case mapping persistence
        clients/
        schemas/
        config.py
      tests/
      Dockerfile
      pyproject.toml

    eval/
      app/
        main.py
        api/                  # Eval trigger/status/result APIs
        runners/              # Eval execution flow
        ragas/                # RAGAS adapters
        metrics/              # Metric aggregation/custom checks
        mlflow/               # MLflow logging
        datasets/             # Eval set handling
        clients/
        schemas/
        config.py
      tests/
      Dockerfile
      pyproject.toml

  libs/
    auth/                     # JWT/principal utilities
    policy/                   # Policy service client/common models
    audit/                    # Audit event schema/writer
    observability/            # OpenTelemetry/logging helpers
    schemas/                  # Shared Pydantic types
    clients/                  # Shared HTTP client utilities
    errors/                   # Common exception/error response model

  contracts/
    openapi/
      api-wrapper-openapi.json    # Exported frontend-facing OpenAPI spec
      orchestrator-openapi.json   # Internal service spec if needed
      ingestion-openapi.json
      eval-openapi.json

  scripts/
    export-openapi.py             # Generates OpenAPI specs from FastAPI
    detect-changed-services.sh    # Build only changed services
    smoke-test.sh

  tests/
    integration/                  # Cross-service integration tests
    contract/                     # API contract tests

  .gitlab-ci.yml                  # Backend CI/CD
  README.md
```

- **Modular Monolith (GenAI Backend)**
  - Grouping services alongside shared libs eliminates the massive overhead of publishing and versioning internal packages that all these services require

    1 > dev working on RAG pipeline branches off main/master (ex- feature/ingestion-enhancements). MOdify code inside /services/ingestion and test locally if possible
  - Contract verification: if they modify an API endpoint, they must run scripts/export-openapi.py locally to update the contracts/openapi/ingestion-openapi.json
  - repo calls detect-changed-services.sh (in the atlas-cicd repo), it analyses the git diff and generates a dynamic .gitlab-ci-child.yaml on the fly, instructing which specific services has to be built
    (LOGIC - changes inside any services - trigger that service pipeline)
    - Changes inside libs/schema/common folders - trigger pipeline for all services which depend on the shared things
  - Lint, Unit test and contract test run automatically in the test stage
  - CODEOWNERS automatically adds dev/architects for review once the MR is raised
  - CI - Build, Tag the docker with short GIT SHA and Publish the docker image (artifactory as well as ECR)
  - CD - updates the EKS config repo - updates the dev/ingestion/values.yaml with new docker tag / release tag and commits the changes and executes helm upgrade to push the new images into dev cluster

- **Config only update** — CHanges to EKS config

```
eks-config-repo/
├── charts/
│   └── genai-service/        # The Universal Helm Chart (Platform Owned)
│       ├── Chart.yaml
│       └── templates/
│           ├── deployment.yaml  # Contains if/else logic for node selectors
│           ├── service.yaml
│           ├── hpa.yaml         # Horizontal Pod Autoscaling
│           └── serviceaccount.yaml # For AWS IRSA (IAM Roles)
│
└── environments/
    └── dev/                  # Developer-Owned Configs
        ├── api-wrapper/
        │   └── values.yaml
        ├── orchestrator/
        │   └── values.yaml
        ├── ingestion-hr/
        │   └── values.yaml
        └── ingestion-xyz/
            └── values.yaml
```

**universal chart –**

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ .Release.Name }}
spec:
  template:
    spec:
      serviceAccountName: {{ .Release.Name }}-sa
      containers:
        - name: {{ .Release.Name }}
          image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
          env:
            {{- toYaml .Values.env | nindent 12 }}

      # Dynamically assign to Karpenter provisioners if defined
      {{- if .Values.nodeSelector }}
      nodeSelector:
        {{- toYaml .Values.nodeSelector | nindent 8 }}
      {{- end }}

      # Dynamically add tolerations for specialized hardware
      {{- if .Values.tolerations }}
      tolerations:
        {{- toYaml .Values.tolerations | nindent 8 }}
      {{- end }}
```

Dev working on the ingestion service needs to deploy EKS — no need to touch kubernetest yaml for deployment

**modify specific yaml files**

```yaml
# environments/dev/ingestion/values.yaml

image:
  repository: 123456789012.dkr.ecr.ap-southeast-2.amazonaws.com/genai-ingestion
  tag: "a1b2c3d" # This is injected by the GitLab CI/CD pipeline

# Assign an AWS IAM Role for Bedrock/OpenSearch access (IRSA)
serviceAccount:
  create: true
  annotations:
    eks.amazonaws.com/role-arn: "arn:aws:iam::123456789012:role/IngestionServiceRole"

# Request Karpenter to provision a specific node type for heavy workloads
nodepool:

# Application specific configuration
env:
  - name: OPENSEARCH_ENDPOINT
    value: "https://vpc-opensearch-domain.ap-southeast-2.es.amazonaws.com"
  - name: LOG_LEVEL
    value: "DEBUG"
```

**As part of the CD –**

```bash
helm upgrade --install ingestion ./charts/genai-universal \
  -f ./environments/devtest/hr-agent/ingestion-values.yaml \
  --set image.tag=$CI_COMMIT_SHORT_SHA \
  --namespace hr-agent
```

- **Code + config**
  - Deploy the config First –

    Updating and adding a dynamo Table, redis cluster URL, etc
  - Deploy the code (mono repo)

**Package the chart**
Instead of using flat yaml files, we can package them to a helm repo
App version v1.5 → rely on helm chart ingestion-chart:1.0.0
App version v2.0 → (new IAM role) relies on helm chart ingestion-chart-2.0.0

**Centralized CI**
- template A: Build Template (build.yaml) — Docker image built and pushed with short SHA and tags
- template B: Deploy template — expects var such as TARGET env, IMAGE tag. Pull helm chart from EKS config and executes helm upgrade

*(Document ends with the header row of a table: **Pros | Cons | Comments** — body rows cut off / not visible in the photos)*