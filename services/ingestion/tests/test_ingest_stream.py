"""POST /ingest/stream — live per-phase SSE, and the run_ingest generator.

Both entry points share `app.pipeline.run.run_ingest`; here we assert the phase
ORDER the generator yields (parsing → chunking → embedding[current/total] →
indexing → done), that a skip short-circuits after chunking with no embed/index,
and that a typed failure arrives as a terminal `error` frame (HTTP 200 for the
whole stream, so the status rides inside the event). Every pipeline stage is a
recording fake — no AWS, no OpenSearch.
"""

import json

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.pipeline import run as run_module
from app.pipeline.fetch import SourceNotFoundError
from app.pipeline.run import PhaseEvent, ResultEvent, run_ingest
from app.schemas.ingest import IngestRequest

SOURCE = "s3://atlas-demo-shared-s3-docs/guides/setup.md"
# Long enough to chunk into several pieces so embedding reports real i-of-N.
MARKDOWN = ("# Title\n\n" + ("lorem ipsum dolor sit amet " * 400)).strip()


@pytest.fixture
def client():
    return TestClient(create_app())


@pytest.fixture
def fake_pipeline(monkeypatch):
    """Happy-path fakes; embedding yields one vector per chunk (sequential)."""

    def fake_fetch(source):
        return MARKDOWN.encode("utf-8")

    def fake_dedup(doc_id, sha256, expected_chunk_count, index=None):
        return False

    def fake_embed_iter(chunks):
        for _ in chunks:
            yield [0.1] * 1024

    def fake_index(chunks, vectors, *, doc_id, source_uri, sha256, index=None, **kwargs):
        return len(chunks)

    def fake_prune(doc_id, *, keep_sha256, index=None):
        return 0

    monkeypatch.setattr(run_module, "fetch_bytes", fake_fetch)
    monkeypatch.setattr(run_module, "find_complete_raw_duplicate", lambda *a, **k: None)
    monkeypatch.setattr(run_module, "is_complete_duplicate", fake_dedup)
    monkeypatch.setattr(run_module, "embed_texts_iter", fake_embed_iter)
    monkeypatch.setattr(run_module, "index_chunks", fake_index)
    monkeypatch.setattr(run_module, "prune_stale_chunks", fake_prune)


def _events(response) -> list[dict]:
    """Parse the SSE body into the list of decoded `data:` JSON payloads."""
    return [
        json.loads(line[len("data:") :].strip())
        for line in response.text.splitlines()
        if line.startswith("data:")
    ]


def test_run_ingest_yields_phases_in_order_then_result(fake_pipeline):
    events = list(run_ingest(IngestRequest(source=SOURCE)))

    phases = [e.phase for e in events if isinstance(e, PhaseEvent)]
    # parsing → chunking → embedding(...) → indexing, embedding repeated per chunk.
    assert phases[0] == "parsing"
    assert phases[1] == "chunking"
    assert phases[2] == "embedding"
    assert phases[-1] == "indexing"
    assert phases.count("indexing") == 1

    # Embedding reports 0..N of a fixed total; the final result matches the count.
    embeds = [e for e in events if isinstance(e, PhaseEvent) and e.phase == "embedding"]
    total = embeds[0].total
    assert total > 1  # the fixture text is multi-chunk
    assert [e.current for e in embeds] == list(range(0, total + 1))
    assert all(e.total == total for e in embeds)

    result = events[-1]
    assert isinstance(result, ResultEvent)
    assert result.skipped is False
    assert result.chunks_indexed == total


def test_stream_emits_phase_frames_then_done(client, fake_pipeline):
    response = client.post("/ingest/stream", json={"source": SOURCE})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _events(response)

    assert events[0] == {"phase": "parsing"}
    assert events[1] == {"phase": "chunking"}
    assert any(
        e["phase"] == "embedding" and e.get("current") == 1 and e.get("total") > 1
        for e in events
    )
    done = events[-1]
    assert done["phase"] == "done"
    assert done["skipped"] is False
    assert done["chunks_indexed"] >= 1
    assert "doc_id" in done and "sha256" in done


def test_stream_skip_short_circuits_after_chunking(client, monkeypatch, fake_pipeline):
    monkeypatch.setattr(run_module, "find_complete_raw_duplicate", lambda *a, **k: None)
    monkeypatch.setattr(run_module, "is_complete_duplicate", lambda *a, **k: True)

    events = _events(client.post("/ingest/stream", json={"source": SOURCE}))

    phases = [e["phase"] for e in events]
    assert "embedding" not in phases  # a skip never embeds or indexes
    assert "indexing" not in phases
    done = events[-1]
    assert done["phase"] == "done"
    assert done["skipped"] is True
    assert done["chunks_indexed"] == 0


def test_stream_failure_arrives_as_terminal_error_event(client, monkeypatch):
    def raise_missing(source):
        raise SourceNotFoundError("no such object")

    monkeypatch.setattr(run_module, "fetch_bytes", raise_missing)

    response = client.post("/ingest/stream", json={"source": "s3://b/missing.md"})

    # The stream itself is a 200; the failure rides inside the terminal event.
    assert response.status_code == 200
    events = _events(response)
    assert events[0] == {"phase": "parsing"}  # we entered parsing before failing
    error = events[-1]
    assert error["phase"] == "error"
    assert error["status"] == 404
    assert "no such object" in str(error["detail"])
