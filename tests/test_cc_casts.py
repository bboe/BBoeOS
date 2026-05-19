#!/usr/bin/env python3
"""cc.py cast-expression coverage.

Drives cc.py over small C snippets that exercise (T)expr and (T *)expr,
assembles the output through nasm, and confirms cc.py treats casts as
identity (no truncation / sign-extension instructions injected).

Usage:
    tests/test_cc_casts.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CC = REPO_ROOT / "cc.py"
INCLUDE_DIR = REPO_ROOT / "src" / "include"


def compile_snippet(*, name: str, source: str, work: Path) -> str:
    """Run cc.py + nasm on ``source``; return the cc.py-emitted asm text."""
    source_path = work / f"{name}.c"
    asm_path = work / f"{name}.asm"
    bin_path = work / f"{name}.bin"
    source_path.write_text(source)
    subprocess.run(
        ["python3", str(CC), "--bits", "32", str(source_path), str(asm_path)],
        capture_output=True,
        check=True,
        text=True,
    )
    subprocess.run(
        [
            "nasm",
            "-f",
            "bin",
            "-i",
            str(INCLUDE_DIR) + "/",
            str(asm_path),
            "-o",
            str(bin_path),
        ],
        capture_output=True,
        check=True,
        text=True,
    )
    return asm_path.read_text()


def main() -> int:
    """Run every test_* under a shared tempdir; return 0 iff all pass."""
    fail_count = 0
    with tempfile.TemporaryDirectory(prefix="test_cc_casts_") as temporary_directory:
        work = Path(temporary_directory)
        for test in TESTS:
            try:
                test(work=work)
                print(f"PASS  {test.__name__}")
            except AssertionError as failure:
                fail_count += 1
                print(f"FAIL  {test.__name__}: {failure}")
            except subprocess.CalledProcessError as failure:
                fail_count += 1
                stderr_tail = (failure.stderr or "").strip().splitlines()[-1:]
                print(f"FAIL  {test.__name__}: subprocess: {stderr_tail}")
    print()
    print(f"{len(TESTS) - fail_count} passed, {fail_count} failed")
    return 1 if fail_count else 0


def test_cast_in_comparison(*, work: Path) -> None:
    """Cast as a comparison operand round-trips (regression: src/fs/fd/audio.c)."""
    compile_snippet(
        name="cast_in_comparison",
        source=(
            "int main() {\n"
            "    int chunk = 100;\n"
            "    int free_bytes = 50;\n"
            "    if ((uint32_t)chunk > free_bytes) { return 1; }\n"
            "    return 0;\n"
            "}\n"
        ),
        work=work,
    )


def test_pointer_cast_is_identity(*, work: Path) -> None:
    """Pointer cast through &local must round-trip through cc.py + nasm."""
    compile_snippet(
        name="pointer_cast",
        source=("int main() {\n    uint8_t b = 7;\n    uint8_t *p = (uint8_t *)&b;\n    return *p;\n}\n"),
        work=work,
    )


def test_struct_pointer_cast(*, work: Path) -> None:
    """Struct-pointer cast through &local round-trips and ->field accesses work."""
    compile_snippet(
        name="struct_pointer_cast",
        source=(
            "struct foo { uint8_t x; };\nint main() {\n    uint8_t b = 7;\n    struct foo *f = (struct foo *)&b;\n    return f->x;\n}\n"
        ),
        work=work,
    )


def test_value_cast_is_identity(*, work: Path) -> None:
    """(uint8_t)int_expr emits no truncation in main's body."""
    asm = compile_snippet(
        name="value_cast",
        source="int main() { int x = 42; return (uint8_t)x; }\n",
        work=work,
    )
    # Isolate main's body so we don't trip on string-table or epilogue tokens.
    body = asm.split("main:", 1)[1]
    body = body.split("\n_", 1)[0] if "\n_" in body else body
    assert "and " not in body.lower(), f"unexpected truncation in main body:\n{body}"


TESTS = (
    test_cast_in_comparison,
    test_pointer_cast_is_identity,
    test_struct_pointer_cast,
    test_value_cast_is_identity,
)


if __name__ == "__main__":
    sys.exit(main())
