"""Parse stage: raw document bytes → plain text, dispatched by extension.

Format scope (project-scoped chat): markdown (existing) + PDF (native text via
pypdf). The extracted text flows into the EXISTING normalize → chunk → embed →
index stages unchanged — this stage only turns bytes into text.

Dispatch by source extension:
  - `.pdf`              → pypdf native-text extraction (`extract_pdf_text`)
  - `.md` / `.markdown` → UTF-8 decode (markdown path, unchanged)
  - anything else       → `InvalidSourceError` (honest, never a silent pass)

Scanned / image-only PDFs (no extractable text layer) raise the typed
`NoExtractableTextError` → mapped to a 422 by the API — never a silent empty
index. DOCX / image / OCR remain OUT (fast-follow): no mammoth, no Textract.

pypdf pattern reference: `references/donna/backend/app/services/pdf.py`.
"""

from io import BytesIO

from app.pipeline.fetch import InvalidSourceError


class NoExtractableTextError(ValueError):
    """A PDF carried no extractable native text (scanned / image-only).

    Mapped to HTTP 422 by the API layer — an honest failure, never a silent
    empty index. OCR of scanned PDFs is out of scope (fast-follow).
    """


def extract_text(source: str, data: bytes) -> str:
    """Turn raw `data` bytes into plain text, dispatching on `source`'s extension."""
    lower = source.lower()
    if lower.endswith(".pdf"):
        return extract_pdf_text(data)
    if lower.endswith((".md", ".markdown")):
        return data.decode("utf-8")
    raise InvalidSourceError(
        f"Unsupported document type for source {source!r}: expected a .pdf, "
        ".md, or .markdown file."
    )


def extract_pdf_text(data: bytes) -> str:
    """Extract native text from a PDF's bytes with pypdf.

    Pages are extracted in order and joined with blank lines; per-page text is
    whitespace-normalized. A PDF with NO extractable text (scanned / image-only)
    raises `NoExtractableTextError` rather than returning an empty string that
    would silently index nothing.
    """
    # Import inside the function so importing this module stays cheap and the
    # dependency surface is explicit at the call site.
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(data))
    parts: list[str] = []
    for page in reader.pages:
        text_content = page.extract_text() or ""
        normalized = " ".join(text_content.split())
        if normalized:
            parts.append(normalized)

    text = "\n\n".join(parts)
    if not text.strip():
        raise NoExtractableTextError(
            "The PDF has no extractable text (it is likely scanned or "
            "image-only). OCR of scanned PDFs is not supported."
        )
    return text
