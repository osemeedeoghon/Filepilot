"""In-memory background jobs for long, read-only duplicate scans.

One duplicate scan runs at a time. The browser polls its progress and may
cancel it. Finished jobs are kept briefly (a few at most) and then forgotten.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from .config import Settings, settings as default_settings
from .duplicates import DuplicateReport, Progress, find_duplicates
from .scanner import ScanError, validate_root

logger = logging.getLogger("filepilot")

RUNNING, COMPLETE, CANCELLED, FAILED = "running", "complete", "cancelled", "failed"


class JobBusy(Exception):
    def __init__(self, job_id: str):
        super().__init__("A duplicate scan is already running.")
        self.job_id = job_id


@dataclass
class DuplicateJob:
    id: str
    root: str
    include_hidden: bool
    status: str = RUNNING
    progress: Progress = field(default_factory=Progress)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    result: DuplicateReport | None = None
    error: dict | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "root": self.root,
            "status": self.status,
            "progress": self.progress.to_dict(),
            "result": self.result.to_dict() if self.result else None,
            "error": self.error,
        }


class DuplicateJobManager:
    def __init__(self, keep: int = 5, cfg: Settings = default_settings):
        self._jobs: OrderedDict[str, DuplicateJob] = OrderedDict()
        self._lock = threading.Lock()
        self._keep = keep
        self._cfg = cfg

    def start(self, raw_path: object, include_hidden: bool = False) -> DuplicateJob:
        root = validate_root(raw_path, self._cfg)  # fail fast with a clear error
        with self._lock:
            for job in self._jobs.values():
                if job.status == RUNNING:
                    raise JobBusy(job.id)
            job = DuplicateJob(id=secrets.token_urlsafe(16), root=str(root), include_hidden=include_hidden)
            self._jobs[job.id] = job
            while len(self._jobs) > self._keep:
                oldest = next(iter(self._jobs))
                if self._jobs[oldest].status == RUNNING:
                    break
                self._jobs.pop(oldest)
        threading.Thread(target=self._run, args=(job,), name=f"dup-{job.id[:6]}", daemon=True).start()
        return job

    def _run(self, job: DuplicateJob) -> None:
        try:
            report = find_duplicates(
                job.root,
                include_hidden=job.include_hidden,
                cfg=self._cfg,
                progress=job.progress,
                cancel=job.cancel_event,
            )
            job.result = report
            job.status = CANCELLED if report.cancelled else COMPLETE
        except ScanError as exc:
            job.error = {"code": exc.code, "message": exc.message}
            job.status = FAILED
        except Exception:  # never let a worker thread die silently
            logger.exception("Duplicate scan failed")
            job.error = {"code": "internal_error", "message": "Something went wrong while checking for duplicates. No files were changed."}
            job.status = FAILED
        finally:
            job.progress.phase = "done"

    def get(self, job_id: str) -> DuplicateJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> DuplicateJob | None:
        job = self.get(job_id)
        if job is not None:
            job.cancel_event.set()
        return job

    def wait(self, job_id: str, timeout: float = 10.0) -> DuplicateJob | None:
        """Block until a job finishes (used by tests)."""
        end = time.monotonic() + timeout
        job = self.get(job_id)
        while job is not None and job.status == RUNNING and time.monotonic() < end:
            time.sleep(0.01)
        return job
