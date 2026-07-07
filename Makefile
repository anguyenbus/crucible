# Infra helpers.
# OpenSearch POC domain lifecycle - see infra/opensearch-poc/template.yaml.
# Cost guard: `make opensearch-down` deletes the whole stack (domain + SG),
# which stops all charges. `make opensearch-status` shows what is running.

AWS_REGION ?= ap-southeast-2
STACK_NAME ?= opensearch-poc
TEMPLATE   := infra/opensearch-poc/template.yaml

.PHONY: help opensearch-validate opensearch-up opensearch-status opensearch-endpoint opensearch-down

help:
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-22s %s\n", $$1, $$2}'

opensearch-validate: ## Syntax-check the CloudFormation template (no resources created)
	aws cloudformation validate-template --region $(AWS_REGION) \
		--template-body file://$(TEMPLATE)

opensearch-up: ## Create/update the POC domain (~15-25 min first time, ~US$0.05/h while up)
	@status=$$(aws cloudformation describe-stacks --region $(AWS_REGION) \
		--stack-name $(STACK_NAME) \
		--query "Stacks[0].StackStatus" --output text 2>/dev/null || true); \
	if [ "$$status" = "ROLLBACK_COMPLETE" ]; then \
		echo "ERROR: stack is in ROLLBACK_COMPLETE (a previous create failed)."; \
		echo "CloudFormation cannot update it: run 'make opensearch-down' first, then retry."; \
		exit 1; \
	fi
	@aws iam get-role --role-name AWSServiceRoleForAmazonOpenSearchService >/dev/null 2>&1 || { \
		echo "Creating OpenSearch service-linked role (required for VPC domains, one-time per account)..."; \
		aws iam create-service-linked-role --aws-service-name opensearchservice.amazonaws.com >/dev/null; \
	}
	aws cloudformation deploy --region $(AWS_REGION) \
		--stack-name $(STACK_NAME) \
		--template-file $(TEMPLATE) \
		--no-fail-on-empty-changeset
	@$(MAKE) --no-print-directory opensearch-endpoint

opensearch-status: ## Show stack status (distinguishes 'deleted' from 'cannot tell')
	@out=$$(aws cloudformation describe-stacks --region $(AWS_REGION) \
		--stack-name $(STACK_NAME) \
		--query "Stacks[0].StackStatus" --output text 2>&1); rc=$$?; \
	if [ $$rc -eq 0 ]; then echo "$$out"; \
	elif echo "$$out" | grep -q "does not exist"; then \
		echo "Stack not found - no OpenSearch resources, no charges."; \
	else \
		echo "WARNING: could not determine stack status (credentials? region?):"; \
		echo "$$out"; exit 1; \
	fi

opensearch-endpoint: ## Print the domain endpoint and outputs
	@aws cloudformation describe-stacks --region $(AWS_REGION) \
		--stack-name $(STACK_NAME) \
		--query "Stacks[0].Outputs[].{Key:OutputKey,Value:OutputValue}" --output table

opensearch-down: ## Delete the stack (domain + SG) and wait until gone - stops billing
	aws cloudformation delete-stack --region $(AWS_REGION) --stack-name $(STACK_NAME)
	@echo "Deletion started (domain teardown takes ~10-20 min); waiting..."
	aws cloudformation wait stack-delete-complete --region $(AWS_REGION) --stack-name $(STACK_NAME)
	@echo "Stack deleted - no OpenSearch charges accruing."

# Eval data plane — SERVICE-DRIVEN ingest path (POC, amendment 2 2026-07-07).
# `opensearch-ingest` drives the INGESTION-SERVICE path: the ingestion service
# (services/ingestion) owns the index — this target provisions the index +
# hybrid-search-pipeline via its create_index.py, then drives the
# legal-rag-bench corpus (4,876 passages) through POST /ingest with the
# eval-side driver script. PREREQUISITE: the ingestion service must be running
# locally (from services/ingestion):
#   INGESTION_INDEX_NAME=$(OPENSEARCH_INGEST_INDEX) uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
# Endpoint discovery split is deliberate: the Makefile is CloudFormation-aware
# (auto-resolves DomainEndpoint); the Python side is env-only
# (INGESTION_OPENSEARCH_HOST for the service, EVAL_INGESTION_SERVICE_URL for
# the driver). START/LIMIT slice the corpus for resumable chunked runs.
.PHONY: opensearch-ingest opensearch-ingest-fallback

OPENSEARCH_INGEST_INDEX ?= legal-rag-bench
EVAL_INGESTION_SERVICE_URL ?= http://127.0.0.1:8000

opensearch-ingest: ## Provision legal-rag-bench + ingest corpus via the ingestion service (START=/LIMIT= to slice)
	@endpoint="$$INGESTION_OPENSEARCH_HOST"; \
	if [ -n "$$endpoint" ]; then \
		echo "Using endpoint $$endpoint (from INGESTION_OPENSEARCH_HOST env)"; \
	else \
		endpoint=$$(aws cloudformation describe-stacks --region $(AWS_REGION) \
			--stack-name $(STACK_NAME) \
			--query "Stacks[0].Outputs[?OutputKey=='DomainEndpoint'].OutputValue" \
			--output text); \
		if [ -z "$$endpoint" ] || [ "$$endpoint" = "None" ]; then \
			echo "ERROR: could not resolve DomainEndpoint from stack $(STACK_NAME) (make opensearch-status)"; \
			exit 1; \
		fi; \
		echo "Using endpoint $$endpoint (resolved from stack $(STACK_NAME))"; \
	fi; \
	echo "Provisioning index '$(OPENSEARCH_INGEST_INDEX)' + hybrid-search-pipeline (ingestion service create_index.py)..."; \
	( cd services/ingestion && \
		INGESTION_OPENSEARCH_HOST="$$endpoint" INGESTION_INDEX_NAME="$(OPENSEARCH_INGEST_INDEX)" \
		uv run python scripts/create_index.py ) || exit 1; \
	echo "Checking ingestion service health at $(EVAL_INGESTION_SERVICE_URL)..."; \
	curl -fsS "$(EVAL_INGESTION_SERVICE_URL)/healthz" >/dev/null || { \
		echo "ERROR: ingestion service not reachable at $(EVAL_INGESTION_SERVICE_URL)."; \
		echo "Start it first (from services/ingestion):"; \
		echo "  INGESTION_INDEX_NAME=$(OPENSEARCH_INGEST_INDEX) uv run uvicorn app.main:app --host 127.0.0.1 --port 8000"; \
		exit 1; \
	}; \
	cd services/eval && EVAL_INGESTION_SERVICE_URL="$(EVAL_INGESTION_SERVICE_URL)" \
		uv run python -m dev.scripts.ingest_legal_rag_bench_via_service \
		$(if $(START),--start $(START)) $(if $(LIMIT),--limit $(LIMIT))

# FALLBACK / BYO-reference path ONLY (NOT the POC ingest path): the eval-side
# GST ingest script chunks/embeds/bulk-indexes itself. Known limitations,
# documented not fixed: (a) its bulk 500-chunk batches are too heavy for
# t3.small.search and trigger 429s; (b) the vendored GST corpus is damaged
# (documents.jsonl truncated at 512 KiB) — GST is parked until re-supplied.
opensearch-ingest-fallback: ## [FALLBACK/BYO-reference] eval-side GST ingest script (known 429 limitation; GST corpus damaged)
	@endpoint="$$EVAL_OPENSEARCH_ENDPOINT"; \
	if [ -n "$$endpoint" ]; then \
		echo "Using endpoint $$endpoint (from EVAL_OPENSEARCH_ENDPOINT env)"; \
	else \
		endpoint=$$(aws cloudformation describe-stacks --region $(AWS_REGION) \
			--stack-name $(STACK_NAME) \
			--query "Stacks[0].Outputs[?OutputKey=='DomainEndpoint'].OutputValue" \
			--output text); \
		if [ -z "$$endpoint" ] || [ "$$endpoint" = "None" ]; then \
			echo "ERROR: could not resolve DomainEndpoint from stack $(STACK_NAME) (make opensearch-status)"; \
			exit 1; \
		fi; \
		echo "Using endpoint $$endpoint (resolved from stack $(STACK_NAME))"; \
	fi; \
	cd services/eval && EVAL_OPENSEARCH_ENDPOINT="$$endpoint" \
		uv run --extra opensearch --extra bedrock \
		python -m dev.scripts.ingest_gst_to_opensearch $(if $(RECREATE),--recreate)

# Manual operational smoke check (replaces any pytest live test): ONE hardcoded
# legal-domain query end-to-end through the eval retriever against the live
# index — embed (Titan V2) -> hybrid search (explicit hybrid-search-pipeline)
# -> ClaudeGenerator answer -> schema-valid v1.1.0 output. Prints ranked hits,
# the answer, and a final SMOKE PASS/FAIL line; non-zero exit on failure.
.PHONY: opensearch-smoke

opensearch-smoke: ## One hardcoded query end-to-end vs the live index (SMOKE PASS/FAIL; no pytest)
	@endpoint="$$EVAL_OPENSEARCH_ENDPOINT"; \
	if [ -n "$$endpoint" ]; then \
		echo "Using endpoint $$endpoint (from EVAL_OPENSEARCH_ENDPOINT env)"; \
	else \
		endpoint=$$(aws cloudformation describe-stacks --region $(AWS_REGION) \
			--stack-name $(STACK_NAME) \
			--query "Stacks[0].Outputs[?OutputKey=='DomainEndpoint'].OutputValue" \
			--output text); \
		if [ -z "$$endpoint" ] || [ "$$endpoint" = "None" ]; then \
			echo "ERROR: could not resolve DomainEndpoint from stack $(STACK_NAME) (make opensearch-status)"; \
			exit 1; \
		fi; \
		echo "Using endpoint $$endpoint (resolved from stack $(STACK_NAME))"; \
	fi; \
	cd services/eval && \
		EVAL_OPENSEARCH_ENDPOINT="$$endpoint" \
		EVAL_OPENSEARCH_INDEX="$(OPENSEARCH_INGEST_INDEX)" \
		EVAL_OPENSEARCH_PIPELINE="hybrid-search-pipeline" \
		EVAL_OPENSEARCH_TEXT_FIELD="content" \
		EVAL_OPENSEARCH_VECTOR_FIELD="content_vector" \
		AWS_REGION="$(AWS_REGION)" \
		uv run --extra opensearch --extra bedrock \
		python -m dev.stubs.rag.opensearch_query
