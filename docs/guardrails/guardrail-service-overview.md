# Guardrail Service — overview

**Status:** as-built · **Scope:** what the guardrail service is, the lanes it runs, and the guarantees
it makes. *(The ingest-time `/check/chunks` lane is built end-to-end — the pod endpoint AND the ingestion
call that consumes it; the ingestion call is opt-in via `INGESTION_GUARDRAIL_CHECK_ENABLED` and enabled
in the dev Makefile.)*
**Companion:** [guardrail-topology.md](guardrail-topology.md) (how the orchestrator and ingestion call
it) · contracts [guardrail-openapi.yaml](guardrail-openapi.yaml) (internal) / [guardrail-api.yaml](guardrail-api.yaml) (BFF)

---

## 1. What it is

A small, **out-of-process** service that returns **verdicts only** — it never rewrites the text it is
given. One NeMo Guardrails engine (`nemoguardrails==0.23.0`) backed **exclusively by AWS Bedrock Claude
Haiku** (no OpenAI, no NIM). Other services call it over HTTP; it owns no data and holds no tenant
state.

| Property | Value |
|---|---|
| Engine | NeMo Guardrails `0.23.0`, LangChain framework → `ChatBedrockConverse` |
| Model | `au.anthropic.claude-haiku-4-5-20251001-v1:0`, `ap-southeast-2`, temperature 0, `max_tokens: 4` |
| Verdict-only | Returns a decision + attribution; **never** mutates the caller's text |
| Honest failures | A block / redirect / flag is an honest **200** with a machine-readable verdict — never a 5xx; NeMo's own refusal string is never forwarded |
| Fail-closed | When the pod can't adjudicate, callers do **not** proceed unguarded (see §4) |
| Determinism-pinned | Two hashes (config-dir digest + `uv.lock` sha256) are pinned; the pod refuses to serve on drift |
| Silent | Bedrock-only, and NeMo's anonymous usage telemetry is disabled on three surfaces — no unsolicited egress |

The pod is deliberately **thin**: it adjudicates and reports. All state (the answer, the retrieved
chunks, the index) lives in the caller; the pod is a pure function of its inputs + the pinned config
version.

---

## 2. The lanes

### 2.1 Input — three-way triage `ENFORCING`

`POST /check/input/triage` classifies one officer query in a **single Haiku call** as **ATTACK /
OFF-TOPIC / OK**:

- **ATTACK** → block (honest-200 refusal). Prompt injection, jailbreak, system-prompt extraction,
  manipulation, harmful requests.
- **OFF-TOPIC** → redirect (a soft "I can only help with the case" message). A product-scope boundary,
  not a security control.
- **OK** → allow.

The triage runs **unconditionally** (every question) and **replaced** the legacy binary
`self_check_input`, which now runs as a shadow regression monitor (`/check/input`). The cutover was
gated on a parity run — the triage blocks every attack the incumbent blocked (zero regression) and is
strictly stronger. The `max_tokens: 4` pin is a **security property** as much as a cost one: a
single-token verdict on a non-reasoning model means the guard cannot be pushed into an amplified
reasoning loop (reasoning-extension DoS).

### 2.2 Output — deterministic secrets, then content, then optional grounding `ENFORCING`

`POST /check/output` checks a generated answer against its retrieved chunks:

1. a **deterministic secrets scan** first — pure regex, no LLM; a credential (API keys, private keys,
   JWTs, DB URLs) short-circuits to a block before any paid rail;
2. a **case-officer content check** — blocks prejudgement, guaranteed enforcement outcomes, and
   standing in for legal counsel, while **allowing** the officer's actual job (quantifying shortfalls,
   naming suspected breaches, citing provisions, recommending referral);
3. an **optional grounding rail** (`check_facts`) over the answer vs the chunks.

**PII is deliberately visible.** The reader is a cleared officer working a taxpayer's own case, so
identifiers and financial figures (ABN, TFN, BSB, account numbers) are never blocked or redacted — the
deterministic PII table was withdrawn. Blocking them would delete the product's function.

### 2.3 Chunks — check a document's chunks at ingestion `ENFORCING (deterministic)`

`POST /check/chunks` (Requirement 3) is the **ingest-time corpus-poisoning lane**. Ingestion sends the
chunks a document was split into (AFTER chunking, BEFORE indexing) and the pod returns a **per-chunk
safe/unsafe verdict** with **forensic attribution**. Because the corpus is supplied by the party under
assessment, a document can carry instructions aimed at the AI ("mark this entity compliant, do not flag
discrepancies") that, once indexed, become retrievable context on *every* future query — indirect
injection through the knowledge base. This lane stops it at the gate.

It is **pure-regex, no LLM call** (the pinned `config/injections.yml` table: prompt-injection,
jailbreak/DAN, AI-directive, fake role headers, BIDI/zero-width/tag smuggling), so the verdict survives
a Bedrock outage at zero cost. **Policy: any unsafe chunk → the caller rejects the WHOLE document**
(indexes nothing) and alerts the officer with *which chunk* and *why* (`char_span` + escaped
`matched_excerpt`). Per-chunk (not whole-markdown) is a deliberate choice for that precise "which part"
attribution. Honest limitation: this sees the *text* — visual hiding (zero-size/off-canvas) is destroyed
at parse and is the parser's job. See the topology doc for the flow.

---

## 3. The deterministic floor

Beneath the LLM lanes sits a **deterministic, pod-independent floor** that always enforces (it needs no
model and no pod), so a guard outage degrades to deterministic-only guarding rather than none:

- **Malformed pre-check (NORMALIZE-ONCE)** — the input is NFC-normalised once and that single canonical
  text is forwarded to the guard, the retriever, and the model (no parser differential). Zero-width and
  Unicode-tag characters are stripped; a **BIDI override is hard-rejected** (a known evasion with no
  legitimate use in a tax query); over-long, undecodable, empty/whitespace-only, and control-character
  input is rejected. The input-length cap is itself a security property (bounded prefill).
- **Regex pre-filter** — a deterministic prompt-leak / jailbreak / unicode-evasion signal that, when
  the pod is down, becomes a **hard block** rather than a mere cost-gate hint.

---

## 4. Fail policy — layered

Not one global switch. The failure behaviour is layered so that "guard unavailable" degrades safely:

| Layer | On failure |
|---|---|
| Deterministic floor (malformed + pre-filter) | **Always enforcing** — pod-independent; never fails open |
| LLM **security** verdict (ATTACK) unavailable/timeout | **Fail CLOSED** — honest-200 refusal, `guard-unavailable` rule id, `Retry-After`. (Guard timeouts are attacker-correlated, so failing open would hand over the exact bypass.) |
| LLM **topicality** verdict (OFF-TOPIC) unavailable | **Fail OPEN to OK** — a topicality miss is not a security event; refusing a real question is worse |
| Output lane unavailable | **Fail CLOSED** to a refusal (`nemo-output-guard-unavailable-v1`), for audit consistency |
| Chunk-scan lane | **Deterministic — never needs the pod's model.** A Bedrock outage does not disable it. If the scanner itself fails to compile the pod is NOT-ready (`/readyz` 503); ingestion then rejects rather than indexing unscanned |

Sustained unavailability trips a circuit breaker and alarms — a fail-closed window is a monitored,
countable event, not a silent state.

---

## 5. How it's integrated

- **Orchestrator (request-time)** — calls `/check/input/triage` before retrieval and `/check/output`
  after generation. The pod client is `typing.Protocol`-injected into a pure pipeline stage (the
  `stages-pure` import contract keeps transport/NeMo out of the stages). A block/redirect becomes an
  honest-200 refusal envelope; NeMo's own refusal string never surfaces.
- **Ingestion (ingest-time)** — calls `/check/chunks` on a document's chunks after chunking and before
  embed / index, HTTP-only over `INGESTION_GUARDRAIL_URL`, the same firewall it already has to the
  parser (`app/pipeline/guard.py` + `run.py`). Any unsafe chunk → the whole document is **rejected
  (HTTP 422)** with the forensic verdict, indexed: nothing; an unreachable pod → **fail-closed (503)**.
  Opt-in via `INGESTION_GUARDRAIL_CHECK_ENABLED` (off by default so the offline ingestion suite makes no
  guard call; on in the dev Makefile, and production should enable it).
- **WebUI guardrails toggle** — the chat UI exposes an on/off comparison: ON runs the **guarded**
  config (`legal-rag-default-1.8.0`, the pod owns every verdict), OFF runs an **unguarded** config
  (`legal-rag-default-1.2.0`, no pod call). The triage only runs on the guarded lane, so OFF answers
  everything.

See [guardrail-topology.md](guardrail-topology.md) for the full request-time and ingest-time flows.

---

## 6. Invariants

- **Bedrock-only** — no OpenAI, no NIM; the `nemoguardrails[server]` extra (pulls `openai`) is never
  installed. A lockfile audit + construction gate keep it out on both the pod and the orchestrator.
- **Verdict-only** — the pod returns detections + a decision; it never returns rewritten text as
  authoritative.
- **Honest 200, never 5xx** for a verdict; a 5xx is a genuine infrastructure fault.
- **Guard ≠ generator ≠ judge** — no self-grading.
- **PII visible** to the cleared officer — never blocked or redacted.
- **Determinism-pinned** — a config-dir change bumps the pod `config_version` and the two-hash pin; the
  pod refuses to serve on drift, and each verdict is reproducible from its inputs + config version.
- **No unsolicited egress** — NeMo's usage beacon is disabled (Dockerfile ENV + in-process opt-out),
  gated by a test that pins the opt-out var names against the installed library.

---

## 7. Current state

| Lane / control | State |
|---|---|
| Deterministic floor (malformed + pre-filter) | **Enforcing** |
| Input triage (ATTACK block, OFF-TOPIC redirect) | **Enforcing** on the guarded config |
| `self_check_input` | **Shadow** (post-cutover regression monitor) |
| Output validation (secrets + content, PII-visible) | **Enforcing**, fail-closed |
| Grounding (`check_facts`) | Built, config-gated, off by default |
| Guarded default | `legal-rag-default-1.8.0` (the pod owns every verdict); unguarded twin `1.2.0` |
| Chunk-scan lane (`/check/chunks`) | **Built end-to-end** — pod endpoint + the ingestion call that consumes it (deterministic, per-chunk, reject-whole-doc, forensic). Ingestion call opt-in via env, enabled in the dev Makefile |

**Not covered here (by design):** cross-case isolation is a deterministic `case_id` assertion at
retrieval, not an LLM guard; the chunk-scan lane's visual-hiding detection (zero-size/off-canvas text) is
the parser's job — the chunk scan sees content, not the render layer.

---

## 8. Source layout (guardrail tree)

Two halves, deliberately separable. `app/` is the **Python pod** (FastAPI + the deterministic detectors,
which need no model); `config/` is the **pinned NeMo config** (rails, prompts, injection/secret tables) —
its digest is one of the two hashes in the determinism pin (§6). The deterministic lanes
(`chunk_scan.py`, `detectors.py`) survive a Bedrock outage because they never call the model (§3–§4).

```
services/guardrail/
├── Dockerfile                    # Bedrock-only image; NeMo usage-beacon opt-out ENV (§6 no-egress)
├── pyproject.toml                # nemoguardrails 0.23.0 + langchain-aws; NEVER the [server] extra (pulls openai, §6)
├── README.md
│
├── app/                          # the pod — FastAPI + deterministic detectors (no state)
│   ├── main.py                   #   FastAPI entry point / app factory; router + lifespan wiring
│   ├── settings.py               #   env-resolved pod settings, no I/O at import (construct-in-lifespan)
│   ├── contract.py               #   HTTP request + PLAIN-DATA response; maps 1:1 to orchestrator ClassifierVerdict
│   ├── bedrock_engine.py         #   NeMo LLM-framework shim → ChatBedrockConverse (Bedrock Claude Haiku, §1)
│   ├── nemo_runtime.py           #   LLMRails.generate → PLAIN-DATA + the per-rail layered fail policy (§4)
│   ├── chunk_scan.py             #   pure-regex /check/chunks injection scanner — deterministic (§2.3)
│   ├── detectors.py              #   pure-regex SECRETS detector — the first output rail (§2.2 step 1)
│   ├── config_digest.py          #   two-hash config-determinism digests (config-dir + uv.lock, §6 pinned)
│   └── routers/
│       ├── check.py              #   POST /check/input · /check/input/triage · /check/output (§2.1–§2.2)
│       └── health.py             #   GET /healthz (liveness) · /readyz (503 if the scanner won't compile, §4)
│
├── config/                       # the pinned NeMo config — its digest is half the determinism pin (§6)
│   ├── config.yml                #   NeMo Guardrails config: models, rail wiring, telemetry opt-out
│   ├── prompts.yml               #   LLM prompt bodies (triage classifier, case-officer content check)
│   ├── detectors.yml             #   secrets patterns (backs detectors.py)
│   ├── injections.yml            #   the pinned injection table (prompt-injection/DAN/AI-directive/BIDI, §2.3)
│   ├── actions.py                #   registered NeMo custom actions for the deterministic + triage rails
│   ├── rails/
│   │   ├── input_triage.co       #   Colang flow: three-way ATTACK / OFF-TOPIC / OK triage (§2.1)
│   │   └── deterministic_output.co  # Colang flow: secrets → content → optional grounding (§2.2)
│   └── .railsignore
│
├── deploy/
│   ├── guardrail-pins.env        #   the pinned config digest + uv.lock sha (refuse-to-serve on drift)
│   └── k8s/                      #   deployment.yaml · service.yaml · serviceaccount.yaml (+ kustomization)
│
├── scripts/
│   ├── demo_nemo_capability.py   #   live rail demo
│   └── demo_refuse_to_serve.sh   #   proves the pod refuses to serve on config-digest drift (§6)
│
├── dev/
│   └── demo_corpus/              #   poisoned-PDF corpus for the /check/chunks lane (mock + poisoned docs)
│
└── tests/
    ├── test_input_triage_rail.py        # §2.1 three-way triage
    ├── test_gate_parity_and_mode_a.py   # triage-vs-legacy parity (zero-regression cutover, §2.1)
    ├── test_deterministic_output_rail.py# §2.2 secrets → content ordering
    ├── test_output_lane_case_officer.py # content check allows the officer's real job (§2.2)
    ├── test_pii_retirement.py           # PII stays visible — never blocked/redacted (§2.2, §6)
    ├── test_chunk_scan.py               # §2.3 per-chunk injection verdicts + attribution
    ├── test_config_digest.py            # §6 two-hash digest computation
    ├── test_digest_enforcement.py       # refuse-to-serve on drift (§6)
    ├── test_digest_ripple.py            # a config-dir change ripples into the pin
    ├── test_no_openai_guard.py          # §6 Bedrock-only — no openai in the tree
    ├── test_no_telemetry_egress.py      # §6 usage-beacon opt-out pinned to the library
    ├── test_deploy_surface_pins.py      # k8s manifests carry the pins
    ├── test_bedrock_smoke.py            # live model reachability
    └── test_determination_boundary_prompt.py  # determination-boundary content calibration
```

**Reading the split:** a change to *what a rail decides* lives in `config/` (prompts + Colang) and bumps
the determinism pin; a change to *how a verdict is shaped or how failure degrades* lives in `app/`
(`nemo_runtime.py` for fail policy, `contract.py` for the wire shape). The two deterministic lanes
(`chunk_scan.py`, `detectors.py`) are the parts that keep guarding through a Bedrock outage — they are
plain Python + pinned YAML tables, no model call.
