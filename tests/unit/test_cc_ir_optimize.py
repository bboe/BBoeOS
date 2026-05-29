"""Unit tests for cc/ir_optimize.py.

Exercises each rewrite in isolation by constructing flat IR
instruction lists by hand and asserting the output shape.  No QEMU,
no AST, no codegen — just the IR passes.
"""

from __future__ import annotations

from cc import ast_nodes, ir, ir_optimize


def _basic_function(body: list[ir.Instruction], /, *, carry_return: bool = False) -> ir.Function:
    """Wrap *body* in an :class:`ir.Function` with a minimal AST stub."""
    ast = ast_nodes.Function(
        body=[],
        carry_return=carry_return,
        line=1,
        name="caller",
        params=[],
    )
    return ir.Function(ast_node=ast, body=list(body), strings=[])


def _optimize(body: list[ir.Instruction], /, *, carry_return: bool = False) -> list[ir.Instruction]:
    """Run every pass once and return the rewritten body."""
    function = _basic_function(body, carry_return=carry_return)
    program = ir.Program(functions=[function], globals=[])
    ir_optimize.optimize(program)
    return function.body


def test_optimize_carry_return_function_skips_tail_call_rewrite() -> None:
    """Carry-return functions must NOT have tail calls rewritten.

    The carry flag carries the return value; a tail ``jmp`` to a
    plain-AX callee would not set it.
    """
    body = [
        ir.Call(args=(), destination="_ir_0", name="callee"),
        ir.Return(value="_ir_0"),
    ]
    result = _optimize(body, carry_return=True)
    assert result == body


def test_optimize_eliminates_call_return_to_tail_call() -> None:
    """``Call(_ir_0) ; Return(_ir_0)`` collapses to a single ``TailCall``."""
    body = [
        ir.Call(args=(), destination="_ir_0", name="helper"),
        ir.Return(value="_ir_0"),
    ]
    result = _optimize(body)
    assert result == [ir.TailCall(args=(), name="helper")]


def test_optimize_eliminates_tail_call_with_multiple_arguments() -> None:
    """Multi-arg ``Call`` + ``Return`` rewrites the same way."""
    body = [
        ir.Call(args=(1, 2, "x"), destination="_ir_3", name="add3"),
        ir.Return(value="_ir_3"),
    ]
    result = _optimize(body)
    assert result == [ir.TailCall(args=(1, 2, "x"), name="add3")]


def test_optimize_eliminates_tail_call_through_loop_boundary() -> None:
    """``LoopBoundary`` markers between Call and Return don't block the rewrite.

    They carry no runtime semantics — codegen uses them only to
    bookkeep the continue/end label stack — so a TailCall that lives
    at the end of a loop body still gets eliminated.
    """
    body = [
        ir.Call(args=(), destination="_ir_0", name="helper"),
        ir.LoopBoundary(continue_label=".l1", end_label=".l2", push=False),
        ir.Return(value="_ir_0"),
    ]
    result = _optimize(body)
    assert result == [
        ir.LoopBoundary(continue_label=".l1", end_label=".l2", push=False),
        ir.TailCall(args=(), name="helper"),
    ]


def test_optimize_skips_tail_call_when_callee_is_asm_pseudo_builtin() -> None:
    """``asm`` is a pseudo-builtin with no normal calling convention.

    Rewriting it would emit ``jmp asm`` to a label that doesn't exist.
    """
    body = [
        ir.Call(args=("'hlt'",), destination="_ir_0", name="asm"),
        ir.Return(value="_ir_0"),
    ]
    result = _optimize(body)
    assert result == body


def test_optimize_skips_tail_call_when_callee_starts_with_underscore() -> None:
    """``__builtin_*`` and other internal hooks are conservatively skipped."""
    body = [
        ir.Call(args=(), destination="_ir_0", name="__builtin_va_arg"),
        ir.Return(value="_ir_0"),
    ]
    result = _optimize(body)
    assert result == body


def test_optimize_skips_tail_call_when_instruction_separates_call_and_return() -> None:
    """A runtime instruction between Call and Return blocks the rewrite.

    Even a no-op-looking Copy may have observable timing or AX
    clobbering effects; the rewrite must only fire when the Return
    immediately consumes the call's result.
    """
    body = [
        ir.Call(args=(), destination="_ir_0", name="helper"),
        ir.Copy(destination="other", source=0),
        ir.Return(value="_ir_0"),
    ]
    result = _optimize(body)
    assert result == body


def test_optimize_skips_tail_call_when_return_value_is_not_call_destination() -> None:
    """``Call(_ir_0) ; Return(_ir_1)`` must not rewrite — different temp."""
    body = [
        ir.Call(args=(), destination="_ir_0", name="helper"),
        ir.Copy(destination="_ir_1", source=42),
        ir.Return(value="_ir_1"),
    ]
    result = _optimize(body)
    assert result == body


def test_optimize_skips_tail_call_when_temp_used_elsewhere() -> None:
    """If the call result is read twice (Return + something else), don't rewrite.

    Conservative — the second read must observe the same value as
    the Return, but a TailCall has no destination to read from.
    """
    body = [
        ir.Call(args=(), destination="_ir_0", name="helper"),
        ir.Copy(destination="saved", source="_ir_0"),
        ir.Return(value="_ir_0"),
    ]
    result = _optimize(body)
    assert result == body


def test_optimize_unreachable_code_dropped_after_tail_call() -> None:
    """Instructions after a TailCall (until the next Label) are unreachable.

    The pass runs after tail-call elimination, so an unreachable
    ``Return`` planted by source code after a tail call is dropped.
    """
    body = [
        ir.Call(args=(), destination="_ir_0", name="helper"),
        ir.Return(value="_ir_0"),
        ir.Copy(destination="dead", source=99),
        ir.Label(name=".resume"),
        ir.Return(value=None),
    ]
    result = _optimize(body)
    assert result == [
        ir.TailCall(args=(), name="helper"),
        ir.Label(name=".resume"),
        ir.Return(value=None),
    ]
