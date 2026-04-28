#!/usr/bin/env python3
"""Verify archive/kernel/ snapshots and README rows stay in sync.

Kernel files don't assemble standalone (they %include each other and
share globals), so the user-space archive's "build both forms and diff
the bytes" approach doesn't transfer directly.  This test is the
minimal scaffold:

  1. Parses the byte-count table in archive/kernel/README.md.
  2. For each row, asserts the snapshot file exists at
     archive/kernel/<path>.asm.
  3. Optionally enforces that the live source tree has the matching
     C port at src/<path>.c.

Per-row byte verification (build os.bin twice and diff) is deferred
until size drift becomes a problem — for now port commits land the
README numbers manually and downstream readers can re-check by
swapping the %include and rebuilding.

Usage:
    tests/test_kernel_archive.py            # check everything
    tests/test_kernel_archive.py drivers/ps2  # check one path
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ARCHIVE_KERNEL = REPO_ROOT / "archive" / "kernel"
README_PATH = ARCHIVE_KERNEL / "README.md"
SRC_DIR = REPO_ROOT / "src"

# ``| path | asm_bytes | c_bytes | delta |`` rows.  Path may contain
# slashes (drivers/vga, fs/fd, …).  Delta may have a leading sign and
# may be 0 (no sign).
TABLE_ROW = re.compile(
    r"^\|\s*(?P<path>[A-Za-z_][A-Za-z0-9_/]*)\s*"
    r"\|\s*(?P<asm>\d+)\s*"
    r"\|\s*(?P<c>\d+)\s*"
    r"\|\s*(?P<delta>[+\-]?\d+)\s*\|"
)


@dataclass(frozen=True, slots=True)
class Expected:
    """Expected byte counts for a single kernel file row."""

    asm: int
    c: int
    delta: int


def check_path(*, path: str, expected: Expected) -> tuple[bool, str]:
    """Verify the snapshot and the C port both exist for one row."""
    archive_asm = ARCHIVE_KERNEL / f"{path}.asm"
    if not archive_asm.is_file():
        return False, f"missing archive snapshot at {archive_asm.relative_to(REPO_ROOT)}"
    src_c = SRC_DIR / f"{path}.c"
    if not src_c.is_file():
        return False, f"missing C port at {src_c.relative_to(REPO_ROOT)}"
    actual_delta = expected.c - expected.asm
    if actual_delta != expected.delta:
        return False, f"README delta {expected.delta:+d} != c-asm = {actual_delta:+d}"
    return True, f"asm={expected.asm} c={expected.c} Δ={expected.delta:+d}"


def main() -> int:
    """Walk the README table and verify each row's snapshot + C port."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("path", nargs="?", help="restrict to one path (e.g. 'drivers/ps2')")
    arguments = parser.parse_args()

    rows = parse_readme_table(readme=README_PATH)
    if arguments.path:
        if arguments.path not in rows:
            print(f"No README row for '{arguments.path}'")
            return 1
        rows = {arguments.path: rows[arguments.path]}

    if not rows:
        print("(no archive rows yet — scaffold OK)")
        return 0

    pass_count = 0
    fail_count = 0
    failed: list[str] = []
    for path in sorted(rows):
        ok, message = check_path(path=path, expected=rows[path])
        status = "PASS" if ok else "FAIL"
        print(f"  {status}  {path:<24} {message}")
        if ok:
            pass_count += 1
        else:
            fail_count += 1
            failed.append(path)

    print()
    print(f"{pass_count} passed, {fail_count} failed")
    if fail_count:
        print("Failed:", " ".join(failed))
    return 1 if fail_count else 0


def parse_readme_table(*, readme: Path) -> dict[str, Expected]:
    """Return a mapping of path -> Expected row from the README."""
    rows: dict[str, Expected] = {}
    for line in readme.read_text(encoding="utf-8").splitlines():
        match = TABLE_ROW.match(line)
        if match is None:
            continue
        rows[match.group("path")] = Expected(
            asm=int(match.group("asm")),
            c=int(match.group("c")),
            delta=int(match.group("delta")),
        )
    return rows


if __name__ == "__main__":
    sys.exit(main())
