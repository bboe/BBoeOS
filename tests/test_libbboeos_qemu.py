#!/usr/bin/env python3
"""On-OS libc smoke test.

Builds tools/libc/test_program/hello.c against libbboeos.a and the
tools/libc/program.ld linker script, drops it on the disk image as
bin/hello, runs it from the shell, and verifies the expected serial
markers.  This is end-to-end coverage for the libc shim: printf,
malloc/free, setjmp/longjmp, and program exit through _start.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LIBC = REPO / "tools" / "libc"

HELLO_BIN = LIBC / "test_program" / "hello"
HELLO_SRC = LIBC / "test_program" / "hello.c"
PROGRAM_LD = LIBC / "program.ld"

sys.path.insert(0, str(REPO / "tests"))

from run_qemu import run_commands  # noqa: E402

# Serial markers the test asserts the program emits.  The test searches
# each pattern in the full output independently, so this list is sorted
# alphabetically rather than in the program's emit order.
EXPECTED = [
    r"\[bboeos libc\] -1 4000000000 cafe ok",
    r"\[bboeos libc\] done",
    r"\[bboeos libc\] hello",
    r"\[bboeos libc\] longjmp returned 42",
    r"\[bboeos libc\] malloc-works",
]


def _build_hello() -> None:
    """Compile hello.c via the libc Makefile, then link with program.ld → flat binary.

    The compile step reuses the Makefile's %.o : %.c pattern so the
    freestanding CFLAGS stay defined in exactly one place.  The link
    step stays here so the test owns the cross-cutting "build a
    bboeos program" recipe rather than baking program-specific link
    rules into the libc Makefile.
    """
    obj = HELLO_SRC.with_suffix(".o")
    subprocess.check_call(["make", "-C", str(LIBC), str(obj.relative_to(LIBC))])
    if HELLO_BIN.exists():
        HELLO_BIN.unlink()
    subprocess.check_call([
        "ld",
        "-m",
        "elf_i386",
        "-T",
        str(PROGRAM_LD),
        "--oformat",
        "binary",
        "-o",
        str(HELLO_BIN),
        str(LIBC / "_start.o"),
        str(obj),
        str(LIBC / "libbboeos.a"),
    ])


def _build_image_and_install() -> None:
    """Run make_os.sh to produce a fresh drive.img, then add bin/hello."""
    subprocess.check_call(["./make_os.sh"], cwd=REPO)
    subprocess.check_call(["./add_file.py", "-x", "-d", "bin", str(HELLO_BIN)], cwd=REPO)


def _build_libbboeos() -> None:
    """Build libbboeos.a (and the per-source .o files) via tools/libc/Makefile."""
    subprocess.check_call(["make", "-C", str(LIBC)])


def main() -> None:
    """Build libc + hello, install on disk, run via run_commands, verify markers."""
    _build_libbboeos()
    _build_hello()
    _build_image_and_install()
    result = run_commands(["hello"], memory="16")
    output = result.output.replace("\r", "")
    for pattern in EXPECTED:
        if not re.search(pattern, output):
            print(f"MISSING: {pattern}")
            print("--- output ---")
            print(output)
            sys.exit(1)
    print("libbboeos QEMU smoke test pass")


if __name__ == "__main__":
    main()
