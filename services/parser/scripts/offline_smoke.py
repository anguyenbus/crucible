"""In-container OFFLINE-STARTUP smoke check (run with networking DISABLED).

Runs inside the built image after `docker run --network none`. It:

  1. Imports the app factory and asserts `/healthz` serves (service starts).
  2. Runs the real `parse_to_markdown` over the bundled fixture using ONLY baked
     models — with `HF_HUB_OFFLINE=1` set, any un-baked model would try the
     network and fail, so a non-empty markdown result is proof the bake is
     sufficient for offline runtime.

Exit 0 on success, non-zero (with a reason) on failure. This is the real,
verifiable acceptance for the model bake — NOT "a model file was copied".
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

FIXTURE = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "hello.pdf"


def _assert_offline_env() -> None:
    # Belt-and-braces: even inside `--network none`, assert docling won't reach out.
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")


def _check_healthz() -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app

    resp = TestClient(create_app()).get("/healthz")
    assert resp.status_code == 200 and resp.json() == {"status": "ok"}, resp.text
    print("[offline-smoke] /healthz 200 — service starts", flush=True)


def _check_offline_parse() -> None:
    from parser_service.markdown_pipeline import parse_to_markdown

    result = parse_to_markdown(FIXTURE)
    md = (result.get("markdown") or "").strip()
    if not md:
        print(
            f"[offline-smoke] FAIL: empty markdown offline; warnings={result.get('warnings')}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    print(f"[offline-smoke] parse OK offline — {len(md)} chars, routes={result.get('page_routes')}", flush=True)


def main() -> None:
    _assert_offline_env()
    _check_healthz()
    _check_offline_parse()
    print("[offline-smoke] PASS", flush=True)


if __name__ == "__main__":
    main()
