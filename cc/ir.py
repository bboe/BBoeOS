"""Three-address code (TAC) intermediate representation.

A :data:`Value` operand is either an integer constant or a name
(variable, temp, or string label).  Every TAC instruction has at most
one operator and one destination, with simple operands on the right-
hand side.  :class:`Builder` flattens an AST :class:`cc.ast_nodes.Program`
into a flat list of instructions per function; :mod:`cc.codegen` then
lowers them to x86 assembly.

Import this module as a namespace (``from cc import ir``) so the
instruction types read as ``ir.BinaryOperation``, ``ir.Call`` etc.
Several names overlap with :mod:`cc.ast_nodes` (``BinaryOperation``,
``Call``, ``Function``, …) — the module prefix disambiguates the IR
form from the AST form.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import ClassVar

from cc import ast_nodes
from cc.errors import CompileError
from cc.tokens import COMPARISON_OPERATIONS, INVERT_COMPARISON

Value = int | str | ast_nodes.PlaceAddressOf


class _NoValueFields:
    """Mixin for instruction classes that read no :data:`Value` operands.

    Centralizes the empty :attr:`VALUE_FIELDS` declaration so opaque /
    control-only instructions (Block, Jump, Label, LoopBoundary, Switch,
    etc.) inherit the contract instead of redeclaring ``= ()``.  The
    empty :attr:`__slots__` keeps the mixin compatible with
    ``@dataclass(slots=True)`` subclasses — without it, subclass
    instances would still get a ``__dict__``.
    """

    __slots__ = ()
    VALUE_FIELDS: ClassVar[tuple[str, ...]] = ()


def _is_arrow_member_address_of(node: ast_nodes.Node, /) -> bool:
    """Return True for ``&pointer->member`` address-of on a plain pointer variable.

    ``PlaceAddressOf(MemberPlace(DereferencePlace(VariablePlace(p)), field))`` is
    the lea-terminal address twin of :func:`_is_arrow_member_load`: the chain
    breaks at the dereference, so the segment's ``base_value`` is the pointer
    ``Value`` (the name ``"p"``), which emission re-materializes IDENTICALLY to
    the inline ``generate_expression`` the legacy ``_emit_place_address_of`` path
    runs — no separate pointer ``Load`` op, byte-neutral.  AddressOf is a pure
    ``lea`` with no dereference and no width.  Stage 3b.1 lowers exactly this
    shape onto :class:`Address` + :class:`AddressOf` (``&directory->entry`` in
    ``dirent.c``); every other address-of shape stays on :class:`Block`
    unchanged (``&var`` rides a passed-through leaf, not this path).
    """
    return (
        isinstance(node, ast_nodes.PlaceAddressOf)
        and isinstance(node.place, ast_nodes.MemberPlace)
        and isinstance(node.place.base, ast_nodes.DereferencePlace)
        and isinstance(node.place.base.pointer, ast_nodes.VariablePlace)
    )


def _is_arrow_member_load(node: ast_nodes.Node, /) -> bool:
    """Return True for ``pointer->member`` reads on a plain pointer variable.

    ``PlaceLoad(MemberPlace(DereferencePlace(VariablePlace(p)), field))`` is an
    arrow-member read where the dereferenced pointer is a plain pointer-variable
    read.  The chain breaks at the dereference: the segment's ``base_value`` is
    the pointer ``Value`` ``_build_expr(Var(p))`` (the name ``"p"``), which
    emission turns back into ``Var("p")`` and materializes IDENTICALLY to the
    inline ``generate_expression`` the legacy arrow-load path runs, so the load
    is byte-neutral with no separate pointer ``Load`` op for this plain-var case.
    Stage 3b.1 slice 2 lowers exactly this shape onto :class:`Address` +
    :class:`Load`; every other arrow / deref shape (``(*pp)->f``, ``a[i]->f``,
    ``expr->f``, multi-level ``p->a->b``) stays on :class:`Access` unchanged.
    """
    return (
        isinstance(node, ast_nodes.PlaceLoad)
        and isinstance(node.place, ast_nodes.MemberPlace)
        and isinstance(node.place.base, ast_nodes.DereferencePlace)
        and isinstance(node.place.base.pointer, ast_nodes.VariablePlace)
    )


def _is_arrow_member_member_load(node: ast_nodes.Node, /) -> bool:
    """Return True for ``pointer->outer.inner`` reads — member-of-member-of-deref.

    ``PlaceLoad(MemberPlace(MemberPlace(DereferencePlace(VariablePlace(p)),
    outer), inner))`` is the simplest MULTI-LEVEL member access: one chain-break
    at the dereference of a plain pointer variable, then TWO static member
    offsets (``outer`` selecting an embedded struct value, ``inner`` selecting
    its field) accumulate into the same deref-broken segment shape.  No new
    chain-break mechanic beyond slice 2's arrow load — the only difference is a
    longer static member shape, which ``resolve_address`` /
    ``_resolve_member_place_info`` already accumulate recursively (Stage 3a).
    The segment's ``base_value`` is the pointer ``Value`` ``p`` (the single
    dynamic leaf the emission helpers read), so the optimizer counts the pointer
    use exactly as the arrow-load slice does.  Stage 3b.1 slice 6 lowers exactly
    this two-level shape onto :class:`Address` + :class:`Load`; deeper / mixed
    shapes (``p->a->b``, ``p->a.b.c``) stay on :class:`Access` unchanged.
    """
    return (
        isinstance(node, ast_nodes.PlaceLoad)
        and isinstance(node.place, ast_nodes.MemberPlace)
        and isinstance(node.place.base, ast_nodes.MemberPlace)
        and isinstance(node.place.base.base, ast_nodes.DereferencePlace)
        and isinstance(node.place.base.base.pointer, ast_nodes.VariablePlace)
    )


def _is_arrow_member_member_store(node: ast_nodes.Node, /) -> bool:
    """Return True for ``pointer->outer.inner = leaf`` writes — the multi-level store twin.

    ``PlaceStore(MemberPlace(MemberPlace(DereferencePlace(VariablePlace(p)),
    outer), inner), value)`` is the store twin of
    :func:`_is_arrow_member_member_load`: one chain-break at the plain-pointer
    dereference, then two static member offsets, then the byte-safe leaf RHS is
    written through it.  The segment's ``base_value`` is the pointer ``Value``
    ``p``.  Gated on a byte-safe leaf RHS (:func:`_is_byte_safe_store_rhs`) so
    the RHS-vs-address ordering stays byte-identical to the legacy store.  Stage
    3b.1 slice 6 lowers exactly this two-level shape onto :class:`Address` +
    :class:`Store` (``directory->entry.d_ino = ...`` in ``dirent.c`` is a real
    consumer); deeper / mixed shapes stay on :class:`Access` unchanged.
    """
    return (
        isinstance(node, ast_nodes.PlaceStore)
        and isinstance(node.place, ast_nodes.MemberPlace)
        and isinstance(node.place.base, ast_nodes.MemberPlace)
        and isinstance(node.place.base.base, ast_nodes.DereferencePlace)
        and isinstance(node.place.base.base.pointer, ast_nodes.VariablePlace)
        and _is_byte_safe_store_rhs(node.value)
    )


def _is_arrow_member_store(node: ast_nodes.Node, /) -> bool:
    """Return True for ``pointer->member = leaf`` writes on a plain pointer variable.

    ``PlaceStore(MemberPlace(DereferencePlace(VariablePlace(p)), field), value)``
    is the store twin of :func:`_is_arrow_member_load`: the chain breaks at the
    dereference, so the segment's ``base_value`` is the pointer ``Value`` (the
    name ``"p"``), which emission re-materializes IDENTICALLY to the inline
    ``generate_expression`` the legacy arrow-store path runs.  Gated on a
    byte-safe leaf RHS (:func:`_is_byte_safe_store_rhs`): without a register
    allocator a compound RHS pre-lowered to a temp would spill/reload and
    reorder versus the legacy store's RHS-vs-base evaluation ordering, breaking
    byte-neutrality.  Stage 3b.1 slice 3 lowers exactly this shape onto
    :class:`Address` + :class:`Store`; every other arrow / deref store shape
    stays on :class:`Access` unchanged.
    """
    return (
        isinstance(node, ast_nodes.PlaceStore)
        and isinstance(node.place, ast_nodes.MemberPlace)
        and isinstance(node.place.base, ast_nodes.DereferencePlace)
        and isinstance(node.place.base.pointer, ast_nodes.VariablePlace)
        and _is_byte_safe_store_rhs(node.value)
    )


def _is_byte_safe_store_rhs(node: ast_nodes.Node, /) -> bool:
    """Return True for a member-store RHS that :meth:`_build_expr` lowers with no emitted code.

    ``Int`` / ``Var`` / ``String`` / ``&variable`` are the leaves
    :meth:`_build_expr` returns directly as an :data:`Value` (an immediate, a
    name, a string label, or a passed-through ``PlaceAddressOf``) WITHOUT
    appending any preceding instruction.  Restricting member-store migration to
    these leaves keeps the RHS-vs-address evaluation ordering byte-identical to
    the legacy store: ``_ir_value_to_ast`` round-trips the leaf back to the
    exact AST node the legacy ``_emit_place_store`` evaluated in place, so no
    spill/reload is introduced.

    A COMPOUND RHS stays excluded — re-measured in Stage 3b.1 slice 5 on top of
    PR #587's IR-temp register allocator and confirmed STILL byte-regressing.
    The legacy ``_emit_place_store`` computes the RHS directly into the
    accumulator and stores it through the freshly materialized address; lifting
    the RHS to an IR temp evaluated BEFORE the address forces the allocator to
    spill the temp to a frame slot and reload it, because resolving the store
    address — and any RHS sub-computation that clobbers a scratch register, e.g.
    ``div``'s ``edx`` — reuses the register the temp would otherwise live in.
    Measured deltas admitting a ``BinaryOperation`` RHS: ``tv->tv_sec =
    total_ms / 1000`` grew +21 bytes (an ``eax`` spill/reload plus a
    ``push``/``pop edx`` around the ``div``), with further regressions in
    ``readdir`` / ``_emit_str`` / ``release`` / ``malloc`` / ``symbol_add`` /
    ``strtol``.  Unlike a compound subscript INDEX leaf (slice 4) — which the
    allocator keeps register-resident because it is consumed IMMEDIATELY by the
    address scale — a compound RHS must stay live ACROSS the address resolution,
    so its live range crosses the clobber and the allocator cannot save it.  A
    compound RHS therefore keeps the store on :class:`Access`.
    """
    return isinstance(node, (ast_nodes.Int, ast_nodes.String, ast_nodes.Var)) or (
        isinstance(node, ast_nodes.PlaceAddressOf) and isinstance(node.place, ast_nodes.VariablePlace)
    )


def _is_constant_true(condition: ast_nodes.Node, /) -> bool:
    """Return True if *condition* is statically nonzero.

    Recognises both the bare ``Int(value=N)`` form (not wrapped by the
    parser) and the ``BinaryOperation("!=", Int(value=N), Int(value=0))``
    form that ``Parser.parse_condition`` produces for bare expressions.
    """
    if isinstance(condition, ast_nodes.Int) and condition.value != 0:
        return True
    if not isinstance(condition, ast_nodes.BinaryOperation):
        return False
    if condition.operation != "!=":
        return False
    if condition.right != ast_nodes.Int(value=0):
        return False
    return isinstance(condition.left, ast_nodes.Int) and condition.left.value != 0


def _is_deref_store(node: ast_nodes.Node, /) -> bool:
    """Return True for ``*pointer = leaf`` writes on a plain pointer variable.

    ``PlaceStore(DereferencePlace(Var(p)), value)`` is the lvalue deref store:
    the parser only produces a bare :class:`DereferencePlace` as a store target
    for the ``*name = expr`` form (rvalue ``*p`` desugars to ``Index(p, 0)``
    instead), and only over a plain pointer-variable read ``Var(p)``.  This is
    the no-member, no-index sibling of :func:`_is_arrow_member_store`: the
    dereference IS the whole place, so the segment's ``base_value`` is the
    pointer ``Value`` (the name ``"p"``) and there is no static member offset to
    derive.  Gated on a byte-safe leaf RHS (:func:`_is_byte_safe_store_rhs`) so
    the RHS-vs-address ordering stays byte-identical to the legacy deref store.
    Emission drives the EXACT legacy ``_emit_place_store`` path off the
    immutable ``DereferencePlace`` ``shape``; ``base_value`` is purely the
    optimizer-visible dynamic leaf.  Stage 3b.1 slice 5 lowers exactly this
    plain-pointer-var subset onto :class:`Address` + :class:`Store`; every
    other deref store shape (``*(p + 1)``, ``*(T *)e``, ``*pp``) stays on
    :class:`Access` unchanged.
    """
    return (
        isinstance(node, ast_nodes.PlaceStore)
        and isinstance(node.place, ast_nodes.DereferencePlace)
        and isinstance(node.place.pointer, ast_nodes.Var)
        and _is_byte_safe_store_rhs(node.value)
    )


def _is_migrated_access(node: ast_nodes.Node, /) -> bool:
    """Return True for the Place-access shapes Stage 1 lowers to :class:`Access`.

    PlaceLoad / PlaceStore / PlaceCall — the always-complex (member /
    dereference / subscript-of-expression) accesses.  PlaceAddressOf and
    PlaceIncrementDecrement stay on :class:`Block` this stage (their
    ``VariablePlace`` forms back the loop induction-variable matchers in
    ``cc.loops``); see the Stage 1 plan's Scope boundary.
    """
    return isinstance(node, (ast_nodes.PlaceCall, ast_nodes.PlaceLoad, ast_nodes.PlaceStore))


def _is_static_member_load(node: ast_nodes.Node, /) -> bool:
    """Return True for ``variable.member`` reads — the simplest static ir.Access load.

    ``PlaceLoad(MemberPlace(VariablePlace))`` is a plain dot-member read of a
    local / global struct: its address is fully static (a member offset on a
    symbol-rooted base, no dynamic subscript, no dereference).  Stage 3b.1
    slice 1 lowers exactly this shape onto :class:`Address` + :class:`Load`;
    every other access shape stays on :class:`Access` unchanged.
    """
    return (
        isinstance(node, ast_nodes.PlaceLoad)
        and isinstance(node.place, ast_nodes.MemberPlace)
        and isinstance(node.place.base, ast_nodes.VariablePlace)
    )


def _is_static_member_store(node: ast_nodes.Node, /) -> bool:
    """Return True for ``variable.member = leaf`` writes — the static store twin.

    ``PlaceStore(MemberPlace(VariablePlace), value)`` is the store twin of
    :func:`_is_static_member_load`: a fully static member offset on a
    symbol-rooted base.  Gated on a byte-safe leaf RHS
    (:func:`_is_byte_safe_store_rhs`) so the RHS-vs-address ordering stays
    byte-identical to the legacy dot store.  Stage 3b.1 slice 3 lowers exactly
    this shape onto :class:`Address` + :class:`Store`; every other store shape
    stays on :class:`Access` unchanged.
    """
    return (
        isinstance(node, ast_nodes.PlaceStore)
        and isinstance(node.place, ast_nodes.MemberPlace)
        and isinstance(node.place.base, ast_nodes.VariablePlace)
        and _is_byte_safe_store_rhs(node.value)
    )


def _is_struct_array_member_load(node: ast_nodes.Node, /) -> bool:
    """Return True for ``array[index].member`` reads — the first DYNAMIC-index ir.Access load.

    ``PlaceLoad(MemberPlace(SubscriptPlace(VariablePlace)))`` is a member read
    of a struct-array element: one dynamic subscript ``index`` selects the
    element, then a static member offset selects the field.  This is the
    simplest access shape with exactly ONE dynamic index leaf and otherwise
    static structure — the SubscriptPlace base is a bare array variable (no
    dereference, no nested subscript), so the chain never breaks
    (``base_value`` stays ``None``) and the single subscript index is the
    segment's only dynamic leaf.  Stage 3b.1 slice 4 lowers exactly this shape
    onto :class:`Address` (carrying the element index in its ``index`` leaf) +
    :class:`Load`, proving the design's central mechanic: pre-lowering a
    dynamic subscript index to an optimizer-visible :data:`Value` that emission
    then materializes / scales exactly where ``_accumulate_subscript`` does.
    Every other subscript / member shape stays on :class:`Access` unchanged.
    """
    return (
        isinstance(node, ast_nodes.PlaceLoad)
        and isinstance(node.place, ast_nodes.MemberPlace)
        and isinstance(node.place.base, ast_nodes.SubscriptPlace)
        and isinstance(node.place.base.base, ast_nodes.VariablePlace)
    )


def _is_struct_array_member_store(node: ast_nodes.Node, /) -> bool:
    """Return True for ``array[index].member = leaf`` writes — the dynamic-index store twin.

    ``PlaceStore(MemberPlace(SubscriptPlace(VariablePlace)), value)`` is the
    store twin of :func:`_is_struct_array_member_load`: one dynamic subscript
    ``index`` selects the struct-array element, then a static member offset
    selects the field, then the byte-safe leaf RHS is written through it.  The
    SubscriptPlace base is a bare array variable (no dereference, no nested
    subscript), so the chain never breaks (``base_value`` stays ``None``) and
    the single subscript index is the segment's only dynamic leaf — pre-lowered
    to a :data:`Value` carried on ``Address.indices`` and re-seated into the
    ``shape`` at emission by :meth:`_ir_address_with_index`, exactly as the load
    twin does.  Gated on a byte-safe leaf RHS (:func:`_is_byte_safe_store_rhs`)
    so the RHS-vs-address ordering stays byte-identical to the legacy store.
    Stage 3b.1 slice 5 lowers exactly this shape onto :class:`Address` +
    :class:`Store`; every other subscript-member store shape stays on
    :class:`Access` unchanged.
    """
    return (
        isinstance(node, ast_nodes.PlaceStore)
        and isinstance(node.place, ast_nodes.MemberPlace)
        and isinstance(node.place.base, ast_nodes.SubscriptPlace)
        and isinstance(node.place.base.base, ast_nodes.VariablePlace)
        and _is_byte_safe_store_rhs(node.value)
    )


@dataclass(frozen=True, kw_only=True, slots=True)
class Access(_NoValueFields):
    """Complex-lvalue access (PlaceLoad / PlaceStore / PlaceCall).

    Carved out of :class:`Block` so the access family is a distinct,
    optimizer-visible op (Plan 5 Stage 1).  Like ``Block`` it wraps an
    AST ``node`` lowered by the existing statement codegen and reads no
    IR ``Value`` operands directly — its reads are discovered by walking
    ``node`` (``_iter_ast_var_names``).  Treated identically to ``Block``
    at every conservative / opaque optimizer site; the type tag is what
    later stages key structured-operand handling off of.
    """

    node: ast_nodes.Node


@dataclass(frozen=True, kw_only=True, slots=True)
class Address:
    """Structured-reference value — resolves a place to an address; emits no code itself.

    The IR-level twin of the input to the x86 generator's
    ``resolve_address`` (GCC's GIMPLE structured-reference model).  It
    carries the deref-free place ``shape`` (an AST ``Place`` subtree) so
    emission derives the *static* layout — member offsets, constant
    subscripts, element size, bitfield, array decay — from the existing
    layout helpers, exactly as today.  Its *dynamic* leaves are
    first-class optimizer-visible operands: ``indices`` is the segment's
    per-dimension dynamic element index temps (one :data:`Value` per
    subscript position, outermost dimension first — empty for a
    subscript-free segment, a one-tuple for a single ``array[i].member``
    subscript, an N-tuple for an N-dimensional contiguous-array access
    ``m[i][j]...`` whose per-position strides stay layout-derived at
    emission), and ``base_value`` is the pointer ``Value`` produced by the
    preceding :class:`Load` when the chain was broken at a dereference
    (``None`` for a symbol-rooted — global / local — segment).  Dereference
    breaks the chain, so a segment carries at most one ``base_value`` plus
    the index tuple; ``VALUE_FIELDS`` enumerates both and the tuple is
    flattened element-wise by the operand walkers.

    Pure and DCE-able; never CSE'd or hoisted in Stage 3b (that is 3c).
    """

    VALUE_FIELDS: ClassVar[tuple[str, ...]] = ("base_value", "indices")

    base_value: Value | None
    destination: str
    indices: tuple[Value, ...]
    shape: ast_nodes.Node


@dataclass(frozen=True, kw_only=True, slots=True)
class AddressOf:
    """destination = &place — pure address materialization (lowers to ``lea``).

    Reads the resolved ``address`` :class:`Address` value and writes it to
    ``destination`` without dereferencing.  A read with no memory effect:
    DCE-able when ``destination`` is unused, but not reordered across a
    :class:`Store` / :class:`Call` (memory-barrier discipline).
    """

    VALUE_FIELDS: ClassVar[tuple[str, ...]] = ("address",)

    address: Value
    destination: str


@dataclass(frozen=True, kw_only=True, slots=True)
class BinaryOperation:
    """destination = left operation right — arithmetic or bitwise binary operation."""

    VALUE_FIELDS: ClassVar[tuple[str, ...]] = ("left", "right")

    destination: str
    left: Value
    operation: str
    right: Value


@dataclass(frozen=True, kw_only=True, slots=True)
class Block(_NoValueFields):
    """Escape hatch: lower this AST node via the existing statement codegen."""

    node: ast_nodes.Node


@dataclass(frozen=True, kw_only=True, slots=True)
class BranchFalse:
    """Jump to *target* when the condition ``left operation right`` is FALSE."""

    VALUE_FIELDS: ClassVar[tuple[str, ...]] = ("left", "right")

    left: Value
    operation: str
    right: Value
    target: str


@dataclass(frozen=True, kw_only=True, slots=True)
class Call:
    """destination = name(args) — call expression; destination is None to discard return."""

    VALUE_FIELDS: ClassVar[tuple[str, ...]] = ("args",)

    args: tuple[Value, ...]
    destination: str | None
    name: str


@dataclass(frozen=True, kw_only=True, slots=True)
class CarryBranch(_NoValueFields):
    """Call a ``carry_return`` function, then branch on the carry flag.

    ``__attribute__((carry_return))`` functions report their boolean
    return in CF (clear = true, set = false).  When such a call is used
    directly as an ``if`` / ``while`` condition, lowering the call to a
    value temp and comparing it against zero would lose the CF — we'd
    test whatever happens to be in AX.  ``CarryBranch`` keeps the call
    and the branch together so the lowering emits the tight ``call X /
    jc target`` (when=``set``) or ``jnc target`` (when=``clear``) that
    the AST ``emit_condition`` shortcut produces.  ``call_ast`` holds
    the original :class:`ast_nodes.Call` so ``generate_call`` can set
    up arguments (regparm / stack) the same way a direct AST-path call
    would.
    """

    call_ast: ast_nodes.Call
    target: str
    when: str  # "set" → ``jc``, "clear" → ``jnc``


@dataclass(frozen=True, kw_only=True, slots=True)
class Copy:
    """destination = source — scalar assignment."""

    VALUE_FIELDS: ClassVar[tuple[str, ...]] = ("source",)

    destination: str
    source: Value


@dataclass(frozen=True, kw_only=True, slots=True)
class Index:
    """destination = base[index] — array / pointer read."""

    VALUE_FIELDS: ClassVar[tuple[str, ...]] = ("index",)

    base: str
    destination: str
    index: Value


@dataclass(frozen=True, kw_only=True, slots=True)
class IndexAssign:
    """base[index] = source — array / pointer write."""

    VALUE_FIELDS: ClassVar[tuple[str, ...]] = ("index", "source")

    base: str
    index: Value
    source: Value


@dataclass(frozen=True, kw_only=True, slots=True)
class InlineAsm(_NoValueFields):
    """Pass-through inline-asm block."""

    content: str


@dataclass(frozen=True, kw_only=True, slots=True)
class Jump(_NoValueFields):
    """Unconditional jump."""

    target: str


@dataclass(frozen=True, kw_only=True, slots=True)
class Label(_NoValueFields):
    """A branch target label."""

    name: str


@dataclass(frozen=True, kw_only=True, slots=True)
class Load:
    """destination = *(width) address — memory read at a resolved :class:`Address`.

    ``signed`` selects ``movsx`` vs ``movzx`` for sub-word widths.  A read
    with no memory effect: DCE-able when ``destination`` is unused, but
    not reordered or CSE'd across a :class:`Store` / :class:`Call`
    (memory-barrier discipline in Stage 3b).
    """

    VALUE_FIELDS: ClassVar[tuple[str, ...]] = ("address",)

    address: Value
    destination: str
    signed: bool
    width: int


@dataclass(frozen=True, kw_only=True, slots=True)
class LoopBoundary(_NoValueFields):
    """Push or pop loop label context for the emission layer.

    Emitted around loop bodies so that ``Continue`` / ``Break`` nodes
    inside ``Block``-wrapped AST statements (e.g. a Switch inside a
    while loop) can resolve to the correct jump targets.

    ``continue_label`` is ``None`` for switch lowerings: ``break``
    applies to the switch's end label but ``continue`` passes through
    to the enclosing loop (per C semantics).  In that case the
    push/pop affects only ``loop_end_labels``.
    """

    continue_label: str | None
    end_label: str
    push: bool


@dataclass(frozen=True, kw_only=True, slots=True)
class RepString:
    """``rep movs``/``rep stos`` over an element-wise loop region.

    Produced by :func:`cc.loops.recognize_string_loops` when a natural
    loop is a unit-stride fill or copy.  Side-effecting (a store); never
    eliminated by DCE.  ``count`` is the iteration count n; for a signed
    counter the emitter guards ``n <= 0`` before the ``rep``.  ``final_iv``
    materializes the induction variable's post-loop value when it is read
    after the loop.
    """

    VALUE_FIELDS: ClassVar[tuple[str, ...]] = ("count", "fill_value")

    operation: str  # "fill" | "copy"
    element_size: int  # 1 | 2 | 4
    dest: str  # base name (pointer / array)
    source: str | None  # base name for copy; None for fill
    count: Value  # iteration count n
    fill_value: Value | None  # fill value; None for copy
    counter_signed: bool
    final_iv: tuple[str, Value] | None


@dataclass(frozen=True, kw_only=True, slots=True)
class Return:
    """Function return, optionally with a value."""

    VALUE_FIELDS: ClassVar[tuple[str, ...]] = ("value",)

    value: Value | None


@dataclass(frozen=True, kw_only=True, slots=True)
class Store:
    """*(width) address = value — memory write at a resolved :class:`Address`.

    Side-effecting: never eliminated by DCE, and a memory barrier that
    :class:`Load` / :class:`AddressOf` must not be reordered or CSE'd
    across (Stage 3b's conservative treatment).
    """

    VALUE_FIELDS: ClassVar[tuple[str, ...]] = ("address", "value")

    address: Value
    value: Value
    width: int


@dataclass(kw_only=True, slots=True)
class Switch(_NoValueFields):
    """``switch (discriminant) { case ...: ... default: ... }`` statement.

    The discriminant is kept as an AST node so the codegen's
    type-dependent paths (char-typed labels, enum-typed exhaustiveness,
    pinned-register / memory-scalar hoist) work unchanged.
    ``original_ast`` preserves the source :class:`ast_nodes.Switch` so
    enum exhaustiveness can be checked against the right variant set.
    Each :class:`SwitchCase` body is a list of IR instructions; a
    ``break`` inside an arm lowers to :class:`Jump` to ``end_label``,
    which the codegen emits once at the end of the lowered switch.
    """

    cases: list[SwitchCase]
    discriminant: ast_nodes.Node
    end_label: str
    original_ast: ast_nodes.Switch


@dataclass(kw_only=True, slots=True)
class SwitchCase:
    """A single arm of an :class:`Switch` instruction.

    ``value`` is the resolved integer constant for a ``case`` arm, or
    ``None`` for the ``default`` arm.  ``body`` is the lowered IR
    instruction list for the arm — labels and gotos inside the body are
    visible to IR-level passes.  Mutable so optimizer passes can rewrite
    the body in place.
    """

    body: list[Instruction]
    value: int | None


@dataclass(frozen=True, kw_only=True, slots=True)
class TailCall:
    """name(args) in tail position — codegen lowers to ``jmp name`` (no ``ret``).

    Produced by :func:`cc.ir_optimize.optimize` when a :class:`Call`
    whose result is consumed only by an immediately-following
    :class:`Return` is rewritten as a single control-flow terminator.
    The codegen falls back to a normal call / return sequence when the
    call site fails the runtime eligibility check (e.g. stack args,
    pinned saves required, callee is a builtin).
    """

    VALUE_FIELDS: ClassVar[tuple[str, ...]] = ("args",)

    args: tuple[Value, ...]
    name: str


Instruction = (
    Access
    | Address
    | AddressOf
    | BinaryOperation
    | Block
    | BranchFalse
    | Call
    | CarryBranch
    | Copy
    | Index
    | IndexAssign
    | InlineAsm
    | Jump
    | Label
    | Load
    | LoopBoundary
    | RepString
    | Return
    | Store
    | Switch
    | TailCall
)


@dataclass(kw_only=True, slots=True)
class Function:
    """IR form of a single function; ``ast_node`` is kept for frame setup."""

    ast_node: ast_nodes.Function
    body: list[Instruction]
    strings: list[tuple[str, str]] = field(default_factory=list)


@dataclass(kw_only=True, slots=True)
class Program:
    """IR for an entire translation unit."""

    functions: list[Function]
    globals: list[ast_nodes.Node]


class Builder:
    """Convert an AST :class:`cc.ast_nodes.Program` to an :class:`Program`.

    Each function body is flattened into a linear list of
    :data:`Instruction` instructions.  Nested expressions are broken
    into sequences of temporaries (``_ir_0``, ``_ir_1``, …) so every
    instruction has at most one operator and simple operands.  Control
    flow (``if`` / ``while`` / ``do``-``while``) is linearised into
    :class:`Label` / :class:`Jump` / :class:`BranchFalse` instructions.
    Complex forms that the lowering cannot easily handle fall back to
    :class:`Block` so the existing AST-based codegen path handles them
    unchanged.
    """

    def __init__(self, *, carry_return_functions: frozenset[str] = frozenset()) -> None:
        """Initialize counters and record which callees use ``carry_return``.

        ``carry_return_functions`` is the set of function names declared
        with ``__attribute__((carry_return))``.  Conditions of the shape
        ``call(...) != 0`` / ``call(...) == 0`` where the callee is in
        this set lower to :class:`CarryBranch` instead of going through
        a value temp, preserving the CF-based return-value convention.
        """
        self._counter = 0
        self._str_counter = 0
        self._carry_return_functions = carry_return_functions

    def build_program(self, program: ast_nodes.Program, /) -> Program:
        """Lower every function in *program* to IR."""
        # Build a table of struct_name → frozenset of bitfield field names so
        # _lower_assign_expr can reject bitfield assignment-as-expression.
        self._struct_bitfield_names: dict[str, frozenset[str]] = {}
        for node in program.globals:
            if isinstance(node, ast_nodes.StructDecl):
                bitfield_names = frozenset(
                    field.field_name
                    for field in node.fields
                    if isinstance(field, ast_nodes.StructField) and field.bit_width is not None and field.field_name is not None
                )
                if bitfield_names:
                    self._struct_bitfield_names[node.name] = bitfield_names
        return Program(functions=[self._build_function(function) for function in program.functions], globals=program.globals)

    @staticmethod
    def _assign_rhs_field_name(node: ast_nodes.Node, /) -> str:
        """Return the attribute name that holds the RHS expression for any *Assign node.

        Every ``*Assign`` dataclass in ``cc/ast_nodes.py`` exposes the RHS
        as ``expr``, except :class:`~ast_nodes.PlaceStore` which uses
        ``value``.
        """
        if isinstance(node, ast_nodes.PlaceStore):
            return "value"
        return "expr"

    def _build_cond_false(
        self,
        *,
        cond: ast_nodes.Node,
        out: list[Instruction],
        strings: list[tuple[str, str]],
        target: str,
    ) -> None:
        """Emit IR that jumps to *target* when *cond* evaluates to false."""
        match cond:
            case ast_nodes.LogicalAnd(left=left, right=right):
                self._build_cond_false(cond=left, out=out, strings=strings, target=target)
                self._build_cond_false(cond=right, out=out, strings=strings, target=target)
            case ast_nodes.LogicalOr(left=left, right=right):
                skip_lbl = self._label("lor")
                self._build_cond_true(cond=left, out=out, strings=strings, target=skip_lbl)
                self._build_cond_false(cond=right, out=out, strings=strings, target=target)
                out.append(Label(name=skip_lbl))
            case ast_nodes.BinaryOperation(operation=operation, left=left, right=right) if (
                operation in ("!=", "==") and self._is_carry_return_call(left) and right == ast_nodes.Int(value=0)
            ):
                # ``if (carry_return_call() != 0)`` / ``... == 0`` — jump to
                # *target* when the condition is false, i.e. jump on CF set
                # for ``!=`` (false means the call returned 0) and on CF
                # clear for ``==``.
                when = "set" if operation == "!=" else "clear"
                out.append(CarryBranch(call_ast=left, target=target, when=when))
            case ast_nodes.BinaryOperation(operation=operation, left=left, right=right) if operation in COMPARISON_OPERATIONS:
                left_value = self._build_expr(expr=left, out=out, strings=strings)
                right_value = self._build_expr(expr=right, out=out, strings=strings)
                out.append(BranchFalse(left=left_value, operation=operation, right=right_value, target=target))
            case _:
                # General case: evaluate to a temp, test non-zero.
                value = self._build_expr(expr=cond, out=out, strings=strings)
                out.append(BranchFalse(left=value, operation="!=", right=0, target=target))

    def _build_cond_true(
        self,
        *,
        cond: ast_nodes.Node,
        out: list[Instruction],
        strings: list[tuple[str, str]],
        target: str,
    ) -> None:
        """Emit IR that jumps to *target* when *cond* evaluates to true."""
        match cond:
            case ast_nodes.LogicalOr(left=left, right=right):
                self._build_cond_true(cond=left, out=out, strings=strings, target=target)
                self._build_cond_true(cond=right, out=out, strings=strings, target=target)
            case ast_nodes.LogicalAnd(left=left, right=right):
                skip_lbl = self._label("land")
                self._build_cond_false(cond=left, out=out, strings=strings, target=skip_lbl)
                self._build_cond_true(cond=right, out=out, strings=strings, target=target)
                out.append(Label(name=skip_lbl))
            case ast_nodes.BinaryOperation(operation=operation, left=left, right=right) if (
                operation in ("!=", "==") and self._is_carry_return_call(left) and right == ast_nodes.Int(value=0)
            ):
                # Dual of the false-jump shortcut in ``_build_cond_false``:
                # jump on CF clear for ``!=`` (true means the call returned 1),
                # on CF set for ``==``.
                when = "clear" if operation == "!=" else "set"
                out.append(CarryBranch(call_ast=left, target=target, when=when))
            case ast_nodes.BinaryOperation(operation=operation, left=left, right=right) if operation in COMPARISON_OPERATIONS:
                left_value = self._build_expr(expr=left, out=out, strings=strings)
                right_value = self._build_expr(expr=right, out=out, strings=strings)
                # Invert the condition: true-jump means false-branch doesn't fire.
                inverted = INVERT_COMPARISON[operation]
                out.append(BranchFalse(left=left_value, operation=inverted, right=right_value, target=target))
            case _:
                value = self._build_expr(expr=cond, out=out, strings=strings)
                out.append(BranchFalse(left=value, operation="==", right=0, target=target))

    def _build_do_while(
        self,
        *,
        body: list[ast_nodes.Node],
        cond: ast_nodes.Node,
        out: list[Instruction],
        strings: list[tuple[str, str]],
    ) -> None:
        loop_lbl = self._label("dloop")
        cond_lbl = self._label("dcond")
        end_lbl = self._label("dend")
        out.extend([
            Label(name=loop_lbl),
            LoopBoundary(continue_label=cond_lbl, end_label=end_lbl, push=True),
        ])
        self._build_stmts(stmts=body, out=out, break_tgt=end_lbl, cont_tgt=cond_lbl, strings=strings)
        out.extend([
            LoopBoundary(continue_label=cond_lbl, end_label=end_lbl, push=False),
            Label(name=cond_lbl),
        ])
        self._build_cond_true(cond=cond, out=out, strings=strings, target=loop_lbl)
        out.append(Label(name=end_lbl))

    def _build_expr(
        self,
        *,
        expr: ast_nodes.Node,
        out: list[Instruction],
        strings: list[tuple[str, str]],
    ) -> Value:
        match expr:
            case ast_nodes.Int(value=integer_value):
                return integer_value
            case ast_nodes.Var(name=variable_name):
                return variable_name
            case ast_nodes.String(content=content):
                label = f"_ir_s{self._str_counter}"
                self._str_counter += 1
                strings.append((label, content))
                return label
            case ast_nodes.BinaryOperation(operation=operation, left=left, right=right):
                left_value = self._build_expr(expr=left, out=out, strings=strings)
                right_value = self._build_expr(expr=right, out=out, strings=strings)
                temp = self._tmp()
                out.append(BinaryOperation(destination=temp, left=left_value, operation=operation, right=right_value))
                return temp
            case ast_nodes.Call(name=name) if name in self._carry_return_functions:
                # ``carry_return`` callees report their result via CF,
                # not AX.  The IR flow would store (garbage) AX to a
                # temp; delegate to the AST codegen (which knows how
                # to synthesise ``0``/``1`` from CF when the call's
                # return value is actually needed).
                temp = self._tmp()
                out.append(Block(node=ast_nodes.Assign(expr=expr, name=temp)))
                return temp
            case ast_nodes.Call(name=name, args=args):
                arg_values = tuple(self._build_expr(expr=a, out=out, strings=strings) for a in args)
                temp = self._tmp()
                out.append(Call(args=arg_values, destination=temp, name=name))
                return temp
            case ast_nodes.Index(array=ast_nodes.Var(name=base), index=index_node):
                index_value = self._build_expr(expr=index_node, out=out, strings=strings)
                temp = self._tmp()
                out.append(Index(base=base, destination=temp, index=index_value))
                return temp
            case ast_nodes.LogicalOr() | ast_nodes.LogicalAnd():
                # Short-circuit boolean: lower to conditional set (0 or 1).
                temp = self._tmp()
                true_lbl = self._label("btrue")
                end_lbl = self._label("bend")
                self._build_cond_true(cond=expr, out=out, strings=strings, target=true_lbl)
                out.extend([
                    Copy(destination=temp, source=0),
                    Jump(target=end_lbl),
                    Label(name=true_lbl),
                    Copy(destination=temp, source=1),
                    Label(name=end_lbl),
                ])
                return temp
            case ast_nodes.PlaceAddressOf(place=ast_nodes.VariablePlace()):
                # Pass ``&name`` through as-is so generate_call can detect
                # out_register arguments without the node being replaced by a
                # temp.  Member / dereference ``PlaceAddressOf`` shapes fall to
                # the default temp+Block, exactly as they did before the fold
                # (except the arrow-member shape lowered just below).
                return expr
            case ast_nodes.PlaceAddressOf(place=place) if _is_arrow_member_address_of(expr):
                # ``&pointer->member`` address-of on a plain pointer variable
                # (Stage 3b.1 — the lea-terminal twin of the slice 2 arrow
                # load).  One chain-break at the dereference: ``base_value`` is
                # the pointer var ``Value`` (the name ``"p"``), which emission
                # re-materializes exactly like the legacy address-of path seeds
                # its base, so no separate pointer ``Load`` op is needed.  The
                # member ``shape`` (still rooted at the dereference) drives the
                # static field offset at emission via the same helpers the
                # legacy ``_emit_place_address_of`` used; :class:`AddressOf` is a
                # pure ``lea`` terminal — no width, no signedness.
                base_value = place.base.pointer.name
                address_temp = self._tmp()
                result_temp = self._tmp()
                out.extend([
                    Address(base_value=base_value, destination=address_temp, indices=(), shape=place),
                    AddressOf(address=address_temp, destination=result_temp),
                ])
                return result_temp
            case ast_nodes.AssignExpr(inner=inner):
                return self._lower_assign_expr(inner=inner, out=out, strings=strings)
            case ast_nodes.PlaceLoad(place=place) if _is_arrow_member_member_load(expr):
                # ``pointer->outer.inner`` read — member-of-member-of-deref on a
                # plain pointer variable (Stage 3b.1 slice 6).  One chain-break
                # at the dereference: the segment's ``base_value`` is the pointer
                # var ``Value`` (the name ``"p"``), exactly as the single-level
                # arrow load (slice 2).  The two static member offsets ride the
                # immutable ``shape`` and accumulate recursively in
                # ``_resolve_member_place_info`` / ``resolve_address`` at
                # emission — no new dynamic leaf, so this is just a longer static
                # member shape over the proven deref chain-break.  ``width`` /
                # ``signed`` carry emission-ignored placeholders.
                base_value = place.base.base.pointer.name
                address_temp = self._tmp()
                result_temp = self._tmp()
                out.extend([
                    Address(base_value=base_value, destination=address_temp, indices=(), shape=place),
                    Load(address=address_temp, destination=result_temp, signed=False, width=0),
                ])
                return result_temp
            case ast_nodes.PlaceLoad(place=place) if _is_arrow_member_load(expr):
                # ``pointer->member`` read on a plain pointer variable
                # (Stage 3b.1 slice 2).  The dereference breaks the chain: the
                # segment's ``base_value`` is the pointer var ``Value`` (the
                # name ``"p"``), which emission re-materializes via
                # ``_ir_value_to_ast`` exactly like the legacy arrow-load path
                # seeds its base, so no separate pointer ``Load`` op is needed.
                # The member ``shape`` (still rooted at the dereference) drives
                # the static field layout at emission via the same helpers the
                # ``ir.Access`` arrow-load path used; ``width`` / ``signed``
                # carry emission-ignored placeholders (the IR builder has no
                # struct layout).
                base_value = place.base.pointer.name
                address_temp = self._tmp()
                result_temp = self._tmp()
                out.extend([
                    Address(base_value=base_value, destination=address_temp, indices=(), shape=place),
                    Load(address=address_temp, destination=result_temp, signed=False, width=0),
                ])
                return result_temp
            case ast_nodes.PlaceLoad(place=place) if _is_static_member_load(expr):
                # ``variable.member`` read — the simplest static ir.Access load
                # (Stage 3b.1 slice 1).  Resolve the deref-free member ``place``
                # to an address value, then load through it.  ``width`` /
                # ``signed`` are derived at emission from ``Address.shape`` via
                # the existing field-layout helpers (the IR builder has no
                # struct layout), so they carry emission-ignored placeholders.
                address_temp = self._tmp()
                result_temp = self._tmp()
                out.extend([
                    Address(base_value=None, destination=address_temp, indices=(), shape=place),
                    Load(address=address_temp, destination=result_temp, signed=False, width=0),
                ])
                return result_temp
            case ast_nodes.PlaceLoad(place=place) if _is_struct_array_member_load(expr):
                # ``array[index].member`` read — the first DYNAMIC-index shape
                # to ride the uniform ops (Stage 3b.1 slice 4).  The single
                # subscript ``index`` is the segment's only dynamic leaf: it is
                # pre-lowered to a :data:`Value` here and carried on
                # ``Address.indices``, proving the design's central mechanic.  For
                # a simple-var / constant index ``_build_expr`` emits NO
                # preceding instruction, so emission's ``_ir_value_to_ast``
                # round-trip reconstructs the exact AST index node
                # ``resolve_address`` walked inline today — byte-neutral.  A
                # compound index (``a[i + 1].f``) pre-lowers to a temp that
                # PR #587's register allocator keeps register-resident; the byte
                # gate is the backstop.  The member ``shape`` (rooted at the
                # SubscriptPlace) still drives the static field offset / element
                # stride / width at emission via the same helpers the
                # ``ir.Access`` struct-array load used; ``width`` / ``signed``
                # carry emission-ignored placeholders (the IR builder has no
                # struct layout).
                index_value = self._build_expr(expr=place.base.index, out=out, strings=strings)
                address_temp = self._tmp()
                result_temp = self._tmp()
                out.extend([
                    Address(base_value=None, destination=address_temp, indices=(index_value,), shape=place),
                    Load(address=address_temp, destination=result_temp, signed=False, width=0),
                ])
                return result_temp
            case _ if _is_migrated_access(expr):
                temp = self._tmp()
                out.append(Access(node=ast_nodes.Assign(expr=expr, name=temp)))
                return temp
            case _:
                # Complex: use a temp + Block to let AST codegen handle it.
                temp = self._tmp()
                out.append(Block(node=ast_nodes.Assign(expr=expr, name=temp)))
                return temp

    def _build_for(
        self,
        *,
        body: list[ast_nodes.Node],
        cond: ast_nodes.Node | None,
        init: list[ast_nodes.Node],
        out: list[Instruction],
        step: list[ast_nodes.Node],
        strings: list[tuple[str, str]],
    ) -> None:
        self._build_stmts(stmts=init, out=out, break_tgt=None, cont_tgt=None, strings=strings)
        loop_lbl = self._label("floop")
        step_lbl = self._label("fstep")
        end_lbl = self._label("fend")
        out.append(Label(name=loop_lbl))
        if cond is not None and not _is_constant_true(cond):
            self._build_cond_false(cond=cond, out=out, strings=strings, target=end_lbl)
        out.append(LoopBoundary(continue_label=step_lbl, end_label=end_lbl, push=True))
        self._build_stmts(stmts=body, out=out, break_tgt=end_lbl, cont_tgt=step_lbl, strings=strings)
        out.extend([
            LoopBoundary(continue_label=step_lbl, end_label=end_lbl, push=False),
            Label(name=step_lbl),
        ])
        for step_expr in step:
            self._build_expr(expr=step_expr, out=out, strings=strings)
        out.extend([Jump(target=loop_lbl), Label(name=end_lbl)])

    def _build_function(self, function: ast_nodes.Function, /) -> Function:
        out: list[Instruction] = []
        strings: list[tuple[str, str]] = []
        # Pre-scan parameter and VarDecl types so the IR builder can
        # detect expressions whose value type doesn't fit a plain ``int``
        # temp (notably ``unsigned long``-pointee Index loads on the
        # 16-bit target) and delegate them to the AST codegen via
        # :class:`Block`.  Without this, an ``unsigned long *p; x =
        # p[0]`` lowers to ``temp = p[0]; x = temp`` where ``temp`` is
        # an int-typed slot — the long-store path then rejects ``Var
        # temp`` with ``expected 'unsigned long' expression, got 'int'``.
        self._var_types: dict[str, str] = {}
        for parameter in function.params:
            self._var_types[parameter.name] = parameter.type
        self._collect_local_types(function.body)
        # Per-function user-label bookkeeping: definitions collected as
        # ``Label`` nodes are lowered; references collected as ``Goto``
        # nodes are lowered.  Validated after the body completes so
        # forward references resolve naturally.
        self._user_labels_defined: dict[str, int] = {}
        self._user_labels_referenced: dict[str, int] = {}
        self._build_stmts(break_tgt=None, cont_tgt=None, out=out, stmts=function.body, strings=strings)
        for name, line in self._user_labels_referenced.items():
            if name not in self._user_labels_defined:
                message = f"goto target '{name}' has no matching label in function '{function.name}'"
                raise CompileError(message, line=line)
        return Function(ast_node=function, body=out, strings=strings)

    def _build_if(
        self,
        *,
        body: list[ast_nodes.Node],
        break_tgt: str | None,
        cond: ast_nodes.Node,
        cont_tgt: str | None,
        else_body: list[ast_nodes.Node] | None,
        out: list[Instruction],
        strings: list[tuple[str, str]],
    ) -> None:
        if else_body is not None:
            else_lbl = self._label("else")
            end_lbl = self._label("endif")
            self._build_cond_false(cond=cond, out=out, strings=strings, target=else_lbl)
            self._build_stmts(stmts=body, out=out, break_tgt=break_tgt, cont_tgt=cont_tgt, strings=strings)
            out.extend([Jump(target=end_lbl), Label(name=else_lbl)])
            self._build_stmts(stmts=else_body, out=out, break_tgt=break_tgt, cont_tgt=cont_tgt, strings=strings)
            out.append(Label(name=end_lbl))
        else:
            end_lbl = self._label("endif")
            self._build_cond_false(cond=cond, out=out, strings=strings, target=end_lbl)
            self._build_stmts(stmts=body, out=out, break_tgt=break_tgt, cont_tgt=cont_tgt, strings=strings)
            out.append(Label(name=end_lbl))

    def _build_stmt(
        self,
        *,
        break_tgt: str | None,
        cont_tgt: str | None,
        out: list[Instruction],
        stmt: ast_nodes.Node,
        strings: list[tuple[str, str]],
    ) -> None:
        match stmt:
            case ast_nodes.VarDecl():
                # Preserve full VarDecl semantics (constant aliases, visibility
                # registration, byte-type tracking) via the existing AST path.
                out.append(Block(node=stmt))
            case ast_nodes.ArrayDecl():
                # Array initializers are complex; delegate to existing codegen.
                out.append(Block(node=stmt))
            case ast_nodes.Assign(name=name, expr=expr):
                # ``unsigned long`` destinations or long-pointee Index
                # right-hand sides can't round-trip through an int-typed
                # IR temp — let the AST codegen lower the whole
                # assignment in one shot so the DX:AX (16-bit) / EAX
                # (32-bit) shape is preserved end-to-end.  ``dest = cond
                # ? dest : other`` / ``dest = cond ? other : dest``
                # (the MIN / MAX guarded-update shape) is also routed
                # to the AST path so ``_try_emit_guarded_update``'s
                # tight ``cmp / Jcc / mov dest, other`` lowering fires;
                # the IR-temp path would round-trip through AX and
                # break the fast-path test.
                if (
                    self._var_types.get(name) == "unsigned long"
                    or self._is_long_pointee_index(expr)
                    or self._is_guarded_update(expression=expr, name=name)
                ):
                    out.append(Block(node=stmt))
                elif self._is_multi_binop_constant_chain(expr):
                    # Constant-foldable shapes (``a | b | c`` of named
                    # constants / enum values) lose their fold when broken
                    # into per-binop IR temps — the codegen's
                    # ``_constant_expression`` walks the AST RHS once and
                    # emits a single ``mov reg, (A|B|C)``; the per-temp
                    # IR form forces a runtime push/pop chain.  Delegate
                    # multi-binop chains (single binops still benefit from
                    # the IR path's pinned-register fast paths) to the
                    # AST codegen to preserve the fold.
                    out.append(Block(node=stmt))
                elif self._is_inplace_self_modify(expression=expr, name=name):
                    # ``x = x op K`` — the AST ``emit_store_local``
                    # recognises this self-mod pattern and emits ``inc
                    # reg`` / ``add reg, imm`` / ``or reg, imm`` etc.
                    # The IR-temp lowering would round-trip through
                    # ``tmp = x op K; x = tmp`` (3 instructions instead
                    # of 1), so delegate to the AST codegen.
                    out.append(Block(node=stmt))
                else:
                    source = self._build_expr(expr=expr, out=out, strings=strings)
                    out.append(Copy(destination=name, source=source))
            case ast_nodes.IndexAssign(array=ast_nodes.Var(name=base), index=index_node, expr=expr):
                index_value = self._build_expr(expr=index_node, out=out, strings=strings)
                source = self._build_expr(expr=expr, out=out, strings=strings)
                out.append(IndexAssign(base=base, index=index_value, source=source))
            case ast_nodes.Call(name="asm"):
                # asm() requires raw String args; pass through as-is.
                out.append(Block(node=stmt))
            case ast_nodes.Call() as call:
                args = tuple(self._build_expr(expr=a, out=out, strings=strings) for a in call.args)
                out.append(Call(args=args, destination=None, name=call.name))
            case ast_nodes.If(cond=cond, body=body, else_body=else_body):
                self._build_if(body=body, break_tgt=break_tgt, cond=cond, cont_tgt=cont_tgt, else_body=else_body, out=out, strings=strings)
            case ast_nodes.While(cond=cond, body=body):
                self._build_while(body=body, cond=cond, out=out, strings=strings)
            case ast_nodes.DoWhile(cond=cond, body=body):
                self._build_do_while(body=body, cond=cond, out=out, strings=strings)
            case ast_nodes.For(init=init, cond=cond, step=step, body=body):
                self._build_for(body=body, cond=cond, init=init, out=out, step=step, strings=strings)
            case ast_nodes.Break():
                assert break_tgt is not None, "break outside loop"
                out.append(Jump(target=break_tgt))
            case ast_nodes.Compound(body=body):
                self._build_stmts(stmts=body, out=out, break_tgt=break_tgt, cont_tgt=cont_tgt, strings=strings)
            case ast_nodes.Continue():
                assert cont_tgt is not None, "continue outside loop"
                out.append(Jump(target=cont_tgt))
            case ast_nodes.Goto(name=name):
                self._user_labels_referenced.setdefault(name, stmt.line)
                out.append(Jump(target=f".user_{name}"))
            case ast_nodes.Label(name=name):
                if name in self._user_labels_defined:
                    message = f"duplicate label '{name}'"
                    raise CompileError(message, line=stmt.line)
                self._user_labels_defined[name] = stmt.line
                out.append(Label(name=f".user_{name}"))
            case ast_nodes.Return(value=value):
                # A long-pointee Index in a return position must be
                # produced in DX:AX (16-bit) / EAX (32-bit); the int-typed
                # IR temp would truncate it.  Delegate the whole return
                # to the AST codegen.
                if value is not None and self._is_long_pointee_index(value):
                    out.append(Block(node=stmt))
                else:
                    v = self._build_expr(expr=value, out=out, strings=strings) if value is not None else None
                    out.append(Return(value=v))
            case ast_nodes.InlineAsm(content=content):
                out.append(InlineAsm(content=content))
            case ast_nodes.Switch(cases=cases, discriminant=discriminant):
                self._build_switch(
                    cases=cases,
                    cont_tgt=cont_tgt,
                    discriminant=discriminant,
                    original_ast=stmt,
                    out=out,
                    strings=strings,
                )
            case ast_nodes.PlaceStore(place=place, value=value) if _is_arrow_member_member_store(stmt):
                # ``pointer->outer.inner = leaf`` write — the multi-level store
                # twin of the slice 6 load.  One chain-break at the plain-pointer
                # dereference: ``base_value`` is the pointer var ``Value`` (the
                # name ``"p"``); the two static member offsets ride the ``shape``
                # and accumulate at emission.  The byte-safe leaf RHS emits no
                # preceding instruction, so the RHS-vs-address ordering matches
                # the legacy store byte-for-byte (``directory->entry.d_ino = ...``
                # in ``dirent.c``).  ``width`` carries an emission-ignored
                # placeholder (the field width is derived from the ``shape``).
                store_value = self._build_expr(expr=value, out=out, strings=strings)
                address_temp = self._tmp()
                out.extend([
                    Address(base_value=place.base.base.pointer.name, destination=address_temp, indices=(), shape=place),
                    Store(address=address_temp, value=store_value, width=0),
                ])
            case ast_nodes.PlaceStore(place=place, value=value) if _is_arrow_member_store(stmt):
                # ``pointer->member = leaf`` write on a plain pointer variable
                # (Stage 3b.1 slice 3).  The dereference breaks the chain: the
                # segment's ``base_value`` is the pointer var ``Value`` (the
                # name ``"p"``), mirroring the arrow LOAD slice.  The RHS is a
                # byte-safe leaf, so ``_build_expr`` emits NO preceding
                # instruction and returns the leaf ``Value`` directly; emission
                # reconstructs the original RHS node via ``_ir_value_to_ast``
                # and drives the EXISTING member-store path, which orders the
                # RHS evaluation versus the base materialization exactly as the
                # legacy arrow store did.  The member ``shape`` drives the
                # static field layout (offset / width / bitfield) at emission;
                # ``width`` carries an emission-ignored placeholder (the IR
                # builder has no struct layout).
                store_value = self._build_expr(expr=value, out=out, strings=strings)
                address_temp = self._tmp()
                out.extend([
                    Address(base_value=place.base.pointer.name, destination=address_temp, indices=(), shape=place),
                    Store(address=address_temp, value=store_value, width=0),
                ])
            case ast_nodes.PlaceStore(place=place, value=value) if _is_static_member_store(stmt):
                # ``variable.member = leaf`` write — the static store twin of
                # the slice 1 dot-member load (Stage 3b.1 slice 3).  Resolve the
                # deref-free member ``place`` to a (static) address value, then
                # store the byte-safe leaf RHS through it.  ``_build_expr`` emits
                # no preceding instruction for the leaf, so the RHS-vs-address
                # ordering the legacy dot store used is preserved byte-for-byte;
                # ``width`` is derived at emission from ``Address.shape`` and
                # carries an emission-ignored placeholder here.
                store_value = self._build_expr(expr=value, out=out, strings=strings)
                address_temp = self._tmp()
                out.extend([
                    Address(base_value=None, destination=address_temp, indices=(), shape=place),
                    Store(address=address_temp, value=store_value, width=0),
                ])
            case ast_nodes.PlaceStore(place=place, value=value) if _is_deref_store(stmt):
                # ``*pointer = leaf`` write on a plain pointer variable
                # (Stage 3b.1 slice 5).  The dereference IS the whole place: the
                # segment's ``base_value`` is the pointer var ``Value`` (the
                # name ``"p"``), with no static member offset and no index.  The
                # RHS is a byte-safe leaf, so ``_build_expr`` emits NO preceding
                # instruction and emission reconstructs it via ``_ir_value_to_ast``
                # and drives the EXISTING ``_emit_place_store`` deref path, which
                # orders the RHS evaluation versus the base materialization
                # exactly as the legacy deref store did.  ``width`` carries an
                # emission-ignored placeholder (the pointee width is derived at
                # emission from the ``DereferencePlace`` ``shape``).
                store_value = self._build_expr(expr=value, out=out, strings=strings)
                address_temp = self._tmp()
                out.extend([
                    Address(base_value=place.pointer.name, destination=address_temp, indices=(), shape=place),
                    Store(address=address_temp, value=store_value, width=0),
                ])
            case ast_nodes.PlaceStore(place=place, value=value) if _is_struct_array_member_store(stmt):
                # ``array[index].member = leaf`` write — the dynamic-index store
                # twin of slice 4's struct-array load (Stage 3b.1 slice 5).  The
                # single subscript ``index`` is the segment's only dynamic leaf:
                # pre-lowered to a :data:`Value` here and carried on
                # ``Address.indices``, re-seated into the ``shape`` at emission by
                # ``_ir_address_with_index``.  The byte-safe leaf RHS emits no
                # preceding instruction, so the RHS-vs-address ordering matches
                # the legacy store byte-for-byte; the member ``shape`` (rooted at
                # the SubscriptPlace) drives the static field offset / element
                # stride / width at emission via the unchanged helpers.
                index_value = self._build_expr(expr=place.base.index, out=out, strings=strings)
                store_value = self._build_expr(expr=value, out=out, strings=strings)
                address_temp = self._tmp()
                out.extend([
                    Address(base_value=None, destination=address_temp, indices=(index_value,), shape=place),
                    Store(address=address_temp, value=store_value, width=0),
                ])
            case _ if _is_migrated_access(stmt):
                out.append(Access(node=stmt))
            case _:
                out.append(Block(node=stmt))

    def _build_stmts(
        self,
        *,
        break_tgt: str | None,
        cont_tgt: str | None,
        out: list[Instruction],
        stmts: list[ast_nodes.Node],
        strings: list[tuple[str, str]],
    ) -> None:
        for s in stmts:
            self._build_stmt(break_tgt=break_tgt, cont_tgt=cont_tgt, out=out, stmt=s, strings=strings)

    def _build_switch(
        self,
        *,
        cases: list[ast_nodes.SwitchCase],
        cont_tgt: str | None,
        discriminant: ast_nodes.Node,
        original_ast: ast_nodes.Switch,
        out: list[Instruction],
        strings: list[tuple[str, str]],
    ) -> None:
        # ``break`` inside a case body exits the switch via this label,
        # mirroring AST codegen's loop_end_labels push.  ``continue``
        # inherits the enclosing loop's continue label (or remains None
        # outside a loop) — the AST path does the same by not pushing
        # to loop_continue_labels in generate_switch.
        end_lbl = self._label("swend")
        ir_cases: list[SwitchCase] = []
        for case in cases:
            case_body: list[Instruction] = []
            self._build_stmts(stmts=case.body, out=case_body, break_tgt=end_lbl, cont_tgt=cont_tgt, strings=strings)
            ir_cases.append(SwitchCase(body=case_body, value=case.value))
        out.append(Switch(cases=ir_cases, discriminant=discriminant, end_label=end_lbl, original_ast=original_ast))

    def _build_while(
        self,
        *,
        body: list[ast_nodes.Node],
        cond: ast_nodes.Node,
        out: list[Instruction],
        strings: list[tuple[str, str]],
    ) -> None:
        loop_lbl = self._label("wloop")
        end_lbl = self._label("wend")
        out.append(Label(name=loop_lbl))
        # ``while (1)`` (and other statically-nonzero conditions) skip
        # the condition check entirely.  ``parse_condition`` wraps the
        # bare ``1`` as ``BinaryOperation("!=", Int(1), Int(0))``, so
        # both shapes have to be recognised.
        if not _is_constant_true(cond):
            self._build_cond_false(cond=cond, out=out, strings=strings, target=end_lbl)
        out.append(LoopBoundary(continue_label=loop_lbl, end_label=end_lbl, push=True))
        self._build_stmts(stmts=body, out=out, break_tgt=end_lbl, cont_tgt=loop_lbl, strings=strings)
        out.append(LoopBoundary(continue_label=loop_lbl, end_label=end_lbl, push=False))
        out.extend([Jump(target=loop_lbl), Label(name=end_lbl)])

    def _collect_local_types(self, stmts: list[ast_nodes.Node], /) -> None:
        """Walk *stmts* and record every ``VarDecl`` name → type binding.

        Nested blocks (``if`` / ``while`` / ``do``-``while``) are
        recursed into so a long-typed local declared inside a branch
        is still recognised when the IR builder lowers an Index in the
        same branch.
        """
        for statement in stmts:
            if isinstance(statement, ast_nodes.VarDecl):
                self._var_types[statement.name] = statement.type_name
            elif isinstance(statement, ast_nodes.If):
                self._collect_local_types(statement.body)
                if statement.else_body is not None:
                    self._collect_local_types(statement.else_body)
            elif isinstance(statement, (ast_nodes.Compound, ast_nodes.DoWhile, ast_nodes.While)):
                self._collect_local_types(statement.body)
            elif isinstance(statement, ast_nodes.Switch):
                for case in statement.cases:
                    self._collect_local_types(case.body)

    def _is_carry_return_call(self, node: ast_nodes.Node, /) -> bool:
        """Return True if *node* is a :class:`ast_nodes.Call` to a carry_return function."""
        return isinstance(node, ast_nodes.Call) and node.name in self._carry_return_functions

    @staticmethod
    def _is_guarded_update(*, expression: ast_nodes.Node, name: str) -> bool:
        """Return True if *expression* is ``cond ? <name> : other`` or ``cond ? other : <name>``.

        That's the shape ``MIN(name, other)`` / ``MAX(name, other)`` produce —
        the AST-codegen fast path ``_try_emit_guarded_update`` lowers it to a
        tight ``cmp / Jcc / mov dest, other`` that bypasses an AX round-trip.
        Routing such assignments through the IR-temp path would defeat the
        fast path, so they stay on the AST path via :class:`Block`.
        """
        if not isinstance(expression, ast_nodes.Conditional):
            return False
        then_is_self = isinstance(expression.then_expr, ast_nodes.Var) and expression.then_expr.name == name
        else_is_self = isinstance(expression.else_expr, ast_nodes.Var) and expression.else_expr.name == name
        return then_is_self or else_is_self

    @staticmethod
    def _is_inplace_self_modify(*, expression: ast_nodes.Node, name: str) -> bool:
        """Return True if *expression* is ``Var(name) op X`` for an in-place-eligible op.

        Recognises the ``x = x op K`` self-modification shape that the
        AST ``emit_store_local`` / ``_generate_binary_operation_expression``
        lower to ``inc reg`` / ``add reg, imm`` / ``or reg, imm`` etc.
        Going through the IR's per-binop temp lowering rebuilds the
        same value via ``tmp = x op K; x = tmp``, which expands to 3
        instructions instead of 1.  The check is intentionally
        limited to ``Var(name)`` on the left and any operand on the
        right — the fast paths fire whenever the destination operand
        appears once on the RHS.
        """
        if not isinstance(expression, ast_nodes.BinaryOperation):
            return False
        if expression.operation not in ("+", "-", "&", "|", "^", "<<", ">>", "*"):
            return False
        return isinstance(expression.left, ast_nodes.Var) and expression.left.name == name

    def _is_long_pointee_index(self, expression: ast_nodes.Node, /) -> bool:
        """Return True if *expression* is ``base[i]`` whose pointee is a 4-byte unsigned int.

        Used by ``_build_stmt`` / ``_build_expr`` to short-circuit the
        normal ``temp = Index(...)`` lowering — those temps are int-typed
        slots and would silently truncate a 32-bit pointee to the
        target's native acc width.  Delegating to the AST codegen via
        :class:`Block` preserves the full DX:AX (16-bit) / EAX (32-bit)
        value.
        """
        if not isinstance(expression, ast_nodes.Index):
            return False
        if not isinstance(expression.array, ast_nodes.Var):
            return False
        base_type = self._var_types.get(expression.array.name)
        return base_type == "unsigned long*"

    @staticmethod
    def _is_multi_binop_constant_chain(expression: ast_nodes.Node, /) -> bool:
        """Return True for a multi-binop chain of named-constant leaves.

        Restricted to chains whose leaves are ``Var`` (potentially a
        ``NAMED_CONSTANT`` / enum value resolvable by the codegen's
        :meth:`_constant_expression`) and at least one of which is
        a ``BinaryOperation`` — i.e. ``A | B | C`` of capital-letter
        identifiers.  Chains involving ``Int`` literals or local
        variables are excluded so the IR's per-temp lowering can keep
        the pinned-register fast paths firing where the AST path
        would lose the fold and emit a push/pop scratch sequence.
        """
        if not isinstance(expression, ast_nodes.BinaryOperation):
            return False
        if expression.operation not in ("+", "-", "*", "&", "|", "^"):
            return False
        if not Builder._is_named_constant_chain(expression.left):
            return False
        if not Builder._is_named_constant_chain(expression.right):
            return False
        # Require at least one branch to be itself a BinaryOperation so the
        # chain has 2+ operators; a bare ``Var op Var`` doesn't qualify
        # (the IR path's fast paths beat the AST path's frame round-trip).
        return isinstance(expression.left, ast_nodes.BinaryOperation) or isinstance(expression.right, ast_nodes.BinaryOperation)

    @staticmethod
    def _is_named_constant_chain(expression: ast_nodes.Node, /) -> bool:
        """Return True if *expression* is a chain of likely-NAMED_CONSTANT leaves.

        The codegen's :meth:`_constant_expression` only resolves
        ``Var`` nodes when they refer to ``NAMED_CONSTANTS`` /
        ``enum_constants`` / ``constant_aliases``; a chain that mixes
        in non-constant ``Var`` names is rejected by the folder and
        falls back to a verbose push/pop scratch sequence on the AST
        path.  Restricting ``Var`` leaves to ALL_CAPS identifiers (the
        convention for those tables) keeps the Block-delegation
        conservative — local-variable ``Var`` chains stay on the IR
        path where their pinned-register fast paths still fire.
        """
        if isinstance(expression, ast_nodes.Int):
            return True
        if isinstance(expression, ast_nodes.Var):
            return expression.name == expression.name.upper()
        if isinstance(expression, ast_nodes.BinaryOperation) and expression.operation in ("+", "-", "*", "&", "|", "^"):
            return Builder._is_named_constant_chain(expression.left) and Builder._is_named_constant_chain(expression.right)
        return False

    def _label(self, tag: str = "l", /) -> str:
        name = f"._ir_{tag}{self._counter}"
        self._counter += 1
        return name

    def _lower_assign_expr(
        self,
        *,
        inner: ast_nodes.Node,
        out: list[Instruction],
        strings: list[tuple[str, str]],
    ) -> str:
        """Lower a parenthesized assignment to IR; return the temp holding its value.

        Strategy: evaluate the RHS into a temp, rewrite the wrapped
        ``*Assign`` so its RHS is ``Var(temp)``, emit the rewritten
        ``*Assign`` as a statement (reusing the existing per-lvalue store
        paths), and return the temp as the expression value.  This
        guarantees the original RHS expression is evaluated exactly once.
        """
        line = inner.line
        rhs_field = self._assign_rhs_field_name(inner)
        original_rhs = getattr(inner, rhs_field)
        # ``unsigned long`` lvalues don't round-trip cleanly through
        # int-typed temps.  Out-of-scope per the spec.
        if isinstance(inner, ast_nodes.Assign) and self._var_types.get(inner.name) == "unsigned long":
            message = "assignment-as-expression to 'unsigned long' is not supported"
            raise CompileError(message, line=line)
        rhs_value = self._build_expr(expr=original_rhs, out=out, strings=strings)
        temp = self._tmp()
        out.append(Copy(destination=temp, source=rhs_value))
        rebound = dataclasses.replace(inner, **{rhs_field: ast_nodes.Var(line=line, name=temp)})
        self._build_stmt(break_tgt=None, cont_tgt=None, out=out, stmt=rebound, strings=strings)
        return temp

    def _tmp(self) -> str:
        name = f"_ir_{self._counter}"
        self._counter += 1
        return name
