# Chainlit demo UI (`demo_ui/`)

A dependency-firewalled Chainlit chat front-end for the orchestrator's LIVE
RAG pipeline: real token streaming from `POST /query/stream` (SSE), bounded
per-session multi-turn memory via the request `history` field, and one JOINED
Phoenix trace per chat turn (the UI's CHAIN span parenting the pipeline
spans).

This is its OWN uv project (own `pyproject.toml`, own `.venv`, own
`uv.lock`). It talks to the orchestrator ONLY over HTTP and never imports the
orchestrator's `app.*` package; Chainlit's dependency tree never enters the
orchestrator venv, `uv.lock`, or Docker image (`demo_ui/` is in the service's
`.dockerignore`).

## Run it

Prerequisites: the orchestrator serving at `ORCHESTRATOR_URL` (default
`http://localhost:8000`) with its dependencies up — see "Bringing the stack
up" below. Phoenix is optional (see Tracing).

```bash
cd services/orchestrator/demo_ui
uv sync
uv run chainlit run app.py          # opens http://localhost:8000 by default —
                                    # pick another port if the orchestrator is
                                    # local: uv run chainlit run app.py --port 8501
```

With tracing:

```bash
PHOENIX_ENDPOINT=http://localhost:6006 uv run chainlit run app.py --port 8501
```

On chat start the UI probes `GET /readyz`. If the orchestrator is down or not
ready, the REAL dependency error is shown and the chat stops — there is
deliberately NO degraded fake mode.

## Env surface

| Variable | Default | Meaning |
| --- | --- | --- |
| `ORCHESTRATOR_URL` | `http://localhost:8000` | Orchestrator base URL (the UI's only channel to the pipeline). |
| `PHOENIX_ENDPOINT` | unset | Phoenix UI endpoint (e.g. `http://localhost:6006`). Unset → a genuine no-op tracer, no `traceparent` injected, no trace links rendered; the UI stays fully functional. |
| `DEMO_UI_PIPELINE_CONFIG` | `legal-rag-default-1.2.0` | The single pinned config ref sent on every request. `1.2.0` is the history-aware config; eval's lane stays on `legal-rag-default-1.1.0`. |

## Cost and honesty rules

- Each turn = exactly ONE paid Bedrock generation + one Titan query
  embedding (stated in the welcome banner). The history-aware query rewrite
  is deterministic and adds no paid call.
- Tokens render live from real SSE deltas (`msg.stream_token` per `token`
  event) — no typewriter pacing, no fake progress.
- Citations (resolved from `[start,end)` claim spans), sources, per-stage
  `timings_ms`, and provenance (`config_sha256` short form,
  `pipeline_version`, index) render ONLY from the `final` envelope. Zero
  citations is reported honestly.
- Guardrails are Phase-3 stubs (`guardrail_decisions` always `[]`) and are
  never rendered as active.
- A mid-stream `error` event renders typed (detail, dependency, retry
  guidance); the partial streamed text stays visible, clearly marked
  INCOMPLETE, and the failed turn is NOT added to session history.
- Memory is per-session only (`cl.user_session`), truncated client-side to
  the same bounds the request schema enforces (20 turns / 8,000 chars per
  turn, oldest dropped first).

## Phoenix tracing — single shared project (decision)

The UI configures its OWN OTel tracer provider (OTLP-HTTP to
`{PHOENIX_ENDPOINT}/v1/traces`) with resource **`PROJECT_NAME =
"orchestrator"`** — the SAME Phoenix project as the service — and a distinct
**`service.name = "chainlit-ui"`**.

Rationale: Phoenix groups traces by the project resource attribute. Splitting
the UI into its own project would render each joined trace partially in each
view, defeating the point of joining them. Attribution within the shared
project comes from `service.name` instead.

Per chat message the UI emits one OpenInference CHAIN span (`INPUT_VALUE` =
user message, `OUTPUT_VALUE` = final answer text) and injects the W3C
`traceparent` on the `/query/stream` call, so the orchestrator's root span
(which extracts incoming trace context) becomes its child: ONE joined trace
per turn, containing both the `chainlit-ui` CHAIN span and the pipeline's
`embedding` / `retrieval` / `context_assembly` / `generation` /
`citation_build` children.

This decision is also recorded in `agent-os/product/tech-stack.md`.

## Known limitation: deterministic query rewrite

Follow-up questions are made retrieval-ready by a DETERMINISTIC rewrite: a
config-pinned window of recent user turns is prefixed to the current question
(pins in `legal-rag-default-1.2.0`). This keeps the per-turn cost at one paid
generation, but it does not resolve pronouns/ellipsis as well as an LLM
condense-question step. An LLM rewrite (pinned haiku-class model, a new
config version, and an updated cost banner) is a documented LATER upgrade —
out of scope here. The honest provenance channel for "what was actually
retrieved against" is the retrieval span's `INPUT_VALUE` in Phoenix.

## Bringing the stack up

1. **OpenSearch endpoint discovery** (same order as
   `services/orchestrator/scripts/demo_live_query.py` and the repo Makefile):
   `ORCHESTRATOR_OPENSEARCH_ENDPOINT` env, else `EVAL_OPENSEARCH_ENDPOINT`
   env, else the `opensearch-poc` CloudFormation stack output:

   ```bash
   aws cloudformation describe-stacks --region ap-southeast-2 \
     --stack-name opensearch-poc \
     --query "Stacks[0].Outputs[?OutputKey=='DomainEndpoint'].OutputValue" \
     --output text
   ```

2. **Phoenix** (optional, for the joined trace):
   `docker compose -f docker-compose.observability.yml up -d` from the repo
   root (UI at `http://localhost:6006`).

3. **Orchestrator** (own venv, real AWS creds in the ambient boto3 chain):

   ```bash
   cd services/orchestrator
   ORCHESTRATOR_OPENSEARCH_ENDPOINT=<endpoint> \
   PHOENIX_ENDPOINT=http://localhost:6006 \
   uv run uvicorn app.main:app --port 8000
   curl localhost:8000/readyz   # must be 200 before the UI is useful
   ```

4. **This UI** (its own venv — never the orchestrator's):

   ```bash
   cd services/orchestrator/demo_ui
   PHOENIX_ENDPOINT=http://localhost:6006 uv run chainlit run app.py --port 8501
   ```

## Tests

Browser-free logic (SSE line parser, history windowing, envelope→render
mapping, readyz-gate error path, tracing bootstrap) lives in `chat_ui/` and
is covered by plain pytest — no browser, no Chainlit runtime:

```bash
uv run pytest tests/
```

The full UI is verified by the documented manual acceptance drill (chat →
streamed tokens → citations → one joined Phoenix trace); browser E2E infra is
out of scope by decision.
