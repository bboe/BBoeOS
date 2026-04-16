#!/usr/bin/env python3
"""Filesystem regression tests.

Boots the OS in QEMU, runs a sequence of shell commands, and inspects the
resulting drive image to verify that the filesystem syscalls (fs_copy,
fs_mkdir, fs_find, fs_create, ...) handle large files (>64 KB) and
directory entries that live in the second directory sector.

Requires: nasm, qemu-system-i386
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from run_qemu import run_commands  # noqa: E402

from add_file import FLAG_DIRECTORY, SECTOR_SIZE, find_entry, read_assign  # noqa: E402

BASE_IMAGE = "drive.img"
COMMAND_TIMEOUT = 30


def main() -> int:
    """Run the filesystem regression test suite."""
    os.chdir(REPO_ROOT)
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("test", nargs="?", help="run only the named test")
    arguments = parser.parse_args()

    directory_sector = read_assign("DIRECTORY_SECTOR")
    directory_sectors = read_assign("DIRECTORY_SECTORS")

    tests = [
        ("copy_large", test_copy_large),
        ("copy_to_subdirectory", test_copy_to_subdirectory),
        ("cross_directory_move", test_cross_directory_move),
        ("make_directory_high_sector", test_make_directory_high_sector),
        ("second_directory_sector", test_second_directory_sector),
    ]
    if arguments.test:
        tests = [t for t in tests if t[0] == arguments.test]
        if not tests:
            print(f"No test named '{arguments.test}'")
            return 1

    pass_count = 0
    fail_count = 0
    failed: list[str] = []
    with tempfile.TemporaryDirectory(prefix="test_fs_") as temporary_path:
        temporary_directory = Path(temporary_path)
        image = temporary_directory / BASE_IMAGE
        subprocess.run(
            ["./make_os.sh", str(image)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for name, test_function in tests:
            started = time.monotonic()
            try:
                test_function(
                    directory_sector=directory_sector,
                    directory_sectors=directory_sectors,
                    temporary_directory=temporary_directory,
                )
                ok, message = True, ""
            except AssertionError as e:
                ok, message = False, str(e)
            except Exception as e:  # noqa: BLE001
                ok, message = False, f"{type(e).__name__}: {e}"
            elapsed = time.monotonic() - started
            label = name
            if ok:
                print(f"  PASS  {label:<22}  {elapsed:6.2f}s")
                pass_count += 1
            else:
                print(f"  FAIL  {label:<22}  {message}  {elapsed:6.2f}s")
                fail_count += 1
                failed.append(label)

    print()
    print(f"{pass_count} passed, {fail_count} failed")
    if fail_count:
        print("Failed:", " ".join(failed))
    return 1 if fail_count else 0


def make_drive(*, name: str, temporary_directory: Path) -> Path:
    """Create a copy of the base drive image for a test case."""
    drive = temporary_directory / f"drive_{name}.img"
    shutil.copy(temporary_directory / BASE_IMAGE, drive)
    return drive


def test_copy_large(*, directory_sector: int, directory_sectors: int, temporary_directory: Path) -> None:
    """Copy src/asm.asm (>64 KB, sector >255) to a new root file.

    Verify the destination is byte-identical to the source.
    """
    drive = make_drive(name="copy_large", temporary_directory=temporary_directory)
    run_commands(["cp src/asm.asm big"], command_timeout=COMMAND_TIMEOUT, drive=drive)
    image = drive.read_bytes()

    big = find_entry(
        directory_sectors=directory_sectors,
        directory_start_sector=directory_sector,
        image=image,
        name="big",
    )
    assert big is not None, "big not found in root"
    _flags, big_sector, big_size = big
    assert big_size > 65535, f"expected size > 64 KB, got {big_size}"
    assert big_sector > 255, f"expected sector > 255, got {big_sector}"

    source_entry = find_entry(
        directory_sectors=directory_sectors,
        directory_start_sector=directory_sector,
        image=image,
        name="src",
    )
    assert source_entry is not None
    assert source_entry[0] & FLAG_DIRECTORY
    asm = find_entry(
        directory_sectors=directory_sectors,
        directory_start_sector=source_entry[1],
        image=image,
        name="asm.asm",
    )
    assert asm is not None, "src/asm.asm not found"
    _, source_sector, source_size = asm

    big_data = image[(big_sector - 1) * SECTOR_SIZE :][:big_size]
    source_data = image[(source_sector - 1) * SECTOR_SIZE :][:source_size]
    assert big_size == source_size, f"size {big_size} != {source_size}"
    assert big_data == source_data, "copied data does not match source"


def test_copy_to_subdirectory(*, directory_sector: int, directory_sectors: int, temporary_directory: Path) -> None:
    """Copy a file into a subdirectory and verify the entry shows up there."""
    drive = make_drive(name="copy_subdirectory", temporary_directory=temporary_directory)
    run_commands(
        ["mkdir d", "cp bin/cat d/h"],
        command_timeout=COMMAND_TIMEOUT,
        drive=drive,
    )
    image = drive.read_bytes()

    directory_entry = find_entry(
        directory_sectors=directory_sectors,
        directory_start_sector=directory_sector,
        image=image,
        name="d",
    )
    assert directory_entry is not None and directory_entry[0] & FLAG_DIRECTORY, "d/ not created"
    file_entry = find_entry(
        directory_sectors=directory_sectors,
        directory_start_sector=directory_entry[1],
        image=image,
        name="h",
    )
    assert file_entry is not None, "d/h not found"
    _, file_sector, file_size = file_entry

    bin_entry = find_entry(
        directory_sectors=directory_sectors,
        directory_start_sector=directory_sector,
        image=image,
        name="bin",
    )
    cat = find_entry(
        directory_sectors=directory_sectors,
        directory_start_sector=bin_entry[1],
        image=image,
        name="cat",
    )
    _, cat_sector, cat_size = cat
    assert file_size == cat_size
    h_data = image[(file_sector - 1) * SECTOR_SIZE :][:file_size]
    cat_data = image[(cat_sector - 1) * SECTOR_SIZE :][:cat_size]
    assert h_data == cat_data, "subdirectory copy data mismatch"


def test_cross_directory_move(*, directory_sector: int, directory_sectors: int, temporary_directory: Path) -> None:
    """Copy a file from bin/ into root, then mv it back into bin/.

    Verify the source entry is gone from root, the dest entry exists
    in bin/, and the data is preserved.
    """
    drive = make_drive(name="cross_move", temporary_directory=temporary_directory)
    run_commands(
        ["cp bin/cat a.txt", "mv a.txt bin/a.txt"],
        command_timeout=COMMAND_TIMEOUT,
        drive=drive,
    )
    image = drive.read_bytes()

    assert (
        find_entry(
            directory_sectors=directory_sectors,
            directory_start_sector=directory_sector,
            image=image,
            name="a.txt",
        )
        is None
    ), "a.txt still in root after mv"

    bin_entry = find_entry(
        directory_sectors=directory_sectors,
        directory_start_sector=directory_sector,
        image=image,
        name="bin",
    )
    assert bin_entry is not None
    assert bin_entry[0] & FLAG_DIRECTORY
    moved = find_entry(
        directory_sectors=directory_sectors,
        directory_start_sector=bin_entry[1],
        image=image,
        name="a.txt",
    )
    assert moved is not None, "bin/a.txt not found after mv"
    _, moved_sector, moved_size = moved

    cat = find_entry(
        directory_sectors=directory_sectors,
        directory_start_sector=bin_entry[1],
        image=image,
        name="cat",
    )
    _, cat_sector, cat_size = cat
    assert moved_size == cat_size
    moved_data = image[(moved_sector - 1) * SECTOR_SIZE :][:moved_size]
    cat_data = image[(cat_sector - 1) * SECTOR_SIZE :][:cat_size]
    assert moved_data == cat_data, "moved data mismatch"


def test_make_directory_high_sector(*, directory_sector: int, directory_sectors: int, temporary_directory: Path) -> None:
    """Verify mkdir allocates a 16-bit sector past sector 255.

    asm.asm pushes the next free sector well beyond 256.
    """
    drive = make_drive(name="make_directory_high", temporary_directory=temporary_directory)
    run_commands(["mkdir hi"], command_timeout=COMMAND_TIMEOUT, drive=drive)
    image = drive.read_bytes()
    hi = find_entry(
        directory_sectors=directory_sectors,
        directory_start_sector=directory_sector,
        image=image,
        name="hi",
    )
    assert hi is not None, "hi/ not created"
    flags, hi_sector, _ = hi
    assert flags & FLAG_DIRECTORY, "hi is not a directory"
    assert hi_sector > 255, f"expected sector > 255, got {hi_sector}"
    # The two-sector directory must be zero-filled.
    subdirectory_data = image[(hi_sector - 1) * SECTOR_SIZE :][: directory_sectors * SECTOR_SIZE]
    assert subdirectory_data == b"\x00" * len(subdirectory_data), "subdirectory not zero-filled"


def test_second_directory_sector(*, directory_sector: int, directory_sectors: int, temporary_directory: Path) -> None:
    """Exercise lookup, copy, and rename of an entry in the second directory sector.

    bin/ holds enough programs that shell lands in the second sector;
    cp it to bin/s, then mv bin/s to bin/s2.
    """
    drive = make_drive(name="second", temporary_directory=temporary_directory)
    image = drive.read_bytes()
    bin_entry = find_entry(
        directory_sectors=directory_sectors,
        directory_start_sector=directory_sector,
        image=image,
        name="bin",
    )
    assert bin_entry is not None
    assert bin_entry[0] & FLAG_DIRECTORY
    # Verify shell is in the SECOND directory sector (not the first).
    assert (
        find_entry(
            directory_sectors=1,
            directory_start_sector=bin_entry[1],
            image=image,
            name="shell",
        )
        is None
    ), "shell unexpectedly in first bin sector"

    run_commands(
        ["cp bin/shell bin/s", "mv bin/s bin/s2"],
        command_timeout=COMMAND_TIMEOUT,
        drive=drive,
    )
    image = drive.read_bytes()
    bin_entry = find_entry(
        directory_sectors=directory_sectors,
        directory_start_sector=directory_sector,
        image=image,
        name="bin",
    )
    s2 = find_entry(
        directory_sectors=directory_sectors,
        directory_start_sector=bin_entry[1],
        image=image,
        name="s2",
    )
    assert s2 is not None, "bin/s2 not found after rename"
    original = find_entry(
        directory_sectors=directory_sectors,
        directory_start_sector=bin_entry[1],
        image=image,
        name="shell",
    )
    assert original is not None
    _, s2_sector, s2_size = s2
    _, original_sector, original_size = original
    assert s2_size == original_size
    s2_data = image[(s2_sector - 1) * SECTOR_SIZE :][:s2_size]
    original_data = image[(original_sector - 1) * SECTOR_SIZE :][:original_size]
    assert s2_data == original_data, "copy data mismatch"


if __name__ == "__main__":
    sys.exit(main())
