"""Tests for the legal-rag-bench service-ingest driver (mocked HTTP; no network)."""

import json

import httpx

from dev.scripts import ingest_legal_rag_bench_via_service as driver


def _write_corpus(tmp_path, records):
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text(
        "\n".join(json.dumps(record) for record in records), encoding="utf-8"
    )
    return corpus


def _ok_transport(seen_requests):
    """MockTransport that records request bodies and answers a 200 ingest."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen_requests.append((request.url.path, body))
        return httpx.Response(
            200,
            json={
                "doc_id": body["doc_id"],
                "sha256": "0" * 64,
                "chunks_indexed": 1,
                "skipped": False,
            },
        )

    return httpx.MockTransport(handler)


def test_request_body_carries_doc_id_and_md_source(tmp_path):
    """Each POST /ingest carries doc_id=<record id> and source=<temp .md path>."""
    records = [
        {"id": "1.1-c1-s1", "title": "Intro", "text": "Some passage text."},
        {"id": "2.2-c9-s3", "title": "Later", "text": "More passage text."},
    ]
    corpus = _write_corpus(tmp_path, records)
    md_dir = tmp_path / "md"
    seen = []

    exit_code = driver.main(
        ["--corpus", str(corpus), "--tmp-dir", str(md_dir)],
        transport=_ok_transport(seen),
    )

    assert exit_code == 0
    assert [path for path, _ in seen] == ["/ingest", "/ingest"]
    assert [body["doc_id"] for _, body in seen] == ["1.1-c1-s1", "2.2-c9-s3"]
    first_source = seen[0][1]["source"]
    assert first_source == str(md_dir / "1.1-c1-s1.md")
    # The temp file holds the rendered markdown the service will fetch.
    assert (md_dir / "1.1-c1-s1.md").read_text(encoding="utf-8").startswith("# Intro")


def test_render_markdown_title_heading_shape():
    """Title becomes an H1 heading, but is never duplicated if already present."""
    plain = {"id": "a", "title": "My Title", "text": "body text"}
    assert driver.render_markdown(plain) == "# My Title\n\nbody text"

    already_headed = {"id": "b", "title": "My Title", "text": "# My Title\n\nbody"}
    assert driver.render_markdown(already_headed) == "# My Title\n\nbody"

    untitled = {"id": "c", "title": "", "text": "just body"}
    assert driver.render_markdown(untitled) == "just body"


def test_start_and_limit_slice_the_corpus(tmp_path):
    """--start/--limit drive only the selected window (resumable chunked runs)."""
    records = [
        {"id": f"doc-{n}", "title": f"T{n}", "text": f"text {n}"} for n in range(5)
    ]
    corpus = _write_corpus(tmp_path, records)
    seen = []

    exit_code = driver.main(
        ["--corpus", str(corpus), "--tmp-dir", str(tmp_path / "md"),
         "--start", "1", "--limit", "2"],
        transport=_ok_transport(seen),
    )

    assert exit_code == 0
    assert [body["doc_id"] for _, body in seen] == ["doc-1", "doc-2"]


def test_nonzero_exit_on_first_502(tmp_path, capsys):
    """A 502 aborts immediately with a non-zero exit and a resume offset."""
    records = [
        {"id": "doc-0", "title": "A", "text": "aa"},
        {"id": "doc-1", "title": "B", "text": "bb"},
    ]
    corpus = _write_corpus(tmp_path, records)
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.url.path)
        return httpx.Response(502, json={"detail": {"message": "upstream down"}})

    exit_code = driver.main(
        ["--corpus", str(corpus), "--tmp-dir", str(tmp_path / "md")],
        transport=httpx.MockTransport(handler),
    )

    assert exit_code != 0
    assert attempts == ["/ingest"]  # aborted on the FIRST failure
    assert "Resume with --start 0" in capsys.readouterr().err
