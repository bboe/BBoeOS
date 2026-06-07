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
    row-major multidim plan whose materialization is the legacy Horner walk
    (``_emit_horner_index_offsets`` over the term strides) rather than the
    per-term ``_accumulate_subscript`` scale-and-sum.  Each
    :class:`AddressTerm` ``scale`` is the BYTE stride of one step of that
    index (outermost dimension first); constant indices are pre-folded into
    ``displacement`` exactly where the legacy walk folded them.  On a Horner
    plan ``base_kind="pointer"`` means the base register is seeded by loading
    the named pointer's VALUE into SI (the arrow-member / pointer-to-array
    walks), not by the SI-or-BX member-base load the scalar pointer arm uses.

    ``base_always_in_register`` marks a Horner plan whose base address is
    materialized into the SI base register unconditionally — even when every
    index folded into the displacement (the member-multidim ``lea`` and the
    pointer-value load both always run).  Bare-multidim plans leave it False:
    their base stays a static frame/label operand unless a dynamic index over
    a frame base forces the SI materialization at 16-bit.

    ``subscript_terminal`` marks a plan whose shape the legacy dispatch
    routed through the protect-BX subscript terminals
    (``_emit_subscript_resolved_load`` / ``_emit_subscript_resolved_store``)
    — even when every index folded to a constant (``terms`` empty), because
    those terminals emit the BX guard and the rhs spill unconditionally.

    ``call_slot`` marks the function-pointer-slot plan of a statement
    ``name[index]()`` call.  Its materialization is owned exclusively by the
    ``ir.IndirectCall`` terminal (the legacy ``generate_indexed_call`` SI-base
    accumulate, which differs from the BX-seeded ``_accumulate_subscript``
    walk); the generic materializer refuses it loudly.

    ``deref_store`` marks the bare ``*pointer = leaf`` store plan over a
    named pointer.  Like ``call_slot``, its emission is owned exclusively by
    its terminal (``ir.Store``): the pointer VALUE loads into SI via
    ``_emit_load_var`` (not the member "pointer" kind's SI-or-BX
    ``_load_member_base``) and the store width is the plan's ``field_size``
    (the legacy byte-vs-full-accumulator select, including its documented
    ``unsigned short *`` gap); the generic materializer refuses it loudly.
    """

    base: AddressPlan | str
    base_kind: str  # "frame" | "label" | "plan" | "pointer"
    base_always_in_register: bool = False
    base_is_static: bool = True
    base_preserves_accumulator: bool = False
    bitfield: FieldInfo | None = None
    call_slot: bool = False
    clobbers: frozenset[str] = field(default_factory=frozenset)
    decay_to_address: bool = False
    deref_store: bool = False
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
