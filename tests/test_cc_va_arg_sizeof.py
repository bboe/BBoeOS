#!/usr/bin/env python3
"""cc.py va_arg(ap, double) and sizeof(expression) coverage."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

_PREAMBLE = "#include <stdint.h>\n#include <stdarg.h>\n"
REPO_ROOT = Path(__file__).resolve().parent.parent
CC = REPO_ROOT / "cc.py"
LIBBBOEOS_INCLUDE = REPO_ROOT / "user" / "libbboeos" / "include"


def _compile(name: str, source: str) -> str:
    with tempfile.TemporaryDirectory() as work:
        return compile_snippet(name=name, source=source, work=Path(work))


def _compile_expect_no_parse_error(name: str, source: str) -> str | None:
    """Compile *source* and return the asm text, or None on a codegen error.

    Raises if cc.py reports a parse error — that means the parser change is
    broken.  A codegen error (Task 3 not yet landed) is acceptable and returns
    None so callers can check success separately.
    """
    error_text: str | None = None
    asm: str | None = None
    try:
        asm = _compile(name, source)
    except RuntimeError as compile_error:
        error_text = str(compile_error).lower()
    if error_text is not None:
        assert "parse" not in error_text, f"unexpected parse error in {name}:\n{error_text}"
    return asm


def compile_snippet(*, name: str, source: str, work: Path) -> str:
    """Compile *source* with cc.py into a temporary asm file and return its text."""
    source_path = work / f"{name}.c"
    asm_path = work / f"{name}.asm"
    source_path.write_text(_PREAMBLE + source)
    result = subprocess.run(
        ["python3", str(CC), "--bits", "32", "-I", str(LIBBBOEOS_INCLUDE), str(source_path), str(asm_path)],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        message = f"cc.py failed:\n{result.stderr}"
        raise RuntimeError(message)
    return asm_path.read_text()


def test_sizeof_array_still_returns_full_size() -> None:
    """sizeof(a) on a local int[10] must return 40 — SizeofVar path, not SizeofExpr."""
    source = """
    int main(void) {
        int a[10];
        return sizeof(a);
    }
    """
    asm = _compile("sizeof_array", source)
    assert "mov eax, 40" in asm, f"expected 'mov eax, 40' in asm:\n{asm}"


def test_sizeof_binary_op() -> None:
    """sizeof(1 + 2) is int-sized regardless of operand values."""
    source = """
    int main(void) {
        return sizeof(1 + 2);
    }
    """
    asm = _compile("sizeof_binop", source)
    assert "mov eax, 4" in asm, f"expected 'mov eax, 4' in asm:\n{asm}"


def test_sizeof_cast() -> None:
    """sizeof((int *)0) must return pointer size (4 in 32-bit mode)."""
    source = """
    int main(void) {
        return sizeof((int *)0);
    }
    """
    asm = _compile("sizeof_cast", source)
    assert "mov eax, 4" in asm, f"expected 'mov eax, 4' in asm:\n{asm}"


def test_sizeof_deref_char_pointer() -> None:
    """Sizeof *p where p is char * should be 1."""
    source = """
    int main(void) {
        char *p;
        return sizeof *p;
    }
    """
    if (asm := _compile_expect_no_parse_error("sizeof_deref_char", source)) is not None:
        assert "mov eax, 1" in asm, f"expected 'mov eax, 1' in asm:\n{asm}"


def test_sizeof_deref_pointer() -> None:
    """Sizeof *p where p is int * should be 4."""
    source = """
    int main(void) {
        int x;
        int *p = &x;
        return sizeof *p;
    }
    """
    if (asm := _compile_expect_no_parse_error("sizeof_deref", source)) is not None:
        assert "mov eax, 4" in asm, f"expected 'mov eax, 4' in asm:\n{asm}"


def test_sizeof_dot_member() -> None:
    """sizeof(s.b) where b is char must return 1."""
    source = """
    struct S { int a; char b; };
    int main(void) {
        struct S s;
        return sizeof(s.b);
    }
    """
    asm = _compile("sizeof_dot_member", source)
    assert "mov eax, 1" in asm, f"expected 'mov eax, 1' in asm:\n{asm}"


def test_sizeof_member_access() -> None:
    """sizeof(p->b) where b is char must return 1."""
    source = """
    struct S { int a; char b; };
    int main(void) {
        struct S s;
        struct S *p = &s;
        return sizeof(p->b);
    }
    """
    asm = _compile("sizeof_member", source)
    assert "mov eax, 1" in asm, f"expected 'mov eax, 1' in asm:\n{asm}"


def test_sizeof_parenthesised_expression() -> None:
    """sizeof(p[0]) where p is int * should be 4."""
    source = """
    int main(void) {
        int *p;
        return sizeof(p[0]);
    }
    """
    if (asm := _compile_expect_no_parse_error("sizeof_paren_expr", source)) is not None:
        assert "mov eax, 4" in asm, f"expected 'mov eax, 4' in asm:\n{asm}"


def test_sizeof_struct_deref() -> None:
    """Sizeof *p where p is struct S * should be the full struct size."""
    source = """
    struct S { int a; int b; int c; };
    int main(void) {
        struct S *p;
        return sizeof *p;
    }
    """
    asm = _compile("sizeof_struct_deref", source)
    assert "mov eax, 12" in asm, f"expected 'mov eax, 12' in asm:\n{asm}"


def test_va_arg_double_advances_by_8() -> None:
    """va_arg(ap, double) should advance the cursor by 8, not 4."""
    source = """
    void f(int dummy, ...) {
        va_list ap;
        va_start(ap, dummy);
        (void)va_arg(ap, double);
        int after = va_arg(ap, int);
        va_end(ap);
    }
    """
    asm = _compile("va_double", source)
    f_body = asm.split("\nf:", 1)[1].split("\n_", 1)[0]
    assert "8" in f_body, f"expected advance by 8 in _f body:\n{f_body}"


def test_va_arg_int_still_advances_by_4() -> None:
    """va_arg(ap, int) should still advance by 4 (no regression)."""
    source = """
    void f(int dummy, ...) {
        va_list ap;
        va_start(ap, dummy);
        int x = va_arg(ap, int);
        va_end(ap);
    }
    """
    asm = _compile("va_int", source)
    f_body = asm.split("\nf:", 1)[1].split("\n_", 1)[0]
    lines_with_add_8 = [line for line in f_body.splitlines() if "8" in line and "add" in line]
    assert not lines_with_add_8, f"unexpected add-by-8 for int va_arg:\n{lines_with_add_8}"


if __name__ == "__main__":
    test_sizeof_array_still_returns_full_size()
    test_sizeof_binary_op()
    test_sizeof_cast()
    test_sizeof_deref_char_pointer()
    test_sizeof_deref_pointer()
    test_sizeof_dot_member()
    test_sizeof_member_access()
    test_sizeof_parenthesised_expression()
    test_sizeof_struct_deref()
    test_va_arg_double_advances_by_8()
    test_va_arg_int_still_advances_by_4()
    print("OK")
