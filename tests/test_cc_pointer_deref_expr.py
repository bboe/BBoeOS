#!/usr/bin/env python3
"""cc.py ``*(expr)`` — dereference of a parenthesized pointer expression.

kilo writes ``*(p+1)`` / ``*(p+klen)`` in its syntax highlighter.  cc.py's
``*``-deref parser previously only accepted ``*name``, ``*++p``, and the
``*(T *)cast`` form, so a parenthesized non-cast operand raised "expected
IDENT, got LPAREN".  The ``*(base + index)`` and ``*(base)`` forms now
desugar to ``base[index]`` so the existing Index lowering handles the
pointee-typed load.

Usage:
    tests/test_cc_pointer_deref_expr.py
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CC = REPO_ROOT / "cc.py"


def _compile(*, source: str, work: Path) -> tuple[subprocess.CompletedProcess, str]:
    source_path = work / "input.c"
    asm_path = work / "input.asm"
    source_path.write_text(source)
    result = subprocess.run(
        ["python3", str(CC), "--bits", "32", "--permissive", str(source_path), str(asm_path)],
        capture_output=True,
        check=False,
        text=True,
    )
    asm = asm_path.read_text() if result.returncode == 0 else ""
    return result, asm


def main() -> int:
    """Run every test_* under a shared tempdir; return 0 iff all pass."""
    fail_count = 0
    with tempfile.TemporaryDirectory(prefix="test_cc_pointer_deref_expr_") as temporary_directory:
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


def test_deref_base_plus_constant_loads_at_offset(*, work: Path) -> None:
    """``*(p+1)`` loads the byte one element past ``p`` (== ``p[1]``)."""
    result, asm = _compile(source="int f(char *p){ return *(p+1); }\n", work=work)
    assert result.returncode == 0, result.stderr
    assert re.search(r"byte \[\w+\+1\]", asm), f"expected a byte load at [reg+1]:\n{asm}"


def test_deref_base_plus_variable(*, work: Path) -> None:
    """``*(p+klen)`` (variable index) compiles."""
    result, _ = _compile(source="int f(char *p, int klen){ return *(p+klen); }\n", work=work)
    assert result.returncode == 0, result.stderr


def test_deref_parenthesized_base(*, work: Path) -> None:
    """``*(p)`` (redundant parentheses) compiles."""
    result, _ = _compile(source="int f(char *p){ return *(p); }\n", work=work)
    assert result.returncode == 0, result.stderr


TESTS = [
    test_deref_base_plus_constant_loads_at_offset,
    test_deref_base_plus_variable,
    test_deref_parenthesized_base,
]


if __name__ == "__main__":
    sys.exit(main())
