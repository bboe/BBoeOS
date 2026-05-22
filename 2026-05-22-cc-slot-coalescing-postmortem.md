# 2026-05-22 — Post-mortem: cc.py stack-slot liveness coalescing

The slot-coalescing approach proposed in
[`2026-05-22-cc-slot-coalescing-design.md`](2026-05-22-cc-slot-coalescing-design.md)
was implemented end-to-end on branch `bboe/cc-slot-coalescing` and then
abandoned.  This document records what happened so the next person who looks at
`sb16_open`'s 76-byte frame doesn't re-walk the same path.

## Outcome

- All test suites green at branch tip.
- `sb16_open` frame: 76 → 76 (no change).
- `kernel.bin`: 40966 → 41094 (+128 bytes regression).
- Branch dropped; only the latent bb-asm encoding fix it surfaced was salvaged
  (PR #487).

## What went wrong

The spec's motivating premise — "`sb16_open`'s 76-byte frame is mostly IR temps
spilled around the cascade of `kernel_outb` / `sb16_dsp_out` calls" — was wrong.
The actual frame composition is:

- Three named ints (`i`, `phys`, `dma_count`): ~12 bytes.
- Four address-taken struct locals (`imr`, `mask_ch1`, `unmask_ch1`,
  `mode_audio`), each read via `*(u8 *)&imr` style casts so cc.py can't prove
  the address doesn't escape: ~60 bytes.
- A handful of `_ir_*` temps, most of them carried across the call sequence
  (saved via `push edx` / `pop edx` around each call, not via the frame).

Liveness-based slot coalescing can only touch the first bucket.  The
address-taken filter correctly excludes the structs.  Coalescing the three named
ints recovers maybe 8 of the 76 bytes in principle, but in practice their live
ranges overlap enough that even that win didn't materialise.

## Secondary problem: cross-kind coalescing fights the peepholes

The full implementation (Phase 1 analyzer generalisation + Phase 2 coalescing
pass + bb-asm short-disp fix + named-vs-IR partition removal) cost 128 bytes net
in `kernel.bin`.

Source: `peephole_memory_arithmetic`'s third pass fuses `mov ax, D; op ax, [X];
mov D, ax` into `mov ax, [X]; op D, ax`.  It bails when `rhs == source` (a
precise but over-conservative aliasing guard).  Cross-kind coalescing — letting
a named local share a slot with an `_ir_*` temp — makes more `source` and `rhs`
resolve to the same `[bp-N]` cell, suppressing the fusion.  The peephole
correctness condition could be tightened (the fusion is safe when the slot is
dead before the store), but doing so requires liveness information at peephole
time, which the peephole engine doesn't currently have.

## What we kept

The bb-self-hosted assembler's `emit_alu_mem_imm` was always emitting the
`[disp32]` form for memory operands, missing the short `[ebp+disp8]` encoding
NASM picks.  It also silently mis-encoded `add dword [mem], <reg>` by resolving
the register as a label.

The investigation surfaced both bugs.  The fix is independently valuable (it
would have unlocked any future codegen change that emits `[ebp+disp8]` more
often) and shipped as PR #487.

## Lessons / next steps

1. **Measure before designing.**  The spec premise should have been verified
   against the actual IR for `sb16_open` (count `_ir_*` allocations, count
   address-taken locals, count peephole-eliminated slot accesses) before
   committing to slot coalescing as the approach.  A 30-second `grep _ir_ <
   ir-dump` would have shown the premise was wrong.
2. **The real `sb16_open` opportunity is the address-taken structs.** Every
   `*(u8 *)&imr` access is constant-folded down to an `imm` — the struct never
   has to materialise on the frame.  A pass that recognises "every field access
   on this struct is dropped through the bitfield constant-fold peepholes" could
   elide the storage entirely.  This is closer to constant propagation than
   register allocation.
3. **Peephole-engine coupling is the real cost ceiling on any slot-renaming
   optimisation.**  Several peepholes key off `[bp-N]`-identity (write counts,
   dead-temp detection, fuse-modify-in-place, the `inc esi` micro-pattern).  Any
   future optimisation that renames or re-roles slots will have to either teach
   the peephole engine about colour groups or live with the pessimisation.
   Worth deciding which before writing more code.
4. **Liveness analyzer generalisation has no consumer.**  Phase 1 (the
   `LivenessAnalyzer` walking IR `Instruction` nodes) was clean and tested but
   had no caller besides Phase 2.  Reverted with the rest.  If a future pass
   wants per-IR-instruction liveness it can reintroduce the extension.

## Cross-references

- Design:
  [`2026-05-22-cc-slot-coalescing-design.md`](2026-05-22-cc-slot-coalescing-design.md)
- Plan:
  [`2026-05-22-cc-slot-coalescing-plan.md`](2026-05-22-cc-slot-coalescing-plan.md)
- bb-asm fix PR: bboe/BBoeOS#487
- Abandoned implementation: branch `bboe/cc-slot-coalescing` (deleted)
