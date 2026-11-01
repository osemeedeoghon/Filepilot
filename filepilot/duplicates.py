"""Read-only exact-duplicate detection.

Files are grouped by size, then (for large files) by a SHA-256 of their first
bytes, and finally confirmed with a full SHA-256 of their contents. Filenames
play no part in the decision.

Files are only ever opened read-only, and are opened component by component
from the selected folder with O_NOFOLLOW, so a symbolic link is never followed
at any level, even if one is swapped in while the scan runs. Contents are read
in fixed-size chunks and never held in memory as a whole.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import Settings, settings as default_settings
from .organizer import BUNDLE_EXTENSIONS, PRUNED_FOLDER_NAMES, PlanBoundaryError, ensure_within_root
from .scanner import FileEntry, SkippedEntry, scan_folder

# Version-control folders hold many internal copies; never open them.
VCS_FOLDER_NAMES = frozenset({".git", ".hg", ".svn"})

_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
# O_NONBLOCK stops a FIFO swapped in mid-scan from blocking the open.
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


class SkipFile(Exception):
    """This file cannot be checked; the message is the user-facing reason."""


class _Stop(Exception):
    """A limit was reached or the user cancelled."""


def should_prune(name: str) -> bool:
    suffix = name.rsplit(".", 1)[-1].lower() if "." in name[1:] else ""
    return name in VCS_FOLDER_NAMES or name in PRUNED_FOLDER_NAMES or suffix in BUNDLE_EXTENSIONS


@dataclass
class Progress:
    phase: str = "starting"  # starting | listing | hashing | done
    files_total: int = 0
    files_done: int = 0
    bytes_total: int = 0
    bytes_read: int = 0
    groups_found: int = 0
    current: str | None = None
    started: float = field(default_factory=time.monotonic)

    def to_dict(self) -> dict:
        data = asdict(self)
        data.pop("started")
        data["elapsed_seconds"] = round(time.monotonic() - self.started, 1)
        return data


@dataclass
class DuplicateFile:
    relative_path: str
    name: str
    modified: str


@dataclass
class DuplicateGroup:
    sha256: str
    size: int
    count: int
    recoverable_bytes: int
    hard_linked: bool
    files: list[DuplicateFile]


@dataclass
class DuplicateReport:
    root: str
    groups: list[DuplicateGroup] = field(default_factory=list)
    group_count: int = 0
    duplicate_files: int = 0
    recoverable_bytes: int = 0
    files_considered: int = 0
    files_hashed: int = 0
    unique_size_files: int = 0
    empty_files: int = 0
    symlinks_ignored: int = 0
    bytes_read: int = 0
    unchecked_files: int = 0
    skipped: list[SkippedEntry] = field(default_factory=list)
    skipped_count: int = 0
    protected_folders: list[str] = field(default_factory=list)
    scan_truncated: bool = False
    truncated: bool = False
    truncated_reason: str | None = None
    cancelled: bool = False
    duration_ms: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


# --- safe reading -----------------------------------------------------------------

def open_beneath(root: Path, rel: str) -> int:
    """Open `root/rel` read-only without following any symbolic link on the way.

    Each folder is opened relative to its parent with O_NOFOLLOW, so the file
    that is opened is guaranteed to be reached through real folders inside root.
    """
    ensure_within_root(root, rel)
    parts = rel.split("/")
    fd = os.open(root, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC)
    try:
        for part in parts[:-1]:
            nfd = os.open(part, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = nfd
        return os.open(parts[-1], os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC, dir_fd=fd)
    finally:
        os.close(fd)


def hash_file(
    root: Path,
    rel: str,
    expected_size: int,
    *,
    limit: int | None = None,
    chunk_bytes: int = default_settings.dup_chunk_bytes,
    check: Callable[[], None] = lambda: None,
    on_read: Callable[[int], None] = lambda n: None,
) -> tuple[str, tuple[int, int]]:
    """SHA-256 of a file's contents (or its first `limit` bytes), read in chunks.

    Returns (hex digest, (device, inode)). Raises SkipFile with a reason when
    the file cannot be read safely, and propagates whatever `check()` raises.
    """
    try:
        fd = open_beneath(root, rel)
    except PlanBoundaryError:
        raise SkipFile("Outside the selected folder") from None
    except FileNotFoundError:
        raise SkipFile("File disappeared during the scan") from None
    except PermissionError:
        raise SkipFile("Permission denied") from None
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR, getattr(errno, "EMLINK", -1)):
            raise SkipFile("A symbolic link appeared in its path during the scan, so it was not followed") from None
        raise SkipFile(f"Could not open the file ({exc.strerror or 'error'})") from None

    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise SkipFile("No longer a regular file")
        if before.st_size != expected_size:
            raise SkipFile("Changed size during the scan")
        remaining = before.st_size if limit is None else min(limit, before.st_size)
        digest = hashlib.sha256()
        while remaining > 0:
            check()
            try:
                chunk = os.read(fd, min(chunk_bytes, remaining))
            except OSError as exc:
                raise SkipFile(f"Could not read the file ({exc.strerror or 'error'})") from None
            if not chunk:
                raise SkipFile("File became shorter while being read")
            digest.update(chunk)
            remaining -= len(chunk)
            on_read(len(chunk))
        after = os.fstat(fd)
        if after.st_size != before.st_size or after.st_mtime_ns != before.st_mtime_ns:
            raise SkipFile("Changed while being read")
        return digest.hexdigest(), (before.st_dev, before.st_ino)
    finally:
        os.close(fd)


# --- detection --------------------------------------------------------------------

def find_duplicates(
    raw_path: object,
    *,
    include_hidden: bool = False,
    cfg: Settings = default_settings,
    progress: Progress | None = None,
    cancel: threading.Event | None = None,
) -> DuplicateReport:
    """Find exact duplicate files under a user-selected folder. Read-only."""
    progress = progress or Progress()
    cancel = cancel or threading.Event()
    started = time.monotonic()
    deadline = started + cfg.dup_max_seconds

    progress.phase = "listing"
    scan = scan_folder(raw_path, include_hidden=include_hidden, prune=should_prune, cfg=cfg)
    root = Path(scan.root)
    report = DuplicateReport(
        root=scan.root,
        scan_truncated=scan.truncated,
        symlinks_ignored=scan.symlinks,
        protected_folders=list(scan.pruned),
    )

    def skip(rel: str, reason: str) -> None:
        report.skipped_count += 1
        if len(report.skipped) < cfg.max_reported_errors:
            report.skipped.append(SkippedEntry(relative_path=rel, reason=reason))

    for s in scan.skipped:
        skip(s.relative_path, s.reason)

    # Only regular files; folders are never listed and symlinks are counted, not read.
    by_size: dict[int, list[FileEntry]] = {}
    for f in scan.files:
        if f.kind != "file":
            continue
        if f.size == 0:
            report.empty_files += 1
            continue
        report.files_considered += 1
        by_size.setdefault(f.size, []).append(f)

    candidates = {size: files for size, files in by_size.items() if len(files) > 1}
    report.unique_size_files = report.files_considered - sum(len(v) for v in candidates.values())

    progress.phase = "hashing"
    progress.files_total = sum(len(v) for v in candidates.values())
    progress.bytes_total = sum(size * len(v) for size, v in candidates.items())

    def check() -> None:
        if cancel.is_set():
            raise _Stop("Cancelled. Results so far are shown.")
        if time.monotonic() > deadline:
            raise _Stop(f"Stopped after the {cfg.dup_max_seconds:g}-second limit. Results so far are shown.")
        if report.bytes_read > cfg.dup_max_bytes:
            raise _Stop(f"Stopped after reading the {cfg.dup_max_bytes / 1024**3:g} GB limit. Results so far are shown.")

    def on_read(n: int) -> None:
        report.bytes_read += n
        progress.bytes_read = report.bytes_read

    def hash_all(files: list[FileEntry], limit: int | None) -> dict[str, list[tuple[FileEntry, tuple[int, int]]]]:
        out: dict[str, list[tuple[FileEntry, tuple[int, int]]]] = {}
        for f in files:
            progress.current = f.relative_path
            try:
                digest, ident = hash_file(root, f.relative_path, f.size, limit=limit,
                                          chunk_bytes=cfg.dup_chunk_bytes, check=check, on_read=on_read)
            except SkipFile as exc:
                skip(f.relative_path, str(exc))
                progress.files_done += 1
                continue
            out.setdefault(digest, []).append((f, ident))
        return out

    # Largest potential savings first, so a time limit still finds the most space.
    order = sorted(candidates.items(), key=lambda kv: (-kv[0] * (len(kv[1]) - 1), -kv[0]))
    try:
        for size, files in order:
            check()
            groups = [files]
            if size > cfg.dup_prefix_threshold:
                # Cheap first pass: files whose first bytes differ cannot be identical.
                prefixed = hash_all(files, cfg.dup_prefix_bytes)
                groups = []
                for members in prefixed.values():
                    if len(members) > 1:
                        groups.append([f for f, _ in members])
                    else:
                        progress.files_done += 1
                        progress.bytes_total -= size
            for members in groups:
                confirmed = hash_all(members, None)
                for digest, hits in confirmed.items():
                    report.files_hashed += len(hits)
                    progress.files_done += len(hits)
                    if len(hits) < 2:
                        continue
                    hits.sort(key=lambda h: h[0].relative_path)
                    unique = len({ident for _, ident in hits})
                    report.groups.append(
                        DuplicateGroup(
                            sha256=digest,
                            size=size,
                            count=len(hits),
                            recoverable_bytes=size * (unique - 1),
                            hard_linked=unique < len(hits),
                            files=[DuplicateFile(f.relative_path, f.name, f.modified) for f, _ in hits],
                        )
                    )
                    progress.groups_found = len(report.groups)
    except _Stop as stop:
        report.truncated = True
        report.truncated_reason = str(stop)
        report.cancelled = cancel.is_set()
        report.unchecked_files = max(0, progress.files_total - progress.files_done)

    report.groups.sort(key=lambda g: (-g.recoverable_bytes, -g.size, g.files[0].relative_path))
    report.group_count = len(report.groups)
    report.duplicate_files = sum(g.count - 1 for g in report.groups)
    report.recoverable_bytes = sum(g.recoverable_bytes for g in report.groups)
    report.duration_ms = int((time.monotonic() - started) * 1000)
    progress.current = None
    progress.phase = "done"
    return report
