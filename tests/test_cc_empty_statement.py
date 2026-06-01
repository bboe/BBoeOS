#!/usr/bin/env python3
"""cc.py empty-statement (``;``) support.

A bare ``;`` is a no-op statement, most often the body of a spin loop
(``while ((n = read(fd,&c,1)) == 0);`` in kilo's editorReadKey).  cc.py's
statement parser previously raised "expected statement, got SEMI"; it now
models the empty statement as an empty Compound that lowers to no code.

Usage:
    tests/test_cc_empty_statement.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CC = REPO_ROOT / "cc.py"


def _compile(*, source: str, work: Path) -> subprocess.CompletedProcess:
    source_path = work / "input.c"
    asm_path = work / "input.asm"
    source_path.write_text(source)
    return subprocess.run(
        ["python3", str(CC), "--bits", "32", "--permissive", str(source_path), str(asm_path)],
        capture_output=True,
        check=False,
        text=True,
    )


def main() -> int:
    """Run every test_* under a shared tempdir; return 0 iff all pass."""
    fail_count = 0
    with tempfile.TemporaryDirectory(prefix="test_cc_empty_statement_") as temporary_directory:
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


def test_bare_empty_statement_in_block(*, work: Path) -> None:
    """A standalone ``;`` inside a block is accepted."""
    result = _compile(source="int f(void){ int x = 1; ; return x; }\n", work=work)
    assert result.returncode == 0, result.stderr


def test_empty_while_body(*, work: Path) -> None:
    """``while (cond);`` (spin loop with an empty body) compiles."""
    result = _compile(
        source="int read(int, void *, unsigned);\nint f(int fd){ char c; int n; while ((n = read(fd,&c,1)) == 0); return n; }\n",
        work=work,
    )
    assert result.returncode == 0, result.stderr


TESTS = [
    test_bare_empty_statement_in_block,
    test_empty_while_body,
]


if __name__ == "__main__":
    sys.exit(main())
