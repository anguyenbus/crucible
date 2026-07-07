# OpenSearch Index Contract v2 — "Bring Your Own Index"

**Status:** current (v2, 2026-07-07). Supersedes v1 (eval-owned GST index,
fixed field names, mandatory `_meta`/`char_span`).

The eval harness evaluates against an OpenSearch index it does **not** own or
build. Given an endpoint, an index name, and a search-pipeline name, the
retriever (`services/eval/dev/stubs/rag/opensearch_query.py`, exposed as
`eval-rag --rag opensearch`) runs hybrid BM25 + k-NN retrieval against your
index and feeds the results through the unchanged Phoenix-native evaluation
harness.

**A conforming external index needs ONLY the env vars listed below — no code
changes, no eval-side ingest, no CloudFormation.** The proof is the POC index
itself: `legal-rag-bench` is built and owned by a separate service
(`services/ingestion`), uses different field names than the eval defaults, and
has no `_meta`, no `char_span`, and no `chunk_id` field — and the eval path
runs against it end-to-end (4,876/4,876 documents ingested and verified;
`make opensearch-smoke` PASS with a correctly-cited answer).

---

## 1. Required index properties

Your index MUST provide all of the following.

| # | Requirement | Detail |
|---|-------------|--------|
| 1 | `doc_id` field | `keyword` field named exactly `doc_id` in `_source`. Joins retrieval hits back to gold passages. |
| 2 | Text field (BM25 leg) | One analyzed `text` field. Name configurable via `EVAL_OPENSEARCH_TEXT_FIELD` (default `text`; POC index uses `content`). |
| 3 | Vector field (k-NN leg) | One `knn_vector` field, **1024-dim**, `space_type: cosinesimil`. Name configurable via `EVAL_OPENSEARCH_VECTOR_FIELD` (default `embedding`; POC index uses `content_vector`). |
| 4 | Embeddings | **Bedrock Titan Text Embeddings V2** (`amazon.titan-embed-text-v2:0`, `dimensions: 1024`, `normalize: true`). The query-side embedder must match what the index was embedded with — the retriever embeds questions with Titan V2/1024 by default, so an index embedded with anything else produces garbage retrieval scored as a real result. The k-NN **engine may be `lucene` or `faiss`** — engine choice is transparent at query time (the POC index uses faiss with `ef_search` 512; the canary proved lucene on the same domain). |
| 5 | Deterministic `_id` | `_id = "{doc_id}:{chunk_idx}"`. The retriever derives the schema's `chunk_id` directly from the hit `_id` (verbatim pass-through) — no `chunk_id` field is required in the index. This also makes re-ingest an upsert, never a duplicate. |
| 6 | Hybrid search pipeline | A search pipeline with a normalization processor: `min_max` normalization + `arithmetic_mean` combination. Name via `EVAL_OPENSEARCH_PIPELINE` (default `hybrid-search-pipeline`; the POC pipeline uses weights `[0.5, 0.5]`). The retriever binds it **explicitly per request** via the `search_pipeline` query parameter — it never relies on `index.search.default_pipeline`. |

## 2. Optional index properties

None of these are required; the POC index has none of them.

| Property | Behavior when present | Behavior when absent |
|----------|----------------------|----------------------|
| `chunk_id` field | Ignored — `chunk_id` is always derived from `_id`. | Derived from `_id`. |
| `char_span` in `_source` | Emitted into the eval output chunk as `char_span` (`[start, end)` character offsets into the source document). Schema v1.1.0 made this optional. | Key simply omitted from the output chunk — still schema-valid. |
| Mapping `_meta` block | **Verified once at client init** (the retriever fetches the index mapping the first time it connects to a given endpoint+index). Recognized keys: `embedder` (model id string) and `dims` (integer). Each key is checked only if it exists: `embedder` != the query-side embedder model id, or `dims` != the query-side embedding dimension, raises `ValueError` with a loud, explanatory message — the run stops before any question is scored. Extra `_meta` keys are ignored. | Complete no-op — no warning, no failure. |

## 3. Score semantics

- The schema `score` is the **backend-native `_score` passed through
  unchanged**. For the hybrid path this means pipeline-normalized fused values
  (min_max per leg, then arithmetic mean).
- Scores are **ordering-only meaningful**. Do not compare them across
  backends — in particular they are NOT comparable with the ChromaDB
  stub-local backend's `1 - distance` scores — nor across pipelines or weight
  configurations.
- `rank` is the 0-based position in the returned hit list; hit order is
  preserved exactly as OpenSearch returned it.

## 4. Configuration surface (env vars — the complete list)

Python-side configuration is **env-only**. There is no config file, no flag,
and no CloudFormation lookup in the Python path.

| Env var | Required | Default | Meaning |
|---------|----------|---------|---------|
| `EVAL_OPENSEARCH_ENDPOINT` | **yes** (ValueError if unset) | — | Domain endpoint; scheme optional (`https://` is stripped). Connection is HTTPS on port 443. |
| `EVAL_OPENSEARCH_INDEX` | **yes** (ValueError if unset) | — | Target index name (POC: `legal-rag-bench`). |
| `EVAL_OPENSEARCH_PIPELINE` | no | `hybrid-search-pipeline` | Search-pipeline name, passed as the `search_pipeline` query parameter on every hybrid search. |
| `EVAL_OPENSEARCH_TEXT_FIELD` | no | `text` | Analyzed text field for the BM25 leg (POC: `content`). |
| `EVAL_OPENSEARCH_VECTOR_FIELD` | no | `embedding` | `knn_vector` field for the k-NN leg (POC: `content_vector`). |
| `AWS_REGION` | no | `ap-southeast-2` | Region for SigV4 signing and the Bedrock Titan embedder. |

Auth is SigV4 against service `es` via the boto3 credential chain (instance
role on the POC box) — no static keys, no fine-grained access control.

### Minimal checklist for an index provider

To point the eval harness at your own index, ALL you do is:

1. Build an index satisfying section 1 (six requirements) — chunk however you
   like, but embed with Titan V2 1024 normalized and write
   `_id = "{doc_id}:{chunk_idx}"`.
2. Create a `min_max` + `arithmetic_mean` search pipeline on the domain.
3. Ensure the caller has SigV4 network + IAM access to the domain (VPC/SG) and
   Bedrock access for `amazon.titan-embed-text-v2:0` (query embedding) and the
   generator/judge models.
4. Export the env vars:

   ```bash
   export EVAL_OPENSEARCH_ENDPOINT=<your-domain-endpoint>
   export EVAL_OPENSEARCH_INDEX=<your-index>
   export EVAL_OPENSEARCH_PIPELINE=<your-pipeline>        # if not hybrid-search-pipeline
   export EVAL_OPENSEARCH_TEXT_FIELD=<your-text-field>    # if not "text"
   export EVAL_OPENSEARCH_VECTOR_FIELD=<your-vector-field># if not "embedding"
   ```

5. Run `eval-rag --rag opensearch --slice pico` (Phoenix server running;
   `--top-k` defaults to 5). That's it — nothing else to configure.

Notes: `--rag opensearch` automatically selects the Bedrock Titan embedder for
query embedding (`_build_embedder` in `dev/cli/run_rag_eval.py`; `stub-local`
keeps sentence-transformers). `--force-reingest` is a documented **no-op** for
opensearch — eval never builds or mutates the index. The RagAdapter's
`corpus_dir` parameter is accepted and ignored (documented stub-ism).

## 5. What the retriever does (query shape and output)

The single query-construction function builds, for the default `hybrid` mode:

```json
{
  "size": <top_k>,
  "_source": {"excludes": ["<vector_field>"]},
  "query": {
    "hybrid": {
      "queries": [
        {"match": {"<text_field>": "<question>"}},
        {"knn": {"<vector_field>": {"vector": [<1024 floats>], "k": <top_k>}}}
      ]
    }
  }
}
```

sent with `?search_pipeline=<EVAL_OPENSEARCH_PIPELINE>`. The `_source`
exclusion means 1024 floats per hit are never hauled back over the wire.

A **pure-knn debug mode** exists (`mode="knn"`): the bare `knn` query with NO
`search_pipeline` parameter. It is a debug aid only — hybrid is the only
spec'd evaluation path.

Each hit maps (pure function, no I/O) to a schema chunk:
`chunk_id` = hit `_id`; `doc_id` = `_source.doc_id`; `text` = the configured
text field; `rank` = list position; `score` = raw `_score`; `char_span` only
when present in `_source`. The full output (answer via `ClaudeGenerator`,
citations, `timings_ms`, optional trace block) is validated against
`services/eval/app/contracts/rag_query_output.schema.json` **v1.1.0**
(`char_span` optional since 1.1.0) before it is returned.

> **Known limitation — `answer.citations` is always empty on this backend.**
> The citation extractor's pattern (`dev/stubs/rag/citations.py`) only matches
> legacy `*_chunk_N` ids and cannot match this contract's
> `{doc_id}:{chunk_idx}` ids, so the `citations` list is `[]` in every
> OpenSearch-backed output. The `[chunk_id]` markers Claude is prompted to
> emit are still visible inside `answer.text`, and no DeepEval metric consumes
> the `citations` field — retrieval/answer scores are unaffected. Fixing this
> means extending the extractor pattern to the contract id format.

## 6. POC worked example: the `legal-rag-bench` index

The reference conforming index, externally owned by the ingestion service
(`services/ingestion`, a standalone gitignored project):

- Mapping: `content` (analyzed text), `content_vector` (`knn_vector`, 1024,
  HNSW, `cosinesimil`, engine **faiss**, `index.knn.algo_param.ef_search: 512`),
  `doc_id` / `source_uri` / `sha256` (keyword), `chunk_index` (integer),
  `created_at` (date). **No `_meta`, no `char_span`, no `chunk_id` field.**
- `_id = "{doc_id}:{chunk_index}"`; sha256 dedup makes re-ingest safe.
- Pipeline: `hybrid-search-pipeline` — `min_max` normalization +
  `arithmetic_mean` combination, weights `[0.5, 0.5]` — created by the
  service's `scripts/create_index.py` (idempotent, safe to re-run).
- Ingest: `POST /ingest` per document → normalize → chunk (recursive token
  chunker, 800 tokens / 120 overlap, tiktoken) → embed (Titan V2 1024
  normalized, throttle-aware retries) → bulk-index → dedup.
- Eval-side env for this index: `EVAL_OPENSEARCH_INDEX=legal-rag-bench`,
  `EVAL_OPENSEARCH_TEXT_FIELD=content`,
  `EVAL_OPENSEARCH_VECTOR_FIELD=content_vector`,
  `EVAL_OPENSEARCH_PIPELINE=hybrid-search-pipeline`.
- Verified 2026-07-07: **4,876 / 4,876** corpus doc_ids indexed (exact
  set-difference verification, 0 missing / 0 extra; each passage fit a single
  800-token chunk) and `make opensearch-smoke` **PASS** end-to-end with a
  correctly-cited answer.

The eval harness queries this index **directly** (hybrid query + explicit
`search_pipeline`); it does NOT call the ingestion service's `/search` API.

## 7. Lifecycle loop and Makefile targets

The whole POC lifecycle, from the repo-root `Makefile`:

```
make opensearch-up          # create/update the CloudFormation stack (~15-25 min first time,
                            #   ~US$0.05/h while up; creates the service-linked role if needed)
  → provision + ingest      # make opensearch-ingest (service-driven, see below)
  → eval runs               # make opensearch-smoke; eval-rag --rag opensearch --slice pico|full
make opensearch-down        # delete the stack and wait — spend back to ZERO
```

| Target | What it does |
|--------|--------------|
| `opensearch-up` | Deploy/update the `opensearch-poc` stack (`infra/opensearch-poc/template.yaml`), guarding against `ROLLBACK_COMPLETE` and creating the OpenSearch service-linked role once per account. |
| `opensearch-status` | Stack status; distinguishes "deleted, no charges" from "cannot tell". |
| `opensearch-endpoint` | Print the stack outputs (incl. `DomainEndpoint`). |
| `opensearch-ingest` | **Service-driven POC ingest path**: provisions the index + `hybrid-search-pipeline` via the ingestion service's `create_index.py`, then drives the legal-rag-bench corpus through `POST /ingest` with the eval-side driver (`dev/scripts/ingest_legal_rag_bench_via_service.py`; `START=`/`LIMIT=` slice for resumable runs). Prerequisite: the ingestion service running locally (`INGESTION_INDEX_NAME=legal-rag-bench uv run uvicorn app.main:app ...` from `services/ingestion`). Re-materializing after teardown is `opensearch-up` → this target again — dedup + deterministic `_id`s make re-ingest safe. |
| `opensearch-ingest-fallback` | **FALLBACK / BYO-reference path only** — the eval-side GST ingest script (`dev/scripts/ingest_gst_to_opensearch.py`) that chunks (FixedChunker 512/0), embeds (Titan V2) and bulk-indexes itself. It shows how to build a conforming index yourself, but is NOT the POC path. Known limitations, documented not fixed: (a) its bulk 500-chunk batches are too heavy for t3.small.search and trigger 429s; (b) the vendored GST corpus is damaged (GST parked until re-supplied). |
| `opensearch-smoke` | Manual operational check that replaces any pytest live test: ONE hardcoded query end-to-end through the eval retriever (Titan V2 embed → hybrid search with explicit `hybrid-search-pipeline` → `ClaudeGenerator` answer → schema-valid v1.1.0 output), printing ranked hits, the answer, and a final `SMOKE PASS`/`SMOKE FAIL` line (non-zero exit on failure). There are no live tests in the pytest suite — all unit tests are mocked. |

### Endpoint-discovery split (deliberate)

- **Makefile = CloudFormation-aware convenience.** `opensearch-ingest`,
  `opensearch-ingest-fallback` and `opensearch-smoke` auto-resolve the
  `DomainEndpoint` output from the `opensearch-poc` stack when the relevant
  env var is unset, echoing which source won (env takes precedence).
- **Python = env-only.** The retriever and scripts read
  `EVAL_OPENSEARCH_ENDPOINT` (and the ingest driver reads
  `EVAL_INGESTION_SERVICE_URL`) and never touch CloudFormation — BYO-index
  users set env vars and never need the stack, the Makefile, or AWS
  CloudFormation permissions at all.
