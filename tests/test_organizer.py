import os
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from filepilot.config import Settings
from filepilot.organizer import (
    CATEGORIES,
    PlanBoundaryError,
    build_plan,
    ensure_within_root,
    path_key,
    preview_organization,
)
from filepilot.scanner import FileEntry, ScanError, ScanResult, categorize

# Fixed "now" well after the fixture files' mtimes, so nothing counts as recently modified.
LATER = datetime.now(timezone.utc) + timedelta(days=1)
OLD = "2024-01-01T00:00:00+00:00"

needs_non_root = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")


def items(plan):
    return {i.relative_path: i for i in plan.items}


def preview(path, **kw):
    kw.setdefault("now", LATER)
    return preview_organization(str(path), **kw)


def write(root: Path, rel: str, data: bytes = b"x") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def snapshot(root: Path):
    out = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            st = p.lstat()
            out[str(p.relative_to(root))] = (st.st_mode, st.st_size, st.st_mtime_ns)
    return out


def assert_destinations_safe(plan):
    """No overwrite, no two files sharing a destination, nothing outside the root."""
    root = Path(plan.root)
    existing = {path_key(str(p.relative_to(root))) for p in root.rglob("*")} if root.exists() else set()
    seen = set()
    for it in plan.items:
        if it.destination is None:
            continue
        key = path_key(it.destination)
        assert key not in seen, f"two files share {it.destination}"
        seen.add(key)
        assert key not in existing, f"{it.destination} would overwrite an existing item"
        resolved = os.path.normpath(os.path.join(root, it.destination))
        assert os.path.commonpath([str(root), resolved]) == str(root)
        assert ".." not in it.destination.split("/")
        assert not it.destination.startswith("/")


def fake_scan(root: str, entries: list[tuple[str, int]], folders=(), **kw) -> ScanResult:
    files = []
    for rel, size in entries:
        name = rel.rsplit("/", 1)[-1]
        cat, ext = categorize(name)
        files.append(FileEntry(name=name, relative_path=rel, kind="file", category=cat, extension=ext, size=size, modified=OLD))
    return ScanResult(root=root, files=files, folders=list(folders), **kw)


@pytest.fixture
def messy(tmp_path: Path) -> Path:
    root = tmp_path / "Downloads"
    root.mkdir()
    for rel in [
        "report.pdf", "notes.txt", "photo.jpg", "clip.mp4", "song.mp3", "budget.xlsx",
        "slides.pptx", "backup.zip", "script.py", "font.ttf", "mystery.xyz", "README",
        "trip/beach.png", "trip/itinerary.docx",
    ]:
        write(root, rel, b"data")
    return root


# --- categorization --------------------------------------------------------------

def test_categories_for_known_extensions(messy):
    plan = preview(messy)
    got = {rel: it.suggested_category for rel, it in items(plan).items()}
    assert got["report.pdf"] == "Documents"
    assert got["notes.txt"] == "Documents"
    assert got["photo.jpg"] == "Images"
    assert got["clip.mp4"] == "Videos"
    assert got["song.mp3"] == "Audio"
    assert got["budget.xlsx"] == "Spreadsheets"
    assert got["slides.pptx"] == "Presentations"
    assert got["backup.zip"] == "Archives"
    assert got["script.py"] == "Code"
    assert got["font.ttf"] == "Other"
    assert got["mystery.xyz"] == "Other"


def test_clear_cases_are_plain_moves_with_reasons(messy):
    it = items(preview(messy))["photo.jpg"]
    assert it.status == "move"
    assert it.destination == "Images/photo.jpg"
    assert "“.jpg” files are images" in it.reason
    assert it.notes == []


def test_category_counts_and_status_counts(messy):
    plan = preview(messy, scope="all")
    assert plan.category_counts["Images"]["files"] == 2
    assert plan.category_counts["Documents"]["files"] == 3
    assert sum(plan.status_counts.values()) == len(plan.items)
    assert set(plan.category_counts) == set(CATEGORIES)
    assert plan.size_to_move == sum(i.size for i in plan.items if i.status != "keep")


def test_unselected_categories_are_left_in_place(messy):
    plan = preview(messy, categories=["Images"])
    assert items(plan)["photo.jpg"].status == "move"
    pdf = items(plan)["report.pdf"]
    assert pdf.status == "keep"
    assert pdf.destination is None
    assert pdf.suggested_category == "Documents"
    assert "category_not_selected" in pdf.flags


def test_no_categories_selected_means_no_moves(messy):
    plan = preview(messy, categories=[])
    assert all(i.status == "keep" for i in plan.items)


def test_invalid_category_and_scope_rejected(messy):
    scan = fake_scan(str(messy), [])
    with pytest.raises(ValueError):
        build_plan(scan, categories=["Secrets"])
    with pytest.raises(ValueError):
        build_plan(scan, scope="everything")


# --- proposed paths --------------------------------------------------------------

def test_top_level_scope_leaves_subfolders_alone(messy):
    plan = preview(messy)
    beach = items(plan)["trip/beach.png"]
    assert beach.status == "keep"
    assert "in_subfolder" in beach.flags
    assert beach.suggested_category == "Images"


def test_all_scope_keeps_subfolder_paths(messy):
    plan = preview(messy, scope="all")
    assert items(plan)["trip/beach.png"].destination == "Images/trip/beach.png"
    assert items(plan)["trip/itinerary.docx"].destination == "Documents/trip/itinerary.docx"
    assert items(plan)["report.pdf"].destination == "Documents/report.pdf"
    assert_destinations_safe(plan)


def test_already_organized_files_stay(tmp_path):
    root = tmp_path / "r"
    write(root, "Images/a.jpg")
    write(root, "Images/sub/c.gif")
    plan = preview(root, scope="all")
    for rel in ("Images/a.jpg", "Images/sub/c.gif"):
        assert items(plan)[rel].status == "keep"
        assert "already_organized" in items(plan)[rel].flags


def test_existing_category_folder_spelling_is_reused(tmp_path):
    root = tmp_path / "r"
    (root / "images").mkdir(parents=True)
    write(root, "pic.jpg")
    it = items(preview(root))["pic.jpg"]
    assert it.destination == "images/pic.jpg"


def test_file_in_wrong_category_folder_needs_review(tmp_path):
    root = tmp_path / "r"
    write(root, "Images/notes.txt")
    it = items(preview(root))["Images/notes.txt"]
    assert it.status == "review"
    assert it.destination == "Documents/notes.txt"
    assert "in_other_category_folder" in it.flags


# --- conflicts and duplicate filenames ----------------------------------------

def test_existing_destination_is_never_overwritten(tmp_path):
    root = tmp_path / "r"
    write(root, "photo.jpg", b"new!")
    write(root, "Images/photo.jpg", b"old")
    write(root, "Images/photo (2).jpg", b"old")
    it = items(preview(root))["photo.jpg"]
    assert it.status == "conflict"
    assert "destination_exists" in it.flags
    assert it.destination == "Images/photo (3).jpg"
    assert "possible_duplicate" not in it.flags
    assert any("will not be overwritten" in n for n in it.notes)


def test_same_size_existing_file_flagged_as_possible_duplicate(tmp_path):
    root = tmp_path / "r"
    write(root, "photo.jpg", b"same")
    write(root, "Images/photo.jpg", b"same")
    it = items(preview(root))["photo.jpg"]
    assert {"destination_exists", "possible_duplicate"} <= set(it.flags)


def test_two_files_wanting_the_same_name(tmp_path):
    root = tmp_path / "r"
    write(root, "pic.jpg", b"1")
    write(root, "Documents/pic.jpg", b"22")  # wrong folder -> also wants Images/pic.jpg
    plan = preview(root)
    a, b = items(plan)["Documents/pic.jpg"], items(plan)["pic.jpg"]
    assert a.status == b.status == "conflict"
    assert "duplicate_name" in a.flags and "duplicate_name" in b.flags
    assert {a.destination, b.destination} == {"Images/pic.jpg", "Images/pic (2).jpg"}
    assert_destinations_safe(plan)


def test_duplicate_detection_is_case_and_unicode_insensitive(tmp_path):
    nfc = unicodedata.normalize("NFC", "café.jpg")
    nfd = unicodedata.normalize("NFD", "café.jpg")
    scan = fake_scan(
        str(tmp_path),
        [(f"Documents/{nfc}", 1), (f"Audio/{nfd}", 1), ("Code/CAFÉ.JPG", 2)],
        folders=["Documents", "Audio", "Code"],
    )
    plan = build_plan(scan, now=LATER)
    dests = sorted(path_key(i.destination) for i in plan.items)
    assert len(set(dests)) == 3
    assert all(i.status == "conflict" for i in plan.items)
    assert any("possible_duplicate" in i.flags for i in plan.items)


def test_alternative_names_keep_compound_extensions(tmp_path):
    root = tmp_path / "r"
    write(root, "backup.tar.gz")
    write(root, "Archives/backup.tar.gz")
    assert items(preview(root))["backup.tar.gz"].destination == "Archives/backup (2).tar.gz"


def test_alternative_never_takes_another_files_wanted_name(tmp_path):
    scan = fake_scan(
        str(tmp_path),
        [("a.jpg", 1), ("Documents/a.jpg", 2), ("a (2).jpg", 3)],
        folders=["Documents", "Images"],
    )
    plan = build_plan(scan, now=LATER)
    dests = {i.relative_path: i.destination for i in plan.items}
    assert dests["a (2).jpg"] == "Images/a (2).jpg"
    assert dests["a.jpg"] == "Images/a (3).jpg"
    assert dests["Documents/a.jpg"] == "Images/a.jpg"


def test_category_folder_name_taken_by_a_file(tmp_path):
    root = tmp_path / "r"
    write(root, "Images", b"i am a file")  # blocks Images/ as a folder
    write(root, "pic.jpg")
    it = items(preview(root))["pic.jpg"]
    assert it.status == "review"
    assert "blocked_destination" in it.flags


# --- hidden files ----------------------------------------------------------------

def test_hidden_files_excluded_and_counted(tmp_path):
    root = tmp_path / "r"
    write(root, ".DS_Store")
    write(root, ".env")
    write(root, "visible.pdf")
    plan = preview(root)
    assert set(items(plan)) == {"visible.pdf"}
    assert plan.hidden_skipped == 2


def test_hidden_files_shown_but_never_moved(tmp_path):
    root = tmp_path / "r"
    write(root, ".notes.txt")
    write(root, ".config/settings.json")
    plan = preview(root, include_hidden=True)
    it = items(plan)[".notes.txt"]
    assert it.status == "keep" and "hidden" in it.flags
    assert ".config/settings.json" not in items(plan)  # hidden folders are not opened
    assert any(p.relative_path == ".config" for p in plan.protected_folders)


# --- unknown and ambiguous types (conservative) --------------------------------

@pytest.mark.parametrize(
    "name, flag",
    [
        ("mystery.xyz", "unknown_type"),
        ("README", "no_extension"),
        ("app.ts", "ambiguous_type"),
        ("deck.key", "ambiguous_type"),
        ("logo.svg", "ambiguous_type"),
        ("config.json", "ambiguous_type"),
        ("invoice.pdf.zip", "double_extension"),
        ("photo.jpg.mov", "double_extension"),
    ],
)
def test_uncertain_files_need_review(tmp_path, name, flag):
    root = tmp_path / "r"
    write(root, name)
    it = items(preview(root))[name]
    assert it.status == "review"
    assert flag in it.flags
    assert it.notes


def test_legitimate_compound_extension_is_not_flagged(tmp_path):
    root = tmp_path / "r"
    write(root, "src.tar.gz")
    write(root, "jquery.min.js")
    plan = preview(root)
    assert items(plan)["src.tar.gz"].status == "move"
    assert items(plan)["jquery.min.js"].status == "move"
    assert "double_extension" not in items(plan)["jquery.min.js"].flags


def test_extension_alone_is_not_trusted(tmp_path):
    root = tmp_path / "r"
    p = write(root, "invoice.pdf")
    p.chmod(0o755)
    write(root, "empty.docx", b"")
    write(root, "fresh.png")
    plan = preview(root, now=datetime.now(timezone.utc))
    assert "executable_bit" in items(plan)["invoice.pdf"].flags
    assert "empty_file" in items(plan)["empty.docx"].flags
    assert "recently_modified" in items(plan)["fresh.png"].flags
    assert all(i.status == "review" for i in plan.items)


@pytest.mark.parametrize(
    "name, flag",
    [
        ("setup.exe", "executable"),
        ("installer.command", "executable"),
        ("movie.mp4.crdownload", "temporary"),
        ("~$report.docx", "temporary"),
        ("cert.pem", "sensitive"),
        ("id_rsa", "sensitive"),
    ],
)
def test_risky_files_are_left_in_place(tmp_path, name, flag):
    root = tmp_path / "r"
    write(root, name)
    it = items(preview(root))[name]
    assert it.status == "keep"
    assert flag in it.flags
    assert it.destination is None


# --- packages and projects -------------------------------------------------------

def test_macos_packages_are_not_opened(tmp_path):
    root = tmp_path / "r"
    write(root, "Photos Library.photoslibrary/originals/1.jpg")
    write(root, "Thing.app/Contents/Info.plist")
    plan = preview(root, scope="all")
    assert not any(".photoslibrary" in i.relative_path or ".app/" in i.relative_path for i in plan.items)
    protected = {p.relative_path for p in plan.protected_folders}
    assert {"Photos Library.photoslibrary", "Thing.app"} <= protected


def test_files_inside_software_projects_stay(tmp_path):
    root = tmp_path / "r"
    write(root, "myapp/package.json")
    write(root, "myapp/logo.png")
    write(root, "loose.png")
    plan = preview(root, scope="all")
    assert items(plan)["myapp/logo.png"].status == "keep"
    assert "inside_project" in items(plan)["myapp/logo.png"].flags
    assert items(plan)["loose.png"].status == "move"


def test_root_that_is_a_project_gets_no_moves(tmp_path):
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    write(root, ".git/HEAD")
    write(root, "main.py")
    write(root, "diagram.png")
    plan = preview(root, scope="all")
    assert all(i.status == "keep" for i in plan.items)
    assert any("software project" in w for w in plan.warnings)


# --- symbolic links --------------------------------------------------------------

def test_symlinks_are_kept_and_not_followed(tmp_path, outside_tree):
    root = tmp_path / "r"
    root.mkdir()
    (root / "outside_dir").symlink_to(outside_tree, target_is_directory=True)
    (root / "secret_link.txt").symlink_to(outside_tree / "secret.txt")
    plan = preview(root, scope="all", include_hidden=True)
    assert items(plan)["secret_link.txt"].status == "keep"
    assert "symlink" in items(plan)["secret_link.txt"].flags
    assert items(plan)["outside_dir"].status == "keep"
    assert not any("secret.txt" == i.name for i in plan.items)


def test_symlinked_category_folder_is_not_used(tmp_path, outside_tree):
    root = tmp_path / "r"
    root.mkdir()
    (root / "Documents").symlink_to(outside_tree, target_is_directory=True)
    write(root, "letter.pdf")
    it = items(preview(root))["letter.pdf"]
    assert it.status == "review"
    assert "blocked_destination" in it.flags
    assert any("symbolic link" in n for n in it.notes)


def test_symlink_root_rejected(tmp_path, messy):
    link = tmp_path / "link"
    link.symlink_to(messy, target_is_directory=True)
    with pytest.raises(ScanError) as exc:
        preview(link)
    assert exc.value.code == "symlink"


# --- path traversal and root boundary --------------------------------------------

@pytest.mark.parametrize(
    "rel",
    ["../x", "a/../../x", "/etc/passwd", "", ".", "a//b", "a/./b", "a\x00b", "a\\..\\b", ".."],
)
def test_ensure_within_root_rejects_escapes(tmp_path, rel):
    with pytest.raises(PlanBoundaryError):
        ensure_within_root(tmp_path, rel)


def test_ensure_within_root_accepts_normal_paths(tmp_path):
    assert ensure_within_root(tmp_path, "Images/a b/c.jpg") == "Images/a b/c.jpg"
    assert ensure_within_root(tmp_path, "..hidden-ish/x") == "..hidden-ish/x"


def test_traversal_in_root_path_rejected(messy, outside_tree):
    with pytest.raises(ScanError) as exc:
        preview(f"{messy}/../{outside_tree.name}")
    assert exc.value.code == "path_traversal"


def test_malicious_entries_from_scan_are_ignored(tmp_path):
    scan = fake_scan(str(tmp_path), [("../evil.pdf", 1), ("/abs.pdf", 1), ("ok.pdf", 1)])
    plan = build_plan(scan, now=LATER)
    assert items(plan)["../evil.pdf"].status == "keep"
    assert "outside_root" in items(plan)["../evil.pdf"].flags
    assert items(plan)["/abs.pdf"].destination is None
    assert items(plan)["ok.pdf"].destination == "Documents/ok.pdf"


def test_every_destination_stays_inside_root(messy, outside_tree):
    (messy / "escape").symlink_to(outside_tree, target_is_directory=True)
    write(messy, "Images/photo.jpg")
    for scope in ("top_level", "all"):
        assert_destinations_safe(preview(messy, scope=scope))


# --- scan limits and unreadable locations ------------------------------------------

def test_truncated_scan_marks_everything_for_review(messy):
    plan = preview(messy, cfg=Settings(max_entries=4))
    assert not plan.complete
    assert plan.scan_truncated
    assert all(i.status in ("review", "keep", "conflict") for i in plan.items)
    assert any("incomplete_scan" in i.flags for i in plan.items)


@needs_non_root
def test_unreadable_destination_folder_is_not_trusted(tmp_path):
    root = tmp_path / "r"
    write(root, "pic.jpg")
    images = root / "Images"
    images.mkdir()
    images.chmod(0)
    try:
        plan = preview(root)
    finally:
        images.chmod(0o755)
    it = items(plan)["pic.jpg"]
    assert it.status == "review"
    assert "unverified_destination" in it.flags


# --- strictly read-only ------------------------------------------------------------

def test_preview_changes_nothing_on_disk(messy):
    write(messy, "Images/photo.jpg")
    before = snapshot(messy)
    for scope in ("top_level", "all"):
        preview(messy, scope=scope, include_hidden=True)
    assert snapshot(messy) == before
    assert not (messy / "Documents").exists()
    assert not (messy / "Videos").exists()
