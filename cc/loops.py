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
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cc import ir
from cc.cfg import BasicBlock, ControlFlowGraph, build_cfg, compute_dominators, flatten_cfg

if TYPE_CHECKING:
    from collections.abc import Iterator

#: Synthetic-label template used for preheader blocks inserted by
#: :func:`insert_preheaders`.  The leading ``.`` matches the IR-level
#: convention for branch targets so backend label resolution treats the
#: preheader like any other compiler-generated label.
_PREHEADER_LABEL_TEMPLATE = ".licm_preheader_{counter}"


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


def _hoist_invariants_into_preheader(
    *,
    body_block_order: list[BasicBlock],
    definition_count: dict[str, int],
    excluded_names: frozenset[str],
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

    Returns True when any instruction was hoisted.
    """
    names_defined_in_loop = _names_defined_in_loop(body_block_order=body_block_order)
    if any(
        isinstance(instruction, (ir.Call, ir.CarryBranch, ir.TailCall)) for block in body_block_order for instruction in block.instructions
    ) or any(isinstance(block.terminator, (ir.CarryBranch, ir.TailCall)) for block in body_block_order):
        names_defined_in_loop |= excluded_names
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
    """Return True if *instruction* is a pure, speculatable kind safe to hoist.

    The first cut admits only :class:`cc.ir.BinaryOperation` — arithmetic
    and bitwise operations have no observable side effects and cannot
    fault.  :class:`cc.ir.Copy` is intentionally excluded because copy
    propagation already removes most loop-local copies; hoisting them
    would only churn the IR without enabling further wins.
    :class:`cc.ir.Index` (memory load) is also excluded for now: a
    speculatively-hoisted load could fault on a pointer that the loop
    pre-condition would have rejected.  A future pass can lift loads
    by adding a "dominates every loop exit" check.
    """
    return isinstance(instruction, ir.BinaryOperation)


def _iter_ast_referenced_names(node: object, /) -> Iterator[str]:
    """Yield every string-valued dataclass field anywhere in the AST subtree at *node*.

    A superset of :func:`cc.ssa._iter_ast_var_names`: that walker only
    yields :class:`cc.ast_nodes.Var` reads, but the LICM invariance
    check also needs to see declarations and assignments that write to
    loop-local names through fields that vary by AST node type —
    ``VarDecl.name``, ``ArrayDecl.name``, ``Assign.name``,
    :class:`cc.ast_nodes.IncrementDecrement` ``target_name``, etc.

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
            if isinstance(instruction, (ir.Block, ir.CarryBranch, ir.Switch)):
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
    hoisted_any = False
    for loop, preheader in preheaders.items():
        body_block_order = sorted(loop.body, key=block_index.__getitem__)
        if _hoist_invariants_into_preheader(
            body_block_order=body_block_order, definition_count=definition_count, excluded_names=excluded_names, preheader=preheader
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
