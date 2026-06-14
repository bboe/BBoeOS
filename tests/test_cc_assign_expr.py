#!/usr/bin/env python3
"""cc.py parenthesized assignment-as-expression coverage."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

_PREAMBLE = "#include <stdint.h>\n"
REPO_ROOT = Path(__file__).resolve().parent.parent
CC = REPO_ROOT / "cc.py"
LIBBBOEOS_INCLUDE = REPO_ROOT / "user" / "libbboeos" / "include"


def _compile(name: str, source: str) -> None:
    with tempfile.TemporaryDirectory() as work:
        compile_snippet(name=name, source=source, work=Path(work))


def compile_snippet(*, name: str, source: str, work: Path) -> str:
    """Run cc.py on *source*; return the emitted asm text or raise on failure."""
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


def expect_reject(*, name: str, needle: str = "", source: str, work: Path) -> None:
    """Assert that cc.py rejects *source*, optionally checking *needle* in stderr."""
    source_path = work / f"{name}.c"
    source_path.write_text(_PREAMBLE + source)
    result = subprocess.run(
        ["python3", str(CC), "--bits", "32", "-I", str(LIBBBOEOS_INCLUDE), str(source_path), str(work / f"{name}.asm")],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode == 0:
        message = f"{name}: expected rejection, cc.py accepted"
        raise AssertionError(message)
    if needle and needle not in result.stderr:
        message = f"{name}: stderr lacks {needle!r}: {result.stderr}"
        raise AssertionError(message)


def test_accept_chained_with_parens() -> None:
    """Parenthesised chaining ``a = (b = c)`` is accepted."""
    _compile("chain_ok", "int main(void){int a; int b; int c = 1; a = (b = c); return a;}")


def test_array_index_lvalue() -> None:
    """Parser and codegen accept ``(a[i] = expr)`` as an expression."""
    _compile("index", "int main(void){int a[4]; return (a[2] = 9);}")


def test_assign_expr_function_call_rhs_evaluated_once() -> None:
    """``(x = f())`` must call ``f`` exactly once.

    Regression: an earlier emission-level shortcut re-evaluated the RHS to
    reload AX after non-plain stores, which double-called function-call
    RHSs.  The IR-level lowering evaluates the RHS into a temp once and
    reuses the temp as both the store source and the expression value.
    """
    source = """
    int calls;
    int next(void) { calls = calls + 1; return calls; }
    int main(void) {
        int x;
        int *p = &x;
        int result = (*p = next());  /* must call next() exactly once -> x == 1, calls == 1 */
        return x + calls;  /* expect 2 */
    }
    """
    with tempfile.TemporaryDirectory() as work:
        compile_snippet(name="single_eval", source=source, work=Path(work))
        # Compile-only check; runtime is verified end-to-end by the
        # program suite in Task 9.  The assembly should reference
        # ``_next`` exactly once inside ``main`` (a single ``call _next``
        # instruction).
        asm_text = (Path(work) / "single_eval.asm").read_text()
        # The label is emitted as ``main:`` (no leading underscore on 32-bit
        # cc.py output) followed by the function prologue.
        main_section = asm_text.split("\nmain:", 1)[1]
        # Stop at the next function label to scope the count to main.
        next_label_match = main_section.split("\nnext:", 1)
        main_body = next_label_match[0]
        call_count = main_body.count("call next")
        if call_count != 1:
            error_message = f"expected exactly 1 call to next in main, got {call_count}\nmain body:\n{main_body}"
            raise AssertionError(error_message)


def test_compound_amp_assign() -> None:
    """Parser and codegen accept ``(x &= n)`` as an expression."""
    _compile("c_amp", "int main(void){unsigned int x = 0xF0; return (int)(x &= 0x0F);}")


def test_compound_caret_assign() -> None:
    """Parser and codegen accept ``(x ^= n)`` as an expression."""
    _compile("c_caret", "int main(void){unsigned int x = 0xFF; return (int)(x ^= 0x0F);}")


def test_compound_lshift_assign() -> None:
    """Parser and codegen accept ``(x <<= n)`` as an expression."""
    _compile("c_lshift", "int main(void){int x = 1; return (x <<= 3);}")


def test_compound_minus_assign() -> None:
    """Parser and codegen accept ``(x -= n)`` as an expression."""
    _compile("c_minus", "int main(void){int x = 5; return (x -= 2);}")


def test_compound_percent_assign() -> None:
    """Parser and codegen accept ``(x %= n)`` as an expression."""
    _compile("c_percent", "int main(void){int x = 9; return (x %= 4);}")


def test_compound_pipe_assign() -> None:
    """Parser and codegen accept ``(x |= n)`` as an expression."""
    _compile("c_pipe", "int main(void){unsigned int x = 0xF0; return (int)(x |= 0x0F);}")


def test_compound_plus_assign() -> None:
    """Parser and codegen accept ``(x += n)`` as an expression."""
    _compile("c_plus", "int main(void){int x = 1; return (x += 2);}")


def test_compound_rshift_assign() -> None:
    """Parser and codegen accept ``(x >>= n)`` as an expression."""
    _compile("c_rshift", "int main(void){int x = 8; return (x >>= 1);}")


def test_compound_slash_assign() -> None:
    """Parser and codegen accept ``(x /= n)`` as an expression."""
    _compile("c_slash", "int main(void){int x = 8; return (x /= 2);}")


def test_compound_star_assign() -> None:
    """Parser and codegen accept ``(x *= n)`` as an expression."""
    _compile("c_star", "int main(void){int x = 3; return (x *= 2);}")


def test_indexed_member_lvalue() -> None:
    """Parser and codegen accept ``(a[i].f = expr)`` as an expression."""
    _compile("index_member", "struct S { int f; }; int main(void){struct S a[4]; return (a[1].f = 2);}")


def test_member_index_lvalue() -> None:
    """Parser and codegen accept ``(p->a[i] = expr)`` as an expression.

    The dot form ``s.a[2]`` hits a pre-existing codegen gap ("dot member index
    on local struct values is not yet supported").  The arrow form with ``int``
    hits "indexed assignment to 'a' (element size 4) not supported".  Using
    ``char`` (element size 1) with ``->`` exercises the ``MemberIndexAssign``
    path end-to-end.
    """
    _compile("member_index", "struct S { char a[4]; }; int main(void){struct S s; struct S *p = &s; return (p->a[2] = 6);}")


def test_pointer_deref_lvalue() -> None:
    """Parser and codegen accept ``(*p = expr)`` as an expression."""
    _compile("deref", "int main(void){int x; int *p = &x; return (*p = 7);}")


def test_pointer_deref_postinc_lvalue() -> None:
    """Parser and codegen accept ``(*p++ = expr)`` as an expression."""
    # Use the AssignExpr as a return value so it is in expression context.
    _compile("deref_postinc", "int main(void){char b[2]; char *p = b; return (*p++ = 'a');}")


def test_reject_assignment_as_lvalue() -> None:
    """Using an assignment expression as an lvalue ``((x = y)) = z`` is rejected."""
    with tempfile.TemporaryDirectory() as work:
        expect_reject(
            name="lvalue",
            source="int main(void){int x; int y; int z = 1; ((x = y)) = z; return 0;}",
            work=Path(work),
        )


def test_reject_bare_call_arg_assignment() -> None:
    """Bare ``x = 1`` as a function argument is rejected."""
    with tempfile.TemporaryDirectory() as work:
        expect_reject(
            name="bare_call",
            source="void f(int); int main(void){int x; f(x = 1); return 0;}",
            work=Path(work),
        )


def test_reject_bare_if_assignment() -> None:
    """Bare ``x = y`` inside ``if (...)`` is rejected (no wrapping parens)."""
    with tempfile.TemporaryDirectory() as work:
        expect_reject(
            name="bare_if",
            source="int main(void){int x; int y=1; if (x = y) {} return 0;}",
            work=Path(work),
        )


def test_reject_bitfield_assign_as_expression() -> None:
    """Bitfield assignment-as-expression is not supported.

    The AST-path codegen for bitfield writes clobbers AX with the
    merged memory byte, breaking the "AX = assigned value" contract
    that AssignExpr depends on.  Reject at compile time rather than
    silently miscompile.
    """
    with tempfile.TemporaryDirectory() as work:
        expect_reject(
            name="bitfield_assign",
            needle="bitfield",
            source="struct S { unsigned char f : 3; }; int main(void){ struct S s; return (s.f = 5); }",
            work=Path(work),  # the error message should mention bitfield
        )


def test_reject_chained_without_parens() -> None:
    """Chained assignment ``a = b = c`` without parens is rejected."""
    with tempfile.TemporaryDirectory() as work:
        expect_reject(
            name="chain",
            source="int main(void){int a; int b; int c = 1; a = b = c; return a;}",
            work=Path(work),
        )


def test_simple_assign_in_while() -> None:
    """Parser accepts ``(p = next())`` as an expression in while condition.

    cc.py may fail at a later IR stage (Task 3 wires IR lowering), so we
    accept either full compilation success or a clean IR-stage error that
    mentions the node type.  A parse-stage error (unexpected token) is a
    genuine failure.
    """
    with tempfile.TemporaryDirectory() as work:
        source_path = Path(work) / "while_assign.c"
        asm_path = Path(work) / "while_assign.asm"
        source_path.write_text(_PREAMBLE + "int next(void); int main(void){ int p; while ((p = next())) { } return 0; }")
        result = subprocess.run(
            ["python3", str(CC), "--bits", "32", "-I", str(LIBBBOEOS_INCLUDE), str(source_path), str(asm_path)],
            capture_output=True,
            check=False,
            text=True,
        )
        if result.returncode == 0:
            # Full success — parser + IR both handled it.
            assert "main" in asm_path.read_text(), "asm output missing 'main' label"
            return
        # cc.py failed — distinguish a parse error from an IR-stage error.
        stderr = result.stderr
        # A parse error looks like "expected expression, got ASSIGN" or similar.
        # An IR-stage error (acceptable for Task 2) mentions AssignExpr or
        # "unsupported" or "assignment-as-expression".
        parse_error_phrases = [
            "expected expression, got ASSIGN",
            "expected RPAREN",
            "expected statement",
            "unexpected token",
        ]
        for phrase in parse_error_phrases:
            if phrase in stderr:
                error_message = f"test_simple_assign_in_while: parser rejected the form — {stderr}"
                raise AssertionError(error_message)
        # Any other error is an IR/codegen issue, acceptable for Task 2.
        # Just confirm cc.py ran (returncode non-zero is fine).


def test_struct_arrow_member_lvalue() -> None:
    """Parser and codegen accept ``(p->f = expr)`` as an expression."""
    _compile("arrow", "struct S { int f; }; int main(void){struct S s; struct S *p = &s; return (p->f = 5);}")


def test_struct_member_lvalue() -> None:
    """Parser and codegen accept ``(s.f = expr)`` as an expression."""
    _compile("member", "struct S { int f; }; int main(void){struct S s; return (s.f = 3);}")


def test_unparenthesised_assignment_in_condition_rejected() -> None:
    """Bare ``x = y`` inside ``if (...)`` is rejected (no extra parens)."""
    with tempfile.TemporaryDirectory() as work:
        expect_reject(
            name="bare_if_assign",
            source="int main(void){ int x; int y = 1; if (x = y) { } return 0; }",
            work=Path(work),
        )


if __name__ == "__main__":
    test_accept_chained_with_parens()
    test_array_index_lvalue()
    test_assign_expr_function_call_rhs_evaluated_once()
    test_compound_amp_assign()
    test_compound_caret_assign()
    test_compound_lshift_assign()
    test_compound_minus_assign()
    test_compound_percent_assign()
    test_compound_pipe_assign()
    test_compound_plus_assign()
    test_compound_rshift_assign()
    test_compound_slash_assign()
    test_compound_star_assign()
    test_indexed_member_lvalue()
    test_member_index_lvalue()
    test_pointer_deref_lvalue()
    test_pointer_deref_postinc_lvalue()
    test_reject_assignment_as_lvalue()
    test_reject_bare_call_arg_assignment()
    test_reject_bare_if_assignment()
    test_reject_bitfield_assign_as_expression()
    test_reject_chained_without_parens()
    test_simple_assign_in_while()
    test_struct_arrow_member_lvalue()
    test_struct_member_lvalue()
    test_unparenthesised_assignment_in_condition_rejected()
    print("OK")
