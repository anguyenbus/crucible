# Guardrail demo quick-start — run from services/orchestrator/demo_ui/
#
#   make backend        # terminal 1: orchestrator on :8000 (Phoenix + OpenSearch wired)
#   make guardrail-pod  # terminal 2: NeMo guardrail pod on :8080 (digests ENFORCED)
#   make ui             # terminal 3: Chainlit demo UI on :8501 (default config = 1.4.0)
#   make test-redact    # fire a redaction test query at the running backend
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
GUARDRAIL_DIR    := ../../guardrail

# Two-hash determinism digests for the ACTIVE NeMo config (`legal-rag-default-1.8.0`,
# the nemo-all config whose config_dir_digest now covers detectors.yml). The pod's
# `verify_pinned_digests` (services/guardrail/app/main.py lifespan) recomputes these
# live at startup and REFUSES to serve on any config/ or uv.lock drift. Regenerate
# with: `cd services/guardrail && uv run python -m app.config_digest`.
#
# NOTE: the pre-fold config_dir_digest (413182c3...) is SUPERSEDED by the
# detectors.yml fold — this 1.8.0 digest matches the post-fold live pod config/.
#
# ESCAPE HATCH (dev iteration): to edit services/guardrail/config/ WITHOUT the pod
# refusing to serve, run `make guardrail-pod-dev` (or launch uvicorn with BOTH env
# vars UNSET). A `None` expectation skips that half of verify_pinned_digests — an
# EMPTY string does NOT (it is compared and mismatches), so genuinely unset them.
GUARDRAIL_EXPECTED_CONFIG_DIR_DIGEST ?= b5da30dbae57309c6045138bd569bf13d7c67ed19ceb2153484183e1eb7b2183
GUARDRAIL_EXPECTED_UV_LOCK_SHA256    ?= 86d6e9a5b1b7b1bb4bd549747890c232e5d8d521ec5078767ad7d060b1f19603

# Redaction test query. Neutral "exhibit / test card" framing so the model
# actually reproduces the number in its answer (an SSN it usually refuses to
# echo); 4111 1111 1111 1111 is a Luhn-valid test card, so the credit_card rule
# redacts it → `‹redacted:credit_card›` + a `transform` decision. NOTE: this is
# model-dependent — if the model declines to echo, `guardrail_decisions` is []
# (correct: the guard can only redact PII the model actually writes). For a
# deterministic, model-free proof of the redaction logic, use `make test-redact-unit`.
Q ?= In a fraud matter an exhibit records the test card number 4111 1111 1111 1111. Please include that card number in your summary of the exhibit.

.PHONY: help backend guardrail-pod guardrail-pod-dev ui readyz test-redact test-redact-unit

help:
	@echo "Targets: backend | guardrail-pod | guardrail-pod-dev | ui | readyz | test-redact | test-redact-unit"
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

## Start the NeMo guardrail pod on :8080 with the two-hash digests ENFORCED
## (its own venv, services/guardrail/). verify_pinned_digests recomputes the live
## config/ digest + uv.lock sha at startup and REFUSES to serve on any mismatch, so
## the demo exercises the determinism lineage end-to-end, not on faith. Needs real
## AWS creds in the ambient boto3 chain. See guardrail-pod-dev for the escape hatch.
guardrail-pod:
	cd $(GUARDRAIL_DIR) && \
	GUARDRAIL_EXPECTED_CONFIG_DIR_DIGEST=$(GUARDRAIL_EXPECTED_CONFIG_DIR_DIGEST) \
	GUARDRAIL_EXPECTED_UV_LOCK_SHA256=$(GUARDRAIL_EXPECTED_UV_LOCK_SHA256) \
	uv run uvicorn app.main:app --port 8080

## ESCAPE HATCH — start the pod on :8080 WITHOUT the digest check (both expected env
## UNSET ⇒ verify_pinned_digests skips), so you can iterate on services/guardrail/
## config/ without a refuse-on-mismatch. NOT for the acceptance demo (use guardrail-pod).
guardrail-pod-dev:
	cd $(GUARDRAIL_DIR) && uv run uvicorn app.main:app --port 8080

## Start the Chainlit demo UI on :8501 (its own venv; default config legal-rag-default-1.4.0).
ui:
	PHOENIX_ENDPOINT=$(PHOENIX_ENDPOINT) \
	DEMO_UI_PIPELINE_CONFIG=$(PIPELINE_CONFIG) \
	uv run chainlit run app.py --port 8501

## Check the backend is up before driving the UI (must print 200).
readyz:
	@curl -s -o /dev/null -w "readyz: %{http_code}\n" $(ORCHESTRATOR_URL)/readyz
