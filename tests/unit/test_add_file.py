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

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
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


@pytest.fixture
def ext2_image(tmp_path: Path) -> Path:
    """Per-test fresh ext2 drive image.

    Each test mutates the image (adds files), so the build cannot be
    cached across tests the way ``base_image`` is.
    """
    image = tmp_path / "drive_ext2.img"
    subprocess.run(
        ["./make_os.sh", "--ext2", str(image)],
        check=True,
        cwd=REPO_ROOT,
        capture_output=True,
    )
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


def test_batch_runs_single_debugfs_session(
    ext2_image: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """add_files() on ext2 must spawn exactly one debugfs process."""
    files = []
    for name in ("zalpha", "zbeta", "zgamma"):
        path = tmp_path / name
        path.write_text("hi\n")
        files.append(str(path))

    import add_file as add_file_module  # noqa: PLC0415

    original_run = subprocess.run
    debugfs_calls: list[list[str]] = []

    def counting_run(  # type: ignore[misc]
        command: list[str] | str,
        *args: object,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        if isinstance(command, list) and command and command[0].endswith("debugfs"):
            debugfs_calls.append(list(command))
        return original_run(command, *args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(add_file_module.subprocess, "run", counting_run)
    add_files(
        executable=False,
        file_paths=files,
        image_path=str(ext2_image),
        subdirectory="bin",
    )

    # ext2_add_files batches all writes into a single debugfs session.
    assert len(debugfs_calls) == 1, f"expected 1 debugfs invocation, got {len(debugfs_calls)}: {debugfs_calls}"


def test_batch_files_appear(ext2_image: Path, tmp_path: Path) -> None:
    """Files added via add_files() are visible in the ext2 partition."""
    files = []
    for name in ("zalpha", "zbeta", "zgamma"):
        path = tmp_path / name
        path.write_text("hi\n")
        files.append(str(path))
    add_files(
        executable=True,
        file_paths=files,
        image_path=str(ext2_image),
        subdirectory="bin",
    )
    ext2_start_sector = compute_directory_sector(image_path=str(ext2_image))
    partition = tmp_path / "partition.ext2"
    subprocess.run(
        ["dd", f"if={ext2_image}", f"of={partition}", "bs=512", f"skip={ext2_start_sector}", "status=none"],
        check=True,
    )
    listing = subprocess.run(
        ["debugfs", "-R", "ls /bin", str(partition)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    for name in ("zalpha", "zbeta", "zgamma"):
        assert name in listing, f"{name!r} not found in ext2 /bin listing"


def test_cli_accepts_multiple_files(bbfs_image: Path, tmp_path: Path) -> None:
    """CLI invoked with multiple positional file args adds all of them."""
    paths = []
    for name in ("zone", "ztwo", "zthree"):
        path = tmp_path / name
        path.write_bytes(b"hi\n")
        paths.append(str(path))
    result = subprocess.run(
        ["./add_file.py", "--image", str(bbfs_image), "-d", "bin", *paths],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    bin_start = _bin_start_sector(image=bbfs_image)
    directory_sectors = read_assign("DIRECTORY_SECTORS")
    image_data = bytearray(bbfs_image.read_bytes())
    for name in ("zone", "ztwo", "zthree"):
        entry = find_entry(
            directory_sectors=directory_sectors,
            directory_start_sector=bin_start,
            image=image_data,
            name=name,
        )
        assert entry is not None, f"{name!r} not in image after CLI batch add"
