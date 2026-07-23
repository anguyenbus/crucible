# Guardrail pod (`services/guardrail/`)

An out-of-process **NeMo Guardrails** service that owns **every** guard verdict
for the RAG orchestrator. It is a small FastAPI app wrapping one reusable
`LLMRails` engine (Bedrock Haiku) plus a pure-regex secrets detector, and answers
two questions over HTTP:

- **`POST /check/input`** — is this user turn a jailbreak / prompt-injection?
- **`POST /check/output`** — does this generated answer leak a credential,
  violate policy, or make an ungrounded claim?

It is **verdict-only**: it returns a decision (`unsafe` / `flag` + attribution),
never rewritten answer text. The orchestrator calls it through an injected
`typing.Protocol` client, so no NeMo/LangChain type crosses the wire and the
orchestrator's `stages-pure` import boundary is untouched.

**The in-house guard is gone.** There is no second implementation to compare
against, no split-brain, no rollback lane. The pod is the only guard, reached
only by `legal-rag-default-1.8.0` — see [Current state](#current-state-read-before-you-rely-on-this).

Pinned to **`nemoguardrails==0.23.0`** (`uv.lock`). Every upstream claim below is
cited by `path:line` against the NeMo Guardrails source so it can be re-checked
rather than trusted. Paths are relative to the installed package —
`.venv/lib/python3.13/site-packages/nemoguardrails/`.

---

## Quick start

```bash
# From services/guardrail/ — pod on :8080 (AWS creds come from the ambient boto3 chain)
uv run uvicorn app.main:app --port 8080

# With the two determinism digests ENFORCED (what the demo runs):
cd ../orchestrator/demo_ui && make guardrail-pod          # enforces GUARDRAIL_EXPECTED_*
cd ../orchestrator/demo_ui && make guardrail-pod-dev      # escape hatch: digests unset, no enforcement

# Regenerate both hashes after ANY config/ or dependency change, and commit the result:
uv run python -m app.config_digest --write-env
```

Endpoints: `POST /check/input`, `POST /check/output`, `GET /healthz` (liveness),
`GET /readyz` (readiness). Model: **Bedrock Claude Haiku 4.5**
(`au.anthropic.claude-haiku-4-5-20251001-v1:0`) via the `bedrock_converse`
engine, `ap-southeast-2`, temperature 0.

---

## The contract (plain data only)

| Field | Meaning |
|---|---|
| `unsafe` | `true` ⇒ BLOCK. The orchestrator turns this into a **200 canned refusal**, never a 5xx. |
| `flag` | `true` ⇒ advisory (deliver + note). On the output lane this is also the **fail-OPEN** signal — not a block. |
| `rationale` | Terse token naming the rail that fired (never NeMo's own message text). |
| `model_id` | The Haiku id, **stamped by the pod** — NeMo's own reported id is unreliable. |
| `input_tokens` / `output_tokens` | Real accounting from `generate(options={"log": {"llm_calls": True}})`. |
| `detections` | `[{category, label, count}]` — labels + counts, **no offsets** (nothing is redacted, so offsets would be dead weight). |

`detections` can currently only carry `category: "secrets"`. The field shape is
frozen and did not change when the PII table was withdrawn.

---

## The rails pipeline

```
/check/input   →  [ self check input ]                                        → verdict
/check/output  →  [ self check deterministic output ] → [ self check output ] → [ self check facts ] → verdict
                    (pure regex, runs FIRST, no LLM)     (policy, LLM)           (grounding, LLM, opt-in)
```

On `/check/output` the deterministic rail runs **first** and **short-circuits**:
a secrets hit blocks the answer without paying for either LLM rail. `self check
facts` runs only when the request sets `check_facts` — it is off by default and
gated two independent ways (see below).

### What the output rail actually judges

`self_check_output` is framed for **a compliance case officer investigating a
subject entity** — a company or client suspected of not declaring assets, income
or profits. It is not framed for a taxpayer seeking advice. The block clause
targets the three ways an answer can harm that reader:

1. stating evasion / fraud / concealment as **established fact** where the
   material supports only assessed risk;
2. **guaranteeing** an enforcement, penalty or litigation outcome;
3. giving **binding legal direction in place of the agency's own counsel**, or
   telling the officer to skip a required review, authorisation or referral.

The allow-list is the load-bearing half. Identifying, quantifying and flagging
suspected non-compliance *is the product's core function*, so the prompt states
explicitly that naming a suspected breach, citing the provision, quoting
shortfalls and penalty amounts, describing a declared-vs-third-party
discrepancy, and recommending escalation **must never be blocked**. Hedged
findings ("appears", "indicates", "is consistent with") are allowed. An
over-blocking policy rail here does not degrade this product — it deletes it.

Regression cover: [`tests/test_advice_boundary_prompt.py`](tests/test_advice_boundary_prompt.py).

---

## How NeMo Guardrails actually behaves here

Read out of the library source. These are the mechanisms the pod
depends on; each one is a place where a plausible assumption is wrong.

### 1. Bedrock is reachable through exactly one framework

`nemoguardrails` 0.23.0 splits its LLM path into two frameworks. The global
default is OpenAI-compatible and **raises** for any provider without a known
base URL — the error message literally tells you to switch
(`llm/frameworks/default.py:50-55`).
Only the `langchain` framework reaches Bedrock.

The selection is **global, not per-model** — there is no `config.yml` field for
it. The registry reads it from the environment **at import time**
(`llm/frameworks/registry.py:27`)
and `set_default_framework()` mutates that global afterwards
(`registry.py:80-85`).

The pod therefore applies **both**, so import order cannot matter: the Dockerfile
sets `NEMOGUARDRAILS_LLM_FRAMEWORK=langchain`, **and**
`bedrock_engine.force_langchain_framework()` calls `set_default_framework` in the
lifespan before `LLMRails` is built. It then **asserts the result** — if the
constructed inner model is not `ChatBedrockConverse`, `build_rails` raises rather
than serve on the wrong backend.

`pyproject.toml` deliberately omits the `nemoguardrails[server]` extra (it pulls
`openai`); [`tests/test_no_openai_guard.py`](tests/test_no_openai_guard.py)
asserts `openai` is absent from the resolved environment.

### 2. The verdict parser reads two words, and its fallback is not symmetric

All three rails force the built-in `is_content_safe` parser
(`output_check/actions.py:93`).
That parser:

- strips non-word characters and looks at **only the first two words** of the
  response (`llm/output_parsers.py:125`);
- matches `safe` / `unsafe` / `yes` / `no` — where **`yes` means block**;
- and on **anything it can't parse, returns `[False]`**
  (`output_parsers.py:138`).

That fallback lands differently per rail, and the asymmetry is deliberate
upstream but easy to misread:

| Rail | Reads `result[0]` as | Unparseable response ⇒ |
|---|---|---|
| `self_check_input` | `is_safe` (`input_check/actions.py:89`) | **BLOCK** (fail closed) |
| `self_check_output` | `is_safe` (`output_check/actions.py:95`) | **BLOCK** (fail closed) |
| `self_check_facts` | `is_not_safe`, then `float(not …)` (`facts/actions.py:91-93`) | **ALLOW** — scores `1.0`, "grounded" (fail open) |

This is why the prompts in `config/prompts.yml` end with an explicit
`Question: … (Yes or No)? / Answer:` and pin `max_tokens: 4` — a preamble like
"Based on the answer…" would consume the two-word window and be parsed as a
block on the policy lane and a pass on the grounding lane.

### 3. `max_tokens: 4` is a cost pin that assumes a non-reasoning model

Upstream logs a warning and takes an explicit fail-safe branch when the model
returns empty content with `finish_reason="length"`
(`actions/llm/utils.py:399-420`):
input/output block, and `self_check_facts` returns `0.0` rather than let its
inverting parser accept the silence. Upstream's guidance is to raise `max_tokens`
to ~2048 for reasoning models (`docs/configure-rails/guardrail-catalog/self-check.mdx:21-33`);
absent the field it defaults to 1024.

Haiku 4.5 emits no reasoning trace here, so 4 tokens is enough for `Yes`/`No` and
keeps the guard call cheap. **Swapping in a reasoning model without raising
`max_tokens` would make every guard call truncate** — silently blocking on the
policy lane. Treat the model id and this number as a pair.

### 4. `lowest_temperature`, not the model temperature, governs the guard call

The self-check actions pass `llm_params={"temperature": config.lowest_temperature}`
on every call (`facts/actions.py:77`,
`output_check/actions.py:79`),
overriding the model block. NeMo's default is
`0.001`, so `config.yml` pins `lowest_temperature: 0.0` explicitly — otherwise
the `temperature: 0` under `models:` would be quietly ignored for exactly the
calls whose determinism we care about.

### 5. A block is an exception turn, and its message is never forwarded

With `enable_rails_exceptions: true`
(`rails/llm/config.py:1818`)
the library flows emit an event whose type ends in `Exception` instead of a
canned refusal string
(`self_check/input_check/flows.v1.co`).
`LLMRails` surfaces it as a turn with `role: "exception"`
(`rails/llm/llmrails.py:1113`),
carrying `content.type` (`InputRailException` / `OutputRailException` /
`FactCheckRailException`) and a human-readable `message`.

`nemo_runtime._extract_block` reads **only `content.type`** as the rationale.
NeMo's own prose ("I'm sorry, I can't respond to that.") never reaches the
orchestrator, let alone a user. Without this setting the block would arrive as a
*successful* generation whose text happens to be a refusal — indistinguishable
from a real answer.

### 6. Which output rails run is chosen per request

`GenerationOptions.rails.input` / `.output` accept **either a bool or a list of
flow names** (`rails/llm/options.py:130-139`).
`config.yml` declares all three output flows statically; `nemo_runtime.check_output`
sends the list that should actually run. An unlisted flow makes **zero** LLM calls.

The input lane exploits the same seam in reverse: `/check/input` passes
`{"input": True, "output": False, "dialog": False, "retrieval": False}`, so a
guard check never pays for a wasted main generation.

### 7. The facts rail is off three ways, and no-ops silently on empty evidence

`self check facts` is a **subflow** guarded by `$check_facts`, which it resets to
`False` immediately (`facts/flows.v1.co`) —
so it must be re-set per request. The pod sets it only when the caller asks, and
also omits the flow from the per-request list. Its threshold is fixed upstream at
`accuracy < 0.5`.

Third gate, and the subtle one: the action reads evidence from the
`relevant_chunks` context variable and **returns `True` (grounded) immediately if
it is empty**, before any LLM call
(`facts/actions.py:55-58`).
A `/check/output` call with `check_facts: true` and `chunks: []` returns *pass*
without checking anything. That is not a bug in the pod, but it means a green
grounding verdict is only meaningful when chunks were actually supplied.

### 8. `.railsignore` is load-bearing, not decoration

`RailsConfig.from_path` walks the config directory and `yaml.safe_load`s **every**
`.yml`/`.yaml` it finds, merging them all into one raw config
(`rails/llm/config.py:1551-1589`).
Without `config/.railsignore`, `detectors.yml`'s `secrets:` key would be merged
into the `RailsConfig` as if it were rails configuration. The ignore file is
resolved by walking **upward** from the config path to the filesystem root
(`utils.py:301-323`),
so the one inside `config/` wins.

Likewise `config/actions.py` is auto-discovered: the dispatcher scans for a
directory named `actions` or a file named `actions.py` and loads it
(`actions/action_dispatcher.py:83-85`).
That is what makes `deterministic_output_scan` a first-class registered action
rather than a side function.

> **Known gap.** Both files are load-bearing but **neither is in the config
> digest** — `_digest_member_paths` hashes `config.yml`, `prompts.yml`,
> `detectors.yml` and `rails/*.co` only. Deleting `.railsignore` or editing
> `actions.py` changes guard behaviour without changing the pin. Their presence
> is covered by tests, not by the hash.

---

## Determinism pins and deploy surfaces

"Same `config_sha256` ⇒ same guard behaviour" has to survive the two-service
split, so the pin carries **two** hashes, both folded into the orchestrator's
`legal-rag-default-1.8.0.yaml`:

- **`config_dir_digest`** — sha256 over `config.yml` + `prompts.yml` +
  `detectors.yml` + sorted `rails/*.co`, each framed by its POSIX relative path
  and NUL separators (so a rename is tamper-evident and enumeration order is
  irrelevant). Folding `detectors.yml` into the *same* digest means a regex edit
  becomes a pod `config_version` bump plus a fresh orchestrator `config_sha256`
  — one mechanism, no second pin.
- **`uv_lock_sha256`** — sha256 of the resolved `uv.lock`, pinning the transitive
  NeMo/LangChain graph. A bare config digest would miss resolution drift.

At startup the pod recomputes both and **refuses to serve on any mismatch**
(`verify_pinned_digests`, before the engine is built and before any AWS call), so
a drifted image cannot answer under a stale pin.

Both values have exactly **one** source — `deploy/guardrail-pins.env`, generated,
never hand-edited, never hand-copied:

| Surface | How it reads the pins |
|---|---|
| Docker Compose (dev-only) | `env_file: ./services/guardrail/deploy/guardrail-pins.env` |
| Kubernetes | `deploy/kustomization.yaml` → `configMapGenerator` over the **same file** → `envFrom` in `deploy/k8s/deployment.yaml` |
| Demo Makefile | `services/orchestrator/demo_ui/Makefile` reads the values out of the same file |

A pin hand-copied into a compose file dies the moment compose stops being the
deploy surface, so the value must appear nowhere else —
[`tests/test_deploy_surface_pins.py`](tests/test_deploy_surface_pins.py) asserts
this. Neither surface carries an AWS credential: the pod uses the ambient chain
(mounted `~/.aws` + `AWS_PROFILE` in compose, IRSA / node role in K8s).

Prove it end-to-end against a real process — shipping config serves, drifted
config refuses, drift with the pins unset still serves (the control):

```bash
scripts/demo_refuse_to_serve.sh
```

---

## File tree

```
services/guardrail/
├── app/
│   ├── main.py              # FastAPI entry: lifespan verifies digests → compiles detector → builds the ONE LLMRails; all error mapping lives here
│   ├── settings.py          # Env-resolved settings (no I/O at import); model id, config/lock paths, expected digests
│   ├── contract.py          # HTTP contract — request shapes + the PLAIN-DATA CheckResponse (no NeMo types on the wire)
│   ├── bedrock_engine.py    # Framework shim: force `langchain` before construction, then ASSERT ChatBedrockConverse
│   ├── detectors.py         # Pure-regex secrets detector (+ retained blocks/validator seam) — the FIRST output rail, no LLM call
│   ├── nemo_runtime.py      # Maps LLMRails.generate → CheckResponse; per-rail fail policy; runs the detector first
│   ├── config_digest.py     # Computes both hashes; `python -m app.config_digest [--write-env]`
│   └── routers/
│       ├── check.py         # POST /check/input, POST /check/output
│       └── health.py        # GET /healthz, GET /readyz (readiness fails if the detector didn't compile)
├── config/                  # The NeMo config directory — its digest is pinned into the orchestrator's 1.8.0 config
│   ├── config.yml           # Models, lowest_temperature, rail flow ordering, enable_rails_exceptions
│   ├── prompts.yml          # The three self-check prompts (output = case-officer framing)
│   ├── detectors.yml        # Secrets pattern table; the `pii:` table is WITHDRAWN (see below)
│   ├── actions.py           # Registers `deterministic_output_scan` as a NeMo action (auto-loaded)  ← not hashed
│   ├── rails/
│   │   └── deterministic_output.co   # Colang flow invoking that action as the first output rail
│   └── .railsignore         # Stops NeMo merging detectors.yml as rails config              ← not hashed
├── deploy/
│   ├── guardrail-pins.env   # GENERATED single source of both pins — compose `env_file:` AND kustomize
│   ├── kustomization.yaml   # configMapGenerator over those same bytes
│   └── k8s/                 # Deployment (envFrom the pins ConfigMap, /healthz + /readyz probes), Service, ServiceAccount
├── scripts/
│   ├── demo_nemo_capability.py       # Capability walk-through against a running pod
│   └── demo_refuse_to_serve.sh       # Proves refuse-on-drift against a real process
├── tests/
│   ├── test_deterministic_output_rail.py   # Detector: block / short-circuit / attribution, /readyz fail-fast
│   ├── test_pii_retirement.py              # PII table is gone; the blocks/validator seam still works (synthetic fixture)
│   ├── test_advice_boundary_prompt.py      # The case-officer output prompt says what it must say
│   ├── test_gate_parity_and_mode_a.py      # Deterministic parity + Mode A (LLM rail fails → detector still blocks)
│   ├── test_bedrock_smoke.py               # @requires_aws live smoke — real Haiku verdicts end-to-end
│   ├── test_config_digest.py               # Digest is stable and folds detectors.yml
│   ├── test_digest_enforcement.py          # Startup refuses to serve on config/ or uv.lock drift
│   ├── test_digest_ripple.py               # A config edit ripples to the 1.8.0 pin and the generated env file
│   ├── test_deploy_surface_pins.py         # Both surfaces carry both pins from the ONE source; no credentials in config
│   ├── test_no_openai_guard.py             # `openai` is absent from the resolved environment
│   ├── test_rails.py / test_skeleton.py    # Contract + engine wiring
│   ├── conftest.py / helpers.py            # Substitution seam: pre-install mock rails/settings/detectors — tests never reach AWS
│   └── fixtures/                           # nemo_config/ (digest tests) + parity/detector_answers.jsonl
├── Dockerfile               # Two-stage; runtime ENV NEMOGUARDRAILS_LLM_FRAMEWORK=langchain; CMD uvicorn :8080
├── pyproject.toml           # nemoguardrails + langchain-aws; NEVER the `[server]` extra (it pulls openai)
└── uv.lock                  # The resolved artifact whose sha256 is the second determinism hash
```

---

## Design

**1. A separate pod, not code inside the orchestrator.** NeMo drags in LangChain
and `langchain-aws`; hosting it in the orchestrator would bloat the image and
violate the `stages-pure` import boundary. Behind an injected `Protocol` client
the orchestrator stays lean and pure, and the pod is already shaped for the
independent-K8s-pod future (its own `/readyz`, its own credentials) — compose is
dev-only, not the only wiring.

**2. Verdict-only — a validator, not a transformer.** `/check/output` returns a
decision, never rewritten text. A pod bug cannot corrupt or un-redact delivered
content, and the contract stays plain data. The direct consequence is that a
detected secret is **blocked, not masked**: masking a span in place would make
the pod a content transformer. A refusal is the safe failure.

**3. Deterministic detection stays deterministic — and stays a real NeMo rail.**
An LLM self-check cannot give in-place determinism (`AKIA[0-9A-Z]{16}` either
matches or it doesn't — free, exact, reproducible) or a guaranteed verdict. So
the secrets table runs as a **registered NeMo action** (`config/actions.py` → a
Colang flow), ordered first. Three things at once: the whole guard surface is
expressed as NeMo rails config and captured by the digest; detection is free and
exact; and because it never touches the LLM it survives an LLM-rail failure.

The pod's authoritative path (`nemo_runtime.check_output`) runs the same detector
**in-process before `generate`**, so a hit skips the paid call entirely. The
Colang flow is not redundant — it is what makes the rail genuinely runnable
inside `LLMRails`, and what puts it in the digest.

**4. PII is deferred, not dropped.** The `pii:` table (credit_card, ssn,
passport, mrn, email, phone) was withdrawn deliberately. It was **US-shaped** —
it never covered the Australian identifier set (TFN, ABN, ACN, BSB) at all, so it
bought no real coverage while costing real false positives on an accounting
product that must legitimately *see* financial identifiers to do its job. And
compliance has not ruled on whether those identifiers should be blocked, flagged
or redacted. No interim replacement table was invented. When compliance rules,
the route is Presidio via NeMo's `sensitive_data_detection` rail
(`docs/configure-rails/guardrail-catalog/pii-detection.mdx:184-216`), whose AU
recognizers carry real checksums. The `blocks:` / `validator:` seam and the Luhn
implementation are retained for that return.

**5. Per-failure-mode fail policy.** One service now guards everything, so its
failure behaviour is designed per mode rather than flat:

- **Mode A — LLM rail fails, pod up** (throttle, transport, `ClientError`). The
  deterministic secrets rail **still blocks** (pure regex, no model); `self check
  output` / `self check facts` **fail OPEN with an advisory flag**. Credential
  protection survives the common failure. Owned at the pod boundary
  (`nemo_runtime`), so a guard failure is an honest 200 verdict, not a 5xx.
- **Mode B — pod unreachable** (the orchestrator's concern). Input fails
  **safe/block**; output fails **open + advisory flag**, made loud with a
  distinct `nemo-output-fail-open-v1` rule id — never silent. Owned in the
  orchestrator's pure stage.

Only the `generate` call itself is wrapped. A genuine block and a mapping bug are
never swallowed; the app-level handlers in `main.py` are a backstop for errors
*outside* the guarded call.

**6. Construct-once, fail-fast readiness.** The one `LLMRails` engine and the
compiled detector are built once in the lifespan and reused. `/readyz` returns
503 if the detector didn't compile — a broken pattern must never silently no-op.
Readiness deliberately makes **no paid Bedrock probe**; invoke permission is
proven by the first real `/check`. Importing any module does no I/O, and lifespan
fills each `app.state` slot only when absent, so tests pre-install mocks and never
reach AWS.

**7. Never trust NeMo for identity or message text.** `model_id` is stamped by
the pod; a block surfaces only as the exception `type`. See mechanisms 1 and 5.

---

## Testing

```bash
uv run pytest                        # full suite; live `requires_aws` tests auto-SKIP without credentials
uv run pytest -m "not requires_aws"  # the deterministic CI subset
uv run pytest -m requires_aws        # live Bedrock smoke (needs ap-southeast-2 credentials)
```

The deterministic rail, Mode A, digest enforcement, the deploy-surface pin
plumbing and the output-prompt content are all asserted **without AWS** — the
detector is pure regex and the fail policies are mapped in-process. Only the
LLM-verdict smoke needs a live rail.

---

## Current state (read before you rely on this)

- **Only `legal-rag-default-1.8.0` is guarded.** Configs `1.0.0`–`1.4.0` still
  declare `input_categories` / `output_categories`, but those fields are now
  accepted-and-ignored: the in-house implementation behind them was deleted.
  They resolve with **no guarding at all**.
- **The defaults still point at those configs.** `DEFAULT_PIPELINE_CONFIG_REF` is
  `legal-rag-default-1.1.0` and the Chainlit UI pins `1.4.0`. Nothing is
  protected until those are flipped to `1.8.0`.
- **`services/eval/scripts/guardrail_parity_gate.py` is stale.** It compares
  in-house `1.4.0` against nemo-all `1.8.0`; with the in-house guard gone, arm A
  now measures an unguarded config. It is not a cutover gate any more.
- **PII coverage is zero by design** (see design point 4), pending a compliance
  ruling.
