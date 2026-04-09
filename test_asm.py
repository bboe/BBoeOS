#!/usr/bin/env python3
"""Test that the self-hosted assembler produces byte-identical output to NASM.

With no argument, tests every program in static/ that has `org 0600h`.
With a name (e.g. ./test_asm.py edit), tests only that one program. On
single-program runs the artifacts (nasm reference binary, extracted output,
drive image) are copied to a persistent temp directory so they can be
inspected after a failure; its path is printed at the end.

Requires: nasm, qemu-system-i386
"""

from __future__ import annotations

import argparse
import os
import select
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from add_file import (
    NAME_FIELD,
    OFF_SECTOR,
    OFF_SIZE,
    SECTOR_SIZE,
    iter_entries,
    read_assign,
)

BOOT_TIMEOUT = 30
CMD_TIMEOUT = 8
IMAGE = Path("drive.img")
ORG_DIRECTIVE = "org 0600h"
PROMPT = b"$ "
SER_BASENAME = "ser"
STATIC_DIR = Path("static")


def cleanup_fifos(*, tempdir: Path) -> None:
    for path in fifo_paths(tempdir=tempdir):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def compare_drive_output(
    *,
    dir_sector: int,
    dir_sectors: int,
    drive: Path,
    out_bin: Path,
    out_name: str,
    ref_bytes: bytes,
) -> tuple[bool, str]:
    image = bytearray(drive.read_bytes())
    base = (dir_sector - 1) * SECTOR_SIZE
    for entry_off in iter_entries(image, base, dir_sectors):
        if image[entry_off] == 0:
            continue
        entry_name = (
            bytes(image[entry_off : entry_off + NAME_FIELD])
            .rstrip(b"\x00")
            .decode(errors="replace")
        )
        if entry_name != out_name:
            continue
        start_sec = struct.unpack_from("<H", image, entry_off + OFF_SECTOR)[0]
        size = struct.unpack_from("<I", image, entry_off + OFF_SIZE)[0]
        data_off = (start_sec - 1) * SECTOR_SIZE
        out_data = bytes(image[data_off : data_off + size])
        out_bin.write_bytes(out_data)
        if out_data == ref_bytes:
            return True, ""
        return False, f"expected {len(ref_bytes)} bytes, got {size} bytes"
    return False, "output file not found on drive"


def discover_programs(*, only: str | None) -> list[Path]:
    programs: list[Path] = []
    for src in sorted(STATIC_DIR.glob("*.asm")):
        if ORG_DIRECTIVE not in src.read_text(errors="replace"):
            continue
        name = src.stem
        if only is not None:
            if name == only:
                programs.append(src)
            continue
        programs.append(src)
    return programs


def fifo_paths(*, tempdir: Path) -> tuple[Path, Path]:
    return (
        tempdir / f"{SER_BASENAME}.in",
        tempdir / f"{SER_BASENAME}.out",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "program",
        nargs="?",
        help="restrict the test to one program (e.g. 'edit')",
    )
    args = parser.parse_args()

    print("Building OS...")
    subprocess.run(
        ["./make_os.sh"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    programs = discover_programs(only=args.program)
    if not programs:
        if args.program:
            print(f"No program named '{args.program}' in static/")
        else:
            print("No programs found in static/")
        return 1

    print("Programs to test:", " ".join(p.name for p in programs))
    print()

    dir_sector = read_assign("DIR_SECTOR")
    dir_sectors = read_assign("DIR_SECTORS")
    keep_artifacts = args.program is not None

    with tempfile.TemporaryDirectory(prefix="test_asm_") as tmp:
        tempdir = Path(tmp)
        refs: dict[str, Path] = {}
        for src in programs:
            name = src.stem
            ref = tempdir / f"ref_{name}.bin"
            subprocess.run(
                ["nasm", "-f", "bin", "-o", str(ref), str(src), "-I", "static/"],
                check=True,
            )
            refs[name] = ref

        pass_count = 0
        fail_count = 0
        failed: list[str] = []
        for src in programs:
            name = src.stem
            ref = refs[name]
            out_bin = tempdir / f"out_{name}.bin"
            started = time.monotonic()
            ok, message = test_program(
                dir_sector=dir_sector,
                dir_sectors=dir_sectors,
                name=name,
                out_bin=out_bin,
                ref=ref,
                tempdir=tempdir,
            )
            elapsed = time.monotonic() - started
            label = f"{name}.asm"
            if ok:
                print(
                    f"  PASS  {label:<20} {ref.stat().st_size:>6} bytes"
                    f"  {elapsed:6.2f}s"
                )
                pass_count += 1
            else:
                print(f"  FAIL  {label:<20} {message}  {elapsed:6.2f}s")
                fail_count += 1
                failed.append(label)

        persisted: Path | None = None
        if keep_artifacts:
            persisted = persist_artifacts(tempdir=tempdir)

    print()
    print(f"{pass_count} passed, {fail_count} failed")
    if fail_count:
        print("Failed:", " ".join(failed))
    if persisted is not None:
        print(f"Artifacts kept in: {persisted}")
    return 1 if fail_count else 0


def persist_artifacts(*, tempdir: Path) -> Path:
    """Copy non-fifo artifacts out of `tempdir` to a persistent directory."""
    persist = Path(tempfile.mkdtemp(prefix="test_asm_keep_"))
    fifos = set(fifo_paths(tempdir=tempdir))
    for item in tempdir.iterdir():
        if item in fifos or not item.is_file():
            continue
        shutil.copy(item, persist / item.name)
    return persist


def run_in_qemu(
    *,
    cmd_timeout: float,
    command: str,
    drive: Path,
    tempdir: Path,
) -> None:
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
            f.write(command)
        wait_for_bytes(fd=ser_fd, needle=PROMPT, proc=qemu, timeout=cmd_timeout)
    finally:
        if ser_fd is not None:
            os.close(ser_fd)
        if qemu is not None:
            terminate(proc=qemu)
        cleanup_fifos(tempdir=tempdir)


def setup_fifos(*, tempdir: Path) -> None:
    cleanup_fifos(tempdir=tempdir)
    for path in fifo_paths(tempdir=tempdir):
        os.mkfifo(path)


def terminate(*, proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def test_program(
    *,
    dir_sector: int,
    dir_sectors: int,
    name: str,
    out_bin: Path,
    ref: Path,
    tempdir: Path,
) -> tuple[bool, str]:
    out_name = f"{name}_t"
    drive = tempdir / f"drive_{name}.img"
    shutil.copy(IMAGE, drive)

    run_in_qemu(
        cmd_timeout=CMD_TIMEOUT,
        command=f"asm src/{name}.asm {out_name}\r",
        drive=drive,
        tempdir=tempdir,
    )
    return compare_drive_output(
        dir_sector=dir_sector,
        dir_sectors=dir_sectors,
        drive=drive,
        out_bin=out_bin,
        out_name=out_name,
        ref_bytes=ref.read_bytes(),
    )


def wait_for_bytes(
    *,
    fd: int,
    needle: bytes,
    proc: subprocess.Popen,
    timeout: float,
) -> None:
    """Read from `fd` until `needle` appears in the accumulated output.

    `proc` is the QEMU process; if it exits before `needle` is seen, raise
    RuntimeError. Raises TimeoutError if `timeout` seconds elapse.
    """
    deadline = time.monotonic() + timeout
    buf = bytearray()
    while needle not in buf:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"timed out waiting for {needle!r}")
        if proc.poll() is not None:
            raise RuntimeError(
                f"qemu exited with {proc.returncode} before {needle!r} appeared"
            )
        ready, _, _ = select.select([fd], [], [], min(remaining, 0.1))
        if not ready:
            continue
        try:
            chunk = os.read(fd, 4096)
        except BlockingIOError:
            continue
        if not chunk:
            # No writer attached yet, or transient empty read — back off briefly.
            time.sleep(0.01)
            continue
        buf.extend(chunk)


if __name__ == "__main__":
    sys.exit(main())
