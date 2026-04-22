#!/usr/bin/env python3
"""Runtime smoke tests for programs loaded from an ext2 filesystem.

Builds the OS with `make_os.sh --ext2`, boots in QEMU, runs a representative
command for each test program, and checks the output against an expected regex.
Each test gets its own QEMU boot with `snapshot=on` so writes don't affect the
shared image.

Programs that read file content via `io_read` (e.g. `cat`) exercise the
`vfs_read_sec` function pointer, which routes through `ext2_read_sec` to
translate byte positions to ext2 block lookups.  Programs that list directory
contents via `fd_read_dir` (e.g. `ls`) are excluded because directory reads
are still bbfs-only.

Usage:
    ./test_ext2.py            # run the full suite
    ./test_ext2.py hello      # run one program
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASE_IMAGE = "drive_ext2.img"

sys.path.insert(0, str(REPO_ROOT))

from run_qemu import run_commands  # noqa: E402


@dataclass
class ProgramTest:
    """One runtime test: shell commands to run and a regex the output must match."""

    name: str
    commands: list[str]
    expect: str
    timeout: float = 10.0


TESTS: list[ProgramTest] = [
    ProgramTest("cat", ["cat src/parse_ip.asm"], r"^parse_ip:"),
    ProgramTest("cat_large", ["cat src/asm.asm"], r"org 0600h", timeout=30.0),
    ProgramTest("echo", ["echo ext2"], r"^ext2$"),
    ProgramTest("hello", ["hello"], r"Hello world!"),
    ProgramTest("ls", ["ls bin"], r"hello\*"),
    ProgramTest("uptime", ["uptime"], r"\d+:\d{2}:\d{2}"),
]


def _build_os(*, temporary_directory: Path) -> None:
    """Run make_os.sh --ext2; abort if the build fails."""
    image = temporary_directory / BASE_IMAGE
    result = subprocess.run(
        ["./make_os.sh", "--ext2", str(image)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        sys.exit(1)


def _run_test(*, temporary_directory: Path, test: ProgramTest) -> tuple[bool, str]:
    """Run one ProgramTest; return (passed, short message for report)."""
    try:
        output = run_commands(
            test.commands,
            command_timeout=test.timeout,
            drive=temporary_directory / BASE_IMAGE,
            snapshot=True,
        )
    except TimeoutError as error:
        return False, f"timeout: {error}"
    except RuntimeError as error:
        return False, f"qemu error: {error}"
    if re.search(test.expect, output.replace("\r", ""), re.MULTILINE):
        return True, ""
    return False, f"expected regex {test.expect!r} not found in output"


def main() -> int:
    """Run the selected ProgramTests and print a summary."""
    os.chdir(REPO_ROOT)
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("program", nargs="?", help="restrict to one program (e.g. 'hello')")
    arguments = parser.parse_args()

    tests = [t for t in TESTS if arguments.program is None or t.name == arguments.program]
    if not tests:
        print(f"No test named {arguments.program!r}")
        return 1

    with tempfile.TemporaryDirectory(prefix="test_ext2_") as temporary_path:
        temporary_directory = Path(temporary_path)
        _build_os(temporary_directory=temporary_directory)

        pass_count = 0
        fail_count = 0
        failed: list[str] = []
        for test in tests:
            started = time.monotonic()
            ok, message = _run_test(temporary_directory=temporary_directory, test=test)
            elapsed = time.monotonic() - started
            if ok:
                print(f"  PASS  {test.name:<12}              {elapsed:6.2f}s")
                pass_count += 1
            else:
                print(f"  FAIL  {test.name:<12}  {message}   {elapsed:6.2f}s")
                fail_count += 1
                failed.append(test.name)

    print()
    print(f"{pass_count} passed, {fail_count} failed")
    if fail_count:
        print("Failed:", " ".join(failed))
    return 1 if fail_count else 0


if __name__ == "__main__":
    sys.exit(main())
