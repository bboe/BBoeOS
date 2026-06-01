#!/usr/bin/env python3
"""cc.py ``ptr->field[i][j]`` — double-indexing a pointer-of-pointer member.

kilo's editorSelectSyntaxHighlight does ``s->filematch[i][0]`` (a
``char **`` member chased twice).  Var double-index (``m[i][j]``) and
single member index (``s->field[i]``) already worked; the chained ``[j]``
after a member index is new.  Stage 1 reuses MemberIndex to load the
outer ``T *`` element; stage 2 indexes into it, sized by ``sizeof(T)``.

Usage:
    tests/test_cc_member_double_index.py
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
    with tempfile.TemporaryDirectory(prefix="test_cc_member_double_index_") as temporary_directory:
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


def test_char_double_pointer_member_byte_load(*, work: Path) -> None:
    """``s->filematch[i][0]`` (char **) loads a byte from the inner pointer."""
    result, asm = _compile(
        source=("struct s { char **filematch; };\nint f(struct s *s, int i){ return s->filematch[i][0]; }\n"),
        work=work,
    )
    assert result.returncode == 0, result.stderr
    assert re.search(r"movzx \w+, byte \[", asm), f"expected a byte inner load:\n{asm}"


def test_int_double_pointer_member_scales_inner_by_four(*, work: Path) -> None:
    """``s->grid[i][j]`` (int **) scales the inner index by 4."""
    result, asm = _compile(
        source=("struct s { int pad; int **grid; };\nint f(struct s *s, int i, int j){ return s->grid[i][j]; }\n"),
        work=work,
    )
    assert result.returncode == 0, result.stderr
    # Two shifts: outer index (*4 for the char* element) and inner (*4 for int).
    assert asm.count("shl ") >= 2, f"expected outer+inner index scaling:\n{asm}"


def test_member_double_index_dot_form_constant(*, work: Path) -> None:
    """``s.m[2][3]`` (dot form, constant indices) compiles."""
    result, _ = _compile(
        source="struct s { char **m; };\nchar f(struct s s){ return s.m[2][3]; }\n",
        work=work,
    )
    assert result.returncode == 0, result.stderr


TESTS = [
    test_char_double_pointer_member_byte_load,
    test_int_double_pointer_member_scales_inner_by_four,
    test_member_double_index_dot_form_constant,
]


if __name__ == "__main__":
    sys.exit(main())
