"""Read-only document inspection: safe reading and text extraction.

Supported: PDF, plain text, Markdown, DOCX and CSV. Files are opened inside the
selected folder only, without following symbolic links (see
`duplicates.open_beneath`), and read up to a size limit. Parsing happens in a
short-lived child process that is killed if it exceeds the time limit, so a
hostile or broken document cannot hang the server.

Nothing in a document is ever executed: no macros, scripts, links or embedded
instructions. Extracted text is treated as untrusted data and is only
displayed, summarised by rules, or (optionally) passed to a local model as
clearly delimited data.
"""

from __future__ import annotations

import csv
import errno
import io
import json
import os
import re
import stat
import subprocess
import sys
import time
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .config import Settings, settings as default_settings
from .duplicates import open_beneath
from .organizer import PlanBoundaryError, ensure_within_root

# extension -> (kind, label)
SUPPORTED = {
    "pdf": ("pdf", "PDF document"),
    "txt": ("text", "Plain text"),
    "md": ("markdown", "Markdown"),
    "markdown": ("markdown", "Markdown"),
    "docx": ("docx", "Word document (DOCX)"),
    "csv": ("csv", "CSV table"),
}
TEXT_KINDS = frozenset({"text", "markdown", "csv"})

# Extraction statuses
OK, PARTIAL, EMPTY, NEEDS_OCR, ENCRYPTED, UNSUPPORTED, TOO_LARGE, ERROR, TIMEOUT = (
    "ok", "partial", "empty", "needs_ocr", "encrypted", "unsupported", "too_large", "error", "timeout",
)

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏‪-‮⁦-⁩]")


class DocumentAccessError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


@dataclass
class Extraction:
    status: str
    message: str
    text: str = ""
    facts: list[dict] = field(default_factory=list)  # {"label", "value", "source"}
    warnings: list[str] = field(default_factory=list)
    title: str | None = None
    columns: list[str] = field(default_factory=list)  # CSV header, used by the rules

    def fact(self, label: str, value, source: str) -> None:
        if value not in (None, "", []):
            self.facts.append({"label": label, "value": value, "source": source})


def clean_text(text: str) -> str:
    """Normalise newlines and strip control/bidi characters that could disguise content."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return _CONTROL_CHARS.sub("", text)


def _short(value: object, limit: int = 300) -> str:
    s = clean_text(str(value)).strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


# --- reading ----------------------------------------------------------------------

def read_beneath(root: Path, rel: str, limit: int) -> tuple[bytes, os.stat_result, bool]:
    """Read up to `limit` bytes of root/rel without following symlinks.

    Returns (data, stat, truncated).
    """
    try:
        fd = open_beneath(root, rel)
    except PlanBoundaryError:
        raise DocumentAccessError("path_traversal", "That file is outside the selected folder.") from None
    except FileNotFoundError:
        raise DocumentAccessError("not_found", "That file no longer exists.", 404) from None
    except PermissionError:
        raise DocumentAccessError("permission_denied", "FilePilot does not have permission to read that file.", 403) from None
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise DocumentAccessError("symlink", "That path goes through a symbolic link, which FilePilot does not follow.") from None
        raise DocumentAccessError("os_error", f"The file could not be opened ({exc.strerror or 'error'}).") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise DocumentAccessError("not_a_file", "That path is not a regular file.")
        chunks, remaining = [], min(limit, st.st_size)
        while remaining > 0:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks), st, st.st_size > limit
    except OSError as exc:
        raise DocumentAccessError("os_error", f"The file could not be read ({exc.strerror or 'error'}).") from None
    finally:
        os.close(fd)


def decode_text(data: bytes, truncated: bool) -> tuple[str, str]:
    """Decode bytes as text. Returns (text, encoding). Raises ValueError for binary data."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace"), "UTF-8 (with BOM)"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace"), "UTF-16"
    if b"\x00" in data[:8192]:
        raise ValueError("binary")
    try:
        return data.decode("utf-8"), "UTF-8"
    except UnicodeDecodeError as exc:
        # A truncated read may cut a multi-byte character in half.
        if truncated and exc.start >= len(data) - 3:
            return data[: exc.start].decode("utf-8"), "UTF-8"
    return data.decode("cp1252", errors="replace"), "Windows-1252 (not valid UTF-8)"


# --- extractors (pure functions on bytes; run in the child process) ---------------

def extract_plain(data: bytes, kind: str, truncated: bool, cfg: Settings) -> Extraction:
    try:
        text, encoding = decode_text(data, truncated)
    except ValueError:
        return Extraction(ERROR, "This file contains binary data, so it isn't a readable text file.")
    text = clean_text(text)
    ex = Extraction(OK, "Text read directly from the file.", text=text)
    ex.fact("Encoding", encoding, "detected from the file's bytes")
    ex.fact("Lines", text.count("\n") + (1 if text and not text.endswith("\n") else 0), "counted")
    if kind == "markdown":
        title = markdown_title(text)
        if title:
            ex.title = title
            ex.fact("Title", title, "first Markdown heading")
        ex.fact("Headings", markdown_heading_count(text), "counted (outside code blocks)")
    if not text.strip():
        ex.status, ex.message = EMPTY, "The file is empty or contains only whitespace."
    return ex


def markdown_title(text: str) -> str | None:
    in_fence = False
    lines = text.split("\n")
    for i, line in enumerate(lines[:400]):
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = re.match(r"^#\s+(.+?)\s*#*\s*$", line)
        if m:
            return _short(m.group(1), 200)
        if line.strip() and i + 1 < len(lines) and re.match(r"^=+\s*$", lines[i + 1]):
            return _short(line, 200)
    return None


def markdown_heading_count(text: str) -> int:
    count, in_fence = 0, False
    for line in text.split("\n"):
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
        elif not in_fence and re.match(r"^#{1,6}\s+\S", line):
            count += 1
    return count


def markdown_to_plain(text: str) -> str:
    """Strip common Markdown syntax for analysis (the preview keeps the original)."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"^\s*[-*+>]\s+", "", text, flags=re.M)
    text = re.sub(r"[*_`]{1,3}", "", text)
    return text


def extract_csv(data: bytes, truncated: bool, cfg: Settings) -> Extraction:
    try:
        text, encoding = decode_text(data, truncated)
    except ValueError:
        return Extraction(ERROR, "This file contains binary data, so it isn't a readable CSV file.")
    text = clean_text(text)
    if not text.strip():
        return Extraction(EMPTY, "The CSV file is empty.")
    sample = text[:64 * 1024]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","
    rows: list[list[str]] = []
    total_rows = 0
    widths: dict[int, int] = {}
    try:
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        for row in reader:
            if not any(cell.strip() for cell in row):
                continue
            total_rows += 1
            widths[len(row)] = widths.get(len(row), 0) + 1
            if len(rows) < 200:
                rows.append([_short(c, 200) for c in row[:50]])
    except csv.Error as exc:
        return Extraction(ERROR, f"The CSV file is malformed ({_short(exc, 120)}).")
    if not rows:
        return Extraction(EMPTY, "The CSV file has no data rows.")

    header = rows[0]
    try:
        has_header = csv.Sniffer().has_header(sample)
    except csv.Error:
        has_header = True
    if has_header:
        columns = [c.strip() for c in header]
        body = rows[1:]
    else:
        columns = [f"Column {i + 1}" for i in range(len(header))]
        body = rows
    names = {",": "comma", ";": "semicolon", "\t": "tab", "|": "pipe"}

    # Readable text: header plus rows as "col: value" lines.
    lines = [" | ".join(columns)]
    for row in body[:100]:
        lines.append(" | ".join(row))
    ex = Extraction(OK, "Table read directly from the file.", text="\n".join(lines), columns=columns)
    ex.fact("Encoding", encoding, "detected from the file's bytes")
    ex.fact("Delimiter", names.get(delimiter, repr(delimiter)), "detected from the first rows")
    ex.fact("Columns", len(columns), "counted")
    ex.fact("Column names", ", ".join(columns[:30]) + ("…" if len(columns) > 30 else ""),
            "header row" if has_header else "no header row detected")
    ex.fact("Data rows", total_rows - (1 if has_header else 0), "counted" + (" in the part that was read" if truncated else ""))
    if len(widths) > 1:
        common = max(widths, key=widths.get)
        odd = sum(n for w, n in widths.items() if w != common)
        ex.warnings.append(f"{odd} row(s) have a different number of columns than the rest.")
    return ex


def _pdf_date(value) -> str | None:
    try:
        return value.astimezone(timezone.utc).date().isoformat() if value else None
    except Exception:
        return None


def extract_pdf(data: bytes, cfg: Settings) -> Extraction:
    from pypdf import PdfReader
    from pypdf.errors import DependencyError, PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
    except (PdfReadError, ValueError, KeyError, TypeError, OSError) as exc:
        return Extraction(ERROR, f"This PDF is damaged or not a real PDF ({_short(exc, 120)}).")

    notes = []
    if reader.is_encrypted:
        try:
            ok = reader.decrypt("")
        except (DependencyError, NotImplementedError):
            return Extraction(ENCRYPTED, "This PDF is encrypted with a method FilePilot can't open. Open it in Preview to read it.")
        except Exception:
            ok = 0
        if not ok:
            return Extraction(ENCRYPTED, "This PDF is password-protected. FilePilot does not ask for or try passwords.")
        notes.append("The PDF is encrypted but has no open password, so it could be read.")

    try:
        page_count = len(reader.pages)
    except Exception as exc:
        return Extraction(ERROR, f"This PDF's page list is damaged ({_short(exc, 120)}).")

    ex = Extraction(OK, "Text extracted from the PDF's text layer.")
    ex.fact("Pages", page_count, "PDF structure")
    try:
        meta = reader.metadata
    except Exception:
        meta = None
    if meta:
        title = _short(meta.title, 200) if getattr(meta, "title", None) else None
        if title and title.lower() not in ("untitled", "untitled document"):
            ex.title = title
            ex.fact("Title", title, "PDF metadata")
        ex.fact("Author", _short(meta.author, 200) if getattr(meta, "author", None) else None, "PDF metadata")
        try:
            ex.fact("Created", _pdf_date(meta.creation_date), "PDF metadata")
        except Exception:
            pass

    deadline = time.monotonic() + cfg.doc_timeout_seconds * 0.8
    texts, empty_pages, read_pages, total_chars = [], 0, 0, 0
    for i, page in enumerate(reader.pages):
        if i >= cfg.doc_max_pages:
            ex.warnings.append(f"Only the first {cfg.doc_max_pages} pages were read.")
            break
        if time.monotonic() > deadline:
            ex.warnings.append(f"Stopped after {i} pages because of the time limit.")
            break
        try:
            t = page.extract_text() or ""
        except Exception:
            t = ""
            ex.warnings.append(f"Page {i + 1} could not be read.")
        read_pages += 1
        t = clean_text(t).strip()
        if len(t) < 5:
            empty_pages += 1
        else:
            texts.append(t)
            total_chars += len(t)
        if total_chars > cfg.doc_max_chars:
            ex.warnings.append("Text extraction stopped at the character limit.")
            break

    ex.text = "\n\n".join(texts)
    ex.warnings.extend(notes)
    if page_count == 0:
        ex.status, ex.message = EMPTY, "This PDF has no pages."
    elif not texts:
        ex.status = NEEDS_OCR
        ex.message = ("No text layer was found. This PDF is probably scanned images, so its text would need "
                      "OCR (optical character recognition), which FilePilot does not do yet.")
    elif empty_pages:
        ex.status = PARTIAL
        ex.message = f"Text extracted from {read_pages - empty_pages} of {read_pages} pages."
        ex.warnings.append(f"{empty_pages} page(s) have no text layer (they may be scanned images that need OCR).")
    if ex.warnings and ex.status == OK and any("Only the first" in w or "Stopped" in w for w in ex.warnings):
        ex.status = PARTIAL
    return ex


def extract_docx(data: bytes, cfg: Settings) -> Extraction:
    if data.startswith(b"\xd0\xcf\x11\xe0"):
        return Extraction(ENCRYPTED, "This Word file is password-protected or in the old .doc format, so it can't be read.")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return Extraction(ERROR, "This file is not a valid DOCX document (it is not a DOCX zip package).")
    with zf:
        names = set(zf.namelist())
        if "word/document.xml" not in names:
            return Extraction(ERROR, "This file is not a valid DOCX document (no Word content found).")
        expanded = sum(i.file_size for i in zf.infolist())
        if expanded > cfg.doc_max_unzipped_bytes:
            return Extraction(TOO_LARGE, f"This DOCX expands to {expanded // (1024 * 1024)} MB when unpacked, over the safety limit.")
        has_macros = any(n.lower().endswith("vbaproject.bin") for n in names)

    import docx
    from docx.opc.exceptions import PackageNotFoundError

    try:
        document = docx.Document(io.BytesIO(data))
    except (ValueError, KeyError, PackageNotFoundError, zipfile.BadZipFile) as exc:
        return Extraction(ERROR, f"This DOCX could not be read (it may be macro-enabled or damaged: {_short(exc, 100)}).")
    except Exception as exc:  # lxml errors for broken XML
        return Extraction(ERROR, f"This DOCX is damaged ({type(exc).__name__}).")

    ex = Extraction(OK, "Text extracted from the document's paragraphs and tables.")
    paragraphs, headings, first_heading, styled_title = [], 0, None, None
    for p in document.paragraphs:
        t = clean_text(p.text).strip()
        if not t:
            continue
        style = (p.style.name if p.style is not None else "") or ""
        if style == "Title" and styled_title is None:
            styled_title = t
        if style.startswith("Heading"):
            headings += 1
            if first_heading is None:
                first_heading = t
        paragraphs.append(t)
    table_lines = []
    for table in document.tables[:50]:
        for row in table.rows[:200]:
            cells = [clean_text(c.text).strip() for c in row.cells]
            if any(cells):
                table_lines.append(" | ".join(cells))
    ex.text = "\n".join(paragraphs + ([""] + table_lines if table_lines else []))[: cfg.doc_max_chars]

    props = document.core_properties
    meta_title = _short(props.title, 200) if props.title else None
    if meta_title:
        ex.title, source = meta_title, "document properties"
    elif styled_title:
        ex.title, source = _short(styled_title, 200), "paragraph styled “Title”"
    elif first_heading:
        ex.title, source = _short(first_heading, 200), "first heading"
    if ex.title:
        ex.fact("Title", ex.title, source)
    ex.fact("Author", _short(props.author, 200) if props.author else None, "document properties")
    for label, value in (("Created", props.created), ("Last modified", props.modified)):
        if isinstance(value, datetime):
            ex.fact(label, value.date().isoformat(), "document properties")
    ex.fact("Paragraphs", len(paragraphs), "counted")
    ex.fact("Headings", headings, "counted")
    ex.fact("Tables", len(document.tables), "counted")
    if has_macros:
        ex.warnings.append("This file contains macros. They were not run.")
    if not ex.text.strip():
        ex.status, ex.message = EMPTY, "This document has no text."
    return ex


def run_extractor(kind: str, data: bytes, truncated: bool, cfg: Settings) -> Extraction:
    if kind == "pdf":
        return extract_pdf(data, cfg)
    if kind == "docx":
        return extract_docx(data, cfg)
    if kind == "csv":
        return extract_csv(data, truncated, cfg)
    return extract_plain(data, kind, truncated, cfg)


_WORKER = Path(__file__).with_name("_extract_worker.py")


def extract_isolated(kind: str, data: bytes, truncated: bool, cfg: Settings) -> Extraction:
    """Run the extractor in a separate, isolated Python process with a hard time limit."""
    if not cfg.doc_isolate:
        try:
            return run_extractor(kind, data, truncated, cfg)
        except Exception as exc:
            return Extraction(ERROR, f"The document could not be read ({type(exc).__name__}).")
    header = json.dumps({"kind": kind, "truncated": truncated, "cfg": asdict(cfg)}).encode() + b"\n"
    try:
        proc = subprocess.run(
            [sys.executable, "-I", str(_WORKER)],
            input=header + data,
            capture_output=True,
            timeout=cfg.doc_timeout_seconds,
            env={"PATH": os.environ.get("PATH", ""), "LANG": "en_US.UTF-8"},
            check=False,
        )
    except subprocess.TimeoutExpired:
        return Extraction(TIMEOUT, f"Reading this document took longer than {cfg.doc_timeout_seconds:g} seconds, so it was stopped.")
    if proc.returncode != 0 or not proc.stdout:
        return Extraction(ERROR, "The document reader stopped unexpectedly. The file may be damaged.")
    try:
        return Extraction(**json.loads(proc.stdout))
    except (ValueError, TypeError):
        return Extraction(ERROR, "The document reader returned an unexpected result.")


# --- orchestration -----------------------------------------------------------------

def document_kind(name: str) -> tuple[str, str] | None:
    suffix = PurePosixPath(name).suffix.lower().lstrip(".")
    return SUPPORTED.get(suffix)


def inspect_document(
    raw_root: object,
    relative_path: object,
    *,
    cfg: Settings = default_settings,
) -> dict:
    """Extract facts and text from one document inside the selected folder."""
    from . import doc_rules
    from .scanner import validate_root

    root = validate_root(raw_root, cfg)
    if not isinstance(relative_path, str) or len(relative_path) > cfg.max_path_length:
        raise DocumentAccessError("invalid_path", "The file path is not valid.")
    rel = relative_path  # used exactly as given; absolute or ".." paths are refused below
    try:
        ensure_within_root(root, rel)
    except PlanBoundaryError:
        raise DocumentAccessError("path_traversal", "That file is outside the selected folder.") from None

    name = rel.rsplit("/", 1)[-1]
    kind_info = document_kind(name)
    report: dict = {
        "root": str(root),
        "relative_path": rel,
        "name": name,
        "kind": kind_info[0] if kind_info else None,
        "type_label": kind_info[1] if kind_info else "Unsupported type",
    }
    started = time.monotonic()
    if kind_info is None:
        report.update(_result(Extraction(
            UNSUPPORTED,
            "FilePilot can inspect PDF, TXT, Markdown, DOCX and CSV files. This file type isn't supported.",
        ), size=None, modified=None, truncated=False, cfg=cfg, started=started))
        return report

    kind = kind_info[0]
    limit = cfg.doc_text_read_bytes if kind in TEXT_KINDS else cfg.doc_max_bytes + 1
    data, st, truncated = read_beneath(root, rel, limit)
    modified = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(timespec="seconds")
    if kind not in TEXT_KINDS and st.st_size > cfg.doc_max_bytes:
        report.update(_result(Extraction(
            TOO_LARGE,
            f"This file is larger than the {cfg.doc_max_bytes // (1024 * 1024)} MB limit for {kind_info[1]} files, so it was not opened.",
        ), size=st.st_size, modified=modified, truncated=False, cfg=cfg, started=started))
        return report

    ex = extract_isolated(kind, data, truncated, cfg)
    if truncated and kind in TEXT_KINDS:
        ex.warnings.append(f"Only the first {cfg.doc_text_read_bytes // (1024 * 1024)} MB of this file was read.")
        if ex.status == OK:
            ex.status = PARTIAL
            ex.message = "The beginning of the file was read; the rest was skipped because the file is large."
    if len(ex.text) > cfg.doc_max_chars:
        ex.text = ex.text[: cfg.doc_max_chars]
        ex.warnings.append("Extracted text was cut at the character limit.")
    report.update(_result(ex, size=st.st_size, modified=modified, truncated=truncated, cfg=cfg, started=started))

    analysis_text = markdown_to_plain(ex.text) if kind == "markdown" else ex.text
    report["analysis"] = (
        doc_rules.analyze(analysis_text, kind=kind, title=ex.title, columns=ex.columns)
        if ex.status in (OK, PARTIAL) else None
    )
    report["_text"] = analysis_text  # internal: used for optional AI, removed before responding
    return report


def _result(ex: Extraction, *, size, modified, truncated, cfg: Settings, started: float) -> dict:
    words = len(re.findall(r"\w+", ex.text))
    facts = [{"label": "Size", "value": size, "source": "file system"}] if size is not None else []
    if modified:
        facts.append({"label": "Modified", "value": modified, "source": "file system"})
    facts.extend(ex.facts)
    if ex.text:
        facts.append({"label": "Words", "value": words, "source": "counted in extracted text"})
        facts.append({"label": "Characters", "value": len(ex.text), "source": "counted in extracted text"})
    preview = ex.text[: cfg.doc_preview_chars]
    return {
        "size": size,
        "modified": modified,
        "extraction": {
            "status": ex.status,
            "message": ex.message,
            "warnings": ex.warnings,
            "read_truncated": truncated,
            "preview": preview,
            "preview_truncated": len(ex.text) > len(preview),
            "char_count": len(ex.text),
            "word_count": words,
            "duration_ms": int((time.monotonic() - started) * 1000),
        },
        "title": ex.title,
        "facts": facts,
        "analysis": None,
    }
