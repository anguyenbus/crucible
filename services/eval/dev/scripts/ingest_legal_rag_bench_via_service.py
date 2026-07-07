"""
Drive the legal-rag-bench corpus through the ingestion service's ``POST /ingest``.

This is the POC ingest path (spec amendment 2, 2026-07-07): the ingestion
service (``services/ingestion``) OWNS the index — it normalizes, chunks
(800 tok / 120 overlap), embeds (Titan V2, throttle-aware retries),
bulk-indexes and dedups. This driver only walks ``corpus.jsonl`` (4,876
passages) and, per passage, writes the text to a temp ``.md`` file and POSTs
``{"source": <abs path>, "doc_id": <passage id>}`` to a locally running
instance of the service. ``doc_id`` MUST be the record's ``id`` — gold-passage
matching in the eval harness depends on it.

Config:
- Service URL: env ``EVAL_INGESTION_SERVICE_URL`` (default
  ``http://127.0.0.1:8000``). The service itself must be started with
  ``INGESTION_INDEX_NAME=legal-rag-bench``.
- ``--corpus``: path to ``corpus.jsonl`` (defaults to the HF cache snapshot).
- ``--start`` / ``--limit``: slice of the corpus to drive, for resumability
  and chunked runs. Re-driving already-ingested records is cheap: the
  service's sha256 dedup answers ``skipped=true`` without re-embedding.
- ``--tmp-dir``: where the per-record ``.md`` files are written.

Failure contract: the run aborts with a non-zero exit on the FIRST non-2xx
response (the service already retries Bedrock throttling internally, so a
502 means real upstream trouble); the failing corpus offset is printed so the
run can resume with ``--start``.

Usage (from services/eval):
    uv run python -m dev.scripts.ingest_legal_rag_bench_via_service \
        --start 0 --limit 1250
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Final

import httpx

DEFAULT_CORPUS_PATH: Final[str] = (
    "~/.cache/huggingface/hub/datasets--isaacus--legal-rag-bench/"
    "snapshots/db0b31dc6d195ce9916897e1ac5e4e6209736c8a/corpus.jsonl"
)
DEFAULT_SERVICE_URL: Final[str] = "http://127.0.0.1:8000"
PROGRESS_EVERY: Final[int] = 200
# Embedding a passage takes seconds; the service's internal throttle retries
# can stretch a single ingest well beyond httpx's 5 s default.
REQUEST_TIMEOUT_S: Final[float] = 300.0

_UNSAFE_FILENAME_CHARS: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9._-]")


class IngestServiceError(RuntimeError):
    """A non-2xx response from the ingestion service."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"HTTP {status_code}: {detail}")
        self.status_code = status_code


def load_corpus(path: Path) -> list[dict[str, Any]]:
    """Read corpus.jsonl into a list of {id, title, text, ...} records."""
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def render_markdown(record: dict[str, Any]) -> str:
    """Passage text as markdown, with the title as an H1 heading if present.

    Many legal-rag-bench passages already start with ``# <title>``; the
    heading is only prepended when the text does not already carry it.
    """
    text = record["text"]
    title = (record.get("title") or "").strip()
    if title and not text.lstrip().startswith(f"# {title}"):
        return f"# {title}\n\n{text}"
    return text


def write_markdown(record: dict[str, Any], tmp_dir: Path) -> Path:
    """Write the record's markdown to ``<tmp_dir>/<sanitized id>.md``."""
    safe_name = _UNSAFE_FILENAME_CHARS.sub("_", record["id"])
    path = tmp_dir / f"{safe_name}.md"
    path.write_text(render_markdown(record), encoding="utf-8")
    return path


def ingest_record(
    client: httpx.Client, record: dict[str, Any], md_path: Path
) -> dict[str, Any]:
    """POST one passage to /ingest; return the response body on 200."""
    response = client.post(
        "/ingest",
        json={"source": str(md_path), "doc_id": record["id"]},
    )
    if response.status_code != 200:
        raise IngestServiceError(response.status_code, response.text)
    return response.json()


def run_ingest(
    records: list[dict[str, Any]],
    client: httpx.Client,
    tmp_dir: Path,
    start_offset: int = 0,
) -> int:
    """Drive `records` through the service; return the process exit code."""
    tmp_dir.mkdir(parents=True, exist_ok=True)
    total_chunks = 0
    skipped = 0
    started_at = time.monotonic()

    for position, record in enumerate(records):
        corpus_offset = start_offset + position
        md_path = write_markdown(record, tmp_dir)
        try:
            body = ingest_record(client, record, md_path)
        except (IngestServiceError, httpx.HTTPError) as exc:
            print(
                f"FAIL at corpus offset {corpus_offset} (doc_id={record['id']!r}): "
                f"{exc}\nResume with --start {corpus_offset}.",
                file=sys.stderr,
            )
            return 1
        total_chunks += body["chunks_indexed"]
        skipped += 1 if body["skipped"] else 0
        done = position + 1
        if done % PROGRESS_EVERY == 0 or done == len(records):
            elapsed = time.monotonic() - started_at
            print(
                f"progress: {done}/{len(records)} docs "
                f"(offsets {start_offset}..{corpus_offset}) | "
                f"chunks_indexed={total_chunks} skipped={skipped} | "
                f"{elapsed:.0f}s elapsed",
                flush=True,
            )

    elapsed = time.monotonic() - started_at
    print(
        f"SUMMARY: docs={len(records)} chunks_indexed={total_chunks} "
        f"skipped={skipped} duration_s={elapsed:.0f}"
    )
    return 0


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ingest legal-rag-bench via the ingestion service."
    )
    parser.add_argument(
        "--corpus",
        type=Path,
        default=Path(DEFAULT_CORPUS_PATH).expanduser(),
        help="Path to legal-rag-bench corpus.jsonl (default: HF cache snapshot)",
    )
    parser.add_argument(
        "--start", type=int, default=0, help="Corpus offset to start from"
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Max records to drive (default: all)"
    )
    parser.add_argument(
        "--tmp-dir",
        type=Path,
        default=Path(tempfile.gettempdir()) / "lrb_md",
        help="Directory for the per-record temp .md files",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, transport: httpx.BaseTransport | None = None) -> int:
    """Entry point. `transport` is injectable for tests (mocked HTTP only)."""
    args = parse_args(argv)
    records = load_corpus(args.corpus)
    end = None if args.limit is None else args.start + args.limit
    selected = records[args.start : end]

    service_url = os.environ.get("EVAL_INGESTION_SERVICE_URL", DEFAULT_SERVICE_URL)
    print(
        f"Ingesting {len(selected)} of {len(records)} records "
        f"(start={args.start}, limit={args.limit}) via {service_url}"
    )
    with httpx.Client(
        base_url=service_url, timeout=REQUEST_TIMEOUT_S, transport=transport
    ) as client:
        return run_ingest(selected, client, args.tmp_dir, start_offset=args.start)


if __name__ == "__main__":
    sys.exit(main())
