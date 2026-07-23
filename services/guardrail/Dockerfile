# Dockerfile for the guardrail service (the app/ + config/ deliverable).
#
# Build context is services/guardrail/. .dockerignore keeps tests/, spike/,
# .venv and caches out of the image, so ONLY app/ (the pod) and config/ (the
# hashed NeMo config, authored in Phase 2) ship. Nothing in this pod is imported
# by the orchestrator app (I2) — it is reached ONLY over HTTP.
#
# I1 — Bedrock-only: the venv is resolved from the PINNED uv.lock ONLY (a locked
# artifact). NEVER the `nemoguardrails[server]` extra — it pulls `openai`, which
# the no-OpenAI hard gate forbids.

# ============================================================================
# BUILDER STAGE - resolve the pinned guard/transport closure into a venv
# ============================================================================
FROM python:3.13-slim AS builder

# UV_PROJECT_ENVIRONMENT builds the venv at its FINAL /opt/venv path so
# console-script shebangs (uvicorn) point at a python that still exists in the
# runtime stage.
ENV UV_COMPILE_BYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv

# uv package manager
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /build

# Dependency layer: ONLY the manifests are copied and the project itself is not
# installed (the pod ships as app/ copied below, imported from WORKDIR /app —
# no wheel). --frozen forbids any lock drift at build time: the image resolves
# EXACTLY the audited, openai-free uv.lock or the build fails.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# ============================================================================
# RUNTIME STAGE - minimal final image
# ============================================================================
FROM python:3.13-slim AS runtime

# NEMOGUARDRAILS_LLM_FRAMEWORK=langchain forces NeMo 0.23.0 onto its LangChain
# framework at import time — Bedrock's ONLY path (FINDINGS: the default OpenAI-
# compatible framework RAISES for bedrock). Defence in depth: the lifespan also
# calls set_default_framework("langchain") before building LLMRails.
#
# NEMO_GUARDRAILS_NO_USAGE_STATS + DO_NOT_TRACK disable NeMo's anonymous usage
# telemetry, which `LLMRails.__init__` otherwise fires as an outbound HTTP event
# (nemoguardrails/telemetry.py `report_usage`, called from rails/llm/llmrails.py)
# on EVERY engine construction, plus a heartbeat daemon thread. This pod guards
# legal/accounting traffic: NO unsolicited egress. Both vars are set because NeMo
# honours either (telemetry.py `_is_usage_stats_enabled`); defence in depth, the
# lifespan also calls app.bedrock_engine.disable_usage_telemetry() BEFORE the
# nemoguardrails import, so a dropped ENV cannot silently re-enable the beacon.
ENV PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    NEMOGUARDRAILS_LLM_FRAMEWORK=langchain \
    NEMO_GUARDRAILS_NO_USAGE_STATS=1 \
    DO_NOT_TRACK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Non-root runtime user with a writable $HOME (NeMo/FastEmbed expect a writable
# cache dir at $HOME for uid 1000).
RUN groupadd -r guardrail && \
    useradd -r -u 1000 -g guardrail -s /bin/bash -d /home/guardrail guardrail && \
    mkdir -p /home/guardrail && \
    chown guardrail:guardrail /home/guardrail

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
# The pod (app/) and its hashed NeMo config (config/) ship; tests/ + spike/ are
# excluded by .dockerignore. This is the ONLY copy of the package in the image
# (the venv holds third-party deps only); uvicorn resolves `app` from WORKDIR.
COPY --chown=guardrail:guardrail app /app/app
COPY --chown=guardrail:guardrail config /app/config

USER guardrail

# FastAPI entrypoint (the guard pod).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
