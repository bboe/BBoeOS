"""Translate cc.py's auto-pin economics into cc.regalloc engine inputs.

PR 2 colors user locals/params only.  The economics (reference counts, per-
register clobber save costs, BP index penalty, byte-alias legality) come from
the AST; interference comes from the AST LivenessAnalyzer.  The result feeds
cc.regalloc.color() directly (no ir.Function required).
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from cc import regalloc

if TYPE_CHECKING:
    from cc.codegen.x86.generator import AutoPinEconomics


@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class AllocatorInputs:
    """Everything cc.regalloc.color() needs for one function's locals/params."""

    allocatable: frozenset[str]
    constraints: regalloc.RegisterConstraints
    costs: regalloc.CostModel
    interference: dict[str, set[str]]


def build_allocator_inputs(
    *,
    argument_affinity: dict[str, dict[str, int]],
    base_register: str | None,
    byte_pool: frozenset[str],
    economics: AutoPinEconomics,
    interference: dict[str, set[str]],
    pool: tuple[str, ...],
    precolored: dict[str, str],
    register_clobber_counts: dict[str, int],
) -> AllocatorInputs:
    """Build the engine inputs for *economics* over *pool*.

    ``argument_affinity`` is the per-value {register: count} of register-
    convention call-argument uses (arg1→EDX, arg2→ECX): each count discounts
    that register's save cost, so the colorer homes the value where the call site
    already wants it and skips a ``mov``.  The discount is allowed to drive the
    save cost negative — that strongly prefers the convention register and keeps
    the value from spilling for cost reasons, mirroring the heuristic's homing of
    arg-affine values.  (The tally that produces ``argument_affinity`` already
    suppresses the term for functions whose register-custom builtin call sites
    could otherwise be perturbed into an unbreakable 16-bit cyclic register
    dependency — see ``_compute_argument_register_affinity`` — so the negative
    discount here is unconditional.)
    ``base_register`` is the frame-pointer register that carries a per-subscript
    index penalty (``index_uses``) instead of a clobber cost; pass ``None`` when
    the frame pointer is not in the allocatable pool.
    ``byte_pool`` is the subset of *pool* with an 8-bit alias (AL/BL/CL/DL).
    ``register_clobber_counts`` is the per-function {register: clobbering-call
    count}; ``pool`` is ordered by ascending clobber cost (the colorer's
    tiebreak).  Save cost for a register is its clobber count minus the
    candidate's pre-first-store elided clobbers (then minus its argument
    affinity bonus); for the frame-pointer register (zero clobber) it is the
    candidate's subscript count (the per-index ``mov si, bp`` penalty).
    """
    allocatable = economics.allocatable

    register_save_cost: dict[str, dict[str, int]] = {}
    for name in allocatable:
        elided = economics.pre_store_clobbers.get(name, {})
        affinity = argument_affinity.get(name, {})
        base_register_cost: dict[str, int] = {}
        for register in pool:
            if register == base_register:
                base_register_cost[register] = economics.index_uses.get(name, 0)
            else:
                base_register_cost[register] = max(0, register_clobber_counts.get(register, 0) - elided.get(register, 0))
        # Subtract the affinity bonus; allow the result to go negative so the
        # convention register is strongly preferred and the value never spills
        # for cost reasons (the tally upstream suppresses the term where this
        # could break 16-bit builtin arg lowering).
        per_register = {register: base_register_cost[register] - affinity.get(register, 0) for register in pool}
        register_save_cost[name] = per_register

    spill_benefit = {name: economics.reference_counts.get(name, 0) for name in allocatable}

    allowed: dict[str, frozenset[str]] = {}
    for name in economics.byte_typed & allocatable:
        allowed[name] = frozenset(register for register in pool if register in byte_pool)

    restricted: dict[str, set[str]] = {
        name: {neighbor for neighbor in neighbors if neighbor in allocatable}
        for name, neighbors in interference.items()
        if name in allocatable
    }
    for name in allocatable:
        restricted.setdefault(name, set())

    return AllocatorInputs(
        allocatable=allocatable,
        constraints=regalloc.RegisterConstraints(allowed=allowed, pool=pool, precolored=dict(precolored)),
        costs=regalloc.CostModel(register_save_cost=register_save_cost, spill_benefit=spill_benefit),
        interference=restricted,
    )
