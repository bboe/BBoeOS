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
