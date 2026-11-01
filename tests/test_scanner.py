import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from filepilot.config import Settings
from filepilot.scanner import ScanError, categorize, is_within, scan_folder, validate_root

needs_non_root = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")


def by_path(result):
    return {f.relative_path: f for f in result.files}


# --- valid scans -------------------------------------------------------------

def test_scans_folder_and_subfolders(sample_tree):
    result = scan_folder(str(sample_tree))
    assert set(by_path(result)) == {"readme.md", "photo.JPG", "docs/report.pdf", "docs/nested/data.csv"}
    assert result.total_files == 4
    assert result.total_folders == 3  # docs, docs/nested, empty_dir
    assert result.total_size == 11 + 3 + 5 + 4
    assert result.root == os.path.realpath(sample_tree)
    assert not result.truncated
    assert result.skipped_count == 0


def test_hidden_files_excluded_by_default_and_included_on_request(sample_tree):
    assert ".hidden.txt" not in by_path(scan_folder(str(sample_tree)))
    result = scan_folder(str(sample_tree), include_hidden=True)
    assert ".hidden.txt" in by_path(result)
    assert result.total_files == 5


def test_empty_folder(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    result = scan_folder(str(empty))
    assert result.files == []
    assert (result.total_files, result.total_folders, result.total_size) == (0, 0, 0)


def test_trailing_slash_and_tilde_are_accepted(sample_tree):
    assert scan_folder(str(sample_tree) + "/").total_files == 4
    home = validate_root("~")
    assert home == Path(os.path.realpath(os.path.expanduser("~")))


# --- metadata ----------------------------------------------------------------

def test_file_metadata(sample_tree):
    files = by_path(scan_folder(str(sample_tree)))
    readme = files["readme.md"]
    assert readme.name == "readme.md"
    assert readme.kind == "file"
    assert readme.category == "Document"
    assert readme.extension == "md"
    assert readme.size == 11
    assert datetime.fromisoformat(readme.modified) == datetime.fromtimestamp(1_700_000_000, tz=timezone.utc)

    assert files["photo.JPG"].category == "Image"
    assert files["photo.JPG"].extension == "jpg"
    nested = files["docs/nested/data.csv"]
    assert nested.name == "data.csv"
    assert nested.category == "Spreadsheet"


@pytest.mark.parametrize(
    "name, expected",
    [
        ("a.PY", ("Code", "py")),
        ("Makefile", ("Other", "")),
        (".bashrc", ("Other", "")),
        ("archive.tar.gz", ("Archive", "gz")),
        ("weird.xyz", ("Other", "xyz")),
    ],
)
def test_categorize(name, expected):
    assert categorize(name) == expected


def test_scanner_never_modifies_files(sample_tree):
    before = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in sample_tree.rglob("*")}
    scan_folder(str(sample_tree), include_hidden=True)
    after = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in sample_tree.rglob("*")}
    assert before == after


# --- invalid paths -----------------------------------------------------------

@pytest.mark.parametrize(
    "raw, code",
    [
        ("", "invalid_path"),
        ("   ", "invalid_path"),
        ("relative/folder", "invalid_path"),
        ("./here", "invalid_path"),
        ("/no/such/folder/for/filepilot", "not_found"),
        ("/tmp/bad\x00name", "invalid_path"),
        ("/tmp/line\nbreak", "invalid_path"),
        ("/" + "a" * 2000, "invalid_path"),
        (None, "invalid_path"),
        (123, "invalid_path"),
    ],
)
def test_invalid_paths_rejected(raw, code):
    with pytest.raises(ScanError) as exc:
        scan_folder(raw)
    assert exc.value.code == code


def test_file_instead_of_folder(sample_tree):
    with pytest.raises(ScanError) as exc:
        scan_folder(str(sample_tree / "readme.md"))
    assert exc.value.code == "not_a_directory"


@pytest.mark.parametrize("raw", ["/", "/System", "/Library", "/usr", "/Users", "/var", "/etc", "/private"])
def test_system_locations_blocked(raw):
    if not os.path.exists(raw):
        pytest.skip(f"{raw} does not exist on this system")
    with pytest.raises(ScanError) as exc:
        scan_folder(raw)
    assert exc.value.code == "blocked_location"


# --- path boundary -----------------------------------------------------------

def test_dotdot_traversal_rejected(sample_tree, outside_tree):
    sneaky = f"{sample_tree}/../{outside_tree.name}"
    assert os.path.isdir(sneaky)  # the path is real, but must still be refused
    with pytest.raises(ScanError) as exc:
        scan_folder(sneaky)
    assert exc.value.code == "path_traversal"


def test_symlink_as_root_rejected(sample_tree, tmp_path):
    link = tmp_path / "link_to_root"
    link.symlink_to(sample_tree, target_is_directory=True)
    with pytest.raises(ScanError) as exc:
        scan_folder(str(link))
    assert exc.value.code == "symlink"


def test_symlinks_inside_are_listed_but_not_followed(sample_tree, outside_tree):
    (sample_tree / "escape_dir").symlink_to(outside_tree, target_is_directory=True)
    (sample_tree / "escape_file").symlink_to(outside_tree / "secret.txt")
    (sample_tree / "dangling").symlink_to(sample_tree / "missing")

    result = scan_folder(str(sample_tree))
    files = by_path(result)

    assert result.symlinks == 3
    for name in ("escape_dir", "escape_file", "dangling"):
        assert files[name].kind == "symlink"
        assert files[name].category == "Symbolic link"
    # Nothing from the link target leaks into the result.
    assert not any("secret" in p for p in files)
    assert result.total_files == 4
    assert result.total_size == 11 + 3 + 5 + 4


def test_symlink_loop_does_not_hang(sample_tree):
    (sample_tree / "docs" / "loop").symlink_to(sample_tree, target_is_directory=True)
    result = scan_folder(str(sample_tree))
    assert result.total_files == 4
    assert result.symlinks == 1


def test_every_entry_stays_inside_root(sample_tree, outside_tree):
    (sample_tree / "escape_dir").symlink_to(outside_tree, target_is_directory=True)
    result = scan_folder(str(sample_tree), include_hidden=True)
    root = Path(result.root)
    for f in result.files:
        assert not f.relative_path.startswith("/")
        assert ".." not in Path(f.relative_path).parts
        assert is_within(root, root / f.relative_path)


def test_is_within():
    root = Path("/Users/me/Project")
    assert is_within(root, root)
    assert is_within(root, root / "a" / "b.txt")
    assert not is_within(root, Path("/Users/me/Project2/file"))
    assert not is_within(root, Path("/Users/me"))


# --- inaccessible locations --------------------------------------------------

@needs_non_root
def test_inaccessible_subfolder_is_skipped_not_fatal(sample_tree, locked_dir):
    result = scan_folder(str(sample_tree))
    assert result.total_files == 4
    assert result.skipped_count == 1
    assert result.skipped[0].relative_path == "locked"
    assert "Permission denied" in result.skipped[0].reason
    assert "locked/private.txt" not in by_path(result)


@needs_non_root
def test_inaccessible_root_rejected(locked_dir):
    with pytest.raises(ScanError) as exc:
        scan_folder(str(locked_dir))
    assert exc.value.code == "permission_denied"


# --- limits ------------------------------------------------------------------

def test_entry_limit_truncates(sample_tree):
    result = scan_folder(str(sample_tree), cfg=Settings(max_entries=2))
    assert result.truncated
    assert "limit" in result.truncated_reason
    assert len(result.files) + result.total_folders <= 2


def test_depth_limit(sample_tree):
    result = scan_folder(str(sample_tree), cfg=Settings(max_depth=1))
    files = by_path(result)
    assert "docs/report.pdf" in files
    assert "docs/nested/data.csv" not in files
    assert result.depth_limited


def test_depth_zero_lists_only_top_level(sample_tree):
    result = scan_folder(str(sample_tree), cfg=Settings(max_depth=0))
    assert set(by_path(result)) == {"readme.md", "photo.JPG"}
    assert result.depth_limited


def test_time_limit(sample_tree):
    result = scan_folder(str(sample_tree), cfg=Settings(max_scan_seconds=-1))
    assert result.truncated
    assert "seconds" in result.truncated_reason


def test_skipped_errors_are_capped(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root ignores file permissions")
    root = tmp_path / "many"
    root.mkdir()
    dirs = []
    for i in range(5):
        d = root / f"d{i}"
        d.mkdir()
        d.chmod(0)
        dirs.append(d)
    try:
        result = scan_folder(str(root), cfg=Settings(max_reported_errors=2))
    finally:
        for d in dirs:
            d.chmod(0o755)
    assert result.skipped_count == 5
    assert len(result.skipped) == 2
