"""Resolved, target-aware address plans for ``ir.Address`` operations.

An :class:`AddressPlan` is the pure, post-layout form of one ``ir.Address``:
the AST ``shape``'s job ends when the planner produces a plan, and emission
materializes the plan into a :class:`~cc.codegen.x86.generator.MemoryOperand`
without ever re-walking AST. Design:
``design-specs:2026-06-06-cc-native-address-emission-design.md``.

Phase 1 keeps every plan in *folded* mode (the producing ``Address`` op emits
nothing; the consuming terminal absorbs the materialization). The
``clobbers`` field is a declared fact for the register allocator — computed
by the planner, unconsumed until phase 2.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cc.codegen.x86.generator import FieldInfo


@dataclass(kw_only=True, slots=True)
class AddressPlan:
    """The resolved form of one ``ir.Address`` (see module docstring).

    ``base_kind`` selects how ``base`` reads:

    - ``"frame"`` — ``base`` is a frame-relative string (``"ebp-8"``).
    - ``"label"`` — ``base`` is a NASM label (``"_g_table"``).
    - ``"plan"`` — ``base`` is a nested :class:`AddressPlan` whose
      materialization (a decayed struct-value member address) seeds the base
      register; used by chained dot members (``a.b.c``).
    - ``"pointer"`` — ``base`` is a named pointer variable; materialization
      loads it via the shared SI-or-BX base load (``_load_member_base``).

    ``base_is_static`` / ``base_preserves_accumulator`` capture the store
    orderings of ``_emit_member_scalar_resolved_store``; ``horner`` marks a
    multi-term plan whose legacy materialization is the row-major Horner walk
    rather than per-term scale-and-sum.

    ``subscript_terminal`` marks a plan whose shape the legacy dispatch
    routed through the protect-BX subscript terminals
    (``_emit_subscript_resolved_load`` / ``_emit_subscript_resolved_store``)
    — even when every index folded to a constant (``terms`` empty), because
    those terminals emit the BX guard and the rhs spill unconditionally.
    """

    base: AddressPlan | str
    base_kind: str  # "frame" | "label" | "plan" | "pointer"
    base_is_static: bool = True
    base_preserves_accumulator: bool = False
    bitfield: FieldInfo | None = None
    clobbers: frozenset[str] = field(default_factory=frozenset)
    decay_to_address: bool = False
    displacement: int = 0
    element_size: int = 0
    field_size: int = 0
    horner: bool = False
    line: int = 0
    raw_width: bool = False
    subscript_terminal: bool = False
    terms: tuple[AddressTerm, ...] = ()


@dataclass(kw_only=True, slots=True)
class AddressTerm:
    """One dynamic subscript: ``index_value`` scaled by ``scale`` bytes."""

    index_value: int | str
    scale: int


def scale_encodes_in_operand(*, bits: int, scale: int) -> bool:
    """Return True when ``scale`` folds into a memory operand at ``bits``.

    32-bit SIB encoding supports scales 1/2/4/8. 16-bit addressing has no
    SIB byte: an index register participates only unscaled.
    """
    if bits == 16:
        return scale == 1
    return scale in (1, 2, 4, 8)
