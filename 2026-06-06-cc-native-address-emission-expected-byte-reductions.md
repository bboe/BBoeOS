# cc.py native-Address emission — expected byte reductions (measured ledger)

**Status:** Ledger (2026-06-06). Companion to the Stage 3b design
([2026-06-03-cc-place-refactor-plan5-stage3b-design.md](./2026-06-03-cc-place-refactor-plan5-stage3b-design.md))
and PR #590. This document records every byte regression measured against the
per-function byte gate while migrating the access family onto
`Address`/`Load`/`Store`/`AddressOf`/`IncrementDecrement`/`IndirectCall`, with
its root cause and the reason the **native-Address emission refactor** is
expected to eliminate it. Each entry is a checkable claim: when the refactor
lands, re-admit the class and the gate must report 0-delta or `shrank`.

## Why these regressions are artifacts, not design costs

Stage 3b.1 emits every folded terminal by **re-seating the AST shape and
re-running the legacy walker**. That makes byte-neutrality provable (identity
in, identity out) but has two structural consequences:

1. The legacy address walk is **opaque to regalloc** — its internal scratch
   register usage is invisible, so any IR temp that must stay live across it
   gets a conservative spill or save.
2. The `Value` round-trip through `_ir_value_to_ast` preserves only the leaf
   (`Var`/`Int`), **dropping AST-level attributes** like a cast's store width.

The native-Address refactor removes both: emission materializes each address
once from the pre-lowered `base_value`/`indices` leaves, terminals become `mov
[reg+disp]`-style operands over explicit registers, `Store.value` becomes a true
`Value` with an accurate `width`, and regalloc models the now-explicit clobbers.

## The ledger

All deltas measured on 2026-06-06 against the 361-function / 49-file gate, on
top of PR #587's IR-temp register allocator, by admitting each RHS/index class
into the store-fold predicates and running the gate (then reverting).

### 1. `BinaryOperation` store RHS — ~18 sites, worst +21 bytes

**CASHED (phase 2, 2026-06-07) — scoped.** Re-admitted for leaf-operand and
pure-binop-chain shapes; all nine measured functions gate 0-delta
(`gettimeofday` byte-exact legacy: one `div`, no `push edx`, no spills). Clobber
visibility alone was NOT sufficient: the design's evaluation-order contract had
to be implemented as store-RHS *sinking* (suppress the single-use def, replay it
at the terminal's legacy RHS slot, including left-spine chains with accumulator
continuity), plus a DX pool reservation in div/mod functions to preserve the
remainder fusion.  RESIDUAL: the `PlaceLoad`-operand subfamily (`p->bytes +=
q->bytes`, `release`/`malloc`) stays on `Access` — its legacy 1-byte push/pop
stack choreography cannot be matched by slot-resident temps;
`_is_byte_safe_binary_operation` documents the scope.

- **Sites:** `tv->tv_sec = total_ms / 1000` family (worst case, +21);
  regressions also in `readdir` / `_emit_str` / `release` / `malloc` /
  `symbol_add` / `strtol`. Census: 7× `Member(Deref)`, 5× `Deref`, 5× multidim,
  1× `Member(Subscript)` (user/ + tests/programs).
- **Mechanism:** the RHS temp must stay live ACROSS the opaque address
  resolution; the allocator cannot prove any register survives, so the temp
  spills to a frame slot (+`mov` pair) and `div`'s `edx` clobber forces a
  `push`/`pop edx`.
- **Why it disappears:** with address materialization explicit, the clobber set
  is visible; the temp lands in a register that genuinely does not conflict, or
  emission orders address-then-RHS exactly as the legacy inline walk did. The
  `push`/`pop edx` goes away because the allocator can simply avoid `edx` for
  values live across a `div`.
- **Check:** re-admit `BinaryOperation` in `_is_byte_safe_store_rhs`; gate must
  show 0-delta or shrink on all listed functions.

### 2. `Cast` store RHS — 3 sites, +6 bytes in `readdir`

**CASHED (phase 3, 2026-06-19) — corrected mechanism.** Re-admitted via the
store-RHS sink, NOT `Store.width`: recon falsified the width premise. The
`readdir` +6 was never a width drop (the `(ino_t)` cast stores a dword into a
dword field, `mov [ebx], eax` at the same width before and after) — it was a
cast-temp spill/reload across address resolution, the exact class-1/3/4 anatomy.
A `Cast` store RHS lowers to `ir.Block(Assign(Cast))`, so phase 3 extended
`_collect_ir_sunk_store_values` to collect and replay that Block def at the
terminal RHS slot (mirroring `_collect_ir_sunk_index_terms`). `readdir` 0-delta;
`grow_heap`/`strtol` were always 0-delta (pointer-width casts).

- **Sites:** `dirent.c` `readdir` (+6, the width-bearing `char` cast);
  `stdlib.c` `grow_heap` and `strtol` casts measured 0-delta in the same
  experiment (pointer-width casts — nothing to drop).
- **Mechanism:** the cast node selects the store width; the bare `Value`
  round-trip reconstructs only the inner leaf, so the re-seated store picks the
  wrong width path.
- **Why it disappears:** by construction — `Store.width` carries the width on
  the op itself, derived at lowering from the cast's `target_type`. No AST
  round-trip remains to drop it.
- **Check:** re-admit `Cast`; `readdir` must be 0-delta or shrink.

### 3. `Index` store RHS — 1 site, +2 bytes in `readdir`

**CASHED (phase 2, 2026-06-07).** `readdir` 248 → 248 via the store-RHS sink
(the first re-admission attempt without the sink measured +6 — worse than the
original +2 — because the consuming store's base materializes through the
accumulator and a register home only adds a bounce).

- **Mechanism:** same live-across-the-opaque-walk story as class 1, smaller
  because the RHS is a single load.
- **Why it disappears:** same as class 1.
- **Check:** re-admit `Index`; `readdir` must be 0-delta or shrink.

### 4. Compound-INDEX leaf-RHS stores — 2 sites, +6 / +12 bytes

**CASHED (phase 2, 2026-06-07).** `_emit` and `vsnprintf` 0-delta via a new
`member_index` AddressPlan flavor (mirroring the legacy `_resolve_member_index`
resolver exactly) plus index-TERM sinking: the single-use index def (a planned
`Load`, or an `ir.Block` conditional) is suppressed and replayed at the
resolver's legacy index slot.

- **Sites:** `stdio.c` `_emit` (+6, `s->buf[s->len] = c`) and `vsnprintf` (+12).
  Measured under a combined relaxation (compound indices + 1-subscript chains +
  deref roots), so the attribution is the class, not the exact instruction
  split.
- **Mechanism:** unlike `IndirectCall`'s compound index (proven 0-delta in PR
  #590 — consumed immediately by the address scale), a store's index temp
  must coexist with the RHS evaluation, so one of the two crosses the opaque
  walk.
- **Why it disappears:** same clobber-visibility argument; with explicit address
  code the index temp and RHS value occupy two known registers.
- **Check:** relax the store predicates to compound indices; `_emit` and
  `vsnprintf` must be 0-delta or shrink.

## Expected reductions beyond parity (the 3c payoff)

Parity on the four classes above is the *floor*. The same refactor is what makes
Stage 3c produce actual shrinks — today even a CSE'd `Address` value would be
re-materialized at every terminal, so these are unreachable until addresses
materialize once:

- **Address CSE:** `p->field` load/store pairs (the `release` / `malloc` relink
  chains touch `block->next` / `block->prev` repeatedly) share one base
  materialization.
- **LICM:** loop-invariant address math hoists (`symbol_table[index]` inside
  `symbol_add`'s copy loops; the multidim row base in `run_2d` / `run_3d`).
- **Horner re-use:** `m[i][j]` read-modify-write pairs stop recomputing the
  row-major walk.

These land as gate `shrank` reports + a deliberate baseline refresh, per gate
policy.

## Risks that could void the expectations

1. **Addressing-mode preservation.** The legacy emitter folds aggressively:
   `call [table+eax*4]`, `mov [table+reg*4], eax` are single instructions.
   Native emission must keep producing scaled-index memory operands rather than
   decomposing into `lea` + plain access, or the flip-over itself grows bytes.
   This is the central design constraint.
2. **CSE cost model.** A shared address occupies a register, while re-deriving
   `[base+index*scale+disp]` inside an instruction is free on x86. Naive CSE in
   register-tight functions can cost more than it saves; the gate's no-growth
   policy is the enforcement.

## Provenance

- Measurements: PR #590 development session, 2026-06-06; methodology in the
  `_is_byte_safe_store_rhs` docstring (cc/ir.py), which carries the same numbers
  and points here.
- Census end state after PR #590: 24 `Access` producers (all in classes 1-4
  above), 80 `Block` producers (bare-`Var` increments, the Stage 3b.2
  rep-matcher residents).
