# Comparing Chunking Strategies with OpenSearch + Phoenix

**Date:** 2026-07-07 · **Dataset:** legal-rag-bench (4,876 passages, `nano` slice = 10 QA) · **Status:** harness proven; nano-scale scores below

**📺 Video demo:** [watch the walkthrough on YouTube](https://www.youtube.com/watch?v=BSDB7oXit8Q)

This document records how we compared two chunking strategies end-to-end — ingest → OpenSearch index → retrieval → LLM-judged evaluation → side-by-side comparison in Phoenix — and serves as a **guide for anyone who wants to compare a new approach** (a different chunker, embedder, top-k, hybrid weighting, …). The pattern is always the same: *one index per approach, one Phoenix experiment per approach, all experiments on the same Phoenix dataset.*

---

## 1. The idea

A RAG evaluation compares **configurations**, not just models. To attribute a score difference to one variable (here: the chunking strategy), everything else must stay fixed:

| Held constant | Value |
|---|---|
| Corpus | legal-rag-bench, 4,876 passages |
| Embeddings | Bedrock Titan V2 (`amazon.titan-embed-text-v2:0`), 1024-dim |
| Retrieval | hybrid BM25 + k-NN, `hybrid-search-pipeline` (0.5/0.5 min-max mean), top-k 5 |
| Generator | Claude (Bedrock) via the eval retriever |
| Judge | Bedrock Claude, DeepEval metrics |
| Questions | the same 10 (`--slice nano`) |

| Varied (one axis) | Arm |
|---|---|
| No chunking (whole passages) | `nano-whole-passage-baseline` |
| Recursive splitter, 256 tokens / 50 overlap | `nano-recursive-256-50` |
| Fixed token windows, 256 tokens / 50 overlap | `nano-fixed-256-50` |

**Why three arms and not two?** A corpus measurement reshaped the design: legal-rag-bench passages are short (median ≈ 280 tokens, max ≈ 610). At the ingestion service's default 800/120, the "recursive chunker" never actually splits anything — the pre-existing index is effectively *whole passages*. Comparing "recursive 800/120 vs fixed 256/50" would therefore have conflated chunk **size** with chunk **strategy**. Running both candidate strategies at the *same* 256/50 isolates the strategy variable, and the existing index becomes a free whole-passage baseline.

> **Lesson: measure your corpus before designing the experiment.** If most documents fit in one chunk, a "chunking comparison" at that size compares nothing.

### The two strategies

Both live in the ingestion service (`services/ingestion/app/pipeline/chunk.py`), selected by the `INGESTION_CHUNK_STRATEGY` env var (pydantic setting `chunk_strategy`, default `recursive`):

- **`recursive`** — LangChain `RecursiveCharacterTextSplitter` over tiktoken cl100k tokens. Splits at natural separators (`\n\n`, `\n`, `. `, ` `) and merges pieces up to the token budget. Overlap is built from whole pieces, so it's *up to* 50 tokens, not exact. Respects sentence/paragraph boundaries.
- **`fixed`** — LangChain `TokenTextSplitter`. Hard cl100k token windows: every chunk is exactly 256 tokens (last one shorter), overlap exactly 50. Ignores separators and **cuts mid-sentence by design** — that is the hypothesis under test.

---

## 2. What was created in OpenSearch

One index per arm, all on the shared POC domain (`eval-poc`, VPC-only, t3.small.search, managed by the `opensearch-poc` CloudFormation stack — `make opensearch-up`/`opensearch-status`). Each index is provisioned by the ingestion service's `scripts/create_index.py`, which creates two things: the **index** with the service's standard mapping (fields: `doc_id`, `chunk_index`, `content`, `content_vector` — a 1024-dim `knn_vector` using the faiss engine — `sha256`, `source_uri`, `created_at`; `_id = "{doc_id}:{chunk_index}"`), and the **`hybrid-search-pipeline`** search pipeline (min-max normalization + 0.5/0.5 arithmetic-mean combination of BM25 and k-NN scores; created once, shared by all indexes):

| Index | Strategy | Chunks | Distinct docs | Chunks/passage |
|---|---|---|---|---|
| `legal-rag-bench` | none in practice (recursive 800/120; every passage fits) | 4,876 | 4,876 | 1.00 |
| `lrb-rec-256-50` | recursive 256/50 | 8,814 | 4,876 | 1.81 |
| `lrb-fix-256-50` | fixed 256/50 | 8,697 | 4,876 | 1.78 |

Note the two 256-token arms produce **nearly the same number of chunks** — the strategies differ in *where the cuts land*, not how much they cut. The `1.8×` ratio versus `1.0×` in the baseline was the early sanity check that the chunking env was actually applied.

**Verification after each ingest** (do not skip this — a partial index silently biases every downstream score):

```bash
EP=<domain-endpoint>
# total chunks
uvx awscurl --service es --region ap-southeast-2 "https://$EP/<index>/_count"
# distinct source docs must equal the corpus size (4,876)
uvx awscurl --service es --region ap-southeast-2 "https://$EP/<index>/_search?size=0" \
  -X POST -H "Content-Type: application/json" \
  -d '{"aggs":{"docs":{"cardinality":{"field":"doc_id","precision_threshold":10000}}}}'
```

---

## 3. How each arm was ingested

The **ingestion service owns the index** (BYO-index contract: eval only reads). Per arm: create the index, start the service with that arm's chunking env, drive the corpus through `POST /ingest`.

```bash
# 1. Provision index + search pipeline (from services/ingestion)
INGESTION_INDEX_NAME=lrb-fix-256-50 uv run python scripts/create_index.py

# 2. Start the service configured for this arm (from services/ingestion)
INGESTION_INDEX_NAME=lrb-fix-256-50 \
INGESTION_CHUNK_STRATEGY=fixed \
INGESTION_CHUNK_TOKENS=256 \
INGESTION_CHUNK_OVERLAP=50 \
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000

# 3. Drive all 4,876 passages through the service (from services/eval)
EVAL_INGESTION_SERVICE_URL=http://127.0.0.1:8000 \
uv run python -m dev.scripts.ingest_legal_rag_bench_via_service
```

(For the recursive arm: same, with `INGESTION_CHUNK_STRATEGY` omitted/`recursive` and index `lrb-rec-256-50`.)

Operational notes from the actual runs — expect these on a t3.small:

- **~1.2 s/passage, ≈ 1.5–2 h per arm.** The pipeline is sequential (dedup search → 1 Titan call per chunk → bulk index with `refresh=wait_for` → prune) and the node is tiny.
- **Transient HTTP 502s** ("search index temporarily unavailable") every few hundred docs. The driver prints `Resume with --start N`; wrap it in a retry loop. Docs reported failed have usually indexed anyway (the 502 fires in the post-index prune) — the sha256 dedup makes re-driving safe and idempotent.
- **Restart from `--start 0` after any interruption is cheap**: dedup fast-skips complete docs at ~30 ms each and re-ingests only partial ones. A mid-ingest machine reboot cost us nothing but time.

---

## 4. How the evaluation was run

The eval side never builds indexes; it points its provider-agnostic retriever at each index via env vars. Phoenix (Arize) must be running (`localhost:6006`). One command per arm — **only `EVAL_OPENSEARCH_INDEX` and `--experiment-name` change**:

```bash
cd services/eval
export EVAL_OPENSEARCH_ENDPOINT=<domain-endpoint>
export EVAL_OPENSEARCH_PIPELINE=hybrid-search-pipeline
export EVAL_OPENSEARCH_TEXT_FIELD=content
export EVAL_OPENSEARCH_VECTOR_FIELD=content_vector
export AWS_REGION=ap-southeast-2

EVAL_OPENSEARCH_INDEX=lrb-fix-256-50 \
uv run python -m dev.cli.run_rag_eval \
  --slice nano --rag opensearch \
  --config dev/fixtures/eval_config.yaml \
  --experiment-name nano-fixed-256-50 \
  --output-dir results/eval_rag/chunking_nano/nano-fixed-256-50
```

What happens per run: the 10 nano questions are loaded into (or fetched from) the Phoenix dataset `legal-rag-bench-nano`; for each question the retriever embeds it (Titan V2), runs the hybrid query against the arm's index, generates an answer (Claude), and four DeepEval judge metrics score the result (faithfulness, context_precision, context_recall, answer_relevancy). Results are exported as CSV/JSON/parquet under `--output-dir`.

**The naming is the comparison mechanism.** All arms share one Phoenix *dataset* (auto-named `legal-rag-bench-nano`), and each arm is a distinctly named *experiment* on it (`--experiment-name` was added for exactly this). Phoenix then gives you:

- **Experiments table** (`/datasets/<id>/experiments`): one row per arm, mean score per metric — the headline comparison.
- **Compare view**: select 2+ experiments → per-question, side-by-side retrieved chunks, generated answers, and metric scores. This is where you learn *why* an arm won or lost a question.

Each run takes ~9–10 minutes (dominated by judge calls).

---

## 5. Results (nano, 10 questions, top-k 5)

| metric | whole-passage baseline | recursive 256/50 | fixed 256/50 |
|---|---|---|---|
| faithfulness | **1.000** | 0.976 | 0.993 |
| context_precision | **0.980** | 0.957 | 0.973 |
| context_recall | 0.767 | 0.667 | **0.800** |
| answer_relevancy | 1.000 | 1.000 | 1.000 |

### Which one is better?

**At this sample size: no strategy separates from the others — and saying so is the correct conclusion.** Only 4 of 10 questions diverge on recall, and every divergence is a single judge-verdict flip:

| query | baseline | recursive | fixed | note |
|---|---|---|---|---|
| Q3 | 0.5 | 0.5 | **1.0** | fixed's mid-sentence window happened to capture the needed content |
| Q6 | 1.0 | **0.5** | 1.0 | recursive's boundary split separated a needed fact from the retrieved chunk |
| Q9 | 1.0 | **0.5** | 1.0 | same failure shape as Q6 |
| Q10 | 0.5 | 0.5 | 0.33 | hard for everyone — worth reading in the compare view |

So the apparent ordering (fixed > baseline > recursive on recall) rests on ~2 judge verdicts. What the run does support:

1. **On this corpus, chunking at 256/50 does not obviously help** — passages are short enough that whole-passage retrieval already scores near the top on precision and faithfulness. Chunking earns its keep on *long* documents; this corpus barely has any.
2. **Recursive's two recall losses (Q6, Q9) are a real failure shape worth watching**: separator-based splitting can strand a fact right at a paragraph boundary, and with top-k fixed at 5, the complementary chunk doesn't always make the cut.
3. **A decision needs `--slice full` (100 questions).** The indexes are already built; three more eval runs is all it takes. At n=100 a 0.1 recall gap is signal; at n=10 it's one flipped verdict.

Artifacts: Phoenix UI → dataset `legal-rag-bench-nano` → Experiments; file exports under `services/eval/results/eval_rag/chunking_nano/<experiment-name>/` (per-question CSV, summary JSON, parquet).

---

## 6. Checklist: comparing your own approach

1. **Measure the corpus first** (token-length distribution). Confirm your variable will actually do something at your chosen parameters.
2. **Change exactly one axis per arm.** If you change chunker *and* size, you can't attribute the difference.
3. **One index per arm**, named after the configuration (`lrb-fix-256-50` beats `test2`). Never re-ingest into a shared index.
4. If your variable lives in the ingestion service, prefer an **env-selected setting** (like `INGESTION_CHUNK_STRATEGY`) over code forks — arms then differ only in launch env.
5. **Verify each index** before evaluating: chunk count sane, distinct `doc_id` count == corpus size.
6. **One Phoenix experiment per arm, same dataset, distinct `--experiment-name`.** That single convention is what makes the Phoenix compare view work.
7. Run the cheap slice (`nano`) first to shake out plumbing, then **decide on `full`**. Don't conclude from n=10.
8. Read the **per-question compare view** for the diverging questions — the aggregate table tells you *whether*, the compare view tells you *why*.
9. On the t3.small POC domain, space heavy operations out: sustained ingest+eval load drove JVM pressure to ~80% and eventually wedged the node for ~16 minutes (TLS handshake timeouts). Bump the node size before iterating seriously.
10. **Clean up experiment indexes when you're done** (see below) — every extra index costs memory on the shared node permanently, not just while you're testing.

---

## 7. Cleaning up after testing

Three layers hold state after a comparison; clean them up from cheapest to most drastic.

### 7.1 Delete the experiment indexes (usual case)

The per-arm indexes are disposable — they can be rebuilt from the corpus at any time (~1.5–2 h each). Deleting them matters beyond tidiness: on the 2 GB t3.small, the extra faiss graphs are a standing memory cost (the three-index setup ran at ~80% JVM pressure and wedged the node once).

```bash
EP=<domain-endpoint>
uvx awscurl --service es --region ap-southeast-2 -X DELETE "https://$EP/lrb-rec-256-50"
uvx awscurl --service es --region ap-southeast-2 -X DELETE "https://$EP/lrb-fix-256-50"

# confirm what remains
uvx awscurl --service es --region ap-southeast-2 "https://$EP/_cat/indices?v"
```

Keep `legal-rag-bench` (the production baseline index) unless you mean to rebuild it. Deletion is immediate and irreversible — there is no recycle bin. The shared `hybrid-search-pipeline` is *not* deleted with the indexes and should stay (the baseline index uses it).

Also stop any leftover local processes from the ingest (`uvicorn app.main:app` on port 8000) — each arm's service instance is only needed during its ingest.

### 7.2 Phoenix experiments and local exports (optional)

- **Phoenix** stores datasets and experiments in `~/.phoenix/phoenix.db` on the machine running the server. They cost nothing while idle and are useful history — the usual move is to keep them. To remove one anyway, use the Phoenix UI (dataset page → delete experiment/dataset).
- **File exports** live under `services/eval/results/eval_rag/chunking_nano/<experiment-name>/` (CSV/JSON/parquet). `results/` is gitignored; delete freely, but note these are your only copies outside Phoenix.

### 7.3 Tear down the whole domain (when the POC is over)

The domain bills ~US$0.05/h while it exists, regardless of activity. When nobody needs *any* index anymore:

```bash
make opensearch-down    # deletes the CloudFormation stack: domain, all indexes, SG (~10–20 min)
make opensearch-status  # should report: stack not found, no charges
```

This destroys **every** index including `legal-rag-bench` — after this, a full re-ingest is required before any evaluation can run. `make opensearch-up` recreates the empty domain from the same template.
