"""A tiny PDF writer for plain-text reports (B18): monospaced text, wrapped and paginated.

Enough for a printable report without a PDF library: Courier (a standard PDF font, nothing
embedded), A4 pages, one text object per page. Characters outside Latin-1 become '?'.
"""

import textwrap

PAGE_W, PAGE_H = 595, 842  # A4 in points
MARGIN = 40
FONT_SIZE = 8.5
LEADING = 11
COLUMNS = 110
LINES_PER_PAGE = int((PAGE_H - 2 * MARGIN) / LEADING)


def _escape(line: str) -> str:
    safe = line.encode("latin-1", errors="replace").decode("latin-1")
    return safe.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _lines(text: str) -> list[str]:
    out: list[str] = []
    for raw in text.splitlines():
        out.extend(
            textwrap.wrap(raw, COLUMNS, replace_whitespace=False, drop_whitespace=False) or [""]
        )
    return out


def text_to_pdf(text: str, title: str = "Report") -> bytes:
    lines = _lines(text)
    pages = [lines[i : i + LINES_PER_PAGE] for i in range(0, max(len(lines), 1), LINES_PER_PAGE)]
    objects: list[bytes] = []

    def add(body: str | bytes) -> int:
        objects.append(body.encode("latin-1") if isinstance(body, str) else body)
        return len(objects)

    catalog = add("")  # placeholder, filled below
    pages_obj = add("")
    font = add("<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>")
    page_ids: list[int] = []
    for page_lines in pages:
        ops = [f"BT /F1 {FONT_SIZE} Tf {LEADING} TL {MARGIN} {PAGE_H - MARGIN} Td"]
        ops += [f"({_escape(line)}) Tj T*" for line in page_lines]
        ops.append("ET")
        stream = "\n".join(ops).encode("latin-1")
        content = add(
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
        )
        page_ids.append(
            add(
                f"<< /Type /Page /Parent {pages_obj} 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] "
                f"/Resources << /Font << /F1 {font} 0 R >> >> /Contents {content} 0 R >>"
            )
        )
    kids = " ".join(f"{i} 0 R" for i in page_ids)
    objects[pages_obj - 1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode()
    objects[catalog - 1] = f"<< /Type /Catalog /Pages {pages_obj} 0 R >>".encode()
    info = add(f"<< /Title ({_escape(title)}) /Producer (VazhiSync) >>")

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for n, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R /Info {info} 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)
