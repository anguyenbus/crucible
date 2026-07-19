# Parser service image. Bakes docling (layout + table) + rapidocr OCR models at
# BUILD time so the runtime can serve with networking disabled (offline bake).
#
# Python 3.11-slim matches the reference's proven wheel resolution (docling +
# torch + rapidocr) — deliberately NOT monorepo-3.12. Build artifact only; runtime
# config comes from PARSER_* env + the standard AWS credential chain (see README).
#
# Verify the bake with: services/parser/scripts/run_offline_smoke.sh
FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# System libs docling/rapidocr (opencv/onnxruntime) need at import + run time.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /service
ENV PYTHONPATH=/service

# Install pinned deps (docling==2.96.0 exact; rapidocr via docling[standard]).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# App boundary + vendored package + the bake/smoke scripts and fixture.
COPY app ./app
COPY parser_service ./parser_service
COPY scripts ./scripts
COPY tests/fixtures ./tests/fixtures

# Bake models into the image (network ON here). Fails the build if a real parse
# of the fixture cannot be produced from the downloaded models.
RUN uv run --no-sync python scripts/bake_models.py

# Runtime is fully offline: no HuggingFace / model network calls when serving.
ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    PARSER_ESCALATION_ENGINE=textract

EXPOSE 8000
CMD ["uv", "run", "--no-sync", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
