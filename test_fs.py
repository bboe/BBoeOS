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
import contextlib
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from add_file import (
    NAME_FIELD,
    OFFSET_FLAGS,
    OFFSET_SECTOR,
    OFFSET_SIZE,
    SECTOR_SIZE,
    iter_entries,
    read_assign,
)
from test_asm import (
    BOOT_TIMEOUT,
    PROMPT,
    SERIAL_BASENAME,
    cleanup_fifos,
    setup_fifos,
    terminate,
    wait_for_bytes,
)

COMMAND_TIMEOUT = 30
DIRECTORY_ENTRY_SIZE = 32
FLAG_DIRECTORY = 0x02
IMAGE = Path("drive.img")


def boot_and_run(*, commands: list[str], drive: Path, temporary_directory: Path) -> None:
    """Boot QEMU on `drive`, send each command, and shut down."""
    setup_fifos(temporary_directory=temporary_directory)
    serial_base = temporary_directory / SERIAL_BASENAME
    qemu: subprocess.Popen | None = None
    serial_file_descriptor: int | None = None
    try:
        qemu = subprocess.Popen(
            [
                "qemu-system-i386",
                "-chardev",
                f"pipe,id=s,path={serial_base}",
                "-display",
                "none",
                "-drive",
                f"file={drive},format=raw",
                "-monitor",
                "none",
                "-serial",
                "chardev:s",
            ],
        )
        serial_file_descriptor = os.open(f"{serial_base}.out", os.O_RDONLY | os.O_NONBLOCK)
        wait_for_bytes(file_descriptor=serial_file_descriptor, needle=PROMPT, process=qemu, timeout=BOOT_TIMEOUT)
        with Path(f"{serial_base}.in").open("w", encoding="utf-8") as serial_input:
            for command in commands:
                serial_input.write(command + "\r")
                serial_input.flush()
                wait_for_bytes(file_descriptor=serial_file_descriptor, needle=PROMPT, process=qemu, timeout=COMMAND_TIMEOUT)
            serial_input.write("shutdown\r")
            serial_input.flush()
        # Wait for QEMU to exit (shutdown takes a moment)
        with contextlib.suppress(subprocess.TimeoutExpired):
            qemu.wait(timeout=10)
    finally:
        if serial_file_descriptor is not None:
            os.close(serial_file_descriptor)
        if qemu is not None:
            terminate(process=qemu)
        cleanup_fifos(temporary_directory=temporary_directory)


def find_entry(
    *,
    directory_sectors: int,
    directory_start_sector: int,
    image: bytes,
    name: str,
) -> tuple[int, int, int] | None:
    """Return (flags, start_sector, size) for `name` in the directory.

    Search the directory starting at `directory_start_sector`, or return None
    if not found.
    """
    base = (directory_start_sector - 1) * SECTOR_SIZE
    target = name.encode()
    for entry_offset in iter_entries(base_offset=base, sector_count=directory_sectors):
        if image[entry_offset] == 0:
            continue
        entry_name = bytes(image[entry_offset : entry_offset + NAME_FIELD]).rstrip(b"\x00")
        if entry_name != target:
            continue
        flags = image[entry_offset + OFFSET_FLAGS]
        sector = struct.unpack_from("<H", image, entry_offset + OFFSET_SECTOR)[0]
        size = struct.unpack_from("<I", image, entry_offset + OFFSET_SIZE)[0]
        return (flags, sector, size)
    return None


def make_drive(*, name: str, temporary_directory: Path) -> Path:
    """Create a copy of the base drive image for a test case."""
    drive = temporary_directory / f"drive_{name}.img"
    shutil.copy(IMAGE, drive)
    return drive


def main() -> int:
    """Run the filesystem regression test suite."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("test", nargs="?", help="run only the named test")
    arguments = parser.parse_args()

    print("Building OS...")
    subprocess.run(
        ["./make_os.sh"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    directory_sector = read_assign("DIR_SECTOR")
    directory_sectors = read_assign("DIR_SECTORS")

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


def test_copy_large(*, directory_sector: int, directory_sectors: int, temporary_directory: Path) -> None:
    """Copy src/asm.asm (>64 KB, sector >255) to a new root file.

    Verify the destination is byte-identical to the source.
    """
    drive = make_drive(name="copy_large", temporary_directory=temporary_directory)
    boot_and_run(commands=["cp src/asm.asm big"], drive=drive, temporary_directory=temporary_directory)
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
    boot_and_run(
        commands=["mkdir d", "cp src/hello.asm d/h"],
        drive=drive,
        temporary_directory=temporary_directory,
    )
    image = drive.read_bytes()

    d_entry = find_entry(
        directory_sectors=directory_sectors,
        directory_start_sector=directory_sector,
        image=image,
        name="d",
    )
    assert d_entry is not None and d_entry[0] & FLAG_DIRECTORY, "d/ not created"
    h_entry = find_entry(
        directory_sectors=directory_sectors,
        directory_start_sector=d_entry[1],
        image=image,
        name="h",
    )
    assert h_entry is not None, "d/h not found"
    _, h_sector, h_size = h_entry

    source_entry = find_entry(
        directory_sectors=directory_sectors,
        directory_start_sector=directory_sector,
        image=image,
        name="src",
    )
    hello = find_entry(
        directory_sectors=directory_sectors,
        directory_start_sector=source_entry[1],
        image=image,
        name="hello.asm",
    )
    _, hello_sector, hello_size = hello
    assert h_size == hello_size
    h_data = image[(h_sector - 1) * SECTOR_SIZE :][:h_size]
    hello_data = image[(hello_sector - 1) * SECTOR_SIZE :][:hello_size]
    assert h_data == hello_data, "subdirectory copy data mismatch"


def test_cross_directory_move(*, directory_sector: int, directory_sectors: int, temporary_directory: Path) -> None:
    """Copy a file into root, then mv it into bin/.

    Verify the source entry is gone, the dest entry exists in bin/, and
    the data is preserved.
    """
    drive = make_drive(name="cross_move", temporary_directory=temporary_directory)
    boot_and_run(
        commands=["cp src/hello.asm a.txt", "mv a.txt bin/a.txt"],
        drive=drive,
        temporary_directory=temporary_directory,
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

    source_entry = find_entry(
        directory_sectors=directory_sectors,
        directory_start_sector=directory_sector,
        image=image,
        name="src",
    )
    hello = find_entry(
        directory_sectors=directory_sectors,
        directory_start_sector=source_entry[1],
        image=image,
        name="hello.asm",
    )
    _, hello_sector, hello_size = hello
    assert moved_size == hello_size
    moved_data = image[(moved_sector - 1) * SECTOR_SIZE :][:moved_size]
    hello_data = image[(hello_sector - 1) * SECTOR_SIZE :][:hello_size]
    assert moved_data == hello_data, "moved data mismatch"


def test_make_directory_high_sector(*, directory_sector: int, directory_sectors: int, temporary_directory: Path) -> None:
    """Verify mkdir allocates a 16-bit sector past sector 255.

    asm.asm pushes the next free sector well beyond 256.
    """
    drive = make_drive(name="make_directory_high", temporary_directory=temporary_directory)
    boot_and_run(commands=["mkdir hi"], drive=drive, temporary_directory=temporary_directory)
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

    src/ holds enough programs that uptime.asm is in the second sector;
    cp it back to src/u, then mv src/u to src/u2.
    """
    drive = make_drive(name="second", temporary_directory=temporary_directory)
    image = drive.read_bytes()
    source_entry = find_entry(
        directory_sectors=directory_sectors,
        directory_start_sector=directory_sector,
        image=image,
        name="src",
    )
    assert source_entry is not None
    assert source_entry[0] & FLAG_DIRECTORY
    source_directory_sector = source_entry[1]
    # Verify uptime.asm is in the SECOND directory sector.
    first_sector = image[(source_directory_sector - 1) * SECTOR_SIZE : source_directory_sector * SECTOR_SIZE]
    target = b"uptime.asm"
    in_first = any(
        bytes(
            first_sector[i * DIRECTORY_ENTRY_SIZE : i * DIRECTORY_ENTRY_SIZE + NAME_FIELD],
        ).rstrip(b"\x00")
        == target
        for i in range(SECTOR_SIZE // DIRECTORY_ENTRY_SIZE)
    )
    assert not in_first, "uptime.asm unexpectedly in first src sector"

    boot_and_run(
        commands=["cp src/uptime.asm src/u", "mv src/u src/u2"],
        drive=drive,
        temporary_directory=temporary_directory,
    )
    image = drive.read_bytes()
    source_entry = find_entry(
        directory_sectors=directory_sectors,
        directory_start_sector=directory_sector,
        image=image,
        name="src",
    )
    u2 = find_entry(
        directory_sectors=directory_sectors,
        directory_start_sector=source_entry[1],
        image=image,
        name="u2",
    )
    assert u2 is not None, "src/u2 not found after rename"
    u_orig = find_entry(
        directory_sectors=directory_sectors,
        directory_start_sector=source_entry[1],
        image=image,
        name="uptime.asm",
    )
    assert u_orig is not None
    _, u2_sector, u2_size = u2
    _, original_sector, original_size = u_orig
    assert u2_size == original_size
    u2_data = image[(u2_sector - 1) * SECTOR_SIZE :][:u2_size]
    original_data = image[(original_sector - 1) * SECTOR_SIZE :][:original_size]
    assert u2_data == original_data, "copy data mismatch"


if __name__ == "__main__":
    sys.exit(main())
