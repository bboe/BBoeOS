"""IR-level optimizer tests.

Hand-built ``ir.Function`` inputs exercise specific shapes — Copy
propagation, constant folding, DCE — and assert the resulting body
matches what a hand-trace would predict.
"""

from __future__ import annotations

from cc import ast_nodes, ir
from cc.ir_optimize import Optimizer


def _function(body: list[ir.Instruction], /, *, carry_return: bool = False) -> ir.Function:
    """Wrap *body* in an ``ir.Function`` with a minimal AST node."""
    ast_function = ast_nodes.Function(
        body=[],
        carry_return=carry_return,
        name="test",
        params=[],
    )
    return ir.Function(ast_node=ast_function, body=body, strings=[])


def _optimize(body: list[ir.Instruction], /, *, carry_return: bool = False) -> list[ir.Instruction]:
    """Run :class:`Optimizer` on a single-function program and return the optimized body."""
    program = ir.Program(functions=[_function(body, carry_return=carry_return)], globals=[])
    optimized = Optimizer().optimize(program)
    return optimized.functions[0].body


def test_copy_propagation_substitutes_literal_into_later_use() -> None:
    """``Copy(_ir_0, 5)`` followed by use of ``_ir_0`` substitutes the literal in."""
    body = [
        ir.Copy(destination="_ir_0", source=5),
        ir.Copy(destination="result", source="_ir_0"),
    ]
    optimized = _optimize(body)
    # _ir_0 = 5 is dead after propagation; result <- 5 directly.
    assert optimized == [ir.Copy(destination="result", source=5)]


def test_copy_propagation_substitutes_name_through_chain() -> None:
    """Chained ``Copy(t1, x); Copy(t2, t1)`` collapses to ``Copy(t2, x)`` in one pass."""
    body = [
        ir.Copy(destination="_ir_0", source="param"),
        ir.Copy(destination="_ir_1", source="_ir_0"),
        ir.Return(value="_ir_1"),
    ]
    optimized = _optimize(body)
    assert optimized == [ir.Return(value="param")]


def test_constant_folding_of_two_int_operands() -> None:
    """``BinaryOperation`` with two int operands folds into a literal ``Copy``."""
    body = [
        ir.BinaryOperation(destination="_ir_0", left=4, operation="+", right=3),
        ir.Copy(destination="result", source="_ir_0"),
    ]
    optimized = _optimize(body)
    assert optimized == [ir.Copy(destination="result", source=7)]


def test_constant_folding_chain_via_propagation() -> None:
    """A propagated literal feeding a BinaryOperation collapses end-to-end."""
    body = [
        ir.Copy(destination="_ir_0", source=10),
        ir.BinaryOperation(destination="_ir_1", left="_ir_0", operation="*", right=4),
        ir.Copy(destination="result", source="_ir_1"),
    ]
    optimized = _optimize(body)
    assert optimized == [ir.Copy(destination="result", source=40)]


def test_dce_drops_unused_pure_temp() -> None:
    """A side-effect-free ``BinaryOperation`` whose destination is unused is dropped."""
    body = [
        ir.BinaryOperation(destination="_ir_0", left="a", operation="+", right="b"),
        ir.Return(value=0),
    ]
    optimized = _optimize(body)
    assert optimized == [ir.Return(value=0)]


def test_dce_preserves_call_strips_dead_destination() -> None:
    """An unused ``Call`` destination is rewritten to ``None`` but the call stays."""
    body = [
        ir.Call(args=(1,), destination="_ir_0", name="puts"),
        ir.Return(value=0),
    ]
    optimized = _optimize(body)
    assert optimized == [
        ir.Call(args=(1,), destination=None, name="puts"),
        ir.Return(value=0),
    ]


def test_dce_keeps_call_destination_when_used() -> None:
    """A live ``Call.destination`` is preserved when its temp is later read.

    Uses a second non-Return read so tail-call elimination doesn't kick in
    and collapse the ``Call + Return`` pair to a single ``TailCall``.
    """
    body = [
        ir.Call(args=(), destination="_ir_0", name="read_input"),
        ir.Copy(destination="saved", source="_ir_0"),
        ir.Return(value="_ir_0"),
    ]
    optimized = _optimize(body)
    assert optimized == body


def test_dce_does_not_remove_indexassign() -> None:
    """``IndexAssign`` is a side-effecting memory store and must be kept."""
    body = [
        ir.IndexAssign(base="arr", index=0, source=42),
        ir.Return(value=0),
    ]
    optimized = _optimize(body)
    assert optimized == [
        ir.IndexAssign(base="arr", index=0, source=42),
        ir.Return(value=0),
    ]


def test_user_local_is_not_propagated() -> None:
    """``Copy(user_local, ...)`` is left alone — user locals can be reassigned."""
    body = [
        ir.Copy(destination="x", source=5),
        ir.Copy(destination="x", source=7),
        ir.Return(value="x"),
    ]
    optimized = _optimize(body)
    # Body must be preserved verbatim: x might be the user's actual local
    # variable and the optimizer cannot prove the second assignment dominates.
    assert optimized == body


def test_block_use_keeps_temp_alive() -> None:
    """A ``Block`` containing ``Var(name=_ir_*)`` blocks DCE of the temp's def."""
    block_node = ast_nodes.Assign(
        expr=ast_nodes.Var(line=1, name="_ir_0"),
        line=1,
        name="x",
    )
    body = [
        ir.BinaryOperation(destination="_ir_0", left="a", operation="+", right="b"),
        ir.Block(node=block_node),
    ]
    optimized = _optimize(body)
    # The BinaryOperation defines _ir_0 which the Block reads; both must remain.
    assert optimized == body


def test_propagation_into_branch_condition() -> None:
    """A propagated literal substitutes into a ``BranchFalse`` operand.

    Uses ``<`` (not in :data:`_FOLDABLE_COMPARISON_OPS` — signedness-dependent)
    so the branch survives constant-folding and we observe the substituted
    operand directly.
    """
    body = [
        ir.Copy(destination="_ir_0", source=42),
        ir.BranchFalse(left="_ir_0", operation="<", right="bound", target=".L1"),
        ir.Label(name=".L1"),
    ]
    optimized = _optimize(body)
    assert optimized == [
        ir.BranchFalse(left=42, operation="<", right="bound", target=".L1"),
        ir.Label(name=".L1"),
    ]


def test_unfoldable_operation_left_intact() -> None:
    """Division depends on signedness and is left for the codegen."""
    body = [
        ir.Copy(destination="_ir_0", source=10),
        ir.BinaryOperation(destination="_ir_1", left="_ir_0", operation="/", right=2),
        ir.Return(value="_ir_1"),
    ]
    optimized = _optimize(body)
    # _ir_0 propagates to a literal but '/' is not in the foldable set;
    # the BinaryOperation survives with the substituted literal operand.
    assert optimized == [
        ir.BinaryOperation(destination="_ir_1", left=10, operation="/", right=2),
        ir.Return(value="_ir_1"),
    ]


def test_call_arguments_get_propagated_literals() -> None:
    """Literal-source Copy into a temp feeding a Call argument substitutes through."""
    body = [
        ir.Copy(destination="_ir_0", source=255),
        ir.Call(args=("_ir_0",), destination=None, name="set_color"),
    ]
    optimized = _optimize(body)
    assert optimized == [ir.Call(args=(255,), destination=None, name="set_color")]


def test_multi_def_temp_is_not_propagated() -> None:
    """LogicalOr / LogicalAnd lowering writes the same temp twice on different paths.

    The optimizer must NOT propagate either of the source values into uses
    of the temp — the actual value depends on which branch ran.  Regression
    for the isspace() miscompile where ``return a == 1 || b == 2`` was
    collapsed to ``return 0``.
    """
    body = [
        ir.BranchFalse(left="cond", operation="!=", right=0, target=".true"),
        ir.Copy(destination="_ir_0", source=0),
        ir.Jump(target=".end"),
        ir.Label(name=".true"),
        ir.Copy(destination="_ir_0", source=1),
        ir.Label(name=".end"),
        ir.Return(value="_ir_0"),
    ]
    optimized = _optimize(body)
    # _ir_0 has TWO defs (0 on the false path, 1 on the true path).  Neither
    # may be substituted into the Return; the Return must still read _ir_0.
    assert optimized == body


def test_index_operand_gets_propagated_literal() -> None:
    """Literal-source Copy into an Index's index operand substitutes through."""
    body = [
        ir.Copy(destination="_ir_0", source=3),
        ir.Index(base="arr", destination="_ir_1", index="_ir_0"),
        ir.Return(value="_ir_1"),
    ]
    optimized = _optimize(body)
    assert optimized == [
        ir.Index(base="arr", destination="_ir_1", index=3),
        ir.Return(value="_ir_1"),
    ]


# ---------------------------------------------------------------------------
# Control-flow simplification
# ---------------------------------------------------------------------------


def test_unreachable_code_after_jump_is_dropped() -> None:
    """Instructions between an unconditional Jump and the next Label are unreachable.

    Fixed-point shows the full chain: the unreachable Copy + IndexAssign go
    away, the now-redundant Jump-to-next-Label collapses, the orphaned
    Label is pruned, and only the live tail Return remains.
    """
    body = [
        ir.Jump(target=".end"),
        ir.Copy(destination="x", source=5),
        ir.IndexAssign(base="arr", index=0, source=1),
        ir.Label(name=".end"),
        ir.Return(value=0),
    ]
    optimized = _optimize(body)
    assert optimized == [ir.Return(value=0)]


def test_unreachable_code_after_return_is_dropped() -> None:
    """A Return is an unconditional transfer; instructions until the next Label are dead.

    Fixed-point continues: after the unreachable Copy and orphaned Label
    fall away, the trailing Return is also unreachable past the first one.
    """
    body = [
        ir.Return(value=42),
        ir.Copy(destination="x", source=5),
        ir.Label(name=".tail"),
        ir.Return(value=0),
    ]
    optimized = _optimize(body)
    assert optimized == [ir.Return(value=42)]


def test_jump_to_immediately_following_label_dropped() -> None:
    """``Jump(L) ; Label(L)`` collapses to just the Label (fall-through is equivalent)."""
    body = [
        ir.Jump(target=".end"),
        ir.Label(name=".end"),
        ir.Return(value=0),
    ]
    optimized = _optimize(body)
    # Jump to the next label is the same as fall-through; drop it.  The
    # newly-orphan-but-still-targetted label stays; nothing else
    # references it after the Jump went away, so dead-label removes it.
    assert optimized == [ir.Return(value=0)]


def test_branch_over_jump_inverts_and_drops_jump() -> None:
    """``BranchFalse(L1) ; Jump(L2) ; Label(L1)`` becomes ``BranchFalse_inv(L2) ; Label(L1)``.

    The intermediate Jump disappears; the branch's comparison is inverted
    so the new target is reached on the originally-true-condition path.
    The trailing ``Label(L2)`` survives because the rewritten branch now
    targets it.
    """
    body = [
        ir.BranchFalse(left="a", operation="==", right=0, target=".true"),
        ir.Jump(target=".end"),
        ir.Label(name=".true"),
        ir.Label(name=".end"),
        ir.Return(value=0),
    ]
    optimized = _optimize(body)
    assert optimized == [
        ir.BranchFalse(left="a", operation="!=", right=0, target=".end"),
        ir.Label(name=".end"),
        ir.Return(value=0),
    ]


def test_branch_over_jump_inverts_carry_branch() -> None:
    """``CarryBranch(when=set)`` over a Jump flips to ``when=clear`` and retargets."""
    call_ast = ast_nodes.Call(args=(), line=1, name="probe")
    body = [
        ir.CarryBranch(call_ast=call_ast, target=".true", when="set"),
        ir.Jump(target=".end"),
        ir.Label(name=".true"),
        ir.Label(name=".end"),
        ir.Return(value=0),
    ]
    optimized = _optimize(body)
    assert optimized == [
        ir.CarryBranch(call_ast=call_ast, target=".end", when="clear"),
        ir.Label(name=".end"),
        ir.Return(value=0),
    ]


def test_label_forwarding_redirects_through_trampoline() -> None:
    """Branches to a label that just jumps elsewhere get retargeted at the ultimate label."""
    body = [
        ir.BranchFalse(left="a", operation="==", right=0, target=".mid"),
        ir.Return(value=1),
        ir.Label(name=".mid"),
        ir.Jump(target=".end"),
        ir.Label(name=".end"),
        ir.Return(value=0),
    ]
    optimized = _optimize(body)
    # `.mid` was a trampoline.  Its name is forwarded to `.end`; the
    # branch now targets `.end` directly, the trampoline label becomes
    # unreferenced (dead-label removes it), and the trampoline's Jump
    # becomes unreachable after the Return (unreachable-code drops it).
    assert optimized == [
        ir.BranchFalse(left="a", operation="==", right=0, target=".end"),
        ir.Return(value=1),
        ir.Label(name=".end"),
        ir.Return(value=0),
    ]


def test_constant_true_branch_falsefalse_dropped() -> None:
    """``BranchFalse(5, ==, 5)`` is unconditionally true; the BranchFalse is removed."""
    body = [
        ir.BranchFalse(left=5, operation="==", right=5, target=".skip"),
        ir.Copy(destination="result", source=1),
        ir.Label(name=".skip"),
        ir.Return(value="result"),
    ]
    optimized = _optimize(body)
    # The comparison is statically true → BranchFalse never fires → drop it.
    assert optimized == [
        ir.Copy(destination="result", source=1),
        ir.Return(value="result"),
    ]


def test_constant_false_branch_lowers_to_unconditional_jump() -> None:
    """``BranchFalse(5, ==, 6)`` is unconditionally false; becomes a plain Jump.

    Fixed-point continues past the Jump-lowering: the now-unreachable
    Copy is removed, the Jump-to-next-Label collapses, the orphaned
    Label is pruned.
    """
    body = [
        ir.BranchFalse(left=5, operation="==", right=6, target=".end"),
        ir.Copy(destination="result", source=1),
        ir.Label(name=".end"),
        ir.Return(value="result"),
    ]
    optimized = _optimize(body)
    assert optimized == [ir.Return(value="result")]


def test_dead_label_with_no_referrers_removed() -> None:
    """A Label with no inbound branches is dropped."""
    body = [
        ir.Copy(destination="x", source=5),
        ir.Label(name=".unused"),
        ir.Return(value="x"),
    ]
    optimized = _optimize(body)
    assert optimized == [
        ir.Copy(destination="x", source=5),
        ir.Return(value="x"),
    ]


def test_self_loop_label_is_not_treated_as_trampoline() -> None:
    """``Label(L) ; Jump(L)`` is an infinite loop, not a forwardable trampoline."""
    body = [
        ir.Label(name=".loop"),
        ir.Jump(target=".loop"),
    ]
    optimized = _optimize(body)
    # Must NOT be collapsed.  The label still must remain — it's the
    # loop entry point, referenced by the Jump.
    assert optimized == body


# ---------------------------------------------------------------------------
# Automatic tail-call elimination
# ---------------------------------------------------------------------------


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
    """``LoopBoundary`` markers between Call and Return don't block the rewrite."""
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
    """``asm`` is a pseudo-builtin with no normal calling convention."""
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
    """A runtime instruction between Call and Return blocks the rewrite."""
    body = [
        ir.Call(args=(), destination="_ir_0", name="helper"),
        ir.Copy(destination="other", source=0),
        ir.Return(value="_ir_0"),
    ]
    result = _optimize(body)
    assert result == body


def test_optimize_skips_tail_call_when_temp_used_elsewhere() -> None:
    """If the call result is read twice (Return + something else), don't rewrite."""
    body = [
        ir.Call(args=(), destination="_ir_0", name="helper"),
        ir.Copy(destination="saved", source="_ir_0"),
        ir.Return(value="_ir_0"),
    ]
    result = _optimize(body)
    assert result == body


def test_optimize_unreachable_code_dropped_after_tail_call() -> None:
    """Instructions after a TailCall (until the next Label) are unreachable.

    Branches in from elsewhere into ``.resume`` so the dead-label pass
    keeps that label live; the ``Copy`` between the tail-call and the
    label has no path to it and is dropped.
    """
    body = [
        ir.BranchFalse(left="x", operation="==", right=0, target=".resume"),
        ir.Call(args=(), destination="_ir_0", name="helper"),
        ir.Return(value="_ir_0"),
        ir.Copy(destination="dead", source=99),
        ir.Label(name=".resume"),
        ir.Return(value=None),
    ]
    result = _optimize(body)
    assert result == [
        ir.BranchFalse(left="x", operation="==", right=0, target=".resume"),
        ir.TailCall(args=(), name="helper"),
        ir.Label(name=".resume"),
        ir.Return(value=None),
    ]
