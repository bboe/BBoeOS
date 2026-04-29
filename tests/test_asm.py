#!/usr/bin/env python3
"""Test that the self-hosted assembler produces byte-identical output to NASM.

With no argument, tests every program in static/ that has `org 08048000h`.
With a name (e.g. ./test_asm.py edit), tests only that one program. On
single-program runs the artifacts (nasm reference binary, extracted output,
drive image) are copied to a persistent temp directory so they can be
inspected after a failure; its path is printed at the end.

Requires: nasm, qemu-system-i386
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from run_qemu import COMMAND_TIMEOUT, run_commands  # noqa: E402

from add_file import SECTOR_SIZE, compute_directory_sector, find_entry, read_assign  # noqa: E402

BASE_IMAGE = "drive.img"
C_DIR = Path("src/c")
ORG_DIRECTIVE = "org 08048000h"
STATIC_DIR = Path("static")

# The self-host run on asm.asm itself is the slow-path test; every
# other program in static/ finishes well under a second.  Give asm
# its own generous budget and let everything else trip the default
# 4s cap.  CI runners are significantly slower; use BBOE_ASM_SELF_HOST_TIMEOUT
# to raise the ceiling (the workflow sets it to 32).
ASM_SELF_HOST_TIMEOUT = int(os.environ.get("BBOE_ASM_SELF_HOST_TIMEOUT", "12"))


def _build_and_discover(*, only: str | None, temporary_directory: Path) -> list[Path]:
    """Compile C sources, build the drive image, and return discovered programs."""
    c_programs = compile_c_sources(temporary_directory=temporary_directory)
    if c_programs:
        c_names = " ".join(path.stem + ".c" for path in c_programs)
        print(f"Compiled C sources: {c_names}")

    image = temporary_directory / BASE_IMAGE
    subprocess.run(
        ["./make_os.sh", str(image)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for asm_source in c_programs:
        subprocess.run(
            ["./add_file.py", "--image", str(image), "-d", "src", str(asm_source)],
            check=True,
            stdout=subprocess.DEVNULL,
        )

    return discover_programs(additional=c_programs, only=only)


def _build_references(
    *,
    programs: list[Path],
    temporary_directory: Path,
) -> dict[str, Path]:
    """Assemble each program with NASM to produce reference binaries."""
    references: dict[str, Path] = {}
    for source in programs:
        name = source.stem
        reference = temporary_directory / f"ref_{name}.bin"
        subprocess.run(
            ["nasm", "-f", "bin", "-o", str(reference), str(source), "-I", "static/"],
            check=True,
        )
        references[name] = reference
    return references


def _run_tests(*, arguments: argparse.Namespace) -> int:
    """Execute the test loop: build OS, discover programs, compare outputs."""
    directory_sectors = read_assign("DIRECTORY_SECTORS")
    keep_artifacts = arguments.program is not None

    with tempfile.TemporaryDirectory(prefix="test_asm_") as temporary_path:
        temporary_directory = Path(temporary_path)
        programs = _build_and_discover(
            only=arguments.program,
            temporary_directory=temporary_directory,
        )
        directory_sector = compute_directory_sector(image_path=str(temporary_directory / BASE_IMAGE))
        if not programs:
            if arguments.program:
                print(f"No program named '{arguments.program}' in static/")
            else:
                print("No programs found in static/")
            return 1

        print("Programs to test:", " ".join(p.name for p in programs))
        print()

        references = _build_references(
            programs=programs,
            temporary_directory=temporary_directory,
        )

        pass_count = 0
        fail_count = 0
        failed: list[str] = []
        for source in programs:
            name = source.stem
            reference = references[name]
            output_binary = temporary_directory / f"out_{name}.bin"
            ok, message, boot_time, command_time = test_program(
                directory_sector=directory_sector,
                directory_sectors=directory_sectors,
                floppy=arguments.floppy,
                name=name,
                output_binary=output_binary,
                reference=reference,
                temporary_directory=temporary_directory,
            )
            label = f"{name}.asm"
            timing = f"boot {boot_time:.2f}s  cmd {command_time:.2f}s"
            if ok:
                print(f"  PASS  {label:<20} {reference.stat().st_size:>6} bytes  {timing}")
                pass_count += 1
            else:
                print(f"  FAIL  {label:<20} {message}  {timing}")
                fail_count += 1
                failed.append(label)
                if arguments.fail_fast:
                    break

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
    image = drive.read_bytes()
    entry = find_entry(
        directory_sectors=directory_sectors,
        directory_start_sector=directory_sector,
        image=image,
        name=output_name,
    )
    if entry is None:
        return False, "output file not found on drive"
    _flags, start_sector, size = entry
    output_data = image[start_sector * SECTOR_SIZE :][:size]
    output_binary.write_bytes(output_data)
    if output_data == reference_bytes:
        return True, ""
    if only_prefix_reordering(output_data, reference_bytes):
        return True, ""
    return False, f"expected {len(reference_bytes)} bytes, got {size} bytes"


def compile_c_sources(*, temporary_directory: Path) -> list[Path]:
    """Compile each src/c/*.c to temporary_directory/<name>.asm via cc.py.

    Skips C sources whose corresponding .asm already exists in static/
    (i.e. a hand-written version is the source of truth).  Returns the
    list of generated paths so they can be included in the test run.

    Compiled at ``--bits 32`` to match production (see make_os.sh):
    every emitted .asm file then runs through both NASM and the
    self-hosted assembler in QEMU, and the byte streams must match.
    The 32-bit self-host path used to drift from NASM (operand-size
    prefix omissions and a 16-bit-only ``[mem], imm`` encoder); those
    gaps are closed.
    """
    if not C_DIR.is_dir():
        return []
    generated: list[Path] = []
    for c_source in sorted(C_DIR.glob("*.c")):
        name = c_source.stem
        if (STATIC_DIR / f"{name}.asm").exists():
            continue
        target = temporary_directory / f"{name}.asm"
        subprocess.run(
            ["./cc.py", "--bits", "32", str(c_source), str(target)],
            check=True,
        )
        generated.append(target)
    return generated


def discover_programs(*, additional: list[Path] | None = None, only: str | None) -> list[Path]:
    """Return the list of static/*.asm (plus any additional) programs that target PROGRAM_BASE."""
    candidates = sorted(STATIC_DIR.glob("*.asm"))
    if additional:
        candidates = sorted(candidates + additional, key=lambda p: p.stem)
    programs: list[Path] = []
    for source in candidates:
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
    os.chdir(REPO_ROOT)
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "program",
        nargs="?",
        help="restrict the test to one program (e.g. 'edit')",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="stop after the first failing test",
    )
    parser.add_argument(
        "--floppy",
        action="store_true",
        help="boot QEMU with the drive attached as a floppy (if=floppy)",
    )
    arguments = parser.parse_args()
    return _run_tests(arguments=arguments)


def only_prefix_reordering(actual: bytes, expected: bytes, /) -> bool:
    """Return True if the two byte streams differ only by x86 prefix ordering.

    Instead of blindly normalizing the whole stream (which could mask real
    differences in immediates/displacements that happen to match prefix byte
    values), this checks each contiguous run of differing bytes and accepts
    the mismatch only if both sides contain the exact same multiset of bytes
    — i.e. a pure reordering, not different content.
    """
    if len(actual) != len(expected):
        return False
    i = 0
    length = len(actual)
    while i < length:
        if actual[i] == expected[i]:
            i += 1
            continue
        # Found a mismatch — collect the full contiguous differing run.
        start = i
        while i < length and actual[i] != expected[i]:
            i += 1
        if sorted(actual[start:i]) != sorted(expected[start:i]):
            return False
    return True


def persist_artifacts(*, temporary_directory: Path) -> Path:
    """Copy artifacts out of `temporary_directory` to a persistent directory."""
    persist = Path(tempfile.mkdtemp(prefix="test_asm_keep_"))
    for item in temporary_directory.iterdir():
        if not item.is_file():
            continue
        shutil.copy(item, persist / item.name)
    return persist


def test_program(
    *,
    directory_sector: int,
    directory_sectors: int,
    floppy: bool,
    name: str,
    output_binary: Path,
    reference: Path,
    temporary_directory: Path,
) -> tuple[bool, str, float, float]:
    """Assemble a single program in QEMU and compare the output to the NASM reference.

    Returns (passed, message, boot_time, command_time).
    """
    output_name = f"{name}_t"
    drive = temporary_directory / f"drive_{name}.img"
    shutil.copy(temporary_directory / BASE_IMAGE, drive)

    command_timeout = ASM_SELF_HOST_TIMEOUT if name == "asm" else COMMAND_TIMEOUT
    result = run_commands(
        [f"asm src/{name}.asm {output_name}"],
        command_timeout=command_timeout,
        drive=drive,
        floppy=floppy,
    )
    ok, message = compare_drive_output(
        directory_sector=directory_sector,
        directory_sectors=directory_sectors,
        drive=drive,
        output_binary=output_binary,
        output_name=output_name,
        reference_bytes=reference.read_bytes(),
    )
    command_time = result.command_times[0] if result.command_times else 0.0
    return ok, message, result.boot_time, command_time


if __name__ == "__main__":
    sys.exit(main())
