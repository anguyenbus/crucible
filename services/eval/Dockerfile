# Dockerfile for the eval service (the app/ deliverable).
#
# Build context is services/eval/ (mirrors the monorepo path
# genai-backend/services/eval/). .dockerignore keeps dev/, tests/, .venv and
# caches out of the image, so ONLY app/ ships. CMD runs the FastAPI control
# plane; the worker runs as a batch via `python -m app.worker`.

# ============================================================================
# BUILDER STAGE - resolve deps into a venv
# ============================================================================
FROM python:3.12-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    PYTHONUNBUFFERED=1

# uv package manager
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /build

# Resolve against the committed lockfile (deps + uv_build packaging of
# app/contracts/*.json). The bedrock + phoenix extras are installed so the
# judge + observability paths resolve.
COPY pyproject.toml uv.lock ./
COPY app ./app
RUN uv sync --frozen --no-dev --extra bedrock --extra phoenix
RUN mv .venv /opt/venv

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

# Non-root runtime user.
RUN groupadd -r eval && \
    useradd -r -u 1000 -g eval -s /bin/bash -d /home/eval eval && \
    mkdir -p /home/eval

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
# Only app/ ships (dev/ + tests/ are excluded by .dockerignore).
COPY --chown=eval:eval app /app/app

USER eval

# Control-plane entrypoint (the worker runs as a batch: python -m app.worker).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
