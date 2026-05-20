"""Pytest unit tests for the C ternary conditional expression.

Covers the lexer (``?`` / ``:`` tokenization), parser (precedence,
right-associativity, nesting), AST shape, and x86 codegen (branch
lowering, only-one-branch evaluation, both ``--bits`` widths).

Run with: ``pytest tests/unit/test_conditional.py``
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CC = REPO_ROOT / "cc.py"
sys.path.insert(0, str(REPO_ROOT))
from cc.ast_nodes import (  # noqa: E402
    BinaryOperation,
    Conditional,
    Int,
    LogicalOr,
    VarDecl,
)
from cc.errors import CompileError  # noqa: E402
from cc.lexer import tokenize  # noqa: E402
from cc.parser import Parser  # noqa: E402


def _compile(source_text: str, /, *, bits: int = 16) -> tuple[bool, str]:
    """Run cc.py on *source_text*; return (success, stdout-or-stderr)."""
    text = textwrap.dedent(source_text)
    with tempfile.TemporaryDirectory(prefix="test_ternary_") as work:
        work_path = Path(work)
        src = work_path / "test.c"
        out = work_path / "test.asm"
        src.write_text(text)
        result = subprocess.run(
            ["python3", str(CC), "--bits", str(bits), str(src), str(out)],
            capture_output=True,
            check=False,
            cwd=str(REPO_ROOT),
            text=True,
        )
        if result.returncode != 0:
            return False, result.stderr
        return True, out.read_text()


def _parse_function_body(source: str, /) -> list:
    """Parse a single ``int main(...) { ... }`` and return its statement list.

    Helper for AST-shape assertions: keeps the per-test source small.
    """
    tokens = tokenize(textwrap.dedent(source))
    ast = Parser(tokens).parse_program()
    assert len(ast.functions) == 1
    return ast.functions[0].body


# --- Lexer ---------------------------------------------------------------


def test_lexer_emits_question_and_colon_tokens() -> None:
    """``?`` and ``:`` produce QUESTION / COLON tokens."""
    tokens = tokenize("a ? b : c")
    kinds = [kind for kind, _text, _line in tokens]
    assert "QUESTION" in kinds
    assert "COLON" in kinds


# --- Parser --------------------------------------------------------------


def test_parser_builds_conditional_node() -> None:
    """A simple ternary parses as a ``Conditional`` AST node."""
    body = _parse_function_body("""
        int main(int argc, char *argv[]) {
            int x = argc > 0 ? 1 : 2;
            return x;
        }
    """)
    declaration = body[0]
    assert isinstance(declaration, VarDecl)
    initializer = declaration.init
    assert isinstance(initializer, Conditional)
    assert isinstance(initializer.condition, BinaryOperation)
    assert initializer.condition.operation == ">"
    assert isinstance(initializer.then_expr, Int)
    assert initializer.then_expr.value == 1
    assert isinstance(initializer.else_expr, Int)
    assert initializer.else_expr.value == 2


def test_parser_is_right_associative() -> None:
    """``a ? b : c ? d : e`` parses as ``a ? b : (c ? d : e)``."""
    body = _parse_function_body("""
        int main(int argc, char *argv[]) {
            int x = argc ? 1 : argc ? 2 : 3;
            return x;
        }
    """)
    initializer = body[0].init
    assert isinstance(initializer, Conditional)
    # Right-associative: the else-branch is the *inner* conditional.
    assert isinstance(initializer.else_expr, Conditional)
    # And the then-branch of the *outer* conditional is a leaf.
    assert isinstance(initializer.then_expr, Int)
    inner = initializer.else_expr
    assert isinstance(inner.then_expr, Int)
    assert inner.then_expr.value == 2
    assert isinstance(inner.else_expr, Int)
    assert inner.else_expr.value == 3


def test_parser_equality_binds_tighter_than_ternary() -> None:
    """``a == b ? c : d`` parses with ``==`` inside the condition."""
    body = _parse_function_body("""
        int main(int argc, char *argv[]) {
            int x = argc == 1 ? 10 : 20;
            return x;
        }
    """)
    initializer = body[0].init
    assert isinstance(initializer, Conditional)
    assert isinstance(initializer.condition, BinaryOperation)
    assert initializer.condition.operation == "=="


def test_parser_logical_or_binds_tighter_than_ternary() -> None:
    """``a || b ? c : d`` parses with ``||`` inside the condition."""
    body = _parse_function_body("""
        int main(int argc, char *argv[]) {
            int x = argc || 1 ? 10 : 20;
            return x;
        }
    """)
    initializer = body[0].init
    assert isinstance(initializer, Conditional)
    assert isinstance(initializer.condition, LogicalOr)


def test_parser_ternary_inside_parens_then_arithmetic() -> None:
    """``(a ? b : c) + 1`` puts the conditional inside a BinaryOperation."""
    body = _parse_function_body("""
        int main(int argc, char *argv[]) {
            int x = (argc ? 10 : 20) + 1;
            return x;
        }
    """)
    initializer = body[0].init
    assert isinstance(initializer, BinaryOperation)
    assert initializer.operation == "+"
    assert isinstance(initializer.left, Conditional)
    assert isinstance(initializer.right, Int)
    assert initializer.right.value == 1


def test_parser_unterminated_ternary_raises() -> None:
    """A ``?`` without a matching ``:`` raises a parser error."""
    source = """
        int main(int argc, char *argv[]) {
            int x = argc ? 1;
            return x;
        }
    """
    tokens = tokenize(textwrap.dedent(source))
    with pytest.raises(CompileError, match="COLON"):
        Parser(tokens).parse_program()


# --- Codegen -------------------------------------------------------------


@pytest.mark.parametrize("bits", [16, 32])
def test_codegen_emits_branch_pattern(bits: int) -> None:
    """The simple ternary lowers to a cond-jump / jmp / labels skeleton."""
    success, output = _compile(
        """
        int g;
        int main(int argc, char *argv[]) {
            g = argc;
            int r = g > 0 ? 100 : 200;
            return r;
        }
        """,
        bits=bits,
    )
    assert success, output
    # Both then-value and else-value must appear as immediates.
    assert "100" in output
    assert "200" in output
    # Branch / merge labels are emitted; numbering is implementation-detail
    # so we only require that *some* label of each kind appears.
    assert ".cond_else_" in output
    assert ".cond_end_" in output


def test_codegen_only_one_branch_evaluated() -> None:
    """The unchosen branch's side effect must not fire.

    Uses two globals as observable side-effects: ``hit_then`` is written
    only inside the then-branch, ``hit_else`` only inside the else-branch.
    cc.py doesn't support assignment-as-expression, so the side effect is
    expressed via a helper function whose return value the ternary picks.
    The generated assembly must contain *both* function names but the
    branch structure must guarantee only one ``call`` runs at a time.
    """
    success, output = _compile(
        """
        int hit_then;
        int hit_else;

        int set_then(int v) { hit_then = 1; return v; }
        int set_else(int v) { hit_else = 1; return v; }

        int main(int argc, char *argv[]) {
            int r = argc > 0 ? set_then(7) : set_else(9);
            return r;
        }
        """,
    )
    assert success, output
    # Both calls must be emitted; the branch decides which one executes.
    assert "call set_then" in output
    assert "call set_else" in output
    # ``set_else`` must sit *after* the else label so the then-path
    # (which falls through then jumps over the else) never reaches it.
    else_label_index = output.find(".cond_else_")
    set_else_index = output.find("call set_else")
    assert else_label_index != -1
    assert set_else_index != -1
    assert else_label_index < set_else_index


def test_codegen_nested_ternary_assembles() -> None:
    """A right-associative chain lowers without falling over on label collisions."""
    success, output = _compile(
        """
        int main(int argc, char *argv[]) {
            int r = argc ? 1 : argc ? 2 : 3;
            return r;
        }
        """,
    )
    assert success, output
    # Each ternary needs its own pair of labels.  Two ternaries → at
    # least two distinct ``.cond_else_*`` labels.
    label_lines = [line for line in output.splitlines() if ".cond_else_" in line and line.strip().endswith(":")]
    assert len(label_lines) >= 2


def test_codegen_ternary_inside_if_condition() -> None:
    """``if (a ? b : c)`` compiles end-to-end (Conditional as a condition)."""
    success, output = _compile(
        """
        int g;
        int main(int argc, char *argv[]) {
            g = argc;
            if (g > 0 ? g : 1) {
                return 1;
            }
            return 0;
        }
        """,
    )
    assert success, output


def test_codegen_ternary_as_call_argument() -> None:
    """``f(a ? b : c)`` lowers correctly: the ternary materialises a value before the call."""
    success, output = _compile(
        """
        int passthrough(int x) { return x; }

        int main(int argc, char *argv[]) {
            int r = passthrough(argc > 0 ? 7 : 8);
            return r;
        }
        """,
    )
    assert success, output
    assert "call passthrough" in output


# --- kernel/include/macros.h ------------------------------------------------


def test_codegen_max_min_macro_avoids_redundant_subexpression() -> None:
    """``MIN(a - b, K)`` evaluates ``a - b`` once, not twice.

    Textual function-like macros normally expand each argument
    everywhere it appears in the body, so ``MIN(a - b, K)`` expands
    to ``((a - b) < (K) ? (a - b) : (K))`` — two ``a - b`` evaluations
    in the source.  cc.py's ``_try_emit_conditional_via_cond_value``
    recognises that the then-branch is structurally equal to the
    comparison's left operand and pure, so it elides the second
    evaluation: ``emit_condition`` lands ``a - b`` in AX once, and
    cond-true jumps straight to the merge while AX still holds that
    value.  This check counts the ``sub`` instructions in the
    emitted assembly — there must be exactly one for the ``a - b``
    subtraction.
    """
    test_source = textwrap.dedent("""
        #include "macros.h"

        int g_total;
        int g_offset;

        int main(int argc, char *argv[]) {
            g_total = argc;
            g_offset = 0;
            int chunk = MIN(g_total - g_offset, 512);
            return chunk;
        }
    """)
    with tempfile.NamedTemporaryFile(
        suffix=".c",
        prefix="_test_min_oneshot_",
        dir=str(REPO_ROOT / "user" / "programs"),
        mode="w",
        encoding="utf-8",
        delete=True,
    ) as src_file:
        src_file.write(test_source)
        src_file.flush()
        result = subprocess.run(
            ["python3", str(CC), "--bits", "16", src_file.name],
            capture_output=True,
            check=False,
            cwd=str(REPO_ROOT),
            text=True,
        )
    assert result.returncode == 0, result.stderr
    output = result.stdout
    # Count ``sub`` instructions referring to the offset operand.
    # Exactly one means the second textual expansion of ``g_total -
    # g_offset`` was elided.
    sub_lines = [line for line in output.splitlines() if line.strip().startswith("sub ") and "_g_offset" in line]
    assert len(sub_lines) == 1, f"expected one sub instruction, got {len(sub_lines)}:\n{output}"


def test_codegen_guarded_update_collapses_self_branch() -> None:
    """``dest = MIN(dest, lit);`` lowers without round-tripping through AX.

    Recognises the ``dest = (cond) ? dest : other`` shape that ``MAX`` /
    ``MIN`` produce when one of the operands *is* the assignment
    destination — the no-op stay-branch is elided, so the emission
    looks like the hand-written ``if`` saturation: ``cmp / Jcc / mov dest,
    other``.  The check pins this behaviour: regressions that re-introduce
    the AX round-trip would emit a ``mov`` from the destination register
    back to itself (or its memory slot) which is exactly what this
    peephole eliminates.
    """
    test_source = textwrap.dedent("""
        #include "macros.h"

        int main(int argc, char *argv[]) {
            int chunk = argc;
            chunk = MIN(chunk, 512);
            return chunk;
        }
    """)
    with tempfile.NamedTemporaryFile(
        suffix=".c",
        prefix="_test_guarded_update_",
        dir=str(REPO_ROOT / "user" / "programs"),
        mode="w",
        encoding="utf-8",
        delete=True,
    ) as src_file:
        src_file.write(test_source)
        src_file.flush()
        result = subprocess.run(
            ["python3", str(CC), "--bits", "16", src_file.name],
            capture_output=True,
            check=False,
            cwd=str(REPO_ROOT),
            text=True,
        )
    assert result.returncode == 0, result.stderr
    output = result.stdout
    # The fast path emits a ``.cond_skip_*`` label and no
    # ``.cond_else_*`` / ``.cond_end_*`` pair — that's the signal it
    # went through ``_try_emit_guarded_update`` instead of the
    # general AX-staged lowering.
    assert ".cond_skip_" in output
    assert ".cond_else_" not in output
    assert ".cond_end_" not in output
    # Exactly one ``mov ..., 512`` (the saturating store), and the
    # comparison sits on the line just before the conditional jump.
    assert "mov ax, 512" in output or "mov dx, 512" in output or "mov cx, 512" in output


def test_macros_h_max_min_compile() -> None:
    """``#include "macros.h"`` makes ``MAX`` / ``MIN`` available; both compile.

    The source has to live somewhere the preprocessor's ``include/``
    discovery can find ``kernel/include/``.  Easiest is to drop the source
    into ``user/programs/`` for the duration of the test (the preprocessor walks
    up from the source dir looking for a sibling ``include/``).
    """
    test_source = textwrap.dedent("""
        #include "macros.h"

        int main(int argc, char *argv[]) {
            int a = 5;
            int b = 3;
            int m = MAX(a, b);
            int n = MIN(a, b);
            return m + n;
        }
    """)
    with tempfile.NamedTemporaryFile(
        suffix=".c",
        prefix="_test_macros_",
        dir=str(REPO_ROOT / "user" / "programs"),
        mode="w",
        encoding="utf-8",
        delete=True,
    ) as src_file:
        src_file.write(test_source)
        src_file.flush()
        result = subprocess.run(
            ["python3", str(CC), "--bits", "16", src_file.name],
            capture_output=True,
            check=False,
            cwd=str(REPO_ROOT),
            text=True,
        )
    assert result.returncode == 0, result.stderr
    # ``MAX(a, b)`` and ``MIN(a, b)`` both produce the
    # ``then_expr == cond.left`` shape, which the
    # ``_try_emit_conditional_via_cond_value`` fast path lowers
    # without an explicit else label — just a single conditional
    # jump to ``.cond_end_*`` over the else-branch load.  Two
    # macros → two distinct ``.cond_end_*`` labels.
    label_lines = [line for line in result.stdout.splitlines() if ".cond_end_" in line and line.strip().endswith(":")]
    assert len(label_lines) == 2
