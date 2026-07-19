# Document Analyser

A Next.js 15 / React 19 / TypeScript 5 (strict) / Tailwind 4 / shadcn/ui chat
frontend over the orchestrator's `POST /query/stream`. Frontend only (Phase 1):
the browser reaches the orchestrator **exclusively** through a Next.js
route-handler proxy — the orchestrator has no CORS and is never modified.
Harvested and adapted from the MIT-licensed `references/donna/frontend`
(see `NOTICE`).

## Running

```bash
npm install
cp .env.example .env.local     # set ORCHESTRATOR_URL for your orchestrator
npm run dev                    # Next.js dev server on http://localhost:3000
```

Other scripts: `npm run build`, `npm start` (both on port 3000),
`npm run lint` (ESLint 9), `npm run typecheck` (tsc strict), `npm test`
(Vitest, browser-free unit suite).

## Environment

| Variable | Scope | Default | Purpose |
| --- | --- | --- | --- |
| `ORCHESTRATOR_URL` | **server-side only** | `http://localhost:8000` | Base URL of the orchestrator. Read only inside the proxy (`src/lib/serverEnv.ts`). **Never** `NEXT_PUBLIC_` — that would leak the address into the client bundle and break the firewall. |
| `NEXT_PUBLIC_WEBUI_CONFIG_REF` | client-readable | `legal-rag-default-1.8.0` | The pinned pipeline config ref sent on every request. A non-secret identifier, not an address. No in-UI picker. |
| `NEXT_PUBLIC_GENERAL_INDEX` | client-readable | `legal-rag-bench` | The GENERAL retrieval index added to the scope when the general-index toggle is ON. A non-secret location identifier read client-side so it rides in the request body verbatim; no literal is hardcoded in component logic. |
| `NEXT_PUBLIC_BFF_URL` | client-readable | `http://localhost:8002` | Base URL of the BFF (projects/documents/upload), reached browser-direct. The frontend reads each project's `index_name` from the BFF; index names never come from the orchestrator. |

## Configuration modes

The config ref is env-pinned (no UI picker). The provenance line under every
answer shows the **live** ref returned in the `final` envelope, so the mode is
always visible on screen.

| Config ref | Guardrails | Delivery | Guardrail pod |
| --- | --- | --- | --- |
| `legal-rag-default-1.8.0` **(default)** | NeMo-all (in/out rails) | **buffered** final (zero `token` events, one `final`) | **required** |
| `legal-rag-default-1.4.0` | in-house output guard | **buffered** final | not needed |
| `legal-rag-default-1.2.0` | none | **live token streaming** — the ONLY mode where tokens stream | not needed |

Both output-guard families buffer (verified against the orchestrator): the UI
shows an honest "waiting on guarded generation" state, then the full answer at
once — never a fake typewriter. `1.2.0` streams real token deltas as received.

## Project-scoped chat (index scope + general-index toggle)

A chat is scoped to a selected project's own OpenSearch index. The scope bar
under the chat input carries a project selector and a **general-index toggle**
(DEFAULT OFF — project docs only; per-session UI state, not persisted). The
scope maps to the orchestrator's optional `retrieval_indices` request field by
the pure, browser-free `src/lib/retrievalScope.ts`:

- **No project selected** (general chat) → field OMITTED → the orchestrator
  keeps its single-`legal-rag-bench` default, byte-identical to before.
- **Project selected, toggle OFF** → `[proj-{id}]` (project docs only).
- **Project selected, toggle ON** → `[proj-{id}, <general>]`, where `<general>`
  is `NEXT_PUBLIC_GENERAL_INDEX` (default `legal-rag-bench`).

`retrieval_indices` rides in the request body the browser POSTs to the Next
proxy, which forwards it VERBATIM to the orchestrator — no proxy change was
needed. Cross-index score comparability under toggle-ON is a documented,
unsolved caveat (two indices of different size/distribution).

## Dependency firewall

The browser talks only to same-origin proxy routes; those are the sole path to
the orchestrator, and the Phase-2 BFF later absorbs them:

- `POST /api/query/stream` → forwards to `${ORCHESTRATOR_URL}/query/stream`.
  Client abort propagates to the upstream fetch (Stop actually cancels Bedrock);
  the `text/event-stream` is passed through per-event, uncompressed, byte-verbatim
  (never rewritten or enriched); pre-stream HTTP errors (404/422/500/502/503,
  `Retry-After` on 503) pass through as plain HTTP — no SSE body is fabricated.
- `GET /api/readyz` → forwards to `${ORCHESTRATOR_URL}/readyz`, passing the real
  dependency error body through for the banner gate.

`ORCHESTRATOR_URL` is server-side only. No compose service-name assumptions are
baked in — the independent-K8s-pod future is env-only.

## Honesty rules

Mirrored from the Chainlit `demo_ui`, binding throughout:

- No simulated streaming and no typewriter drip — live `token` deltas render as
  real text exactly as received.
- Citations, numbered source cards (rank/score/text), per-stage `timings_ms`,
  and provenance render **only** from the `final` envelope; zero citations is an
  honest state.
- Inline `[n]` citation markers are driven strictly by the envelope `citations`
  array (mirroring `number_citations()`): stable 1-based numbering by first
  marker appearance among chunks both cited and retrieved; a marker without a
  matching source degrades to plain text — never a crash, never a fabricated
  link. Numbered source cards remain regardless.
- The guardrail chip (action + rule count) renders **only** when
  `final.guardrail_decisions` is non-empty (including the input-guard refusal
  `final`, which carries zero tokens). No rationale panels or trace links (Phase 3).
- On `error` or user abort, the partial text stays visible marked **INCOMPLETE**
  and the turn is dropped from history. Every stream ends in exactly one terminal
  event (`final` | `error`).
- Multi-chat state is in-memory and session-scoped (lost on refresh); each chat
  carries its own bounded history (20 turns / 8,000 chars per turn, oldest
  dropped first) built only from its own completed turns.

## Testing

Browser-free unit tests only (Vitest), mirroring `demo_ui`'s pytest pattern —
the SSE parser, envelope→render mapping, citation numbering/degradation, history
windowing, the proxy handlers, and the readyz gate all live in plain TS modules
separate from React. `npm test` runs the full suite; there is no browser E2E —
the manual acceptance drill below is the standing acceptance test.

## Manual acceptance drill

Run against a live orchestrator (guardrail pod up for the `1.8.0` default),
recording evidence in `verifications/acceptance-drill.md`:

1. Start the orchestrator (live AWS/OpenSearch); `npm run dev` on `:3000` with
   `ORCHESTRATOR_URL` set. Confirm the readyz banner is absent when `/readyz` is
   200 and appears with the real error when it is not.
2. **Buffered default (`1.8.0`):** ask a question — honest waiting state (zero
   tokens) then the full answer at once; clickable inline `[n]` citations resolve
   to numbered source cards; `timings_ms` + provenance show the live `1.8.0` ref;
   the guardrail chip renders. Trigger the input-guard refusal and confirm one
   refusal `final` renders with the chip and no hang.
3. **Live-token (`1.2.0`):** re-pin via `NEXT_PUBLIC_WEBUI_CONFIG_REF`; confirm
   tokens stream live as real deltas; provenance shows the `1.2.0` ref;
   citations/sources/timings appear only after `final`.
4. **Multi-chat, Stop, errors:** create multiple chats; confirm switching swaps
   the message list + history with no cross-chat leakage and that chats are lost
   on refresh (accepted). Press Stop mid-stream — partial text marked INCOMPLETE,
   turn dropped, generation stops upstream. Force a dependency error and confirm
   the typed error render + INCOMPLETE partial + dropped turn.
5. Confirm no user-visible "Donna"/"Crucible"; `grep -ri supabase services/webui/`
   empty; the Chainlit `demo_ui/` still runs untouched.
