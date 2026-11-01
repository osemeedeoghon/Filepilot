"""FastAPI application: serves the FilePilot UI and a small read-only API."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Path as PathParam, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import picker
from .config import settings
from .documents import DocumentAccessError, inspect_document
from .jobs import DuplicateJobManager, JobBusy
from .local_ai import AIResponseError, AIUnavailable, OllamaProvider
from .organizer import CATEGORIES, preview_organization
from .scanner import ScanError, scan_folder

logger = logging.getLogger("filepilot")

STATIC_DIR = Path(__file__).parent / "static"
ALLOWED_HOSTS = ["127.0.0.1", "localhost", "testserver"]
ALLOWED_ORIGINS = {
    f"http://127.0.0.1:{settings.port}",
    f"http://localhost:{settings.port}",
    "http://testserver",
}

# At most two scans at once; extra requests get a clear "busy" response.
_scan_slots = threading.BoundedSemaphore(2)
_picker_lock = threading.Lock()
duplicate_jobs = DuplicateJobManager()
_doc_slots = threading.BoundedSemaphore(2)
_ai_lock = threading.Lock()
ai_provider = OllamaProvider()
JobId = PathParam(pattern=r"^[A-Za-z0-9_-]{16,64}$")

app = FastAPI(title="FilePilot", version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None)
# Rejects requests whose Host header is not loopback (blocks DNS-rebinding).
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)


@app.middleware("http")
async def guard_requests(request: Request, call_next):
    if request.method not in ("GET", "HEAD"):
        origin = request.headers.get("origin")
        if origin is not None and origin not in ALLOWED_ORIGINS:
            return _error(403, "forbidden_origin", "Requests from other websites are not allowed.")
        if not request.headers.get("content-type", "").startswith("application/json"):
            return _error(415, "unsupported_media_type", "Requests must be sent as JSON.")
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


@app.exception_handler(ScanError)
async def scan_error_handler(_request: Request, exc: ScanError) -> JSONResponse:
    return _error(exc.status, exc.code, exc.message)


@app.exception_handler(DocumentAccessError)
async def document_error_handler(_request: Request, exc: DocumentAccessError) -> JSONResponse:
    return _error(exc.status, exc.code, exc.message)


@app.exception_handler(Exception)
async def unexpected_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unexpected error", exc_info=exc)
    return _error(500, "internal_error", "Something went wrong while scanning. No files were changed.")


class ScanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=settings.max_path_length)
    include_hidden: StrictBool = False


Category = Literal[CATEGORIES]


class OrganizePreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=settings.max_path_length)
    categories: list[Category] = Field(default_factory=lambda: list(CATEGORIES), max_length=len(CATEGORIES))
    scope: Literal["top_level", "all"] = "top_level"
    include_hidden: StrictBool = False


class DuplicateScanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=settings.max_path_length)
    include_hidden: StrictBool = False


class DocumentInspectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=settings.max_path_length)
    relative_path: str = Field(min_length=1, max_length=settings.max_path_length)
    use_ai: StrictBool = False


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "picker_available": picker.picker_available(),
        "organize_categories": list(CATEGORIES),
        "limits": {
            "max_entries": settings.max_entries,
            "max_depth": settings.max_depth,
            "max_scan_seconds": settings.max_scan_seconds,
            "max_path_length": settings.max_path_length,
            "dup_max_seconds": settings.dup_max_seconds,
            "dup_max_bytes": settings.dup_max_bytes,
            "doc_max_bytes": settings.doc_max_bytes,
            "doc_text_read_bytes": settings.doc_text_read_bytes,
            "doc_timeout_seconds": settings.doc_timeout_seconds,
        },
    }


@app.post("/api/pick-folder")
def pick_folder() -> JSONResponse:
    if not _picker_lock.acquire(blocking=False):
        return _error(409, "picker_open", "A folder picker is already open.")
    try:
        path = picker.choose_folder()
    except picker.PickerCancelled as exc:
        return JSONResponse({"cancelled": True, "message": str(exc)})
    except picker.PickerUnavailable as exc:
        return _error(501, "picker_unavailable", str(exc))
    finally:
        _picker_lock.release()
    return JSONResponse({"cancelled": False, "path": path})


@app.post("/api/scan")
def scan(body: ScanRequest) -> JSONResponse:
    if not _scan_slots.acquire(blocking=False):
        return _error(429, "busy", "Another scan is already running. Try again in a moment.")
    try:
        result = scan_folder(body.path, include_hidden=body.include_hidden)
    finally:
        _scan_slots.release()
    return JSONResponse(result.to_dict())


@app.post("/api/organize/preview")
def organize_preview(body: OrganizePreviewRequest) -> JSONResponse:
    """Propose an organization plan. Read-only: nothing is created, moved or changed."""
    if not _scan_slots.acquire(blocking=False):
        return _error(429, "busy", "Another scan is already running. Try again in a moment.")
    try:
        plan = preview_organization(
            body.path,
            categories=list(dict.fromkeys(body.categories)),
            scope=body.scope,
            include_hidden=body.include_hidden,
        )
    finally:
        _scan_slots.release()
    return JSONResponse(plan.to_dict())


@app.post("/api/duplicates/start", status_code=202)
def duplicates_start(body: DuplicateScanRequest) -> JSONResponse:
    """Start a read-only duplicate scan in the background. Nothing is changed."""
    try:
        job = duplicate_jobs.start(body.path, include_hidden=body.include_hidden)
    except JobBusy as exc:
        return JSONResponse(
            status_code=409,
            content={"error": {"code": "busy", "message": str(exc), "job_id": exc.job_id}},
        )
    return JSONResponse(status_code=202, content=job.to_dict())


@app.get("/api/duplicates/{job_id}")
def duplicates_status(job_id: str = JobId) -> JSONResponse:
    job = duplicate_jobs.get(job_id)
    if job is None:
        return _error(404, "unknown_job", "That duplicate scan is no longer available. Start a new one.")
    return JSONResponse(job.to_dict())


@app.post("/api/duplicates/{job_id}/cancel")
def duplicates_cancel(job_id: str = JobId) -> JSONResponse:
    job = duplicate_jobs.cancel(job_id)
    if job is None:
        return _error(404, "unknown_job", "That duplicate scan is no longer available.")
    return JSONResponse({"id": job.id, "status": job.status, "cancel_requested": True})


@app.get("/api/ai/status")
def ai_status() -> JSONResponse:
    """Report whether a local Ollama model is available. Never installs or downloads anything."""
    return JSONResponse(ai_provider.status())


@app.post("/api/documents/inspect")
def documents_inspect(body: DocumentInspectRequest) -> JSONResponse:
    """Extract text and facts from one document inside the selected folder. Read-only."""
    if not _doc_slots.acquire(blocking=False):
        return _error(429, "busy", "Another document is being read. Try again in a moment.")
    try:
        report = inspect_document(body.path, body.relative_path)
    finally:
        _doc_slots.release()

    text = report.pop("_text", "")
    report["ai"] = {"requested": body.use_ai, "result": None, "error": None}
    if body.use_ai:
        if not text.strip() or report["extraction"]["status"] not in ("ok", "partial"):
            report["ai"]["error"] = "There is no extracted text for the local model to read."
        elif not _ai_lock.acquire(blocking=False):
            report["ai"]["error"] = "The local model is busy with another document. Try again in a moment."
        else:
            try:
                result = ai_provider.analyze(text, kind=report["kind"], title=report.get("title"))
                report["ai"]["result"] = result.to_dict()
            except (AIUnavailable, AIResponseError) as exc:
                report["ai"]["error"] = str(exc)
            finally:
                _ai_lock.release()
    return JSONResponse(report)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
