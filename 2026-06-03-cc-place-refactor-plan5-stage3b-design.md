# cc.py Place Refactor — Plan 5 / Stage 3b Design: operand lowering + fold `Index` + rep-string matcher rewrite

**Status:** Designed (2026-06-03). Ready for implementation planning.

**Goal:** Introduce the optimizer-visible uniform access ops
(`Address`/`Load`/`Store`/`AddressOf`), lower the **entire** access-family of
places onto them, fold the named-array `ir.Index`/`ir.IndexAssign` nodes into
the same ops (deleting both nodes), and rewrite the rep-string / loop-idiom
matcher to recognize the uniform shapes. This is the substrate that makes
address math visible to the optimizer; the CSE / LICM / strength-reduction that
*exploits* that visibility is Stage 3c.

**Relationship to prior stages.** Stage 3a unified all lvalue-address codegen
onto one recursive `resolve_address(place) -> MemoryOperand`, but it emits
**straight to x86 with no optimizer-visible address IR**: complex accesses ride
`ir.Access(node=<AST>)` and named arrays ride `ir.Index`/`ir.IndexAssign`. The
`Address`/`Load`/`Store` ops the original decision-spike's "Stage 1" sketched
were **never built**. Stage 3b builds them. The decision-spike's acceptance
gate (per-function **byte-efficiency**, not byte-identity) governs.

## Decisions taken during brainstorming

1. **Rep-string matcher: rewrite onto the uniform ops (spike endgame), not
   defer.** `ir.Index`/`ir.IndexAssign` are deleted; the matcher is rewritten to
   recognize `Load`/`Store`/`Address` loop shapes. This matches the
   decision-spike's stated Stage-3 endgame and unlocks future rep-matching of the
   inner contiguous dimension of multidim accesses once 3c's LICM hoists the
   outer address. (The lower-risk alternative — keep the nodes as a transient
   form and lower *after* rep-string recognition — was rejected.)
2. **Two gated PRs.** 3b.1 is the byte-neutral substrate (new ops + fold the
   access family + emission folds back to identical x86). 3b.2 is the risky half
   (fold `Index`/`IndexAssign`, delete the nodes, rewrite the matcher). Each PR
   is independently byte-gated, so a size regression or rep-string miss is easy
   to bisect.
3. **Fold the full access family in 3b.** `PlaceLoad`/`PlaceStore`/`PlaceCall`
   **and** `PlaceAddressOf` **and** `PlaceIncrementDecrement` all come off the
   `Block`/`Access` escape hatch onto the uniform ops. Only the non-access
   `Block` residents (VarDecl, struct-init, bitfield *assign*, the long-type /
   const-chain / self-modify `Assign` special-cases) stay on `Block` for Stage 4.
4. **Structured-reference op (GCC GIMPLE model), layout derived at emission;
   defer the layout-to-`cc/types.py` migration.** The `Address` op carries the
   structured place **shape** (the `Member`/`Subscript` tree, for static
   layout) with its **dynamic leaves pre-lowered to IR temps** (the index summed
   to one temp; each dereference's pointer is a preceding `ir.Load` result);
   emission reuses 3a's `resolve_address` layout logic (the documented
   `get_inner_reference` / `EmitLValue` analog) almost verbatim, fed the
   pre-lowered `Value` leaves instead of re-walking AST sub-expressions. This is
   GCC's "structured ref + SSA leaves" model; cc.py already committed to it in
   3a. (The pure precomputed-`(base, disp, index, scale)` tuple — closer to an
   LLVM GEP — was rejected: LLVM only avoids the resulting code bloat via a full
   instruction-selection addressing-mode matcher that cc.py has deliberately
   chosen not to build, so the flat form gains nothing here while discarding the
   structured semantic info and adding offset/size byte-regression risk.) The
   layout logic stays in the x86 generator for 3b; relocating it into the
   existing `cc/types.py` so IR passes and emission share one target-organized
   layout source is a **separate future refactor** (orthogonal to the op shape,
   which is durable either way).

## Architecture

### New IR ops (`cc/ir.py`)

```
Address   (structured-reference value — emits NO code on its own; GCC GIMPLE model)
  destination: str                  # SSA temp naming this resolved address
  shape:       ast_nodes.<Place>    # the deref-free place segment, for STATIC layout
                                    #   (member offsets, element size, bitfield, decay);
                                    #   its dynamic leaves are pre-lowered to IR temps
  base_value:  Value | None         # pointer Value from the preceding ir.Load
                                    #   (the segment's deref-broken base); None for a
                                    #   symbol-rooted segment (global / local)
  index:       Value | None         # the segment's summed dynamic ELEMENT index temp
  VALUE_FIELDS = ("base_value", "index")   # the two optimizer-visible dynamic leaves

Load      (memory read)   destination, address: Value, width, signed
                          VALUE_FIELDS = ("address",)
Store     (memory write)  address: Value, width, value: Value
                          VALUE_FIELDS = ("address", "value")
AddressOf (pure -> lea)   destination, address: Value
                          VALUE_FIELDS = ("address",)
```

`Address` is the IR-level twin of the *input* to 3a's `resolve_address`: it
carries the structured place **shape** (so emission derives `displacement` /
`element_size` / `bitfield` / `decay` from the existing layout helpers, exactly
as today — the `get_inner_reference` decomposition) while the **dynamic** parts
— the summed element `index` and, for a deref-broken segment, the pointer
`base_value` — are first-class optimizer-visible `Value` operands rather than
sub-expressions evaluated inline at codegen. Because deref-breaks-the-chain and
multiple dynamic indices sum into one temp at lowering, each `Address` segment
has at most these two dynamic leaves, so `VALUE_FIELDS` stays flat and static
(the `_NoValueFields`-style mixin pattern requires it). A static-only segment
carries `base_value = None`, `index = None`.

### Lowering (`cc/ir.py` builder) — segment the chain, pre-lower the dynamic leaves

Stage 3a's *codegen-time* recursion becomes a *lowering-time* segmentation that
emits the address chain into the optimizable IR. Lowering does **not** compute
layout (that stays at emission); it splits the place at each dereference and
pre-lowers the dynamic leaves to IR `Value`s:

- Walk the place; accumulate `Member` / `Subscript` nodes into the **current
  deref-free segment shape** (a `Place` subtree). For each **dynamic** subscript
  index, `_build_expr` the index to a `Value`; sum multiple dynamic indices in a
  segment into one temp via an ordinary `BinaryOperation` (now
  optimizer-visible); store that temp as the segment's `index`. Constant
  subscripts and all member selections stay in the shape (their offsets/strides
  are static, derived at emission).
- `DereferencePlace(ptr)` → **chain-break**: close the current segment as
  `Address(shape, base_value, index)`, emit `Load(address = that Address) -> p`
  (the pointer load 3a already emits anyway), and open a fresh segment with
  `base_value = p`. Arbitrary depth falls out as a flat `Address` / `Load`
  sequence — `Member` / `Subscript` accumulate onto the current segment; each
  `Dereference` ends a segment and starts the next.
- The dynamic leaves the optimizer sees are exactly per-segment `index` and
  `base_value`; the static structure (which member, which array, constant
  subscripts) rides the immutable `shape`.

Terminals (applied to the final segment's `Address`):

- `PlaceLoad` → `Load`.
- `PlaceStore` → `Store`.
- `PlaceAddressOf` → `AddressOf` (`lea`-terminal; also the bare-array /
  struct-value member decay path, which sets `Address.decay`-style handling at
  the terminal exactly as 3a's `decay_to_address`).
- `PlaceCall` → `Load` the function pointer, then an indirect call through the
  loaded `Value`. Reuse the existing function-pointer call emission; if `ir.Call`
  cannot take a `Value` target today, add the minimal indirect-target support
  (flagged for the plan to confirm against the current call path).
- `PlaceIncrementDecrement` → `Load(address) -> t`, `BinaryOperation(t, +/- 1)
  -> t2`, `Store(address, t2)`. For bitfield members the mask/shift
  read-modify-write stays at the `Load`/`Store` **terminal** (driven by
  `Address.bitfield`), not in the address arithmetic — identical to 3a.

### Emission (`cc/codegen/x86/`) — folds back to identical bytes

`Address` emits nothing on its own. `Load` / `Store` / `AddressOf` /
indirect-call resolve their `Address` operand to a `MemoryOperand` via a
**lightly refactored `resolve_address`**: instead of evaluating index
sub-expressions inline and calling `generate_expression(pointer)` at each deref,
it consumes the `Address`'s pre-lowered `index` and `base_value` `Value`s
(materializing/scaling the `index` into a register and seeding a register base
from `base_value` exactly where 3a's `_accumulate_subscript` /
`_resolve_dereference` do). The static layout (`displacement` from member
offsets + constant subscripts, `element_size`, `bitfield`, `decay_to_address`)
is still derived from the `shape` by the **existing** layout helpers
(`_member_layout_on`, `_resolve_member_place_info`, the multidim/pointer-array
address helpers). The terminal then reuses the **existing** width `mov` /
`movzx`, bitfield mask-shift, and `lea` emitters.

Because layout derivation and terminal emission are the same code 3a runs, and
the only change is *where the dynamic leaves come from* (a pre-lowered `Value`
vs. an inline AST walk), the folded output is the **same x86 `resolve_address`
produces today** — the byte-neutrality claim for 3b.1, with the per-function
size gate as the backstop.

**Central 3b.1 risk — operand materialization parity.** Making the dynamic
leaves IR `Value`s means materializing them as temps, whereas today
`resolve_address` evaluates complex-place index / pointer sub-expressions
*inline*. For a leaf that was already a simple value (literal, variable, or a
temp) this is a no-op; for a compound leaf (`a[i + 1]`, `a[f(x)]`) it hoists the
computation ahead of the address — a sequence cc.py already produces for
`ir.Index` (whose index is pre-lowered to a `Value` under the same gate), so it
is demonstrably byte-manageable, but it must be re-proven **per access shape**.
The migration therefore moves one place-shape at a time behind the size gate
(mirroring 3a), fixing each register/fold/ordering delta as it appears.
`resolve_address` must also keep reproducing its existing fast-paths
(frame-direct deref, accumulator-favored short encodings such as `A1`
`mov eax,[disp32]`).

### Optimizer treatment

**3b.1 (substrate only) — conservative, behavior- and byte-neutral:**

- `Load` / `AddressOf` are reads: DCE-able when their destination is unused, but
  **not** reordered or CSE'd across `Store` / `Call` (memory-barrier discipline).
  `Store` is side-effecting and never eliminated.
- `Address` is pure and DCE-able, but is **not** CSE'd / hoisted yet (that is 3c).
- Use-count discovery: the new operands are first-class `VALUE_FIELDS`, counted
  directly. The load-bearing `_iter_ast_var_names` special cases
  (`VariablePlace.name`, `target_name`, `object_name`) must keep working for
  whatever **still** rides `Block` (VarDecl, struct-init, the `Assign`
  special-cases) so SSA induction-variable analysis stays correct.

**3b.2 — rep-string matcher rewrite (`cc/loops.py`):** after `Index`/
`IndexAssign` are gone, loop kernels are uniform ops. Re-key the matchers:

- **fill** `_match_fill_body`: one `Store(Address(base_symbol = dst, index = IV,
  scale = k), value = V)`, `V` and `dst` loop-invariant.
- **copy** `_match_copy_body`: `Load(Address(src, index = IV, scale = k)) -> t`
  then `Store(Address(dst, index = IV, scale = k), t)`, `src`/`dst`
  loop-invariant and non-aliasing.

Extract `element_size` from `Address.scale`, `dst`/`src` from `base_symbol`, the
unit stride from the (unchanged) IV step, and loop-invariance / aliasing from the
`Address` operands. Ordering invariants preserved: lowering-to-uniform-ops runs
**before** recognition (the matcher only ever sees `Load`/`Store`), and
recognition still runs **before SSA** (so the IV entry value is not
copy-propagated into the comparison / index).

The deeper CSE / LICM / strength-reduction *over* `Address` values is **Stage
3c**, explicitly out of scope here.

## What gets deleted / absorbed

- **3b.1:** the per-shape `Access`-driven lowering and the access-family `_emit_*`
  wrappers that the uniform `Load`/`Store`/`AddressOf` emission replaces. (The
  `resolve_address` core survives, lightly refactored so it consumes the
  `Address` op's pre-lowered `index` / `base_value` `Value`s instead of walking
  AST sub-expressions inline.)
- **3b.2:** `ir.Index`, `ir.IndexAssign`, and their dedicated emit paths
  (`_generate_index_expression`, `generate_index_assign`,
  `_generate_nested_index_expression`).

`Block` still exists after 3b (its non-access residents are Stage 4). `Access`'s
fate: once the full access family is folded, `Access(node=...)` has no remaining
producers — confirm and delete it in 3b.1 if so, else document why it survives.

## Testing & gate

- **Primary efficiency oracle:** `tests/test_cc_function_sizes.py` (per-function
  ELF `.text.<name>` sizes vs `tests/golden/cc_function_sizes_baseline.json`;
  `BBOE_UPDATE_SIZES=1` regenerates). **3b.1 targets zero deltas** (any residual
  must be an explained, justified benign reshape, not a regression); 3b.2
  regenerates only where a delta is justified (and each is explained in the PR).
- **Tripwire:** `tests/test_cc_place.py` golden (`tests/golden/cc_place_index_member.asm`;
  `BBOE_UPDATE_GOLDEN=1`) — re-bless once in 3b.1 to absorb any benign sequence
  reshape, then it reverts to a deliberate-change tripwire.
- **Rep-string oracle (3b.2):** `tests/test_programs.py::rep_loops_test`
  (`^65 4660 3735928559 65 65$`, covering `rep stosb/w/d` + `rep movsb` + the
  signed-counter `n <= 0` guard) must stay green; add uniform-op-shape unit tests
  for the rewritten matchers.
- **Correctness matrix** (per the "run full CI matrix locally on big changes"
  rule): `tests/test_asm.py` (incl. the asm.c-through-cc.py self-host path —
  watch for any new instruction form the self-hosted assembler must parse),
  `tests/test_programs.py` bbfs + ext2 (incl. `e2fsck`), `tests/test_bboefs.py`,
  `tests/test_cc_bits.py` (assembles every cc program at `--bits 16` **and** 32),
  and the `tests/unit/` suite. The arbitrary-depth runtime probes added in 3a
  (`depth_triple`, `depth_arrow_index`, `double_index_ptr_store`, the struct-array
  stride probes, etc.) must stay green.

## Risk register

1. **Operand-materialization parity (3b.1)** — pre-lowering a compound dynamic
   leaf (`a[i + 1]`, `a[f(x)]`) to a temp can reorder/insert vs. 3a's inline
   evaluation, and `resolve_address` must keep reproducing its register choices /
   short encodings / fast-paths when fed a pre-lowered `Value`. Mitigated by
   migrating one place-shape at a time behind the size gate (the same discipline
   `ir.Index`'s pre-lowered index already passes); the size gate + re-blessed
   `cc_place` golden are the backstop, each delta a register/fold/ordering tweak.
2. **Rep-string matcher rewrite (3b.2)** — highest risk in the whole program. A
   missed shape silently stops `rep`-ifying a loop (size regression, caught by the
   gate); a mis-identified induction step miscompiles (caught by the rep-string
   runtime suite). Port with the existing rep-string tests as the oracle.
3. **Memory-ordering discipline for `Load`/`Store`** — `Load` must not be
   reordered/CSE'd across `Store`/`Call` even in 3b.1's conservative treatment, or
   a later 3c pass inherits an unsound base. Establish the barrier semantics in
   3b.1.
4. **Bitfield RMW at the terminal** — `PlaceIncrementDecrement` and bitfield
   `Store` must preserve the load-mask / store-mask sequence for both shallow and
   arbitrary-depth bitfield members.
5. **`PlaceCall` indirect target** — confirm `ir.Call` (or the call emitter) can
   call through a loaded `Value`; add minimal support if not.
6. **`_iter_ast_var_names` discipline** — the `VariablePlace` / `target_name` /
   `object_name` string-extraction must keep covering the remaining `Block`
   residents so SSA induction analysis stays correct after the fold.

## Out of scope (later stages)

- **Stage 3c:** SSA value-numbering / CSE of `Address`, LICM hoisting of address
  math, strength reduction over `Address` index expressions — the optimization
  payoff this substrate enables.
- **Stage 4:** retire `Block` entirely (VarDecl / ArrayDecl, struct initializers,
  bitfield *assigns*, the `Assign` special-cases, route `asm` to opaque
  `InlineAsm`/`ExtendedAsm`), then delete the `Block` node.
