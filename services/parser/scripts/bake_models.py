"""Build-time model bake (network ON) for offline runtime.

Downloads and warms every model the parser needs at runtime so the container can
serve with networking disabled:

  1. Docling **layout + table** models (the digital/complex-PDF path).
  2. **rapidocr** OCR models (the scanned/image path — OCR via rapidocr per spec).
  3. A real ``parse_to_markdown`` pass over the bundled fixture as a final proof
     that the baked models actually satisfy a parse (not merely "a file copied").

Run at image build with network available. It writes into the HuggingFace / model
caches that the runtime image keeps, and the runtime sets ``HF_HUB_OFFLINE=1`` so
no network call is attempted when serving.

Escalation (Textract/VLM) is deliberately NOT exercised here: it needs AWS and is
irrelevant to the offline bake — a clean digital PDF is Docling-only.
"""

from __future__ import annotations

import sys
from pathlib import Path

FIXTURE = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "hello.pdf"


def _download_docling_models() -> None:
    """Prefetch docling layout + table (+ default OCR) artifacts into the cache."""
    from docling.utils.model_downloader import download_models

    # Downloads the standard docling model set (layout + tableformer, and the
    # default OCR/enrichment artifacts) into the local model cache.
    download_models(progress=False)
    print("[bake] docling layout + table models downloaded", flush=True)


def _warm_rapidocr() -> None:
    """Force rapidocr's ONNX models to be fetched/initialised into the cache."""
    try:
        from rapidocr import RapidOCR  # rapidocr 3.x
    except ImportError:  # pragma: no cover - packaging variance across versions
        from rapidocr_onnxruntime import RapidOCR  # type: ignore[no-redef]

    RapidOCR()
    print("[bake] rapidocr OCR models initialised", flush=True)


def _prove_offline_parse() -> None:
    """Run the real never-raises entrypoint once to prove the bake is sufficient."""
    from parser_service.markdown_pipeline import parse_to_markdown

    result = parse_to_markdown(FIXTURE)
    md = (result.get("markdown") or "").strip()
    if not md:
        print(f"[bake] FAIL: empty markdown; warnings={result.get('warnings')}", file=sys.stderr)
        raise SystemExit(1)
    print(f"[bake] proof parse OK — {len(md)} chars of markdown from fixture", flush=True)


def main() -> None:
    _download_docling_models()
    _warm_rapidocr()
    _prove_offline_parse()
    print("[bake] complete", flush=True)


if __name__ == "__main__":
    main()
