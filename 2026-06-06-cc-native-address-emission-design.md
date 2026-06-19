# cc.py native-Address emission refactor — design

**Status:** Approved design (2026-06-06). Successor to the Stage 3b design
([2026-06-03-cc-place-refactor-plan5-stage3b-design.md](./2026-06-03-cc-place-refactor-plan5-stage3b-design.md));
its acceptance criteria are the measured ledger
([2026-06-06-cc-native-address-emission-expected-byte-reductions.md](./2026-06-06-cc-native-address-emission-expected-byte-reductions.md)).
Scope agreed with Bryce: flip-over to parity, the four ledger re-admissions,
Stage 3b.2 (delete `ir.Index`/`ir.IndexAssign`, rewrite the rep-string
matchers), and Stage 3c (CSE / LICM / Horner over addresses) — one coherent
design, landed as five gated phases.

## Problem

Stage 3b.1 migrated the access family onto uniform
`Address`/`Load`/`Store`/`AddressOf`/`IncrementDecrement`/`IndirectCall` IR ops,
but emission still works by **re-seating the AST shape** carried on each
`ir.Address` and re-running the legacy `resolve_address` walker. That made
byte-neutrality provable (identity in, identity out) at two structural costs:

1. The legacy walk uses **fixed scratch registers** (AX for index evaluation,
   BX/SI for accumulation) that are invisible to the register allocator — any IR
   temp live across the walk gets a conservative spill or save. This is the root
   cause of ledger classes 1, 3, and 4.
2. The store RHS round-trips through `_ir_value_to_ast`, which preserves only
   the leaf and **drops AST-level attributes** like a cast's store width —
   ledger class 2.

It also blocks Stage 3c: even a CSE'd `Address` value would be re-materialized
at every consuming terminal, so address CSE/LICM cannot produce byte wins until
materialization is explicit and happens once.

## Approaches considered

- **A — Shape-for-layout, native materializer (minimal).** Emission stops
  re-seating AST but derives layout from `shape` at each terminal. Smallest
  diff, but the optimizer never sees address structure — CSE/LICM operate on
  opaque equality with no principled cost model.
- **B — Explicit address arithmetic in IR (maximal).** The IR builder gets a
  layout oracle; `Address` becomes pure arithmetic and `shape` is deleted.
  Maximum optimizer power, but the entire layout subsystem (struct layouts,
  decay, bitfields, multidim strides, 16-vs-32 sizing) must move ahead of
  codegen — enormous lift, highest parity risk, and bitfields/decay are awkward
  as bare arithmetic. **Rejected** (this also rejects, again, the transitional
  opaque-AST-RHS `StoreExpression` crutch).
- **C — Two-tier: shape-bearing IR + a codegen-entry AddressPlan pass.**
  **Chosen.** `ir.Address` stays shape-bearing (the builder still needs no type
  table); a lowering pass inside codegen — after layout registration, before
  emission — converts each `Address` into an explicit, target-aware
  **AddressPlan**. Emission materializes plans; the 3c passes run over plans,
  where the cost model naturally lives.

## Architecture

The pipeline grows one explicit layer inside codegen; the builder/IR contract is
untouched:

```
ir.Builder            (no type table — Address stays shape-bearing)
  → ir_optimize       (DCE/copy-prop, unchanged)
  → ssa/regalloc      (IR temps, unchanged inputs + NEW per-Address clobber facts)
  → codegen
      generator registers layouts (unchanged)
      [NEW] AddressPlanner: ir.Address → AddressPlan   (one walk per Address)
      [NEW] 3c passes over plans: CSE / LICM / Horner  (target-aware cost model)
      emission: materialize plan → MemoryOperand → terminal asm
```

### AddressPlan

New module `cc/codegen/address_plan.py`. An AddressPlan is the resolved,
target-aware form of one `ir.Address`:

- `base_kind` / `base` — frame slot, symbol, or register-class base (a
  dereferenced pointer's IR `Value`).
- `terms: tuple[(index_value: ir.Value, scale: int), ...]` — dynamic subscripts
  with their layout-derived strides. Each term is classified **SIB-encodable**
  (32-bit, scale 1/2/4/8) vs **needs-arithmetic** (arbitrary struct strides;
  everything in 16-bit, which has no scaling).
- `displacement: int` — member offsets plus constant-index × stride, all folded
  at plan time.
- `field_size` / `element_size` / `decay_to_address` / `bitfield` — the same
  terminal-shaping facts `MemoryOperand` carries today.
- `line`.

### Planner

Runs per function after layout registration. It walks each `Address.shape` chain
**once**, calling the existing layout helpers (`_member_layout_on`, stride/decay
resolution, `_variable_base`) — layout knowledge never leaves the generator. The
AST shape's job ends at plan time; emission never sees it again.

### Materialization

Replaces the `_ir_address_with_index` AST re-seat. `Int` values are already
folded into `displacement`; a SIB-encodable term goes straight into the operand
(`[base+reg*4+disp]` — preserving the ledger's risk-#1 single-instruction folds,
e.g. `call [table+eax*4]`); needs-arithmetic terms emit the same
scale/accumulate sequence as today but over **declared** registers; multiple
terms accumulate into one index register (x86's one-base + one-index operand
limit).

**Parity strategy for the flip-over:** the materializer is written to reproduce
the legacy `_accumulate_subscript` / `_accumulate_subscript_on_register` byte
sequences exactly for every currently-folded shape — fixed BX/SI scratch
retained in phase 1, with the byte gate (361 functions / 49 files), the
`cc_place` golden, and `cc_bits` 16/32 as enforcement. What changes immediately
is *visibility*: the clobbers become declared facts regalloc can consume,
instead of an opaque walk.

## Terminals, Store.width, and the clobber model

**Terminals consume plans, not shapes.** Each of the five access ops looks up
its producer's plan and drives the existing terminal emitters (width-aware
`mov`/`movzx`, bitfield mask/shift, `lea`, memory `inc`/`dec`, `call [slot]`)
directly from the plan's `MemoryOperand`. The `PlaceLoad` / `PlaceStore` /
`PlaceCall` rebuild-and-recurse trick is deleted along with
`_ir_address_with_index`, `_reseat_nested_subscript_indices`, and
`_ir_value_to_ast`'s role in store RHS round-trips.

**`ir.Store` gains `width: int | None`.**

- `None` (default) — width derives from the plan's `field_size`, exactly as
  today.
- Set at lowering when the source RHS was a `Cast`: the builder reads the cast's
  `target_type` width (syntactic — no layout table needed) and stamps it on the
  op. The `Value` round-trip that dropped the `char` cast in `readdir` (ledger
  class 2) no longer exists by construction.

This makes `Store.value` a true `Value`: any RHS the builder can pre-lower to a
temp is admissible, because the width and the address are both explicit —
`_is_byte_safe_store_rhs` stops being a wall and becomes a *byte-parity gate*
that phases 2–3 progressively open.

**Regalloc models the clobbers.** Two declared facts per `Address`:

1. **Clobber set** — the planner knows exactly which registers the
   materialization touches: nothing (pure-displacement plans), AX+BX (scaled
   dynamic term), AX+SI (register-base fold). This flows into regalloc the same
   way builtin clobber sets do: a temp live across an `Address` interferes only
   with that plan's actual clobbers, so the RHS of `tv->tv_sec = total_ms /
   1000` keeps its register and the `push`/`pop edx` around `div` disappears
   (ledger class 1).
2. **Evaluation-order contract** — emission evaluates RHS-then-address or
   address-then-RHS to match the legacy inline ordering per terminal shape; with
   both sides in declared registers, neither crosses an opaque region, so
   nothing spills (ledger classes 3–4).

Phase 1 keeps the *fixed* BX/SI scratch (byte parity); the declared clobbers are
what phases 2–3 exploit. Full allocation of the scratch registers themselves
(letting regalloc pick the index register) is deliberately deferred to 3c, where
the materialize-once policy needs it anyway — flipping both at once would make
gate failures unattributable.

## Stage 3b.2 subsumed: deleting ir.Index / ir.IndexAssign

With native materialization in place, `ir.Index`/`ir.IndexAssign` are redundant
special cases — a simple-name subscript is just a one-term plan.

**Lowering.** The builder emits `Address(base_value=None, indices=(i,),
shape=SubscriptPlace(VariablePlace(name), i))` + `Load`/`Store` everywhere it
emits `Index`/`IndexAssign` today. The planner resolves the stride from the
registered array/pointer type exactly as `_generate_index_expression` does now.
Both IR nodes are then **deleted**, along with their hand-written branches in
`ir_optimize` (`_has_side_effects`, `_instruction_value_operands`,
`_substitute_value`), `_collect_ir_index_operand_temps` in emission, and the
`_MODELED_VALUE_TYPES` entries — the access family becomes the *only* memory
path through IR.

**Rep-string matchers.** `cc/loops.py`'s fill/copy recognizers are rewritten
onto the uniform shapes:

- *fill*: loop body = `Address(arr, i)` + `Store(value=const_or_invariant)` +
  induction increment → `rep stos`.
- *copy*: `Address(src, i)` + `Load` + `Address(dst, i)` + `Store(loaded_temp)`
  + induction increment → `rep movs`.

The matchers get *simpler*, not just ported: today they must recognize one of
two encodings of the same access; afterward there is exactly one. The
`REP_STRING_CLOBBERS` contract and the emitted `rep` sequences are untouched — a
matcher-input change only, byte-gated 0-delta.

**Bare-`Var` increments (the 80 `Block` residents).** `i++` statements lower
onto `Address(shape=VariablePlace(i))` + `IncrementDecrement` — a
pure-displacement plan whose terminal is the same memory `inc [bp-N]` / `inc
dword [sym]` byte sequence. The loop matchers' induction recognition keys on the
`IncrementDecrement` op instead of AST `Block` contents.

**Exit criteria:** zero `ir.Access` and zero `ir.Block` producers in the corpus
census (`Block` survives as a node only if some non-access shape still needs it
— confirm by census; if none, delete it too), `ir.Index`/`ir.IndexAssign` gone
from the tree, byte gate 0-delta.

## Stage 3c: CSE / LICM / Horner over plans

**The unifying mechanism: a materialized Address is just a temp.** `ir.Address`
already has a `destination`. Today it is a phantom (emits nothing; terminals
fold the full computation). 3c gives it two emission modes:

- **Folded** (sole consumer, cheap re-derivation): emits nothing; the terminal
  absorbs the plan — exactly today's behavior.
- **Materialized** (multiple consumers or hoisted): the Address op itself emits
  the address computation (`lea`/scale/accumulate) into its destination, which
  is an ordinary IR temp regalloc already knows how to home; every consumer's
  terminal becomes `[reg]`/`[reg+disp]`. This is also where the fixed BX/SI
  scratch becomes allocator-chosen.

**The passes** (over plans, inside codegen, after planning):

1. **Address CSE** — dedupe `Address` ops with equal plans *and* unmodified
   contributing values: rewrite later consumers onto the first op's destination,
   mark it materialized. Address CSE needs **no alias analysis**: a plan depends
   only on the *values* of its base local and index terms, never on memory
   contents. Invalidation is "base/index local redefined between the two sites"
   — plus a conservative kill when the base local's address has been taken (`&p`
   exists) and a call or pointer-store intervenes. Target consumers: the
   `release`/`malloc` `block->next` / `block->prev` relink chains.
2. **LICM** — a plan whose contributing values are loop-invariant (no
   redefinition inside the `LoopBoundary` span, same address-taken caveat)
   hoists its materialization above the loop. Targets: `symbol_table[index]` in
   `symbol_add`'s copy loops (stride 32 — needs-arithmetic, so re-derivation is
   *not* free), the multidim row bases in `run_2d`/`run_3d`.
3. **Horner re-use** — the read-modify-write pair (`Load` then `Store` off equal
   adjacent plans, e.g. `m[i][j] = m[i][j] + k`) is the degenerate local-CSE
   case; falls out of pass 1.

**Cost model** (the ledger's risk #2, made explicit):

- A single-term SIB-encodable plan (`[sym+reg*4]`) is **never** CSE'd —
  re-derivation is free inside the operand; materializing can only add bytes.
  This is the addressing-mode-preservation guarantee (risk #1) stated as policy.
- A plan is a CSE/LICM **candidate** only when re-derivation emits instructions:
  needs-arithmetic scale, multi-term accumulation, or a register-base
  dereference (`mov bx, [p]` per access).
- Profit test per function: `(uses − 1) × re-derivation bytes > materialization
  bytes + pressure cost`, where pressure cost is determined by consulting
  regalloc — if homing the destination would evict or spill anything, skip. The
  byte gate's no-growth policy is the backstop.

**Gate policy:** 3c lands as gate `shrank` reports with deliberate baseline
refreshes (per ledger), one pass per PR so each shrink is attributable: CSE
(with Horner) → LICM.

## Phase plan

Five gated phases, each its own PR series. Every PR: byte gate 0-delta unless
stated, `cc_bits` 16/32, `cc_place` golden, unit suite, programs.

| Phase | Content | Gate expectation |
| --- | --- | --- |
| 1. Flip-over | AddressPlan + planner + native materializer; delete the AST re-seat (`_ir_address_with_index`, `_reseat_nested_subscript_indices`); fixed BX/SI scratch retained; clobber facts declared but unconsumed | 0-delta |
| 2. Clobber-aware re-admissions | regalloc consumes Address clobber sets; re-admit `BinaryOperation` + `Index` RHS and compound-index stores (ledger 1, 3, 4) | 0-delta or `shrank` on every ledger-listed function |
| 3. `Store.width` | width stamped from `Cast` at lowering; re-admit `Cast` RHS (ledger 2) | `readdir` 0-delta or shrank |
| 4. 3b.2 | simple subscripts onto Address+Load/Store; rep matchers rewritten; `ir.Index`/`ir.IndexAssign` deleted; bare-`Var` increments folded; `Block`/`Access` census → zero (delete if confirmed) | 0-delta |
| 5. 3c | CSE+Horner, then LICM; allocator-chosen address registers; cost model live | `shrank` + deliberate baseline refreshes |

Ordering rationale: phases 2 and 3 cash the ledger checks *early* — they are the
refactor's falsifiable claims. If they do not gate clean, the design is wrong
and we find out before investing in 4–5. Phase 4 precedes 5 so the 3c passes see
the final uniform IR.

## Testing strategy

- **TDD per slice** as established: source→IR asserts in `test_cc_ir.py`, plus a
  new `test_cc_address_plan.py` for planner unit coverage (shape → plan:
  displacement folding, term classification, 16-vs-32 SIB split, bitfield/decay
  carry-through).
- **The ledger is the acceptance suite** for phases 2–3: each re-admission's
  check is copied into the PR description and verified by a gate run.
- **3c semantic safety**: CSE/LICM are the first passes that can *miscompile*
  (wrong invalidation), not just regress bytes. Each gets adversarial unit tests
  (base redefined between uses, address-taken base + intervening call,
  loop-carried index) plus the QEMU runtime suites (`test_programs.py`, both
  filesystems) on every 3c PR, not just at the end.
- **Risk watch items**: (1) addressing-mode preservation is now *policy* (never
  materialize single-term SIB plans) — regression fails the gate loudly; (2)
  16-bit parity — every phase runs `cc_bits` at both widths, since 16-bit has no
  SIB and exercises the needs-arithmetic path everywhere.

## Out of scope

Recorded as deliberate exclusions:

- Alias-precise CSE across pointer stores (the conservative address-taken kill
  suffices for the corpus).
- Strength reduction of induction-variable addresses (post-3c candidate).
- Any IR-builder type table (explicitly rejected with Approach B).

## Phase-1 errata (2026-06-07, implementation `bboe/cc-native-address-phase1`)

Task 9's "delete the AST re-seat machinery" proved impossible at 0-delta within
phase 1: the array-of-pointers subscript chain (`name[i][j]` over `char
*name[N]`, 6 corpus sites in shell.c) requires a mid-chain element-pointer load
that has no plan model without an IR-visible type table, and four
diagnostic-owning arms (aliased-pointer and non-pointer-holder deref stores,
bitfield AddressOf/IncrementDecrement, undefined-name call slots) keep their
place-anchored CompileErrors on the legacy arms. `_ir_address_with_index` /
`_reseat_nested_subscript_indices` therefore survive as a NARROWED residual
path, locked by
`tests/unit/test_cc_address_plan.py::test_residual_address_census_matches_allowlist`
(`RESIDUAL_CENSUS_ALLOWLIST = {"user/programs/shell.c": 6}`). The deletion moves
to phase 4 (3b.2), which adds the chain-splitting element-pointer-load plan
extension. A controller-added slice (Task 9a) planned bare deref stores
`*pointer = leaf` natively to narrow the census to the array-of-pointers family
alone.

## Phase-2 errata (2026-06-07, implementation `bboe/cc-native-address-phase2`)

Phase 2 cashed ledger classes 1, 3, and 4 at byte parity (the full gate 0-delta;
`gettimeofday` byte-exact legacy), but the mechanism inventory differs from the
phase table's one-line summary:

- **Clobber visibility alone was insufficient.** The declared clobber sets
  (consumed as `RegisterConstraints.allowed` restrictions over per-instruction
  live-across sets) fix correctness — including a REAL latent miscompile found
  during implementation: a BX-homed temp live across a planned arrow-member load
  was destroyed by `_load_member_base` on HEAD — but the ledger regressions are
  dominated by the accumulator (AX), which is not allocatable.  The byte wins
  came from implementing the design's *evaluation-order contract* as **def
  sinking**: a single-use store-RHS def (or left-spine chain of defs, or
  compound-index TERM def) is suppressed at its IR position and replayed at the
  consuming terminal's legacy evaluation slot, reproducing the legacy bytes by
  construction.  Accumulator-aware single-use pinning covers the complementary
  preserve-AX orderings.
- **DX is handled by pool reservation**, mirroring the member-index BX
  precedent: in functions containing a DX-writing division, IR temps never home
  in DX, because the `dx_pinned` guard is function-global and a single DX home
  kills the div→mod remainder fusion everywhere.  The liveness-aware `dx_pinned`
  alternative was not needed.
- **Class 1 is scoped.** Leaf-operand and pure-binop-chain RHS are admitted; the
  `PlaceLoad`-operand subfamily (`p->bytes += q->bytes`) stays on `Access`
  permanently-for-now: legacy evaluates it with 1-byte push/pop stack
  choreography that slot-resident temps cannot match.  Recorded in the ledger as
  a residual.
- **Class 4 required a new plan flavor** (`member_index`, mirroring the legacy
  `_resolve_member_index` resolver) — the widened
  `_is_mixed_subscript_chain_store` family had no plan model in phase 1.
- **Pre-existing bugs surfaced by review** (not introduced, zero corpus
  exposure, tracked for follow-up): the `division_remainder` fusion is never
  invalidated by an intervening call or operand mutation between the fused div
  and mod — both shapes miscompile on `main` today.

## Phase-3 errata (2026-06-19, implementation `bboe/cc-naddr-phase3-cast`)

The `Store.width` premise in the "Terminals, Store.width, and the clobber model"
section is **wrong** and was not implemented. Reconnaissance on `main`
(6f545794) established:

- **`ir.Store` already has a vestigial `width: int` field** — constructed
  `width=0` at all six builder sites and entirely unused at emission. Store
  width is derived from the lvalue's field layout (`AddressPlan` /
  `MemoryOperand`), never from `Store.width`.
- **The `readdir` +6 was never a width drop.** The regressing store is
  `directory->entry.d_ino = (ino_t)d_ino` (`ino_t` = `uint32_t`), a dword cast
  into a dword field; the emitted store stays `mov [ebx], eax` at the same width
  before and after. The +6 is a cast-temp spill/reload across address resolution
  — the same class-1/3/4 anatomy phase 2 erased with the store-RHS sink. The
  bare-`Value` round-trip the section blames never occurs: a `Cast` store RHS
  lowers to `ir.Block(Assign(Cast))`, which preserves the cast in AST; the width
  was always correct.
- **Casts are codegen-identity for stores** (`generate_expression`'s `Cast` arm
  ignores `target_type`, no truncation) and the cast width already equals the
  field width at every corpus site, so a populated `Store.width` would override
  nothing.

**Corrected mechanism (implemented):** re-admit `Cast` by extending the value
sink (`_collect_ir_sunk_store_values`) to the `Block(Assign(Cast))` def shape
and replaying it at the terminal RHS slot — identical in spirit to the
`_collect_ir_sunk_index_terms` Block handling. No `Store.width` plumbing.

**`Store.width` disposition:** stays vestigial; it is a candidate for
**removal**, not population. It is not even the right hook for the one real
future need it gestured at — **narrowing-cast truncation** (`(char)0x1FF` should
be `0xFF`; cc.py currently never truncates). That correctness gap is
deliberately deferred and, when addressed, belongs in `generate_expression`'s
`Cast` codegen (a cast's truncation is the cast's property), not on the store op
(a store's width is the lvalue's property). The design conflated the two.

Phase table row 3 mechanism is "store-RHS cast sink", not "`Store.width`".
