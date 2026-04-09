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
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from add_file import (
    NAME_FIELD,
    OFF_FLAGS,
    OFF_SECTOR,
    OFF_SIZE,
    SECTOR_SIZE,
    iter_entries,
    read_assign,
)

from test_asm import (
    BOOT_TIMEOUT,
    PROMPT,
    SER_BASENAME,
    cleanup_fifos,
    setup_fifos,
    terminate,
    wait_for_bytes,
)

CMD_TIMEOUT = 30
DIR_ENTRY_SIZE = 32
FLAG_DIR = 0x02
IMAGE = Path("drive.img")


def boot_and_run(*, commands: list[str], drive: Path, tempdir: Path) -> None:
    """Boot QEMU on `drive`, send each command, wait for the prompt after
    each, then shut down."""
    setup_fifos(tempdir=tempdir)
    ser_base = tempdir / SER_BASENAME
    qemu: subprocess.Popen | None = None
    ser_fd: int | None = None
    try:
        qemu = subprocess.Popen(
            [
                "qemu-system-i386",
                "-chardev",
                f"pipe,id=s,path={ser_base}",
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
        ser_fd = os.open(f"{ser_base}.out", os.O_RDONLY | os.O_NONBLOCK)
        wait_for_bytes(fd=ser_fd, needle=PROMPT, proc=qemu, timeout=BOOT_TIMEOUT)
        with open(f"{ser_base}.in", "w") as f:
            for cmd in commands:
                f.write(cmd + "\r")
                f.flush()
                wait_for_bytes(fd=ser_fd, needle=PROMPT, proc=qemu, timeout=CMD_TIMEOUT)
            f.write("shutdown\r")
            f.flush()
        # Wait for QEMU to exit (shutdown takes a moment)
        try:
            qemu.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
    finally:
        if ser_fd is not None:
            os.close(ser_fd)
        if qemu is not None:
            terminate(proc=qemu)
        cleanup_fifos(tempdir=tempdir)


def find_entry(
    *, image: bytes, dir_start_sector: int, dir_sectors: int, name: str
) -> tuple[int, int, int] | None:
    """Return (flags, start_sector, size) for `name` in the directory
    starting at `dir_start_sector`, or None if not found."""
    base = (dir_start_sector - 1) * SECTOR_SIZE
    target = name.encode()
    for entry_off in iter_entries(image, base, dir_sectors):
        if image[entry_off] == 0:
            continue
        entry_name = bytes(image[entry_off : entry_off + NAME_FIELD]).rstrip(b"\x00")
        if entry_name != target:
            continue
        flags = image[entry_off + OFF_FLAGS]
        sec = struct.unpack_from("<H", image, entry_off + OFF_SECTOR)[0]
        size = struct.unpack_from("<I", image, entry_off + OFF_SIZE)[0]
        return (flags, sec, size)
    return None


def make_drive(*, tempdir: Path, name: str) -> Path:
    drive = tempdir / f"drive_{name}.img"
    shutil.copy(IMAGE, drive)
    return drive


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("test", nargs="?", help="run only the named test")
    args = parser.parse_args()

    print("Building OS...")
    subprocess.run(
        ["./make_os.sh"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    dir_sector = read_assign("DIR_SECTOR")
    dir_sectors = read_assign("DIR_SECTORS")

    tests = [
        ("cp_large", test_cp_large),
        ("cp_to_subdir", test_cp_to_subdir),
        ("cross_dir_mv", test_cross_dir_mv),
        ("mkdir_high_sector", test_mkdir_high_sector),
        ("second_dir_sector", test_second_dir_sector),
    ]
    if args.test:
        tests = [t for t in tests if t[0] == args.test]
        if not tests:
            print(f"No test named '{args.test}'")
            return 1

    pass_count = 0
    fail_count = 0
    failed: list[str] = []
    with tempfile.TemporaryDirectory(prefix="test_fs_") as tmp:
        tempdir = Path(tmp)
        for name, fn in tests:
            started = time.monotonic()
            try:
                fn(tempdir=tempdir, dir_sector=dir_sector, dir_sectors=dir_sectors)
                ok, message = True, ""
            except AssertionError as e:
                ok, message = False, str(e)
            except Exception as e:
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


def test_cp_large(*, tempdir: Path, dir_sector: int, dir_sectors: int) -> None:
    """Copy src/asm.asm (>64 KB, sector >255) to a new root file and verify
    the destination is byte-identical to the source."""
    drive = make_drive(tempdir=tempdir, name="cp_large")
    boot_and_run(commands=["cp src/asm.asm big"], drive=drive, tempdir=tempdir)
    image = drive.read_bytes()

    big = find_entry(
        image=image, dir_start_sector=dir_sector, dir_sectors=dir_sectors, name="big"
    )
    assert big is not None, "big not found in root"
    flags, big_sec, big_size = big
    assert big_size > 65535, f"expected size > 64 KB, got {big_size}"
    assert big_sec > 255, f"expected sector > 255, got {big_sec}"

    src_entry = find_entry(
        image=image, dir_start_sector=dir_sector, dir_sectors=dir_sectors, name="src"
    )
    assert src_entry is not None and src_entry[0] & FLAG_DIR
    asm = find_entry(
        image=image,
        dir_start_sector=src_entry[1],
        dir_sectors=dir_sectors,
        name="asm.asm",
    )
    assert asm is not None, "src/asm.asm not found"
    _, src_sec, src_size = asm

    big_data = image[(big_sec - 1) * SECTOR_SIZE :][:big_size]
    src_data = image[(src_sec - 1) * SECTOR_SIZE :][:src_size]
    assert big_size == src_size, f"size {big_size} != {src_size}"
    assert big_data == src_data, "copied data does not match source"


def test_cp_to_subdir(*, tempdir: Path, dir_sector: int, dir_sectors: int) -> None:
    """Copy a file into a subdirectory and verify the entry shows up there."""
    drive = make_drive(tempdir=tempdir, name="cp_subdir")
    boot_and_run(
        commands=["mkdir d", "cp src/hello.asm d/h"], drive=drive, tempdir=tempdir
    )
    image = drive.read_bytes()

    d = find_entry(
        image=image, dir_start_sector=dir_sector, dir_sectors=dir_sectors, name="d"
    )
    assert d is not None and d[0] & FLAG_DIR, "d/ not created"
    h = find_entry(
        image=image, dir_start_sector=d[1], dir_sectors=dir_sectors, name="h"
    )
    assert h is not None, "d/h not found"
    _, h_sec, h_size = h

    src_entry = find_entry(
        image=image, dir_start_sector=dir_sector, dir_sectors=dir_sectors, name="src"
    )
    hello = find_entry(
        image=image,
        dir_start_sector=src_entry[1],
        dir_sectors=dir_sectors,
        name="hello.asm",
    )
    _, hello_sec, hello_size = hello
    assert h_size == hello_size
    h_data = image[(h_sec - 1) * SECTOR_SIZE :][:h_size]
    hello_data = image[(hello_sec - 1) * SECTOR_SIZE :][:hello_size]
    assert h_data == hello_data, "subdir copy data mismatch"


def test_cross_dir_mv(*, tempdir: Path, dir_sector: int, dir_sectors: int) -> None:
    """Copy a file into root, then mv it into bin/. Verify the source entry
    is gone, the dest entry exists in bin/, and the data is preserved."""
    drive = make_drive(tempdir=tempdir, name="cross_mv")
    boot_and_run(
        commands=["cp src/hello.asm a.txt", "mv a.txt bin/a.txt"],
        drive=drive,
        tempdir=tempdir,
    )
    image = drive.read_bytes()

    assert (
        find_entry(
            image=image,
            dir_start_sector=dir_sector,
            dir_sectors=dir_sectors,
            name="a.txt",
        )
        is None
    ), "a.txt still in root after mv"

    bin_entry = find_entry(
        image=image, dir_start_sector=dir_sector, dir_sectors=dir_sectors, name="bin"
    )
    assert bin_entry is not None and bin_entry[0] & FLAG_DIR
    moved = find_entry(
        image=image,
        dir_start_sector=bin_entry[1],
        dir_sectors=dir_sectors,
        name="a.txt",
    )
    assert moved is not None, "bin/a.txt not found after mv"
    _, moved_sec, moved_size = moved

    src_entry = find_entry(
        image=image, dir_start_sector=dir_sector, dir_sectors=dir_sectors, name="src"
    )
    hello = find_entry(
        image=image,
        dir_start_sector=src_entry[1],
        dir_sectors=dir_sectors,
        name="hello.asm",
    )
    _, hello_sec, hello_size = hello
    assert moved_size == hello_size
    moved_data = image[(moved_sec - 1) * SECTOR_SIZE :][:moved_size]
    hello_data = image[(hello_sec - 1) * SECTOR_SIZE :][:hello_size]
    assert moved_data == hello_data, "moved data mismatch"


def test_mkdir_high_sector(*, tempdir: Path, dir_sector: int, dir_sectors: int) -> None:
    """Verify mkdir allocates a 16-bit sector when the disk is already full
    past sector 255 (asm.asm pushes the next free sector well beyond 256)."""
    drive = make_drive(tempdir=tempdir, name="mkdir_hi")
    boot_and_run(commands=["mkdir hi"], drive=drive, tempdir=tempdir)
    image = drive.read_bytes()
    hi = find_entry(
        image=image, dir_start_sector=dir_sector, dir_sectors=dir_sectors, name="hi"
    )
    assert hi is not None, "hi/ not created"
    flags, hi_sec, _ = hi
    assert flags & FLAG_DIR, "hi is not a directory"
    assert hi_sec > 255, f"expected sector > 255, got {hi_sec}"
    # The two-sector directory must be zero-filled.
    sub = image[(hi_sec - 1) * SECTOR_SIZE :][: dir_sectors * SECTOR_SIZE]
    assert sub == b"\x00" * len(sub), "subdir not zero-filled"


def test_second_dir_sector(*, tempdir: Path, dir_sector: int, dir_sectors: int) -> None:
    """Exercise lookup, copy, and rename of an entry that lives in the
    second sector of a subdirectory.

    src/ holds enough programs that uptime.asm is in the second sector;
    cp it back to src/u, then mv src/u to src/u2."""
    drive = make_drive(tempdir=tempdir, name="second")
    image = drive.read_bytes()
    src_entry = find_entry(
        image=image, dir_start_sector=dir_sector, dir_sectors=dir_sectors, name="src"
    )
    assert src_entry is not None and src_entry[0] & FLAG_DIR
    src_dir_sec = src_entry[1]
    # Verify uptime.asm is in the SECOND directory sector.
    first_sector = image[(src_dir_sec - 1) * SECTOR_SIZE : src_dir_sec * SECTOR_SIZE]
    target = b"uptime.asm"
    in_first = any(
        bytes(
            first_sector[i * DIR_ENTRY_SIZE : i * DIR_ENTRY_SIZE + NAME_FIELD]
        ).rstrip(b"\x00")
        == target
        for i in range(SECTOR_SIZE // DIR_ENTRY_SIZE)
    )
    assert not in_first, "uptime.asm unexpectedly in first src sector"

    boot_and_run(
        commands=["cp src/uptime.asm src/u", "mv src/u src/u2"],
        drive=drive,
        tempdir=tempdir,
    )
    image = drive.read_bytes()
    src_entry = find_entry(
        image=image, dir_start_sector=dir_sector, dir_sectors=dir_sectors, name="src"
    )
    u2 = find_entry(
        image=image, dir_start_sector=src_entry[1], dir_sectors=dir_sectors, name="u2"
    )
    assert u2 is not None, "src/u2 not found after rename"
    u_orig = find_entry(
        image=image,
        dir_start_sector=src_entry[1],
        dir_sectors=dir_sectors,
        name="uptime.asm",
    )
    assert u_orig is not None
    _, u2_sec, u2_size = u2
    _, orig_sec, orig_size = u_orig
    assert u2_size == orig_size
    u2_data = image[(u2_sec - 1) * SECTOR_SIZE :][:u2_size]
    orig_data = image[(orig_sec - 1) * SECTOR_SIZE :][:orig_size]
    assert u2_data == orig_data, "copy data mismatch"


if __name__ == "__main__":
    sys.exit(main())
