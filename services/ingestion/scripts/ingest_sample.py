"""CLI helper: ingest one local or S3 markdown file for testing/validation.

Invokes the ingest pipeline DIRECTLY, in-process (fetch -> normalize ->
dedup -> chunk -> embed -> bulk index -> prune), by calling the same handler
that backs `POST /ingest` — no running service is required, and the output
matches the API contract exactly. AWS credentials (instance role) and
network access to the OpenSearch VPC domain are therefore needed in the
environment this script runs in.

Usage:
    uv run scripts/ingest_sample.py /abs/path/file.md
    uv run scripts/ingest_sample.py s3://bucket/key.md
    uv run scripts/ingest_sample.py s3://bucket/key.md --doc-id mydoc

Exits 0 on success (including a dedup `skipped: true` no-op), 1 on failure,
printing the same status code and detail the HTTP API would return.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException

from app.api.ingest import ingest
from app.schemas.ingest import IngestRequest


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ingest one local or S3 markdown file via the ingest pipeline."
    )
    parser.add_argument(
        "source",
        help="s3://bucket/key.md URI or local absolute path to a markdown file",
    )
    parser.add_argument(
        "--doc-id",
        default=None,
        help="explicit doc_id (default: derived truncated sha256 of the source URI)",
    )
    args = parser.parse_args()

    try:
        response = ingest(IngestRequest(source=args.source, doc_id=args.doc_id))
    except HTTPException as exc:
        print(f"FAIL: HTTP {exc.status_code}")
        print(json.dumps({"detail": exc.detail}, indent=2, default=str))
        return 1

    print(json.dumps(response.model_dump(), indent=2))
    if response.skipped:
        print("RESULT: OK — unchanged content already fully indexed (skipped)")
    else:
        print(f"RESULT: OK — {response.chunks_indexed} chunk(s) indexed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
