"""HTTP client for the guardrail pod's `POST /check/chunks` (ingest-time scan).

The ingest-time corpus-poisoning guard. After chunking and BEFORE embed/index,
ingestion sends a document's chunks to the guardrail pod, which returns a
per-chunk safe/unsafe verdict with forensic attribution. ANY unsafe chunk means
the WHOLE document must be rejected (indexed: nothing). Ingestion NEVER imports
the guardrail package — it calls it over `INGESTION_GUARDRAIL_URL` only (the same
HTTP firewall it has to the parser).

The pod's chunk scan is DETERMINISTIC (pure regex, no model), so it is fast and
needs no AWS; a network fault reaching it is normalized to `GuardUnavailableError`
so the pipeline fails CLOSED (reject, do not index unscanned) rather than hanging.
"""

from dataclasses import dataclass

import httpx

# The scan is deterministic + local, so it returns quickly; bound every phase.
GUARD_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)


class GuardUnavailableError(Exception):
    """The guardrail pod could not be reached, or did not return a valid verdict."""


@dataclass(frozen=True)
class GuardVerdict:
    """The whole-document verdict from `/check/chunks` (plain data)."""

    safe: bool
    chunk_count: int
    unsafe_chunk_count: int
    detection_count: int
    results: list[dict]  # per-chunk {chunk_id, ordinal, verdict, detections[]}


def check_chunks(
    chunks: list[str], *, url: str, document_id: str, source_ref: str
) -> GuardVerdict:
    """Scan a document's chunks; return the whole-document verdict.

    Raises `GuardUnavailableError` on any transport / decode failure so the
    caller can fail CLOSED. The chunk texts are sent as-is (the pod never mutates
    them — it only reports).
    """
    payload = {
        "document_id": document_id,
        "source_ref": source_ref,
        "chunks": [
            {"id": f"{document_id}-c{i}", "ordinal": i, "text": text}
            for i, text in enumerate(chunks)
        ],
    }
    try:
        response = httpx.post(
            f"{url.rstrip('/')}/check/chunks", json=payload, timeout=GUARD_TIMEOUT
        )
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise GuardUnavailableError(str(exc)) from exc

    return GuardVerdict(
        safe=bool(data.get("safe", False)),
        chunk_count=int(data.get("chunk_count", len(chunks))),
        unsafe_chunk_count=int(data.get("unsafe_chunk_count", 0)),
        detection_count=int(data.get("detection_count", 0)),
        results=list(data.get("results", [])),
    )
