#!/usr/bin/env python3
"""cc.py bitfield codegen + negative-case coverage.

Runs cc.py over small C snippets that exercise uint8_t bitfield reads,
writes, sizeof, and the 1-bit literal-store peephole, plus negative
cases (run overflow, non-uint8_t container, width out of range,
&bitfield).

Usage:
    tests/test_cc_bitfields.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CC = REPO_ROOT / "cc.py"
INCLUDE_DIR = REPO_ROOT / "kernel" / "include"


def compile_expect_fail(*, message_fragment: str, name: str, source: str, work: Path) -> None:
    """Run cc.py; assert it exits non-zero and stderr contains ``message_fragment``."""
    source_path = work / f"{name}.c"
    asm_path = work / f"{name}.asm"
    source_path.write_text(source)
    result = subprocess.run(
        ["python3", str(CC), "--bits", "32", str(source_path), str(asm_path)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode != 0, f"expected cc.py to reject {name!r}; got exit 0"
    assert message_fragment in result.stderr, f"expected {message_fragment!r} in stderr; got {result.stderr!r}"


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
    with tempfile.TemporaryDirectory(prefix="test_cc_bitfields_") as temporary_directory:
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


def test_addressof_bitfield_rejected(*, work: Path) -> None:
    """&bitfield_member must be rejected with the canonical error message."""
    compile_expect_fail(
        message_fragment="cannot take address of bitfield",
        name="addressof_bitfield",
        source=("struct f { uint8_t a : 1; };\nstruct f global_x;\nint main() { uint8_t *p = &global_x.a; return 0; }\n"),
        work=work,
    )


def test_anonymous_padding_advances_offset(*, work: Path) -> None:
    """Anonymous bitfield gap advances bit_offset so the next named field is correct."""
    asm = compile_snippet(
        name="anon_padding",
        source=("struct s { uint8_t a : 1; uint8_t : 3; uint8_t b : 4; };\nstruct s global_f;\nint main() { return global_f.b; }\n"),
        work=work,
    )
    # b starts at bit_offset 4 (1 + 3 anonymous padding bits).
    body = asm.split("main:", 1)[1]
    body = body.split("\n_", 1)[0] if "\n_" in body else body
    assert "shr al, 4" in body.lower(), f"expected 'shr al, 4' in main body:\n{body}"


def test_non_uint8_container_rejected(*, work: Path) -> None:
    """A bitfield container other than uint8_t must be rejected."""
    compile_expect_fail(
        message_fragment="must be uint8_t",
        name="non_uint8_container",
        source="struct bad { uint16_t a : 4; };\nint main() { return 0; }\n",
        work=work,
    )


def test_read_1bit_at_offset_0(*, work: Path) -> None:
    """Reading a 1-bit field at bit_offset 0 emits 'and al, 1' and no shr."""
    asm = compile_snippet(
        name="read_1bit_offset0",
        source=("struct s { uint8_t a : 1; };\nstruct s global_f;\nint main() { return global_f.a; }\n"),
        work=work,
    )
    body = asm.split("main:", 1)[1]
    body = body.split("\n_", 1)[0] if "\n_" in body else body
    assert "and al, 1" in body.lower(), f"expected 'and al, 1' in main body:\n{body}"
    assert "shr" not in body.lower(), f"unexpected shr in main body (offset 0):\n{body}"


def test_read_4bit_at_offset_4(*, work: Path) -> None:
    """Reading a 4-bit field at bit_offset 4 emits shr then and-mask."""
    asm = compile_snippet(
        name="read_4bit_offset4",
        source=("struct s { uint8_t a : 4; uint8_t c : 4; };\nstruct s global_f;\nint main() { return global_f.c; }\n"),
        work=work,
    )
    body = asm.split("main:", 1)[1]
    body = body.split("\n_", 1)[0] if "\n_" in body else body
    assert "shr al, 4" in body.lower(), f"expected 'shr al, 4' in main body:\n{body}"
    assert "and al, 15" in body.lower() or "and al, 0xf" in body.lower(), f"expected 'and al, 15' or 'and al, 0xf' in main body:\n{body}"


def test_run_overflow_rejected(*, work: Path) -> None:
    """A bitfield run that exceeds 8 bits must be rejected."""
    compile_expect_fail(
        message_fragment="run exceeds 8 bits",
        name="run_overflow",
        source="struct bad { uint8_t a : 4; uint8_t b : 5; };\nint main() { return 0; }\n",
        work=work,
    )


def test_sizeof_mixed_run(*, work: Path) -> None:
    """Sizeof a struct with a bitfield run plus a regular byte field returns 2."""
    asm = compile_snippet(
        name="sizeof_mixed",
        source=("struct s { uint8_t a : 4; uint8_t : 4; uint8_t b; };\nint main() { return sizeof(struct s); }\n"),
        work=work,
    )
    body = asm.split("main:", 1)[1]
    body = body.split("\n_", 1)[0] if "\n_" in body else body
    assert "mov eax, 2" in body.lower() or "mov ax, 2" in body.lower(), f"expected 'mov eax, 2' or 'mov ax, 2' in main body:\n{body}"


def test_sizeof_packed_byte(*, work: Path) -> None:
    """Sizeof a struct whose bitfields sum to exactly 8 bits returns 1."""
    asm = compile_snippet(
        name="sizeof_packed",
        source=("struct s { uint8_t a : 4; uint8_t b : 4; };\nint main() { return sizeof(struct s); }\n"),
        work=work,
    )
    body = asm.split("main:", 1)[1]
    body = body.split("\n_", 1)[0] if "\n_" in body else body
    assert "mov eax, 1" in body.lower() or "mov ax, 1" in body.lower(), f"expected 'mov eax, 1' or 'mov ax, 1' in main body:\n{body}"


def test_width_too_large_rejected(*, work: Path) -> None:
    """A bitfield width of 9 (out of 1..8 range) must be rejected."""
    compile_expect_fail(
        message_fragment="width must be 1..8",
        name="width_too_large",
        source="struct bad { uint8_t a : 9; };\nint main() { return 0; }\n",
        work=work,
    )


def test_write_1bit_literal_0_peephole(*, work: Path) -> None:
    """Writing literal 0 to a 1-bit field emits the 'and byte' peephole."""
    asm = compile_snippet(
        name="write_1bit_0",
        source=("struct s { uint8_t a : 1; };\nstruct s global_f;\nint main() { global_f.a = 0; return 0; }\n"),
        work=work,
    )
    assert "and byte" in asm.lower(), f"expected 'and byte' peephole in asm:\n{asm}"


def test_write_1bit_literal_1_peephole(*, work: Path) -> None:
    """Writing literal 1 to a 1-bit field emits the 'or byte' peephole."""
    asm = compile_snippet(
        name="write_1bit_1",
        source=("struct s { uint8_t a : 1; };\nstruct s global_f;\nint main() { global_f.a = 1; return 0; }\n"),
        work=work,
    )
    assert "or byte" in asm.lower(), f"expected 'or byte' peephole in asm:\n{asm}"


def test_write_multibit_through_ebx_preserves_base(*, work: Path) -> None:
    """Multi-bit write through a struct-pointer in EBX must not clobber EBX.

    Regression: ``_emit_bitfield_write`` used to stash the rhs in BL,
    which is the low byte of EBX — the same register the arrow path
    loads the struct pointer into.  The subsequent ``mov al, [ebx]``
    then read from a corrupted address.  Fix: use CL as scratch.
    """
    asm = compile_snippet(
        name="write_multibit_ebx",
        source=(
            "struct s { uint8_t a : 1; uint8_t r : 3; uint8_t p : 2; };\nvoid set_r(struct s *c) { c->r = 5; }\nint main() { return 0; }\n"
        ),
        work=work,
    )
    body = asm.split("set_r:", 1)[1].split("\n_", 1)[0].lower()
    assert "mov cl, al" in body, f"expected 'mov cl, al' (CL scratch) in:\n{body}"
    assert "mov bl, al" not in body, f"BL scratch would clobber EBX base; got:\n{body}"


TESTS = (
    test_addressof_bitfield_rejected,
    test_anonymous_padding_advances_offset,
    test_non_uint8_container_rejected,
    test_read_1bit_at_offset_0,
    test_read_4bit_at_offset_4,
    test_run_overflow_rejected,
    test_sizeof_mixed_run,
    test_sizeof_packed_byte,
    test_width_too_large_rejected,
    test_write_multibit_through_ebx_preserves_base,
    test_write_1bit_literal_0_peephole,
    test_write_1bit_literal_1_peephole,
)


if __name__ == "__main__":
    sys.exit(main())
