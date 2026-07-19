"""Debug utility: inspect the chunks stored in an OpenSearch index.

Reuses the ingestion service's SigV4-signed OpenSearch client (so it works
against the VPC domain with the instance role, no hand-rolled auth). Prints the
chunk count and, for each chunk, its `_id` / `doc_id` / `chunk_index` /
char-length / a text preview — the fastest way to see what a project index
(`proj-{id}`) actually holds vs. what a PDF viewer shows.

Usage (from services/ingestion/, using the service venv):
    ./.venv/bin/python -m scripts.inspect_index proj-6d38cf387b0d4998af52ad0c61d45a16
    ./.venv/bin/python -m scripts.inspect_index <index> --full     # full chunk text, not a preview
    ./.venv/bin/python -m scripts.inspect_index <index> --limit 20
"""

from __future__ import annotations

import argparse

from app.clients.opensearch import get_opensearch_client


def main() -> None:
    parser = argparse.ArgumentParser(description="Dump the chunks in an OpenSearch index.")
    parser.add_argument("index", help="Index name, e.g. proj-<project_id> or legal-rag-bench")
    parser.add_argument("--limit", type=int, default=100, help="Max chunks to print (default 100)")
    parser.add_argument("--full", action="store_true", help="Print full chunk text, not a preview")
    args = parser.parse_args()

    client = get_opensearch_client()

    if not client.indices.exists(index=args.index):
        print(f"index {args.index!r} does NOT exist")
        return

    count = client.count(index=args.index)["count"]
    knn = (
        client.indices.get_settings(index=args.index)
        .get(args.index, {})
        .get("settings", {})
        .get("index", {})
        .get("knn")
    )
    print(f"=== {args.index}: {count} chunk(s) | index.knn={knn} ===")

    res = client.search(
        index=args.index,
        body={
            "size": args.limit,
            "sort": [{"chunk_index": "asc"}],
            "_source": ["doc_id", "chunk_index", "source_uri", "content"],
        },
    )
    for hit in res["hits"]["hits"]:
        src = hit["_source"]
        text = src.get("content", "")
        head = (
            f"--- _id={hit['_id']} | doc_id={src.get('doc_id')} "
            f"| chunk_index={src.get('chunk_index')} | chars={len(text)} ---"
        )
        print(head)
        if args.full:
            print(text)
        else:
            print("   " + text[:240].replace("\n", " ") + ("..." if len(text) > 240 else ""))


if __name__ == "__main__":
    main()
