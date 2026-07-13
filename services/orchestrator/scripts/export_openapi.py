"""
Deterministic OpenAPI export for the orchestrator service.

This is the SINGLE code path behind the committed contract at
``services/orchestrator/openapi.json``: humans run it via
``make orchestrator-openapi`` (repo root) and the Tier 1 pre-commit gate runs
it with ``--check`` to fail on any uncommitted contract drift. Serialization
is byte-stable (sorted keys, fixed indent, trailing newline) so a re-export of
an unchanged app is always a no-op.

Usage (from services/orchestrator/):
    uv run python scripts/export_openapi.py            # write openapi.json
    uv run python scripts/export_openapi.py --check    # fail if it drifted
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Final

# Committed contract location, anchored on this file (never CWD-relative).
OPENAPI_PATH: Final[Path] = Path(__file__).resolve().parents[1] / "openapi.json"


def render_openapi_document() -> str:
    """
    Render the FastAPI app's OpenAPI document as a deterministic string.

    Sorted keys + fixed indent + trailing newline make the output byte-stable,
    so byte comparison (Tier 1 gate, pytest tripwire) detects exactly the
    contract changes and nothing else.
    """
    from app.main import app

    return json.dumps(app.openapi(), sort_keys=True, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Export the OpenAPI document, or verify the committed copy with --check."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit 1 if the committed openapi.json differs from a re-export",
    )
    args = parser.parse_args(argv)

    rendered = render_openapi_document()

    if args.check:
        committed = OPENAPI_PATH.read_bytes() if OPENAPI_PATH.is_file() else None
        if committed != rendered.encode("utf-8"):
            print(
                f"ERROR: {OPENAPI_PATH} does not match a fresh export of the app's "
                f"OpenAPI document (uncommitted contract drift).\n"
                f"Run `make orchestrator-openapi` from the repo root and commit the result.",
                file=sys.stderr,
            )
            return 1
        print("openapi.json matches the app's OpenAPI document.")
        return 0

    OPENAPI_PATH.write_bytes(rendered.encode("utf-8"))
    print(f"Exported OpenAPI document to {OPENAPI_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
