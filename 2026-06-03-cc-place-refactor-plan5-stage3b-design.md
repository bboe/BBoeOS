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

## Architecture

### New IR ops (`cc/ir.py`)

```
Address   (pure descriptor value — emits NO code on its own)
  destination:  str            # SSA temp naming this address
  base_kind:    "label" | "frame" | "value"
  base_symbol:  str  | None    # "_g_points" / "ebp-12"   (kind in {label, frame})
  base_value:   Value | None   # pointer temp             (kind == "value")
  displacement: int = 0        # member offsets + constant subscripts
  index:        Value | None   # dynamic ELEMENT index (literal or temp)
  scale:        int = 1        # element size in bytes
  bitfield:     FieldInfo | None
  raw_width:    bool = False   # mirror MemoryOperand's movzx-suppression
  VALUE_FIELDS = ("base_value", "index")   # both optimizer-visible operands

Load      (memory read)   destination, address: Value, width, signed
                          VALUE_FIELDS = ("address",)
Store     (memory write)  address: Value, width, value: Value
                          VALUE_FIELDS = ("address", "value")
AddressOf (pure -> lea)   destination, address: Value
                          VALUE_FIELDS = ("address",)
```

`Address` is the IR twin of 3a's `MemoryOperand` (`cc/codegen/x86/generator.py`),
with one deliberate difference: `index` is an **optimizer-visible `Value`** (a
temp) rather than a pre-allocated register, so the index arithmetic becomes
ordinary `BinaryOperation`/`Copy` the optimizer already understands. The
`base_symbol` / `base_value` split keeps `VALUE_FIELDS` static (the
`_NoValueFields`-style mixin pattern requires it); exactly one is populated.

### Lowering (`cc/ir.py` builder) — the recursion moves to lowering time

Stage 3a's *codegen-time* recursion becomes a *lowering-time* linearization (the
GCC GEP-chain / LLVM `EmitLValue` model — the address chain is emitted into the
optimizable IR, not reconstructed at codegen):

- `VariablePlace(name)` → seed `Address(base_kind = label|frame,
  base_symbol = "_g_<name>" | "ebp-off", displacement = 0)`.
- `MemberPlace(base, m)` → resolve base to a running `Address`; add `m`'s static
  offset to `displacement`; carry `bitfield` / `raw_width` when `m` is the
  terminal member.
- `SubscriptPlace(base, idx)` → resolve base; a constant `idx` folds into
  `displacement`; a dynamic `idx` becomes the `index` `Value` with `scale` = the
  element size. A second dynamic index in the same segment sums into the existing
  index via an ordinary `BinaryOperation` temp (now optimizer-visible) — today's
  shape-B `add bx, ax`.
- `DereferencePlace(ptr)` → **chain-break**: emit `Load(address = <current
  Address>)` → pointer temp; start a fresh `Address(base_kind = "value",
  base_value = that temp, displacement = 0)`. Arbitrary depth falls out as a flat
  `Address` / `Load` sequence — `Member`/`Subscript` accumulate onto the current
  segment; each `Dereference` ends a segment and starts the next.

Terminals:

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

`Address` emits nothing. A new `_materialize_address(address_op) ->
MemoryOperand` mirrors the **tail** of 3a's `resolve_address`: it materializes
and scales the `index` `Value` into a register and sets up the base, producing
the same `MemoryOperand` the Place-driven resolver produces today. `Load` /
`Store` / `AddressOf` / indirect-call then reuse the **existing** terminal
emitters (width `mov` / `movzx`, bitfield mask-shift store/load, `lea`).

Because the `Address` descriptor maps 1:1 onto `MemoryOperand`, the folded
output is the **same x86 that `resolve_address` produces today** — that is the
byte-neutrality claim for 3b.1, with the per-function size gate as the backstop.

**Central 3b.1 risk:** 3a's `resolve_address` makes specific register choices
and fast-paths (frame-direct deref, accumulator-favored short encodings such as
`A1` `mov eax,[disp32]`). The IR-linearized lowering + `_materialize_address`
must reproduce them exactly; this is where the size-gate iteration lives. The
deref-chain `Load` is the unavoidable pointer load 3a already emits, so it costs
nothing extra.

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
  recursive `resolve_address` core survives, refactored so its materialization
  tail is reachable as `_materialize_address` from the IR ops.)
- **3b.2:** `ir.Index`, `ir.IndexAssign`, and their dedicated emit paths
  (`_generate_index_expression`, `generate_index_assign`,
  `_generate_nested_index_expression`).

`Block` still exists after 3b (its non-access residents are Stage 4). `Access`'s
fate: once the full access family is folded, `Access(node=...)` has no remaining
producers — confirm and delete it in 3b.1 if so, else document why it survives.

## Testing & gate

- **Primary efficiency oracle:** `tests/test_cc_function_sizes.py` (per-function
  ELF `.text.<name>` sizes vs `tests/golden/cc_function_sizes_baseline.json`;
  `BBOE_UPDATE_SIZES=1` regenerates). **3b.1 expects zero deltas**; 3b.2
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

1. **Byte-neutrality of the IR-linearized lowering (3b.1)** — reproducing 3a's
   exact register choices / short encodings through `_materialize_address`. The
   size gate + re-blessed `cc_place` golden are the backstop; each regression is a
   register/fold tweak.
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
