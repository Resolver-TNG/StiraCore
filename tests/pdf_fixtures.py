"""Small PDFs for the web_fetch tests (#585), built in code (no binary fixture to review).

``text_pdf`` writes one Helvetica text line per page; ``blank_pdf`` pages carry no text (what
a scanned, image-only PDF gives an extractor); ``encrypted_pdf`` needs a password.
"""

from __future__ import annotations

import io


def _pdf(page_streams: list[bytes]) -> bytes:
    objects: list[bytes] = []
    count = len(page_streams)
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(count))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {count} >>".encode())
    font = 3 + 2 * count
    for index, stream in enumerate(page_streams):
        content = 4 + 2 * index
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content} 0 R "
                f"/Resources << /Font << /F1 {font} 0 R >> >> >>"
            ).encode()
        )
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return out.getvalue()


def text_pdf(lines: list[str]) -> bytes:
    def stream(line: str) -> bytes:
        safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        return f"BT /F1 12 Tf 72 720 Td ({safe}) Tj ET".encode("latin-1")

    return _pdf([stream(line) for line in lines])


def blank_pdf(pages: int = 2) -> bytes:
    return _pdf([b"0 0 m 10 10 l S" for _ in range(pages)])


def encrypted_pdf(password: str = "pw") -> bytes:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(text_pdf(["secret page"]))))
    writer.encrypt(user_password=password, owner_password=password, algorithm="RC4-128")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
