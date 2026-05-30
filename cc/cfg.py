"""Basic-block control-flow graph + dominator tree over the flat IR.

The IR produced by :class:`cc.ir.Builder` is a flat list of instructions per
function with labels and explicit branches.  Most non-trivial optimizations
(SSA construction, real CSE, LICM, GVN, mem2reg) want a richer view: blocks of
straight-line code, explicit edges between them, and a dominator tree.  This
module produces that view *non-destructively* from the existing flat IR — the
original :class:`cc.ir.Function.body` is the source of truth; CFG objects are
analysis-only and never mutated by codegen.

A :class:`BasicBlock` is a maximal run of straight-line IR with at most one
entry point (its leading :class:`cc.ir.Label`, or the function entry) and at
most one terminator (:class:`cc.ir.Jump`, :class:`cc.ir.BranchFalse`,
:class:`cc.ir.CarryBranch`, :class:`cc.ir.Return`, :class:`cc.ir.TailCall`).
Mid-block instructions never transfer control to a label.  Two opaque cases
are treated as straight-line for now: :class:`cc.ir.Switch` (multi-way
dispatch over nested case bodies — the outer CFG sees a single fall-through
edge to whatever follows the Switch) and :class:`cc.ir.Block` /
:class:`cc.ir.InlineAsm` (AST escape hatches whose internal control flow is
invisible at the IR layer).  This keeps Phase 1 simple; Phase 2 SSA
construction will revisit Switch when mem2reg needs to see across arms.

:class:`cc.ir.LoopBoundary` is emission metadata (push/pop loop label
context for the codegen) and is preserved verbatim inside the BB's
``instructions`` list.  It has no control-flow effect.

Dominators are computed with the Cooper-Harvey-Kennedy iterative algorithm
("A Simple, Fast Dominance Algorithm", 2001) — simpler than
Lengauer-Tarjan and fast enough for the small CFGs OS programs produce.
Dominance frontiers come from the same paper.  Both are needed by Phase 2
SSA construction (phi-node placement via dominance frontiers).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cc import ir

#: Label prefix used for synthetic BB names (function entry and
#: fall-through blocks after unlabeled terminators).  IR-level labels
#: always start with ``.``, so a leading ``<`` is impossible to confuse
#: with any real branch target.
_SYNTHETIC_LABEL_PREFIX = "<"

_TERMINATOR_TYPES = (ir.BranchFalse, ir.CarryBranch, ir.Jump, ir.Return, ir.TailCall)


def _build_blocks(body: list[ir.Instruction], /, *, bb_starts: list[tuple[int, str]]) -> list[BasicBlock]:
    """Slice *body* into :class:`BasicBlock` instances using *bb_starts* as boundaries."""
    blocks: list[BasicBlock] = []
    for index, (start, label) in enumerate(bb_starts):
        end = bb_starts[index + 1][0] if index + 1 < len(bb_starts) else len(body)
        block_slice = body[start:end]
        # The leading Label (if any) names the BB; strip it from the instruction list.
        if block_slice and isinstance(block_slice[0], ir.Label):
            block_slice = block_slice[1:]
        terminator: ir.Instruction | None = None
        if block_slice and isinstance(block_slice[-1], _TERMINATOR_TYPES):
            terminator = block_slice[-1]
            instructions = list(block_slice[:-1])
        else:
            instructions = list(block_slice)
        blocks.append(BasicBlock(instructions=instructions, label=label, terminator=terminator))
    return blocks


def _identify_block_starts(body: list[ir.Instruction]) -> list[tuple[int, str]]:
    """Return ``[(index, label), ...]`` for every BB boundary in *body*, in order.

    The function entry is always boundary 0.  Every :class:`cc.ir.Label`
    introduces a boundary at its index, using the label name.  Every
    instruction immediately following a terminator becomes a boundary
    with a synthetic ``<fallthrough_N>`` label, unless a real Label
    already lives at that index.
    """
    # Entry block.  If the function starts with a Label, that label names
    # the entry BB; otherwise synthesize an ``<entry>`` label.
    if body and isinstance(body[0], ir.Label):
        bb_starts = [(0, body[0].name)]
    else:
        bb_starts = [(0, f"{_SYNTHETIC_LABEL_PREFIX}entry>")]
    for index, instruction in enumerate(body):
        if index == 0:
            continue
        if isinstance(instruction, ir.Label):
            if bb_starts[-1][0] != index:
                bb_starts.append((index, instruction.name))
            continue
        previous = body[index - 1]
        if isinstance(previous, _TERMINATOR_TYPES) and bb_starts[-1][0] != index:
            synthetic = f"{_SYNTHETIC_LABEL_PREFIX}fallthrough_{index}>"
            bb_starts.append((index, synthetic))
    return bb_starts


def _intersect_dominators(
    first: BasicBlock,
    second: BasicBlock,
    /,
    *,
    idom: dict[BasicBlock, BasicBlock],
    postorder_index: dict[BasicBlock, int],
) -> BasicBlock:
    """Return the deepest common ancestor of *first* and *second* in the dom tree.

    Walks each pointer up the (partial) dominator tree, repeatedly
    promoting the one with the lower postorder number until they meet.
    Postorder numbering ensures that walking "up" the tree always
    increases the index (because the entry block has the highest
    postorder number).
    """
    finger_a = first
    finger_b = second
    while finger_a is not finger_b:
        while postorder_index[finger_a] < postorder_index[finger_b]:
            finger_a = idom[finger_a]
        while postorder_index[finger_b] < postorder_index[finger_a]:
            finger_b = idom[finger_b]
    return finger_a


def _postorder_index(postorder: list[BasicBlock]) -> dict[BasicBlock, int]:
    """Return ``{block: index_in_postorder}``.

    Postorder visits a block after all its descendants in the DFS tree,
    so the entry block ends up with the highest index.  Used by
    :func:`_intersect_dominators` to compare "depth" without an
    explicit dominator-tree walk.
    """
    # Reverse-postorder iteration in compute_dominators visits the entry first,
    # so flip the order here to get true postorder (entry last → highest index).
    return {block: index for index, block in enumerate(reversed(postorder))}


def _reverse_postorder(cfg: ControlFlowGraph) -> list[BasicBlock]:
    """Return blocks in reverse postorder from the entry — the order CHK needs.

    Iterative DFS to avoid recursion limits on long linear bodies.
    Unreachable blocks (no path from entry) are not included.
    """
    visited: set[BasicBlock] = set()
    postorder: list[BasicBlock] = []
    # Stack entries: (block, iterator over yet-unvisited successors)
    stack: list[tuple[BasicBlock, list[BasicBlock]]] = [(cfg.entry, list(cfg.entry.successors))]
    visited.add(cfg.entry)
    while stack:
        block, remaining = stack[-1]
        if remaining:
            successor = remaining.pop(0)
            if successor in visited:
                continue
            visited.add(successor)
            stack.append((successor, list(successor.successors)))
        else:
            postorder.append(block)
            stack.pop()
    postorder.reverse()
    return postorder


def _wire_predecessors(blocks: list[BasicBlock]) -> None:
    """Populate ``predecessors`` on each block by reversing ``successors`` edges."""
    for block in blocks:
        for successor in block.successors:
            successor.predecessors.append(block)


def _wire_successors(blocks: list[BasicBlock], /, *, label_to_block: dict[str, BasicBlock]) -> None:
    """Populate ``successors`` on each block based on its terminator + source order.

    Conditional branches (:class:`cc.ir.BranchFalse`,
    :class:`cc.ir.CarryBranch`) have two successors: the explicit target
    and the fall-through (next block in source order).  Unconditional
    jumps have one.  Returns / tail-calls have none.  Blocks with no
    terminator fall through to the next block in source order.
    """
    for index, block in enumerate(blocks):
        fall_through = blocks[index + 1] if index + 1 < len(blocks) else None
        terminator = block.terminator
        if terminator is None:
            if fall_through is not None:
                block.successors.append(fall_through)
            continue
        if isinstance(terminator, ir.Jump):
            block.successors.append(label_to_block[terminator.target])
        elif isinstance(terminator, (ir.BranchFalse, ir.CarryBranch)):
            block.successors.append(label_to_block[terminator.target])
            if fall_through is not None:
                block.successors.append(fall_through)
        elif isinstance(terminator, (ir.Return, ir.TailCall)):
            pass  # No successors — function exits here.


@dataclass(eq=False, kw_only=True, slots=True)
class BasicBlock:
    """One maximal straight-line block in a function's CFG.

    ``label`` is the IR :class:`cc.ir.Label` name that identifies this
    block's entry point, or a synthetic ``<entry>`` / ``<fallthrough_N>``
    name when no IR label exists (function entry or the block immediately
    following an unlabeled terminator — the latter only happens for
    dead code the IR optimizer hasn't pruned yet).

    ``instructions`` contains every non-terminator, non-leading-Label
    instruction in source order, including :class:`cc.ir.LoopBoundary`
    metadata.  ``terminator`` is the trailing control-transfer
    instruction (``None`` only for the last block when the function
    ends without an explicit ``Return`` — control-flow analysis treats
    it as if it falls through to nothing).

    ``successors`` / ``predecessors`` are mutable lists populated by
    :func:`build_cfg`.  They reference other :class:`BasicBlock`
    instances by identity (not label), so passes that rewire edges in
    later phases can mutate them without re-resolving labels.
    """

    instructions: list[ir.Instruction] = field(default_factory=list)
    label: str
    predecessors: list[BasicBlock] = field(default_factory=list)
    successors: list[BasicBlock] = field(default_factory=list)
    terminator: ir.Instruction | None = None


@dataclass(eq=False, kw_only=True, slots=True)
class ControlFlowGraph:
    """All :class:`BasicBlock` instances for one IR function, plus the entry block.

    ``blocks`` is in source order (the order they appear in the original
    flat IR) so that fall-through edges from ``BranchFalse`` /
    ``CarryBranch`` line up with the next block in the list.  Optimizer
    passes that reorder blocks should rebuild the CFG rather than try to
    keep this invariant.

    ``label_to_block`` maps every real IR label and every synthetic BB
    label to its block, so branch targets resolve in O(1).
    """

    blocks: list[BasicBlock]
    entry: BasicBlock
    label_to_block: dict[str, BasicBlock]


def build_cfg(body: list[ir.Instruction]) -> ControlFlowGraph:
    """Split *body* into basic blocks and wire successor / predecessor edges.

    ``body`` is the flat IR for one :class:`cc.ir.Function`.  A new BB
    starts at the function entry, at every :class:`cc.ir.Label`, and at
    every instruction immediately following a terminator (so dead code
    after a ``Jump`` or ``Return`` still gets its own block — the
    IR-level optimizer should already have removed it, but the CFG
    builder doesn't assume that).
    """
    bb_starts = _identify_block_starts(body)
    blocks = _build_blocks(body, bb_starts=bb_starts)
    label_to_block = {bb.label: bb for bb in blocks}
    _wire_successors(blocks, label_to_block=label_to_block)
    _wire_predecessors(blocks)
    return ControlFlowGraph(blocks=blocks, entry=blocks[0], label_to_block=label_to_block)


def compute_dominance_frontiers(idom: dict[BasicBlock, BasicBlock], /) -> dict[BasicBlock, set[BasicBlock]]:
    """Return ``{block: dominance_frontier_set}`` for every block in *idom*.

    The dominance frontier of a block ``B`` is the set of blocks ``F``
    such that ``B`` dominates a predecessor of ``F`` but does not strictly
    dominate ``F`` itself.  Phase 2 SSA construction inserts phi nodes
    at every block in the dominance frontier of a definition.
    """
    frontiers: dict[BasicBlock, set[BasicBlock]] = {block: set() for block in idom}
    for block, block_idom in idom.items():
        if len(block.predecessors) < 2:
            continue
        for predecessor in block.predecessors:
            if predecessor not in idom:
                continue
            runner = predecessor
            while runner is not block_idom:
                frontiers[runner].add(block)
                next_idom = idom.get(runner)
                if next_idom is None or next_idom is runner:
                    break
                runner = next_idom
    return frontiers


def compute_dominators(cfg: ControlFlowGraph) -> dict[BasicBlock, BasicBlock]:
    """Return ``{block: immediate_dominator}`` for every reachable block.

    Uses the Cooper-Harvey-Kennedy iterative algorithm.  The entry
    block is its own immediate dominator (sentinel — no predecessor
    actually dominates the entry).  Unreachable blocks are omitted
    from the result so passes that iterate the dominator tree don't
    accidentally process dead regions.
    """
    postorder = _reverse_postorder(cfg)
    reachable = set(postorder)
    idom: dict[BasicBlock, BasicBlock] = {cfg.entry: cfg.entry}
    changed = True
    while changed:
        changed = False
        # Walk in reverse-postorder, skipping the entry block.
        for block in postorder:
            if block is cfg.entry:
                continue
            processed_preds = [p for p in block.predecessors if p in idom]
            if not processed_preds:
                continue
            new_idom = processed_preds[0]
            for predecessor in processed_preds[1:]:
                new_idom = _intersect_dominators(predecessor, new_idom, idom=idom, postorder_index=_postorder_index(postorder))
            if idom.get(block) is not new_idom:
                idom[block] = new_idom
                changed = True
    # Sanity-prune: any block we never assigned must be unreachable.
    return {block: dominator for block, dominator in idom.items() if block in reachable}
