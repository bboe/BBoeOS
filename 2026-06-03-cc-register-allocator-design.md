# cc.py Register Allocator Design: unified graph-coloring allocator over the IR/CFG

**Status:** Designed (2026-06-03). Ready for implementation planning. Prerequisite
for Plan 5 Stage 3b (see [[2026-06-03-cc-place-refactor-plan5-stage3b-design]]).

**Goal:** Replace cc.py's heuristic AST-based auto-pin with one principled
graph-coloring register allocator that assigns register/spill *homes* to **all**
named values — user locals, parameters, **and `_ir_*` IR temporaries** —
uniformly. The immediate driver is to make single-use IR temps register-resident
instead of always spilling to a frame slot, which is the prerequisite that makes
Stage 3b's SSA-value operand-lowering substrate byte-viable.

## Motivation: the no-register-allocator finding

cc.py's x86 backend has **no register allocator**. Every IR temp `_ir_N` is
pre-allocated a frame slot (`generate_function` → `_collect_ir_temps` →
`allocate_local`) and lives in memory; the peephole only removes *same-register*
store/reload. So a compound access leaf like `arr[i + 1]` compiles to

```asm
inc eax              ; i + 1 in EAX
mov [ebp-8], eax     ; SPILL the temp to a frame slot
mov esi, [ebp-8]     ; RELOAD into the index register
shl esi, 2
mov eax, [_g_arr+esi]
```

— a spill/reload pair that survives the peephole (the store is to `eax`, the
reload into `esi`, a different register). Consequently, Stage 3b's plan to lift
an access's dynamic leaves to optimizer-visible IR `Value`s would **add a
spill/reload per compound leaf versus the inline evaluation `resolve_address`
does today, failing the per-function byte-efficiency gate by construction.**
(cc.py's own Stage-2 design already warned: "operand lowering alone … is pure
byte cost that fails the gate.") The fix is to give single-use temps a register
home; rather than a targeted patch, we build the real thing.

## What already exists (this is an extension, not greenfield)

cc.py already has most of an allocator — it just never covers IR temps:

- **Auto-pin** (`_select_auto_pin_candidates`, `compute_safe_pin_registers`,
  `cc/codegen/x86/generator.py`) keeps high-traffic **locals/params** in
  registers (`BX/CX/DX/DI`, + `BP` in `main`), via a greedy reference-count
  ranking matched to a register pool ordered by clobber cost.
- A **clobber-cost economic model**: a value is pinned only when
  `refs > effective_cost`, where `effective_cost` is the per-call `push`/`pop`
  overhead minus the pre-first-store clobbers that PR #454's liveness pre-pass
  elides (`register_clobber_counts`). Liveness-driven *sharing* lets
  non-overlapping locals reuse a register when candidates exceed the pool.
- A **`LivenessAnalyzer`** (`cc/codegen/liveness.py`) computing per-statement
  live-in/out and an **interference graph** over the AST (with a known gap:
  `MemberPlace`-rooted places are unmodeled, so such functions skip auto-pin).
- Full **emission wiring**: `pinned_register: dict[str,str]`; the prologue loads
  pinned params from caller slots; `emit_store_local` / variable reads consult
  `pinned_register`; call-sites `push`/`pop` the clobbered pins.
- An **SSA/CFG layer** (`cc/ssa.py`, `cc/cfg.py`) with GVN/copy-prop, and all the
  hard constraints already modeled piecemeal: the `register_pool`, the 16-bit
  `[BP+BX]`-illegal index restriction, byte-alias availability
  (`SI/DI/BP/SP` have none), the fastcall regparm registers (`EAX/EDX/ECX`), and
  per-builtin / user-call clobber sets.

The gap is precise: auto-pin candidates are scanned from the **AST body**, so
`_ir_*` temps (created during IR building) are never eligible and always spill.

## Decisions taken during brainstorming

1. **Unified principled allocator, replacing the heuristic auto-pin** (not a
   minimal extension): one allocator assigns homes to locals, params, and IR
   temps uniformly.
2. **Graph coloring (Chaitin-Briggs)** — builds on the interference graph cc.py
   already computes; precolored fixed registers model the ABI/accumulator/16-bit
   constraints cleanly; subsumes today's interference-sharing.
3. **Operate on the flat IR + a CFG-level liveness pass** (Option (a)), not in
   SSA form (Option (b)). Classic Chaitin works on non-SSA given
   liveness+interference; `optimize_ssa` has already run its GVN/copy-prop and
   destructed back to flat IR, so the allocator is a clean backend pass with no
   SSA-destruction entanglement, uniform for multi-def locals and single-def
   temps. The new IR-level liveness supersedes the AST `LivenessAnalyzer`'s role
   (and closes its `MemberPlace` gap, since it works on lowered IR).
4. **Emission stays accumulator-based.** The allocator assigns *homes* (a
   physical register or a spill slot); AX/EAX remains the evaluation scratch.
   No virtual-register instruction-selection rewrite (the decision-spike already
   ruled out building isel).

## Architecture

### Pipeline placement
A new **backend pass between IR-optimization and emission**, per function, over
the flat IR + its CFG (`cc.cfg.build_cfg`). It produces a **home map**
`value → PhysReg | SpillSlot` that emission consults. It does not rewrite
instruction selection.

### Allocation units & precoloring
Interference-graph nodes = every named value live beyond the trivial
def-then-immediate-use window: user locals/params (as today) **plus `_ir_*`
temps** (new). **Precolored fixed nodes** capture the hard constraints:

- **AX/EAX** — accumulator/scratch and return register, so a value can be
  *coalesced* to stay in AX when its live range allows and is forced out of AX
  across the next AX-defining op.
- **Fastcall regparm** (`EAX/EDX/ECX`), **call-clobber sets** (per-builtin + the
  full `register_pool` for user calls), **16-bit index legality** (index must be
  `SI/DI`; no `[BP+BX]`), **byte-alias availability** (`SI/DI/BP/SP` have no byte
  alias → a byte-typed value cannot color there), **`BP` available only in
  `main`**, `SP` reserved.

### Liveness / interference
A new **IR-level liveness** over the CFG — per-instruction live-in/out via
backward dataflow (reusing the control-flow-shaped dataflow the AST analyzer
already implements) — yields the interference graph and the **live-across-call**
information that drives both spill cost and the precolored-clobber economics.

## Coloring (Chaitin-Briggs) with cc.py's cost economics

cc.py's call-clobber model is **economic, not a hard constraint**: a value may
live in a clobbered register and pay a `push`/`pop` per crossed call. So coloring
separates hard constraints (the graph) from soft costs (selection + spill):

- **Interference graph (hard):** edge between two simultaneously-live values;
  edges to precolored nodes for the true hard limits (byte value ✗ `SI/DI/BP`;
  16-bit index value must be `SI/DI`; `SP`/`BP` reserved except `main`; a value
  live at a fastcall site vs. the regparm registers it occupies).
- **Simplify / optimistic-spill / select (Briggs):** remove `< K`-degree nodes
  and push; optimistically push high-degree nodes; on select assign a legal
  color.
- **Soft cost at selection:** among the colors legal for a node, choose the one
  with the **lowest save cost** for that value's call-crossings — reusing
  `register_clobber_counts` + the pre-first-store `push`/`pop` elision. A node
  that cannot be colored, or whose `refs ≤ effective_cost` so no register pays
  off, **actually spills** to a frame slot via the existing `allocate_local`
  path.
- **Coalescing (conservative / Briggs):** fold move-related values — the
  `mov reg, eax` accumulator move-outs and `Copy(a, b)` chains — onto one
  register. This is what **erases the targeted spill/reload pairs**: a temp
  defined in AX and used once coalesces to stay in AX (zero moves) or onto its
  consumer's home.

`K` = the existing pool (`DX, CX, BX, DI`, + `BP` in `main`, + `SI` where 16-bit
index legality permits), with AX precolored as scratch/return.

## Emission integration — mostly already wired

The home map **generalizes today's `pinned_register: dict[str,str]`** to also key
`_ir_*` temps; the existing machinery then does the work:

- `_lower_ir_instruction` → `emit_store_local(name=temp)` already writes to
  `pinned_register[temp]` when present → **the spill store vanishes**;
  `_ir_value_to_ast(temp)` → `Var(temp)` → register read → **the reload
  vanishes**.
- Frame pre-allocation allocates slots **only for spilled values**, so
  register-homed temps shrink `frame_size` → a smaller `sub esp, N` prologue
  (a byte win).
- The pinned-param prologue loads and the call-site `push`/`pop` of clobbered
  homes (`_pinned_registers_to_save`) already exist; they now also cover temp
  homes live across calls.
- AX-homed values are only the very-short-lived (coalesced def→use) temps; any
  intervening AX-defining op interferes with the AX precolor and forces a real
  home — handled by the graph, not a special case.

## Subsuming the existing auto-pin

The coloring allocator becomes the **single** authority for register homes;
`_select_auto_pin_candidates` and the greedy ranking are **deleted**, but their
**economics are retained** (the `register_clobber_counts` cost model, the
pre-first-store `push`/`pop` elision, the `main`-pins-`BP` extension), folded into
coloring's soft-cost selection and spill gate. The crux risk is **parity**:
coloring must match-or-beat the hand-tuned heuristic's byte wins on every
function, since the gate forbids any per-function increase.

## Phasing — four gated PRs

- **PR 1 — engine, unwired.** IR-level liveness/interference over the CFG + the
  cost-aware Briggs coloring, as a pure module with unit tests on synthetic IR
  (known interference → expected coloring; precolor legality; spill under
  pressure; coalescing). No codegen consumes it → **byte-neutral by
  construction**.
- **PR 2 — switch locals/params to the engine; delete the auto-pin heuristic.**
  Gate: per-function bytes **≤ current baseline** across the whole corpus. The
  **parity step and dominant risk** — proves coloring matches the tuned heuristic
  before temps enter. IR temps still spill here.
- **PR 3 — extend allocation to `_ir_*` temps.** The actual goal:
  register-resident temps. Gate: ≤ baseline, **expecting broad decreases**
  (spills + frame slots removed). Baseline regenerated wholesale; the gate
  enforces *no function increases* and the PR reports the aggregate shrink.
- **PR 4 — cleanup.** Remove now-dead auto-pin code; rename `pinned_register` →
  the unified home map; retire the AST `LivenessAnalyzer` if fully superseded.

Then **Stage 3b/3c resume** on register-resident temps.

## Byte-gate strategy under churn

`tests/test_cc_function_sizes.py` enforces per-function `.text.<name>` size ≤
baseline (`tests/golden/cc_function_sizes_baseline.json`; `BBOE_UPDATE_SIZES=1`
regenerates) — no eyeballing: any single function growing fails. **Self-host is
robust to the churn**: `tests/test_asm.py` checks that asm.c-compiled-by-cc.py
still *correctly assembles* programs byte-for-byte vs nasm — it does **not**
require asm.c's own compiled bytes to be stable, so the allocator may compile
asm.c to smaller code as long as the resulting assembler stays correct.

## Testing & gate

- **Unit (PR 1):** liveness/interference and coloring on synthetic IR — known
  interference → expected coloring, precolor legality (byte ✗ `SI/DI`, 16-bit
  index ⇒ `SI/DI`), spill under register pressure, conservative coalescing of
  `mov reg, eax` / `Copy` chains, live-across-call save accounting.
- **Byte gate:** `tests/test_cc_function_sizes.py` per the phasing above.
- **Tripwire:** `tests/test_cc_place.py` golden (`cc_place_index_member.asm`;
  `BBOE_UPDATE_GOLDEN=1`) — it will churn; re-bless once per landing PR.
- **Correctness matrix** (the "run full CI matrix locally on big changes" rule):
  `tests/test_asm.py` (incl. self-host), `tests/test_programs.py` bbfs + ext2
  incl. `e2fsck`, `tests/test_bboefs.py`, `tests/test_cc_bits.py` (assembles
  every cc program at `--bits 16` **and** 32 — catches illegal index registers
  NASM rejects), the `rep_loops` runtime test, and the `tests/unit/` suite.

## Risk register (priority order)

1. **Locals parity vs the tuned auto-pin (PR 2)** — coloring must not regress any
   function's bytes. Mitigated by reusing the existing economics and tuning
   coalescing / spill-cost until ≤. Fallback if a few functions stall: a
   **coexistence interim** — keep auto-pin for locals, let the allocator take
   only the leftover pool for temps — which unblocks 3b without forcing full
   parity.
2. **Precolor correctness** — a byte value colored to `SI/DI`, or a 16-bit index
   not in `SI/DI`, is a NASM reject or miscompile. `tests/test_cc_bits.py` (both
   bit widths) is the net.
3. **Call-clobber save correctness** for temp homes live across calls — a missed
   `push`/`pop` save miscompiles. The runtime suites are the net.
4. **IR-level liveness correctness** across switch / goto / loops — a liveness
   miss → two values share a register → miscompile.
5. **regparm / `out_register` ABI** interaction at call boundaries — homes must
   respect the fastcall regparm registers and `in_register` params.

## Out of scope

- A virtual-register instruction-selection rewrite (emission stays
  accumulator-based).
- SSA-form (live-range-split) allocation — Option (b); the flat-IR coloring is
  sufficient and lower-risk.
- The Stage 3b/3c operand lowering itself — this allocator is only the
  prerequisite that makes it byte-viable.
