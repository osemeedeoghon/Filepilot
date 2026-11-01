"""Read-only organization planner.

Turns scan metadata into a *proposed* plan: for each file, a suggested category,
a destination path relative to the selected folder, a status and the reasons
behind it. Nothing here touches the filesystem except through the read-only
scanner; the plan is never applied.

Rules are deterministic and conservative. A file is only proposed as a plain
"move" when no rule finds a reason for doubt; everything else is "review",
"conflict" or "keep" (left in place).
"""

from __future__ import annotations

import os
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

from .config import Settings, settings as default_settings
from .scanner import CATEGORY_BY_EXTENSION, FileEntry, ScanResult, categorize, scan_folder

CATEGORIES = (
    "Documents",
    "Images",
    "Videos",
    "Audio",
    "Spreadsheets",
    "Presentations",
    "Archives",
    "Code",
    "Other",
)

SCOPES = ("top_level", "all")

# Scanner category -> destination folder. Executables have no destination.
SCANNER_TO_PLAN = {
    "Document": "Documents",
    "Image": "Images",
    "Video": "Videos",
    "Audio": "Audio",
    "Spreadsheet": "Spreadsheets",
    "Presentation": "Presentations",
    "Archive": "Archives",
    "Code": "Code",
    "Font": "Other",
    "Other": "Other",
}

MOVE, CONFLICT, REVIEW, KEEP = "move", "conflict", "review", "keep"
STATUSES = (MOVE, CONFLICT, REVIEW, KEEP)

# Folders that are macOS packages: they look like folders but act as one file.
BUNDLE_EXTENSIONS = frozenset(
    {
        "app", "bundle", "framework", "plugin", "kext", "appex", "xpc", "prefpane",
        "xcodeproj", "xcworkspace", "playground", "xcassets", "photoslibrary",
        "photolibrary", "aplibrary", "musiclibrary", "imovielibrary", "tvlibrary",
        "fcpbundle", "logicx", "band", "rtfd", "pages", "numbers", "key",
        "sparsebundle", "pkg", "mpkg", "scptd", "mlmodelc", "docarchive", "lproj",
    }
)

# Folders whose contents FilePilot never lists or proposes to move.
PRUNED_FOLDER_NAMES = frozenset({"node_modules", "venv", "__pycache__", "site-packages", "Pods", "DerivedData"})

# Names whose presence marks a folder as a software project.
PROJECT_MARKERS = frozenset(
    {
        ".git", ".hg", ".svn", "package.json", "pyproject.toml", "setup.py", "setup.cfg",
        "Cargo.toml", "go.mod", "Gemfile", "pom.xml", "build.gradle", "build.gradle.kts",
        "CMakeLists.txt", "Makefile", "Package.swift", "composer.json", "node_modules", ".venv",
    }
)
PROJECT_MARKER_SUFFIXES = (".xcodeproj", ".xcworkspace")

TEMP_EXTENSIONS = frozenset(
    {"crdownload", "part", "partial", "download", "opdownload", "tmp", "temp", "swp", "swo", "aria2", "lock"}
)
SENSITIVE_EXTENSIONS = frozenset({"pem", "p12", "pfx", "keychain", "keychain-db", "kdbx", "ovpn", "gpg", "asc", "cer", "crt"})
SENSITIVE_PREFIXES = ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")
SENSITIVE_NAMES = frozenset({"credentials", "credentials.json", "secrets.json", "secrets.yaml", "secrets.yml"})

AMBIGUOUS_EXTENSIONS = {
    "ts": "“.ts” can be TypeScript code or an MPEG video stream.",
    "key": "“.key” can be a Keynote presentation or a cryptographic key.",
    "svg": "SVG files are images, but are also used as assets inside apps and websites.",
    "html": "HTML files can be saved web pages or website source code.",
    "htm": "HTML files can be saved web pages or website source code.",
    "xml": "XML files are often configuration or data used by another app.",
    "json": "JSON files are often configuration or data used by another app.",
    "raw": "“.raw” can be a camera photo or raw data from another app.",
}
COMPOUND_EXTENSIONS = frozenset({("tar", "gz"), ("tar", "bz2"), ("tar", "xz"), ("tar", "zst")})

RECENT_WINDOW = timedelta(minutes=10)


class PlanBoundaryError(ValueError):
    """A path would fall outside the selected folder."""


@dataclass
class PlanItem:
    relative_path: str
    name: str
    size: int
    modified: str
    suggested_category: str | None
    destination: str | None
    status: str
    reason: str
    notes: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


@dataclass
class ProtectedFolder:
    relative_path: str
    reason: str


@dataclass
class OrganizationPlan:
    root: str
    scope: str
    selected_categories: list[str]
    items: list[PlanItem] = field(default_factory=list)
    protected_folders: list[ProtectedFolder] = field(default_factory=list)
    status_counts: dict[str, int] = field(default_factory=dict)
    category_counts: dict[str, dict[str, int]] = field(default_factory=dict)
    size_to_move: int = 0
    hidden_skipped: int = 0
    warnings: list[str] = field(default_factory=list)
    complete: bool = True
    scan_truncated: bool = False
    scan_skipped_count: int = 0
    duration_ms: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


# --- path helpers --------------------------------------------------------------

def path_key(rel: str) -> str:
    """Comparison key matching APFS defaults (case-insensitive, Unicode-normalised)."""
    return unicodedata.normalize("NFC", rel).casefold()


def ensure_within_root(root: Path, rel: str) -> str:
    """Validate a relative path and return it; raise if it could escape `root`."""
    if not rel or "\x00" in rel or rel.startswith("/") or "\\" in rel:
        raise PlanBoundaryError(f"Invalid relative path: {rel!r}")
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise PlanBoundaryError(f"Relative path escapes the selected folder: {rel!r}")
    joined = os.path.normpath(os.path.join(root, rel))
    if os.path.commonpath([str(root), joined]) != str(root) or joined == str(root):
        raise PlanBoundaryError(f"Relative path escapes the selected folder: {rel!r}")
    return rel


def _prefixes(rel: str) -> list[str]:
    """'a/b/c.txt' -> ['a', 'a/b'] (ancestor folders, nearest last)."""
    parts = rel.split("/")[:-1]
    return ["/".join(parts[: i + 1]) for i in range(len(parts))]


def _extension(name: str) -> str:
    suffix = PurePosixPath(name).suffix
    return suffix[1:].lower() if suffix else ""


def _alternative_name(name: str, n: int) -> str:
    """'photo.jpg' -> 'photo (2).jpg'; 'backup.tar.gz' -> 'backup (2).tar.gz'."""
    suffixes = PurePosixPath(name).suffixes
    if len(suffixes) >= 2 and (suffixes[-2][1:].lower(), suffixes[-1][1:].lower()) in COMPOUND_EXTENSIONS:
        ext = "".join(suffixes[-2:])
    elif name.startswith(".") or not suffixes:
        ext = ""
    else:
        ext = suffixes[-1]
    stem = name[: len(name) - len(ext)] if ext else name
    return f"{stem} ({n}){ext}"


def should_prune(name: str) -> bool:
    """Folders the organizer records but does not open."""
    return name.startswith(".") or name in PRUNED_FOLDER_NAMES or _extension(name) in BUNDLE_EXTENSIONS


def _is_project_marker(name: str) -> bool:
    return name in PROJECT_MARKERS or name.endswith(PROJECT_MARKER_SUFFIXES)


# --- rules -----------------------------------------------------------------------

def _keep_reason(entry: FileEntry, parts: list[str], project_dirs: dict[str, str]) -> tuple[str, str] | None:
    """Rules that leave a file in place. Returns (flag, reason) or None."""
    name = entry.name
    if entry.kind == "symlink":
        return "symlink", "Symbolic link. FilePilot never moves or follows links."
    if any(p.startswith(".") for p in parts):
        return "hidden", "Hidden item. These are usually app or system settings, so it stays where it is."
    for p in parts[:-1]:
        if _extension(p) in BUNDLE_EXTENSIONS:
            return "inside_package", f"Inside the macOS package “{p}”. Moving parts of a package can break it."
    for ancestor in [""] + _prefixes(entry.relative_path):
        if ancestor in project_dirs:
            where = "this folder" if ancestor == "" else f"“{ancestor}”"
            return "inside_project", f"Part of a software project in {where} ({project_dirs[ancestor]} found). Moving files could break it."
    ext = _extension(name)
    lower = name.lower()
    if lower.startswith("~$") or name.endswith("~") or ext in TEMP_EXTENSIONS:
        return "temporary", "Looks like a temporary file or unfinished download."
    if ext in SENSITIVE_EXTENSIONS or lower.startswith(SENSITIVE_PREFIXES) or lower in SENSITIVE_NAMES:
        return "sensitive", "May contain keys or credentials, which other apps expect to find in a fixed place."
    if entry.category == "Executable":
        return "executable", "Apps and executables are left in place."
    return None


def _review_notes(entry: FileEntry, category: str, now: datetime) -> list[tuple[str, str]]:
    """Rules that make a proposed move need human review."""
    notes: list[tuple[str, str]] = []
    name = entry.name
    ext = _extension(name)
    if not ext:
        notes.append(("no_extension", "Has no file extension, so its type is unknown."))
    elif category == "Other":
        notes.append(("unknown_type", f"“.{ext}” is not a recognised file type."))
    if ext in AMBIGUOUS_EXTENSIONS:
        notes.append(("ambiguous_type", AMBIGUOUS_EXTENSIONS[ext]))

    suffixes = [s[1:].lower() for s in PurePosixPath(name).suffixes]
    if len(suffixes) >= 2:
        inner = suffixes[-2]
        inner_cat = CATEGORY_BY_EXTENSION.get(inner)
        if (
            inner_cat is not None
            and (inner, suffixes[-1]) not in COMPOUND_EXTENSIONS
            and inner_cat != CATEGORY_BY_EXTENSION.get(suffixes[-1])
        ):
            notes.append(("double_extension", f"Has two extensions (.{inner}.{suffixes[-1]}). The real type may not match its name."))

    if entry.executable:
        notes.append(("executable_bit", "Marked as executable, which is unusual for a file of this type."))
    if entry.size == 0:
        notes.append(("empty_file", "The file is empty (0 bytes)."))
    try:
        modified = datetime.fromisoformat(entry.modified)
    except ValueError:
        modified = None
    if modified is not None and now - modified < RECENT_WINDOW:
        notes.append(("recently_modified", "Changed in the last 10 minutes and may still be in use."))
    return notes


# --- planner ---------------------------------------------------------------------

def build_plan(
    scan: ScanResult,
    *,
    categories: list[str] | tuple[str, ...] = CATEGORIES,
    scope: str = "top_level",
    include_hidden: bool = False,
    max_depth: int = default_settings.max_depth,
    now: datetime | None = None,
) -> OrganizationPlan:
    """Build a proposed organization plan from scan metadata. Pure function."""
    if scope not in SCOPES:
        raise ValueError(f"Unknown scope: {scope!r}")
    unknown = [c for c in categories if c not in CATEGORIES]
    if unknown:
        raise ValueError(f"Unknown categories: {unknown}")
    selected = [c for c in CATEGORIES if c in set(categories)]
    now = now or datetime.now(timezone.utc)
    root = Path(scan.root)

    plan = OrganizationPlan(
        root=scan.root,
        scope=scope,
        selected_categories=selected,
        scan_truncated=scan.truncated,
        scan_skipped_count=scan.skipped_count,
    )

    # Everything that already exists, keyed case-insensitively.
    existing: dict[str, tuple[str, str, int | None]] = {}  # key -> (kind, actual path, size)
    for folder in scan.folders:
        existing[path_key(folder)] = ("folder", folder, None)
    for f in scan.files:
        existing[path_key(f.relative_path)] = (f.kind, f.relative_path, f.size)

    # Folders whose contents FilePilot could not fully list.
    unlisted = {path_key(p) for p in scan.pruned}
    unlisted |= {path_key(s.relative_path) for s in scan.skipped}
    unlisted |= {path_key(f) for f in scan.folders if len(f.split("/")) > max_depth}

    # Software projects: folders containing a marker file or folder.
    project_dirs: dict[str, str] = {}
    for rel in [*scan.folders, *(f.relative_path for f in scan.files)]:
        name = rel.rsplit("/", 1)[-1]
        if _is_project_marker(name):
            parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
            project_dirs.setdefault(parent, name)

    for rel in scan.pruned:
        name = rel.rsplit("/", 1)[-1]
        if name.startswith("."):
            if not include_hidden:
                continue
            reason = "Hidden folder. Not opened."
        elif _extension(name) in BUNDLE_EXTENSIONS:
            reason = "macOS package (acts as a single file). Not opened or split up."
        else:
            reason = "App or project support folder. Not opened."
        plan.protected_folders.append(ProtectedFolder(rel, reason))
    for rel, marker in sorted(project_dirs.items()):
        plan.protected_folders.append(
            ProtectedFolder(rel or ".", f"Software project ({marker} found). Its files are left in place.")
        )
    if "" in project_dirs:
        plan.warnings.append(
            f"This folder looks like a software project ({project_dirs['']} found), so FilePilot proposes no moves inside it."
        )

    category_folder_keys = {path_key(c): c for c in CATEGORIES}
    candidates: list[PlanItem] = []

    for entry in sorted(scan.files, key=lambda e: e.relative_path):
        rel = entry.relative_path
        parts = rel.split("/")

        def item(status: str, reason: str, category: str | None = None, dest: str | None = None) -> PlanItem:
            return PlanItem(
                relative_path=rel,
                name=entry.name,
                size=entry.size,
                modified=entry.modified,
                suggested_category=category,
                destination=dest,
                status=status,
                reason=reason,
            )

        try:
            ensure_within_root(root, rel)
        except PlanBoundaryError:
            bad = item(KEEP, "Path is outside the selected folder, so it is ignored.")
            bad.flags.append("outside_root")
            plan.items.append(bad)
            continue

        is_hidden = any(p.startswith(".") for p in parts)
        if is_hidden and not include_hidden:
            plan.hidden_skipped += 1
            continue

        category = SCANNER_TO_PLAN.get(entry.category)
        keep = _keep_reason(entry, parts, project_dirs)
        if keep is not None:
            flag, reason = keep
            it = item(KEEP, reason, category if flag not in ("symlink", "executable") else None)
            it.flags.append(flag)
            plan.items.append(it)
            continue
        assert category is not None

        if category not in selected:
            it = item(KEEP, f"Looks like {category.lower()}, but that category is not selected.", category)
            it.flags.append("category_not_selected")
            plan.items.append(it)
            continue

        # Already filed inside a top-level category folder?
        top = category_folder_keys.get(path_key(parts[0])) if len(parts) > 1 else None
        if top == category:
            it = item(KEEP, f"Already in the {parts[0]} folder.", category)
            it.flags.append("already_organized")
            plan.items.append(it)
            continue

        if scope == "top_level" and len(parts) > 1 and top is None:
            it = item(KEEP, "In a subfolder. Turn on “Include subfolders” to include it.", category)
            it.flags.append("in_subfolder")
            plan.items.append(it)
            continue

        # Destination: <Category>/<name>, or <Category>/<subfolders>/<name> with subfolders.
        # Reuse an existing folder's spelling (e.g. "images") on case-insensitive disks.
        info = existing.get(path_key(category))
        cat_folder = info[1] if info is not None and info[0] == "folder" else category
        if top is not None:
            tail = parts[1:]  # leave the mismatched category folder behind
        elif scope == "all":
            tail = parts
        else:
            tail = [entry.name]
        dest = "/".join([cat_folder, *tail])

        notes = _review_notes(entry, category, now)
        if top is not None:
            notes.append(("in_other_category_folder", f"Currently filed in “{parts[0]}”, but looks like {category.lower()}."))
        it = item(REVIEW if notes else MOVE, "", category, dest)
        for flag, text in notes:
            it.flags.append(flag)
            it.notes.append(text)
        ext = _extension(entry.name)
        it.reason = f"“.{ext}” files are {category.lower()}." if ext and category != "Other" else "Type could not be determined."
        if len(parts) > 1 and scope == "all" and top is None:
            it.notes.append(f"Keeps its subfolder path “{'/'.join(parts[:-1])}” inside {cat_folder}.")
        if scan.truncated:
            it.status = REVIEW
            it.flags.append("incomplete_scan")
            it.notes.append("The scan stopped early, so conflicts could not be fully checked.")
        candidates.append(it)

    _resolve_destinations(root, candidates, existing, unlisted)
    plan.items.extend(candidates)
    plan.items.sort(key=lambda i: i.relative_path)

    if scan.truncated:
        plan.complete = False
        plan.warnings.append("The scan stopped early (item or time limit). Every proposed move is marked for review.")
    if scan.depth_limited:
        plan.warnings.append("Some deeply nested folders were not opened, so their files are not in this plan.")
    if scan.skipped_count:
        plan.warnings.append(f"{scan.skipped_count} item(s) could not be read and are not in this plan.")

    _summarise(plan)
    return plan


def _resolve_destinations(
    root: Path,
    candidates: list[PlanItem],
    existing: dict[str, tuple[str, str, int | None]],
    unlisted: set[str],
) -> None:
    """Flag blocked, unverifiable and clashing destinations. Never reuses a taken name."""
    wanted: dict[str, list[PlanItem]] = {}
    for it in candidates:
        wanted.setdefault(path_key(it.destination), []).append(it)
    claimed: set[str] = set()

    for it in candidates:
        dest = it.destination
        ensure_within_root(root, dest)

        # Every folder on the way must be a real folder (or not exist yet) that we could read.
        blocked = False
        for prefix in _prefixes(dest):
            key = path_key(prefix)
            info = existing.get(key)
            if info is not None and info[0] != "folder":
                kind = "symbolic link" if info[0] == "symlink" else "file"
                it.flags.append("blocked_destination")
                it.notes.append(f"“{info[1]}” already exists as a {kind}, so it can't be used as a folder.")
                it.status = REVIEW
                blocked = True
                break
            if key in unlisted:
                it.flags.append("unverified_destination")
                it.notes.append(f"Couldn't check what is already inside “{prefix}”.")
                it.status = REVIEW
                blocked = True
                break
        if blocked:
            claimed.add(path_key(dest))
            continue

        key = path_key(dest)
        group = wanted[key]
        clash_existing = existing.get(key)
        clash_plan = len(group) > 1

        if clash_existing is not None:
            kind, actual, size = clash_existing
            it.flags.append("destination_exists")
            if kind == "file" and size == it.size:
                it.flags.append("possible_duplicate")
                it.notes.append(f"“{actual}” already exists with the same size. It may be a duplicate copy.")
            else:
                it.notes.append(f"“{actual}” already exists and will not be overwritten.")
        if clash_plan:
            it.flags.append("duplicate_name")
            others = [o.relative_path for o in group if o is not it]
            same_size = [o for o in group if o is not it and o.size == it.size]
            it.notes.append(f"{len(group)} files would be named “{it.name}” here (also: {', '.join(others[:3])}{'…' if len(others) > 3 else ''}).")
            if same_size:
                it.flags.append("possible_duplicate")

        if clash_existing is not None or key in claimed:
            # Pick the first free alternative that no other file wants either.
            parent, _, name = dest.rpartition("/")
            n = 2
            while True:
                alt = f"{parent}/{_alternative_name(name, n)}"
                alt_key = path_key(alt)
                if alt_key not in existing and alt_key not in claimed and alt_key not in wanted:
                    break
                n += 1
            ensure_within_root(root, alt)
            it.destination = alt
            it.notes.append(f"Renamed to “{alt.rsplit('/', 1)[-1]}” in the proposal to avoid overwriting.")
            key = alt_key
        if clash_existing is not None or clash_plan:
            it.status = CONFLICT
        claimed.add(key)


def _summarise(plan: OrganizationPlan) -> None:
    plan.status_counts = {s: 0 for s in STATUSES}
    plan.category_counts = {c: {"files": 0, **{s: 0 for s in STATUSES}} for c in CATEGORIES}
    for it in plan.items:
        plan.status_counts[it.status] += 1
        if it.suggested_category:
            counts = plan.category_counts[it.suggested_category]
            counts["files"] += 1
            counts[it.status] += 1
        if it.status in (MOVE, CONFLICT, REVIEW):
            plan.size_to_move += it.size


def preview_organization(
    raw_path: object,
    *,
    categories: list[str] | tuple[str, ...] = CATEGORIES,
    scope: str = "top_level",
    include_hidden: bool = False,
    cfg: Settings = default_settings,
    now: datetime | None = None,
) -> OrganizationPlan:
    """Scan the folder (read-only) and return a proposed organization plan."""
    started = datetime.now(timezone.utc)
    # Hidden items are always scanned so they count as existing names for conflict checks.
    scan = scan_folder(raw_path, include_hidden=True, collect_folders=True, prune=should_prune, cfg=cfg)
    plan = build_plan(
        scan,
        categories=categories,
        scope=scope,
        include_hidden=include_hidden,
        max_depth=cfg.max_depth,
        now=now,
    )
    plan.duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    return plan
