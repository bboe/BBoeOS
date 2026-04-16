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
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from add_file import (
    NAME_FIELD,
    OFFSET_SECTOR,
    OFFSET_SIZE,
    SECTOR_SIZE,
    iter_entries,
    read_assign,
)
from run_qemu import run_commands

C_DIR = Path("src/c")
IMAGE = Path("drive.img")
ORG_DIRECTIVE = "org 0600h"
STATIC_DIR = Path("static")


def _run_tests(*, arguments: argparse.Namespace) -> int:
    """Execute the test loop: build OS, discover programs, compare outputs."""
    print("Building OS...")
    subprocess.run(
        ["./make_os.sh"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    programs = discover_programs(only=arguments.program)
    if not programs:
        if arguments.program:
            print(f"No program named '{arguments.program}' in static/")
        else:
            print("No programs found in static/")
        return 1

    print("Programs to test:", " ".join(p.name for p in programs))
    print()

    directory_sector = read_assign("DIRECTORY_SECTOR")
    directory_sectors = read_assign("DIRECTORY_SECTORS")
    keep_artifacts = arguments.program is not None

    with tempfile.TemporaryDirectory(prefix="test_asm_") as temporary_path:
        temporary_directory = Path(temporary_path)
        references: dict[str, Path] = {}
        for source in programs:
            name = source.stem
            reference = temporary_directory / f"ref_{name}.bin"
            subprocess.run(
                ["nasm", "-f", "bin", "-o", str(reference), str(source), "-I", "static/"],
                check=True,
            )
            references[name] = reference

        pass_count = 0
        fail_count = 0
        failed: list[str] = []
        for source in programs:
            name = source.stem
            reference = references[name]
            output_binary = temporary_directory / f"out_{name}.bin"
            started = time.monotonic()
            ok, message = test_program(
                directory_sector=directory_sector,
                directory_sectors=directory_sectors,
                name=name,
                output_binary=output_binary,
                reference=reference,
                temporary_directory=temporary_directory,
            )
            elapsed = time.monotonic() - started
            label = f"{name}.asm"
            if ok:
                print(f"  PASS  {label:<20} {reference.stat().st_size:>6} bytes  {elapsed:6.2f}s")
                pass_count += 1
            else:
                print(f"  FAIL  {label:<20} {message}  {elapsed:6.2f}s")
                fail_count += 1
                failed.append(label)

        persisted: Path | None = None
        if keep_artifacts:
            persisted = persist_artifacts(temporary_directory=temporary_directory)

    print()
    print(f"{pass_count} passed, {fail_count} failed")
    if fail_count:
        print("Failed:", " ".join(failed))
    if persisted is not None:
        print(f"Artifacts kept in: {persisted}")
    return 1 if fail_count else 0


def compare_drive_output(
    *,
    directory_sector: int,
    directory_sectors: int,
    drive: Path,
    output_binary: Path,
    output_name: str,
    reference_bytes: bytes,
) -> tuple[bool, str]:
    """Extract the assembled output from the drive image and compare to the NASM reference."""
    image = bytearray(drive.read_bytes())
    base = (directory_sector - 1) * SECTOR_SIZE
    for entry_offset in iter_entries(base_offset=base, sector_count=directory_sectors):
        if image[entry_offset] == 0:
            continue
        entry_name = bytes(image[entry_offset : entry_offset + NAME_FIELD]).rstrip(b"\x00").decode(errors="replace")
        if entry_name != output_name:
            continue
        start_sector = struct.unpack_from("<H", image, entry_offset + OFFSET_SECTOR)[0]
        size = struct.unpack_from("<I", image, entry_offset + OFFSET_SIZE)[0]
        data_offset = (start_sector - 1) * SECTOR_SIZE
        output_data = bytes(image[data_offset : data_offset + size])
        output_binary.write_bytes(output_data)
        if output_data == reference_bytes:
            return True, ""
        return False, f"expected {len(reference_bytes)} bytes, got {size} bytes"
    return False, "output file not found on drive"


def compile_c_sources() -> list[Path]:
    """Compile each src/c/*.c to static/<name>.asm via cc.py.

    Skips C sources whose corresponding .asm already exists in static/
    (i.e. a hand-written version is the source of truth).  Returns the
    list of generated paths so they can be cleaned up after testing.
    """
    if not C_DIR.is_dir():
        return []
    generated: list[Path] = []
    for c_source in sorted(C_DIR.glob("*.c")):
        name = c_source.stem
        target = STATIC_DIR / f"{name}.asm"
        if target.exists():
            continue
        subprocess.run(
            ["./cc.py", str(c_source), str(target)],
            check=True,
        )
        generated.append(target)
    return generated


def discover_programs(*, only: str | None) -> list[Path]:
    """Return the list of static/*.asm programs that target PROGRAM_BASE."""
    programs: list[Path] = []
    for source in sorted(STATIC_DIR.glob("*.asm")):
        if ORG_DIRECTIVE not in source.read_text(errors="replace"):
            continue
        name = source.stem
        if only is not None:
            if name == only:
                programs.append(source)
            continue
        programs.append(source)
    return programs


def main() -> int:
    """Run the self-hosted assembler test suite."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "program",
        nargs="?",
        help="restrict the test to one program (e.g. 'edit')",
    )
    arguments = parser.parse_args()

    # Compile C sources to .asm and place in static/ so make_os.sh
    # includes them on the disk image alongside hand-written .asm files.
    generated = compile_c_sources()
    if generated:
        c_names = " ".join(path.stem + ".c" for path in generated)
        print(f"Compiled C sources: {c_names}")

    try:
        return _run_tests(arguments=arguments)
    finally:
        restore_static(generated)


def persist_artifacts(*, temporary_directory: Path) -> Path:
    """Copy artifacts out of `temporary_directory` to a persistent directory."""
    persist = Path(tempfile.mkdtemp(prefix="test_asm_keep_"))
    for item in temporary_directory.iterdir():
        if not item.is_file():
            continue
        shutil.copy(item, persist / item.name)
    return persist


def restore_static(generated: list[Path], /) -> None:
    """Undo compile_c_sources(): delete generated files."""
    for target in generated:
        target.unlink(missing_ok=True)


def test_program(
    *,
    directory_sector: int,
    directory_sectors: int,
    name: str,
    output_binary: Path,
    reference: Path,
    temporary_directory: Path,
) -> tuple[bool, str]:
    """Assemble a single program in QEMU and compare the output to the NASM reference."""
    output_name = f"{name}_t"
    drive = temporary_directory / f"drive_{name}.img"
    shutil.copy(IMAGE, drive)

    run_commands(
        [f"asm src/{name}.asm {output_name}"],
        drive=drive,
    )
    return compare_drive_output(
        directory_sector=directory_sector,
        directory_sectors=directory_sectors,
        drive=drive,
        output_binary=output_binary,
        output_name=output_name,
        reference_bytes=reference.read_bytes(),
    )


if __name__ == "__main__":
    sys.exit(main())
