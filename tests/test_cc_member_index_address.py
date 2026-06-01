#!/usr/bin/env python3
"""cc.py ``&obj.field[i]`` / ``&ptr->field[i]`` — address of an indexed member.

Taking the address of one element of an array/pointer-typed struct
member (``editorRowHasOpenComment(&E.row[row->idx-1])`` in kilo) reuses
MemberIndex's element-address scaling but ``lea``s the address instead of
loading the element.  Because no value is loaded, the element may be any
size — a struct-sized element is scaled with a general ``imul`` and must
appear in the emitted asm so the index is not treated as a byte offset.

Usage:
    tests/test_cc_member_index_address.py
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
    with tempfile.TemporaryDirectory(prefix="test_cc_member_index_address_") as temporary_directory:
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


def test_address_of_arrow_member_element(*, work: Path) -> None:
    """``&ptr->field[i]`` (arrow form) compiles."""
    result, _ = _compile(
        source=(
            "struct e { int a, b, c; };\n"
            "struct cfg { struct e *row; };\n"
            "int g(struct e *e);\n"
            "int f(struct cfg *c, int i){ return g(&c->row[i]); }\n"
        ),
        work=work,
    )
    assert result.returncode == 0, result.stderr


def test_address_of_large_element_scales_with_imul(*, work: Path) -> None:
    """A struct-sized element (12 bytes) scales by ``imul``, not a shift."""
    result, asm = _compile(
        source=(
            "struct e { int a, b, c; };\n"
            "struct cfg { struct e *row; };\n"
            "struct cfg E;\n"
            "int g(struct e *e);\n"
            "int f(int i){ return g(&E.row[i]); }\n"
        ),
        work=work,
    )
    assert result.returncode == 0, result.stderr
    assert re.search(r"imul\s+\w+,\s*12\b", asm), f"expected `imul reg, 12` element scaling:\n{asm}"
    assert "lea " in asm, f"expected a lea for the element address:\n{asm}"


def test_address_of_member_element_compiles(*, work: Path) -> None:
    """``&E.row[i]`` (dot form, single-int element) compiles."""
    result, _ = _compile(
        source=(
            "struct e { int x; };\n"
            "struct cfg { struct e *row; };\n"
            "struct cfg E;\n"
            "int g(struct e *e);\n"
            "int f(int i){ return g(&E.row[i]); }\n"
        ),
        work=work,
    )
    assert result.returncode == 0, result.stderr


TESTS = [
    test_address_of_arrow_member_element,
    test_address_of_large_element_scales_with_imul,
    test_address_of_member_element_compiles,
]


if __name__ == "__main__":
    sys.exit(main())
