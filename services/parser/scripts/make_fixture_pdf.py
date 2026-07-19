"""Generate the tiny digital-text PDF fixture used by the offline smoke test.

A digital PDF with a real text layer is the ideal offline-smoke input: Docling's
quality gate keeps it (route ``docling-kept``) with NO escalation call, so the
parse runs fully offline and needs no AWS — it exercises only the baked docling
layout/table models. Regenerate with:

    python scripts/make_fixture_pdf.py

Writes ``tests/fixtures/hello.pdf`` deterministically (xref offsets computed, no
external deps).
"""

from __future__ import annotations

from pathlib import Path

TEXT = "Hello Parser Service offline smoke"


def build_pdf() -> bytes:
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 320 120] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\nBT /F1 18 Tf 20 60 Td (%s) Tj ET\nendstream"
        % (len(b"BT /F1 18 Tf 20 60 Td (%s) Tj ET" % TEXT.encode()), TEXT.encode()),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i
        out += body
        out += b"\nendobj\n"

    xref_pos = len(out)
    n = len(objects) + 1
    out += b"xref\n0 %d\n" % n
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\n" % n
    out += b"startxref\n%d\n%%%%EOF\n" % xref_pos
    return bytes(out)


def main() -> None:
    dest = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "hello.pdf"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(build_pdf())
    print(f"wrote {dest} ({dest.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
