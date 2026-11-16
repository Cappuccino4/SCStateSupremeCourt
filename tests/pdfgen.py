"""Build small, valid text PDFs for tests, with no third-party dependencies.

    make_pdf([["Page one, line one", "line two"], ["Page two"]])  -> bytes
    make_pdf_from_text("long text ...")                          -> bytes (wrapped and paginated)

Text is set in Helvetica with WinAnsiEncoding, so characters such as the en dash and
the section sign survive the round trip through pdfplumber and pypdf.
"""

from __future__ import annotations

import textwrap

LINES_PER_PAGE = 48
WRAP_WIDTH = 95


def _pdf_string(text: str) -> bytes:
    raw = text.encode("cp1252", errors="replace")
    out = bytearray(b"(")
    for b in raw:
        if b in (0x28, 0x29, 0x5C):  # ( ) backslash
            out += b"\\" + bytes([b])
        elif b < 32 or b > 126:
            out += f"\\{b:03o}".encode("ascii")
        else:
            out.append(b)
    out += b")"
    return bytes(out)


def make_pdf(pages: list[list[str]]) -> bytes:
    """Return the bytes of a PDF with one page per entry of ``pages`` (each a list of lines)."""
    if not pages:
        pages = [[""]]
    n = len(pages)
    # Object numbers: 1 catalog, 2 pages, 3 font, then (page, content) pairs.
    page_ids = [4 + 2 * i for i in range(n)]
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [" + b" ".join(f"{pid} 0 R".encode() for pid in page_ids) + f"] /Count {n} >>".encode(),
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    }
    for i, lines in enumerate(pages):
        pid, cid = page_ids[i], page_ids[i] + 1
        ops = [b"BT", b"/F1 11 Tf", b"14 TL", b"72 740 Td"]
        for j, line in enumerate(lines or [""]):
            if j:
                ops.append(b"T*")
            ops.append(_pdf_string(line) + b" Tj")
        ops.append(b"ET")
        stream = b"\n".join(ops)
        objects[pid] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {cid} 0 R >>"
        ).encode()
        objects[cid] = f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream"

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}
    for num in sorted(objects):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode() + objects[num] + b"\nendobj\n"
    xref_at = len(out)
    size = max(objects) + 1
    out += f"xref\n0 {size}\n".encode() + b"0000000000 65535 f \n"
    for num in range(1, size):
        out += f"{offsets[num]:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n".encode()
    return bytes(out)


def make_pdf_from_text(text: str, lines_per_page: int = LINES_PER_PAGE) -> bytes:
    """Wrap ``text`` at ~95 characters and paginate it. Form feeds (\\f) force a new page."""
    pages: list[list[str]] = []
    for chunk in text.split("\f"):
        lines: list[str] = []
        for para in chunk.splitlines():
            lines.extend(textwrap.wrap(para, WRAP_WIDTH) or [""])
        for start in range(0, max(len(lines), 1), lines_per_page):
            pages.append(lines[start : start + lines_per_page])
    return make_pdf(pages)
