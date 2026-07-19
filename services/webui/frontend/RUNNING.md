# Running the full stack (make, step by step)

How to bring up the Document Analyser web UI and every backend it needs, using
the `make` targets in this directory's [`Makefile`](./Makefile).

**Run every `make` command below from this directory** (`services/webui/frontend/`) —
that is where the Makefile lives. Each long-running service takes over its
terminal (foreground `uvicorn`/`npm` with `--reload`), so you need **one terminal
per service**. Open a new terminal for each numbered step.

---

## 1. What talks to what

```
 Browser ──browser-direct (CORS)──▶ BFF :8002 ──HTTP──▶ ingestion :8001 ──HTTP──▶ parser :8003
    │                                  │                     │
    │                                  └─────────────────────┴──▶ OpenSearch + Bedrock (AWS)
    └──server-side proxy (/api/*)──▶ orchestrator :8000 ──▶ OpenSearch + Bedrock (AWS)
                                          │
                                          └── (1.8.0 guarded chat only) NeMo guard :8080
```

| Service      | Port | make target        | Needs                                            |
|--------------|------|--------------------|--------------------------------------------------|
| UI (Next.js) | 3000 | `make dev`         | orchestrator (chat) + BFF (projects)             |
| orchestrator | 8000 | `make orchestrator`| AWS (Bedrock + OpenSearch); guard for 1.8.0 chat |
| ingestion    | 8001 | `make ingestion`   | AWS (Bedrock embed + OpenSearch) + parser        |
| BFF          | 8002 | `make bff`         | ingestion + orchestrator                         |
| parser       | 8003 | `make parser`      | AWS (Textract) for scanned/garbled pages only    |
| NeMo guard   | 8080 | `make guardrail-pod`| AWS (Bedrock Haiku) — **only** for 1.8.0 chat   |

There is **no collision** between ports. Every variable (URLs, config ref, port,
region) can be overridden on the command line, e.g.
`make dev ORCHESTRATOR_URL=http://127.0.0.1:8000`.

---

## 2. Pick your stack

You rarely need all six. Choose the smallest that covers what you're doing:

- **Chat only** → orchestrator + UI. (Add the guard pod if you keep the 1.8.0
  default; see §6.)
- **Projects + upload + "Compare Documents for Contradiction"** → orchestrator +
  ingestion + parser + BFF + UI. This is the **full stack**; the rest of this
  guide brings it up.

The new **Compare Documents for Contradiction** feature needs the **orchestrator**
(it runs the LangChain decompose-then-verify pipeline on Bedrock) and the **BFF**
(which forwards both documents' indexed text). The documents must already be
uploaded and `indexed`, which needs **ingestion + parser** too.

---

## 3. Prerequisites (do these once)

### 3a. AWS credentials
The orchestrator, ingestion, and parser use the **ambient AWS credential chain**
in region **ap-southeast-2**. Before starting them, make sure valid credentials
are exported in each of their terminals (or available via your SSO profile):

```bash
export AWS_ACCESS_KEY_ID=…
export AWS_SECRET_ACCESS_KEY=…
export AWS_SESSION_TOKEN=…          # if using temporary/SSO creds
# or:  export AWS_PROFILE=your-profile
```

You need access to Bedrock (Claude Sonnet + Titan embeddings + Haiku), OpenSearch
(the `opensearch-poc` domain — the Makefile resolves its endpoint from
CloudFormation automatically), and Textract (only for scanned pages).

### 3b. Frontend dependencies

```bash
make install          # npm install — node_modules is on tmpfs, so RERUN after a reboot
```

### 3c. Parser heavy dependencies (one-time, multi-GB)

```bash
make parser-sync      # installs docling + torch + rapidocr into the parser's .venv
```

> The orchestrator, ingestion, and BFF venvs are assumed already provisioned
> (`.venv/` present in each service). Only the parser needs this explicit sync,
> and only the first time. If a service venv is missing, run `uv sync` in that
> service directory.

---

## 4. Start the backends (one terminal each)

Order isn't strictly enforced (each service boots independently and hot-reloads),
but this order avoids "dependency not ready" noise on the first readiness probe.

### Terminal 1 — orchestrator (:8000)
```bash
make orchestrator
```
Resolves the OpenSearch endpoint from the `opensearch-poc` CFN stack and wires the
NeMo guard URL. Serves `/query`, `/analyze`, and **`/compare`** (the contradiction
comparison). Needs AWS creds.

### Terminal 2 — parser (:8003)
```bash
make parser
```
Docling + Textract. Digital PDFs / DOCX / XLSX / HTML parse **offline**; only
scanned or garbled pages call Textract. Requires `make parser-sync` to have run.

### Terminal 3 — ingestion (:8001)
```bash
make ingestion
```
Parses, chunks, embeds, and indexes uploads. It routes every non-`.md` upload to
the parser at `$(PARSER_URL)`, so **start the parser first** — otherwise
PDF/DOCX/etc. ingest fails at the parse stage (plain `.md` still works).

### Terminal 4 — BFF (:8002)
```bash
make bff
```
Projects, upload, index lifecycle (SQLite), and the `/projects/{id}/compare`
proxy. It reads `WEBUI_INGESTION_URL` / `WEBUI_ORCHESTRATOR_URL` at process start.

> **Important:** the BFF (and ingestion) cache their settings at startup.
> `--reload` restarts on **code** edits but does **not** re-read environment
> variables. If you change a URL/port variable, fully **stop and restart** that
> service — a hot reload won't pick it up.

---

## 5. Start the UI (:3000)

### Terminal 5 — Next.js dev server
```bash
make dev
```
Serves both chat and projects on <http://localhost:3000>. It injects
`ORCHESTRATOR_URL` (server-side proxy) and `NEXT_PUBLIC_BFF_URL` (browser-direct)
for you, pinned to the buffered default config `legal-rag-default-1.8.0`.

Variant: `make dev-stream` pins the live-token streaming config
(`legal-rag-default-1.2.0`) — useful for watching chat tokens stream in.

---

## 6. NeMo guard — only for 1.8.0 guarded chat

The default chat config (`1.8.0`) expects the guard pod. **Projects, upload, and
Compare-for-Contradiction do NOT use it** — skip this unless you're exercising the
guarded chat path.

- To run guarded chat: **Terminal 6** → `make guardrail-pod` (serves :8080), and
  start it **before** the orchestrator so the wiring resolves.
- To skip the guard entirely, run chat on an unguarded config, e.g.
  `make dev-stream` (1.2.0) or `make dev CONFIG_REF=legal-rag-default-1.2.0`.

---

## 7. Verify everything is up

```bash
make readyz     # probes orchestrator /readyz and BFF /readyz (BFF in turn probes ingestion)
```

Then in the browser (<http://localhost:3000>):

1. **Projects** → create a project.
2. Open it → **Documents** tab → **Upload document**. Upload **two** files
   (PDF/DOCX/MD/…). Wait until both rows show **`indexed`** (the list polls
   through parsing → chunking → embedding → indexing).
3. Click **Compare Documents for Contradiction** (enabled once ≥2 docs are
   indexed) → select the two documents → **Compare**. A typed table of
   contradictions (Temporal / Numerical / Authority / Process / Policy Reversal /
   Specificity) with the exact conflicting quote from each document is rendered.

---

## 8. Offline gates (no services needed)

```bash
make check       # frontend: vitest + typecheck + lint + build
make bff-check   # BFF: pytest + ruff (tmpfs venv)
```

---

## 9. Shut down

Press `Ctrl-C` in each service terminal. There is nothing to clean up — the BFF's
state is a local SQLite file and uploaded bytes are under the BFF's upload dir.

---

## 10. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `parser unreachable at http://parser:8000/...` | ingestion started before the Makefile wiring, or the parser isn't up. Start `make parser`, then **restart** `make ingestion` (env is read once at startup). |
| PDF/DOCX upload fails but `.md` works | parser not running, or `make parser-sync` never ran. Bring the parser up. |
| Compare button disabled | Fewer than **two** documents are `indexed`. Upload more / wait for ingest to finish. |
| Compare returns a 502 / dependency error | orchestrator can't reach Bedrock (missing/expired AWS creds or no model access in `ap-southeast-2`). Re-export creds and restart `make orchestrator`. |
| Chat 5xx on the 1.8.0 default | guard pod not running. Start `make guardrail-pod`, or switch to an unguarded config (§6). |
| `No module named uvicorn` (parser) | parser venv not synced. Run `make parser-sync`. |
| Changed a URL/PORT var but nothing happened | `--reload` doesn't re-read env. Fully stop and restart that service. |
| Everything 404s after a reboot | `node_modules` is on tmpfs. Re-run `make install`. |

---

### Quick reference — full stack, six terminals

```bash
# one-time
make install
make parser-sync

# terminal 1   (AWS creds exported)
make orchestrator
# terminal 2   (AWS creds exported)
make parser
# terminal 3   (AWS creds exported)
make ingestion
# terminal 4
make bff
# terminal 5
make dev
# terminal 6   (only for 1.8.0 guarded chat)
make guardrail-pod

# verify
make readyz
```
