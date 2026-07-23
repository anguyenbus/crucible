# Guardrail pod (`services/guardrail/`)

An out-of-process **NeMo Guardrails** service that owns every guard **verdict**
for the RAG orchestrator. It is a small FastAPI app that wraps one reusable
`LLMRails` engine (Bedrock-backed) plus a deterministic secrets/PII detector, and
answers two questions over HTTP:

- **`POST /check/input`** — is this user turn a jailbreak / prompt-injection?
- **`POST /check/output`** — does this generated answer leak a secret / PII,
  violate policy, or make an ungrounded claim?

It is **verdict-only**: it returns a decision (`unsafe` / `flag` + attribution),
never a rewritten answer. The orchestrator calls it through an injected
`typing.Protocol` client, so the orchestrator's `stages-pure` import boundary and
lean image are untouched — no NeMo/LangChain types cross the wire.

This pod is the single home for guarding in the **`legal-rag-default-1.8.0`
("nemo-all")** config — the consolidation target that retires the old split-brain
(some guarding in the orchestrator, some in the pod). See
[`docs/guardrail-consolidation-plan.md`](../../docs/guardrail-consolidation-plan.md).

---

## Quick start

```bash
# From services/guardrail/ — pod on :8080 (needs AWS creds in the ambient boto3 chain)
uv run uvicorn app.main:app --port 8080

# Or, with the two-hash determinism digests ENFORCED (recommended for the demo):
cd ../orchestrator/demo_ui && make guardrail-pod          # enforces GUARDRAIL_EXPECTED_*
cd ../orchestrator/demo_ui && make guardrail-pod-dev      # escape hatch: digests UNset, no enforcement

# Regenerate the two determinism hashes after any config/ or dependency change:
uv run python -m app.config_digest
```

Endpoints: `POST /check/input`, `POST /check/output`, `GET /healthz` (liveness),
`GET /readyz` (readiness). Model: **Bedrock Claude Haiku 4.5** via the
`bedrock_converse` engine, `ap-southeast-2`, temperature 0.

---

## Deploy surfaces — where the determinism pins come from

`GUARDRAIL_EXPECTED_CONFIG_DIR_DIGEST` and `GUARDRAIL_EXPECTED_UV_LOCK_SHA256` are
what make "the pod refuses to serve on drift" real. They have **one** source —
`deploy/guardrail-pins.env`, generated, never hand-edited and never hand-copied:

```bash
# Regenerate after ANY config/ edit (and commit the result with the config change)
uv run python -m app.config_digest --write-env
```

| Surface | How it reads the pins |
|---|---|
| Docker Compose (dev-only) | `env_file: ./services/guardrail/deploy/guardrail-pins.env` on the repo-root `guardrail` service |
| Kubernetes | `deploy/kustomization.yaml` → `configMapGenerator` over the **same file** → `envFrom` in `deploy/k8s/deployment.yaml` (equivalently `kubectl create configmap guardrail-pins --from-env-file=deploy/guardrail-pins.env`) |
| Demo Makefile | `services/orchestrator/demo_ui/Makefile` reads the values out of the same file |

A pin hand-copied into a compose file dies at the pod boundary the moment compose
stops being the deploy surface, so the value must never appear anywhere but the
generated file — asserted by `tests/test_deploy_surface_pins.py`. Neither surface
carries an AWS credential: the pod uses the ambient chain (mounted `~/.aws` +
`AWS_PROFILE` in compose, IRSA / node role in K8s).

Prove the guarantee end-to-end against a real process — shipping config serves,
drifted config refuses, drift with the pins unset still serves (the control):

```bash
scripts/demo_refuse_to_serve.sh
```

---

## File tree

```
services/guardrail/
├── app/
│   ├── main.py              # FastAPI entry: lifespan builds the ONE LLMRails + compiles the detector; all error mapping lives here
│   ├── settings.py          # Env-resolved settings (no I/O at import); model id + the expected determinism hashes
│   ├── contract.py          # HTTP contract — request shapes + the PLAIN-DATA CheckResponse (no NeMo types on the wire)
│   ├── bedrock_engine.py    # NeMo LLM-framework shim: force the `langchain` framework so Bedrock is reachable (I1)
│   ├── detectors.py         # Pure-regex secrets/PII detector (+ Luhn) — the FIRST output rail, no LLM call
│   ├── nemo_runtime.py      # Maps LLMRails.generate → CheckResponse; applies the per-rail fail policy; runs the detector first
│   ├── config_digest.py     # Computes the two determinism hashes (config_dir_digest + uv_lock_sha256); `python -m app.config_digest`
│   └── routers/
│       ├── check.py         # POST /check/input, POST /check/output
│       └── health.py        # GET /healthz, GET /readyz (readiness fails if the detector didn't compile)
├── config/                  # The NeMo config/ directory — its digest is pinned into the orchestrator's 1.8.0 config
│   ├── config.yml           # Models + rails flow ordering (input: self check input; output: deterministic → policy → facts)
│   ├── prompts.yml          # Self-check rail prompts
│   ├── detectors.yml        # Ported deterministic secrets/PII pattern tables (severity: high→block, low→flag; validator: luhn)
│   ├── actions.py           # Registers `deterministic_output_scan` as a first-class NeMo action (auto-loaded by NeMo)
│   ├── rails/
│   │   └── deterministic_output.co   # Colang flow that invokes the deterministic action as an output rail
│   └── .railsignore         # Tells NeMo to skip detectors.yml (it's a hashed DATA file, not a rails artifact)
├── deploy/                  # The deploy surfaces that POPULATE the determinism pins
│   ├── guardrail-pins.env   # GENERATED single source of both pins — read by compose `env_file:` AND by kustomize
│   ├── kustomization.yaml   # K8s: configMapGenerator turns those same bytes into the `guardrail-pins` ConfigMap
│   └── k8s/                 # Deployment (envFrom the pins ConfigMap, /healthz + /readyz probes), Service, ServiceAccount
├── scripts/
│   ├── demo_nemo_capability.py       # Capability walk-through: fires the block/grounding cases at a running pod
│   └── demo_refuse_to_serve.sh       # Proves refuse-on-drift against a real process using the deploy surface's pins
├── tests/
│   ├── test_deterministic_output_rail.py   # Detector: block/short-circuit/Luhn/flag/attribution, /readyz fail-fast
│   ├── test_gate_parity_and_mode_a.py      # Phase-2 deterministic parity + Mode A (LLM-rail failure → detector still blocks)
│   ├── test_bedrock_smoke.py               # @requires_aws live smoke — real Haiku verdicts end-to-end
│   ├── test_config_digest.py               # Digest computation is stable + folds detectors.yml
│   ├── test_digest_enforcement.py          # Startup refuses to serve on a config/ or uv.lock drift
│   ├── test_deploy_surface_pins.py         # Compose + K8s carry both pins from the ONE generated source; no credentials in config
│   ├── test_no_openai_guard.py             # I1: `openai` is absent from the resolved environment
│   ├── test_rails.py / test_skeleton.py    # Contract + engine wiring
│   ├── conftest.py / helpers.py            # The substitution seam: pre-install mock rails/settings/detectors — tests never reach AWS
│   └── fixtures/                           # nemo_config/ (digest tests) + parity/detector_answers.jsonl
├── Dockerfile               # Two-stage; runtime ENV NEMOGUARDRAILS_LLM_FRAMEWORK=langchain; CMD uvicorn :8080
├── pyproject.toml           # Deps: nemoguardrails + langchain-aws (NO `[server]` extra — it pulls openai)
└── uv.lock                  # The resolved dependency artifact whose sha256 is the second determinism hash
```

---

## The contract (plain data only)

`CheckResponse` maps 1:1 onto the orchestrator's existing classifier-verdict
shape, so the pure stage consumes it with no new imports:

| Field | Meaning |
|---|---|
| `unsafe` | `true` ⇒ BLOCK. The orchestrator turns this into a **200 canned refusal**, never a 5xx. |
| `flag` | `true` ⇒ advisory (deliver + note). On the output lane this is also the **fail-OPEN** signal (pod couldn't reach Bedrock) — not a block. |
| `rationale` | Terse token naming the rail that fired (never NeMo's own message text). |
| `model_id` | The Haiku id, **stamped by the pod** (NeMo reports `"unknown"`). |
| `input_tokens` / `output_tokens` | Real token accounting from `generate(... log llm_calls)`. |
| `detections` | `[{category, label, count}]` — labels + counts only (**no offsets**, because nothing is redacted). Scoreable data for the parity gate. |

The pod **never returns a modified answer** — see design point 2.

## The rails pipeline

```
/check/input   →  [ self check input ]                                      → verdict
/check/output  →  [ deterministic secrets/PII ]  →  [ self check output ]   →  [ self check facts ] → verdict
                   (pure regex, runs FIRST)          (policy, LLM)             (grounding, LLM)
```

On `/check/output` the deterministic rail runs **first** and **short-circuits**: a
secrets or high-severity-PII hit blocks the answer **without** paying for the
`self check output` / `self check facts` LLM calls. Low-severity PII (email/phone)
is flagged in the same pass and delivered, and the LLM rails still run.

---

## Design

**1. A separate out-of-process pod, not code inside the orchestrator.**
NeMo Guardrails drags in LangChain + `langchain-aws`; hosting it in the
orchestrator would bloat the image and violate the `stages-pure` import boundary
that keeps pipeline stages free of infra imports. Isolating it behind an injected
`Protocol` client keeps the orchestrator lean and pure, and lines the pod up for
the independent-K8s-pod future (its own `/readyz`, its own creds) — compose is
dev-only, not the only wiring.

**2. Verdict-only — the pod is a validator, not a transformer.**
`/check/output` returns a decision, never rewritten answer text. This keeps the
pod's blast radius small (a pod bug can't corrupt or un-redact delivered content)
and its contract plain-data. The direct consequence: **high-severity PII is
blocked, not redacted** — masking a span in place would make the pod a content
transformer. That capability trade (a legal answer citing an SSN is refused rather
than delivered-with-the-number-masked) was accepted deliberately; it is rare in
legal text and a refusal is a safe failure.

**3. Deterministic detection lives in the pod, but as a real NeMo rail — and it
stays deterministic.** An LLM self-check cannot replicate two things: **in-place
determinism** (`AKIA[0-9A-Z]{16}` either matches or it doesn't — free, 100%
precise, reproducible) and a guaranteed verdict. So the ported secrets/PII tables
run as a **registered NeMo action** (`config/actions.py` → a Colang flow), ordered
first in the output rails. This gives three things at once:
- the whole guard surface is expressed as **NeMo rails config** (the consolidation
  is real, not "NeMo plus a side function"), and it is captured by the config
  digest like any rail;
- detection is **free and exact** — pure `re`, no model call, no FP/FN risk; and
- because it doesn't touch the LLM, it **survives an LLM-rail failure** (see
  point 6).
Detection behavior is byte-for-byte the orchestrator's in-house tables — porting
behavior, not "improving" it, so the Phase-2 parity gate can assert equivalence.

**4. Two-hash config determinism (I4).** "Same `config_sha256` ⇒ same guard
behavior" must survive the two-service split. A bare digest of the NeMo `config/`
directory would miss NeMo/LangChain/`langchain-aws` **resolution** drift, so the
pin carries **two** hashes, folded into the orchestrator's active config YAML:
- **`config_dir_digest`** — a stable digest of `config.yml` + `prompts.yml` +
  `detectors.yml` + `rails/*.co`. Folding `detectors.yml` into this **same** digest
  (no new pin hash) means a regex change becomes a pod `config_version` bump + a
  fresh orchestrator `config_sha256` — good for reproducibility.
- **`uv_lock_sha256`** — the sha256 of the resolved `uv.lock`, pinning the
  transitive dependency graph.
At startup the pod recomputes both live and **refuses to serve on any mismatch**
(`GUARDRAIL_EXPECTED_*`), so a drifted image cannot silently answer under a stale
pin.

**5. Bedrock-only, and the framework gotcha (I1).** No OpenAI, no NVIDIA NIM.
`nemoguardrails==0.23.0` splits its LLM path into two frameworks: the global
default is OpenAI-compatible and **raises** for `bedrock` — only the `langchain`
framework reaches Bedrock, and it is chosen **globally** (there is no per-model
config field). Defense in depth: the Dockerfile sets
`NEMOGUARDRAILS_LLM_FRAMEWORK=langchain` (read at import) **and**
`bedrock_engine.force_langchain_framework()` calls `set_default_framework("langchain")`
in the lifespan before `LLMRails` is built — order-independent, so a missing env
can never silently drop the pod onto the OpenAI path. The `pyproject.toml`
deliberately omits the `nemoguardrails[server]` extra (it pulls `openai`); a test
asserts `openai` is absent.

**6. Per-failure-mode fail policy (resilience).** Because one service now guards
everything, its failure behavior is designed per-mode, not flat:
- **LLM/Bedrock rail fails, pod up** (throttle / error) — the deterministic
  secrets/high-PII rail **still blocks** (pure regex, no model); `self check
  output` / `self check facts` **fail OPEN + flag**. Secrets/PII protection
  survives the common failure mode.
- **Pod unreachable** (the orchestrator's concern) — input fails **safe/block**,
  output fails **open + advisory flag**, and the fail-open window is made **loud**
  (a distinct `nemo-output-fail-open-v1` rule id), never silent.
A guard block is always an **honest 200 verdict**, never a 5xx — the fail policy
lives in `nemo_runtime`, with the app-level handlers a backstop for any error
outside the guarded `generate` call.

**7. Construct-once + fail-fast readiness.** The one `LLMRails` engine and the
compiled detector are built **once** in the lifespan and reused per request (no
per-request rebuild), mirroring the orchestrator's discipline. `/readyz` fails
(503) if the detector didn't compile — a broken pattern must never silently no-op.
Readiness deliberately makes **no paid Bedrock probe**; invoke permission is
proven by the first real `/check` call. Importing any module performs **no I/O**,
and tests pre-install mock `rails`/`settings`/`detectors` on `app.state` (the
substitution seam), so the suite never reaches AWS.

**8. The pod never trusts NeMo for identity or messages.** The `model_id` is
stamped by the pod from its configured Haiku id (NeMo returns `"unknown"`), and a
block surfaces only as a rail **exception turn** (`enable_rails_exceptions`) whose
`type` becomes a terse rationale — NeMo's own generated message text is never
forwarded into the answer.

---

## Testing

```bash
uv run pytest                        # full suite; live `requires_aws` tests auto-SKIP when no creds resolve
uv run pytest -m "not requires_aws"  # deterministic-only (explicit) — the CI subset
uv run pytest -m requires_aws        # live Bedrock smoke — real Haiku verdicts (needs ap-southeast-2 creds)
```

The deterministic rail, both failure modes, and the digest enforcement are all
CI-asserted without AWS (the detector is pure regex; the fail policies are mapped
in-process). Only the LLM-verdict smoke needs a live Bedrock rail. The Phase-2
**parity gate** (in-house `1.4.0` vs nemo-all `1.8.0`) that authorizes cutover
lives on the eval side: `services/eval/scripts/guardrail_parity_gate.py`.
