#!/usr/bin/env python3
"""Syntax-check C sources with clang.

Verifies that every src/c/*.c and tests/programs/*.c file compiles
cleanly under clang with the bboeos.h compatibility header, catching
type errors and syntax mistakes that cc.py's minimal parser might
miss.

Usage:
    tests/test_cc.py            # check all C sources
    tests/test_cc.py cat        # check one program
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HEADER = Path(__file__).resolve().parent / "bboeos.h"
REPO_ROOT = HEADER.parent.parent
SOURCE_DIRS = (REPO_ROOT / "src" / "c", REPO_ROOT / "tests" / "programs")


def check_program(*, source: Path) -> tuple[bool, str]:
    """Run clang -fsyntax-only on a single source file."""
    result = subprocess.run(
        [
            "clang",
            "-fsyntax-only",
            "-include",
            str(HEADER),
            str(source),
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode == 0:
        return True, ""
    return False, result.stderr.strip()


def main() -> int:
    """Check all (or one) C source file with clang."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("program", nargs="?", help="restrict to one program (e.g. 'cat')")
    arguments = parser.parse_args()

    sources = sorted(source for directory in SOURCE_DIRS for source in directory.glob("*.c"))
    if arguments.program:
        sources = [s for s in sources if s.stem == arguments.program]
        if not sources:
            print(f"No C source named '{arguments.program}'")
            return 1

    pass_count = 0
    fail_count = 0
    failed: list[str] = []
    for source in sources:
        ok, message = check_program(source=source)
        if ok:
            print(f"  PASS  {source.name}")
            pass_count += 1
        else:
            print(f"  FAIL  {source.name}")
            print(message)
            fail_count += 1
            failed.append(source.name)

    print()
    print(f"{pass_count} passed, {fail_count} failed")
    if fail_count:
        print("Failed:", " ".join(failed))
    return 1 if fail_count else 0


if __name__ == "__main__":
    sys.exit(main())
