import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from filepilot.app import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def sample_tree(tmp_path: Path) -> Path:
    """A small folder tree with known contents.

    root/
      readme.md            (11 bytes)
      photo.JPG            (3 bytes)
      .hidden.txt          (2 bytes)
      docs/
        report.pdf         (5 bytes)
        nested/
          data.csv         (4 bytes)
      empty_dir/
    """
    root = tmp_path / "root"
    (root / "docs" / "nested").mkdir(parents=True)
    (root / "empty_dir").mkdir()
    (root / "readme.md").write_text("hello world")
    (root / "photo.JPG").write_bytes(b"abc")
    (root / ".hidden.txt").write_text("hi")
    (root / "docs" / "report.pdf").write_bytes(b"%PDF-")
    (root / "docs" / "nested" / "data.csv").write_text("a,b\n")
    os.utime(root / "readme.md", (1_700_000_000, 1_700_000_000))
    return root


@pytest.fixture
def outside_tree(tmp_path: Path) -> Path:
    """A folder next to (not inside) sample_tree, holding a secret file."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("top secret")
    return outside


@pytest.fixture
def locked_dir(sample_tree: Path):
    """A subfolder with all permissions removed; restored after the test."""
    locked = sample_tree / "locked"
    locked.mkdir()
    (locked / "private.txt").write_text("nope")
    locked.chmod(0)
    yield locked
    locked.chmod(0o755)
