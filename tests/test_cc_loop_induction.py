#!/usr/bin/env python3
"""cc.py loop induction-variable codegen correctness.

Regression coverage for the SSA / opaque-``Block`` interaction that
miscompiled unit-stride loops which *store the induction variable
itself* (``for (i = 0; i < n; i++) buffer[i] = i;``).  The ``i++`` step
lowers to an opaque ``Block(IncrementDecrement)`` whose only mention of
``i`` is the bare ``target_name`` string; when the SSA eligibility
filter could not see that write it versioned ``i`` as loop-invariant,
collapsing the guard to ``cmp 0, n`` (an infinite loop for ``n > 0``)
and the stored value to a constant ``0``.

These loops are *not* rep-string idioms (they store the advancing
index, not a constant or a copied source), so the rep-string recognizer
correctly leaves them on the scalar path — exactly where the bug lived.

Usage:
    tests/test_cc_loop_induction.py
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CC = REPO_ROOT / "cc.py"
INCLUDE_DIR = REPO_ROOT / "kernel" / "include"


def _loop_body(asm: str) -> str:
    """Return the text between the first ``floop`` label and the matching ``fend`` label."""
    start = re.search(r"^\._ir_floop\d+:", asm, re.MULTILINE)
    end = re.search(r"^\._ir_fend\d+:", asm, re.MULTILINE)
    assert start is not None and end is not None, f"no for-loop labels in:\n{asm}"
    return asm[start.end() : end.start()]


def compile_snippet(*, name: str, source: str, work: Path) -> str:
    """Run cc.py (``--target kernel``, 32-bit) on ``source``; return the emitted asm."""
    source_path = work / f"{name}.c"
    asm_path = work / f"{name}.asm"
    source_path.write_text(source)
    subprocess.run(
        ["python3", str(CC), "--bits", "32", "--target", "kernel", str(source_path), str(asm_path)],
        capture_output=True,
        check=True,
        text=True,
    )
    return asm_path.read_text()


def main() -> int:
    """Run every test_* under a shared tempdir; return 0 iff all pass."""
    fail_count = 0
    with tempfile.TemporaryDirectory(prefix="test_cc_loop_induction_") as temporary_directory:
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


def test_store_index_guard_is_not_constant_zero(*, work: Path) -> None:
    """``for (i=0;i<n;i++) b[i]=i;`` must compare the live counter, not a re-zeroed register.

    The miscompile emitted ``xor eax, eax`` immediately followed by
    ``cmp eax, [ebp-...]`` — a ``cmp 0, n`` guard that loops forever.
    """
    asm = compile_snippet(
        name="store_index_guard",
        source="void f(int *b, int n) {\n    int i;\n    for (i = 0; i < n; i++) b[i] = i;\n}\n",
        work=work,
    )
    body = _loop_body(asm)
    bug = re.search(r"xor\s+(\w+),\s*\1\s*\n\s*cmp\s+\1,", body)
    assert bug is None, f"loop guard compares a freshly-zeroed register (cmp 0, n):\n{body}"


def test_store_index_stores_advancing_value(*, work: Path) -> None:
    """The stored value must be the advancing index, not a constant ``0``.

    The miscompile collapsed ``b[i] = i`` to ``mov dword [reg], 0`` — both
    the wrong (constant) value and the wrong (un-indexed, offset-0) address.
    """
    asm = compile_snippet(
        name="store_index_value",
        source="void f(int *b, int n) {\n    int i;\n    for (i = 0; i < n; i++) b[i] = i;\n}\n",
        work=work,
    )
    body = _loop_body(asm)
    constant_store = re.search(r"mov\s+(?:dword\s+)?\[[^\]]+\],\s*0\b", body)
    assert constant_store is None, f"array store writes a constant 0 instead of the index:\n{body}"


TESTS = [
    test_store_index_guard_is_not_constant_zero,
    test_store_index_stores_advancing_value,
]


if __name__ == "__main__":
    sys.exit(main())
