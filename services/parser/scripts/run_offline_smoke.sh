#!/usr/bin/env bash
# Host-side OFFLINE-STARTUP smoke test: build the parser image, then run it with
# networking DISABLED and confirm the service starts and a trivial parse succeeds
# using ONLY baked models. This is the real acceptance for the model bake.
#
# Usage:  services/parser/scripts/run_offline_smoke.sh [IMAGE_TAG]
# Exit 0 = pass, 3 = docker unavailable (honest skip), non-zero = real failure.
set -euo pipefail

IMAGE="${1:-parser-offline-smoke:latest}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Honest skip if docker cannot be used in this environment.
if ! docker info >/dev/null 2>&1; then
  echo "[offline-smoke] SKIP: docker daemon not available/usable in this environment" >&2
  exit 3
fi

echo "[offline-smoke] building image ${IMAGE} (network ON for the bake)..."
docker build -t "${IMAGE}" "${HERE}"

echo "[offline-smoke] running the baked image with --network none..."
# --network none guarantees no network at runtime; HF_HUB_OFFLINE is set in the
# image. If any required model were not baked, the parse would fail here.
docker run --rm --network none \
  -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1 \
  "${IMAGE}" python scripts/offline_smoke.py

echo "[offline-smoke] PASS: service started and parsed offline with baked models only"
