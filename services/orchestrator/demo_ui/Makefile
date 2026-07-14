# Guardrail demo quick-start — run from services/orchestrator/demo_ui/
#
#   make backend       # terminal 1: orchestrator on :8000 (Phoenix + OpenSearch wired)
#   make ui            # terminal 2: Chainlit demo UI on :8501 (default config = 1.4.0)
#   make test-redact   # fire a redaction test query at the running backend
#
# PHOENIX_ENDPOINT is set on both the backend and the UI so the joined trace
# (retrieval / generation / guardrail_input / guardrail_output spans) lands in
# the same Phoenix project. Override any variable on the command line, e.g.
#   make backend PHOENIX_ENDPOINT=http://localhost:6006
#   make test-redact Q="your own question"

PHOENIX_ENDPOINT ?= http://localhost:6006
ORCHESTRATOR_URL ?= http://localhost:8000
PIPELINE_CONFIG  ?= legal-rag-default-1.4.0
REGION           ?= ap-southeast-2
ORCH_DIR         := ..

# Redaction test query. Neutral "exhibit / test card" framing so the model
# actually reproduces the number in its answer (an SSN it usually refuses to
# echo); 4111 1111 1111 1111 is a Luhn-valid test card, so the credit_card rule
# redacts it → `‹redacted:credit_card›` + a `transform` decision. NOTE: this is
# model-dependent — if the model declines to echo, `guardrail_decisions` is []
# (correct: the guard can only redact PII the model actually writes). For a
# deterministic, model-free proof of the redaction logic, use `make test-redact-unit`.
Q ?= In a fraud matter an exhibit records the test card number 4111 1111 1111 1111. Please include that card number in your summary of the exhibit.

.PHONY: help backend ui readyz test-redact test-redact-unit

help:
	@echo "Targets: backend | ui | readyz | test-redact | test-redact-unit"
	@echo "Vars:    PHOENIX_ENDPOINT=$(PHOENIX_ENDPOINT)  ORCHESTRATOR_URL=$(ORCHESTRATOR_URL)  PIPELINE_CONFIG=$(PIPELINE_CONFIG)"

## Start the orchestrator backend on :8000 (Phoenix set; OpenSearch endpoint from
## $ORCHESTRATOR_OPENSEARCH_ENDPOINT, else discovered from the opensearch-poc CFN stack).
## Needs real AWS creds in the ambient boto3 chain (e.g. AWS_PROFILE=...).
backend:
	cd $(ORCH_DIR) && \
	ORCHESTRATOR_OPENSEARCH_ENDPOINT=$${ORCHESTRATOR_OPENSEARCH_ENDPOINT:-$$(aws cloudformation describe-stacks \
	  --region $(REGION) --stack-name opensearch-poc \
	  --query "Stacks[0].Outputs[?OutputKey=='DomainEndpoint'].OutputValue" --output text)} \
	PHOENIX_ENDPOINT=$(PHOENIX_ENDPOINT) \
	uv run uvicorn app.main:app --port 8000

## Start the Chainlit demo UI on :8501 (its own venv; default config legal-rag-default-1.4.0).
ui:
	PHOENIX_ENDPOINT=$(PHOENIX_ENDPOINT) \
	DEMO_UI_PIPELINE_CONFIG=$(PIPELINE_CONFIG) \
	uv run chainlit run app.py --port 8501

## Check the backend is up before driving the UI (must print 200).
readyz:
	@curl -s -o /dev/null -w "readyz: %{http_code}\n" $(ORCHESTRATOR_URL)/readyz