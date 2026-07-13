"""
Phase 2 demo: one LIVE query through the real orchestrator pipeline.

What it shows, step by step: real readiness checks (OpenSearch + Bedrock),
the pinned config with its INLINED prompt template (covered by
``config_sha256``), a live retrieve -> assemble -> prompt -> generate -> cite
run against the ``legal-rag-bench`` index, citations with interval
``claim_span``s resolved back into the answer text, verbatim schema
validation, provenance echoes, per-stage timings, the Phoenix trace link,
and the free error paths.

COST: each run makes exactly ONE paid Bedrock generation call (plus one tiny
Titan query embedding). The error-path step is free (rejected before the
pipeline runs).

Two modes:

- **In-process (default)** — mounts the FastAPI app (with real lifespan
  clients) in this process. Under a debugger you can step from STEP 4
  straight into ``app.routers.query.post_query``, the stages in
  ``app/orchestrator/``, and the AWS clients. Needs AWS credentials in the
  ambient boto3 chain and the OpenSearch endpoint — resolved automatically:
  ``ORCHESTRATOR_OPENSEARCH_ENDPOINT`` env, else ``EVAL_OPENSEARCH_ENDPOINT``
  env, else the ``opensearch-poc`` CloudFormation stack output (same
  discovery the repo Makefile uses).
- **HTTP** — talks to a running server over the wire::

      cd services/orchestrator
      ORCHESTRATOR_OPENSEARCH_ENDPOINT=<endpoint> PHOENIX_ENDPOINT=http://localhost:6006 ...
      ... uv run uvicorn app.main:app --port 8000      # terminal 1 (one line)

      uv run python scripts/demo_live_query.py --http http://localhost:8000

Run it (from ``services/orchestrator/``)::

    uv run python scripts/demo_live_query.py           # in-process, live
    uv run python scripts/demo_live_query.py --pdb     # pdb before STEP 1
    uv run python -m pdb scripts/demo_live_query.py    # classic pdb

VS Code / Cursor: set breakpoints anywhere under ``app/`` and use the
"Orchestrator demo" launch configuration — in-process mode makes the whole
live request path one steppable call stack.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# Make `app` importable no matter which interpreter/cwd launched us (IDE
# debuggers often run this file by absolute path from the repo root, where
# the service package is not installed).
_SERVICE_ROOT = str(Path(__file__).resolve().parents[1])
if _SERVICE_ROOT not in sys.path:
    sys.path.insert(0, _SERVICE_ROOT)


def _ensure_service_deps() -> None:
    """
    Make the service's dependencies importable under ANY interpreter.

    IDE debuggers often launch this script with the repo-root venv, which
    lacks the orchestrator's AWS deps. Since both venvs share the same
    CPython minor version, prepending the service venv's site-packages puts
    the service's pinned deps first — and keeps the debug session alive
    (a re-exec would detach breakpoints). Fails with guidance only when the
    service venv is missing, unsynced, or on a different Python version.
    """
    import importlib.util

    if importlib.util.find_spec("opensearchpy") is not None:
        return  # correct venv (or deps otherwise present) — nothing to do
    version_tag = f"python{sys.version_info.major}.{sys.version_info.minor}"
    site_packages = Path(_SERVICE_ROOT) / ".venv" / "lib" / version_tag / "site-packages"
    if site_packages.is_dir():
        sys.path.insert(0, str(site_packages))
        if importlib.util.find_spec("opensearchpy") is not None:
            print(f"[env] interpreter {sys.executable} lacks the service deps —")
            print(f"[env] injected {site_packages} (service pins take precedence)")
            return
    raise SystemExit(
        "The orchestrator's dependencies are unavailable to this interpreter "
        f"({sys.executable}, {version_tag}) and could not be injected from "
        f"{site_packages}.\nFix ONE of:\n"
        f"  - sync the service venv: cd {_SERVICE_ROOT} && uv sync\n"
        "  - IDE: pick the 'Orchestrator Phase 2 demo' launch configuration, or select "
        f"the interpreter {_SERVICE_ROOT}/.venv/bin/python\n"
        f"  - terminal: cd {_SERVICE_ROOT} && uv run python scripts/demo_live_query.py"
    )


_ensure_service_deps()

# The demo deliberately uses the same models/helpers the service itself uses,
# so stepping through it exercises real code paths, not a parallel copy.
from app.config import resolve_pipeline_config  # noqa: E402
from app.schemas.contract_validation import validate_result  # noqa: E402
from app.schemas.query import QueryRequest  # noqa: E402

DEFAULT_CONFIG_REF = "legal-rag-default-1.1.0"
DEFAULT_QUESTION = "What elements must the prosecution prove for the offence of theft?"
PHOENIX_UI = os.environ.get("PHOENIX_ENDPOINT", "http://localhost:6006")


def banner(step: str, title: str) -> None:
    """Print a numbered step header so debugger progress is easy to follow."""
    print(f"\n=== {step}: {title} " + "=" * max(0, 58 - len(step) - len(title)))


def resolve_opensearch_endpoint() -> str:
    """
    Resolve the live OpenSearch endpoint for in-process mode.

    Same discovery order the repo Makefile uses: explicit env, eval's env,
    then the ``opensearch-poc`` CloudFormation stack output.
    """
    for var in ("ORCHESTRATOR_OPENSEARCH_ENDPOINT", "EVAL_OPENSEARCH_ENDPOINT"):
        endpoint = os.environ.get(var)
        if endpoint:
            print(f"[endpoint] {endpoint} (from {var})")
            return endpoint
    region = os.environ.get("AWS_REGION", "ap-southeast-2")
    lookup = subprocess.run(
        [
            "aws",
            "cloudformation",
            "describe-stacks",
            "--region",
            region,
            "--stack-name",
            "opensearch-poc",
            "--query",
            "Stacks[0].Outputs[?OutputKey=='DomainEndpoint'].OutputValue",
            "--output",
            "text",
        ],
        capture_output=True,
        text=True,
    )
    endpoint = lookup.stdout.strip()
    if lookup.returncode != 0 or not endpoint or endpoint == "None":
        raise SystemExit(
            "Cannot resolve the OpenSearch endpoint: set "
            "ORCHESTRATOR_OPENSEARCH_ENDPOINT (or EVAL_OPENSEARCH_ENDPOINT), or make "
            f"the opensearch-poc stack queryable.\naws stderr: {lookup.stderr.strip()}"
        )
    print(f"[endpoint] {endpoint} (resolved from CloudFormation stack opensearch-poc)")
    return endpoint


def step_1_check_readiness(client: Any) -> None:
    """STEP 1 — real readiness: config manifest + OpenSearch + Bedrock creds."""
    banner("STEP 1", "Readiness (real dependency checks)")
    response = client.get("/readyz")
    print(f"GET /readyz -> HTTP {response.status_code}: {response.json()}")
    if response.status_code != 200:
        raise SystemExit(
            "Service is not ready — fix the dependency named above (AWS creds, "
            "OpenSearch endpoint/reachability) and re-run."
        )


def step_2_show_pinned_config(config_ref: str) -> None:
    """STEP 2 — the pinned config: every behavior pin, template included."""
    banner("STEP 2", f"Pinned config {config_ref}")
    resolved = resolve_pipeline_config(config_ref)
    cfg = resolved.config
    print(f"pipeline_version : {resolved.pipeline_version}")
    print(f"config_sha256    : {resolved.config_sha256}  (covers EVERY byte below)")
    print(f"generator        : {cfg.generator.model_id}  (temp {cfg.generator.temperature})")
    print(f"embedder         : {cfg.embedder.model_id}")
    print(f"retrieval top_k  : {cfg.retrieval.top_k}")
    template_head = "\n".join(cfg.prompt_template.text.splitlines()[:3])
    print(f"inline prompt template (first lines):\n{template_head}\n  ...")


def step_3_build_request(question: str, config_ref: str) -> QueryRequest:
    """STEP 3 — build and validate the request with the service's own model."""
    banner("STEP 3", "Build the QueryRequest")
    request = QueryRequest(
        question=question,
        query_id="demo-phase2-001",
        pipeline_config=config_ref,
        metadata={"demo": "phase2"},  # opaque; echoed verbatim
    )
    print("request:", json.dumps(request.model_dump(), indent=2))
    return request


def step_4_send_live_query(client: Any, request: QueryRequest) -> dict[str, Any]:
    """STEP 4 — POST /query, the LIVE pipeline. Step INTO the router here."""
    banner("STEP 4", "POST /query (LIVE — one paid Bedrock generation)")
    started = time.perf_counter()
    response = client.post("/query", json=request.model_dump(mode="json"))
    elapsed = time.perf_counter() - started
    print(f"HTTP {response.status_code} in {elapsed:.2f}s")
    if response.status_code != 200:
        print(response.text)
        raise SystemExit(f"expected 200, got {response.status_code}")
    envelope: dict[str, Any] = response.json()
    return envelope


def step_5_inspect_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    """STEP 5 — the envelope: live mode, guardrails still empty (Phase 3)."""
    banner("STEP 5", "Envelope")
    assert envelope["generation_mode"] == "live", "Phase 2 must be live"
    print(f"generation_mode     : {envelope['generation_mode']}")
    print(f"guardrail_decisions : {envelope['guardrail_decisions']}  (populated in Phase 3)")
    return envelope["result"]  # the one-line eval-adapter unwrap


def step_6_show_answer_and_chunks(result: dict[str, Any]) -> None:
    """STEP 6 — the generated answer (markers kept) and REAL retrieved chunks."""
    banner("STEP 6", "Answer + retrieved chunks (live from OpenSearch)")
    print(f"answer:\n{result['answer']['text']}\n")
    for chunk in result["retrieved_chunks"]:
        preview = chunk["text"][:70].replace("\n", " ")
        print(f"rank {chunk['rank']}: {chunk['chunk_id']}  score={chunk['score']:.4f}  {preview}…")


def step_7_resolve_citations(result: dict[str, Any]) -> None:
    """STEP 7 — citations: interval claim_spans resolved back into the answer."""
    banner("STEP 7", "Citations (claim_span = text the marker supports)")
    answer_text = result["answer"]["text"]
    citations = result["answer"]["citations"]
    if not citations:
        print("0 citations — the model answered without citing (reported honestly).")
        return
    for i, citation in enumerate(citations):
        start, end = citation["claim_span"]  # [start, end) offsets into answer.text
        claim = answer_text[start:end].strip().replace("\n", " ")
        print(f"[{i}] chunks {citation['chunk_ids']}  span [{start},{end}):")
        print(f'    "{claim}"')


def step_8_validate_contract(result: dict[str, Any]) -> None:
    """STEP 8 — prove conformance: validate verbatim against the packaged schema."""
    banner("STEP 8", "Validate result against rag_query_output v1.1.0")
    validate_result(result)  # raises on any violation — no key stripping
    print("VALID: live result conforms to the eval contract verbatim")


def step_9_provenance(request: QueryRequest, result: dict[str, Any]) -> None:
    """STEP 9 — reproducibility anchors: what ran, against which corpus."""
    banner("STEP 9", "Provenance & reproducibility")
    version = result["system_version"]
    query = result["query"]
    assert query["query_id"] == request.query_id and query["metadata"] == request.metadata
    print(f"query_id / metadata echoed verbatim: {query['query_id']} / {query['metadata']}")
    for key in sorted(version):
        print(f"system_version.{key} = {version[key]}")


def step_10_observability(result: dict[str, Any]) -> None:
    """STEP 10 — per-stage timings and the Phoenix trace to open in the UI."""
    banner("STEP 10", "Timings + Phoenix trace")
    for stage, ms in result["timings_ms"].items():
        print(f"{stage:>17}: {ms:8.1f} ms")
    trace = result.get("trace")
    if trace:
        print(f"\ntrace_id : {trace['trace_id']}  (project '{trace['phoenix_project']}')")
        print(f"open     : {PHOENIX_UI}  -> project '{trace['phoenix_project']}' -> this trace")
        print("spans    : POST /query > embedding, retrieval, context_assembly, generation,")
        print("           citation_build — OpenInference attributes on each")
    else:
        print("\nno trace block — PHOENIX_ENDPOINT unset (no-op tracer); service still works")


def step_11_error_paths(client: Any) -> None:
    """STEP 11 — the free error paths (rejected before any AWS call)."""
    banner("STEP 11", "Error paths (free)")
    unknown = client.post(
        "/query", json={"question": "q", "pipeline_config": "no-such-config-9.9.9"}
    )
    print(f"unknown ref   -> HTTP {unknown.status_code} (exact ref match only, no fallback)")
    malformed = client.post("/query", json={"question": "q", "pipeline_config": "not_a_ref"})
    print(f"malformed ref -> HTTP {malformed.status_code} (rejected by request validation)")


def main() -> int:
    """Run the eleven demo steps in order (one paid generation total)."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--http",
        metavar="URL",
        default=None,
        help="talk to a running server (e.g. http://localhost:8000) instead of in-process",
    )
    parser.add_argument("--question", default=DEFAULT_QUESTION, help="question to ask")
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_REF,
        help="pinned pipeline config ref (there is deliberately NO top-k flag: the config owns it)",
    )
    parser.add_argument(
        "--pdb", action="store_true", help="drop into pdb before STEP 1 (then: n, s, c)"
    )
    args = parser.parse_args()

    if args.pdb:
        breakpoint()

    with contextlib.ExitStack() as stack:
        if args.http is None:
            os.environ.setdefault("ORCHESTRATOR_OPENSEARCH_ENDPOINT", resolve_opensearch_endpoint())
            from app.main import app
            from fastapi.testclient import TestClient

            print("[mode] in-process (lifespan clients are REAL — steppable into app internals)")
            client: Any = stack.enter_context(TestClient(app))  # `with` runs the lifespan
        else:
            import httpx

            print(f"[mode] HTTP against {args.http}")
            client = stack.enter_context(httpx.Client(base_url=args.http, timeout=120.0))

        step_1_check_readiness(client)
        step_2_show_pinned_config(args.config)
        request = step_3_build_request(args.question, args.config)
        envelope = step_4_send_live_query(client, request)
        result = step_5_inspect_envelope(envelope)
        step_6_show_answer_and_chunks(result)
        step_7_resolve_citations(result)
        step_8_validate_contract(result)
        step_9_provenance(request, result)
        step_10_observability(result)
        step_11_error_paths(client)

    print("\nDemo complete: live query answered, cited, validated, traced.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
