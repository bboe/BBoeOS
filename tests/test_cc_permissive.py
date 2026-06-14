#!/usr/bin/env python3
"""cc.py ``--permissive`` mode: relaxed pointer/char comparison checks.

By default cc.py enforces bboeos house style — a pointer may only be
compared to another pointer or the explicit ``NULL`` spelling, and a
``char`` may only be compared to another ``char`` / character literal —
rejecting ``if (p)``, ``p == 0``, and ``c != 0``.  Third-party C (kilo,
lua, Doom) uses those forms pervasively, so ``--permissive`` treats an
integer literal ``0`` as a null-pointer constant, allows pointer
truthiness, and relaxes the char-vs-int check.  The strict checks remain
the default for first-party code.

Usage:
    tests/test_cc_permissive.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

# Each snippet uses a pointer/char comparison the strict default rejects.
_CHAR_NE_ZERO = "int f(char c) { if (c != 0) return 1; return 0; }\n"
_POINTER_EQ_ZERO = "int f(char *p) { if (p == 0) return 1; return 0; }\n"


_POINTER_NE_ZERO = "int f(char *p) { if (p != 0) return 1; return 0; }\n"


_POINTER_TRUTHINESS = "int f(char *p) { if (p) return 1; return 0; }\n"
REPO_ROOT = Path(__file__).resolve().parent.parent
CC = REPO_ROOT / "cc.py"


def _compile(*, permissive: bool, source: str, work: Path) -> subprocess.CompletedProcess:
    source_path = work / "input.c"
    asm_path = work / "input.asm"
    source_path.write_text(source)
    command = ["python3", str(CC), "--bits", "32"]
    if permissive:
        command.append("--permissive")
    command += [str(source_path), str(asm_path)]
    return subprocess.run(command, capture_output=True, check=False, text=True)


def main() -> int:
    """Run every test_* under a shared tempdir; return 0 iff all pass."""
    fail_count = 0
    with tempfile.TemporaryDirectory(prefix="test_cc_permissive_") as temporary_directory:
        work = Path(temporary_directory)
        for test in TESTS:
            try:
                test(work=work)
                print(f"PASS  {test.__name__}")
            except AssertionError as failure:
                fail_count += 1
                print(f"FAIL  {test.__name__}: {failure}")
    print()
    print(f"{len(TESTS) - fail_count} passed, {fail_count} failed")
    return 1 if fail_count else 0


def test_permissive_allows_char_vs_zero(*, work: Path) -> None:
    """``c != 0`` compiles under --permissive."""
    result = _compile(permissive=True, source=_CHAR_NE_ZERO, work=work)
    assert result.returncode == 0, result.stderr


def test_permissive_allows_pointer_eq_zero(*, work: Path) -> None:
    """``p == 0`` compiles under --permissive."""
    result = _compile(permissive=True, source=_POINTER_EQ_ZERO, work=work)
    assert result.returncode == 0, result.stderr


def test_permissive_allows_pointer_ne_zero(*, work: Path) -> None:
    """``p != 0`` compiles under --permissive."""
    result = _compile(permissive=True, source=_POINTER_NE_ZERO, work=work)
    assert result.returncode == 0, result.stderr


def test_permissive_allows_pointer_truthiness(*, work: Path) -> None:
    """``if (p)`` compiles under --permissive."""
    result = _compile(permissive=True, source=_POINTER_TRUTHINESS, work=work)
    assert result.returncode == 0, result.stderr


def test_strict_default_rejects_pointer_vs_zero(*, work: Path) -> None:
    """Without the flag, ``p == 0`` is still an error (house style preserved)."""
    result = _compile(permissive=False, source=_POINTER_EQ_ZERO, work=work)
    assert result.returncode != 0, "strict mode should reject `p == 0`"
    assert "pointer compared to non-pointer" in result.stderr, result.stderr


TESTS = [
    test_permissive_allows_char_vs_zero,
    test_permissive_allows_pointer_eq_zero,
    test_permissive_allows_pointer_ne_zero,
    test_permissive_allows_pointer_truthiness,
    test_strict_default_rejects_pointer_vs_zero,
]


if __name__ == "__main__":
    sys.exit(main())
