#!/usr/bin/env python3
"""cc.py --bits=16 / --bits=32 regression test.

For each src/c/*.c: run cc.py under both emission modes and pass the
result through NASM.  Fails if either mode produces assembly that NASM
rejects.  This is the only test that actually invokes cc.py under
``--bits=32``; test_cc.py uses clang for C syntax and test_programs.py
runs through the 16-bit default by booting the OS.

Usage:
    tests/test_cc_bits.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CC = REPO_ROOT / "cc.py"
SOURCE_DIR = REPO_ROOT / "src" / "c"
INCLUDE_DIR = REPO_ROOT / "src" / "include"


def compile_and_assemble(*, source: Path, bits: int, work: Path) -> tuple[bool, str]:
    """Run cc.py then nasm; return (passed, first-line-of-error)."""
    asm_path = work / f"{source.stem}-{bits}.asm"
    bin_path = work / f"{source.stem}-{bits}.bin"
    cc_result = subprocess.run(
        ["python3", str(CC), "--bits", str(bits), str(source), str(asm_path)],
        capture_output=True,
        check=False,
        text=True,
    )
    if cc_result.returncode != 0:
        return False, f"cc.py: {cc_result.stderr.strip().splitlines()[:1]}"
    nasm_result = subprocess.run(
        ["nasm", "-f", "bin", "-i", str(INCLUDE_DIR) + "/", str(asm_path), "-o", str(bin_path)],
        capture_output=True,
        check=False,
        text=True,
    )
    if nasm_result.returncode != 0:
        first = (nasm_result.stderr.strip().splitlines() or [""])[0]
        return False, f"nasm: {first}"
    return True, ""


def main() -> int:
    """Run cc.py + nasm over all .c files in both emission modes."""
    sources = sorted(SOURCE_DIR.glob("*.c"))
    fail_count = 0
    failed: list[str] = []
    with tempfile.TemporaryDirectory(prefix="test_cc_bits_") as temp_dir:
        work = Path(temp_dir)
        for bits in (16, 32):
            pass_count = 0
            for source in sources:
                ok, message = compile_and_assemble(source=source, bits=bits, work=work)
                if ok:
                    pass_count += 1
                else:
                    fail_count += 1
                    failed.append(f"{source.name} (--bits={bits}): {message}")
            print(f"--bits={bits}: {pass_count} / {len(sources)} pass")
    print()
    print(f"{len(sources) * 2 - fail_count} passed, {fail_count} failed")
    if fail_count:
        for line in failed:
            print("  FAIL:", line)
    return 1 if fail_count else 0


if __name__ == "__main__":
    sys.exit(main())
