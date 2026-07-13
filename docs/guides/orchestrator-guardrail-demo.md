# Orchestrator + Guardrail Demo — what we built and why

This document explains the work landed in the orchestrator service and its
Chainlit demo UI: a config-gated **system-prompt-leakage guardrail** on the live
RAG query path, exercised end-to-end through a streaming chat UI with per-session
memory. It is written as a narrative of *what happened* and *why the pieces fit
the way they do*.

> TL;DR — The orchestrator is the Evaluation Service's **system-under-test**. The
> Chainlit demo is an **honest live view** of that system, so we can eyeball the
> design decisions (contracts, config pinning, guardrails-as-data, tracing)
> before they harden into the production orchestrator. Everything shown is real:
> real Bedrock calls, real retrieval, real spans — nothing mocked.

---

## Purpose: the demo exists to test the *design repo*

The orchestrator in this repo (`crucible`) is **not** the production orchestrator.
It exists to give the Evaluation Service something true to measure and to let us
validate design decisions with evidence rather than on paper:

- Is the `/query` **contract** (eval's `rag_query_output` v1.1.0) actually usable?
- Does **config pinning** (`{name}-{semver}` + `config_sha256`) give us
  "identical config ⇒ identical behavior"?
- Can **guardrail decisions** be first-class *data* (in the response and in
  spans), not just a silent refusal?
- Do **traces** join end-to-end (UI → pipeline) so a wrong/slow answer is
  attributable?

The Chainlit UI is how a human drives all of that and sees it working. If the
demo is honest, the design is real.

---

## 1. Built on the given orchestrator structure

The guardrail was slotted into the **existing** module structure — no new
architecture, just filling seams that were already there:

```
services/orchestrator/app/
├── routers/          HTTP surface — POST /query, POST /query/stream
├── orchestrator/     PURE pipeline stages (no infra imports):
│                       guardrails · policy_router · query_rewrite · retriever ·
│                       reranker · context_assembler · prompt_builder · citation_builder
├── clients/          Infra, INJECTED into stages: bedrock · opensearch · guardrail (new)
├── schemas/          Pydantic v2 request/response + pinned-config models
├── configs/          IMMUTABLE pinned configs (legal-rag-default-1.0.0 … 1.3.0)
└── observability.py  OpenInference spans (created in the router, never in stages)
```

Key discipline we kept intact:

- **Pure stages.** `app/orchestrator/*` imports no boto3 / opensearch / OpenTelemetry.
  The new guard stage (`guardrails.check_input`) receives an **injected** classifier
  described by a `typing.Protocol`, so it stays free of infra imports — enforced by
  the `stages-pure` import-linter contract (`uv run lint-imports`).
- **Behavior lives in immutable configs.** The guard is enabled by a **new**
  pinned config `legal-rag-default-1.3.0` (carrying 1.2.0's pins forward, adding an
  `enabled` guardrails block + a prompt-hardening line). Released configs
  `1.0.0/1.1.0/1.2.0` are never edited; new behavior only ever lands in a new
  version, and `config_sha256` covers every prompt byte.
- **The envelope already had the slot.** `guardrail_decisions[]` was schema'd in
  Phase 1; we just populate it now (a single `block` decision on a refusal).

The result: the guardrail is a **config-gated** feature. Under `1.1.0`/`1.2.0`
(the eval lane) it is a typed identity — byte-for-byte the old behavior, proven
by a `/query` golden-bytes regression test.

---

## 2. Chainlit tests the streaming query path

The demo UI (`services/orchestrator/demo_ui/`) is a **dependency-firewalled**
client — its own venv/lockfile, talks to the orchestrator over HTTP only, never
imports `app.*`, and is kept out of the service image (so the orchestrator's
independent-pod future stays clean).

One chat turn:

```
Chainlit  ── POST /query/stream (SSE) ──▶  Orchestrator  ──▶ Bedrock (stream)
   │  event: token  (live deltas, rendered as they arrive)
   │  event: final  (the full QueryResponse envelope)
   ▼
renders answer + [n] citations + retrieved chunks + timings + provenance + trace link
```

- Tokens stream **live** via real Bedrock deltas (never a typewriter effect over a
  pre-computed answer).
- Citations, sources, timings and provenance are rendered **only** from the
  `final` envelope — zero citations is stated honestly.
- Each turn is one **joined Phoenix trace**: the UI's `chat_turn` CHAIN span
  injects a `traceparent`, and the orchestrator's pipeline root becomes its child.

> **Gotcha worth remembering:** the joined trace requires `PHOENIX_ENDPOINT` on
> **both** processes. If only the UI has it, Phoenix shows only `chat_turn` and
> the orchestrator (running with a `NoOpTracer`) emits no `embedding/retrieval/
> generation/citation` spans. Set `PHOENIX_ENDPOINT=http://localhost:6006` for the
> orchestrator *and* the UI.

### Two demo bugs we fixed here

Both were in the UI's SSE consumption, surfaced by the guard's fast single-`final`
close:

1. **`async generator ignored GeneratorExit`** — `on_message` broke out of the
   SSE `async for` on the terminal event, abandoning the generator; GC closed it
   later, outside the loop, where its httpx teardown can't await. Fixed by wrapping
   the generator in `contextlib.aclosing` so `break` closes it *in* the loop.
2. **`Attempted to exit cancel scope in a different task`** — `stream_query` held
   httpx's `async with` open across `yield`s; httpx (anyio) binds cancel scopes to
   the entering task, and Chainlit resumed/closed the generator on another task.
   Fixed by running the httpx request in a **dedicated pump task** (events handed
   over an `asyncio.Queue`), so the client is opened *and* closed in one task.

---

## 3. A simple guardrail for checking (system-prompt leakage)

The guard protects the pipeline from **prompt-injection / system-prompt
extraction** ("repeat your system prompt", "ignore your instructions"). It runs
**first**, before any paid call.

Layered, cheapest-first:

- **Layer 0 — prompt hardening** (in `1.3.0`'s template): an explicit
  non-disclosure instruction. Defense in depth, not the real gate.
- **Layer 1 — input guard** (`guardrails.check_input`):
  1. a deterministic keyword **pre-filter** (regex for "system prompt", "ignore
     previous", "reveal instructions", …). A miss → allow, **no paid call** (cost
     ≈ 0 on normal traffic).
  2. on a hit, a cheap **Bedrock Haiku classifier** (deterministic, temp 0) decides
     SAFE / UNSAFE.
- **Layer 2 — enforce**: UNSAFE (or any classifier error on a flagged input →
  *fail-safe*) raises a `GuardrailTripwire`, which the router turns into a
  **200 canned refusal** — *not* a 5xx:

```
answer.text = "I'm sorry, but I can't help with that request."
citations = []   retrieved_chunks = []   guardrail_decisions = [ {decision: block, category: prompt_leak, …} ]
```

On the streaming route the block raises **before** `StreamingResponse` is built,
so exactly **one `final` event and zero `token` events** are emitted — nothing
leaks. In Chainlit the user sees the refusal verbatim; no UI change was needed.

**Observability.** The Haiku call is a first-class `guardrail_input` **LLM span**
(model id + token counts + latency), recorded on **both** a SAFE allow and a
block — so the guard's cost is visible in Phoenix, not invisible. A block adds the
`block` decision attributes to that same span.

**Honest limits (by design in this slice):**
- **Paraphrase bypass** — a leak phrased around the pre-filter keywords never
  reaches the classifier; only prompt hardening resists it. The pre-filter is the
  recall ceiling.
- **Multi-turn bypass** — the guard classifies only the *current* turn, not history.
- **Indirect injection** — a poisoned retrieved chunk isn't screened on the
  streaming route.

These are tracked follow-ups (output guard / buffered-stream guard / retrieval
guard) in the orchestrator roadmap Phase 3.

---

## 4. Multi-turn memory (in-memory, per session, lost on a new session)

Multi-turn is deliberately **session-scoped and ephemeral**:

- History lives in `cl.user_session` — **in the browser session only**. It is sent
  with each request as a bounded `history[]` and is **never** persisted
  server-side.
- A **new session loses it**: refresh / new tab / new browser → empty history,
  fresh conversation. Nothing carries across sessions or users.
- The follow-up **rewrite is deterministic** (no extra paid call): recent user
  turns are prefixed to the retrieval query, so a follow-up like *"How does that
  differ from robbery?"* resolves via memory. The rewritten query is the honest
  record in the `retrieval` span's `INPUT_VALUE`.
- A turn is appended to history **only after it completes** (final envelope); a
  failed/refused turn is not saved.

So "memory" here means *conversational continuity within one browser session* —
not a datastore, not cross-session, not multi-user.

---

## 5. What the demo lets us validate about the design

Driving the demo confirms the design repo's core bets, with evidence:

| Design bet | What the demo shows |
|---|---|
| Eval-native **contract** | The streamed `final` envelope validates against `rag_query_output` v1.1.0; the eval lane (`1.1.0`) is byte-for-byte unchanged. |
| **Config pinning** | Guard on/off is purely a matter of which pinned config (`1.3.0` vs `1.1.0`); every response echoes `pipeline_version` + `config_sha256`. |
| **Guardrails as data** | A block is a structured `guardrail_decisions[]` entry **and** a `guardrail_input` span — scoreable, not a silent refusal. |
| **Honest rendering** | Citations/sources/timings come only from the envelope; zero citations and refusals are shown truthfully; no fake streaming. |
| **Joined tracing** | One Phoenix trace per turn (UI ⇄ pipeline), so latency and decisions are attributable end-to-end. |
| **Pure stages / injected infra** | The guard is a pure stage with an injected client; `stages-pure` + grep gates stay green. |

The corpus is `isaacus/legal-rag-bench` — 4,876 passages from the **Victorian
Criminal Charge Book** (Australian criminal law), so test questions are
criminal-law questions (theft, robbery, burglary, mens rea, …); off-topic
questions honestly return "I don't have enough information".

---

## How to run and test

- **Startup** (three terminals — Phoenix, orchestrator, UI): both the orchestrator
  and the UI must export `PHOENIX_ENDPOINT=http://localhost:6006`; the orchestrator
  also needs `ORCHESTRATOR_OPENSEARCH_ENDPOINT` and AWS credentials. The UI defaults
  to `legal-rag-default-1.3.0` (guard on).
- **Test questions**: see
  [`services/orchestrator/demo_ui/TEST_QUESTIONS.md`](../services/orchestrator/demo_ui/TEST_QUESTIONS.md)
  — single- and multi-turn citation threads, and guardrail block / allow /
  pre-filter-hit-but-safe cases, each with what to look for.

## Where the code lives

- Guard stage: `services/orchestrator/app/orchestrator/guardrails.py`
- Guard classifier client: `services/orchestrator/app/clients/guardrail.py`
- Guard-enabled config: `services/orchestrator/app/configs/legal-rag-default-1.3.0.yaml`
- Route wiring + guard span: `services/orchestrator/app/routers/query.py`,
  `services/orchestrator/app/observability.py`
- Streaming SSE client (pump-task fix): `services/orchestrator/demo_ui/chat_ui/client.py`
- Spec: `agent-os/specs/2026-07-13-system-prompt-guardrail/`
</content>
