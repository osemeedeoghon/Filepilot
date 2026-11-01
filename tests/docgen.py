"""Builders for synthetic test documents (no personal files are ever used)."""

import io
import zipfile


def make_pdf(pages, title=None, author=None) -> bytes:
    """A minimal valid PDF. Each page is a text string, or None for a page with
    only drawing operations and no text layer (like a scanned image)."""
    objs: dict[int, bytes] = {}
    kids = []
    next_id = 4
    for text in pages:
        page_id, content_id = next_id, next_id + 1
        next_id += 2
        if text is None:
            stream = b"0.2 0.2 0.2 rg 50 50 500 700 re f"
        else:
            ops = ["BT", "/F1 12 Tf", "14 TL", "50 750 Td"]
            for line in text.split("\n"):
                esc = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
                ops.append(f"({esc}) Tj T*")
            ops.append("ET")
            stream = "\n".join(ops).encode("latin-1")
        objs[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>"
        ).encode()
        objs[content_id] = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
        kids.append(page_id)
    objs[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objs[2] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>".encode()
    objs[3] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    info_id = None
    if title or author:
        info_id = next_id
        parts = (f"/Title ({title}) " if title else "") + (f"/Author ({author}) " if author else "")
        objs[info_id] = f"<< {parts}>>".encode()

    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for i in sorted(objs):
        offsets[i] = len(out)
        out += f"{i} 0 obj\n".encode() + objs[i] + b"\nendobj\n"
    xref = len(out)
    n = max(objs) + 1
    out += f"xref\n0 {n}\n0000000000 65535 f \n".encode()
    for i in range(1, n):
        out += f"{offsets[i]:010d} 00000 n \n".encode()
    trailer = f"<< /Size {n} /Root 1 0 R" + (f" /Info {info_id} 0 R" if info_id else "") + " >>"
    out += f"trailer\n{trailer}\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def encrypt_pdf(data: bytes, user_password: str, owner_password: str = "owner") -> bytes:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(data)))
    writer.encrypt(user_password=user_password, owner_password=owner_password, algorithm="RC4-128")
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def make_docx(title=None, heading=None, paragraphs=(), table=None, core_title=None, author=None) -> bytes:
    import docx

    d = docx.Document()
    if title:
        d.add_heading(title, level=0)  # paragraph styled "Title"
    if heading:
        d.add_heading(heading, level=1)
    for p in paragraphs:
        d.add_paragraph(p)
    if table:
        t = d.add_table(rows=len(table), cols=len(table[0]))
        for r, row in enumerate(table):
            for c, value in enumerate(row):
                t.cell(r, c).text = value
    if core_title:
        d.core_properties.title = core_title
    if author is not None:
        d.core_properties.author = author
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def add_zip_member(data: bytes, name: str, payload: bytes) -> bytes:
    src = zipfile.ZipFile(io.BytesIO(data))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            dst.writestr(item, src.read(item.filename))
        dst.writestr(name, payload)
    return buf.getvalue()


def make_zip_bomb_docx(expanded_bytes: int) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", b"0" * expanded_bytes)
    return buf.getvalue()
