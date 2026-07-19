"""Task Group 4: the pre-parse raw-bytes SHA-256 dedup gate + provenance stamping.

Lean suite — `parser_client` is MOCKED and OpenSearch is faked, so nothing here
imports docling/torch and nothing touches a live cluster. Focused coverage only
(per task 4.1):

  - HEADLINE: an identical re-upload short-circuits pre-parse and NEVER calls the
    parser (the raw-bytes gate fires before the expensive parse).
  - the gate is keyed on the raw-bytes hash INDEPENDENT of `doc_id` (the same
    bytes via a different source URI / doc_id still hits).
  - `raw_sha256` + `chunk_count` are stamped onto indexed chunk metadata so the
    NEXT upload can confirm a complete copy exists.
  - a partially-indexed prior run does NOT mask the document — the gate confirms
    completeness before firing (correct-by-construction; `is_complete_duplicate`
    stays the second-line check).
  - a compact provenance summary (confidence / route counts / call_counts /
    low-confidence page indices) is stamped onto chunk metadata.

SEAM NOTE (Task Group 6): `run_ingest`'s parsing phase now consumes the STREAMING
client, so the `/ingest` integration tests patch `parse_stream` (the drain
`parse` is unchanged). The gate STILL runs BEFORE the parse stream, so a raw-bytes
hit short-circuits before `parse_stream` is ever reached — unchanged behaviour.
Exhaustive coverage is deliberately skipped.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.pipeline import hash_dedup as hash_dedup_module
from app.pipeline import parser_client as parser_client_module
from app.pipeline import run as run_module
from app.pipeline.hash_dedup import find_complete_raw_duplicate, raw_content_sha256
from app.pipeline.index import build_bulk_actions
from app.pipeline.parse import LOW_CONFIDENCE_THRESHOLD, summarize_provenance
from app.pipeline.parser_client import ParseResult

RAW_BYTES = b"%PDF-1.7 the same exact bytes uploaded twice"
PARSED_MARKDOWN = ("# Report\n\n" + ("hybrid search over the corpus " * 400)).strip()


def _parse_result(markdown: str = PARSED_MARKDOWN) -> ParseResult:
    return ParseResult(
        markdown=markdown,
        confidence={
            "document": 0.82,
            "pages": [
                {"page_index": 0, "confidence": 0.95},
                {"page_index": 1, "confidence": 0.50},  # below the 0.65 threshold
            ],
        },
        page_count=2,
        page_routes=[
            {"page_index": 0, "route": "docling-kept"},
            {"page_index": 1, "route": "textract-fallback-docling"},
        ],
        call_counts={"vlm": 0, "textract": 1},
        warnings=[],
    )


class _FakeSearchClient:
    """Fakes `client.search` for the raw-bytes gate's aggregation query."""

    def __init__(self, buckets):
        self._buckets = buckets
        self.search_calls: list[dict] = []

    def search(self, *, index, body):
        self.search_calls.append({"index": index, "body": body})
        return {"aggregations": {"by_doc": {"buckets": self._buckets}}}


def _bucket(doc_id, doc_count, chunk_count, sha256):
    return {
        "key": doc_id,
        "doc_count": doc_count,
        "expected": {"value": float(chunk_count)},
        "content_sha": {"buckets": [{"key": sha256}]},
    }


@pytest.fixture
def mock_downstream(monkeypatch):
    """Mock embed + OpenSearch write/prune + the post-parse text-sha dedup."""
    calls: list[tuple] = []

    def fake_embed_iter(chunks):
        chunks = list(chunks)
        calls.append(("embed", len(chunks)))
        for _ in chunks:
            yield [0.1] * 1024

    def fake_index(chunks, vectors, *, doc_id, source_uri, sha256, **kwargs):
        calls.append(("index", doc_id, len(chunks), kwargs.get("raw_sha256"), kwargs.get("provenance")))
        return len(chunks)

    monkeypatch.setattr(run_module, "is_complete_duplicate", lambda *a, **k: False)
    monkeypatch.setattr(run_module, "embed_texts_iter", fake_embed_iter)
    monkeypatch.setattr(run_module, "index_chunks", fake_index)
    monkeypatch.setattr(run_module, "prune_stale_chunks", lambda *a, **k: 0)
    return calls


@pytest.fixture
def client():
    return TestClient(create_app())


# --------------------------------------------------------------------------- #
# HEADLINE: identical re-upload skips the parser call ENTIRELY
# --------------------------------------------------------------------------- #
def test_identical_reupload_skips_parser_entirely(client, mock_downstream, monkeypatch):
    """A complete prior copy of these raw bytes short-circuits BEFORE the parser."""
    monkeypatch.setattr(run_module, "fetch_bytes", lambda source: RAW_BYTES)

    def explode(*args, **kwargs):
        raise AssertionError("the parser must NOT be called on a raw-bytes hit")

    monkeypatch.setattr(parser_client_module, "parse_stream", explode)
    # The gate finds a COMPLETE prior copy (doc_count == chunk_count).
    monkeypatch.setattr(
        run_module,
        "find_complete_raw_duplicate",
        lambda raw_sha256, index=None: "prior-content-sha",
    )

    response = client.post("/ingest", json={"source": "s3://bucket/report.pdf"})

    assert response.status_code == 200
    body = response.json()
    assert body["skipped"] is True
    assert body["chunks_indexed"] == 0
    assert body["sha256"] == "prior-content-sha"
    # Never parsed, never embedded, never indexed.
    assert not any(c[0] in ("embed", "index") for c in mock_downstream)


def test_gate_miss_falls_through_to_parser_and_indexes(client, mock_downstream, monkeypatch):
    """No prior copy → the parser runs and the document is indexed (control)."""
    monkeypatch.setattr(run_module, "fetch_bytes", lambda source: RAW_BYTES)
    monkeypatch.setattr(run_module, "find_complete_raw_duplicate", lambda *a, **k: None)
    parsed: list[bool] = []

    def fake_parse_stream(data, filename, *, parser_url, on_phase):
        parsed.append(True)
        return _parse_result()

    monkeypatch.setattr(parser_client_module, "parse_stream", fake_parse_stream)

    response = client.post("/ingest", json={"source": "s3://bucket/report.pdf"})

    assert response.status_code == 200
    assert response.json()["skipped"] is False
    assert parsed == [True]
    assert any(c[0] == "index" for c in mock_downstream)


# --------------------------------------------------------------------------- #
# gate keyed on raw bytes INDEPENDENT of doc_id
# --------------------------------------------------------------------------- #
def test_gate_is_doc_id_independent(client, mock_downstream, monkeypatch):
    """Same bytes via a DIFFERENT source URI (different doc_id) still hits."""
    seen_hashes: list[str] = []

    def fake_find(raw_sha256, index=None):
        seen_hashes.append(raw_sha256)
        return "prior-content-sha"  # a complete copy exists regardless of doc_id

    monkeypatch.setattr(run_module, "fetch_bytes", lambda source: RAW_BYTES)
    monkeypatch.setattr(run_module, "find_complete_raw_duplicate", fake_find)
    monkeypatch.setattr(
        parser_client_module,
        "parse_stream",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("parser must not run")),
    )

    # A brand-new source URI → a brand-new derived doc_id, yet the gate still hits.
    response = client.post(
        "/ingest", json={"source": "s3://other-bucket/DIFFERENT-name.pdf"}
    )

    assert response.status_code == 200 and response.json()["skipped"] is True
    # The gate was keyed purely on the raw-bytes hash, not the doc_id / URI.
    assert seen_hashes == [raw_content_sha256(RAW_BYTES)]


# --------------------------------------------------------------------------- #
# raw_sha256 + chunk_count stamped on indexed chunk metadata
# --------------------------------------------------------------------------- #
def test_raw_sha256_and_chunk_count_stamped_on_chunks():
    """Every chunk carries `raw_sha256` and the doc's total `chunk_count`."""
    chunks = ["a", "b", "c"]
    vectors = [[0.1] * 4, [0.2] * 4, [0.3] * 4]
    actions = build_bulk_actions(
        chunks,
        vectors,
        doc_id="doc1",
        source_uri="s3://b/k.pdf",
        sha256="content-sha",
        raw_sha256="raw-sha",
        provenance={"confidence": 0.9},
    )
    docs = actions[1::2]  # every other action is the document body
    assert [d["raw_sha256"] for d in docs] == ["raw-sha"] * 3
    assert [d["chunk_count"] for d in docs] == [3, 3, 3]
    assert [d["chunk_index"] for d in docs] == [0, 1, 2]
    assert docs[0]["provenance"] == {"confidence": 0.9}


# --------------------------------------------------------------------------- #
# partial prior run does NOT mask the document (correctness)
# --------------------------------------------------------------------------- #
def test_partial_prior_run_does_not_fire_the_gate():
    """A crashed mid-index prior run (doc_count < chunk_count) is NOT a complete copy."""
    partial = _FakeSearchClient(
        buckets=[_bucket("docA", doc_count=2, chunk_count=5, sha256="sha-A")]
    )
    assert (
        find_complete_raw_duplicate("raw-sha", index="idx", client=partial) is None
    )

    complete = _FakeSearchClient(
        buckets=[_bucket("docA", doc_count=5, chunk_count=5, sha256="sha-A")]
    )
    assert (
        find_complete_raw_duplicate("raw-sha", index="idx", client=complete) == "sha-A"
    )


def test_gate_picks_a_complete_bucket_among_partial_ones():
    """One complete doc_id set is enough even if others are partial."""
    mixed = _FakeSearchClient(
        buckets=[
            _bucket("docPartial", doc_count=1, chunk_count=4, sha256="sha-P"),
            _bucket("docComplete", doc_count=3, chunk_count=3, sha256="sha-C"),
        ]
    )
    assert find_complete_raw_duplicate("raw-sha", index="idx", client=mixed) == "sha-C"


# --------------------------------------------------------------------------- #
# compact provenance summary
# --------------------------------------------------------------------------- #
def test_summarize_provenance_is_compact_and_correct():
    """Provenance distils to confidence / route counts / call_counts / low pages."""
    prov = summarize_provenance(_parse_result())
    assert prov["confidence"] == 0.82
    assert prov["route_counts"] == {"docling-kept": 1, "textract-fallback-docling": 1}
    assert prov["call_counts"] == {"vlm": 0, "textract": 1}
    # Page 1 scored 0.50 < 0.65 threshold; page 0 (0.95) is not flagged.
    assert prov["low_confidence_pages"] == [1]
    assert LOW_CONFIDENCE_THRESHOLD == 0.65
    # It is a SUMMARY — the full page_routes array is NOT carried through.
    assert "page_routes" not in prov


def test_provenance_stamped_onto_indexed_chunks(client, mock_downstream, monkeypatch):
    """The parser-routed path stamps a provenance summary onto index_chunks."""
    monkeypatch.setattr(run_module, "fetch_bytes", lambda source: RAW_BYTES)
    monkeypatch.setattr(run_module, "find_complete_raw_duplicate", lambda *a, **k: None)
    monkeypatch.setattr(
        parser_client_module, "parse_stream", lambda *a, **k: _parse_result()
    )

    response = client.post("/ingest", json={"source": "s3://bucket/report.pdf"})

    assert response.status_code == 200
    index_calls = [c for c in mock_downstream if c[0] == "index"]
    assert index_calls
    raw_sha, provenance = index_calls[0][3], index_calls[0][4]
    assert raw_sha == raw_content_sha256(RAW_BYTES)
    assert provenance["confidence"] == 0.82
    assert provenance["low_confidence_pages"] == [1]
