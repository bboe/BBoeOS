#!/usr/bin/env python3
"""cc.py stack-local struct codegen coverage.

Runs cc.py over small C snippets that exercise stack-local struct value
declarations, dot-access reads and writes, ``= { 0 }`` and designated
initializers, ``&local`` / ``&local.field`` address-of, ``sizeof`` on a
local struct variable, indexed access on arrays of local structs, bitfield
reads on local struct bytes, and negatives (positional init rejection).

Usage:
    tests/test_cc_local_structs.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

# Auto-prepended so inline C snippets get standard typedefs without
# each test repeating the include directive.
_STDINT_PREAMBLE = "#include <stdint.h>\n"
REPO_ROOT = Path(__file__).resolve().parent.parent
CC = REPO_ROOT / "cc.py"
INCLUDE_DIR = REPO_ROOT / "kernel" / "include"

LIBBBOEOS_INCLUDE = REPO_ROOT / "user" / "libbboeos" / "include"


def compile_expect_fail(*, message_fragment: str, name: str, source: str, work: Path) -> None:
    """Run cc.py; assert it exits non-zero and stderr contains ``message_fragment``."""
    source_path = work / f"{name}.c"
    asm_path = work / f"{name}.asm"
    source_path.write_text(_STDINT_PREAMBLE + source)
    result = subprocess.run(
        ["python3", str(CC), "--bits", "32", "-I", str(LIBBBOEOS_INCLUDE), str(source_path), str(asm_path)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode != 0, f"expected cc.py to reject {name!r}; got exit 0"
    combined = result.stderr + result.stdout
    assert message_fragment in combined, f"expected {message_fragment!r} in output; got {combined!r}"


def compile_snippet(*, name: str, source: str, work: Path) -> str:
    """Run cc.py + nasm on ``source``; return the cc.py-emitted asm text."""
    source_path = work / f"{name}.c"
    asm_path = work / f"{name}.asm"
    bin_path = work / f"{name}.bin"
    source_path.write_text(_STDINT_PREAMBLE + source)
    subprocess.run(
        ["python3", str(CC), "--bits", "32", "-I", str(LIBBBOEOS_INCLUDE), str(source_path), str(asm_path)],
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
    with tempfile.TemporaryDirectory(prefix="test_cc_local_structs_") as temporary_directory:
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


def test_addressof_local_bitfield_rejected(*, work: Path) -> None:
    """``&c.bitfield_member`` must be rejected with 'cannot take address of bitfield'."""
    compile_expect_fail(
        message_fragment="cannot take address of bitfield",
        name="addressof_local_bitfield",
        source=(
            "struct flags { unsigned char a : 1; unsigned char b : 7; };\n"
            "int wrap() { struct flags f; unsigned char *p = &f.a; return 0; }\n"
            "int main() { return wrap(); }\n"
        ),
        work=work,
    )


def test_addressof_local_field_emits_lea(*, work: Path) -> None:
    """``&local_struct.regular_field`` emits a ``lea`` against the frame pointer."""
    asm = compile_snippet(
        name="addressof_local_field",
        source=(
            "struct point { int x; int y; };\n"
            "int sum(int *a, int *b) { return *a + *b; }\n"
            "int wrap() {\n"
            "    struct point c;\n"
            "    c.x = 3;\n"
            "    c.y = 4;\n"
            "    return sum(&c.x, &c.y);\n"
            "}\n"
            "int main() { return wrap(); }\n"
        ),
        work=work,
    )
    wrap_body = asm.split("wrap:", 1)[1]
    wrap_body = wrap_body.split("\nmain:", 1)[0]
    assert "lea eax, [ebp-" in wrap_body.lower(), f"expected 'lea eax, [ebp-...' for &c.x in wrap body:\n{wrap_body}"


def test_array_of_structs_indexed_access(*, work: Path) -> None:
    """``arr[2].field`` on a local struct array folds the constant index into the frame displacement (no runtime imul)."""
    asm = compile_snippet(
        name="array_of_structs",
        source=(
            "struct point { int x; int y; };\n"
            "int wrap() {\n"
            "    struct point arr[3];\n"
            "    arr[2].x = 99;\n"
            "    return arr[2].x;\n"
            "}\n"
            "int main() { return wrap(); }\n"
        ),
        work=work,
    )
    wrap_body = asm.split("wrap:", 1)[1]
    wrap_body = wrap_body.split("\nmain:", 1)[0]
    # The recursive address resolver folds the constant index at compile time:
    # arr[2].x == base + 2 * sizeof(struct point) (== 16) + offsetof(x) (== 0),
    # so the element offset 16 lands directly in the frame displacement and no
    # runtime ``imul`` for index * struct_size is emitted.
    assert "imul" not in wrap_body.lower(), f"constant index should fold, not emit imul:\n{wrap_body}"
    assert "+16" in wrap_body, f"expected folded element offset (2 * sizeof(point) == 16):\n{wrap_body}"
    # The frame-relative base must reference ebp.
    assert "ebp-" in wrap_body, f"expected 'ebp-N' frame-relative addressing in wrap body:\n{wrap_body}"


def test_bitfield_local_byte_uses_frame_addressing(*, work: Path) -> None:
    """Bitfield reads and writes on a local struct use ``[ebp-N]`` addressing."""
    asm = compile_snippet(
        name="bitfield_local_byte",
        source=(
            "struct flags { unsigned char a : 1; unsigned char b : 1; unsigned char c : 6; };\n"
            "int wrap() {\n"
            "    struct flags f;\n"
            "    f.a = 1;\n"
            "    f.b = 0;\n"
            "    return f.a;\n"
            "}\n"
            "int main() { return wrap(); }\n"
        ),
        work=work,
    )
    wrap_body = asm.split("wrap:", 1)[1]
    wrap_body = wrap_body.split("\nmain:", 1)[0]
    assert "[ebp-" in wrap_body, f"expected '[ebp-N]' frame-relative addressing in wrap body:\n{wrap_body}"
    assert "and al, 1" in wrap_body.lower(), f"expected 'and al, 1' for 1-bit field read in wrap body:\n{wrap_body}"


def test_designated_init_multi_field_const_fold(*, work: Path) -> None:
    """Multi-field designated init for a bitfield struct folds to a single immediate.

    After const-fold + sequential-collapse the multi-field init lands
    as one ``mov byte [ebp-N], 23`` (the packed value
    ``.a=1 | .b=1<<1 | .c=5<<2 = 23``).  When the local's only use is
    a bitfield read on the just-initialised slot,
    ``peephole_fold_single_write_byte_constant_local`` propagates the
    immediate to the consumer and ``peephole_dead_temp_slots`` drops
    the now-orphan store: AL goes straight to ``23`` and the bitfield
    extract (``and al, 1`` for ``f.a``) runs on the immediate.
    """
    asm = compile_snippet(
        name="designated_init_multi",
        source=(
            "struct flags { unsigned char a : 1; unsigned char b : 1; unsigned char c : 6; };\n"
            "int main() {\n"
            "    struct flags f = { .a = 1, .b = 1, .c = 5 };\n"
            "    return f.a;\n"
            "}\n"
        ),
        work=work,
    )
    body = asm.split("main:", 1)[1]
    body = body.split("\n_", 1)[0] if "\n_" in body else body
    assert "mov al, 23" in body, f"expected folded immediate ``mov al, 23``:\n{body}"
    assert "and al, 1" in body, f"expected bitfield extract for ``f.a``:\n{body}"
    assert "[ebp-" not in body, f"frame slot should have folded away:\n{body}"


def test_designated_init_single_bitfield(*, work: Path) -> None:
    """Single-field designated init folds past the frame slot when its only use is a bitfield read.

    The init is one byte, the only use is ``return f.a;`` (a bitfield
    extract on the same slot), so the whole-function single-write
    fold propagates the immediate directly: AL loads ``1`` then the
    bitfield extract masks bit 0.  No frame slot survives.
    """
    asm = compile_snippet(
        name="designated_init_single",
        source=(
            "struct flags { unsigned char a : 1; unsigned char b : 7; };\n"
            "int main() {\n    struct flags f = { .a = 1 };\n    return f.a;\n}\n"
        ),
        work=work,
    )
    body = asm.split("main:", 1)[1]
    body = body.split("\n_", 1)[0] if "\n_" in body else body
    assert "mov al, 1" in body, f"expected folded immediate ``mov al, 1``:\n{body}"
    assert "and al, 1" in body, f"expected bitfield extract for ``f.a``:\n{body}"
    assert "[ebp-" not in body, f"frame slot should have folded away:\n{body}"


def test_dot_read_write_regular_field(*, work: Path) -> None:
    """Dot-access write and read on a local struct value uses ``[ebp-N]`` addressing."""
    asm = compile_snippet(
        name="dot_read_write",
        source=(
            "struct point { int x; int y; };\n"
            "int wrap() {\n"
            "    struct point c;\n"
            "    c.x = 42;\n"
            "    c.y = 7;\n"
            "    return c.x;\n"
            "}\n"
            "int main() { return wrap(); }\n"
        ),
        work=work,
    )
    wrap_body = asm.split("wrap:", 1)[1]
    wrap_body = wrap_body.split("\nmain:", 1)[0]
    assert "mov [ebp-" in wrap_body.lower(), f"expected 'mov [ebp-N]' store for c.x write in wrap body:\n{wrap_body}"
    assert "mov eax, [ebp-" in wrap_body.lower(), f"expected 'mov eax, [ebp-N]' load for c.x read in wrap body:\n{wrap_body}"


def test_sizeof_local_struct(*, work: Path) -> None:
    """``sizeof(c)`` on a local struct variable returns the struct's byte size."""
    asm = compile_snippet(
        name="sizeof_local_struct",
        source=("struct point { int x; int y; };\nint main() {\n    struct point c;\n    return sizeof(c);\n}\n"),
        work=work,
    )
    body = asm.split("main:", 1)[1]
    body = body.split("\n_", 1)[0] if "\n_" in body else body
    # struct point has two int fields → 8 bytes on 32-bit target.
    assert "mov eax, 8" in body.lower() or "mov ax, 8" in body.lower(), f"expected 'mov eax, 8' for sizeof(c) on struct point; got:\n{body}"


def test_zero_init_emits_byte_stores(*, work: Path) -> None:
    """``struct foo c = { 0 };`` emits exactly sizeof(struct foo) zero-byte stores."""
    asm = compile_snippet(
        name="zero_init",
        source=("struct point { int x; int y; };\nint main() {\n    struct point c = { 0 };\n    return c.x;\n}\n"),
        work=work,
    )
    body = asm.split("main:", 1)[1]
    body = body.split("\n_", 1)[0] if "\n_" in body else body
    # struct point is 8 bytes → expect exactly 8 zero-byte stores.
    zero_stores = [line.strip() for line in body.splitlines() if "mov byte" in line.lower() and ", 0" in line]
    assert len(zero_stores) == 8, f"expected 8 zero-byte stores for 'struct point c = {{{{ 0 }}}}'; got {len(zero_stores)}:\n{body}"


TESTS = (
    test_addressof_local_bitfield_rejected,
    test_addressof_local_field_emits_lea,
    test_array_of_structs_indexed_access,
    test_bitfield_local_byte_uses_frame_addressing,
    test_designated_init_multi_field_const_fold,
    test_designated_init_single_bitfield,
    test_dot_read_write_regular_field,
    test_sizeof_local_struct,
    test_zero_init_emits_byte_stores,
)


if __name__ == "__main__":
    sys.exit(main())
