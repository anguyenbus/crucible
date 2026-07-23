#!/usr/bin/env bash
#
# Demonstrate the pod's refuse-to-serve guarantee THROUGH the deploy surface's
# pin source — not from a unit test.
#
# The two determinism pins are exported from the SINGLE GENERATED SOURCE
# (deploy/guardrail-pins.env) exactly the way a deploy surface injects them:
# compose reads the same bytes via `env_file:`, Kubernetes via the
# `configMapGenerator` in deploy/kustomization.yaml. Nothing is hand-copied.
#
# Three cases, run against a real uvicorn process:
#
#   A  pins enforced + shipping config/       => /readyz 200 ready
#   B  pins enforced + DELIBERATELY DRIFTED   => startup aborts (DigestMismatchError),
#      config/                                   the port never binds, /readyz never ready
#   C  pins UNSET    + the same drifted       => serves — proving the refusal in B is the
#      config/                                   PIN enforcement, not the drift itself
#
# Case C is the control: without it, "B did not come up" proves nothing.
#
# Usage:  services/guardrail/scripts/demo_refuse_to_serve.sh
# Needs:  ambient AWS credentials (the engine build in A and C is real).

set -uo pipefail

PORT="${PORT:-8099}"
READY_TIMEOUT_SECONDS="${READY_TIMEOUT_SECONDS:-90}"

SERVICE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIN_ENV="${SERVICE_DIR}/deploy/guardrail-pins.env"
WORK_DIR="$(mktemp -d)"
DRIFTED_CONFIG_DIR="${WORK_DIR}/drifted-config"

failures=0

cleanup() {
  rm -rf "${WORK_DIR}"
}
trap cleanup EXIT

digest_of() {
  (cd "${SERVICE_DIR}" && uv run --quiet python -c \
    "from app.config_digest import compute_config_dir_digest; print(compute_config_dir_digest('$1'))" \
    2>/dev/null | tail -1)
}

# Start the pod, poll /readyz until it answers 200 or the timeout expires.
# Echoes "ready" or "never-ready"; the server log is left at ${WORK_DIR}/<case>.log.
run_case() {
  local case_name="$1" config_dir="$2" enforce_pins="$3"
  local log="${WORK_DIR}/${case_name}.log"
  local pid outcome="never-ready"

  (
    cd "${SERVICE_DIR}" || exit 1
    if [ "${enforce_pins}" = "enforce" ]; then
      set -a
      # shellcheck disable=SC1090
      . "${PIN_ENV}"
      set +a
    else
      unset GUARDRAIL_EXPECTED_CONFIG_DIR_DIGEST GUARDRAIL_EXPECTED_UV_LOCK_SHA256
    fi
    GUARDRAIL_CONFIG_DIR="${config_dir}" \
      exec uv run uvicorn app.main:app --host 127.0.0.1 --port "${PORT}" >"${log}" 2>&1
  ) &
  pid=$!

  local waited=0
  while [ "${waited}" -lt "${READY_TIMEOUT_SECONDS}" ]; do
    if [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 \
            "http://127.0.0.1:${PORT}/readyz")" = "200" ]; then
      outcome="ready"
      break
    fi
    if ! kill -0 "${pid}" 2>/dev/null; then
      outcome="process-exited"
      break
    fi
    sleep 2
    waited=$((waited + 2))
  done

  kill "${pid}" 2>/dev/null
  wait "${pid}" 2>/dev/null
  echo "${outcome}"
}

expect() {
  local case_name="$1" expected="$2" observed="$3"
  if [ "${expected}" = "${observed}" ]; then
    echo "  PASS ${case_name}: ${observed}"
  else
    echo "  FAIL ${case_name}: expected ${expected}, observed ${observed}"
    failures=$((failures + 1))
  fi
}

echo "== pins, read from the single generated source (${PIN_ENV#"${SERVICE_DIR}"/})"
grep -v '^#' "${PIN_ENV}" | grep .

mkdir -p "${DRIFTED_CONFIG_DIR}"
cp -R "${SERVICE_DIR}/config/." "${DRIFTED_CONFIG_DIR}/"
printf '\n# DELIBERATE DRIFT injected by demo_refuse_to_serve.sh\n' >>"${DRIFTED_CONFIG_DIR}/config.yml"

echo
echo "== live config_dir_digest"
echo "  shipping config/: $(digest_of "${SERVICE_DIR}/config")"
echo "  drifted  config/: $(digest_of "${DRIFTED_CONFIG_DIR}")"

echo
echo "== case A — pins enforced, shipping config/ (expect: ready)"
expect "A" "ready" "$(run_case A "${SERVICE_DIR}/config" enforce)"

echo
echo "== case B — pins enforced, DRIFTED config/ (expect: process-exited, never ready)"
expect "B" "process-exited" "$(run_case B "${DRIFTED_CONFIG_DIR}" enforce)"
if grep -q "REFUSES to serve" "${WORK_DIR}/B.log" 2>/dev/null; then
  echo "  PASS B: startup aborted with the refuse-to-serve error"
  grep -m1 "REFUSES to serve" "${WORK_DIR}/B.log" | cut -c1-200
else
  echo "  FAIL B: no refuse-to-serve error in the server log"
  failures=$((failures + 1))
fi

echo
echo "== case C — pins UNSET, same DRIFTED config/ (control, expect: ready)"
expect "C" "ready" "$(run_case C "${DRIFTED_CONFIG_DIR}" skip)"

echo
if [ "${failures}" -eq 0 ]; then
  echo "RESULT: refuse-to-serve DEMONSTRATED through the deploy surface's pin source."
  exit 0
fi
echo "RESULT: ${failures} expectation(s) FAILED."
exit 1
