#!/usr/bin/env python3
"""Verify archive/*.asm still assembles and the bytes match the README.

The archive directory holds the last-known-good assembly form of each
program that has since been rewritten in C.  The byte-size comparison
in archive/README.md is only meaningful if those asm sources keep
assembling under the current kernel ABI — if a shared include grows
or a syscall goes away, the archive must track it.

This test:
  1. Assembles every archive/*.asm with nasm.
  2. Parses the comparison table in archive/README.md.
  3. Fails if any program fails to assemble, is missing from the
     README, or produces a different number of bytes than the
     README claims.

Usage:
    tests/test_archive.py            # check everything
    tests/test_archive.py ping       # check one program
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ARCHIVE_DIR = REPO_ROOT / "archive"
INCLUDE_DIR = REPO_ROOT / "src" / "include"
README_PATH = ARCHIVE_DIR / "README.md"

# "| name | asm_bytes | c_bytes | delta |" rows in the table.
TABLE_ROW = re.compile(r"^\|\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\|\s*(?P<asm>\d+)\s*\|")


def parse_readme_table(*, readme: Path) -> dict[str, int]:
    """Return a mapping of program name -> asm byte count from the README."""
    sizes: dict[str, int] = {}
    for line in readme.read_text(encoding="utf-8").splitlines():
        match = TABLE_ROW.match(line)
        if match is None:
            continue
        if match.group("name") == "Program":
            continue  # header row
        sizes[match.group("name")] = int(match.group("asm"))
    return sizes


def assemble(*, source: Path, output: Path) -> tuple[bool, str]:
    """Run nasm on *source* writing to *output*.  Return (ok, stderr)."""
    result = subprocess.run(
        [
            "nasm",
            "-f",
            "bin",
            "-i",
            str(INCLUDE_DIR),
            "-o",
            str(output),
            str(source),
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    return result.returncode == 0, result.stderr.strip()


def check_program(*, source: Path, expected_bytes: int | None, scratch: Path) -> tuple[bool, str]:
    """Assemble *source* and confirm its size matches the README."""
    output = scratch / source.stem
    ok, stderr = assemble(source=source, output=output)
    if not ok:
        return False, f"nasm failed: {stderr}"
    actual_bytes = output.stat().st_size
    if expected_bytes is None:
        return False, f"no README row (built {actual_bytes} bytes)"
    if actual_bytes != expected_bytes:
        return False, f"size drift: README says {expected_bytes}, built {actual_bytes}"
    return True, f"{actual_bytes} bytes"


def main() -> int:
    """Assemble every archive program and diff against the README table."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("program", nargs="?", help="restrict to one program (e.g. 'ping')")
    arguments = parser.parse_args()

    sources = sorted(ARCHIVE_DIR.glob("*.asm"))
    if arguments.program:
        sources = [s for s in sources if s.stem == arguments.program]
        if not sources:
            print(f"No archive asm named '{arguments.program}'")
            return 1

    sizes = parse_readme_table(readme=README_PATH)

    pass_count = 0
    fail_count = 0
    failed: list[str] = []
    with tempfile.TemporaryDirectory(prefix="test_archive_") as temp_dir:
        scratch = Path(temp_dir)
        for source in sources:
            ok, message = check_program(
                source=source,
                expected_bytes=sizes.get(source.stem),
                scratch=scratch,
            )
            status = "PASS" if ok else "FAIL"
            print(f"  {status}  {source.name:<14} {message}")
            if ok:
                pass_count += 1
            else:
                fail_count += 1
                failed.append(source.name)

    print()
    print(f"{pass_count} passed, {fail_count} failed")
    if fail_count:
        print("Failed:", " ".join(failed))
    return 1 if fail_count else 0


if __name__ == "__main__":
    sys.exit(main())
