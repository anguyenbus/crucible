"""
Demo: the ingest-time /check/chunks corpus-poisoning gate, end to end on the PDFs.

This drives the OWNABLE half of the demo — parse a document to text, chunk it, and
run the pod's injection scan — showing the two outcomes the ingestion service would
act on:

  * mock_investigation.pdf     -> SAFE  -> ingestion would embed + index it
  * poisoned_complementary.pdf -> UNSAFE-> ingestion would REJECT the WHOLE document,
                                           index nothing, and alert the frontend with
                                           the per-chunk forensic attribution below.

Run (in-process scan, no pod needed):
    cd services/guardrail && uv run --with pypdf python dev/demo_corpus/demo_check_chunks.py

Run against a LIVE pod (proves the HTTP contract the ingestion team consumes):
    make guardrail-pod            # in another shell (:8080)
    cd services/guardrail && uv run --with 'pypdf' --with 'httpx' \
        python dev/demo_corpus/demo_check_chunks.py --url http://127.0.0.1:8080

The chunking here is a simple ~800-char/120-overlap splitter standing in for the
ingestion pipeline's tiktoken chunker — enough to show per-chunk verdicts. The
verdict logic is identical either way (the pod runs the same app.chunk_scan).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pypdf import PdfReader

HERE = Path(__file__).resolve().parent
# Make the guardrail package (services/guardrail/app) importable when this script
# is run directly from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
CHUNK_CHARS = 800
OVERLAP = 120


def extract_text(pdf: Path) -> str:
    """Extract text from a PDF (stand-in for the parser service's markdown)."""
    reader = PdfReader(str(pdf))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def chunk(text: str) -> list[str]:
    """Naive char-window chunker with overlap (stand-in for the tiktoken chunker)."""
    text = text.strip()
    if len(text) <= CHUNK_CHARS:
        return [text] if text else []
    out: list[str] = []
    start = 0
    while start < len(text):
        out.append(text[start : start + CHUNK_CHARS])
        start += CHUNK_CHARS - OVERLAP
    return out


def build_request(pdf: Path) -> dict:
    chunks = chunk(extract_text(pdf))
    return {
        "document_id": pdf.stem,
        "source_ref": pdf.name,
        "chunks": [{"id": f"{pdf.stem}-c{i}", "ordinal": i, "text": t} for i, t in enumerate(chunks)],
    }


def scan_in_process(request: dict) -> dict:
    from app.chunk_scan import check_chunks, load_default_chunk_scanner
    from app.contract import CheckChunksRequest

    resp = check_chunks(load_default_chunk_scanner(), CheckChunksRequest(**request))
    return resp.model_dump()


def scan_over_http(request: dict, url: str) -> dict:
    import httpx

    r = httpx.post(f"{url.rstrip('/')}/check/chunks", json=request, timeout=30)
    r.raise_for_status()
    return r.json()


def report(pdf: Path, resp: dict) -> None:
    banner = "SAFE — would be indexed" if resp["safe"] else "UNSAFE — REJECT whole document"
    print("\n" + "=" * 78)
    print(f"{pdf.name}  ->  {banner}")
    print(
        f"  chunks={resp['chunk_count']}  unsafe_chunks={resp['unsafe_chunk_count']}  "
        f"hits={resp['detection_count']}  engine={resp['engine']}"
    )
    if resp["safe"]:
        return
    print("  ── frontend alert: this document is not safe to ingest ──")
    for r in resp["results"]:
        if r["verdict"] != "unsafe":
            continue
        print(f"  chunk {r['chunk_id']} (ordinal {r['ordinal']}) — {len(r['detections'])} finding(s):")
        for d in r["detections"]:
            print(
                f"    [{d['severity']:>6}] {d['category']}/{d['label']} "
                f"@{d['char_start']}-{d['char_end']}: {d['matched_excerpt']!r}"
            )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", help="Live pod base URL (else in-process scan).")
    args = ap.parse_args()

    for name in ("mock_investigation.pdf", "poisoned_complementary.pdf"):
        pdf = HERE / name
        if not pdf.is_file():
            raise SystemExit(f"missing {pdf} — run generate_demo_pdfs.py first")
        request = build_request(pdf)
        resp = scan_over_http(request, args.url) if args.url else scan_in_process(request)
        report(pdf, resp)
    print("\n" + "=" * 78)
    print("The poisoned document is stopped at the gate; the clean document is not.")


if __name__ == "__main__":
    main()
