# Dockerfile for the orchestrator service (the app/ deliverable).
#
# Build context is services/orchestrator/ (mirrors the monorepo path
# genai-backend/services/orchestrator/). .dockerignore keeps tests/, scripts/,
# .venv and caches out of the image, so ONLY app/ ships — carrying the packaged
# app/contracts/ schema mirror and app/configs/ pinned YAML + hash manifest
# that config/schema resolution loads via importlib.resources (no bind mounts).
# NO docker-compose entry / ORCHESTRATOR_URL wiring until Phase 2: local
# uvicorn suffices for eval adapter development.

# ============================================================================
# BUILDER STAGE - resolve third-party deps into a venv
# ============================================================================
FROM python:3.12-slim AS builder

# UV_PROJECT_ENVIRONMENT builds the venv at its FINAL /opt/venv path (rather
# than `mv .venv /opt/venv`) so console-script shebangs like uvicorn's point at
# a python that still exists in the runtime stage.
ENV UV_COMPILE_BYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv

# uv package manager
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /build

# Dependency layer: ONLY the manifests are copied, and --no-install-project
# skips building/installing the orchestrator package itself — so this layer is
# cache-stable across app/ code changes (deps re-resolve only when
# pyproject.toml/uv.lock change). The runtime COPY of app/ below is the SOLE
# source of the package: WORKDIR /app puts it on the import path.
# Phase 1 declares no extras: no boto3 / opensearch-py / opentelemetry until
# Phase 2.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# ============================================================================
# RUNTIME STAGE - minimal final image
# ============================================================================
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Non-root runtime user with a writable $HOME (libraries and tooling expect
# $HOME to be writable at uid 1000, e.g. for cache dirs).
RUN groupadd -r orchestrator && \
    useradd -r -u 1000 -g orchestrator -s /bin/bash -d /home/orchestrator orchestrator && \
    mkdir -p /home/orchestrator && \
    chown orchestrator:orchestrator /home/orchestrator

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
# Only app/ ships (tests/ + scripts/ are excluded by .dockerignore). This is
# the ONLY copy of the package in the image (the venv holds third-party deps
# only); uvicorn's default --app-dir "." resolves `app` from WORKDIR /app.
COPY --chown=orchestrator:orchestrator app /app/app

USER orchestrator

# FastAPI entrypoint (Phase 1: canned /query with real config resolution).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
