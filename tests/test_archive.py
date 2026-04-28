#!/usr/bin/env python3
"""Verify the archive/*.asm and src/c/*.c byte counts match the README table.

The archive directory holds the last-known-good assembly form of each
program that has since been rewritten in C.  The byte-size comparison
in archive/README.md is only meaningful if every cell stays honest —
the ASM side must still assemble under the current kernel ABI, the C
side must still compile (via cc.py) under the current constants and
builtins, and the delta must equal ``c - asm``.

This test:
  1. Assembles every archive/*.asm with nasm.
  2. Compiles the matching src/c/<name>.c via cc.py + nasm.
  3. Parses all four columns of the comparison table in
     archive/README.md.
  4. Fails if any program is missing from the README, fails to
     build, or any of the three numbers (asm, c, delta) disagrees
     with what was built.

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
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ARCHIVE_DIR = REPO_ROOT / "archive"
C_DIR = REPO_ROOT / "src" / "c"
CC_PY = REPO_ROOT / "cc.py"
INCLUDE_DIR = REPO_ROOT / "src" / "include"
README_PATH = ARCHIVE_DIR / "README.md"

# ``| name | asm_16 | asm | c | delta |`` rows.  asm_16 is the frozen
# 16-bit byte size (historical reference, never re-verified).  Delta
# may have a leading sign and may be 0 (no sign).
TABLE_ROW = re.compile(
    r"^\|\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*"
    r"\|\s*(?P<asm_16>\d+)\s*"
    r"\|\s*(?P<asm>\d+)\s*"
    r"\|\s*(?P<c>\d+)\s*"
    r"\|\s*(?P<delta>[+\-]?\d+)\s*\|"
)


@dataclass(frozen=True, slots=True)
class Expected:
    """Expected byte counts for a single program row.

    ``asm_16`` is frozen — the 16-bit baseline preserved for historical
    comparison and never re-verified.  ``asm`` and ``c`` are 32-bit byte
    counts checked against the actual archive .asm and cc.py output.
    """

    asm_16: int
    asm: int
    c: int
    delta: int


def parse_readme_table(*, readme: Path) -> dict[str, Expected]:
    """Return a mapping of program name -> Expected row from the README."""
    rows: dict[str, Expected] = {}
    for line in readme.read_text(encoding="utf-8").splitlines():
        match = TABLE_ROW.match(line)
        if match is None:
            continue
        if match.group("name") == "Program":
            continue
        rows[match.group("name")] = Expected(
            asm_16=int(match.group("asm_16")),
            asm=int(match.group("asm")),
            c=int(match.group("c")),
            delta=int(match.group("delta")),
        )
    return rows


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


def compile_c(*, source: Path, output: Path, scratch: Path) -> tuple[bool, str]:
    """Run cc.py + nasm on *source*, writing the binary to *output*."""
    asm_path = scratch / f"{source.stem}.asm"
    result = subprocess.run(
        [sys.executable, str(CC_PY), str(source), str(asm_path)],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        return False, f"cc.py failed: {result.stderr.strip() or result.stdout.strip()}"
    return assemble(source=asm_path, output=output)


def check_program(*, name: str, expected: Expected | None, scratch: Path) -> tuple[bool, str]:
    """Build both forms of *name* and diff their bytes against the README."""
    asm_source = ARCHIVE_DIR / f"{name}.asm"
    c_source = C_DIR / f"{name}.c"
    if not c_source.is_file():
        return False, f"no src/c/{name}.c for archive entry"
    if expected is None:
        return False, "no README row"
    asm_output = scratch / f"{name}.asm.bin"
    c_output = scratch / f"{name}.c.bin"
    ok, stderr = assemble(source=asm_source, output=asm_output)
    if not ok:
        return False, f"nasm failed on archive asm: {stderr}"
    ok, stderr = compile_c(source=c_source, output=c_output, scratch=scratch)
    if not ok:
        return False, stderr
    actual_asm = asm_output.stat().st_size
    actual_c = c_output.stat().st_size
    actual_delta = actual_c - actual_asm
    problems: list[str] = []
    if actual_asm != expected.asm:
        problems.append(f"asm {expected.asm}→{actual_asm}")
    if actual_c != expected.c:
        problems.append(f"c {expected.c}→{actual_c}")
    if actual_delta != expected.delta:
        problems.append(f"delta {expected.delta:+d}→{actual_delta:+d}")
    if problems:
        return False, "size drift: " + ", ".join(problems)
    return True, f"asm={actual_asm} c={actual_c} Δ={actual_delta:+d}"


def main() -> int:
    """Build every archived program and diff against the README table."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("program", nargs="?", help="restrict to one program (e.g. 'ping')")
    arguments = parser.parse_args()

    names = sorted(p.stem for p in ARCHIVE_DIR.glob("*.asm"))
    if arguments.program:
        names = [n for n in names if n == arguments.program]
        if not names:
            print(f"No archive asm named '{arguments.program}'")
            return 1

    rows = parse_readme_table(readme=README_PATH)

    pass_count = 0
    fail_count = 0
    failed: list[str] = []
    with tempfile.TemporaryDirectory(prefix="test_archive_") as temp_dir:
        scratch = Path(temp_dir)
        for name in names:
            ok, message = check_program(
                name=name,
                expected=rows.get(name),
                scratch=scratch,
            )
            status = "PASS" if ok else "FAIL"
            print(f"  {status}  {name:<12} {message}")
            if ok:
                pass_count += 1
            else:
                fail_count += 1
                failed.append(name)

    print()
    print(f"{pass_count} passed, {fail_count} failed")
    if fail_count:
        print("Failed:", " ".join(failed))
    return 1 if fail_count else 0


if __name__ == "__main__":
    sys.exit(main())
