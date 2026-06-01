#!/usr/bin/env python3
"""cc.py struct-shape parser extensions for third-party C.

Covers two constructs ubiquitous in upstream C (kilo, lua, Doom) that
cc.py's parser previously rejected:

* ``typedef struct [TAG] { ... } ALIAS;`` — a struct *definition* bundled
  with a typedef, both the tagged and anonymous forms.
* comma-separated struct members sharing one base type (``int cx, cy;``).

Usage:
    tests/test_cc_typedef_struct.py
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
    with tempfile.TemporaryDirectory(prefix="test_cc_typedef_struct_") as temporary_directory:
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


def test_anonymous_typedef_struct(*, work: Path) -> None:
    """``typedef struct { ... } s;`` (no tag) compiles and the alias resolves."""
    result = _compile(
        source="typedef struct { void *a; int b; } s;\nint f(void){ s x; x.b = 7; return x.b; }\n",
        work=work,
    )
    assert result.returncode == 0, result.stderr


def test_multi_field_struct_members(*, work: Path) -> None:
    """``int r, g, b;`` declares three distinct fields sharing the base type."""
    result = _compile(
        source="struct c { int r, g, b; };\nint f(void){ struct c x; x.r=1; x.g=2; x.b=3; return x.r+x.g+x.b; }\n",
        work=work,
    )
    assert result.returncode == 0, result.stderr


def test_plain_typedef_of_existing_tag_still_parses(*, work: Path) -> None:
    """Regression: ``typedef struct foo foo_t;`` (a reference, not a definition)."""
    result = _compile(
        source="struct foo { int a; };\ntypedef struct foo foo_t;\nint f(void){ foo_t x; x.a=5; return x.a; }\n",
        work=work,
    )
    assert result.returncode == 0, result.stderr


def test_tagged_typedef_struct(*, work: Path) -> None:
    """``typedef struct erow { ... } erow;`` (tag == alias) compiles and is usable."""
    result = _compile(
        source="typedef struct erow { char *b; int len; } erow;\nint f(void){ erow r; r.len = 3; return r.len; }\n",
        work=work,
    )
    assert result.returncode == 0, result.stderr


TESTS = [
    test_anonymous_typedef_struct,
    test_multi_field_struct_members,
    test_plain_typedef_of_existing_tag_still_parses,
    test_tagged_typedef_struct,
]


if __name__ == "__main__":
    sys.exit(main())
