# Ingestion Service

FastAPI service that ingests markdown from S3 URIs (or local paths), chunks and
embeds it with Bedrock Titan v2 (1024 dims), indexes into the `eval-poc`
OpenSearch 2.19 domain (default index `genai-ingestion-md`, or a per-request
target index), and exposes a hybrid (k-NN + BM25) retrieval endpoint with
min-max score normalization. PDF (native text via pypdf) is ingested
alongside markdown; per-project index lifecycle (provision/delete) is
service-owned so callers never touch OpenSearch directly.

API surface:

| Endpoint | Purpose |
| --- | --- |
| `POST /ingest` | Synchronous pipeline: fetch → parse (pypdf for PDF) → normalize → dedup → chunk → embed → bulk index → prune stale chunks. Optional `index` targets a specific index (default `INGESTION_INDEX_NAME`) |
| `POST /search` | Hybrid retrieval (knn + BM25, configurable weights), ranked scored chunks |
| `POST /indices/{index_name}` | Idempotent provision of a knn index + the hybrid-search pipeline (an existing index is a no-op) |
| `DELETE /indices/{index_name}` | Drop an index (a missing index is a benign no-op) |
| `GET /healthz` | Liveness check |

## Setup

Requires Python 3.13 and [uv](https://docs.astral.sh/uv/).

```bash
cd services/ingestion
uv sync
```

AWS access uses the boto3 credential chain (the EC2 instance role on the POC
host — no keys needed). Region defaults to `ap-southeast-2` (`AWS_REGION` to
override). The instance role needs `es:ESHttp*` on the domain, Bedrock
`InvokeModel` for Titan, and `s3:GetObject` on source buckets.

## Configuration

Service-owned settings use the `INGESTION_` env prefix (per-service prefix
convention); standard AWS vars stay bare. Defined in `app/config.py`:

| Env var | Default | Purpose |
| --- | --- | --- |
| `INGESTION_OPENSEARCH_HOST` | `vpc-eval-poc-cvvwd6y6gsjdyyrdodrfi7p22y.ap-southeast-2.es.amazonaws.com` | VPC OpenSearch endpoint (eval-poc domain) |
| `INGESTION_INDEX_NAME` | `genai-ingestion-md` | Chunk index name |
| `INGESTION_SEARCH_PIPELINE_NAME` | `hybrid-search-pipeline` | Named min-max normalization pipeline (fixed default weights) |
| `INGESTION_EMBED_MODEL_ID` | `amazon.titan-embed-text-v2:0` | Bedrock embedding model |
| `INGESTION_EMBED_DIMENSIONS` | `1024` | Embedding vector dimensions |
| `INGESTION_CHUNK_TOKENS` | `800` | Max tokens per chunk (LangChain `RecursiveCharacterTextSplitter` measured with tiktoken cl100k) |
| `INGESTION_CHUNK_OVERLAP` | `120` | Target token overlap between consecutive chunks (built from whole split pieces, so up to 120) |
| `INGESTION_MAX_CHUNKS_PER_DOC` | `100` | Oversized-document guard (400 before any embedding) |
| `INGESTION_SEARCH_TOP_K` | `5` | Default search result count |
| `INGESTION_KNN_WEIGHT` | `0.5` | Default knn weight (matches the named pipeline) |
| `INGESTION_KEYWORD_WEIGHT` | `0.5` | Default BM25 weight (matches the named pipeline) |
| `AWS_REGION` | `ap-southeast-2` | Standard AWS var — deliberately unprefixed |

## Running the service

```bash
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Then:

```bash
curl localhost:8000/healthz
curl -X POST localhost:8000/ingest -H 'content-type: application/json' \
  -d '{"source": "s3://bucket/docs/guide.md"}'
curl -X POST localhost:8000/search -H 'content-type: application/json' \
  -d '{"query": "how does hybrid search work", "top_k": 5, "knn_weight": 0.7, "keyword_weight": 0.3}'
```

`POST /ingest` accepts `{source, doc_id?, index?}` and returns
`{doc_id, sha256, chunks_indexed, skipped}`. `source` may be a `.md`/
`.markdown` (UTF-8) or a `.pdf` (native text extracted with pypdf) S3 URI or
local path; the optional `index` targets a specific OpenSearch index for the
dedup count, write, AND prune (absent ⇒ the default `INGESTION_INDEX_NAME`,
byte-identical to before). Error contract: `400` invalid/unsupported source or
over `max_chunks_per_doc`, `404` missing S3 object/local file, `422` a PDF with
no extractable native text (scanned/image-only — never a silent empty index;
OCR is out of scope), `502` Bedrock/OpenSearch upstream failure (with per-chunk
`_bulk` failure details in the body when a bulk write fails).

`POST /indices/{index_name}` provisions the per-project index (knn mapping +
the named hybrid-search pipeline) and is idempotent; `DELETE /indices/{index_name}`
drops it (idempotent). Index names are validated (lowercase, no leading
`-`/`_`/`+`) with a `400` before any OpenSearch call. EXPLICIT provisioning is
REQUIRED before first ingest into a new index: dynamic auto-create on first
write would map `content_vector` as a plain float array, not a `knn_vector`,
silently breaking retrieval. Both endpoints keep index lifecycle inside the
service that owns the OpenSearch client (the BFF calls them over HTTP).

`POST /search` with default weights uses the named `hybrid-search-pipeline`;
non-default weights send a temporary inline search pipeline in the request
body (a named pipeline has fixed weights). Empty index / no matches returns
`200` with `results: []`.

### Docker (build artifact only)

```bash
docker build -t ingestion-service .
docker run -p 8000:8000 ingestion-service   # needs AWS creds + VPC network at runtime
```

No deployment/Helm/CI wiring — the image is a build artifact per plan.md.

## Operational scripts

### `scripts/create_index.py` — idempotent provisioning

Creates the chunk index (`index.knn: true`, HNSW `content_vector` dim 1024,
cosinesimil, engine faiss with nmslib fallback) and the named
`hybrid-search-pipeline` (min-max normalization, 0.5/0.5 weights). Safe to
re-run: existing index/pipeline are skipped cleanly (no-op), exit 0.

```bash
uv run scripts/create_index.py
```

### `scripts/ingest_sample.py` — CLI ingest helper

Ingests one local or S3 markdown file by invoking the ingest pipeline
directly, in-process (same handler as `POST /ingest`; no running service
needed). Exits 0 on success — including a dedup skip — and 1 on failure,
printing the API-contract response/error as JSON.

```bash
uv run scripts/ingest_sample.py /abs/path/sample.md
uv run scripts/ingest_sample.py s3://bucket/docs/guide.md
uv run scripts/ingest_sample.py s3://bucket/docs/guide.md --doc-id mydoc
```

## Deterministic document and chunk IDs

When `doc_id` is not supplied, it is derived as a truncated SHA-256 (16 hex
chars) of the canonical source URI string, and every chunk's OpenSearch `_id`
is `{doc_id}:{chunk_index}` (see `app/pipeline/hash_dedup.py`). Deterministic
IDs are what make retries and re-ingest safe: partial failures self-heal by
overwriting the same `_id`s, with no rollback logic. Changed content is
indexed first (overwriting in place) and stale-sha256 leftovers are pruned
only afterwards — worst case is stale-but-searchable, never a data-loss
window. The dedup skip requires BOTH a sha256 match and a complete chunk
count, so a partial prior run never masquerades as a finished ingest.

**Accepted POC caveat (not a bug):** because the doc_id hashes the exact
source URI string, ingesting the same file via a local path (e.g.
`/home/admin/docs/guide.md`) and via its S3 URI
(`s3://bucket/docs/guide.md`) produces two different doc_ids — the two
ingests are treated as distinct documents.

## Tests

```bash
uv run pytest -m "not requires_aws"   # pure/mocked tests, no AWS needed
uv run pytest                          # full suite incl. live smoke/E2E tests
```

Live tests are marked `requires_aws` and skip automatically when no AWS
credentials are present. The live end-to-end tests
(`tests/test_e2e_live.py`) use unique temp-file sources and delete their
documents from the index afterwards, so runs are repeatable.

## Phase 0 verification scripts

Committed pass/fail checks under `scripts/` — each prints a PASS/FAIL summary
and exits 0/1, and doubles as a `requires_aws` smoke test via
`tests/test_phase0_checks.py` (skipped automatically without credentials).

```bash
uv run scripts/check_connectivity.py     # DNS + SigV4 GET /_cluster/health -> 200
uv run scripts/check_titan.py            # Titan v2 dimensions=1024 + token-limit behavior
uv run scripts/check_search_pipeline.py  # min-max normalization pipeline + inline pipeline support
uv run pytest tests/test_phase0_checks.py
```

Override the OpenSearch endpoint with `INGESTION_OPENSEARCH_HOST` if needed.

### DNS blocker resolution (Phase 0, resolved)

The plan.md blocker "VPC OpenSearch endpoint DNS resolution fails from this
host" was caused by a **typo in the recorded endpoint hostname**, not by a
resolver problem:

- Recorded (wrong): `vpc-eval-poc-cvvwd6y6ygsjdyyrdodrfi7p22y.ap-southeast-2.es.amazonaws.com`
- Actual (from `aws opensearch describe-domain --domain-name eval-poc`):
  `vpc-eval-poc-cvvwd6y6gsjdyyrdodrfi7p22y.ap-southeast-2.es.amazonaws.com`
  (the recorded name has an extra `y` after `cvvwd6y6`)

Diagnosis: `resolvectl status` showed the host correctly using the VPC
resolver `172.31.0.2`; querying that resolver directly for the recorded name
returned NXDOMAIN (the record genuinely never existed), while the
authoritative endpoint from `describe-domain` resolves to a private IP and
answers signed requests with HTTP 200. No `/etc/hosts` entry or
systemd-resolved change was needed. `check_connectivity.py` now detects
stale/typo'd hostnames by comparing against `describe-domain` on any DNS
failure.

### Titan findings worth knowing

- `amazon.titan-embed-text-v2:0` accepts `{"inputText": ..., "dimensions": 1024}`
  and returns a 1024-dim vector.
- The 8192-token input limit is a **hard error** (`ValidationException`), not a
  truncation — the embed stage must guard it client-side.
- There is an additional request-level cap of **50,000 characters** on
  `inputText` (also a `ValidationException`); ordinary prose hits it around
  ~5,900 tokens, before the token limit. Both limits must be guarded (800-token
  chunks stay far below either).

### OpenSearch 2.19 pipeline findings

- Named min-max `normalization-processor` search pipelines create and execute
  correctly for hybrid (knn + match) queries.
- Temporary (inline) search pipelines in the search request body work —
  this is what lets `POST /search` honor per-request weights.
