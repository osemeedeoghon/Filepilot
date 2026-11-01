import hashlib
import os
import threading
from pathlib import Path

import pytest

from filepilot import duplicates
from filepilot.config import Settings
from filepilot.duplicates import Progress, SkipFile, find_duplicates, hash_file, open_beneath
from filepilot.organizer import PlanBoundaryError
from filepilot.scanner import ScanError

needs_non_root = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")


def write(root: Path, rel: str, data: bytes) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def groups(report):
    return [sorted(f.relative_path for f in g.files) for g in report.groups]


def snapshot(root: Path):
    out = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            st = p.lstat()
            out[str(p.relative_to(root))] = (st.st_mode, st.st_size, st.st_mtime_ns, st.st_ino)
    return out


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "root"
    r.mkdir()
    return r


# --- exact matching ------------------------------------------------------------

def test_identical_files_with_different_names_are_grouped(root):
    write(root, "a.jpg", b"same bytes")
    write(root, "copy of a.JPG", b"same bytes")
    write(root, "sub/renamed.dat", b"same bytes")
    report = find_duplicates(str(root))
    assert groups(report) == [["a.jpg", "copy of a.JPG", "sub/renamed.dat"]]
    g = report.groups[0]
    assert g.count == 3
    assert g.size == 10
    assert g.sha256 == hashlib.sha256(b"same bytes").hexdigest()
    assert g.recoverable_bytes == 20
    assert not g.hard_linked


def test_same_name_different_contents_are_not_duplicates(root):
    write(root, "one/report.pdf", b"version A")
    write(root, "two/report.pdf", b"version B")  # same size, same name
    report = find_duplicates(str(root))
    assert report.groups == []
    assert report.files_hashed == 2  # same size, so contents were compared


def test_different_sizes_are_never_hashed(root):
    write(root, "a.txt", b"short")
    write(root, "b.txt", b"much longer")
    report = find_duplicates(str(root))
    assert report.groups == []
    assert report.files_hashed == 0
    assert report.bytes_read == 0
    assert report.unique_size_files == 2


def test_empty_files_are_skipped(root):
    write(root, "empty1.txt", b"")
    write(root, "empty2.txt", b"")
    write(root, "empty3.log", b"")
    report = find_duplicates(str(root))
    assert report.groups == []
    assert report.empty_files == 3
    assert report.files_considered == 0


def test_multiple_groups_and_totals(root):
    write(root, "x1", b"x" * 100)
    write(root, "x2", b"x" * 100)
    write(root, "y1", b"y" * 10)
    write(root, "y2", b"y" * 10)
    write(root, "y3", b"y" * 10)
    write(root, "z", b"z" * 10)  # same size as y*, different content
    report = find_duplicates(str(root))
    assert report.group_count == 2
    assert report.duplicate_files == 1 + 2
    assert report.recoverable_bytes == 100 + 20
    assert report.groups[0].size == 100  # most recoverable first
    assert groups(report)[1] == ["y1", "y2", "y3"]


def test_hard_links_do_not_count_as_recoverable(root):
    a = write(root, "a.bin", b"linked data")
    os.link(a, root / "b.bin")
    write(root, "c.bin", b"linked data")
    report = find_duplicates(str(root))
    g = report.groups[0]
    assert g.count == 3
    assert g.hard_linked
    assert g.recoverable_bytes == len(b"linked data")  # two distinct copies -> one recoverable


# --- large files: prefix pass and chunked reads --------------------------------

def test_prefix_pass_then_full_hash(root):
    cfg = Settings(dup_prefix_threshold=16, dup_prefix_bytes=8, dup_chunk_bytes=8)
    write(root, "same1", b"HEADER--" + b"A" * 40)
    write(root, "same2", b"HEADER--" + b"A" * 40)
    write(root, "late_diff", b"HEADER--" + b"A" * 39 + b"B")  # same prefix, differs at the end
    write(root, "early_diff", b"OTHER---" + b"A" * 40)  # eliminated by the prefix pass
    report = find_duplicates(str(root), cfg=cfg)
    assert groups(report) == [["same1", "same2"]]
    # prefix (4 x 8 bytes) + full reads of the 3 files sharing a prefix (3 x 48 bytes)
    assert report.bytes_read == 4 * 8 + 3 * 48


def test_files_are_read_in_chunks(root, monkeypatch):
    data = os.urandom(10_000)
    write(root, "big1", data)
    write(root, "big2", data)
    sizes = []
    real_read = os.read

    def spy(fd, n):
        sizes.append(n)
        return real_read(fd, n)

    monkeypatch.setattr(duplicates.os, "read", spy)
    report = find_duplicates(str(root), cfg=Settings(dup_chunk_bytes=1024, dup_prefix_threshold=1 << 30))
    assert report.groups[0].sha256 == hashlib.sha256(data).hexdigest()
    assert max(sizes) <= 1024
    assert len(sizes) >= 2 * 10


# --- unreadable, vanishing and changing files ----------------------------------

@needs_non_root
def test_unreadable_file_is_skipped_safely(root):
    write(root, "a.txt", b"duplicate!")
    write(root, "b.txt", b"duplicate!")
    locked = write(root, "c.txt", b"duplicate!")
    locked.chmod(0)
    try:
        report = find_duplicates(str(root))
    finally:
        locked.chmod(0o644)
    assert groups(report) == [["a.txt", "b.txt"]]
    assert report.skipped_count == 1
    assert report.skipped[0].relative_path == "c.txt"
    assert "Permission denied" in report.skipped[0].reason


@needs_non_root
def test_unreadable_folder_is_skipped_safely(root):
    write(root, "a.txt", b"dup")
    write(root, "b.txt", b"dup")
    locked = root / "locked"
    write(root, "locked/c.txt", b"dup")
    locked.chmod(0)
    try:
        report = find_duplicates(str(root))
    finally:
        locked.chmod(0o755)
    assert groups(report) == [["a.txt", "b.txt"]]
    assert any(s.relative_path == "locked" for s in report.skipped)


def test_file_disappearing_during_scan(root, monkeypatch):
    write(root, "a.txt", b"vanishing")
    write(root, "b.txt", b"vanishing")
    write(root, "c.txt", b"vanishing")
    real_scan = duplicates.scan_folder

    def scan_then_delete(*args, **kwargs):
        result = real_scan(*args, **kwargs)
        (root / "b.txt").unlink()  # simulate another app deleting it mid-scan
        return result

    monkeypatch.setattr(duplicates, "scan_folder", scan_then_delete)
    report = find_duplicates(str(root))
    assert groups(report) == [["a.txt", "c.txt"]]
    assert report.skipped[0].relative_path == "b.txt"
    assert "disappeared" in report.skipped[0].reason


def test_file_changing_size_during_scan(root):
    write(root, "a.txt", b"12345")
    with pytest.raises(SkipFile, match="Changed size"):
        hash_file(root, "a.txt", expected_size=4)


def test_fifo_is_not_read_and_does_not_block(root):
    os.mkfifo(root / "pipe")
    with pytest.raises(SkipFile, match="regular file"):
        hash_file(root, "pipe", expected_size=0)


def test_unexpected_read_error_is_reported(root, monkeypatch):
    write(root, "a.txt", b"abc")

    def broken(fd, n):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(duplicates.os, "read", broken)
    with pytest.raises(SkipFile, match="Input/output error"):
        hash_file(root, "a.txt", expected_size=3)


# --- symbolic links ---------------------------------------------------------------

def test_symlinks_are_ignored_not_followed(root, outside_tree):
    write(root, "real.txt", b"top secret")  # same contents as outside/secret.txt
    (root / "link_to_real").symlink_to(root / "real.txt")
    (root / "link_to_outside_file").symlink_to(outside_tree / "secret.txt")
    (root / "link_to_outside_dir").symlink_to(outside_tree, target_is_directory=True)
    report = find_duplicates(str(root))
    assert report.groups == []
    assert report.symlinks_ignored == 3
    assert report.files_considered == 1


def test_symlink_as_final_component_is_refused(root, outside_tree):
    (root / "sneaky.txt").symlink_to(outside_tree / "secret.txt")
    with pytest.raises(SkipFile, match="symbolic link"):
        hash_file(root, "sneaky.txt", expected_size=len(b"top secret"))


def test_symlink_swapped_into_parent_folder_is_refused(root, outside_tree):
    # As if "docs" were replaced by a link to another folder after listing.
    (root / "docs").symlink_to(outside_tree, target_is_directory=True)
    with pytest.raises(SkipFile, match="symbolic link"):
        hash_file(root, "docs/secret.txt", expected_size=len(b"top secret"))


def test_symlink_root_rejected(root, tmp_path):
    link = tmp_path / "link"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(ScanError) as exc:
        find_duplicates(str(link))
    assert exc.value.code == "symlink"


# --- root boundary -----------------------------------------------------------------

@pytest.mark.parametrize("rel", ["../outside/secret.txt", "/etc/hosts", "a/../../x", ""])
def test_open_beneath_rejects_escapes(root, rel):
    with pytest.raises(PlanBoundaryError):
        open_beneath(root, rel)


def test_hash_file_outside_root_is_skipped(tmp_path, root, outside_tree):
    with pytest.raises(SkipFile, match="Outside"):
        hash_file(root, f"../{outside_tree.name}/secret.txt", expected_size=10)


@pytest.mark.parametrize(
    "raw, code",
    [("relative/dir", "invalid_path"), ("/no/such/dir/x", "not_found"), ("/", "blocked_location")],
)
def test_root_validation_reused(raw, code):
    with pytest.raises(ScanError) as exc:
        find_duplicates(raw)
    assert exc.value.code == code


def test_traversal_root_rejected(root, outside_tree):
    with pytest.raises(ScanError) as exc:
        find_duplicates(f"{root}/../{outside_tree.name}")
    assert exc.value.code == "path_traversal"


def test_reported_paths_stay_inside_root(root, outside_tree):
    write(root, "a/x.bin", b"dupdup")
    write(root, "b/y.bin", b"dupdup")
    (root / "out").symlink_to(outside_tree, target_is_directory=True)
    report = find_duplicates(str(root))
    for g in report.groups:
        for f in g.files:
            assert not f.relative_path.startswith("/")
            assert ".." not in f.relative_path.split("/")
            resolved = os.path.realpath(os.path.join(report.root, f.relative_path))
            assert os.path.commonpath([report.root, resolved]) == report.root


# --- hidden files, packages and protected folders -------------------------------

def test_hidden_files_follow_the_toggle(root):
    write(root, "a.txt", b"dup")
    write(root, ".a.txt", b"dup")
    assert find_duplicates(str(root)).groups == []
    assert groups(find_duplicates(str(root), include_hidden=True)) == [[".a.txt", "a.txt"]]


def test_packages_and_vcs_folders_are_not_opened(root):
    write(root, "photo.jpg", b"pixels")
    write(root, "Lib.photoslibrary/originals/photo.jpg", b"pixels")
    write(root, "Thing.app/Contents/photo.jpg", b"pixels")
    write(root, "proj/.git/objects/aa", b"pixels")
    write(root, "node_modules/pkg/photo.jpg", b"pixels")
    report = find_duplicates(str(root), include_hidden=True)
    assert report.groups == []
    assert {"Lib.photoslibrary", "Thing.app", "proj/.git", "node_modules"} <= set(report.protected_folders)


# --- limits, cancellation and progress -------------------------------------------

def test_byte_limit_stops_with_partial_results(root):
    for i in range(3):
        write(root, f"a{i}", b"A" * 100)
        write(root, f"b{i}", b"B" * 50)
    report = find_duplicates(str(root), cfg=Settings(dup_max_bytes=150, dup_chunk_bytes=50))
    assert report.truncated
    assert "GB limit" in report.truncated_reason
    assert report.unchecked_files > 0
    assert not report.cancelled


def test_time_limit_stops(root):
    write(root, "a", b"same")
    write(root, "b", b"same")
    report = find_duplicates(str(root), cfg=Settings(dup_max_seconds=-1))
    assert report.truncated
    assert "second" in report.truncated_reason
    assert report.groups == []


def test_cancel(root):
    write(root, "a", b"same")
    write(root, "b", b"same")
    cancel = threading.Event()
    cancel.set()
    report = find_duplicates(str(root), cancel=cancel)
    assert report.cancelled and report.truncated
    assert report.unchecked_files == 2


def test_progress_reaches_completion(root):
    for i in range(4):
        write(root, f"f{i}", b"same content")
    progress = Progress()
    report = find_duplicates(str(root), progress=progress)
    assert progress.phase == "done"
    assert progress.files_total == 4
    assert progress.files_done == 4
    assert progress.groups_found == 1
    assert progress.bytes_read == report.bytes_read
    assert "elapsed_seconds" in progress.to_dict()


# --- strictly read-only ------------------------------------------------------------

def test_detection_changes_nothing(root):
    write(root, "a.txt", b"dup")
    write(root, "b/a.txt", b"dup")
    write(root, "c.txt", b"different")
    before = snapshot(root)
    find_duplicates(str(root), include_hidden=True)
    assert snapshot(root) == before
