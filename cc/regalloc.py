"""Unified graph-coloring register allocator over the flat IR + CFG.

PR 1 (this module) is the *unwired engine*: pure liveness/interference and a
cost-aware Chaitin-Briggs colorer.  Nothing in cc/codegen imports it yet, so its
introduction is byte-neutral.  Target-specific inputs — the register pool,
byte-alias / 16-bit-index legality, regparm precolors, and the call-clobber cost
economics — are passed in by the caller (see RegisterConstraints / CostModel);
PR 2/3 compute them from the generator's clobber data and wire emission to the
result.

Public API:

    inter = build_interference(allocatable=frozenset({"x", "_ir_0"}), function=function)
    alloc = color(constraints=constraints, costs=costs, interference=inter.graph, moves=inter.moves)
    # or, end to end:
    alloc = allocate(allocatable=..., constraints=..., costs=..., function=function)

``alloc.homes`` maps value -> register; ``alloc.spilled`` is the set of values
left in memory.
"""

from __future__ import annotations

import dataclasses
from collections import Counter
from typing import TYPE_CHECKING

from cc import ast_nodes, cfg, ir

if TYPE_CHECKING:
    from collections.abc import Iterator


_CALL_TYPES = (ir.Call, ir.TailCall, ir.CarryBranch)


#: Instruction classes that define a destination name.
_DESTINATION_TYPES = (ir.BinaryOperation, ir.Copy, ir.Index, ir.Call)

#: Instruction classes that carry no reads and no allocatable defs.
_INERT_TYPES = (ir.Jump, ir.Label, ir.LoopBoundary, ir.InlineAsm)

#: Instruction classes whose reads are fully covered by VALUE_FIELDS + the
#: explicit name-string fields handled in ``_instruction_uses``.
_MODELED_VALUE_TYPES = (
    ir.BinaryOperation,
    ir.BranchFalse,
    ir.Call,
    ir.Copy,
    ir.Index,
    ir.IndexAssign,
    ir.RepString,
    ir.Return,
    ir.TailCall,
)

#: Opaque AST-wrapping instructions whose reads are found by walking the AST.
_OPAQUE_TYPES = (ir.Access, ir.Block, ir.CarryBranch, ir.Switch)


def _allowed_registers(*, constraints: RegisterConstraints, value: str) -> tuple[str, ...]:
    """Return the pool registers *value* may occupy (full pool if unconstrained)."""
    permitted = constraints.allowed.get(value)
    if permitted is None:
        return constraints.pool
    return tuple(register for register in constraints.pool if register in permitted)


def _block_instructions(*, block: cfg.BasicBlock) -> list[ir.Instruction]:
    """Return a block's instructions followed by its terminator (if any)."""
    if block.terminator is None:
        return list(block.instructions)
    return [*block.instructions, block.terminator]


def _conservative_coalesce(
    *,
    constraints: RegisterConstraints,
    interference: dict[str, set[str]],
    moves: set[frozenset[str]],
) -> tuple[dict[str, set[str]], dict[str, str]]:
    """Merge safe move-related pairs; return (merged graph, alias->representative).

    Briggs: merge ``{a, b}`` only when they do not interfere and the union of
    their neighbors has fewer than ``K`` members of significant degree
    (``>= K``).  Precolored endpoints and endpoints with incompatible ``allowed``
    sets are left un-coalesced (PR 1 keeps coalescing to the common
    same-constraint case; richer constrained coalescing is deferred).
    """
    pool_size = len(constraints.pool)
    graph: dict[str, set[str]] = {name: set(neighbors) for name, neighbors in interference.items()}
    alias: dict[str, str] = {}

    def find(name: str) -> str:
        while name in alias:
            name = alias[name]
        return name

    for pair in sorted(moves, key=lambda members: tuple(sorted(members))):
        first, second = sorted(pair)
        rep_a, rep_b = find(first), find(second)
        if rep_a == rep_b or rep_b in graph[rep_a]:
            continue
        if rep_a in constraints.precolored or rep_b in constraints.precolored:
            continue
        if constraints.allowed.get(rep_a) != constraints.allowed.get(rep_b):
            continue
        merged_neighbors = graph[rep_a] | graph[rep_b]
        significant = sum(1 for neighbor in merged_neighbors if len(graph[neighbor]) >= pool_size)
        if significant >= pool_size:
            continue
        for neighbor in graph[rep_b]:
            graph[neighbor].discard(rep_b)
            graph[neighbor].add(rep_a)
            graph[rep_a].add(neighbor)
        graph[rep_a].discard(rep_a)
        del graph[rep_b]
        alias[rep_b] = rep_a

    return graph, alias


def _instruction_defs(*, instruction: ir.Instruction) -> tuple[str, ...]:
    """Return the destination name(s) written by *instruction* (empty if none)."""
    if isinstance(instruction, _DESTINATION_TYPES):
        destination = instruction.destination
        return () if destination is None else (destination,)
    return ()


def _instruction_uses(*, instruction: ir.Instruction) -> tuple[str, ...]:
    """Return every name read by *instruction*, exhaustively.

    Combines VALUE_FIELDS reads (filtered to ``str`` operands), the name-string
    operands the VALUE_FIELDS walk skips (``Index.base`` / ``IndexAssign.base`` /
    ``RepString.dest`` / ``RepString.source``), and opaque-AST reads.  Raises
    ``RegallocError`` for an unmodeled instruction so coverage stays exhaustive.
    """
    if isinstance(instruction, _INERT_TYPES):
        return ()
    if isinstance(instruction, _OPAQUE_TYPES):
        if isinstance(instruction, ir.Switch):
            names = list(_iter_ast_var_names(node=instruction.discriminant))
            for case in instruction.cases:
                for inner in case.body:
                    names.extend(_instruction_uses(instruction=inner))
            return tuple(names)
        ast_node = instruction.call_ast if isinstance(instruction, ir.CarryBranch) else instruction.node
        return tuple(_iter_ast_var_names(node=ast_node))
    if not isinstance(instruction, _MODELED_VALUE_TYPES):
        message = f"regalloc: unhandled instruction {type(instruction).__name__}"
        raise RegallocError(message)
    names: list[str] = []
    for field_name in instruction.VALUE_FIELDS:
        value = getattr(instruction, field_name)
        if value is None:
            continue
        operands = value if isinstance(value, tuple) else (value,)
        names.extend(operand for operand in operands if isinstance(operand, str))
    if isinstance(instruction, (ir.Index, ir.IndexAssign)):
        names.append(instruction.base)
    if isinstance(instruction, ir.RepString):
        names.append(instruction.dest)
        if instruction.source is not None:
            names.append(instruction.source)
    return tuple(names)


def _iter_ast_var_names(*, node: object) -> Iterator[str]:
    """Yield every ``Var`` / ``VariablePlace`` name in the AST subtree at *node*.

    Local copy of ``cc.ir_optimize._iter_ast_var_names`` (cc/ssa.py keeps its
    own copy too) so regalloc.py does not import the optimizer's private API.

    Yields:
        Each variable name string found in the subtree.

    """
    if isinstance(node, ast_nodes.Var):
        yield node.name
        return
    if isinstance(node, ast_nodes.VariablePlace):
        yield node.name
        return
    for bare_name_field in ("target_name", "object_name"):
        bare_name = getattr(node, bare_name_field, None)
        if isinstance(bare_name, str):
            yield bare_name
    if dataclasses.is_dataclass(node):
        for declared_field in dataclasses.fields(node):
            yield from _iter_ast_var_names(node=getattr(node, declared_field.name))
        return
    if isinstance(node, (list, tuple)):
        for item in node:
            yield from _iter_ast_var_names(node=item)


def _spill_metric(*, costs: CostModel, degree: dict[str, set[str]], name: str, remaining: set[str]) -> tuple[float, str]:
    """Return the Chaitin spill metric for *name*: (benefit/degree, name).

    Lower values spill first — lowest benefit relative to current degree.
    The tiebreaking name component ensures a deterministic total order.
    """
    current_degree = len(degree[name] & remaining) or 1
    return (costs.spill_benefit.get(name, 0) / current_degree, name)


@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class Allocation:
    """The colorer's result: register homes + the spilled set."""

    homes: dict[str, str]
    spilled: frozenset[str]


@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class CostModel:
    """The soft-cost economics driving register choice and spill decisions.

    ``spill_benefit`` maps a value to how much it wants a register (the auto-pin
    reference count); a value whose benefit does not exceed its chosen
    register's save cost is spilled instead.  ``register_save_cost`` maps a
    value to ``{register: push/pop save cost}`` — the per-call-crossing cost of
    homing that value in that register (from ``register_clobber_counts`` minus
    pre-first-store elision in PR 2/3).  A missing entry means zero cost.
    """

    register_save_cost: dict[str, dict[str, int]]
    spill_benefit: dict[str, int]


@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class InterferenceResult:
    """Output of ``build_interference``.

    ``graph`` is the symmetric adjacency map over allocatable values.  ``moves``
    is the set of coalesce candidates (each a frozenset of two values related by
    a ``Copy``).  ``live_across_call`` maps a value to the number of ``Call`` /
    ``TailCall`` / ``CarryBranch`` instructions it is live across (feeds the
    save-cost model in PR 2/3).
    """

    graph: dict[str, set[str]]
    live_across_call: dict[str, int]
    moves: set[frozenset[str]]


class RegallocError(Exception):
    """Raised when the allocator meets an IR shape it does not model.

    Mirrors ``cc.codegen.liveness.LivenessAnalysisError``: failing loud forces
    the def/use model to be updated when a new IR shape lands, rather than
    silently understating interference (which would let two simultaneously-live
    values share a register — a miscompile).
    """


@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class RegisterConstraints:
    """Hard register constraints the colorer must respect.

    ``pool`` is the ordered tuple of allocatable physical registers (``K`` =
    ``len(pool)``).  ``allowed`` maps a value to the subset of ``pool`` it may
    occupy (a value absent from ``allowed`` may use any pool register); this is
    where byte-alias and 16-bit-index legality land.  ``precolored`` pins a
    value to a fixed register (e.g. a regparm parameter that arrives in EAX/
    EDX/ECX); precolored values are never simplified or spilled.
    """

    allowed: dict[str, frozenset[str]]
    pool: tuple[str, ...]
    precolored: dict[str, str]


def allocate(
    *,
    allocatable: frozenset[str],
    constraints: RegisterConstraints,
    costs: CostModel,
    function: ir.Function,
) -> Allocation:
    """Compute interference for *function* and color it — the end-to-end entry.

    PR 2/3 will call this with target-derived *constraints* / *costs* and then
    wire ``Allocation.homes`` into emission; PR 1 leaves it unconsumed.
    """
    interference = build_interference(allocatable=allocatable, function=function)
    return color(
        constraints=constraints,
        costs=costs,
        interference=interference.graph,
        moves=interference.moves,
    )


def build_interference(*, allocatable: frozenset[str], function: ir.Function) -> InterferenceResult:
    """Compute the interference graph, move pairs, and live-across-call counts.

    Block-level backward dataflow to a fixed point, then a backward walk of each
    block adding Chaitin interference edges (each def vs. the live set) and
    recording Copy move pairs.  Only names in *allocatable* become graph nodes.
    """
    graph = cfg.build_cfg(function.body)
    blocks = graph.blocks

    block_use: dict[cfg.BasicBlock, set[str]] = {}
    block_def: dict[cfg.BasicBlock, set[str]] = {}
    for block in blocks:
        uses: set[str] = set()
        defs: set[str] = set()
        for instruction in _block_instructions(block=block):
            for name in _instruction_uses(instruction=instruction):
                if name in allocatable and name not in defs:
                    uses.add(name)
            for name in _instruction_defs(instruction=instruction):
                if name in allocatable:
                    defs.add(name)
        block_use[block] = uses
        block_def[block] = defs

    live_in: dict[cfg.BasicBlock, set[str]] = {block: set() for block in blocks}
    live_out: dict[cfg.BasicBlock, set[str]] = {block: set() for block in blocks}
    changed = True
    while changed:
        changed = False
        for block in blocks:
            new_out: set[str] = set()
            for successor in block.successors:
                new_out |= live_in[successor]
            new_in = block_use[block] | (new_out - block_def[block])
            if new_in != live_in[block] or new_out != live_out[block]:
                live_in[block] = new_in
                live_out[block] = new_out
                changed = True

    adjacency: dict[str, set[str]] = {name: set() for name in allocatable}
    moves: set[frozenset[str]] = set()
    live_across_call: Counter[str] = Counter()

    def add_edge(*, name_a: str, name_b: str) -> None:
        if name_a == name_b:
            return
        adjacency[name_a].add(name_b)
        adjacency[name_b].add(name_a)

    for block in blocks:
        live = set(live_out[block])
        for instruction in reversed(_block_instructions(block=block)):
            if isinstance(instruction, _CALL_TYPES):
                for name in live:
                    live_across_call[name] += 1
            defs = [name for name in _instruction_defs(instruction=instruction) if name in allocatable]
            for defined in defs:
                for other in live:
                    add_edge(name_a=defined, name_b=other)
            if (
                isinstance(instruction, ir.Copy)
                and isinstance(instruction.source, str)
                and instruction.destination in allocatable
                and instruction.source in allocatable
            ):
                moves.add(frozenset({instruction.destination, instruction.source}))
            live.difference_update(defs)
            for name in _instruction_uses(instruction=instruction):
                if name in allocatable:
                    live.add(name)

    return InterferenceResult(graph=adjacency, live_across_call=dict(live_across_call), moves=moves)


def color(
    *,
    constraints: RegisterConstraints,
    costs: CostModel,
    interference: dict[str, set[str]],
    moves: set[frozenset[str]],
) -> Allocation:
    """Color *interference* with the pool in *constraints*, spilling by cost.

    Chaitin-Briggs: conservative coalescing of move pairs, simplify (remove
    ``< K`` degree non-precolored nodes), optimistic-spill push, then select
    (assign each popped node a legal color by lowest save cost, or spill when
    no register is legal or the benefit gate fails).  Aliases from coalescing
    are expanded so every original value inherits its representative's outcome.
    """
    merged_graph, alias = _conservative_coalesce(constraints=constraints, interference=interference, moves=moves)

    pool_size = len(constraints.pool)
    precolored = dict(constraints.precolored)
    nodes = [name for name in merged_graph if name not in precolored]
    degree: dict[str, set[str]] = {name: set(merged_graph[name]) for name in nodes}

    stack: list[str] = []
    remaining = set(nodes)
    while remaining:
        simplifiable = sorted(name for name in remaining if len(degree[name] & remaining) < pool_size)
        if simplifiable:
            for name in simplifiable:
                stack.append(name)
                remaining.discard(name)
            continue
        victim = min(
            remaining,
            key=lambda candidate: _spill_metric(costs=costs, degree=degree, name=candidate, remaining=remaining),
        )
        stack.append(victim)
        remaining.discard(victim)

    homes: dict[str, str] = dict(precolored)
    spilled: set[str] = set()
    while stack:
        name = stack.pop()
        used = {homes[neighbor] for neighbor in merged_graph[name] if neighbor in homes}
        legal = [reg for reg in _allowed_registers(constraints=constraints, value=name) if reg not in used]
        if not legal:
            spilled.add(name)
            continue
        save_costs = costs.register_save_cost.get(name, {})
        choice = min(legal, key=lambda reg, save_costs=save_costs: (save_costs.get(reg, 0), constraints.pool.index(reg)))
        benefit = costs.spill_benefit.get(name, 0)
        if name in costs.spill_benefit and benefit <= save_costs.get(choice, 0):
            spilled.add(name)
            continue
        homes[name] = choice

    def _resolve(name: str) -> str:
        while name in alias:
            name = alias[name]
        return name

    final_homes: dict[str, str] = {}
    final_spilled: set[str] = set()
    for name in interference:
        representative = _resolve(name)
        if representative in homes:
            final_homes[name] = homes[representative]
        else:
            final_spilled.add(name)
    return Allocation(homes=final_homes, spilled=frozenset(final_spilled))
