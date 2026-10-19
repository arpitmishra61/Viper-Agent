"""Build small PDF / Word / Excel files for tests and eval tasks (no reportlab needed)."""

from __future__ import annotations

import io


def make_pdf(pages: list[str]) -> bytes:
    """A minimal valid PDF, one page per string (ASCII text; '\\n' starts a new line)."""
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    n = len(pages)
    font_id = 3 + 2 * n
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    objs: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode(),
    ]
    for i, text in enumerate(pages):
        lines = text.split("\n") if text else [""]
        stream = ("BT /F1 12 Tf 72 720 Td 14 TL " + " T* ".join(f"({esc(l)}) Tj" for l in lines) + " ET").encode("latin-1", "replace")
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {4 + 2 * i} 0 R "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>".encode()
        )
        objs.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def make_docx(blocks: list) -> bytes:
    """blocks: ('h1', text) | ('p', text) | ('table', [[cell, ...], ...])"""
    from docx import Document

    doc = Document()
    for kind, value in blocks:
        if kind == "h1":
            doc.add_heading(value, level=1)
        elif kind == "p":
            doc.add_paragraph(value)
        elif kind == "table":
            t = doc.add_table(rows=len(value), cols=len(value[0]))
            for r, row in enumerate(value):
                for c, cell in enumerate(row):
                    t.cell(r, c).text = str(cell)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def make_xlsx(sheets: dict[str, list[list]]) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
