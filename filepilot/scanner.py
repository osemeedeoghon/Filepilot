"""Read-only, bounded folder scanner.

The scanner never opens, modifies, moves or deletes files. It only reads
directory listings and `lstat` metadata. Symbolic links are reported but never
followed, and every entry is checked to lie inside the folder the user chose.
"""

from __future__ import annotations

import os
import stat
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings, settings as default_settings

# Top-level locations FilePilot refuses to scan as a whole. Their subfolders
# (for example a project inside /Users/<name>) are still allowed.
BLOCKED_ROOTS = frozenset(
    {
        "/",
        "/System",
        "/Library",
        "/private",
        "/private/var",
        "/private/etc",
        "/usr",
        "/bin",
        "/sbin",
        "/etc",
        "/var",
        "/dev",
        "/cores",
        "/opt",
        "/Volumes",
        "/Users",
        "/Applications",
        "/Network",
    }
)

CATEGORY_BY_EXTENSION = {
    **dict.fromkeys(
        ["jpg", "jpeg", "png", "gif", "bmp", "tif", "tiff", "webp", "heic", "heif", "svg", "ico", "raw"],
        "Image",
    ),
    **dict.fromkeys(
        ["pdf", "doc", "docx", "rtf", "txt", "md", "pages", "odt", "tex", "epub"],
        "Document",
    ),
    **dict.fromkeys(["xls", "xlsx", "csv", "tsv", "numbers", "ods"], "Spreadsheet"),
    **dict.fromkeys(["ppt", "pptx", "key", "odp"], "Presentation"),
    **dict.fromkeys(["mp3", "wav", "aac", "flac", "m4a", "ogg", "aiff", "aif"], "Audio"),
    **dict.fromkeys(["mp4", "mov", "avi", "mkv", "m4v", "webm", "wmv"], "Video"),
    **dict.fromkeys(["zip", "tar", "gz", "tgz", "bz2", "xz", "7z", "rar", "dmg", "iso", "pkg"], "Archive"),
    **dict.fromkeys(
        [
            "py", "js", "ts", "tsx", "jsx", "html", "htm", "css", "scss", "json", "yaml", "yml",
            "toml", "xml", "sh", "zsh", "bash", "c", "h", "cpp", "hpp", "cc", "java", "kt",
            "swift", "m", "go", "rs", "rb", "php", "sql", "ipynb", "r", "lua", "cs",
        ],
        "Code",
    ),
    **dict.fromkeys(["ttf", "otf", "woff", "woff2"], "Font"),
    **dict.fromkeys(["app", "exe", "bin", "command"], "Executable"),
}


class ScanError(Exception):
    """A user-facing error for a path that cannot be scanned."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass
class FileEntry:
    name: str
    relative_path: str
    kind: str  # "file" or "symlink"
    category: str
    extension: str
    size: int
    modified: str
    # Internal metadata for the organizer; not part of the public scan response.
    executable: bool = False


@dataclass
class SkippedEntry:
    relative_path: str
    reason: str


@dataclass
class ScanResult:
    root: str
    files: list[FileEntry] = field(default_factory=list)
    total_files: int = 0
    total_folders: int = 0
    total_size: int = 0
    symlinks: int = 0
    skipped: list[SkippedEntry] = field(default_factory=list)
    skipped_count: int = 0
    truncated: bool = False
    truncated_reason: str | None = None
    depth_limited: bool = False
    duration_ms: int = 0
    # Populated only on request (used by the organizer, not returned by /api/scan).
    folders: list[str] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        data = asdict(self)
        data.pop("folders")
        data.pop("pruned")
        for f in data["files"]:
            f.pop("executable")
        return data


def categorize(name: str) -> tuple[str, str]:
    """Return (category, extension) for a filename, based only on its name."""
    suffix = Path(name).suffix
    ext = suffix[1:].lower() if suffix else ""
    if not ext:
        return "Other", ""
    return CATEGORY_BY_EXTENSION.get(ext, "Other"), ext


def validate_root(raw_path: object, cfg: Settings = default_settings) -> Path:
    """Validate a user-supplied folder path and return its canonical form.

    Rejects anything that is not an absolute path to an existing, readable,
    non-symlink directory outside the blocked system locations.
    """
    if not isinstance(raw_path, str):
        raise ScanError("invalid_path", "The folder path must be text.")
    path_str = raw_path.strip()
    if not path_str:
        raise ScanError("invalid_path", "Enter or choose a folder to scan.")
    if len(path_str) > cfg.max_path_length:
        raise ScanError("invalid_path", f"The folder path is longer than {cfg.max_path_length} characters.")
    if "\x00" in path_str or any(ord(c) < 32 for c in path_str):
        raise ScanError("invalid_path", "The folder path contains control characters.")

    if path_str == "~" or path_str.startswith("~/"):
        path_str = os.path.expanduser(path_str)

    candidate = Path(path_str)
    if not candidate.is_absolute():
        raise ScanError("invalid_path", "Use a full folder path that starts with / (for example /Users/you/Documents).")
    if ".." in candidate.parts:
        raise ScanError("path_traversal", "Folder paths may not contain '..' segments.")

    try:
        st = os.lstat(candidate)
    except FileNotFoundError:
        raise ScanError("not_found", "That folder does not exist.", 404) from None
    except PermissionError:
        raise ScanError("permission_denied", "FilePilot does not have permission to access that folder.", 403) from None
    except OSError as exc:
        raise ScanError("os_error", f"The folder could not be read ({exc.strerror or 'unknown error'}).") from None

    # Ancestors may be symlinks (macOS: /tmp -> /private/tmp); canonicalise them.
    resolved = Path(os.path.realpath(candidate))
    if str(resolved) in BLOCKED_ROOTS or str(candidate) in BLOCKED_ROOTS:
        raise ScanError(
            "blocked_location",
            "FilePilot does not scan system or top-level locations. Choose a more specific folder.",
            403,
        )
    if stat.S_ISLNK(st.st_mode):
        raise ScanError("symlink", "That path is a symbolic link. Choose the real folder instead.")
    if not stat.S_ISDIR(st.st_mode):
        raise ScanError("not_a_directory", "That path is a file, not a folder.")
    if not os.access(resolved, os.R_OK | os.X_OK):
        raise ScanError("permission_denied", "FilePilot does not have permission to read that folder.", 403)
    return resolved


def is_within(root: Path, candidate: Path) -> bool:
    """True when `candidate` is `root` or lies beneath it (no symlink resolution)."""
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def scan_folder(
    raw_path: object,
    *,
    include_hidden: bool = False,
    collect_folders: bool = False,
    prune: Callable[[str], bool] | None = None,
    cfg: Settings = default_settings,
) -> ScanResult:
    """Scan a user-selected folder and its subfolders, read-only and bounded.

    `collect_folders` records every folder's relative path in `result.folders`.
    `prune(name)` returning True records a folder but does not descend into it
    (its relative path goes in `result.pruned`).
    """
    root = validate_root(raw_path, cfg)
    started = time.monotonic()
    deadline = started + cfg.max_scan_seconds
    result = ScanResult(root=str(root))
    entries_seen = 0

    def skip(path: Path, reason: str) -> None:
        result.skipped_count += 1
        if len(result.skipped) < cfg.max_reported_errors:
            rel = path.relative_to(root).as_posix() if is_within(root, path) else path.name
            result.skipped.append(SkippedEntry(relative_path=rel or ".", reason=reason))

    def stop(reason: str) -> None:
        result.truncated = True
        result.truncated_reason = reason

    # Iterative depth-first walk so deep trees cannot exhaust the call stack.
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack and not result.truncated:
        directory, depth = stack.pop()
        try:
            with os.scandir(directory) as it:
                children = sorted(it, key=lambda e: e.name)
        except PermissionError:
            skip(directory, "Permission denied")
            continue
        except FileNotFoundError:
            skip(directory, "Folder disappeared during scan")
            continue
        except OSError as exc:
            skip(directory, f"Could not read folder ({exc.strerror or 'error'})")
            continue

        subdirs: list[Path] = []
        for entry in children:
            if time.monotonic() > deadline:
                stop(f"Scan stopped after {cfg.max_scan_seconds:g} seconds.")
                break
            if entries_seen >= cfg.max_entries:
                stop(f"Scan stopped at the {cfg.max_entries:,}-item limit.")
                break
            if not include_hidden and entry.name.startswith("."):
                continue

            path = Path(entry.path)
            # Defence in depth: names come from the OS, but never trust them blindly.
            if "/" in entry.name or entry.name in (".", "..") or not is_within(root, path):
                skip(path, "Outside the selected folder")
                continue
            entries_seen += 1

            try:
                st = entry.stat(follow_symlinks=False)
            except PermissionError:
                skip(path, "Permission denied")
                continue
            except FileNotFoundError:
                skip(path, "Item disappeared during scan")
                continue
            except OSError as exc:
                skip(path, f"Could not read item ({exc.strerror or 'error'})")
                continue

            rel = path.relative_to(root).as_posix()
            if stat.S_ISLNK(st.st_mode):
                result.symlinks += 1
                result.files.append(
                    FileEntry(
                        name=entry.name,
                        relative_path=rel,
                        kind="symlink",
                        category="Symbolic link",
                        extension="",
                        size=st.st_size,
                        modified=_iso(st.st_mtime),
                    )
                )
            elif stat.S_ISDIR(st.st_mode):
                result.total_folders += 1
                if collect_folders:
                    result.folders.append(rel)
                if prune is not None and prune(entry.name):
                    result.pruned.append(rel)
                elif depth + 1 > cfg.max_depth:
                    result.depth_limited = True
                else:
                    subdirs.append(path)
            elif stat.S_ISREG(st.st_mode):
                category, ext = categorize(entry.name)
                result.total_files += 1
                result.total_size += st.st_size
                result.files.append(
                    FileEntry(
                        name=entry.name,
                        relative_path=rel,
                        kind="file",
                        category=category,
                        extension=ext,
                        size=st.st_size,
                        modified=_iso(st.st_mtime),
                        executable=bool(st.st_mode & 0o111),
                    )
                )
            # Sockets, FIFOs and device nodes are ignored.

        # Push in reverse so folders are visited alphabetically.
        stack.extend((d, depth + 1) for d in reversed(subdirs))

    result.duration_ms = int((time.monotonic() - started) * 1000)
    return result
