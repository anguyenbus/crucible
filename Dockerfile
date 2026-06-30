# Multi-stage Dockerfile for eval
# Minimal image with baked-in datasets (Legal RAG Bench)

# ============================================================================
# BUILDER STAGE - Install dependencies
# ============================================================================
FROM python:3.12-slim AS builder

# Build-time environment
ENV UV_COMPILE_BYTECODE=1 \
    PYTHONOPTIMIZE=2 \
    PYTHONUNBUFFERED=1

# Install uv package manager
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Set working directory
WORKDIR /build

# Copy dependency files
COPY pyproject.toml uv.lock ./
COPY README.md ./
COPY src ./src

# Install dependencies
RUN uv sync --frozen --no-dev
# Move venv to /opt/venv for consistent copying
RUN mv .venv /opt/venv

# ============================================================================
# DATASETS STAGE - Download datasets
# ============================================================================
FROM python:3.12-slim AS datasets

WORKDIR /opt/eval

# Copy uv binary from builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy venv from builder
COPY --from=builder /opt/venv /opt/venv

# Copy source and scripts
COPY --chown=root:root src /opt/eval/src
COPY --chown=root:root scripts /opt/eval/scripts

# Download Legal RAG Bench corpus
WORKDIR /opt/eval
ENV PYTHONPATH=/opt/eval/src
RUN /opt/venv/bin/python scripts/prepare_legal_rag_bench_corpus.py \
    --cache-dir /opt/eval/data/rag/legal_rag_bench \
    --output-dir /opt/eval/data/rag/legal_rag_bench

# Verify datasets
RUN test -d /opt/eval/data/rag/legal_rag_bench || \
    (echo "ERROR: Dataset download failed" && exit 1)

# ============================================================================
# RUNTIME STAGE - Minimal final image
# ============================================================================
FROM python:3.12-slim AS runtime

# Runtime environment
ENV PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH="/opt/eval/src" \
    EVAL_LOG_LEVEL=INFO

# Install runtime dependencies only (no build tools)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Create non-root user
RUN groupadd -r eval && \
    useradd -r -u 1000 -g eval -s /bin/bash -d /home/eval eval && \
    mkdir -p /home/eval

# Create directories
RUN mkdir -p /opt/eval /work/results

# Copy venv from builder
COPY --from=builder /opt/venv /opt/venv
# Fix entrypoint scripts: regenerate with correct python path
RUN /opt/venv/bin/python -m sysconfig && \
    for script in /opt/venv/bin/*; do \
        if head -1 "$script" | grep -q "^#!/build/.venv/bin/python"; then \
            sed -i '1s|#!/build/.venv/bin/python|#!/opt/venv/bin/python|' "$script"; \
        fi; \
    done

# Copy baked datasets from datasets stage
COPY --from=datasets --chown=eval:eval /opt/eval/data /opt/eval/data

# Copy source code
COPY --chown=eval:eval src /opt/eval/src
COPY --chown=eval:eval scripts /opt/eval/scripts
COPY --chown=eval:eval contracts /opt/eval/contracts
COPY --chown=eval:eval eval_config.yaml /opt/eval/eval_config.yaml

# Set ownership
RUN chown -R eval:eval /opt/eval /work

WORKDIR /opt/eval
USER eval

# Default entry point
ENTRYPOINT ["eval"]
CMD ["--help"]
