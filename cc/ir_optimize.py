"""IR-level optimization: scalar passes + control-flow simplification.

Operates on the linear three-address-code IR produced by :class:`cc.ir.Builder`
before any architecture-specific codegen.  Because every transform is expressed
in terms of architecture-agnostic IR shapes (``BinaryOperation``, ``Copy``,
``Call``, ``Index``, ``Jump``, ``BranchFalse`` …), each backend that consumes
the IR — currently x86, future ARM / x86-64 — benefits automatically without
having to re-implement equivalent asm-text peephole rewrites.

Scope for value-flow passes: compiler-generated temps (``_ir_*``) with
exactly one definition in the function body.  Most are single-assigned
by construction (each ``_build_expr`` call emits a fresh temp and one
defining instruction); the exceptions are ``LogicalOr`` / ``LogicalAnd``
lowering, which assigns the result temp twice (once per branch).
Multi-def temps would break the propagation assumption that *any* value
reaching one use reaches every use, so a definition-count pre-pass
excludes them.  User-defined locals (which can be reassigned) are out
of scope until a CFG-based reaching-defs pass exists.

Per function, the driver iterates these transforms to fixed point:

* **Copy propagation** — replace later uses of ``tempN`` with the source
  of ``Copy(destination=tempN, source=value)`` when *value* is a literal
  or another name.
* **Constant folding** — collapse ``BinaryOperation`` whose operands have
  both become integer literals, rewriting the instruction as a ``Copy``.
* **Control-flow simplification** — fold constant-condition branches,
  forward trampoline labels, invert branch-over-jump pairs, drop jumps
  to the immediately-following label, eliminate unreachable code, and
  prune labels nothing branches to.  Together these subsume the
  asm-text peepholes ``peephole_dead_code``, ``peephole_jump_next``,
  ``peephole_double_jump``, and ``peephole_label_forwarding`` for any
  function lowered through the IR path.
* **Dead-code elimination** — drop side-effect-free instructions whose
  destination temp has no remaining uses.  ``Call`` is preserved (it may
  have side effects) but its ``destination`` is set to ``None`` when the
  result is unused.

Conservative treatment: ``Block`` (escape hatch to AST codegen) and
``InlineAsm`` are not rewritten; their reads of a temp are counted as
uses so the def stays live, but their internal shapes are never mutated.
``LoopBoundary`` is emission metadata (continue/break label context),
not control flow — passes that scan for "is the next instruction a
Label" skip past intervening ``LoopBoundary`` entries.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from cc import ast_nodes, ir
from cc.loops import hoist_loop_invariants, recognize_string_loops, reduce_loop_strength, string_loop_type_maps
from cc.ssa import optimize_ssa
from cc.tokens import INVERT_COMPARISON

if TYPE_CHECKING:
    from collections.abc import Iterator

    from cc.target import X86CodegenTarget

#: Binary operations safe to constant-fold with Python ``int`` arithmetic.
#: Bit-pattern preserving + signedness-independent.  Comparisons, division,
#: modulo, and shifts depend on signedness or width semantics that the IR
#: doesn't carry, so they're left to the codegen.
_FOLDABLE_BINARY_OPS = frozenset({"+", "-", "*", "&", "|", "^"})

#: Comparison operations safe to constant-fold without knowing signedness.
#: Equality / inequality have identical signed and unsigned semantics
#: when both operands are integer literals.  Ordered comparisons (``<``,
#: ``>``, etc.) depend on whether the operands are interpreted as signed
#: or unsigned, so we leave them to the codegen even when both sides
#: become literals.
_FOLDABLE_COMPARISON_OPS = frozenset({"==", "!="})

#: Builtin / pseudo callee names that must not be tail-called.  ``asm`` is the
#: macro-expanded inline-asm fragment (no register convention to preserve);
#: ``__builtin_*`` and other underscore-prefixed names are filtered separately
#: via the ``_`` prefix rule in :meth:`Optimizer._eliminate_tail_calls`.
_NON_TAIL_CALLABLE_NAMES = frozenset({"asm"})

_TEMP_PREFIX = "_ir_"


def _branch_target(instruction: ir.Instruction, /) -> str | None:
    """Return the destination label of a control-transfer instruction, else None.

    ``Jump`` / ``BranchFalse`` / ``CarryBranch`` all carry a ``target``
    label.  Other instructions return None; ``Return`` exits the function
    rather than branching within it.
    """
    if isinstance(instruction, (ir.Jump, ir.BranchFalse, ir.CarryBranch)):
        return instruction.target
    return None


def _collect_global_names(program: ir.Program, /) -> frozenset[str]:
    """Return the set of program-level global ``name`` strings from *program*.

    Any AST node in :attr:`cc.ir.Program.globals` carrying a runtime
    ``name`` string is captured.  The SSA optimizer uses this set to
    exclude globals from renaming so propagation cannot forward a
    stale read across a ``Call`` (the callee may have written through
    the same name).  Nodes without a usable ``name`` attribute (e.g.
    ``StructDecl`` shapes) are skipped — the renamer only ever
    consults the set when checking instruction destinations, so
    irrelevant entries cost nothing.
    """
    names: set[str] = set()
    for node in program.globals:
        name = getattr(node, "name", None)
        if isinstance(name, str):
            names.add(name)
    return frozenset(names)


def _evaluate_constant_comparison(*, left: int, operation: str, right: int) -> bool | None:
    """Return the truth value of a comparison when both sides are integer literals.

    Only ``==`` / ``!=`` are evaluated (see :data:`_FOLDABLE_COMPARISON_OPS`);
    ordered comparisons depend on signedness the IR doesn't carry.  Returns
    None when the operation is not in the foldable set.
    """
    if operation == "==":
        return left == right
    if operation == "!=":
        return left != right
    return None


def _fold_binary_operation(instruction: ir.BinaryOperation, /) -> ir.Copy | None:
    """Constant-fold a ``BinaryOperation`` whose operands are both int; else None.

    Only the operations in :data:`_FOLDABLE_BINARY_OPS` are evaluated.
    The result is returned as a ``Copy`` of the integer value into the
    original destination, so downstream propagation sees a literal.
    """
    if not isinstance(instruction.left, int) or not isinstance(instruction.right, int):
        return None
    if instruction.operation not in _FOLDABLE_BINARY_OPS:
        return None
    left, right = instruction.left, instruction.right
    if instruction.operation == "+":
        result = left + right
    elif instruction.operation == "-":
        result = left - right
    elif instruction.operation == "*":
        result = left * right
    elif instruction.operation == "&":
        result = left & right
    elif instruction.operation == "|":
        result = left | right
    else:  # "^"
        result = left ^ right
    return ir.Copy(destination=instruction.destination, source=result)


def _has_side_effects(instruction: ir.Instruction, /) -> bool:
    """Return True when *instruction* must not be removed even with a dead destination."""
    return isinstance(
        instruction,
        (
            ir.Block,
            ir.BranchFalse,
            ir.Call,
            ir.CarryBranch,
            ir.IndexAssign,
            ir.InlineAsm,
            ir.Jump,
            ir.Label,
            ir.LoopBoundary,
            ir.RepString,
            ir.Return,
            ir.TailCall,
        ),
    )


def _instruction_destination(instruction: ir.Instruction, /) -> str | None:
    """Return the destination name written by *instruction*, or None.

    ``Call.destination`` is ``None`` when the return value is discarded.
    Branch / jump / label instructions have no destination.
    """
    if isinstance(instruction, (ir.BinaryOperation, ir.Copy, ir.Index)):
        return instruction.destination
    if isinstance(instruction, ir.Call):
        return instruction.destination
    return None


def _instruction_value_operands(instruction: ir.Instruction, /) -> tuple[ir.Value, ...]:
    """Return the *non-destination* ``Value`` operands read by *instruction*.

    ``Index.base`` / ``IndexAssign.base`` are variable-name strings
    (not ``Value`` operands), so they aren't returned here — they need
    a separate path when counting uses.
    """
    if isinstance(instruction, ir.BinaryOperation):
        return (instruction.left, instruction.right)
    if isinstance(instruction, ir.Copy):
        return (instruction.source,)
    if isinstance(instruction, ir.Call):
        return instruction.args
    if isinstance(instruction, ir.Index):
        return (instruction.index,)
    if isinstance(instruction, ir.IndexAssign):
        return (instruction.index, instruction.source)
    if isinstance(instruction, ir.BranchFalse):
        return (instruction.left, instruction.right)
    if isinstance(instruction, ir.RepString):
        operands = [instruction.count]
        if instruction.fill_value is not None:
            operands.append(instruction.fill_value)
        return tuple(operands)
    if isinstance(instruction, ir.Return):
        return () if instruction.value is None else (instruction.value,)
    return ()


def _is_tail_callable_name(name: str, /) -> bool:
    """Return True if a call to *name* is safe to lower as a tail-call.

    Conservative: rejects ``asm`` (no standard register convention) and any
    name starting with ``_`` (covers ``__builtin_*`` and other internal
    hooks whose calling shape is not the cdecl ``call`` / ``ret`` pair).
    """
    return name not in _NON_TAIL_CALLABLE_NAMES and not name.startswith("_")


def _is_temp(value: object, /) -> bool:
    """Return True if *value* names a compiler-generated single-assignment temp."""
    return isinstance(value, str) and value.startswith(_TEMP_PREFIX)


def _is_unconditional_transfer(instruction: ir.Instruction, /) -> bool:
    """Return True if control cannot fall through *instruction* to the next."""
    return isinstance(instruction, (ir.Jump, ir.Return, ir.TailCall))


def _iter_ast_goto_targets(node: object, /) -> Iterator[str]:
    """Yield every IR-form label name targeted by an AST ``Goto`` in *node*'s subtree.

    AST gotos lower to ``Jump(target=".user_<name>")`` when walked by the IR
    builder, but ``Block``-wrapped subtrees (e.g. ``Switch`` bodies) are
    handed off to the AST codegen unchanged.  The CFG passes still need
    to see those gotos as incoming branches to the IR label they target,
    otherwise ``_eliminate_dead_labels`` will drop a ``Label(.user_foo)``
    whose only references live inside a ``Block``-wrapped ``Switch``.

    Yields:
        Each goto target in its IR form (``.user_<name>``), in source order.

    """
    if isinstance(node, ast_nodes.Goto):
        yield f".user_{node.name}"
        return
    if dataclasses.is_dataclass(node):
        for declared_field in dataclasses.fields(node):
            yield from _iter_ast_goto_targets(getattr(node, declared_field.name))
        return
    if isinstance(node, (list, tuple)):
        for item in node:
            yield from _iter_ast_goto_targets(item)


def _iter_ast_var_names(node: object, /) -> Iterator[str]:
    """Yield every ``Var.name`` appearing anywhere in the AST subtree at *node*.

    Walks dataclass fields, lists, and tuples recursively.  Used to detect
    references to IR temps that survive in ``Block``-wrapped AST nodes
    (``_lower_assign_expr`` rebinds an assignment's RHS to ``Var(name=temp)``).

    Yields:
        Each ``Var.name`` string encountered in source order.

    """
    if isinstance(node, ast_nodes.Var):
        yield node.name
        return
    # Lvalue targets and member/deref bases the AST stores as a bare string
    # rather than a Var node: ``target_name`` (IncrementDecrement /
    # DerefIncrement{,Assign}) and ``object_name`` (the Member* family).
    # The Var-only recursion below cannot see these, so a variable
    # referenced *only* through one of them is invisible to callers that
    # must enumerate every name an opaque region touches.  The canonical
    # failure is a for-loop's ``i++`` step, whose sole mention of ``i`` is
    # ``target_name``: missing it under-counts ``i``'s uses and (in the
    # mirrored SSA filter) versions ``i`` as loop-invariant.  Both field
    # names appear only on variable-bearing nodes, so reading them by
    # attribute is unambiguous.
    for bare_name_field in ("target_name", "object_name"):
        bare_name = getattr(node, bare_name_field, None)
        if isinstance(bare_name, str):
            yield bare_name
    if dataclasses.is_dataclass(node):
        for declared_field in dataclasses.fields(node):
            yield from _iter_ast_var_names(getattr(node, declared_field.name))
        return
    if isinstance(node, (list, tuple)):
        for item in node:
            yield from _iter_ast_var_names(item)


def _next_non_metadata_index(*, body: list[ir.Instruction], start: int) -> int | None:
    """Return the index of the first instruction at or after *start* that's not ``LoopBoundary``.

    ``LoopBoundary`` is emission metadata for resolving ``Continue`` /
    ``Break`` inside ``Block``-wrapped AST; structurally it sits between
    real IR instructions without affecting control flow.  CFG passes
    that need to ask "is the next real instruction a Label" must skip
    past these.  Returns None when no such instruction exists.
    """
    for index in range(start, len(body)):
        if not isinstance(body[index], ir.LoopBoundary):
            return index
    return None


def _replace_branch_target(instruction: ir.Instruction, /, *, old: str, new: str) -> ir.Instruction:
    """Return *instruction* with ``target == old`` rewritten to *new*; unchanged otherwise.

    Used by label-forwarding to redirect every branch that targets a
    trampoline label at the trampoline's destination.
    """
    if not isinstance(instruction, (ir.Jump, ir.BranchFalse, ir.CarryBranch)):
        return instruction
    if instruction.target != old:
        return instruction
    return dataclasses.replace(instruction, target=new)


def _substitute_value(instruction: ir.Instruction, /, *, source: ir.Value, target: str) -> ir.Instruction:
    """Return *instruction* with every ``Value`` operand equal to *target* replaced by *source*.

    Only IR ``Value`` operands are rewritten.  ``Index.base`` /
    ``IndexAssign.base`` (variable-name strings), ``Block.node`` (AST),
    and ``InlineAsm.content`` are intentionally left intact — the
    optimizer doesn't mutate AST shapes, and ``base`` operands never
    reference a temp under the current builder.
    """
    if isinstance(instruction, ir.BinaryOperation):
        if target not in (instruction.left, instruction.right):
            return instruction
        return dataclasses.replace(
            instruction,
            left=source if instruction.left == target else instruction.left,
            right=source if instruction.right == target else instruction.right,
        )
    if isinstance(instruction, ir.Copy):
        if instruction.source != target:
            return instruction
        return dataclasses.replace(instruction, source=source)
    if isinstance(instruction, ir.Call):
        if target not in instruction.args:
            return instruction
        rewritten_args = tuple(source if arg == target else arg for arg in instruction.args)
        return dataclasses.replace(instruction, args=rewritten_args)
    if isinstance(instruction, ir.Index):
        if instruction.index != target:
            return instruction
        return dataclasses.replace(instruction, index=source)
    if isinstance(instruction, ir.IndexAssign):
        if target not in (instruction.index, instruction.source):
            return instruction
        return dataclasses.replace(
            instruction,
            index=source if instruction.index == target else instruction.index,
            source=source if instruction.source == target else instruction.source,
        )
    if isinstance(instruction, ir.BranchFalse):
        if target not in (instruction.left, instruction.right):
            return instruction
        return dataclasses.replace(
            instruction,
            left=source if instruction.left == target else instruction.left,
            right=source if instruction.right == target else instruction.right,
        )
    if isinstance(instruction, ir.Return):
        if instruction.value != target:
            return instruction
        return dataclasses.replace(instruction, value=source)
    return instruction


class Optimizer:
    """Drive IR-level propagation + folding + DCE across a whole Program.

    Stateless: a single instance can optimize many programs.  Each
    function body is optimized independently — no inter-procedural state.

    *target* (when supplied) lets the rep-string loop recognizer resolve
    element widths and counter signedness from each function's declared
    types — the only pass that is target-width-dependent.  When ``None``
    (e.g. unit tests that drive the scalar pipeline directly) the
    rep-string pass is skipped; every other pass is target-agnostic.
    """

    def __init__(self, *, target: X86CodegenTarget | None = None) -> None:
        """Store the codegen *target* used to resolve rep-string type maps."""
        self._target = target

    def optimize(self, program: ir.Program, /) -> ir.Program:
        """Return a new ``Program`` with each function body optimized in place.

        Globals are passed through unchanged.  Each :class:`ir.Function`
        is rebuilt with an optimized body and original ``ast_node`` /
        ``strings`` preserved.  ``carry_return`` functions skip tail-call
        elimination — those return their boolean via the carry flag,
        which a tail ``jmp`` to a plain-AX callee would not set.

        Program-level global names are collected once and forwarded to
        :func:`cc.ssa.optimize_ssa` as the SSA exclusion set so the
        renamer treats every ``Call`` as a potential mutation of any
        global.  Without this, propagation could forward a stale
        ``global = 0`` past a callee that reassigned ``global``,
        miscompiling code that relies on the post-call value.
        """
        global_names = _collect_global_names(program)
        optimized_functions = []
        for function in program.functions:
            if self._target is not None:
                element_sizes, signed_counters = string_loop_type_maps(
                    function.ast_node, program_globals=program.globals, target=self._target
                )
            else:
                element_sizes, signed_counters = None, None
            optimized_functions.append(
                ir.Function(
                    ast_node=function.ast_node,
                    body=self._optimize_body(
                        function.body,
                        carry_return=function.ast_node.carry_return,
                        excluded_ssa_names=global_names,
                        signed_counters=signed_counters,
                        variable_element_sizes=element_sizes,
                    ),
                    strings=function.strings,
                )
            )
        return ir.Program(functions=optimized_functions, globals=program.globals)

    @staticmethod
    def _collapse_jump_to_next_label(body: list[ir.Instruction], /) -> list[ir.Instruction]:
        """Drop ``Jump(L)`` when the next real instruction is ``Label(L)``.

        ``LoopBoundary`` between the ``Jump`` and the ``Label`` is metadata
        and is preserved.  Operates whether or not ``L`` has other references
        — the local fall-through is equivalent to the explicit branch.
        """
        result: list[ir.Instruction] = []
        index = 0
        while index < len(body):
            instruction = body[index]
            if not isinstance(instruction, ir.Jump):
                result.append(instruction)
                index += 1
                continue
            next_index = _next_non_metadata_index(body=body, start=index + 1)
            if next_index is None or not isinstance(body[next_index], ir.Label):
                result.append(instruction)
                index += 1
                continue
            if body[next_index].name != instruction.target:
                result.append(instruction)
                index += 1
                continue
            # Drop the Jump but keep any LoopBoundary between it and the Label.
            result.extend(body[index + 1 : next_index])
            index = next_index
        return result

    @classmethod
    def _collect_all_label_references(cls, body: list[ir.Instruction], /) -> set[str]:
        """Return every label name referenced anywhere reachable from *body*.

        Recurses into ``ir.Switch.cases[].body`` so the optimizer's
        dead-label pass sees branches emitted inside switch arms,
        even though those arms aren't otherwise walked.
        """
        referenced = cls._collect_ast_goto_targets(body)
        for instruction in body:
            target = _branch_target(instruction)
            if target is not None:
                referenced.add(target)
            if isinstance(instruction, ir.Switch):
                for case in instruction.cases:
                    referenced.update(cls._collect_all_label_references(case.body))
        return referenced

    @staticmethod
    def _collect_ast_goto_targets(body: list[ir.Instruction], /) -> set[str]:
        """Return the set of IR labels (``.user_<name>``) referenced by Block-buried Gotos."""
        targets: set[str] = set()
        for instruction in body:
            if isinstance(instruction, ir.Block):
                targets.update(_iter_ast_goto_targets(instruction.node))
            elif isinstance(instruction, ir.CarryBranch):
                targets.update(_iter_ast_goto_targets(instruction.call_ast))
        return targets

    @staticmethod
    def _compute_use_counts(body: list[ir.Instruction], /) -> dict[str, int]:
        """Count every read of every name across the function body.

        Reads include: ``Value`` operands (literals are skipped because
        they aren't names), ``Index.base`` / ``IndexAssign.base`` variable
        names, AST ``Var`` references buried in ``Block.node``, and AST
        ``Var`` references in ``CarryBranch.call_ast`` (arguments to the
        wrapped call).
        """
        counts: dict[str, int] = {}
        for instruction in body:
            for operand in _instruction_value_operands(instruction):
                if isinstance(operand, str):
                    counts[operand] = counts.get(operand, 0) + 1
            if isinstance(instruction, (ir.Index, ir.IndexAssign)):
                counts[instruction.base] = counts.get(instruction.base, 0) + 1
            elif isinstance(instruction, ir.Block):
                for name in _iter_ast_var_names(instruction.node):
                    counts[name] = counts.get(name, 0) + 1
            elif isinstance(instruction, ir.CarryBranch):
                for name in _iter_ast_var_names(instruction.call_ast):
                    counts[name] = counts.get(name, 0) + 1
        return counts

    @staticmethod
    def _count_definitions(body: list[ir.Instruction], /) -> dict[str, int]:
        """Return ``{name: def_count}`` covering every ``destination`` in *body*.

        Used by :meth:`_propagate` to exclude multi-def temps from
        propagation, and by :meth:`_dead_code_elimination` to ensure DCE
        only drops a temp's def when it is the *only* def (otherwise a
        sibling def on another control-flow path would still need to
        run, and the temp's use is alive via that path).
        """
        counts: dict[str, int] = {}
        for instruction in body:
            destination = _instruction_destination(instruction)
            if destination is not None:
                counts[destination] = counts.get(destination, 0) + 1
        return counts

    def _dead_code_elimination(self, body: list[ir.Instruction], /) -> list[ir.Instruction]:
        """Drop side-effect-free instructions whose destination temp has zero remaining uses.

        ``Call`` is kept (it may have side effects) but its ``destination``
        is set to ``None`` when no later instruction reads it, so the
        downstream codegen can skip allocating a slot or move for the
        result.
        """
        use_counts = self._compute_use_counts(body)
        result: list[ir.Instruction] = []
        for instruction in body:
            destination = _instruction_destination(instruction)
            if destination is None or not _is_temp(destination) or use_counts.get(destination, 0) > 0:
                result.append(instruction)
                continue
            if isinstance(instruction, ir.Call):
                if instruction.destination is None:
                    result.append(instruction)
                else:
                    result.append(dataclasses.replace(instruction, destination=None))
                continue
            if _has_side_effects(instruction):
                result.append(instruction)
                continue
            # BinaryOperation, Copy, Index — pure value producers whose
            # result no one reads.  Drop entirely.
        return result

    def _eliminate_dead_labels(self, body: list[ir.Instruction], /) -> list[ir.Instruction]:
        """Drop ``Label`` instructions that no branch (IR or AST-via-Block) targets.

        Removing a label cannot change semantics because nothing branches
        to it; control still falls through from the preceding instruction
        as it did before.  Re-iteration of the outer loop may then expose
        the preceding ``Jump`` as redundant if its target was the label
        immediately following the now-deleted one.

        References come from three sources: IR-level ``Jump`` /
        ``BranchFalse`` / ``CarryBranch`` targets, AST ``Goto`` nodes
        buried inside ``Block`` subtrees (where ``IndexedCall`` etc.
        lower to opaque AST handlers), and IR instructions nested
        inside ``ir.Switch`` case bodies (which the optimizer doesn't
        currently recurse into but whose targets still need to keep
        outer labels live).  All three must be considered or a user
        label referenced only from inside a switch arm would be
        incorrectly pruned.
        """
        referenced = self._collect_all_label_references(body)
        return [instruction for instruction in body if not (isinstance(instruction, ir.Label) and instruction.name not in referenced)]

    @staticmethod
    def _eliminate_tail_calls(body: list[ir.Instruction], /) -> list[ir.Instruction]:
        """Rewrite ``Call(destination=t) ; Return(value=t)`` as a single ``TailCall``.

        Fires only when the temp ``t`` has exactly one downstream use
        (the immediately-following ``Return``, skipping ``LoopBoundary``
        metadata).  Any other read of ``t`` means the value escapes the
        Call/Return pair, and a ``TailCall`` — which carries no
        destination — would break that observation.  Rejects callees
        whose name is in :data:`_NON_TAIL_CALLABLE_NAMES` or starts with
        ``_`` (covers ``__builtin_*`` and other internal hooks whose
        calling shape isn't the cdecl ``call`` / ``ret`` pair).
        """
        use_counts: dict[str, int] = {}
        for instruction in body:
            for operand in _instruction_value_operands(instruction):
                if isinstance(operand, str):
                    use_counts[operand] = use_counts.get(operand, 0) + 1
        result: list[ir.Instruction] = []
        index = 0
        while index < len(body):
            instruction = body[index]
            if (
                isinstance(instruction, ir.Call)
                and instruction.destination is not None
                and _is_temp(instruction.destination)
                and _is_tail_callable_name(instruction.name)
            ):
                return_index = _next_non_metadata_index(body=body, start=index + 1)
                if return_index is not None:
                    following = body[return_index]
                    if (
                        isinstance(following, ir.Return)
                        and following.value == instruction.destination
                        and use_counts.get(instruction.destination, 0) == 1
                    ):
                        result.extend(body[index + 1 : return_index])
                        result.append(ir.TailCall(args=instruction.args, name=instruction.name))
                        index = return_index + 1
                        continue
            result.append(instruction)
            index += 1
        return result

    @staticmethod
    def _eliminate_unreachable_code(body: list[ir.Instruction], /) -> list[ir.Instruction]:
        """Drop instructions strictly after an unconditional transfer until the next label.

        After ``Jump`` or ``Return`` control cannot reach the following
        instruction by fall-through.  Drop everything until the next
        ``Label`` re-establishes a possible entry point.  ``LoopBoundary``
        is preserved — the emission layer still needs the push/pop
        metadata even if the bracketed region is otherwise empty.
        """
        result: list[ir.Instruction] = []
        skipping = False
        for instruction in body:
            if isinstance(instruction, ir.Label):
                skipping = False
                result.append(instruction)
                continue
            if skipping:
                if isinstance(instruction, ir.LoopBoundary):
                    result.append(instruction)
                continue
            result.append(instruction)
            if _is_unconditional_transfer(instruction):
                skipping = True
        return result

    @staticmethod
    def _fold_constant_branches(body: list[ir.Instruction], /) -> list[ir.Instruction]:
        """Collapse ``BranchFalse`` whose operands are both integer literals.

        * Condition evaluates to **true** → the false-branch is never taken;
          drop the ``BranchFalse`` entirely.
        * Condition evaluates to **false** → the false-branch is always
          taken; replace with an unconditional ``Jump`` to the same target.

        Only ``==`` / ``!=`` are folded (see :data:`_FOLDABLE_COMPARISON_OPS`);
        ordered comparisons depend on signedness that the IR doesn't carry.
        """
        result: list[ir.Instruction] = []
        for instruction in body:
            if not isinstance(instruction, ir.BranchFalse):
                result.append(instruction)
                continue
            if not isinstance(instruction.left, int) or not isinstance(instruction.right, int):
                result.append(instruction)
                continue
            value = _evaluate_constant_comparison(left=instruction.left, operation=instruction.operation, right=instruction.right)
            if value is None:
                result.append(instruction)
                continue
            if value:
                # Condition true → BranchFalse never fires; drop it.
                continue
            # Condition false → always branch; lower to Jump.
            result.append(ir.Jump(target=instruction.target))
        return result

    def _forward_trivial_labels(self, body: list[ir.Instruction], /) -> list[ir.Instruction]:
        """Redirect branches through ``Label(L1)`` immediately followed by ``Jump(L2)``.

        For every branch (``Jump`` / ``BranchFalse`` / ``CarryBranch``) whose
        ``target == L1`` and where ``L1`` resolves to a trivial trampoline,
        rewrite the target to ``L2`` directly.  Chains of trampolines collapse
        in one pass via a resolution map: ``L1 → L2``, ``L2 → L3``, … all
        forward to the ultimate non-trampoline label.  The ``Label`` /
        ``Jump`` pair itself is left in place; the dead-label pass picks up
        the now-unreferenced label on a later iteration.

        Skipped for labels named by AST ``Goto`` references hidden inside
        ``Block``-wrapped subtrees — those references are hardcoded by name
        and a rewrite here would leave them pointing at a label the IR
        layer may later prune.
        """
        ast_referenced_labels = self._collect_ast_goto_targets(body)
        trampoline_target: dict[str, str] = {}
        for index, instruction in enumerate(body):
            if not isinstance(instruction, ir.Label):
                continue
            if instruction.name in ast_referenced_labels:
                continue
            next_index = _next_non_metadata_index(body=body, start=index + 1)
            if next_index is None:
                continue
            successor = body[next_index]
            if not isinstance(successor, ir.Jump):
                continue
            if successor.target == instruction.name:
                # Self-loop; not a trampoline.
                continue
            trampoline_target[instruction.name] = successor.target
        if not trampoline_target:
            return body
        # Resolve chains: if L1 -> L2 -> L3, both should ultimately point at L3.

        def _resolve(label: str, /) -> str:
            seen: set[str] = set()
            current = label
            while current in trampoline_target and current not in seen:
                seen.add(current)
                current = trampoline_target[current]
            return current

        result: list[ir.Instruction] = []
        for instruction in body:
            target = _branch_target(instruction)
            if target is None or target not in trampoline_target:
                result.append(instruction)
                continue
            resolved = _resolve(target)
            if resolved == target:
                result.append(instruction)
                continue
            result.append(_replace_branch_target(instruction, old=target, new=resolved))
        return result

    @staticmethod
    def _invert_branch_over_jump(body: list[ir.Instruction], /) -> list[ir.Instruction]:
        """Collapse ``BranchFalse(L1) ; Jump(L2) ; Label(L1)`` to ``BranchFalse_inv(L2) ; Label(L1)``.

        Eliminates the intermediate ``Jump`` by inverting the branch's
        comparison operator and retargeting it at the original jump's
        destination.  Equivalent transform applies to ``CarryBranch`` by
        flipping ``when`` between ``"set"`` and ``"clear"``.  ``LoopBoundary``
        between any two of the three instructions is tolerated as metadata.
        """
        result: list[ir.Instruction] = []
        index = 0
        while index < len(body):
            instruction = body[index]
            if not isinstance(instruction, (ir.BranchFalse, ir.CarryBranch)):
                result.append(instruction)
                index += 1
                continue
            jump_index = _next_non_metadata_index(body=body, start=index + 1)
            if jump_index is None or not isinstance(body[jump_index], ir.Jump):
                result.append(instruction)
                index += 1
                continue
            label_index = _next_non_metadata_index(body=body, start=jump_index + 1)
            if label_index is None or not isinstance(body[label_index], ir.Label):
                result.append(instruction)
                index += 1
                continue
            jump_instruction = body[jump_index]
            label_instruction = body[label_index]
            if label_instruction.name != instruction.target:
                result.append(instruction)
                index += 1
                continue
            if isinstance(instruction, ir.BranchFalse):
                inverted_operation = INVERT_COMPARISON.get(instruction.operation)
                if inverted_operation is None:
                    result.append(instruction)
                    index += 1
                    continue
                rewritten = dataclasses.replace(instruction, operation=inverted_operation, target=jump_instruction.target)
            else:  # CarryBranch
                flipped_when = "clear" if instruction.when == "set" else "set"
                rewritten = dataclasses.replace(instruction, target=jump_instruction.target, when=flipped_when)
            result.append(rewritten)
            # Emit any LoopBoundary metadata that lived between the branch
            # and the jump (we just dropped the jump from the middle).
            result.extend(body[index + 1 : jump_index])
            # Skip past the consumed Jump; the trailing Label and any
            # LoopBoundary between Jump and Label are emitted on the
            # next iteration of the outer loop.
            index = jump_index + 1
        return result

    def _optimize_body(
        self,
        body: list[ir.Instruction],
        /,
        *,
        carry_return: bool = False,
        excluded_ssa_names: frozenset[str] = frozenset(),
        signed_counters: dict[str, bool] | None = None,
        variable_element_sizes: dict[str, int] | None = None,
    ) -> list[ir.Instruction]:
        """Drive the IR-level optimization pipeline to fixed point.

        :meth:`_scalar_fixed_point` runs propagation + folding + CFG
        simplification + DCE in dependency order until the body stops
        changing.  Ordering matters: propagation feeds folding feeds
        branch-condition folding feeds CFG simplification (newly-constant
        branches expose dead code); DCE runs last so it sees the smallest
        live set; the outer iteration keeps composing them because, e.g.,
        dropping a dead label can expose a redundant ``Jump`` whose
        target collapsed.

        After the first scalar fixed point converges,
        :func:`cc.loops.recognize_string_loops` runs once — *before* the
        SSA round-trip — because SSA copy-propagates a fill / copy loop's
        induction-variable entry value into its comparison and index
        (the ``i++`` increment lowers to an opaque ``Block`` SSA can't see
        through), which erases the recognizable loop shape.  Collapsing the
        loop to a single :class:`cc.ir.RepString` first both lets the
        matcher fire and hands SSA / LICM / strength-reduction a clean node
        they leave untouched; the scalar pipeline then drops the now-dead
        IV init.

        The body next round-trips through SSA so the Phase 3 phi /
        copy-propagation passes can collapse joins the linear pipeline
        can't see; another scalar fixed-point cleans up the resulting
        copies.  Then :func:`cc.loops.hoist_loop_invariants` runs once, and
        the scalar pipeline runs one more time to clean up the rewritten
        loops (collapsing empty Jump-only preheaders that no instruction
        populated, propagating through newly-exposed dominance).

        Tail-call elimination runs once after fixed-point because it
        produces a terminator (``ir.TailCall``) that the unreachable-code
        pass inside ``_simplify_control_flow`` then uses to drop any
        stranded instructions before the next ``Label``.  Skipped for
        ``carry_return`` callers — their CF-based return shape cannot
        survive a tail ``jmp``.

        ``excluded_ssa_names`` is forwarded to :func:`cc.ssa.optimize_ssa`
        and :func:`cc.loops.hoist_loop_invariants` so neither renames
        nor hoists across names the caller knows are call-clobbered —
        typically program globals from :func:`_collect_global_names`.

        ``variable_element_sizes`` / ``signed_counters`` are the per-
        function type maps (from :func:`cc.loops.string_loop_type_maps`)
        that the rep-string recognizer uses to pick the ``rep`` width and
        drop the ``n <= 0`` guard for a provably unsigned counter.  Both
        ``None`` (no target supplied to the :class:`Optimizer`) skips the
        rep-string pass entirely.
        """
        current = self._scalar_fixed_point(list(body))
        # Rep-string recognition runs on the canonical pre-SSA loop shape
        # (``BranchFalse(left=IV, ...)`` with the IV's ``i++`` increment
        # still in the body).  The SSA round-trip below cannot see through
        # the ``Block(IncrementDecrement)`` that the IR builder emits for
        # ``i++``, so it copy-propagates the IV's entry value (0) into the
        # comparison / index and erases the recognizable shape — running
        # the matcher after SSA would never fire on real loops.  Lowering
        # the fill / copy to a single :class:`cc.ir.RepString` here also
        # hands SSA a clean node (count / fill_value are ordinary Value
        # operands) it leaves untouched, and spares LICM / strength
        # reduction from re-analyzing the collapsed loop.
        if (
            self._target is not None
            and (
                after_rep := recognize_string_loops(current, signed_counters=signed_counters, variable_element_sizes=variable_element_sizes)
            )
            != current
        ):
            current = self._scalar_fixed_point(after_rep)
        if (after_ssa := optimize_ssa(current, excluded_names=excluded_ssa_names)) != current:
            current = self._scalar_fixed_point(after_ssa)
        if (after_licm := hoist_loop_invariants(current, excluded_names=excluded_ssa_names)) != current:
            current = self._scalar_fixed_point(after_licm)
        if (after_lsr := reduce_loop_strength(current)) != current:
            current = self._scalar_fixed_point(after_lsr)
        if not carry_return:
            current = self._eliminate_tail_calls(current)
            current = self._eliminate_unreachable_code(current)
        return current

    def _propagate(self, body: list[ir.Instruction], /) -> list[ir.Instruction]:
        """Apply one forward pass of copy + constant propagation, then constant folding.

        Walks *body* once.  For every ``Copy(destination=tempN, source=value)``
        at index *i* whose destination has **exactly one** definition in the
        body, instructions at index > *i* have their ``Value`` operands equal
        to ``tempN`` rewritten to *value*.  Chained ``Copy`` of the form
        ``Copy(t1, ...)`` then ``Copy(t2, t1)`` collapse in one pass because
        substitution into ``t2`` happens before ``t2`` itself is propagated.
        Multi-def temps — produced by ``LogicalOr`` / ``LogicalAnd`` lowering,
        which writes the same temp in two branch arms — are excluded because
        the value reaching the use depends on the path taken; propagating
        either source would silently miscompile ``a || b`` to always-true or
        always-false.  After substitution, any ``BinaryOperation`` whose
        operands are both ``int`` is folded into a ``Copy`` of the result.
        """
        definition_counts = self._count_definitions(body)
        result = list(body)
        for index in range(len(result)):
            instruction = result[index]
            if not isinstance(instruction, ir.Copy):
                continue
            destination = instruction.destination
            if not _is_temp(destination):
                continue
            if definition_counts.get(destination, 0) != 1:
                continue
            for follow_index in range(index + 1, len(result)):
                result[follow_index] = _substitute_value(
                    result[follow_index],
                    source=instruction.source,
                    target=destination,
                )
        for index in range(len(result)):
            instruction = result[index]
            if isinstance(instruction, ir.BinaryOperation):
                folded = _fold_binary_operation(instruction)
                if folded is not None:
                    result[index] = folded
        return result

    def _scalar_fixed_point(self, body: list[ir.Instruction], /) -> list[ir.Instruction]:
        """Run propagation + control-flow simplification + DCE until *body* stops changing.

        Single pre-SSA, post-SSA, and post-LICM driver for the scalar
        pipeline.  Each pass converges quickly in practice but each can
        re-expose work for the next (dropping a dead label exposes a
        redundant ``Jump`` whose target collapsed, etc.), so the outer
        iteration keeps composing the three passes until no rewrite
        fires.  ``previous = None`` guarantees the first iteration runs
        even when *body* is already at a fixed point under separate
        invocations.
        """
        current = body
        previous: list[ir.Instruction] | None = None
        while current != previous:
            previous = current
            current = self._dead_code_elimination(self._simplify_control_flow(self._propagate(current)))
        return current

    def _simplify_control_flow(self, body: list[ir.Instruction], /) -> list[ir.Instruction]:
        """Apply the IR-level CFG passes in dependency order.

        1. Fold constant-condition branches to expose dead arms.
        2. Forward trivial-trampoline labels so later passes see real targets.
        3. Invert ``BranchFalse + Jump + Label`` to drop the intermediate jump.
        4. Drop ``Jump(L)`` immediately followed by ``Label(L)``.
        5. Remove unreachable code after unconditional transfers.
        6. Prune labels nothing branches to.
        """
        body = self._fold_constant_branches(body)
        body = self._forward_trivial_labels(body)
        body = self._invert_branch_over_jump(body)
        body = self._collapse_jump_to_next_label(body)
        body = self._eliminate_unreachable_code(body)
        return self._eliminate_dead_labels(body)
