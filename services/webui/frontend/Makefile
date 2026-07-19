# Document Analyser (webui) — quick-start.  Run from services/webui/frontend/
#
# Phase 1 (chat):
#   make check          # offline gates: test + typecheck + lint + build (no services needed)
#   make dev            # Next.js dev server on :3000 (buffered default config 1.8.0)
#   make dev-stream     # dev server pinned to 1.2.0 (the live-token streaming mode)
#   make orchestrator   # convenience: orchestrator on :8000 (chat backend)
#
# Phase 2/3 (projects, upload, project-scoped chat — the BFF at services/webui/backend/):
#   make bff            # BFF on :8002 (projects/upload/index-lifecycle over SQLite)
#   make bff-check      # BFF offline gates: pytest + ruff (tmpfs venv)
#   make ingestion      # ingestion on :8001 — REQUIRED for projects (BFF provisions/deletes
#                       #   the per-project proj-{id} index through ingestion)
#
# Project-scoped chat: pick a project → chat hits its proj-{id} index; the general
# toggle (default OFF) comma-joins GENERAL_INDEX ($(GENERAL_INDEX)). Full stack needs
# all four up: orchestrator (chat) + ingestion (indexing) + BFF (projects) + UI.
#
# Ports (no collisions): orchestrator 8000 · ingestion 8001 · BFF 8002 · UI 3000.
# The UI reaches the orchestrator ONLY via its server-side proxy (ORCHESTRATOR_URL);
# it reaches the BFF browser-direct via NEXT_PUBLIC_BFF_URL (the BFF has CORS).
# Override any variable on the command line, e.g. make dev ORCHESTRATOR_URL=http://127.0.0.1:8000

ORCHESTRATOR_URL ?= http://localhost:8000
INGESTION_URL    ?= http://localhost:8001
BFF_URL          ?= http://localhost:8002
CONFIG_REF       ?= legal-rag-default-1.8.0
STREAM_CONFIG    ?= legal-rag-default-1.2.0
UNGUARDED_CONFIG ?= legal-rag-default-1.2.0
GENERAL_INDEX    ?= legal-rag-bench
PORT             ?= 3000
REGION           ?= ap-southeast-2
PHOENIX_ENDPOINT ?= http://localhost:6006
NEMO_GUARD_URL   ?= http://localhost:8080
ORCH_DIR         := ../../orchestrator
BFF_DIR          := ../backend
INGEST_DIR       := ../../ingestion
DEMO_UI_DIR      := ../../orchestrator/demo_ui

# Resolve the POC OpenSearch domain endpoint from the opensearch-poc CloudFormation
# stack (same mechanism demo_ui uses). Override by exporting ORCHESTRATOR_OPENSEARCH_ENDPOINT.
OS_ENDPOINT_CMD = ORCHESTRATOR_OPENSEARCH_ENDPOINT=$${ORCHESTRATOR_OPENSEARCH_ENDPOINT:-$$(aws cloudformation describe-stacks \
	  --region $(REGION) --stack-name opensearch-poc \
	  --query "Stacks[0].Outputs[?OutputKey=='DomainEndpoint'].OutputValue" --output text)}

.PHONY: help install check test typecheck lint build dev dev-stream \
        orchestrator ingestion bff bff-check guardrail-pod readyz invariants

help:
	@echo "Phase 1 (chat):     check | test | typecheck | lint | build | dev | dev-stream | orchestrator"
	@echo "Phase 2 (projects): bff | bff-check | ingestion"
	@echo "For the 1.8.0 default:  guardrail-pod (NeMo pod on :8080)"
	@echo "Shared:             install | readyz | invariants"
	@echo "Ports: orchestrator=8000  ingestion=8001  bff=8002  ui=$(PORT)"
	@echo "Vars:  ORCHESTRATOR_URL=$(ORCHESTRATOR_URL)  BFF_URL=$(BFF_URL)  CONFIG_REF=$(CONFIG_REF)"

install: ## Install deps from the committed lockfile (node_modules is on tmpfs — rerun after a reboot)
	npm install

# --- Offline gates (no orchestrator required) -------------------------------
check: test typecheck lint build ## Run every offline gate the verifier runs

test: ## Vitest browser-free unit suite (32 tests)
	npm test

typecheck: ## tsc --noEmit (strict)
	npm run typecheck

lint: ## ESLint 9
	npm run lint

build: ## next build
	npm run build

# --- Run the UI (chat + projects both served by one dev server) -------------
dev: ## Dev server on :$(PORT), buffered default config ($(CONFIG_REF)); talks to orchestrator + BFF
	ORCHESTRATOR_URL=$(ORCHESTRATOR_URL) \
	NEXT_PUBLIC_WEBUI_CONFIG_REF=$(CONFIG_REF) \
	NEXT_PUBLIC_BFF_URL=$(BFF_URL) \
	NEXT_PUBLIC_GENERAL_INDEX=$(GENERAL_INDEX) \
	NEXT_PUBLIC_WEBUI_UNGUARDED_CONFIG_REF=$(UNGUARDED_CONFIG) \
	npm run dev -- -p $(PORT)

dev-stream: ## Dev server pinned to the live-token streaming config ($(STREAM_CONFIG))
	ORCHESTRATOR_URL=$(ORCHESTRATOR_URL) \
	NEXT_PUBLIC_WEBUI_CONFIG_REF=$(STREAM_CONFIG) \
	NEXT_PUBLIC_BFF_URL=$(BFF_URL) \
	NEXT_PUBLIC_WEBUI_UNGUARDED_CONFIG_REF=$(UNGUARDED_CONFIG) \
	npm run dev -- -p $(PORT)

# --- Phase 2: the BFF (services/webui/backend/) -----------------------------
bff: ## Run the BFF on :8002 (projects/upload/ingestion; --reload picks up code edits)
	cd $(BFF_DIR) && WEBUI_INGESTION_URL=$(INGESTION_URL) WEBUI_ORCHESTRATOR_URL=$(ORCHESTRATOR_URL) \
	./.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8002 --reload --reload-dir app

bff-check: ## BFF offline gates: pytest + ruff (no live services)
	cd $(BFF_DIR) && uv run pytest && uv run ruff check

# --- Live drill helpers -----------------------------------------------------
orchestrator: ## Convenience: orchestrator on :8000 — resolves OpenSearch from CFN; wires the NeMo guard for 1.8.0
	cd $(ORCH_DIR) && $(OS_ENDPOINT_CMD) \
	ORCHESTRATOR_NEMO_GUARD_URL=$(NEMO_GUARD_URL) \
	PHOENIX_ENDPOINT=$(PHOENIX_ENDPOINT) \
	./.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

guardrail-pod: ## NeMo guardrail pod on :8080 (required for the 1.8.0 default) — delegates to demo_ui's pinned-digest target
	$(MAKE) -C $(DEMO_UI_DIR) guardrail-pod

ingestion: ## Convenience: ingestion service on :8001 — upload target (needs AWS/OpenSearch)
	@# Invoke the service's OWN venv interpreter directly, not `uv run`: a stale sibling
	@# checkout at /home/admin/projects/ingestion can make `uv run` pick the wrong venv
	@# (one without pypdf → 500 on PDF ingest). This service .venv definitively has pypdf.
	cd $(INGEST_DIR) && ./.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8001 --reload --reload-dir app

index-chunks: ## Inspect the chunks in an index: make index-chunks IDX=proj-<id> [FULL=1]
	cd $(INGEST_DIR) && ./.venv/bin/python -m scripts.inspect_index $(IDX) $(if $(FULL),--full,)

readyz: ## Probe readiness: orchestrator (chat) + BFF (projects, which itself probes ingestion)
	@echo "orchestrator:" && curl -s $(ORCHESTRATOR_URL)/readyz | head -c 300; echo
	@echo "bff:" && curl -s $(BFF_URL)/readyz | head -c 300; echo

invariants: ## Re-check the harvest/firewall invariants
	@echo "supabase hits (README prose only expected):" && grep -ri supabase . --exclude-dir=node_modules || true
	@echo "aws-sdk in package.json (should be empty):" && grep -i '@aws-sdk' package.json || echo "  none"
	@echo "BFF sibling-import firewall (should be empty):" && grep -rE "from services\.(orchestrator|ingestion)|import (orchestrator|ingestion)" $(BFF_DIR)/app || echo "  clean"
	@echo "siblings untouched:" && git -C ../../.. status --porcelain services/orchestrator/demo_ui/ services/ingestion/ || true
