# Git Repo Structure

**Plan:** Build Multiple Applications (Use Cases for Gen AI) using the exact same code base **without duplicating single line of python code atleast in the services section.**

## Platform Engines

```
GenAI-MonoRepo
    Services
        api-wrapper
        orchestrator
        ingestion
        eval
    Libs
        auth
        policy-router
        observability
        schemas
    Tests
        Integration
            docker-compose.yml        # Spins up all local engines + OpenSearch + Redis
            test_end-2-end_pipelines.py # Fires cross-services RAG verification suites
```

## Business Apps (Use-Cases)

```
eks-config-repo
    charts
        genai-universal/
    environments/
        devtest/
            case-assistant/               # App 1
                orchestrator-values.yaml
                ingestion-values.yaml
            hr-agent/                     # App 2
                orchestrator-values.yaml
                ingestion-values.yaml
```

\*\* The ingestion service doesnt know what a "hr-agent" or "support-case" is. It knows how to intake a PDF, etc and parse it, chunk it and put into OpenSearch. Similarly, Orchestration only knows how to takr a prompt, execute RAG and query LLM.

\*\* For example – If v1.3.0 of the ingestion services introduces a new PDF parsing library, we can update the case-assistant/ingestion-values.yaml to use the ingestion-v1.2.0 tag (docker image build) while leaving the other services to consume the newer tag.

– If business wants to introduce a new application (use-case), the team doesnot need to replicate whole code. Make the necessary modular changes to the code base of mono repo and change the config in the EKS-config. Pass different values for Opensearch index and Redis cluster, etc.

## Trace the changes – how a shared library impacts downstream services

a. Treat Shared libraries (libs/) as upstream dependencies of the services (platform engines)
b. Pipeline Must Automatically trace the blast radius

**[Diagram – flowchart, top to bottom:]**

- **genai-backend (App Monorepo)** containing: `libs/common-auth`, `libs/schemas`, `services/orchestrator`, `services/ingestion`
- ↓ *Developer Merges to Main*
- **GitLab CI/CD (The Pipeline Gate)**: Integration Tests / Local Compose → Semantic Release / Tag & Seal Image
- ↓ *Updates Image Tags*
- **eks-config-repo (CD Engine)**: `case-assistant/values.yaml` | `hr-agent/values.yaml`
- ↓ *Helm Upgrades* (both paths)
- **Non-Prod Block (EKS)**: Devtest Env → Preprod Env
- ↓ *Manual Release Branch*
- **Prod Block (EKS)**: Discovery Env → Staging Env → Prod Env

## Flow (Trunk-Based)

## Monorepo (GenAI-Backend – App)

Demonstrating this pipeline to resolve your exact questions about tagging, cross-repository mechanics, application isolation, and branching models.

### Phase 1 Deep Dive: Feature Branch Mechanics & Tagging

- Dev A → Case-Assistant → Working on libs/auth
- Dev B → Case-Assistant → Working on Ingestion fetch service
- Dev C → Hr-Agent → Working on libs/observability

When Developer A creates feature/case-assistant-update-jwt-logic branch and alters libs/auth/, the primary objective is ensuring the temporary images built for integration testing are immutable, completely unique, and safe from caching race conditions.

> **How to Tag Feature Branch Images?**

Checkout *feature-case-assistant-update-jwt-logic* from main as the Docker tag ✅

**CI Behaviour** – detects which service or library is changed. Detect-changed-services.sh script handle a Static Dependency matrix (script not only just look for Paths, must understand which services consume which libraries)

- If the code needs changes to libs/common-auth to update token validation logic, then the CI logics flags all relevant services
- Tests –
  - Static Analysis (Linting, type check, security scanning)
  - Unit Tests (pytest tests/unit/ – All external calls to Bedrock, openseaarch are mocked using unittest.mock)
  - Contract Test (Scripts/export-openapi.py generates openapi.json and it it differs from whats committed in the contacts/ unless the developer acknowledge their API change)

> **How Docker Compose Integrates the Unique Tags** — Your GitLab execution runner handles this dynamically using environment variables.

**Option A (Dind)**

| |
|---|
| Build a dedicated docker container specifically for running tests |
| runs in gitlab runner |
| docker compose up creates a private, isolated bridge network inside runner |
| The separation is logical and unique for each dev as the container, networks, etc will be tagged by the "COMPOSE_PROJECT_NAME" to logically separate. Container will fly with short commit SHA |

docker-compose.yml uses variable substitution: **Below is a sample ci.yml showing integration test**

```yaml
integration-tests:
  stage: integration-test
  image: docker:24.0.5
  services:
    - docker:24.0.5-dind #(DinD)
  script:
    # 1. Guarantee logical isolation by injecting a globally unique project name
    - export COMPOSE_PROJECT_NAME="pipeline-${CI_PIPELINE_ID}-job-${CI_JOB_ID}"

    # 2. Guarantee unique image tags for the feature branches
    - export IMAGE_TAG="sha-${CI_COMMIT_SHORT_SHA}"

    # 3. Execute the isolated stack
    - docker compose -f tests/integration/docker-compose.yml build
    - docker compose -f tests/integration/docker-compose.yml up -d --wait

    # 4. Run tests against this highly specific, isolated network
    - docker compose -f tests/integration/docker-compose.yml run --rm test-runner pytest /tests/integration/
```

This docker-compose.yaml (*tests/integration/docker-compose.yml*) file:

```yaml
# tests/integration/docker-compose.yml
version: '3.8'

# Docker automatically prefixes this network with the COMPOSE_PROJECT_NAME
networks:
  genai-ci-net:
    driver: bridge

services:
  # ------------------------------------------
  # BACKING SERVICES
  # ------------------------------------------
  opensearch:
    image: opensearchproject/opensearch:2.0.0
    environment:
      - discovery.type=single-node
      - bootstrap.memory_lock=true
      - "OPENSEARCH_JAVA_OPTS=-Xms512m -Xmx512m"
    networks:
      - genai-ci-net
    healthcheck:
      test: ["CMD", "curl", "-s", "-f", "http://localhost:9200/_cluster/health"]
      interval: 5s
      timeout: 5s
      retries: 10

  redis:
    image: redis:alpine
    networks:
      - genai-ci-net
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 5

  # ------------------------------------------
  # PLATFORM ENGINES
  # ------------------------------------------
  api-wrapper:
    # Uses the dynamically injected tag from GitLab CI
    image: my-registry.aws/api-wrapper:${IMAGE_TAG:-latest}
    build:
      context: ../../
      dockerfile: services/api-wrapper/Dockerfile
    environment:
      - ORCHESTRATOR_URL=http://orchestrator:8000
    networks:
      - genai-ci-net
    depends_on:
      - orchestrator

  orchestrator:
    # Uses the dynamically injected tag from GitLab CI
    image: my-registry.aws/orchestrator:${IMAGE_TAG:-latest}
    build:
      context: ../../
      dockerfile: services/orchestrator/Dockerfile
    environment:
      - OPENSEARCH_URL=http://opensearch:9200
      - REDIS_URL=redis://redis:6379
    networks:
      - genai-ci-net
    depends_on:
      opensearch:
        condition: service_healthy
      redis:
        condition: service_healthy

  # ------------------------------------------
  # TEST RUNNER
  # ------------------------------------------
  test-runner:
    image: my-registry.aws/test-runner:${IMAGE_TAG:-latest}
    build:
      context: ../../
      dockerfile: tests/integration/Dockerfile.test
      # (next lines cut off between photos)
      - ORCHESTRATOR_URL=http://orchestrator:8000
    networks:
      - genai-ci-net
    depends_on:
      - api-wrapper
      - orchestrator
```

> **What Happens After Integration Tests Pass?**

Once the integration tests return a success code, nothing is pushed to **stable production container registry yet**, and **no configurations change yet in to any env**.

- The pipeline turns green in the GitLab Merge Request UI, signaling to reviewers that the code is structurally sound.
- The MR receives a code-review via CODEOWNERS.

> CODEOWNERS should be divided into 2 group approval – @genai-architects and @services-owners

- feature branch tags are generated and pushed via CI pipeline to *sdpaap-develop-docker-repo* in artifactory and in *ECR dev artifactory*

### Phase 2 Deep Dive: The Atomic Commit & EKS Config Synchronization

**Target Branch:** If we want to maintain a true Trunk-Based Development model, developers must merge directly into main.

By running robust integration tests directly inside the feature branch MR, **main** remains perpetually stable and deployable.

> **Tag Naming: Universal vs. Application Prefixes**

Because the services and libraries are shared platform engines, we would never prefix tags with an application name.
- *libs-case-assistant-auth-v1.3.0 and orchestrator-case-assistant-v2.4.0* ❌
- *libs-auth-v1.3.0 and orchestrator-v2.4.0* ✅ (The code engine does not care who is calling it. The application identity exists purely as a runtime configuration parameter injected via Helm).

**Monorepo with Semantic release to guarantee Tag-triggered CI pipelines for docker images (Sealed tags – Code is matured and approved)**

- Main pipeline runs and executes Semantic release
- Semantic release creates the tag based on commit message
- pushes a new Git tag to the repository for the relevant services change (*orchestrator-v2.4.0* and *ingestion-v1.4.0*). changes are in *main branch*
- tag pipeline triggers by gitlab as it detects new tag was pushed and automatically starts a special release pipeline
- CI script uses reges to split the Git tag into SERVICE_NAME and VERSION
- Docker build runs using those 2 parsed vars provisioning exact 1:1 match with git tag

**Automated Monorepo Versioning Architecture**

1. **The Foundation:** Conventional Commits — To automate versioning, 20 developers must format their commit messages strictly following the Conventional Commits specification.

The commit prefix determines the semantic:

```
Version bump: fix(orchestrator): fix memory leak
Patch Bump (e.g., v1.0.0 to v1.0.1) feat(ingestion): add pdf parser
Minor Bump (e.g., v1.0.0 to v1.1.0) feat(api-wrapper)!: change auth contract (or adding BREAKING CHANGE: in the footer)
Major Bump (e.g., v1.0.0 to v2.0.0)
```

release.json file will define all these

2. **The Tooling:** Isolated Semantic Release — Instead of a single global release configuration, every independent service folder contains its own isolated Semantic Release configuration.

\*\* We can use the standard Node-based semantic-release tool running inside the directory of the changed service, leveraging the @semantic-release/commit-analyzer and @semantic-release/release-notes-generator plugins.

3. **Service-Level Configuration:** Each service gets its own configuration file, ensuring it only monitors its own path and generates custom-scoped tags.JSON // services/orchestrator/.releaserc.json

> **How the App Monorepo Modifies the EKS Config Repo (where all the config remains) when the merge train lands code on main**

Semantic Release seals the image with its official version tag (orchestrator:v2.4.0 and libs-auth-v1.3.0).

To update EKS, the Monorepo pipeline leverages a GitLab Multi-Project Pipeline Trigger using API automation which creates a new branch in eks-config and updates relevant env variable on that specific branch. \*\* This can be handled with more control if done manually

**[Sequence diagram — participants: Monorepo Pipeline | Container Registry | EKS Config Repo | Release Manager]**

1. Push sealed image (orchestrator:v2.4.0) → Container Registry
2. *Automated MR Generation Phase:* API Call: Create branch (feature/release/update-v2.4.0) → EKS Config Repo
3. API Call: Commit values.yaml diff to branch → EKS Config Repo
4. API Call: Open Merge Request to main → EKS Config Repo
   - **DEPLOYMENT HALTED: Waiting for Human Gate**
5. Review Diff & Approve MR (Release Manager → EKS Config Repo)
6. Merge to main (Release Manager → EKS Config Repo)
7. Trigger Helm CD Pipeline (Deploy to EKS)

## EKS-config (Repo which controls the – App Deployment)

Isolating Case-assistant from Hr-agent in the EKS Config Repo – If the case-assistant team needs the libs/auth modification immediately, but the hr-agent team is not ready to consume it, this is managed entirely via the environment folder structure on the main branch of the EKS Config repository. Because applications are distinct tenants in different namespaces, they have separate values.yaml configuration profiles. The automation script updates only the target application's configuration.

**Post-Automation Script Execution:** The automation script runs a targeted update command (using a tool like yq) affecting only case-assistant: Bash# Target only the case-assistant app configuration file

*ex- yq eval '.image.tag = "v2.4.0"' -i environments/devtest/case-assistant/orchestrator-values.yaml*

**Resulting State of eks-config-repo (main branch):**

```
environments/
└── devtest/
    ├── case-assistant/
    │   └── orchestrator-values.yaml -> (Now points to v2.4.0) 🚀 Updated!
    └── hr-agent/
        └── orchestrator-values.yaml -> (Still points to v2.3.9) 🔒 Safe!
```

Both applications continue running side-by-side in the Devtest cluster inside their respective namespaces without conflict, using different versions of the exact same code engine.

**environments/devtest/case-assistant/ingestion-values.yaml**

```yaml
image:
  repository: my-registry/ingestion
  tag: "orchestrator-v2.4.0"
env:
  - name: APP_NAME
    value: "case-assistant"
  - name: EMBEDDING_MODEL
    value: "aws:arn:::titanv2"
  - name: PARSING_STRATEGY
    value: "recursive:text_splitter"
  - name: OPENSEARCH_INDEX
    value: "support-tickets-v1"
```

**environments/devtest/hr-agent/orchestrator-values.yaml**

```yaml
image:
  repository: my-registry/orchestrator
  tag: "ingestion-v1.4.0"
env:
  - name: APP_NAME
    value: "hr-agent"
  - name: OPENSEARCH_INDEX
    value: "support-tickets-v1"
```

## EKS Architecture

> **Do different apps needs different resources? YES YES YES**

**Namespace Isolation** – Universal helm chart will create a completely independent set of k8s resources. they will independently scale, we can assign different IAM roles to different apps-services (by separating tje pods and namespaces)

### Phase 3 Deep Dive: Promotion Lifecycle – manage environment promotion

EKS-Config: Moving configurations through your remaining five tiers (Devtest → Preprod → Discovery → Staging → Prod)

| Devtest | Preprod | Discovery | Staging | Prod |
|---|---|---|---|---|
| feature branches | develop | release-discovery | release-staging | release-prod |
| feature branches changes for testing in develop (This is like good form of integration testing (svc-2-svc)) | develop branch can only deploy unless changes are made such as provision of alpha, beta, branches | release-branches and then merged to main | release-branches and then merged to main | release-branches and then merged to main |
| deployments for hr-agent will not affect case-assistant as they are separated by namespaces | here proper semantic tags should be present in the values .yml — deployments for hr-agent will not affect case-assistant | here proper semantic tags should be present | Extra checks to be placed – change request, changes to env files can only be done by authorised | Extra checks to be placed – change request, changes to env files can only be done by authorised |

## MonoRepo (Dev Flow):

1. Dev pushes Feature Branches
2. Pipeline runs through the Unit Tests, Lint, Contract test (no need to build an image till now)
   a. Option A – now a docker build can run if Step 2 is complete – "orchestrator-feature-\*" – pushed to artifactory dev
   b. Option B – Raise a MR to the main branch which triggers the Integration tests
      i. builds the docker image "orchestrator-feature-\*" – pushed to artifactory dev and ECR dev
      ii. Runs the docker-compose tests
3. If Option A or Option B tests passes and dev wants to deploy in devtest, then dev can deploy (after mutual consent)
   a. Option A – deploy-adhoc-devtest job trigger (auto)
   b. Option B – update the env/devtest/case-assistant/orchestrator-values.yml with the image tag created in STEP 2 and update required values
4. Helm overwrites the devtest pods using the latest deployment / changes in case-asistant
5. Once devtest testing is fully complete, the CODEOWNERS can review and merge the code to branch "main"
6. Semantic Release runs on main – build orchestrator-v2.4.0 and pushes the docker image into artifactory prod and ECR promotion through preprod

*(Page ends with the top edge of another diagram — content cut off in the photos)*