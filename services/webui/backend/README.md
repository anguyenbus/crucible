# webui BFF — Projects, Upload, Ingestion

A thin FastAPI / Python 3.13 / uv **backend-for-frontend** for the Document
Analyser webui. It gives the Phase-1 frontend the one thing the platform lacked:
**projects/documents CRUD over SQLite** and an **upload → storage → ingest
bridge** onto the ingestion service's synchronous `POST /ingest`. No auth.

It consumes its sibling services (ingestion, orchestrator) **over HTTP only** —
it never imports their Python packages.

## Two-backend topology (important)

The frontend talks to **two** backends:

| Concern | Backend | How the browser reaches it |
| --- | --- | --- |
| **Chat** (`/query/stream`) | orchestrator | via the Phase-1 **Next.js route-handler proxy** (server-side; the orchestrator has no CORS) — **UNCHANGED**, this BFF does not touch it |
| **Projects / documents / upload** | **this BFF** | **browser-direct** via `NEXT_PUBLIC_BFF_URL` (the BFF is purpose-built WITH CORS) |

This BFF does **NOT** proxy `/query/stream`. Chat stays entirely on the Phase-1
path.

## Dependency firewall

The BFF is its own uv project (own `pyproject.toml` + committed `uv.lock`) and
consumes siblings only over their env-var URLs (`WEBUI_INGESTION_URL`,
`WEBUI_ORCHESTRATOR_URL`). It never imports `services.orchestrator` or
`services.ingestion`. Greppable gate (matches only prose in this package's own
docstrings, never an import):

```bash
grep -rEn "from services\.(orchestrator|ingestion)|import (orchestrator|ingestion)" services/webui/backend
# → empty
```

Service addressing lives in env vars only — there are no compose-only
service-name assumptions, so the independent-K8s-pod future needs no code change.

## Environment surface

Service-owned settings use the `WEBUI_` env prefix; standard AWS vars stay bare.

| Env var | Default | Purpose |
| --- | --- | --- |
| `WEBUI_INGESTION_URL` | `http://localhost:8001` | Ingestion base URL (`/ingest`, `/healthz`) |
| `WEBUI_ORCHESTRATOR_URL` | `http://localhost:8000` | Orchestrator base URL (recorded for parity; chat is NOT proxied here) |
| `WEBUI_UPLOAD_BUCKET` | `""` | S3 bucket for uploads (used when AWS creds are present) |
| `WEBUI_UPLOAD_DIR` | `webui-uploads` | Local fallback dir when credential-less (an absolute path is passed to ingestion) |
| `WEBUI_DB_PATH` | `webui.db` | SQLite store file (create-if-absent at startup) |
| `WEBUI_CORS_ORIGINS` | `http://localhost:3000` | Comma-separated allowed browser origins |
| `AWS_REGION` | `ap-southeast-2` | Standard AWS var — deliberately unprefixed |

Frontend-side: `NEXT_PUBLIC_BFF_URL` (defaults to `http://localhost:8002`) is the
address the browser uses to reach this BFF.

## Run

```bash
cd services/webui/backend
uv sync
uv run uvicorn app.main:app --host 0.0.0.0 --port 8002
```

Quick checks:

```bash
curl localhost:8002/healthz   # liveness (no dependency calls)
curl localhost:8002/readyz    # readiness — probes ingestion's /healthz over HTTP
```

## API surface

| Endpoint | Purpose |
| --- | --- |
| `GET /healthz` | Liveness — makes NO dependency calls |
| `GET /readyz` | Readiness — probes `GET {WEBUI_INGESTION_URL}/healthz`, surfaces the real dependency error |
| `POST /projects` | Create a project `{name}`; provisions the per-project index `proj-{id}` via ingestion and exposes `index_name` |
| `GET /projects` | List projects (each with `document_count` + `index_name`) |
| `GET /projects/{id}` | Project detail (with its documents + `index_name`) |
| `PATCH /projects/{id}` | Rename `{name}` |
| `DELETE /projects/{id}` | Delete (204) — drops the `proj-{id}` index via ingestion, then cascades its document rows |
| `GET /projects/{id}/documents` | List a project's documents |
| `GET /projects/{id}/documents/{doc_id}` | Get one document (with ingestion status) |
| `DELETE /projects/{id}/documents/{doc_id}` | Delete the document metadata row (204) |
| `POST /projects/{id}/documents` | **Upload bridge**: markdown OR PDF multipart → store → synchronous ingest into `proj-{id}` → persisted status |

Empty/whitespace project `name` → 400; unknown project/document → 404.

## Per-project index lifecycle (index_name, provision, drop-on-delete)

Each project owns a dedicated OpenSearch index named `proj-{id}` (the
lowercase hex UUID PK is already a valid index name). The name is STORED in a
new `projects.index_name` column — added by an **idempotent startup migration**
(a guarded `ALTER TABLE` that also backfills existing rows) — and EXPOSED on
every project record so the frontend can scope chat to it.

- **Provision on create.** `POST /projects` calls ingestion's
  `POST /indices/proj-{id}` (idempotent). If ingestion is unreachable the
  project row is rolled back and a `502` is returned — no project without its
  index.
- **Drop on delete.** `DELETE /projects/{id}` calls ingestion's
  `DELETE /indices/proj-{id}` FIRST (idempotent; missing index is a no-op) so a
  transient failure leaves the metadata intact and the delete is retry-safe,
  then cascades the document rows. This removes the CHUNKS, not just metadata
  (fixes the parked "index cleanup on delete" caveat). Document-level delete
  still removes metadata only.

The BFF NEVER holds an OpenSearch client and NEVER imports the sibling
packages — ALL index lifecycle is an HTTP call to ingestion (firewall intact).

## Upload bridge (synchronous, honest)

`POST /projects/{id}/documents` validates the upload is markdown (`.md`/
`.markdown`) OR PDF (`.pdf`) and under a coarse size bound (1 MiB — consistent
with ingestion's `max_chunks_per_doc=100` and Titan's per-chunk char/token
caps), stores the bytes (S3 when AWS creds + `WEBUI_UPLOAD_BUCKET` are present,
else a local absolute path under `WEBUI_UPLOAD_DIR`), then calls ingestion's
`POST /ingest` with the project's `index` target (`proj-{id}`) and **blocks**
on it (ingestion is synchronous — **no job queue, no fake progress**; ingestion
parses PDF natively via pypdf). The document row is persisted with a `status`:

- `indexed` — ingest ok (`skipped=false`); `chunks_indexed`/`sha256`/`ingest_doc_id` recorded
- `skipped` — dedup skip (`skipped=true`)
- `failed`  — ingestion returned a typed error; `failure_reason` + `failure_code` recorded

Ingestion's typed errors (**400** unsupported/oversize, **404** missing,
**502** Bedrock/OpenSearch upstream — and a killed/unreachable ingestion,
normalized to 502) map to a **persisted `failed` status returned as the document
record**, never a swallowed 500. Genuine BFF-internal faults still surface as 5xx.

### Honest doc_id caveat

Ingestion derives its `doc_id` from the exact `source` string, so the **same
file** ingested via a local path vs its `s3://` URI yields **different**
ingestion doc_ids. We do not paper over this — the `storage_uri` recorded on the
document row is the exact source we handed to ingestion.

## Caveats & out-of-scope

- **Project-level index cleanup on delete is now IN scope** (see above): a
  project delete drops its `proj-{id}` index via ingestion. Chunk-level
  (per-document) index cleanup remains OUT of scope — a document delete removes
  BFF metadata only.
- **Cross-index relevance caveat (documented, not solved).** A toggle-ON chat
  query fuses scores across `proj-{id}` + the general index of different
  size/distribution; cross-index score comparability is imperfect and score
  normalization is out of scope.
- No auth of any kind, no Supabase, no user scoping. No async/job-queue
  ingestion. SQLite only (no ORM); a Postgres swap is a documented non-concern,
  not built.

## Tests & quality gates

```bash
uv run pytest        # hermetic — ingestion HTTP + S3/boto3 are mocked; no live services
uv run ruff check .  # lint (target py313)
```

Any live-AWS tests are marked `requires_aws` and auto-skip without credentials
(ingestion's convention). All Group 1–6 tests run with **no live services**.

## Environment caveat (dev-only, not a product requirement)

This host's root filesystem is ~99% full, so the uv venv does **not** fit there.
Mirroring Phase 1's `node_modules`-on-tmpfs approach, the venv lives on tmpfs via
a **gitignored `.venv` symlink**; `uv.lock` **is** committed. The venv is wiped
on reboot — recreate it with `uv sync` (optionally pointing
`UV_PROJECT_ENVIRONMENT` at a tmpfs path). This is a dev-host constraint only.
