# cc.py `Place`: a recursive lvalue representation

## Summary

cc.py has no recursive notion of "a location in memory." The parser can only
recognize whole *flattened* access shapes, and codegen hand-writes the address
arithmetic inline for each shape. The result is that the AST node set is the
**cross product** of (place shapes) × (operations): every new access shape — or
every new operation on an existing shape — needs a brand-new node class plus its
own `generate_*` method. This design replaces that ~15-node zoo with a single
recursive `Place` (addressable-location) AST plus five operation nodes and one
recursive address-resolution routine, so chained accesses like `a[i][j].f[k]`
compose for free.

The staged implementation plan is
[`2026-06-01-cc-place-refactor-plan.md`](./2026-06-01-cc-place-refactor-plan.md).

## Background: the access-node cross product

The cross product is visible directly in the node list. Three orthogonal axes
have each been enumerated as concrete classes:

- **place shape:** `Var`, `Index`, `DoubleIndex`, `MemberAccess`, `MemberIndex`,
  `IndexMemberAccess`, `IndexMemberIndex`, plus the implicit deref of
  `PointerDereference`.
- **terminal operation:** read (rvalue), address-of, assign, `++`/`--`,
  call-through.
- **minor flags:** `arrow` vs `.`, postfix vs prefix.

Cross-producting place × operation is what produced these clusters, all of which
a recursive place node subsumes:

- **subscript family:** `Index`, `IndexAssign`, `IndexedCall`, `DoubleIndex`.
- **member family:** `MemberAccess`, `MemberAssign`, `MemberAddressOf`,
  `MemberIndex`, `MemberIndexAssign`, `MemberIncrementDecrement`.
- **subscript × member combos (pure combinatorial tax):** `IndexMemberAccess`,
  `IndexMemberAssign`, `IndexMemberIndex`, `IndexMemberIndexAssign` — all four
  vanish under the redesign.
- **deref family:** `PointerDereference`, `PointerDereferenceAssign`,
  `DerefAssign`, `DerefIncrement`, `DerefIncrementAssign`.

Two further axes are scattered the same way: **address-of** lives in `AddressOf`
(var), `MemberAddressOf`, and a `MemberIndex.address_of=True` bool, with no
`&arr[i]` form at all; **increment/decrement** lives in `IncrementDecrement`,
`MemberIncrementDecrement`, `DerefIncrement`, and `DerefIncrementAssign`, whose
docstring admits the holes ("no `*p++`, no `a[i]++` yet"). Roughly 15-18 node
classes are facets of what should be about 9.

Several deferred items elsewhere are the *same* missing-`Place` symptom rather
than independent follow-ups: the "wait for a real use case" note on
`MemberAddressOf` / `MemberIndex` / `MemberIndexAssign` /
`MemberIncrementDecrement` carrying `object_name` only, the "assignment to a
double-subscript LHS is not yet supported" note on `DoubleIndex`, and the
pointer-to-pointer classifier work. They stop being follow-ups and become
structurally impossible to omit once generality is in the tree shape instead of
per-shape.

## IR vs. direct emission: why the access nodes bypass the optimizer

This is current-state rationale, recorded because the `Place` work interacts
with it directly.

cc.py has two lowering paths. Most computation flows through a real IR
(`cc/ir.py`) with dedicated instructions — `Index`, `IndexAssign`, `Copy`,
`BinaryOperation`, `Call`, `Return`, `Switch`, `RepString`, … — that gets SSA
(`cc/ssa.py`), optimization (`cc/ir_optimize.py`), and loop analysis
(`cc/loops.py`). But the member / deref / double-index / index-member access
families never enter the IR: the builder wraps them in `ir.Block(node=…)` — an
"escape hatch: lower this AST node via the existing statement codegen"
(`cc/ir.py:76`) — and `lower_ir_body` calls `generate_expression` /
`generate_statement` straight on the AST (`cc/codegen/x86/emission.py:1794`).

This split is **deliberate, and the boundary is principled**: `Index` /
`IndexAssign` live in the IR specifically because the rep-string / loop optimizer
needs to *see* array subscripts inside loops to rewrite `for (i…) dst[i]=src[i];`
into `rep movs` (see
[`2026-06-01-cc-rep-string-loops-design.md`](./2026-06-01-cc-rep-string-loops-design.md)).
Member and bitfield accesses aren't loop-carried array math, so there was no
optimization payoff to justify the cost of modeling them in the IR — every node
added to the IR must also be handled by SSA, every pass, and liveness. Keeping
rarely-optimized constructs out of the IR keeps the optimizer small and correct.

It is, however, a real (if mild) smell, with costs worth naming so the trade-off
stays intentional:

- **Missed optimization.** Anything riding `Block` bypasses SSA, constant
  folding, CSE, and the allocator's view. `s->field` / `arr[i].f` get none of
  the optimization `arr[i]` gets. Usually fine — these are memory touches with
  little headroom — but it's a real asymmetry.
- **The `IntegerOperand` marker is a workaround for exactly this.** Because the
  operand-typing logic can't see *into* a `Block`-wrapped node, the mixin
  (`cc/ast_nodes.py:32`) exists to assert "this evaluates to an integer." It
  patches the layering hole.
- **Duplicated address arithmetic.** The IR `Index` path and the direct member
  path each compute base + scaled-index + offset independently — two places to
  get pointer math right. This is the bug surface the `Place` work shrinks.
- **Layering inversion.** The IR is the lowering *target*, yet `Block` punches a
  hole back up to AST-level codegen.

The boundary is acceptable as long as it stays intentional and the costs above
are tolerated. The `Place` refactor is scoped to the direct-emission path and
neither worsens nor fixes the dual path; whether to *resolve* it is the explicit
decision-spike gating the final stage of that work (see the plan's Roadmap).

## The design

Introduce a small recursive AST node — `Place` — for "an addressable location in
memory" (the same concept Clang's CodeGen calls `LValue` and rustc's MIR calls a
`Place`; the sub-nodes map onto GCC's reference tree codes `ARRAY_REF`,
`COMPONENT_REF`, `INDIRECT_REF`). `Place` is preferred here as the plainer, more
immediately legible name.

```python
class Place(Node): ...

class VariablePlace(Place):       # x
    name: str

class DereferencePlace(Place):    # *p  — pointee of ANY pointer expression
    pointer: Node

class SubscriptPlace(Place):      # base[i]   — base RECURSES
    base: Place
    index: Node

class MemberPlace(Place):         # base.field  (base->field == .field on a DereferencePlace)
    base: Place
    member_name: str
```

`p->f` is sugar for `MemberPlace(base=DereferencePlace(p), "f")`, so the `arrow`
flag can disappear entirely. Five operation nodes then consume *any* `Place`
(shown here with their conceptual names; the implementation prefixes them
`Place*` — `PlaceLoad`, `PlaceStore`, … — to avoid colliding with the existing
`AddressOf` / `IncrementDecrement` / `IndexedCall` nodes until those are retired;
see the plan):

```python
class Load(IntegerOperand, Node):  place: Place                  # rvalue read
class AddressOf(Node):             place: Place                  # &place -> lea
class Store(Node):                 place: Place; value: Node     # place = value
class IncDec(IntegerOperand, Node): place: Place; delta: int; is_postfix: bool
class CallThrough(Node):           place: Place; args: list[Node]
```

(`Load` / `IncDec` keep the `IntegerOperand` mixin that today's `MemberAccess`
carries, so the optimizer still sees member reads as foldable operands.)

The codegen contract collapses to one recursive routine that returns a register
holding the byte address plus the resolved C type:

```python
def address_of(self, place):
    match place:
        case DereferencePlace(ptr):  eval ptr -> acc; base = acc; typ = pointee(type_of(ptr))
        case MemberPlace(b, m):      a = address_of(b); base = a + field_offset(typ, m);     typ = field
        case SubscriptPlace(b, i):   a = address_of(b); base = a + scaled(i, elem_size(typ)); typ = elem
        case VariablePlace(name):    base, typ = symbol_address(name)
    return base, typ
```

Each terminal op becomes shape-agnostic: `Load` → `address_of` then field-load
at the type width; `AddressOf` → `address_of` and stop (`lea`); `Store` →
evaluate value, `address_of`, field-store; `IncDec` → `address_of` once, load,
adjust by `sizeof(pointee)` or 1, store back; `CallThrough` → `Load` the function
pointer then the existing indirect-call path. `arr[i][j].f[k] = x` then just
works with zero new classes. The address-building primitives already exist
(`_resolve_index_member_layout`, `_build_address`, `_emit_struct_element_offset`,
`_emit_field_load`, `_emit_field_store`, `_index_pointee_size`); they are
currently called from per-shape methods instead of from one recursive walker.
`PointerDereference` already does the "evaluate an arbitrary address expression,
then load" thing correctly and is the template for the whole refactor.

Implementation note: to stay byte-exact for the direct-emission families, the
recursive routine reproduces the existing *symbolic* address — a `(const_base,
static_offset, dynamic_index_in_BX)` triple fed to `_build_address` +
`_emit_field_load`/`_emit_field_store` — rather than materializing a pointer in a
register (which would emit an extra `lea`). The plan calls this descriptor
`_PlaceAddress` and the routine `_resolve_place`.

## Migration path (incremental, byte-exact)

1. Add `Place` + the five op nodes alongside the existing zoo; write the
   recursive address routine reusing the existing helpers.
2. Point the parser's postfix-chain / lvalue parser at `Place` for **new** shapes
   only; keep emitting legacy nodes for shapes that already work.
3. Re-express each legacy `generate_*` method as a thin shim that builds the
   equivalent `Place` + op and delegates, golden-diffing against current output —
   `tests/test_asm.py` byte-for-byte diffs against NASM are the safety net.
4. Once the shims match, delete the legacy classes and their `isinstance` arms in
   `parser.py`, `generator.py`, `emission.py`, `liveness.py`, and `base.py`.

The load-bearing risk is the type-propagation in the address routine (today's
per-shape methods each bake in their own element/pointee sizing); getting it
wrong silently miscompiles widths. Stage it behind the byte-exact harness and
convert one family at a time (the four `IndexMember*` classes are the cleanest
first scalp).

## Status

Planned. Staged across several plans — Plan 1 = `Place` infrastructure + the
`IndexMember*` family as a byte-exact proof-of-concept, with the IR-touching
`Index` fold sequenced last behind a decision spike. See
[`2026-06-01-cc-place-refactor-plan.md`](./2026-06-01-cc-place-refactor-plan.md).
