"""Optimization passes that rewrite an :class:`cc.ir.Program` in place.

Each pass is a pure function on the flat instruction list of a
:class:`cc.ir.Function` body.  The driver :func:`optimize` applies the
passes once over each function before codegen consumes the IR.

Currently:

- :func:`_eliminate_tail_calls` collapses ``Call(destination=t) /
  Return(value=t)`` pairs into a single :class:`cc.ir.TailCall`.  When
  the temp ``t`` is dead after the rewrite, the trailing reference
  count drops to zero and downstream passes (if any are added later)
  can drop the ``Call.destination`` rename.
- :func:`_eliminate_unreachable_code` drops instructions that follow
  an unconditional control transfer (``Jump`` / ``Return`` /
  ``TailCall``) until the next ``Label``.
"""

from __future__ import annotations

from cc import ir

# Builtin / pseudo callee names that must not be tail-called.  These
# either have non-standard return shapes (``asm``: macro-expanded
# inline-asm fragment, no register convention to preserve) or rely on
# being followed by codegen-emitted teardown that ``jmp`` would skip.
# ``__builtin_*`` is filtered separately via the ``_`` prefix rule.
_NON_TAIL_CALLABLE_NAMES = frozenset({"asm"})


def _compute_use_counts(body: list[ir.Instruction]) -> dict[str, int]:
    """Return how many times each name appears as an operand across *body*.

    Destinations don't count as uses; only reads through ``left`` /
    ``right`` / ``index`` / ``source`` / ``value`` / ``args`` / branch
    operands do.  Used by :func:`_eliminate_tail_calls` to verify the
    Call's destination temp has exactly one downstream use (the
    following Return).
    """
    counts: dict[str, int] = {}

    def _bump(value: ir.Value | None) -> None:
        if isinstance(value, str):
            counts[value] = counts.get(value, 0) + 1

    for instruction in body:
        match instruction:
            case ir.BinaryOperation(left=left, right=right):
                _bump(left)
                _bump(right)
            case ir.Copy(source=source):
                _bump(source)
            case ir.Call(args=args):
                for arg in args:
                    _bump(arg)
            case ir.Index(index=index):
                _bump(index)
            case ir.IndexAssign(index=index, source=source):
                _bump(index)
                _bump(source)
            case ir.BranchFalse(left=left, right=right):
                _bump(left)
                _bump(right)
            case ir.Return(value=value):
                _bump(value)
            case ir.TailCall(args=args):
                for arg in args:
                    _bump(arg)
            case _:
                pass
    return counts


def _eliminate_tail_calls(body: list[ir.Instruction]) -> list[ir.Instruction]:
    """Rewrite ``Call(destination=t) / Return(value=t)`` as ``TailCall``.

    Fires only when:

    - The ``Call`` destination is a compiler-generated ``_ir_*`` temp.
    - The following instruction (skipping ``LoopBoundary`` metadata,
      which carries no runtime semantics) is a ``Return`` whose value
      is exactly that temp.
    - The temp is used exactly once in the whole body — by the
      ``Return`` itself.  Any other read means the value is observed
      elsewhere and the rewrite would change semantics.
    - The callee name is safe to tail-call: not ``asm``, not a name
      starting with ``_`` (covers ``__builtin_*`` and other internal
      hooks).
    """
    use_counts = _compute_use_counts(body)
    result: list[ir.Instruction] = []
    index = 0
    while index < len(body):
        instruction = body[index]
        if (
            isinstance(instruction, ir.Call)
            and _is_tail_callable_name(instruction.name)
            and instruction.destination is not None
            and instruction.destination.startswith("_ir_")
        ):
            return_index = _next_significant_index(body, index + 1)
            if return_index is not None:
                following = body[return_index]
                if (
                    isinstance(following, ir.Return)
                    and following.value == instruction.destination
                    and use_counts.get(instruction.destination, 0) == 1
                ):
                    # Carry forward any LoopBoundary metadata between the
                    # Call and the Return so the loop-context stack
                    # stays balanced for any later instructions in the
                    # body.
                    result.extend(body[index + 1 : return_index])
                    result.append(ir.TailCall(args=instruction.args, name=instruction.name))
                    index = return_index + 1
                    continue
        result.append(instruction)
        index += 1
    return result


def _eliminate_unreachable_code(body: list[ir.Instruction]) -> list[ir.Instruction]:
    """Drop instructions that follow an unconditional transfer until the next ``Label``.

    ``Jump`` / ``Return`` / ``TailCall`` all transfer control
    unconditionally; anything emitted between one of them and the
    next ``Label`` can never execute and is safe to remove.
    """
    result: list[ir.Instruction] = []
    unreachable = False
    for instruction in body:
        if isinstance(instruction, ir.Label):
            unreachable = False
            result.append(instruction)
            continue
        if unreachable:
            continue
        result.append(instruction)
        if isinstance(instruction, (ir.Jump, ir.Return, ir.TailCall)):
            unreachable = True
    return result


def _has_side_effects(instruction: ir.Instruction) -> bool:
    """Return True if removing *instruction* would change observable behavior.

    Control-flow terminators (``Jump`` / ``BranchFalse`` /
    ``CarryBranch`` / ``Return`` / ``TailCall``), calls, stores, and
    inline asm all qualify.  Pure value-producing instructions
    (``BinaryOperation`` / ``Copy`` / ``Index``) are side-effect-free
    in isolation — a separate dead-store pass decides whether their
    destination is live.
    """
    return isinstance(
        instruction,
        (
            ir.Call,
            ir.CarryBranch,
            ir.IndexAssign,
            ir.InlineAsm,
            ir.Jump,
            ir.BranchFalse,
            ir.Return,
            ir.TailCall,
            ir.Block,
            ir.Label,
            ir.LoopBoundary,
        ),
    )


def _is_tail_callable_name(name: str) -> bool:
    """Return True if *name* is safe to rewrite as a tail call.

    Conservative: rejects builtin / internal names (those starting
    with ``_``) and an explicit denylist of pseudo-callees (``asm``).
    """
    if name in _NON_TAIL_CALLABLE_NAMES:
        return False
    return not name.startswith("_")


def _next_significant_index(body: list[ir.Instruction], start: int) -> int | None:
    """Return the index of the first non-``LoopBoundary`` instruction at or after *start*.

    ``LoopBoundary`` markers are codegen metadata (no runtime
    semantics) — when scanning for the Return that follows a Call,
    skip past any boundary markers so the rewrite still fires inside
    a loop body.
    """
    cursor = start
    while cursor < len(body):
        if not isinstance(body[cursor], ir.LoopBoundary):
            return cursor
        cursor += 1
    return None


def optimize(program: ir.Program) -> ir.Program:
    """Apply every IR pass once to each function body in *program* (in-place).

    Returns *program* for chaining convenience.

    Tail-call elimination is skipped for ``carry_return`` functions —
    those return their boolean via the carry flag, which a tail
    ``jmp`` to a plain-AX callee would not set.
    """
    for function in program.functions:
        if not function.ast_node.carry_return:
            function.body = _eliminate_tail_calls(function.body)
        function.body = _eliminate_unreachable_code(function.body)
    return program
