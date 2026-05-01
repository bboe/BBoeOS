"""Pytest unit tests for add_file.py's batched API.

Builds drive.img once per session (via make_os.sh) and per-test makes a
working copy inspected with find_entry.  These tests cover the bbfs and
ext2 batch paths at the unit level; end-to-end coverage of the ext2
filesystem lives in tests/test_ext2.py.

Run with: ``pytest tests/test_add_file.py``
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from add_file import (  # noqa: E402
    add_files,
    compute_directory_sector,
    find_entry,
    find_subdirectory_entry,
    read_assign,
)


@pytest.fixture(scope="session")
def base_image() -> Iterator[Path]:
    """Build drive.img once per pytest session.

    Yields
    ------
    Path
        Path to the cached drive image.

    """
    tmpdir = Path(tempfile.mkdtemp(prefix="test_add_file_"))
    image = tmpdir / "drive.img"
    subprocess.run(
        ["./make_os.sh", str(image)],
        check=True,
        cwd=REPO_ROOT,
        capture_output=True,
    )
    try:
        yield image
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def bbfs_image(base_image: Path, tmp_path: Path) -> Path:
    """Per-test copy of the cached bbfs drive.img."""
    image = tmp_path / "drive.img"
    shutil.copy2(base_image, image)
    return image


def _bin_start_sector(*, image: Path) -> int:
    """Return the start sector of bin/ in the given image."""
    directory_sector = compute_directory_sector(image_path=str(image))
    directory_sectors = read_assign("DIRECTORY_SECTORS")
    image_data = bytearray(image.read_bytes())
    bin_offset = find_subdirectory_entry(
        directory_sector=directory_sector,
        directory_sectors=directory_sectors,
        image=image_data,
        name="bin",
    )
    assert bin_offset is not None, "bin/ subdirectory not found in image"
    return int.from_bytes(image_data[bin_offset + 26 : bin_offset + 28], "little")


def test_three_fillers_one_save(bbfs_image: Path, tmp_path: Path) -> None:
    """Three empty files added in one add_files() call all appear in bin/."""
    files = []
    for name in ("zalpha", "zbeta", "zgamma"):
        path = tmp_path / name
        path.touch()
        files.append(str(path))
    add_files(
        allow_empty=True,
        executable=False,
        file_paths=files,
        image_path=str(bbfs_image),
        subdirectory="bin",
    )
    bin_start = _bin_start_sector(image=bbfs_image)
    directory_sectors = read_assign("DIRECTORY_SECTORS")
    image_data = bytearray(bbfs_image.read_bytes())
    for name in ("zalpha", "zbeta", "zgamma"):
        entry = find_entry(
            directory_sectors=directory_sectors,
            directory_start_sector=bin_start,
            image=image_data,
            name=name,
        )
        assert entry is not None, f"{name!r} not found in bin/"
