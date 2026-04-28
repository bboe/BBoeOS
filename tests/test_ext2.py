#!/usr/bin/env python3
"""Runtime smoke tests for programs loaded from an ext2 filesystem.

Builds the OS with `make_os.sh --ext2`, boots in QEMU, runs a representative
command for each test program, and checks the output against an expected regex.
Each test gets its own copy of the base image so writes don't affect other
tests.  After QEMU exits, ``e2fsck -f -n`` runs on the modified image to check
filesystem integrity.

Programs that read file content via `io_read` (e.g. `cat`) exercise the
`vfs_read_sec` function pointer, which routes through `ext2_read_sec` to
translate byte positions to ext2 block lookups.  Programs that list directory
contents via `fd_read_dir` (e.g. `ls`) exercise the `vfs_read_dir_fn` function
pointer, which routes through `ext2_read_dir` to translate ext2 variable-length
directory entries into the fixed 32-byte bbfs format.

Usage:
    ./test_ext2.py            # run the full suite
    ./test_ext2.py hello      # run one program
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
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

from add_file import compute_directory_sector, ext2_add_file  # noqa: E402


@dataclass
class ProgramTest:
    """One runtime test: shell commands to run and a regex the output must match."""

    name: str
    commands: list[str]
    expect: str
    timeout: float = 10.0


TESTS: list[ProgramTest] = [
    ProgramTest("cat", ["cat src/parse_ip.asm"], r"^parse_ip:"),
    ProgramTest("cat_large", ["cat src/asm.c"], r"Self-hosted x86 assembler", timeout=30.0),
    ProgramTest(
        "chmod",
        ["cp src/parse_ip.asm out.asm", "chmod +x out.asm", "ls"],
        r"out\.asm\*",
    ),
    ProgramTest("cp", ["cp src/parse_ip.asm out.asm", "cat out.asm"], r"^parse_ip:"),
    ProgramTest(
        "cp_into_subdir",
        ["mkdir mydir", "cp src/parse_ip.asm mydir/copy.asm", "cat mydir/copy.asm"],
        r"^parse_ip:",
    ),
    ProgramTest(
        "cp_overwrite_shrink",
        ["cp src/asm.c out.c", "cp src/parse_ip.asm out.c", "cat out.c"],
        r"^parse_ip:",
        timeout=30.0,
    ),
    ProgramTest(
        "doubly_indirect_cat",
        ["cat src/large.bin"],
        r"EXT2_DOUBLY_INDIRECT_OK",  # sentinel placed at byte 274432 (block 268)
        timeout=60.0,
    ),
    ProgramTest(
        "doubly_indirect_cp",
        ["cp src/large.bin out.bin", "cat out.bin"],
        r"EXT2_DOUBLY_INDIRECT_OK",  # verifies doubly-indirect write path
        timeout=60.0,
    ),
    ProgramTest(
        "doubly_indirect_cp_shrink",
        ["cp src/large.bin out.bin", "cp src/parse_ip.asm out.bin", "cat out.bin"],
        r"^parse_ip:",
        timeout=60.0,
    ),
    ProgramTest("echo", ["echo ext2"], r"^ext2$"),
    ProgramTest("hello", ["hello"], r"Hello world!"),
    ProgramTest("ls", ["ls bin"], r"hello\*"),
    ProgramTest(
        "mkdir",
        ["mkdir mydir", "ls mydir"],
        r"^\.\./",  # '..' entry always present
    ),
    ProgramTest(
        "mkdir_nested",
        ["mkdir parent", "mkdir parent/child", "ls parent/child"],
        r"^\.\./",
    ),
    ProgramTest(
        "mkdir_ls_root",
        ["mkdir mydir", "ls"],
        r"mydir/",
    ),
    ProgramTest(
        "rename",
        ["cp src/parse_ip.asm out.asm", "mv out.asm renamed.asm", "cat renamed.asm"],
        r"^parse_ip:",
    ),
    ProgramTest(
        "rename_cross_parent",
        ["mkdir sub", "cp src/parse_ip.asm sub/file.asm", "mv sub/file.asm out.asm", "cat out.asm"],
        r"^parse_ip:",
    ),
    ProgramTest(
        "rename_dir",
        ["mkdir mydir", "mv mydir newdir", "ls newdir"],
        r"^\.\./",
    ),
    ProgramTest(
        "rename_dir_cross_parent",
        ["mkdir sub", "mkdir mydir", "mv mydir sub/mydir", "ls sub/mydir"],
        r"^\.\./",
    ),
    ProgramTest(
        "rmdir",
        ["mkdir mydir", "rmdir mydir", "ls mydir"],
        r"Not found",  # ls fails because mydir was successfully removed
    ),
    ProgramTest(
        "rmdir_nonempty",
        ["mkdir mydir", "cp src/parse_ip.asm mydir/file.asm", "rmdir mydir"],
        r"Not empty",
    ),
    ProgramTest(
        "rm",
        ["cp src/parse_ip.asm out.asm", "rm out.asm", "cat out.asm"],
        r"File not found",
    ),
    ProgramTest("uptime", ["uptime"], r"\d+:\d{2}:\d{2}"),
]


DOUBLY_INDIRECT_START = (12 + 256) * 1024  # byte 274432 = first doubly-indirect block
DOUBLY_INDIRECT_SENTINEL = b"EXT2_DOUBLY_INDIRECT_OK"


def _add_large_test_file(*, image: Path) -> None:
    """Inject a 280 KB file into src/ to exercise the doubly-indirect block paths.

    With 1 KB blocks the doubly-indirect threshold is 268 KB (12 direct + 256
    singly-indirect).  280 KB puts 12 data blocks into the doubly-indirect
    region, covering both the allocation and free paths.

    A sentinel string is written at byte 274432 (start of block 268, the first
    doubly-indirect block) so tests can confirm that reads and writes actually
    reach the doubly-indirect region rather than matching content in the direct
    or singly-indirect range.
    """
    target_bytes = 280 * 1024
    source = (REPO_ROOT / "src" / "c" / "asm.c").read_bytes()
    content = bytearray((source * (target_bytes // len(source) + 1))[:target_bytes])
    content[DOUBLY_INDIRECT_START : DOUBLY_INDIRECT_START + len(DOUBLY_INDIRECT_SENTINEL)] = DOUBLY_INDIRECT_SENTINEL
    with tempfile.TemporaryDirectory() as tmpdir:
        large_file = Path(tmpdir) / "large.bin"
        large_file.write_bytes(content)
        ext2_add_file(
            executable=False,
            ext2_start_sector=compute_directory_sector(image_path=str(image)),
            file_path=str(large_file),
            image_path=str(image),
            subdirectory="src",
        )


def _build_os(*, temporary_directory: Path, block_size: int = 1024) -> None:
    """Run make_os.sh --ext2; abort if the build fails."""
    image = temporary_directory / BASE_IMAGE
    result = subprocess.run(
        ["./make_os.sh", "--ext2", f"--ext2-block-size={block_size}", str(image)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        sys.exit(1)
    if block_size == 1024:
        _add_large_test_file(image=image)


def _fsck(*, image: Path) -> str | None:
    """Run e2fsck on the ext2 partition; return an error string or None if clean."""
    ext2_offset = compute_directory_sector(image_path=str(image)) * 512
    with Path(image).open("rb") as f:
        f.seek(ext2_offset)
        ext2_data = f.read()
    with tempfile.NamedTemporaryFile(suffix=".ext2", delete=False) as tmp:
        tmp.write(ext2_data)
        ext2_path = Path(tmp.name)
    try:
        result = subprocess.run(
            ["e2fsck", "-f", "-n", str(ext2_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            lines = result.stdout.splitlines()
            for line in lines:
                if line and not line.startswith("Pass ") and not line.startswith("Running ") and not line.startswith("/tmp"):
                    return line
            return f"exit {result.returncode}"
        return None
    finally:
        ext2_path.unlink(missing_ok=True)


def _run_test(*, floppy: bool, temporary_directory: Path, test: ProgramTest) -> tuple[bool, str]:
    """Run one ProgramTest; return (passed, short message for report)."""
    test_image = temporary_directory / f"test_{test.name}.img"
    shutil.copy2(temporary_directory / BASE_IMAGE, test_image)
    try:
        output = run_commands(
            test.commands,
            command_timeout=test.timeout,
            drive=test_image,
            floppy=floppy,
            snapshot=False,
        )
    except TimeoutError as error:
        return False, f"timeout: {error}"
    except RuntimeError as error:
        return False, f"qemu error: {error}"
    failures = []
    if not re.search(test.expect, output.replace("\r", ""), re.MULTILINE):
        failures.append(f"expected regex {test.expect!r} not found in output")
    fsck_error = _fsck(image=test_image)
    if fsck_error:
        failures.append(f"fsck: {fsck_error}")
    return (not failures), "; ".join(failures)


def _run_suite(
    *,
    floppy: bool,
    tests: list[ProgramTest],
    temporary_directory: Path,
    label: str = "",
) -> tuple[int, int, list[str]]:
    """Run a list of ProgramTests; return (pass_count, fail_count, failed_names)."""
    pass_count = 0
    fail_count = 0
    failed: list[str] = []
    for test in tests:
        name = f"{label}{test.name}" if label else test.name
        started = time.monotonic()
        ok, message = _run_test(floppy=floppy, temporary_directory=temporary_directory, test=test)
        elapsed = time.monotonic() - started
        if ok:
            print(f"  PASS  {name:<20}              {elapsed:6.2f}s")
            pass_count += 1
        else:
            print(f"  FAIL  {name:<20}  {message}   {elapsed:6.2f}s")
            fail_count += 1
            failed.append(name)
    return pass_count, fail_count, failed


# Subset of tests to re-run with 2 KB blocks (exercises the variable-block-size paths).
# Excludes tests that don't touch ext2 (echo, hello, uptime).
BLOCK_SIZE_TESTS: list[ProgramTest] = [
    t
    for t in TESTS
    if t.name
    in {
        "cat",
        "cat_large",
        "chmod",
        "cp",
        "cp_into_subdir",
        "cp_overwrite_shrink",
        "ls",
        "mkdir",
        "mkdir_ls_root",
        "mkdir_nested",
        "rename",
        "rename_dir",
        "rm",
        "rmdir",
        "rmdir_nonempty",
    }
]


def main() -> int:
    """Run the selected ProgramTests and print a summary."""
    os.chdir(REPO_ROOT)
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("program", nargs="?", help="restrict to one program (e.g. 'hello')")
    parser.add_argument(
        "--floppy",
        action="store_true",
        help="boot QEMU with the drive attached as a floppy (if=floppy); "
        "skips the 2 KB-block-size matrix because the resulting image "
        "exceeds the 1.44 MB floppy capacity",
    )
    arguments = parser.parse_args()

    tests = [t for t in TESTS if arguments.program is None or t.name == arguments.program]
    if not tests:
        print(f"No test named {arguments.program!r}")
        return 1

    total_pass = 0
    total_fail = 0
    all_failed: list[str] = []

    with tempfile.TemporaryDirectory(prefix="test_ext2_") as temporary_path:
        temporary_directory = Path(temporary_path)
        _build_os(temporary_directory=temporary_directory, block_size=1024)
        p, f, failed = _run_suite(floppy=arguments.floppy, tests=tests, temporary_directory=temporary_directory)
        total_pass += p
        total_fail += f
        all_failed += failed

    # 2 KB block-size tests (only when running the full suite, and not under --floppy:
    # mke2fs grows a 2 KB-block image past 1.44 MB so it can't be addressed via
    # QEMU's floppy backend).
    if arguments.program is None and not arguments.floppy:
        blk2_tests = BLOCK_SIZE_TESTS
        with tempfile.TemporaryDirectory(prefix="test_ext2_2k_") as temporary_path:
            temporary_directory = Path(temporary_path)
            _build_os(temporary_directory=temporary_directory, block_size=2048)
            p, f, failed = _run_suite(
                floppy=arguments.floppy,
                tests=blk2_tests,
                temporary_directory=temporary_directory,
                label="2k/",
            )
            total_pass += p
            total_fail += f
            all_failed += failed
    elif arguments.program is None and arguments.floppy:
        print(f"  SKIP  2k/* ({len(BLOCK_SIZE_TESTS)} tests) — image exceeds 1.44 MB floppy capacity")

    print()
    print(f"{total_pass} passed, {total_fail} failed")
    if total_fail:
        print("Failed:", " ".join(all_failed))
    return 1 if total_fail else 0


if __name__ == "__main__":
    sys.exit(main())
