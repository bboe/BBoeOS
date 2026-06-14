"""Natural-loop detection over the basic-block CFG.

A *natural loop* is identified by a back-edge ``latch → header`` where
``header`` dominates ``latch`` in the CFG's dominator tree.  The loop body
is ``header`` plus every block that can reach ``latch`` without going
through ``header`` — i.e., everything ``header`` dominates that can flow
back into the loop.  Multiple back-edges to the same header (e.g., a
``while`` loop with several ``continue`` paths) coalesce into a single
:class:`NaturalLoop` with multiple latches.

Irreducible CFGs — those produced by ``goto`` jumping into the middle of
a loop — have back-edges whose target does *not* dominate the source.
Such regions are silently skipped: :func:`natural_loops` only reports
true natural loops, so downstream optimizations (preheader insertion,
LICM, future strength reduction) never have to special-case irreducible
control flow.  Callers that need to know whether irreducibility occurred
can compare against the raw back-edge count themselves.

:func:`natural_loops` produces immutable :class:`NaturalLoop` records over
an unmodified CFG.  :func:`insert_preheaders` is the canonical CFG rewrite
that the LICM-style optimizations need: for every loop, it inserts a fresh
block ahead of the header so the header has exactly one non-latch
predecessor.  Hoisted invariants can then be appended to the preheader
without affecting the other paths into the loop.

:func:`hoist_loop_invariants` is the end-to-end LICM driver: it builds the
CFG, identifies natural loops, inserts preheaders, and moves every
loop-invariant instruction from the body to its preheader.  An instruction
is invariant when (a) it is a pure, speculatable kind, (b) its
destination has exactly one definition in the function (so hoisting
cannot change observable behavior under any execution path), and (c)
every operand is a literal, a name defined outside the loop, or the
destination of another invariant instruction in the same loop.
"""

from __future__ import annotations

import dataclasses
import itertools
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cc import ast_nodes, ir
from cc.cfg import BasicBlock, ControlFlowGraph, build_cfg, compute_dominators, flatten_cfg

if TYPE_CHECKING:
    from collections.abc import Iterator

    from cc.target import X86CodegenTarget

#: Local-name template for accumulators introduced by
#: :func:`reduce_loop_strength`.  Each multiplicative reduction allocates
#: a fresh accumulator whose name is guaranteed unique across the
#: function by the per-invocation counter.  The ``_ir_`` prefix matches
#: the convention :meth:`cc.codegen.base.CodegenBase._collect_ir_temps`
#: scans for when allocating stack slots — without it the codegen would
#: see the accumulator as an undeclared name and fail to emit a frame
#: slot for it.
_ACCUMULATOR_NAME_TEMPLATE = "_ir_lsr_acc_{counter}"

#: Synthetic-label template used for preheader blocks inserted by
#: :func:`insert_preheaders`.  The leading ``.`` matches the IR-level
#: convention for branch targets so backend label resolution treats the
#: preheader like any other compiler-generated label.
_PREHEADER_LABEL_TEMPLATE = ".licm_preheader_{counter}"


@dataclass(eq=False, frozen=True, kw_only=True, slots=True)
class NaturalLoop:
    """One natural loop in a function's CFG.

    A natural loop is uniquely identified by its *header*: the single block
    dominating every block in *body*.  *latches* are the body blocks with
    a back-edge to *header* (multiple latches happen when a loop has
    several ``continue`` paths or a do-while inside a for).  *exits* are
    body blocks with at least one successor outside *body*.

    All collection fields are :class:`frozenset` so the loop record is
    hashable by identity and safe to share between analyses without
    accidental mutation.
    """

    body: frozenset[BasicBlock]
    exits: frozenset[BasicBlock]
    header: BasicBlock
    latches: frozenset[BasicBlock]


def _apply_string_rewrites(
    body: list[ir.Instruction],
    /,
    *,
    rewrites: dict[NaturalLoop, tuple[str, ir.RepString]],
) -> list[ir.Instruction]:
    """Splice each matched loop's ``RepString`` into *body*, removing the loop's instructions.

    *rewrites* maps each matched loop to ``(header_label, rep_string)``.
    For each loop we replace the contiguous flat-list span running from
    the header :class:`cc.ir.Label` through the back-:class:`cc.ir.Jump`
    (the latch edge targeting the header) — inclusive — with the single
    ``RepString``.  The preceding IV initialization (``Copy(IV, 0)``) and
    the trailing end :class:`cc.ir.Label` are deliberately left in place:
    the end label keeps any branch to it resolvable, and the now-dead
    init copy is removed by the optimizer's later DCE fixed point.

    Spans are processed right-to-left so earlier indices stay valid while
    splicing.  A loop whose span cannot be located (defensive — should
    not happen for a matched canonical loop) is skipped.
    """
    spans: list[tuple[int, int, ir.RepString]] = []
    for header_label, rep_string in rewrites.values():
        start = next(
            (index for index, instruction in enumerate(body) if isinstance(instruction, ir.Label) and instruction.name == header_label),
            None,
        )
        if start is None:
            continue
        end = max(
            (index for index, instruction in enumerate(body) if isinstance(instruction, ir.Jump) and instruction.target == header_label),
            default=None,
        )
        if end is None or end < start:
            continue
        spans.append((start, end, rep_string))
    if not spans:
        return body
    result = list(body)
    # Splice right-to-left so earlier indices stay valid as spans shrink.
    spans.sort()
    # Insurance: with fill + copy + nesting all live, two matched loops
    # must never claim overlapping flat-list spans — splicing overlapping
    # ranges right-to-left would corrupt the IR (the later splice would
    # land inside a region the earlier one already replaced).  Cheap
    # adjacent-pair check on the sorted spans suffices.
    for (_, previous_end, _), (next_start, _, _) in itertools.pairwise(spans):
        assert next_start > previous_end, f"overlapping rep-string rewrite spans: ...{previous_end}] and [{next_start}..."
    for start, end, rep_string in reversed(spans):
        result[start : end + 1] = [rep_string]
    return result


def _assignment_destination(instruction: ir.Instruction, /) -> str | None:
    """Return the name written by *instruction*, or None when it writes no scalar slot.

    Recognises both IR-native instructions (``Copy``, ``BinaryOperation``,
    ``Index``, ``Call``) via their ``destination`` field **and** the
    AST-escape-hatch form ``Block(node=ast_nodes.Assign(name=…))`` that
    the IR builder emits for ``x = x op K`` self-modifies — the
    builder routes those to the AST codegen for tighter ``inc`` /
    ``add [mem], imm`` emission, but the analysis here still needs to
    see them as writes so an induction variable updated through that
    path is recognised.

    For the ``i++`` / ``++i`` shape (``Block(node=Assign(name=<temp>,
    expr=PlaceIncrementDecrement(place=VariablePlace(i))))``) the *mutated* variable is
    the ``PlaceIncrementDecrement`` place's name, not the ``Assign.name`` (which
    is the discarded result temp).  Report the mutated variable so the
    induction-variable scan counts the real write — the discarded temp is
    dead and never participates in the linear IV relationship.
    """
    destination = getattr(instruction, "destination", None)
    if isinstance(destination, str):
        return destination
    if isinstance(instruction, ir.Block) and isinstance(instruction.node, ast_nodes.Assign):
        if isinstance(instruction.node.expr, ast_nodes.PlaceIncrementDecrement) and isinstance(
            instruction.node.expr.place, ast_nodes.VariablePlace
        ):
            return instruction.node.expr.place.name
        return instruction.node.name
    return None


def _ast_takes_address_of(node: object, /, *, name: str) -> bool:
    """Return True when *name*'s address is taken (``&name``) in the AST subtree at *node*.

    Walks the subtree looking for a ``&name``
    (``PlaceAddressOf(VariablePlace(name))``).  ``&name`` is the only C construct that
    leaks a scalar local's address, so a precise scan for that one node
    kind is sound — far tighter than rejecting on any textual mention of
    the name (which would turn away a loop merely because its own IV
    appears in the increment).
    """
    if ast_nodes.address_of_variable_name(node) == name:
        return True
    if dataclasses.is_dataclass(node):
        return any(_ast_takes_address_of(getattr(node, declared_field.name), name=name) for declared_field in dataclasses.fields(node))
    if isinstance(node, (list, tuple)):
        return any(_ast_takes_address_of(item, name=name) for item in node)
    return False


def _back_edges(cfg: ControlFlowGraph, /, *, dominators_of: dict[BasicBlock, frozenset[BasicBlock]]) -> list[tuple[BasicBlock, BasicBlock]]:
    """Return ``[(latch, header), ...]`` for every back-edge in *cfg*.

    A back-edge is an edge ``latch → header`` where ``header`` dominates
    ``latch``.  Edges from unreachable blocks are skipped — they have no
    entry in *dominators_of* and contribute no real loop.
    """
    edges: list[tuple[BasicBlock, BasicBlock]] = []
    for block in cfg.blocks:
        if block not in dominators_of:
            continue
        edges.extend((block, successor) for successor in block.successors if successor in dominators_of[block])
    return edges


def _collect_array_and_scalar_types(
    statements: list[ast_nodes.Node],
    /,
    *,
    array_element_types: dict[str, str],
    scalar_types: dict[str, str],
) -> None:
    """Populate *array_element_types* / *scalar_types* from a statement list.

    :class:`cc.ast_nodes.ArrayDecl` names map to their *element* type
    (``type_name`` — e.g. ``"unsigned char"`` for ``unsigned char buf[N]``)
    in *array_element_types*.  :class:`cc.ast_nodes.VarDecl` names map to
    their full declared type (``type_name`` — e.g. ``"unsigned char*"`` or
    ``"unsigned int"``) in *scalar_types*.

    Nested blocks (``if`` / ``while`` / ``do``-``while`` / ``switch``
    arms) are recursed into so a base or counter declared inside a branch
    is still recorded — mirrors :meth:`cc.ir.Builder._collect_local_types`.
    """
    for statement in statements:
        if isinstance(statement, ast_nodes.ArrayDecl):
            array_element_types[statement.name] = statement.type_name
        elif isinstance(statement, ast_nodes.VarDecl):
            scalar_types[statement.name] = statement.type_name
        elif isinstance(statement, ast_nodes.If):
            _collect_array_and_scalar_types(statement.body, array_element_types=array_element_types, scalar_types=scalar_types)
            if statement.else_body is not None:
                _collect_array_and_scalar_types(statement.else_body, array_element_types=array_element_types, scalar_types=scalar_types)
        elif isinstance(statement, (ast_nodes.Compound, ast_nodes.DoWhile, ast_nodes.While)):
            _collect_array_and_scalar_types(statement.body, array_element_types=array_element_types, scalar_types=scalar_types)
        elif isinstance(statement, ast_nodes.Switch):
            for case in statement.cases:
                _collect_array_and_scalar_types(case.body, array_element_types=array_element_types, scalar_types=scalar_types)


def _count_destination_definitions(cfg: ControlFlowGraph, /) -> dict[str, int]:
    """Return ``{name: definition_count}`` across every instruction in *cfg*.

    Used by :func:`hoist_loop_invariants` to enforce the single-definition
    safety condition: a hoist candidate's destination must be defined
    exactly once in the function so the move does not change observable
    behavior under any execution path.
    """
    counts: dict[str, int] = {}
    for block in cfg.blocks:
        for instruction in block.instructions:
            destination = getattr(instruction, "destination", None)
            if isinstance(destination, str):
                counts[destination] = counts.get(destination, 0) + 1
    return counts


def _count_loop_definitions(body_block_order: list[BasicBlock], /) -> dict[str, int]:
    """Return ``{name: definition_count}`` restricted to instructions inside *body_block_order*.

    Counterpart to :func:`_count_destination_definitions` (which counts
    over the entire CFG): induction-variable detection wants a count
    scoped to the loop body, since an IV always has a separate
    initialization outside the loop and the in-loop increment is the one
    we actually care about.  Globally counting both would make every
    real IV look multi-def and reject every reduction candidate.
    """
    counts: dict[str, int] = {}
    for block in body_block_order:
        for instruction in block.instructions:
            destination = _assignment_destination(instruction)
            if destination is not None:
                counts[destination] = counts.get(destination, 0) + 1
    return counts


def _dominator_sets(idom: dict[BasicBlock, BasicBlock], /) -> dict[BasicBlock, frozenset[BasicBlock]]:
    """Return ``{block: set_of_dominators_including_self}`` from immediate-dominator map *idom*.

    Walks up the idom chain for each block until reaching the entry
    sentinel (a block that is its own immediate dominator).  Used to
    test the dominance condition for back-edges in O(1) lookup after
    O(N log N) precomputation.
    """
    result: dict[BasicBlock, frozenset[BasicBlock]] = {}
    for block in idom:
        chain: set[BasicBlock] = {block}
        runner = block
        while (parent := idom[runner]) is not runner:
            chain.add(parent)
            runner = parent
        result[block] = frozenset(chain)
    return result


def _element_size_for_type(*, element_type: str | None, is_array: bool, pointer_type: str | None, target: X86CodegenTarget) -> int | None:
    """Return ``sizeof(element)`` for an array/pointer base, or None when not indexable.

    Mirrors :meth:`cc.codegen.base.CodegenBase._index_pointee_size`:

    * an *array* base resolves to ``target.type_size(element_type)`` — the
      width of one declared element (``unsigned short buf[N]`` → 2;
      ``int *names[N]`` → ``int_size`` since the element type is ``int*``);
    * a *pointer* base (``pointer_type`` ends in ``*``) strips the ``*``
      and resolves ``target.type_size(pointee)``.

    Unknown types resolve to ``target.int_size`` (the historical default).
    Returns None when the base is neither an array nor a pointer — such a
    name is not a rep-string base and gets no map entry.
    """
    if is_array and element_type is not None:
        try:
            return target.type_size(element_type)
        except KeyError:
            return target.int_size
    if pointer_type is not None and pointer_type.endswith("*"):
        pointee = pointer_type[:-1].rstrip()
        try:
            return target.type_size(pointee)
        except KeyError:
            return target.int_size
    return None


def _element_size_of(name: str, /, *, variable_element_sizes: dict[str, int] | None) -> int | None:
    """Return the element width (bytes) for base *name*, or None when unknown.

    Looks *name* up in *variable_element_sizes*.  When the map is absent
    or lacks the base the size is *unknown* and this returns ``None`` —
    the matchers then leave the loop scalar rather than guessing a width.
    Byte-defaulting on an unknown base was unsafe: a base the type
    collector missed (most notably a file-scope / global array whose real
    element width is 4) would be lowered to ``rep stosb`` / ``rep movsb``
    and miscompile.  Rejecting on unknown turns every such gap into a
    missed optimization instead of a wrong-width store.
    """
    if variable_element_sizes is None:
        return None
    return variable_element_sizes.get(name)


def _find_induction_variables(body_block_order: list[BasicBlock], /, *, loop_definition_count: dict[str, int]) -> dict[str, int]:
    """Return ``{iv_name: integer_step}`` for every simple additive induction variable in the loop body.

    A *simple additive IV* is a name with exactly one definition in the
    loop body of the form ``X = X + literal`` or ``X = X - literal``
    where ``literal`` is an ``int``.  The single-definition requirement
    means the IV value at any point in the loop body is the preheader
    value plus ``iterations_so_far * step``; without it, an intervening
    write would break the linear relationship that strength reduction
    relies on.

    Outside-the-loop definitions (typically the IV initialization)
    don't count — this is why the caller passes a *loop-local*
    definition count rather than the CFG-wide one.

    Both the IR-native ``BinaryOperation`` form and the AST-escape-hatch
    ``Block(node=Assign(...))`` form qualify — the IR builder routes
    ``x = x op K`` to the latter for tighter codegen, and LSR would
    miss every typical ``for (i = 0; ...; i = i + 1)`` loop without it.
    """
    candidates: dict[str, int] = {}
    for block in body_block_order:
        for instruction in block.instructions:
            destination = _assignment_destination(instruction)
            if destination is None:
                continue
            if loop_definition_count.get(destination, 0) != 1:
                continue
            step = _self_increment_step(instruction, name=destination)
            if step is not None:
                candidates[destination] = step
    return candidates


def _find_multiplicative_iv_uses(
    body_block_order: list[BasicBlock],
    /,
    *,
    definition_count: dict[str, int],
    induction_variables: dict[str, int],
) -> list[tuple[BasicBlock, int, ir.BinaryOperation, str, int]]:
    """Return every ``T = IV * const`` (or ``T = const * IV``) candidate eligible for strength reduction.

    Each entry is ``(block, instruction_index, instruction, iv_name, mul_constant)``.
    The candidate's destination ``T`` must be defined exactly once in
    the entire function — otherwise replacing the multiply with a copy
    of an accumulator could disagree with another write to ``T`` reached
    along a different control-flow path.
    """
    results: list[tuple[BasicBlock, int, ir.BinaryOperation, str, int]] = []
    for block in body_block_order:
        for index, instruction in enumerate(block.instructions):
            if not isinstance(instruction, ir.BinaryOperation):
                continue
            if instruction.operation != "*":
                continue
            if definition_count.get(instruction.destination, 0) != 1:
                continue
            iv_name: str | None = None
            mul_constant: int | None = None
            if isinstance(instruction.left, str) and instruction.left in induction_variables and isinstance(instruction.right, int):
                iv_name = instruction.left
                mul_constant = instruction.right
            elif isinstance(instruction.right, str) and instruction.right in induction_variables and isinstance(instruction.left, int):
                iv_name = instruction.right
                mul_constant = instruction.left
            if iv_name is not None and mul_constant is not None:
                results.append((block, index, instruction, iv_name, mul_constant))
    return results


def _hoist_invariants_into_preheader(
    *,
    body_block_order: list[BasicBlock],
    definition_count: dict[str, int],
    dominator_sets: dict[BasicBlock, frozenset[BasicBlock]],
    excluded_names: frozenset[str],
    loop_exits: frozenset[BasicBlock],
    preheader: BasicBlock,
) -> bool:
    """Identify invariant instructions across *body_block_order* and move them into *preheader*.

    Iterates to fixed point: each pass marks instructions whose
    operands are now invariant (literals, loop-external names, or
    destinations of already-marked invariant instructions).  Marked
    instructions are physically removed from their body block and
    appended to *preheader* in mark order, which is a topological
    order over the invariant dependency graph.

    *body_block_order* gives the loop's body blocks in a deterministic
    order so the preheader's instruction sequence is reproducible across
    runs (set iteration would depend on object hashes otherwise,
    producing different frame layouts on different invocations).

    *excluded_names* is the set of globals / call-clobbered names; when
    the loop body contains any call, those names are conservatively
    treated as defined inside the loop (a callee may have written
    through the underlying slot).

    *loop_exits* and *dominator_sets* gate the safety check that
    speculative load hoisting requires (see below): a load may be
    lifted out of the loop only when its containing block dominates
    every exit, so the value would have been read on every path the
    loop body actually takes to an exit.

    Returns True when any instruction was hoisted.
    """
    names_defined_in_loop = _names_defined_in_loop(body_block_order=body_block_order)
    has_call = any(
        isinstance(instruction, (ir.Call, ir.CarryBranch, ir.TailCall)) for block in body_block_order for instruction in block.instructions
    ) or any(isinstance(block.terminator, (ir.CarryBranch, ir.TailCall)) for block in body_block_order)
    if has_call:
        names_defined_in_loop |= excluded_names
    # An ``ir.Index`` (memory load) is only hoistable when no instruction
    # in the loop can mutate memory the load might read.  ``IndexAssign``
    # is the direct write; ``Call`` / ``CarryBranch`` / ``TailCall``
    # could mutate any memory through a pointer the callee receives.
    # When any of those appear, leave loads in place — alias analysis
    # is out of scope.
    has_memory_writer = has_call or any(
        isinstance(instruction, ir.IndexAssign) for block in body_block_order for instruction in block.instructions
    )
    block_of: dict[int, BasicBlock] = {id(instruction): block for block in body_block_order for instruction in block.instructions}
    invariant: dict[int, ir.Instruction] = {}
    invariant_destinations: set[str] = set()
    changed = True
    while changed:
        changed = False
        for block in body_block_order:
            for instruction in block.instructions:
                if id(instruction) in invariant:
                    continue
                if not _is_hoistable_kind(instruction):
                    continue
                destination = getattr(instruction, "destination", None)
                if not isinstance(destination, str):
                    continue
                if definition_count.get(destination, 0) != 1:
                    continue
                if not _operands_invariant(
                    instruction, invariant_destinations=invariant_destinations, names_defined_in_loop=names_defined_in_loop
                ):
                    continue
                if isinstance(instruction, ir.Index):
                    # Memory-load safety: skip when any writer is in the
                    # loop, when the base pointer is itself loop-defined
                    # (the ``base`` field is a name, not a ``Value``, so
                    # ``_operands_invariant`` does not see it), or when
                    # the containing block does not dominate every exit
                    # — speculatively hoisting a load past a guard the
                    # loop's body would have evaluated would introduce a
                    # fault on a control-flow path the original program
                    # never took.
                    if has_memory_writer:
                        continue
                    if instruction.base in names_defined_in_loop:
                        continue
                    if not all(block_of[id(instruction)] in dominator_sets[exit_block] for exit_block in loop_exits):
                        continue
                invariant[id(instruction)] = instruction
                invariant_destinations.add(destination)
                changed = True
    if not invariant:
        return False
    invariant_ids = set(invariant)
    for block in body_block_order:
        block.instructions = [instruction for instruction in block.instructions if id(instruction) not in invariant_ids]
    preheader.instructions.extend(invariant.values())
    return True


def _is_hoistable_kind(instruction: ir.Instruction, /) -> bool:
    """Return True if *instruction* is a pure-or-speculatable kind that LICM can consider.

    :class:`cc.ir.BinaryOperation` has no observable side effects and
    cannot fault, so it is hoistable whenever its operands are
    invariant.  :class:`cc.ir.Index` (memory load) is hoistable only
    when the caller's additional safety checks pass (no memory writer
    in the loop, base is loop-invariant, the load's block dominates
    every loop exit so the speculative read cannot fault on a path the
    body would have rejected).  :class:`cc.ir.Copy` is intentionally
    excluded: copy propagation already removes loop-local copies; a
    hoist would churn the IR without enabling further wins.
    """
    return isinstance(instruction, (ir.BinaryOperation, ir.Index))


def _iter_ast_read_names(node: object, /) -> Iterator[str]:
    """Yield only the names *read* (not declared / assigned-to) in the AST subtree at *node*.

    Unlike :func:`_iter_ast_referenced_names` (which yields every
    string-valued field, including declaration and write-target names),
    this walker distinguishes reads from writes so the IV-liveness and
    IV-escape guards don't false-reject a perfectly ordinary loop:

    * :class:`cc.ast_nodes.Var` — a value read, yields ``name``;
    * ``&name`` (:class:`cc.ast_nodes.PlaceAddressOf` over a
      :class:`cc.ast_nodes.VariablePlace`) — yields the target name
      (an address-of counts as a use of the variable);
    * :class:`cc.ast_nodes.PlaceIncrementDecrement` — the place's named variable is both
      read and written, so it counts as a read;
    * :class:`cc.ast_nodes.VarDecl` — the declared ``name`` is *not* a
      read; recurse only into the initializer;
    * :class:`cc.ast_nodes.Assign` — the assigned ``name`` is a write
      target, not a read; recurse only into the right-hand side ``expr``.

    Every other dataclass / list / tuple is walked structurally.  This is
    the read-set the rep-string matcher needs: a ``Block(VarDecl(name=i))``
    declaration or the ``Block(Assign(name=_ir_t, PlaceIncrementDecrement(VariablePlace(i))))``
    increment must not be mistaken for an after-loop *read* of ``i``.

    Yields:
        Each read scalar name encountered in the subtree, in source order.

    """
    if isinstance(node, ast_nodes.Var):
        yield node.name
        return
    if (taken_name := ast_nodes.address_of_variable_name(node)) is not None:
        yield taken_name
        return
    if isinstance(node, ast_nodes.PlaceIncrementDecrement):
        # ``x++`` / ``a[i]++`` — the place's named variable is both read
        # and written, so it counts as a read (mirroring the legacy
        # increment-decrement target yield); compound places (a[i]++)
        # also read their index Vars via the structural walk below.
        if isinstance(node.place, ast_nodes.VariablePlace):
            yield node.place.name
            return
        yield from _iter_ast_read_names(node.place)
        return
    if isinstance(node, ast_nodes.VarDecl):
        if node.init is not None:
            yield from _iter_ast_read_names(node.init)
        return
    if isinstance(node, ast_nodes.Assign):
        yield from _iter_ast_read_names(node.expr)
        return
    if dataclasses.is_dataclass(node):
        for declared_field in dataclasses.fields(node):
            yield from _iter_ast_read_names(getattr(node, declared_field.name))
        return
    if isinstance(node, (list, tuple)):
        for item in node:
            yield from _iter_ast_read_names(item)


def _iter_ast_referenced_names(node: object, /) -> Iterator[str]:
    """Yield every string-valued dataclass field anywhere in the AST subtree at *node*.

    A superset of :func:`cc.ssa._iter_ast_var_names`: that walker only
    yields :class:`cc.ast_nodes.Var` reads, but the LICM invariance
    check also needs to see declarations and assignments that write to
    loop-local names through fields that vary by AST node type —
    ``VarDecl.name``, ``ArrayDecl.name``, ``Assign.name``,
    :class:`cc.ast_nodes.PlaceIncrementDecrement` place names, etc.

    Walking *every* string-valued dataclass field is conservative — it
    yields type-name strings, operation symbols, and other non-variable
    identifiers that happen to be stored as strings — but those almost
    never collide with real local-variable names, while a missed entry
    would silently move a loop-defined local's use to before its def.
    Missed entry = miscompile; extra entry = forfeited hoist.

    Yields:
        Each string-valued dataclass field encountered, in source order.

    """
    if dataclasses.is_dataclass(node):
        for declared_field in dataclasses.fields(node):
            value = getattr(node, declared_field.name)
            if isinstance(value, str):
                yield value
                continue
            yield from _iter_ast_referenced_names(value)
        return
    if isinstance(node, (list, tuple)):
        for item in node:
            yield from _iter_ast_referenced_names(item)


def _iter_read_names(instruction: ir.Instruction, /) -> Iterator[str]:
    """Yield every scalar *name* read by *instruction*, never its destination.

    Covers three sources of name references:

    * value operands walked by :func:`_iter_value_operands` — a bare
      ``str`` is a name, and a ``&name``
      (:class:`cc.ast_nodes.PlaceAddressOf` over a
      :class:`cc.ast_nodes.VariablePlace`) operand
      contributes its name (the IV whose address is taken);
    * the pointer-base fields of :class:`cc.ir.Index` /
      :class:`cc.ir.IndexAssign`, which are plain ``str`` names *not*
      listed in ``VALUE_FIELDS``;
    * the pointer-base ``dest`` / ``source`` fields of
      :class:`cc.ir.RepString` (the rep reads both as bases) and the
      name carried by its ``final_iv`` tuple — none of which are in
      ``VALUE_FIELDS`` (only ``count`` / ``fill_value`` are);
    * every name *read* inside an AST escape hatch
      (:class:`cc.ir.Block`, :class:`cc.ir.CarryBranch`,
      :class:`cc.ir.Switch`) via :func:`_iter_ast_read_names` — these wrap
      opaque AST subtrees that may read the IV through fields the
      value-operand walk cannot see.  Crucially this is the *read* walk,
      not the every-string walk: a ``Block(VarDecl(name=i))`` declaration
      or a ``Block(Assign(name=_ir_t, PlaceIncrementDecrement(VariablePlace(i))))`` increment
      must not register as an after-loop read of ``i`` (the former is a
      declaration, the latter an in-loop write), or every real
      ``for (i = 0; ...; i++)`` loop would false-reject.

    Used by the post-loop IV-liveness scan (so a name read after the
    loop region rejects the rewrite).

    Yields:
        Each referenced scalar name, in walk order (value operands
        first, then the index base, then escape-hatch strings).

    """
    for operand in _iter_value_operands(instruction):
        if isinstance(operand, str):
            yield operand
        elif (taken_name := ast_nodes.address_of_variable_name(operand)) is not None:
            yield taken_name
    if isinstance(instruction, (ir.Index, ir.IndexAssign)):
        yield instruction.base
    if isinstance(instruction, ir.RepString):
        yield instruction.dest
        if instruction.source is not None:
            yield instruction.source
        if instruction.final_iv is not None and isinstance(instruction.final_iv[1], str):
            yield instruction.final_iv[1]
    if isinstance(instruction, (ir.Access, ir.Block, ir.CarryBranch, ir.Switch)):
        yield from _iter_ast_read_names(instruction)


def _iter_value_operands(instruction: ir.Instruction, /) -> Iterator[ir.Value]:
    """Yield each non-destination :data:`cc.ir.Value` operand read by *instruction*.

    Walks the :attr:`cc.ir.Instruction.VALUE_FIELDS` class-level field list
    so new instruction kinds participate by declaration alone.  Parallel
    to :func:`cc.ssa._iter_value_operands`; kept local here to avoid
    pulling the optimizer's private API into the loops module.

    Yields:
        Each ``Value`` operand in declaration order.  Tuple-typed fields
        (``Call.args``, ``TailCall.args``) are flattened element-wise.

    """
    for field_name in instruction.VALUE_FIELDS:
        value = getattr(instruction, field_name)
        if value is None:
            continue
        if isinstance(value, tuple):
            yield from value
        else:
            yield value


def _iv_address_taken_in_loop(body_block_order: list[BasicBlock], /, *, induction_variable: str) -> bool:
    """Return True when *induction_variable*'s address is taken anywhere in the loop body.

    A ``&iv`` (``PlaceAddressOf(VariablePlace)``) of the IV — as a direct value
    operand or nested inside a :class:`cc.ir.Block` / ``CarryBranch`` /
    ``Switch`` AST subtree — lets a callee mutate the counter through the
    leaked pointer, breaking the ``count == bound`` linear relationship
    the rewrite relies on.  Conservatively reject when it occurs.

    This guard is *not* redundant with the non-idiomatic-body rejection:
    ``IndexAssign.source`` is a ``Value`` and ``Value`` admits
    ``PlaceAddressOf``, so a bare single-instruction fill
    ``buf[i] = &i;`` is an idiomatic body the matcher would otherwise
    accept — only this guard rejects it.  See
    ``test_recognize_fill_loop_rejects_iv_address_taken_inside_idiomatic_body``.
    """
    for block in body_block_order:
        terminator = () if block.terminator is None else (block.terminator,)
        for instruction in (*block.instructions, *terminator):
            for operand in _iter_value_operands(instruction):
                if ast_nodes.address_of_variable_name(operand) == induction_variable:
                    return True
            # An opaque AST subtree could take the IV's address — but the
            # *only* way to do so in C is a ``&iv`` node,
            # so scan precisely for ``&iv`` rather than rejecting on any
            # mention.  A bare mention (the IV's own ``i++`` increment, a
            # ``Var(i)`` read) is harmless and must not reject, or every
            # real ``for (i = 0; ...; i++)`` loop would be turned away.
            if isinstance(instruction, (ir.Access, ir.Block, ir.CarryBranch, ir.Switch)) and _ast_takes_address_of(
                instruction, name=induction_variable
            ):
                return True
    return False


def _iv_live_after_loop(loop: NaturalLoop, /, *, cfg: ControlFlowGraph, induction_variable: str) -> bool:
    """Return True when *induction_variable* may be read outside *loop*'s body.

    The rewrite never materializes the IV, so any read of it outside the
    loop body would observe garbage (the init ``Copy(IV, 0)`` is later
    DCE'd and the ``RepString`` leaves no IV value behind).  We therefore
    reject when the IV name appears as a read in *any* basic block that is
    not part of ``loop.body`` — as a value operand, a
    ``&name`` (:class:`cc.ast_nodes.PlaceAddressOf`) target, an ``Index`` / ``IndexAssign``
    base, a ``RepString`` field, or anywhere inside an AST escape hatch.

    This is CFG-based rather than a flat-suffix scan, so it stays sound
    under ``goto`` / irreducible control flow where a post-loop use can
    live in a block positioned textually *before* the loop header yet
    reached only after the loop exits (a flat ``body[end+1:]`` scan would
    miss it).  The header is itself a member of ``loop.body`` and is
    therefore excluded.

    Conservative by construction: it also rejects loops whose IV is read
    in a pre-loop block (a missed optimization, not a miscompile).  Reads
    are gathered per block by :func:`_iter_read_names` over every
    instruction and the block terminator.
    """
    for block in cfg.blocks:
        if block in loop.body:
            continue
        terminator = () if block.terminator is None else (block.terminator,)
        for instruction in (*block.instructions, *terminator):
            if induction_variable in set(_iter_read_names(instruction)):
                return True
    return False


def _loop_body(header: BasicBlock, /, *, latches: frozenset[BasicBlock]) -> frozenset[BasicBlock]:
    """Return blocks reachable backward from *latches* without passing through *header*.

    Standard natural-loop body construction: start from each non-header
    latch and walk predecessors, stopping at *header*.  Self-looping
    latches (``latch is header``) contribute no walk — the loop body
    is just ``{header}`` in that case.  Without the ``is not header``
    filter, the walk would step out of the loop entirely on the
    self-loop case (since the latch's predecessors include the entry
    block that falls through to header), pulling pre-loop code into
    the body and letting LICM rewrite it.
    """
    body: set[BasicBlock] = {header, *latches}
    stack: list[BasicBlock] = [latch for latch in latches if latch is not header]
    while stack:
        block = stack.pop()
        for predecessor in block.predecessors:
            if predecessor is header or predecessor in body:
                continue
            body.add(predecessor)
            stack.append(predecessor)
    return frozenset(body)


def _loop_exits(body: frozenset[BasicBlock], /) -> frozenset[BasicBlock]:
    """Return body blocks with at least one successor outside *body* — the loop's exits."""
    return frozenset(block for block in body if any(successor not in body for successor in block.successors))


def _match_copy_body(
    significant: list[ir.Instruction],
    /,
    *,
    bound: ir.Value,
    counter_signed: bool,
    induction_variable: str,
    variable_element_sizes: dict[str, int] | None,
) -> ir.RepString | None:
    """Return the ``copy`` :class:`cc.ir.RepString` when *significant* is a bare load+store copy.

    *significant* is the loop body with all scaffolding (IV step,
    ``LoopBoundary``, ``Label``, back-``Jump``, header ``BranchFalse``)
    already dropped — see :func:`_match_string_loop`.  The copy idiom
    ``for (i = 0; i < n; i++) dst[i] = src[i];`` lowers to exactly two
    surviving instructions, in order:

        Index(destination=t, base=S, index=IV)      # t = src[i]
        IndexAssign(base=D, index=IV, source=t)     # dst[i] = t

    where ``t`` is a compiler temp used *only* as the store's source.  We
    require:

    * exactly those two instructions, in that order;
    * both index operands are exactly the IV;
    * the store's source is the load's destination ``t``;
    * ``t`` is read nowhere else (single-use temp) — guaranteed here by
      the body being exactly these two instructions and ``t`` not feeding
      the bound or any other surviving read, plus ``t`` not escaping the
      loop (it is a fresh per-iteration temp).  The two-instruction
      requirement is what proves the load result is consumed only by the
      store;
    * the source and destination element widths are equal and in
      {1, 2, 4}.  A width disagreement (the type map gives different sizes
      for S and D) rejects — a ``rep movs`` moves equal-width units.

    A forward element-wise copy is semantically identical to a forward
    ``rep movs`` even when S and D overlap: both pointers ascend in
    lock-step, so a byte written to D[i] is never re-read as S[j>i] in a
    way that differs from the scalar loop.  Overlap is therefore NOT a
    rejection condition.

    Returns None for any other shape — the safe no-rewrite outcome.
    """
    if len(significant) != 2:
        return None
    load, store = significant
    if not isinstance(load, ir.Index) or not isinstance(store, ir.IndexAssign):
        return None
    if load.index != induction_variable or store.index != induction_variable:
        return None
    temp = load.destination
    if store.source != temp:
        return None
    # ``temp`` must be a single-use temp: defined only by this load and
    # read only by this store.  With the body reduced to exactly these
    # two instructions, the only remaining place ``temp`` could be read is
    # the loop bound — guard against that — and it must not have been
    # written elsewhere in the loop (it has exactly this one definition,
    # since any second writer would have survived as a third significant
    # instruction).
    if bound == temp:
        return None
    source_size = _element_size_of(load.base, variable_element_sizes=variable_element_sizes)
    dest_size = _element_size_of(store.base, variable_element_sizes=variable_element_sizes)
    if source_size is None or dest_size is None:
        # Either base's element width is unknown — leave the loop scalar
        # rather than guess.  Reject-on-unknown keeps a base the type
        # collector misses a missed optimization, never a miscompile.
        return None
    if source_size != dest_size:
        return None
    element_size = dest_size
    if element_size not in (1, 2, 4):
        return None
    return ir.RepString(
        count=bound,
        counter_signed=counter_signed,
        dest=store.base,
        element_size=element_size,
        fill_value=None,
        final_iv=None,
        operation="copy",
        source=load.base,
    )


def _match_fill_body(
    significant: list[ir.Instruction],
    /,
    *,
    bound: ir.Value,
    counter_signed: bool,
    induction_variable: str,
    names_defined_in_loop: set[str],
    variable_element_sizes: dict[str, int] | None,
) -> ir.RepString | None:
    """Return the ``fill`` :class:`cc.ir.RepString` when *significant* is a bare unit-stride store.

    *significant* is the scaffolding-stripped loop body (see
    :func:`_match_string_loop`).  The fill idiom
    ``for (i = 0; i < n; i++) dst[i] = V;`` reduces to exactly one
    surviving instruction:

        IndexAssign(base=D, index=IV, source=V)

    with ``V`` loop-invariant (a literal or a name not written inside the
    loop).  Returns None for anything else — the safe no-rewrite outcome.
    """
    if len(significant) != 1:
        return None
    store = significant[0]
    if not isinstance(store, ir.IndexAssign):
        return None
    if store.index != induction_variable:
        return None
    fill_value = store.source
    if isinstance(fill_value, str) and fill_value in names_defined_in_loop:
        return None
    element_size = _element_size_of(store.base, variable_element_sizes=variable_element_sizes)
    if element_size is None:
        # The base's element width could not be determined from the type
        # map — leave the loop scalar rather than guess a byte store.
        # Reject-on-unknown keeps any base the type collector misses a
        # missed optimization, never a wrong-width miscompile.
        return None
    if element_size not in (1, 2, 4):
        # codegen's _rep_width_suffix only maps 1 / 2 / 4 — an 8-byte
        # (or other) element would KeyError.  Reject, matching the copy
        # matcher's identical guard.
        return None
    return ir.RepString(
        count=bound,
        counter_signed=counter_signed,
        dest=store.base,
        element_size=element_size,
        fill_value=fill_value,
        final_iv=None,
        operation="fill",
        source=None,
    )


def _match_string_loop(
    loop: NaturalLoop,
    /,
    *,
    block_index: dict[BasicBlock, int],
    body: list[ir.Instruction],
    cfg: ControlFlowGraph,
    dominator_sets: dict[BasicBlock, frozenset[BasicBlock]],
    signed_counters: dict[str, bool] | None,
    variable_element_sizes: dict[str, int] | None,
) -> tuple[str, ir.RepString] | None:
    """Return ``(header_label, RepString)`` when *loop* is a recognizable unit-stride fill or copy.

    Shared loop preconditions for both idioms:

    * the header terminator is ``BranchFalse(left=IV, operation="<",
      right=bound)`` — only ``<`` is matched here (``<=`` / ``!=`` are
      deferred);
    * exactly one additive induction variable, named ``IV`` (the
      BranchFalse left operand), with step ``+1``;
    * ``IV`` is initialized to ``0`` by the nearest preceding
      ``Copy(IV, 0)`` in the flat *body* before the header label.

    After those checks pass, the body is stripped of scaffolding (the IV
    step, ``LoopBoundary``, ``Label``, the back-``Jump`` / header
    ``BranchFalse``) and the remaining *significant* instructions are
    handed to the per-idiom matchers:

    * :func:`_match_fill_body` — one ``IndexAssign(base=dst, index=IV,
      source=V)`` → ``RepString(operation="fill", ...)``;
    * :func:`_match_copy_body` — ``Index(destination=t, base=src,
      index=IV)`` then ``IndexAssign(base=dst, index=IV, source=t)`` →
      ``RepString(operation="copy", ...)``.

    Returns None for anything that is not an obvious fill or copy — the
    safe no-rewrite outcome.

    ``element_size`` is looked up in *variable_element_sizes* by base
    name; when the map is absent or lacks the base the size is unknown and
    the matcher rejects (leaves the loop scalar) rather than guessing a
    byte width — a missed optimization, never a miscompile.
    ``counter_signed`` is looked up in
    *signed_counters* by induction-variable name: an entry of ``False``
    (the IV's C type is provably unsigned) drops the ``n <= 0`` guard,
    since an unsigned counter can never be negative and ``n == 0`` makes
    ``rep`` a no-op.  When the map is absent or lacks the IV the matcher
    defaults to ``True`` (the safe choice — an unnecessary guard is
    harmless, a missing one corrupts memory).  ``final_iv`` is left
    ``None`` for both idioms; materializing the induction variable's
    post-loop value is refined in a later task and is sound here as long
    as the IV is dead after the loop.
    """
    terminator = loop.header.terminator
    if not isinstance(terminator, ir.BranchFalse):
        return None
    if terminator.operation != "<":
        return None
    induction_variable = terminator.left
    if not isinstance(induction_variable, str):
        return None
    bound = terminator.right

    body_block_order = sorted(loop.body, key=block_index.__getitem__)
    loop_definition_count = _count_loop_definitions(body_block_order)
    induction_variables = _find_induction_variables(body_block_order, loop_definition_count=loop_definition_count)
    if induction_variables != {induction_variable: 1}:
        return None

    # Item A: the rewrite never materializes the IV, so an IV read
    # outside the loop body (where the scalar loop would have left it at
    # ``bound``) would observe garbage.  CFG-based so it stays sound under
    # goto / irreducible control flow.  Reject conservatively.
    if _iv_live_after_loop(loop, cfg=cfg, induction_variable=induction_variable):
        return None

    # Item E: a leaked IV address lets a callee perturb the counter,
    # breaking the ``count == bound`` linear relationship.  Reject.
    if _iv_address_taken_in_loop(body_block_order, induction_variable=induction_variable):
        return None

    # Item B: prove the ``Copy(IV, 0)`` initializer dominates the loop
    # header (and is not redefined before it) — a textually-nearest
    # init on a non-dominating ``if`` arm must not false-positive.
    if not _value_starts_at_zero(
        body,
        cfg=cfg,
        dominator_sets=dominator_sets,
        header=loop.header,
        header_label=loop.header.label,
        loop_body=loop.body,
        name=induction_variable,
    ):
        return None

    names_defined_in_loop = _names_defined_in_loop(body_block_order=body_block_order)
    significant: list[ir.Instruction] = []
    for block in body_block_order:
        terminators = () if block.terminator is None else (block.terminator,)
        for instruction in (*block.instructions, *terminators):
            if isinstance(instruction, (ir.Label, ir.LoopBoundary, ir.Jump, ir.BranchFalse)):
                continue
            if _self_increment_step(instruction, name=induction_variable) is not None:
                continue
            significant.append(instruction)

    # ``counter_signed`` defaults True (safe).  Only an explicit
    # ``False`` entry — the IV's C type is provably unsigned — drops the
    # ``n <= 0`` guard.
    counter_signed = True if signed_counters is None else signed_counters.get(induction_variable, True)

    fill = _match_fill_body(
        significant,
        bound=bound,
        counter_signed=counter_signed,
        induction_variable=induction_variable,
        names_defined_in_loop=names_defined_in_loop,
        variable_element_sizes=variable_element_sizes,
    )
    if fill is not None:
        return loop.header.label, fill
    copy = _match_copy_body(
        significant,
        bound=bound,
        counter_signed=counter_signed,
        induction_variable=induction_variable,
        variable_element_sizes=variable_element_sizes,
    )
    if copy is not None:
        return loop.header.label, copy
    return None


def _names_defined_in_loop(*, body_block_order: list[BasicBlock]) -> set[str]:
    """Return every name potentially written across the blocks in *body_block_order*.

    Includes explicit instruction destinations and *every* name referenced
    inside an opaque IR escape hatch (:class:`cc.ir.Block`,
    :class:`cc.ir.Switch`, :class:`cc.ir.CarryBranch`).  The escape
    hatches wrap arbitrary AST subtrees the IR doesn't lower fully —
    those subtrees may declare or assign locals the surrounding
    invariance check cannot see otherwise.  Being conservative here
    only costs missed hoisting opportunities; missing a definition
    would silently move a loop-defined local's use to before its def.

    Callers pass the loop's body in a deterministic order so repeated
    runs produce identical results (``frozenset`` iteration would
    otherwise depend on object hashes).
    """
    names: set[str] = set()
    for block in body_block_order:
        terminator = () if block.terminator is None else (block.terminator,)
        for instruction in (*block.instructions, *terminator):
            destination = getattr(instruction, "destination", None)
            if isinstance(destination, str):
                names.add(destination)
            if isinstance(instruction, (ir.Access, ir.Block, ir.CarryBranch, ir.Switch)):
                names.update(_iter_ast_referenced_names(instruction))
    return names


def _operands_invariant(instruction: ir.Instruction, /, *, invariant_destinations: set[str], names_defined_in_loop: set[str]) -> bool:
    """Return True when every operand of *instruction* is loop-invariant.

    An operand is invariant when it is a literal, a name defined outside
    the loop (not in *names_defined_in_loop*), or a name produced by an
    already-marked invariant instruction in this loop
    (*invariant_destinations*).
    """
    for operand in _iter_value_operands(instruction):
        if not isinstance(operand, str):
            continue
        if operand in invariant_destinations:
            continue
        if operand in names_defined_in_loop:
            return False
    return True


def _reduce_strength_in_loop(
    *,
    accumulator_counter: int,
    body_block_order: list[BasicBlock],
    definition_count: dict[str, int],
    preheader: BasicBlock,
) -> int:
    """Apply strength reduction to every IV-times-constant multiply in *body_block_order*.

    Returns the updated *accumulator_counter* so a caller iterating
    multiple loops can keep the synthesized accumulator names globally
    unique across the function.

    For each ``T = IV * k`` candidate, allocates a fresh accumulator
    ``T_acc``, emits ``T_acc = IV * k`` into *preheader* (a single
    multiply that runs once before the loop), rewrites the body's
    multiply to ``Copy(T, T_acc)``, and inserts ``T_acc = T_acc + step*k``
    immediately after the IV's in-loop update.  The transform preserves
    the invariant ``T_acc == IV * k`` at every program point in the loop
    body where the original multiply appeared, so every later read of
    ``T`` observes the same value as before — with one fewer multiply
    per iteration.
    """
    loop_definition_count = _count_loop_definitions(body_block_order)
    induction_variables = _find_induction_variables(body_block_order, loop_definition_count=loop_definition_count)
    if not induction_variables:
        return accumulator_counter
    candidates = _find_multiplicative_iv_uses(body_block_order, definition_count=definition_count, induction_variables=induction_variables)
    if not candidates:
        return accumulator_counter
    for block, _mul_index, mul_instruction, iv_name, mul_constant in candidates:
        step = induction_variables[iv_name]
        accumulator = _ACCUMULATOR_NAME_TEMPLATE.format(counter=accumulator_counter)
        accumulator_counter += 1
        preheader.instructions.append(ir.BinaryOperation(destination=accumulator, left=iv_name, operation="*", right=mul_constant))
        # Locate the multiply by object identity — the captured index
        # from _find_multiplicative_iv_uses is stale if a prior candidate
        # for the same loop inserted an accumulator update earlier in
        # this block.
        current_index = next(index for index, instruction in enumerate(block.instructions) if instruction is mul_instruction)
        block.instructions[current_index] = ir.Copy(destination=mul_instruction.destination, source=accumulator)
        increment = step * mul_constant
        for body_block in body_block_order:
            updated_instructions: list[ir.Instruction] = []
            for instruction in body_block.instructions:
                updated_instructions.append(instruction)
                if _self_increment_step(instruction, name=iv_name) is not None:
                    updated_instructions.append(
                        ir.BinaryOperation(destination=accumulator, left=accumulator, operation="+", right=increment)
                    )
            body_block.instructions = updated_instructions
    return accumulator_counter


def _self_increment_step(instruction: ir.Instruction, /, *, name: str) -> int | None:
    """Return the integer step when *instruction* is ``name = name + literal`` or ``name = name - literal``.

    Recognises three forms:

    * the IR-native ``BinaryOperation`` form;
    * the AST-escape-hatch ``Block(node=Assign(name=name, expr=BinaryOperation(
      left=Var(name), operation='+'/'-', right=Int)))`` form the IR builder
      emits for ``name = name op K``;
    * the AST-escape-hatch ``Block(node=Assign(name=<temp>, expr=
      PlaceIncrementDecrement(place=VariablePlace(name), delta=±1)))`` form the IR builder
      emits for ``name++`` / ``++name`` (and the prefix / postfix variants).
      The ``Assign.name`` is the discarded result temp; the *mutated*
      variable is the ``PlaceIncrementDecrement`` place's name, so the step keys off
      that.  Without this form the matcher would never recognize a real
      ``for (i = 0; i < n; i++)`` loop — the IR builder routes ``i++``
      through this shape, not a bare ``i = i + 1`` BinaryOperation.

    Returns the signed step (negative for ``-`` / a ``-1`` delta) so a
    downward IV like ``i = i - 1`` produces ``-1`` directly — strength
    reduction then knows to decrement its accumulator by ``-step * k``
    rather than adding.

    Returns None for anything else (writes by other names, non-integer
    step, non-self-modify shapes, calls, opaque ``Block`` content).
    """
    if (
        isinstance(instruction, ir.Block)
        and isinstance(instruction.node, ast_nodes.Assign)
        and isinstance(instruction.node.expr, ast_nodes.PlaceIncrementDecrement)
        and isinstance(instruction.node.expr.place, ast_nodes.VariablePlace)
        and instruction.node.expr.place.name == name
    ):
        return instruction.node.expr.delta
    if (
        isinstance(instruction, ir.BinaryOperation)
        and instruction.destination == name
        and instruction.left == name
        and instruction.operation in ("+", "-")
        and isinstance(instruction.right, int)
    ):
        return instruction.right if instruction.operation == "+" else -instruction.right
    if (
        isinstance(instruction, ir.Block)
        and isinstance(instruction.node, ast_nodes.Assign)
        and instruction.node.name == name
        and isinstance(instruction.node.expr, ast_nodes.BinaryOperation)
        and instruction.node.expr.operation in ("+", "-")
        and isinstance(instruction.node.expr.left, ast_nodes.Var)
        and instruction.node.expr.left.name == name
        and isinstance(instruction.node.expr.right, ast_nodes.Int)
    ):
        value = instruction.node.expr.right.value
        return value if instruction.node.expr.operation == "+" else -value
    return None


def _value_starts_at_zero(
    body: list[ir.Instruction],
    /,
    *,
    cfg: ControlFlowGraph,
    dominator_sets: dict[BasicBlock, frozenset[BasicBlock]],
    header: BasicBlock,
    header_label: str,
    loop_body: frozenset[BasicBlock],
    name: str,
) -> bool:
    """Return True when *name* provably holds ``0`` on entry to *header*.

    Two conditions, both required (and both conservatively fail closed):

    * **Textual proximity** — the nearest write to *name* preceding the
      header :class:`cc.ir.Label` in the flat *body* is a
      ``Copy(name, 0)``.  Anything else (a non-zero source, a different
      instruction shape, no preceding write) yields False.

    * **Dominance** — *name* is written *exactly once* outside the loop
      body, and the block holding that single write dominates *header*.
      This is what stops a ``Copy(name, 0)`` sitting inside a
      non-dominating ``if`` arm (with the loop after the join) from
      false-positiving: such a Copy does not dominate the header, so on
      the not-taken path the IV reaches the header with an arbitrary
      value.  Requiring a *single* outside-loop definition also rejects
      the case where a second, non-dominating write to the IV exists on
      a path between the init and the header.

    Used by :func:`_match_string_loop` to prove the fill / copy induction
    variable starts at zero (so ``count == bound`` for a ``<`` test).
    """
    header_index = next(
        (index for index, instruction in enumerate(body) if isinstance(instruction, ir.Label) and instruction.name == header_label),
        None,
    )
    if header_index is None:
        return False
    nearest_is_zero_copy = False
    for instruction in reversed(body[:header_index]):
        if _assignment_destination(instruction) != name:
            continue
        nearest_is_zero_copy = isinstance(instruction, ir.Copy) and instruction.source == 0
        break
    if not nearest_is_zero_copy:
        return False
    if header not in dominator_sets:
        return False
    # Collect every block *outside the loop body* that writes ``name``.
    # In-loop writes are the IV's own increment (expected).  We require
    # exactly one outside-loop definition, that it is a zero Copy, and
    # that its block dominates the header — a Copy on a non-dominating
    # ``if`` arm fails the dominance test, a second non-dominating write
    # fails the single-definition count.
    outside_defining_blocks: list[BasicBlock] = []
    init_is_zero_copy = True
    for block in cfg.blocks:
        if block in loop_body:
            continue
        wrote = False
        terminator = () if block.terminator is None else (block.terminator,)
        for instruction in (*block.instructions, *terminator):
            if _assignment_destination(instruction) == name:
                wrote = True
                if not (isinstance(instruction, ir.Copy) and instruction.source == 0):
                    init_is_zero_copy = False
        if wrote:
            outside_defining_blocks.append(block)
    if len(outside_defining_blocks) != 1 or not init_is_zero_copy:
        return False
    return outside_defining_blocks[0] in dominator_sets[header]


def hoist_loop_invariants(body: list[ir.Instruction], /, *, excluded_names: frozenset[str] = frozenset()) -> list[ir.Instruction]:
    """Hoist loop-invariant instructions to preheaders inserted ahead of each loop.

    Pipeline: build the CFG, discover natural loops, insert preheaders,
    then mark and move every invariant instruction whose destination has
    a single definition in the whole function.  Returns the rewritten
    flat IR.  Returns *body* unchanged when the function contains no
    loops, when no loop has a non-empty entry-side predecessor set
    (e.g. only unreachable infinite loops), or when no instructions
    qualify as invariant.

    *excluded_names* names program globals (and other names the
    optimizer treats as call-clobbered).  When any loop body contains
    a function call, every name in *excluded_names* is conservatively
    treated as defined inside that loop — a callee may have written to
    the underlying slot, so reads of a global across a call cannot be
    hoisted out.  Matches the convention :func:`cc.ssa.optimize_ssa`
    uses for the same set.

    Empty preheaders left behind by a loop where no instruction
    hoisted are not removed here — the next pass of the optimizer's
    scalar pipeline collapses them via the existing Jump-only
    block simplification.
    """
    if any(isinstance(instruction, ir.InlineAsm) for instruction in body):
        # InlineAsm can read or write any local; defs-in-loop analysis
        # cannot see through it.  Matches the SSA pipeline's bypass.
        return body
    cfg = build_cfg(body)
    loops_in_function = natural_loops(cfg)
    if not loops_in_function:
        return body
    preheaders = insert_preheaders(cfg, loops=loops_in_function)
    if not preheaders:
        return body
    definition_count = _count_destination_definitions(cfg)
    block_index = {block: index for index, block in enumerate(cfg.blocks)}
    dominator_sets = _dominator_sets(compute_dominators(cfg))
    hoisted_any = False
    for loop, preheader in preheaders.items():
        body_block_order = sorted(loop.body, key=block_index.__getitem__)
        if _hoist_invariants_into_preheader(
            body_block_order=body_block_order,
            definition_count=definition_count,
            dominator_sets=dominator_sets,
            excluded_names=excluded_names,
            loop_exits=loop.exits,
            preheader=preheader,
        ):
            hoisted_any = True
    if not hoisted_any:
        return body
    return flatten_cfg(cfg)


def insert_preheaders(cfg: ControlFlowGraph, /, *, loops: list[NaturalLoop]) -> dict[NaturalLoop, BasicBlock]:
    """Insert a preheader block ahead of every loop header in *loops*.

    For each loop, the preheader becomes the unique non-latch predecessor
    of the header.  Non-latch predecessors that reached the header by an
    explicit ``Jump`` / ``BranchFalse`` / ``CarryBranch`` target are
    retargeted to the preheader's label; a predecessor that reached the
    header by fall-through is automatically rerouted because the
    preheader is inserted immediately before the header in source order.
    A loop whose header has *no* non-latch predecessors (e.g., an
    infinite loop with no external entry — usually unreachable code) is
    skipped: there is nothing to merge into a preheader.

    Mutates *cfg* in place (matches the :func:`cc.ssa._split_critical_edges`
    convention) and returns ``{loop: preheader}`` for every loop that
    received a preheader.  The :class:`NaturalLoop` records remain valid
    afterward because the loop body and latches are unchanged; only the
    header's predecessor list shifts.  Callers that need updated loop
    info (e.g., to expose the preheader as part of a wider region)
    should re-run :func:`natural_loops` on the rewritten CFG.
    """
    preheaders: dict[NaturalLoop, BasicBlock] = {}
    counter = 0
    for loop in loops:
        non_latch_predecessors = [predecessor for predecessor in loop.header.predecessors if predecessor not in loop.latches]
        if not non_latch_predecessors:
            continue
        # Materialize fall-through-to-header on the positionally previous
        # block so inserting the preheader between them doesn't silently
        # route a latch's back-edge through the preheader (which would
        # cause hoisted invariants to execute every iteration).  Safe to
        # apply unconditionally — non-latch predecessors get their fresh
        # Jump retargeted to the preheader label in the loop below.
        header_index = cfg.blocks.index(loop.header)
        if header_index > 0:
            previous = cfg.blocks[header_index - 1]
            if previous.terminator is None and loop.header in previous.successors:
                previous.terminator = ir.Jump(target=loop.header.label)
        label = _PREHEADER_LABEL_TEMPLATE.format(counter=counter)
        counter += 1
        preheader = BasicBlock(label=label, terminator=ir.Jump(target=loop.header.label))
        preheader.predecessors.extend(non_latch_predecessors)
        preheader.successors.append(loop.header)
        for predecessor in non_latch_predecessors:
            successor_index = predecessor.successors.index(loop.header)
            predecessor.successors[successor_index] = preheader
            terminator = predecessor.terminator
            if isinstance(terminator, (ir.BranchFalse, ir.CarryBranch, ir.Jump)) and terminator.target == loop.header.label:
                predecessor.terminator = dataclasses.replace(terminator, target=label)
        loop.header.predecessors[:] = [preheader, *(predecessor for predecessor in loop.header.predecessors if predecessor in loop.latches)]
        cfg.blocks.insert(cfg.blocks.index(loop.header), preheader)
        cfg.label_to_block[label] = preheader
        preheaders[loop] = preheader
    return preheaders


def natural_loops(cfg: ControlFlowGraph, /) -> list[NaturalLoop]:
    """Discover every natural loop in *cfg* and return them in deterministic order.

    Returns one :class:`NaturalLoop` per loop header, with multiple
    latches coalesced into a single loop.  Loops are ordered by the
    header's position in ``cfg.blocks`` so downstream passes get stable
    output across runs.

    Irreducible regions contribute no loops (see module docstring).
    """
    idom = compute_dominators(cfg)
    dominators_of = _dominator_sets(idom)
    latches_by_header: dict[BasicBlock, set[BasicBlock]] = {}
    for latch, header in _back_edges(cfg, dominators_of=dominators_of):
        latches_by_header.setdefault(header, set()).add(latch)
    block_index = {block: index for index, block in enumerate(cfg.blocks)}
    loops: list[NaturalLoop] = []
    for header in sorted(latches_by_header, key=lambda block: block_index[block]):
        latches = frozenset(latches_by_header[header])
        body = _loop_body(header, latches=latches)
        loops.append(NaturalLoop(body=body, exits=_loop_exits(body), header=header, latches=latches))
    return loops


def recognize_string_loops(
    body: list[ir.Instruction],
    /,
    *,
    signed_counters: dict[str, bool] | None = None,
    variable_element_sizes: dict[str, int] | None = None,
) -> list[ir.Instruction]:
    """Rewrite unit-stride fill loops into :class:`cc.ir.RepString` ``fill`` nodes.

    Pipeline mirrors :func:`reduce_loop_strength`: build the CFG, discover
    natural loops, match each loop against the fill idiom, and splice a
    ``RepString`` in place of every match.  Returns *body* unchanged when
    no loop is recognizable (no loops, or no loop matches the narrow
    canonical ``for (i = 0; i < n; i++) dst[i] = V;`` shape this task
    handles).

    *variable_element_sizes* maps a destination base name to its array /
    pointer element width (1, 2, or 4 bytes).  When the map is absent or
    is missing the matched base, the base's element width is *unknown* and
    the loop is left scalar (no rewrite) rather than defaulting to a byte
    store — byte-defaulting on an unknown base miscompiled file-scope
    arrays whose real element width was wider.  This parameter is the seam
    the optimizer wiring threads real type information through.

    *signed_counters* maps an induction-variable name to whether its C
    type is signed.  An entry of ``False`` (a provably unsigned counter)
    lets the matched :class:`cc.ir.RepString` drop the ``n <= 0`` guard;
    a missing name or an absent map keeps the safe ``counter_signed=True``
    default.  Like *variable_element_sizes*, this is the seam the
    optimizer wiring threads real type information through.

    Copy recognition and the full rejection hardening (``<=`` / ``!=``
    bounds, non-zero starts via ``final_iv`` materialization, aliasing of
    the fill destination, etc.) are later tasks; this pass is deliberately
    narrow and returns *body* unchanged for anything it does not recognize
    as an obvious fill.

    Functions containing :class:`cc.ir.InlineAsm` are skipped — the asm
    text may reference loop locals the analysis can't see.  Matches the
    bypass convention in :func:`reduce_loop_strength`.
    """
    if any(isinstance(instruction, ir.InlineAsm) for instruction in body):
        return body
    cfg = build_cfg(body)
    loops_in_function = natural_loops(cfg)
    if not loops_in_function:
        return body
    block_index = {block: index for index, block in enumerate(cfg.blocks)}
    dominator_sets = _dominator_sets(compute_dominators(cfg))
    rewrites: dict[NaturalLoop, tuple[str, ir.RepString]] = {}
    for loop in loops_in_function:
        match = _match_string_loop(
            loop,
            block_index=block_index,
            body=body,
            cfg=cfg,
            dominator_sets=dominator_sets,
            signed_counters=signed_counters,
            variable_element_sizes=variable_element_sizes,
        )
        if match is not None:
            rewrites[loop] = match
    if not rewrites:
        return body
    return _apply_string_rewrites(body, rewrites=rewrites)


def reduce_loop_strength(body: list[ir.Instruction], /) -> list[ir.Instruction]:
    """Replace IV-times-constant multiplies in every natural loop with additive accumulators.

    Pipeline mirrors :func:`hoist_loop_invariants`: build the CFG,
    discover natural loops, insert preheaders, then transform each loop
    independently.  Returns the rewritten flat IR.  Returns *body*
    unchanged when no candidate reductions exist (no loops, no
    preheaders, no IVs, or no IV-times-constant multiplies).

    A *candidate* is a multiply ``T = IV * k`` (or ``T = k * IV``)
    inside a natural loop body where ``IV`` is a single-def additive
    induction variable (``IV = IV + literal``) and ``T`` has exactly
    one definition in the entire function — so rewriting the multiply
    site cannot change the value seen at any other write of ``T``.
    Each candidate gets its own accumulator: the preheader initializes
    ``T_acc = IV * k`` (a single multiply, run once before the loop),
    the multiply becomes ``Copy(T, T_acc)``, and ``T_acc`` increments
    by ``step * k`` after every ``IV`` update so the invariant
    ``T_acc == IV * k`` holds at every program point in the body.

    Functions containing :class:`cc.ir.InlineAsm` are skipped — the asm
    text may reference loop locals in ways the analysis can't see.
    Matches the bypass convention in :func:`hoist_loop_invariants`.
    """
    if any(isinstance(instruction, ir.InlineAsm) for instruction in body):
        return body
    cfg = build_cfg(body)
    loops_in_function = natural_loops(cfg)
    if not loops_in_function:
        return body
    preheaders = insert_preheaders(cfg, loops=loops_in_function)
    if not preheaders:
        return body
    definition_count = _count_destination_definitions(cfg)
    block_index = {block: index for index, block in enumerate(cfg.blocks)}
    accumulator_counter = 0
    for loop, preheader in preheaders.items():
        body_block_order = sorted(loop.body, key=block_index.__getitem__)
        accumulator_counter = _reduce_strength_in_loop(
            accumulator_counter=accumulator_counter,
            body_block_order=body_block_order,
            definition_count=definition_count,
            preheader=preheader,
        )
    if accumulator_counter == 0:
        return body
    return flatten_cfg(cfg)


def string_loop_type_maps(
    function: ast_nodes.Function, /, *, program_globals: list[ast_nodes.Node] | None = None, target: X86CodegenTarget
) -> tuple[dict[str, int], dict[str, bool]]:
    """Return ``(variable_element_sizes, signed_counters)`` for *function*.

    *variable_element_sizes* maps every array / pointer base name to
    ``sizeof(element)`` so :func:`recognize_string_loops` can pick the
    ``rep`` width (``stosb`` / ``stosw`` / ``stosd``).  Three scopes
    contribute, in increasing precedence: file-scope (*globals*)
    declarations, then this *function*'s params, then its locals
    (including those declared inside nested blocks).  Globals must be
    threaded in — a loop whose array / pointer base is a file-scope array
    has no params/locals entry, and without the global the base's width
    would be unknown and (now) rejected, regressing a global ``int g[N]``
    fill from ``rep stosd`` to no rewrite.  Including globals lets the
    rewrite agree with the scalar index path's element-size choice
    (``CodeGeneratorBase._index_pointee_size``).

    *signed_counters* maps every integer-scalar name to whether its C type
    is signed.  A name is *unsigned* (``False``) when its declared type
    begins with ``unsigned``; everything else (``int``, ``short``, ``char``
    in this codebase's conventions) is treated as signed (``True``).
    Pointer / array names are omitted — they are never loop counters.
    The matcher only consults this for the induction variable, and only an
    explicit ``False`` drops the guard, so an omitted name stays safe.

    The two maps are the seam the optimizer threads into
    :func:`recognize_string_loops`: element sizes via
    *variable_element_sizes*, counter signedness via *signed_counters*.
    """
    array_element_types: dict[str, str] = {}
    scalar_types: dict[str, str] = {}
    array_param_names: set[str] = set()
    # File-scope declarations first (lowest precedence): a global
    # ``int g[N]`` / ``unsigned short *p`` contributes its element width
    # so a loop over the global base is sized correctly.  Params and
    # locals collected below overwrite on name collision — an inner
    # declaration shadows the global, matching C scoping.
    if program_globals is not None:
        _collect_array_and_scalar_types(program_globals, array_element_types=array_element_types, scalar_types=scalar_types)
    for parameter in function.params:
        if parameter.is_array:
            # Array param: ``parameter.type`` is the *element* type
            # (``parse_type`` consumed the base, the ``[]`` set is_array),
            # matching the ArrayDecl ``type_name`` convention.
            array_element_types[parameter.name] = parameter.type
            array_param_names.add(parameter.name)
        else:
            scalar_types[parameter.name] = parameter.type
    _collect_array_and_scalar_types(function.body, array_element_types=array_element_types, scalar_types=scalar_types)

    variable_element_sizes: dict[str, int] = {}
    for name, element_type in array_element_types.items():
        size = _element_size_for_type(element_type=element_type, is_array=True, pointer_type=None, target=target)
        if size is not None:
            variable_element_sizes[name] = size
    for name, scalar_type in scalar_types.items():
        if name in array_element_types:
            continue
        size = _element_size_for_type(element_type=None, is_array=False, pointer_type=scalar_type, target=target)
        if size is not None:
            variable_element_sizes[name] = size

    signed_counters: dict[str, bool] = {}
    for name, scalar_type in scalar_types.items():
        # Pointer scalars are never loop counters; skip so the map only
        # ever describes integer induction variables.
        if scalar_type.endswith("*") or "*" in scalar_type:
            continue
        signed_counters[name] = not scalar_type.startswith("unsigned")
    return variable_element_sizes, signed_counters
