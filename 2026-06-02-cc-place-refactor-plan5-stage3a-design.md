# cc.py Place Refactor — Plan 5 / Stage 3a Design: recursive address resolver

**Status:** Designed (brainstormed + approved). First sub-stage of the merged
Stage 3 (decomposition A: recursion-first). Sub-stages 3b (fold named-array
`ir.Index` + rep-string rewrite + operand lowering) and 3c (CSE/LICM/SSA
optimizer port) follow.

**Goal:** Replace the five bespoke place-codegen emitters with **one recursive
`resolve_address(place)`** over a generalized machine-operand descriptor, so
arbitrary-depth lvalues (`a[i][j][k]`, `a->b[1][2]`, `(*p)[i][j]`,
`s.grid[i][j].f[k]`) compile and run, and the 2-level `_emit_double_index_place_*`
hack is retired. Gate: per-function bytes ≤ baseline, or — if larger — a notable
performance win.

## Context

Today `_emit_place_load` / `_emit_place_store` (`cc/codegen/x86/generator.py`)
dispatch a Place to one of five hand-rolled paths:
`_emit_member_index_*`, `_emit_member_scalar_*`, `_emit_double_index_place_*`
(the 2-level array-of-pointers hack), `_emit_dereference_place_*`, and the
generic `_resolve_place` → `PlaceAddress` path (struct-array shapes A/B only).
Several "own bespoke sequences that do not fit the `PlaceAddress` model." The
result handles only fixed depths and raises "unsupported Place shape" beyond
them. `ir.Index` / `ir.IndexAssign` (named-array `a[i]`) use a *separate* IR-op
emit path and are **out of scope for 3a** (folded in 3b).

## Architecture

### Generalized descriptor

Generalize `PlaceAddress` (whose base is only `const_base: str`) to a
machine-operand descriptor whose base may be a register:

```
MemOperand:
  base_kind:    "label" | "frame" | "register"
  base:         str          # "_g_points" | "ebp-12" | a register name
  displacement: int          # static: member offsets + constant subscripts
  index:        str | None   # register holding the summed dynamic byte-offset
  field_size / element_size: int   # terminal load/store width (as today)
```

x86 folds `base + displacement + index` into one memory operand
(`_build_address`).

### Recursive resolution

`resolve_address(place) -> MemOperand`, a fold over the Place tree:

- **`VariablePlace(name)`** → seed a static base: `label` (`_g_<name>`) for a
  global, `frame` (`ebp-off`) for a local; displacement 0, no index.
- **`MemberPlace(base, m)`** → `r = resolve_address(base)`; add `m`'s static
  offset to `r.displacement`; return `r`. (Per-`Member` layout via the existing
  `_resolve_index_member_layout` logic, now called inside the recursion.)
- **`SubscriptPlace(base, idx)`** → `r = resolve_address(base)`; constant `idx`
  folds into `r.displacement`; dynamic `idx` is scaled (`shl`/`lea`) and **added
  into the single index register** (`r.index`; a second dynamic index in the
  same segment sums into the same register — today's shape-B `add bx, ax`).
- **`DereferencePlace(ptr)`** → the chain-breaker: evaluate the pointer
  *expression* `ptr` to a register via the existing `generate_expression`
  (which, for a nested `PlaceLoad`, recurses back into `resolve_address` for
  *its* address — the mutual recursion that yields arbitrary depth); return a
  fresh `register`-base `MemOperand`, displacement 0, no index.

Arbitrary depth falls out: `Member`/`Subscript` accumulate onto the current
segment; each `Dereference` ends a segment (materializing a register base) and
starts a new one.

### Materialization & the x86 budget

A deref-free run folds into one operand. Materialization happens at exactly two
points on x86:

1. **`DereferencePlace`** — emit the pointer load `mov reg, [current operand]`;
   `reg` is the new base. This load is needed anyway, so it costs nothing extra.
2. **Terminal** — `mov acc, [operand]` for an rvalue (width-correct field load),
   or `lea reg, [operand]` when the place is wanted as an address (`&member`,
   bare-array-member decay).

x86's disp32 + one summed index register means the budget never forces a
*speculative* mid-segment `lea`; the only register materialization is the
unavoidable pointer load at each deref.

### Register freedom

The resolver allocates scratch registers (index accumulation, pointer loads)
**freely**, preferring registers not holding live/pinned values — so it can
often drop today's defensive `push bx`/`pop bx` (a size win). It need not
reproduce today's exact register identities. Caveat: a few x86 encodings are
accumulator-favored (e.g. `mov eax,[disp32]` = 5 bytes via `A1` vs 6 for another
register); the resolver prefers the accumulator where a short form applies, and
the per-function byte-size gate flags any miss.

### Terminal concerns (stay out of the resolver)

- **Bitfields**: `resolve_address` returns the *containing unit's* operand; the
  terminal keeps today's mask/shift read-modify-write (load and store).
- **Width**: byte/word/full selection (`movzx`/`mov`) stays at the terminal, as
  today.

### Target boundary (documented, not implemented)

The recursion + segmentation are target-independent (the GCC
`get_inner_reference` / LLVM GEP model). Only the `MemOperand` → instruction
encoding and the *materialization budget* (can the base be a label? disp range?
is index scale free?) are target-specific. BBoeOS is x86-only with no ARM
backend planned, so the x86 budget stays **inline** in `resolve_address` — no
target-budget interface (YAGNI). If ARM ever lands, the recursion is reused
untouched; only the encoder + a budget query factor out (e.g. ARM has no
label-base addressing → globals materialize via `ADRP`+`ADD`; limited-imm
displacement → large offsets materialize; native `LSL` scale → the `shl` folds
into the load). This seam is noted so the future split is obvious.

## Consistency with Clang / GCC

The structure matches both production compilers' lvalue resolution: recursive
ref-tree fold (`EmitLValue` / `get_inner_reference`); dereference breaks the
chain (a load then a new GEP / a `MEM_REF` base); **bitfields and width handled
at the load terminal, not in the address arithmetic** (`LValue::MakeBitfield` /
`get_inner_reference`'s `bitpos`); target-specific addressing-mode legalization
as a separable back-half (isel matcher / `legitimize_address`). The one
deliberate divergence: Clang/GCC emit the address into an *optimizable* IR
(LLVM GEPs / GCC RTL) that GVN/LICM/IVOPTS then optimize; 3a's `resolve_address`
emits straight to x86 with no intermediate address-IR. Recovering that
optimization (operand lowering so the optimizer sees the address math, then
CSE/LICM/strength-reduction) is exactly sub-stages **3b/3c** — a staged-rollout
choice, not a design disagreement.

## Scope boundary

- **In 3a:** the `ir.Access` Place shapes (member / dereference /
  subscript-of-expression / the absorbed double-index). One recursive resolver;
  delete the five bespoke emitters.
- **Not in 3a:** named-array `ir.Index` / `ir.IndexAssign` (own IR-op emit path,
  folded in 3b); operand lowering, copy-prop/DCE-into-access, CSE/LICM/SSA (3b/3c);
  the rep-string matcher (3b).

## What gets deleted / absorbed

`_emit_place_load` / `_emit_place_store` collapse to: resolve → terminal
load / `lea` / width-or-bitfield store. Deleted (logic absorbed into the
recursive cases): `_emit_double_index_place_load` / `_store` (the hack retired),
`_emit_dereference_place_load` / `_store`, `_emit_member_scalar_load` / `_store`,
`_emit_member_index_load` / `_store`, and the shape-A/B body of `_resolve_place`
(which becomes the general recursion). `_resolve_index_member_layout` /
`_match_struct_array_member` survive as per-`Member` helpers called inside the
recursion.

## Testing & gate

The byte-*exact* `tests/test_cc_place.py` golden is too strict now — register
choices may legitimately differ — so the oracle shifts:

- **Primary efficiency oracle:** the per-function **byte-size gate**
  (`tests/test_cc_function_sizes.py`) — every function ≤ baseline, or a notable
  perf win. Regenerate the baseline once as part of 3a; explain each non-trivial
  delta in the PR.
- **`cc_place` golden:** regenerate once to bless 3a's sequences, then it reverts
  to a *deliberate-change tripwire* (not an efficiency gate).
- **Correctness:** new arbitrary-depth tests — `a[i][j][k]`, `a->b[1][2]`,
  `(*p)[i][j]`, `s.grid[i][j].f[k]` — **compiled and run** (added to
  `tests/test_programs.py`), asserting computed values; plus the full matrix
  (`test_asm`, `test_programs` bbfs + ext2, `test_bboefs`, `tests/unit/`).

## Risk register

1. **Byte-size parity across every existing shape** (bitfields, word/byte/full,
   frame-direct deref, double-index, member chains) — the size gate + regenerated
   golden are the backstop; each regression is a register/fold tweak.
2. **Bitfield read-modify-write** must be preserved at the terminal (load mask
   + store mask) for both shallow and arbitrary-depth bitfield members.
3. **Scratch-register protection / liveness** across the recursion — register
   freedom helps, but pinned / live values must remain safe (the existing
   `_bx_holds_pinned_var` discipline generalizes to "don't clobber a live pin").
4. **Mutual recursion termination** — `DereferencePlace` → `generate_expression`
   → `resolve_address` must terminate and handle existing shapes, notably the
   parenthesized-deref `*(p+1)` form already in the tree.
5. **`lea`-terminal** (address / array-decay) must work at arbitrary depth, not
   just the current shallow cases.
