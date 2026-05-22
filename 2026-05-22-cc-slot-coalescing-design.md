# 2026-05-22 — cc.py stack-slot liveness coalescing

Coalesce frame-allocated locals (named locals + IR-generated `_ir_*`
temporaries) whose live ranges don't overlap so they share a single stack slot.
The existing `LivenessAnalyzer` is generalized to walk both AST nodes and
post-IR `Instruction` nodes, then queried from two sites: the existing
register-pin sharing pass (AST input, unchanged output) and a new
slot-coalescing pass (post-IR input, includes `_ir_*` temps).

## Motivation

Call-heavy kernel functions pay a frame-size tax that's mostly IR-temp spills,
not named locals.  `sb16_open` is the working example: its 76-byte frame holds
three named ints (`i`, `phys`, `dma_count`), a handful of one-byte bitfield
struct locals, and a long tail of `_ir_*` temps that the IR builder materialises
around the cascade of `kernel_outb` and `sb16_dsp_out` calls.  Each temp gets
its own slot today even though most are dead before the next call returns.

The same shape appears in any function with a long flat sequence of
side-effecting calls — driver init paths, syscall dispatchers, the networking
send/receive helpers.  Saving even a third of those slots shrinks `kernel.bin`
measurably (every `sub esp, N` shrinks, and the per-call save/restore stack
discipline tightens when the frame fits in a smaller immediate encoding).

Two paths were considered earlier and merged into this design:

- **IR-temp-only coalescing** (narrower).  Coalesce `_ir_*` slots with each
  other.  Safer because IR temps are single-def and never address-taken, but
  leaves the wins from coalescing a dead named local with a later IR temp on the
  table.
- **Full slot register-allocation** (broader).  Coalesce any pair of frame
  locals (named or IR temp) whose live ranges don't overlap.  This is the
  version implemented here.

## Architecture

The coalescing logic itself is small.  The substance is in generalising
`LivenessAnalyzer` so the same analyzer answers both the existing register-pin
question and the new slot question.

### Generalized `LivenessAnalyzer`

Today the analyzer walks AST nodes only.  The body fed into the register-pin
sharing call site is the pre-IR AST; the analyzer's `_collect_use_def`,
`_wire_statement`, and `_build_control_flow_graph` methods dispatch on AST node
kinds.  `LivenessAnalysisError` is raised for any unrecognised AST shape so a
silent miscompile cannot result.

The generalisation:

- `_collect_use_def(node)` adds explicit cases for IR `Instruction` kinds:
  `Copy`, `BinaryOperation`, `Index`, `Call`, `Return`, `CondJump`, `Jump`,
  `Label`, `Block`.  For `Block(node=ast_node)` the recursion delegates back to
  the AST cases on the wrapped node so named-local accesses inside
  `Block`-interpolated AST are modelled the same way they are in the pre-IR
  walk.
- `_wire_statement` / `_build_control_flow_graph` add cases for IR control flow:
  `Jump` wires an unconditional successor, `CondJump` wires a conditional branch
  plus fallthrough, `Label` is a join.
- Fixed-point iteration and `interference()` are unchanged.  The use/def sets
  and CFG edges drive both the same way regardless of which input shape produced
  them.

The `LivenessAnalysisError` rule applies uniformly: any IR or AST shape not
modelled raises.  Both call sites surface the failure loudly rather than
silently fall back to a conservative answer. This is the explicit "fail loudly
on unknown shapes" choice — it costs one fix per new IR/AST kind added, in
exchange for guaranteeing that the coalescing pass never sees a partial graph.

### Two call sites

1. **Register pin sharing (existing).**  Runs early on the pre-IR AST body
   during pin candidate selection in `_choose_pin_assignments`. Only AST cases
   fire because the input has no IR `Instruction` nodes.  Output is
   byte-identical to today.
2. **Slot coalescing (new).**  Runs in `generate_function` after `scan_locals`
   and the IR-temp allocation loop, before prologue emission.  The post-IR body
   contains both `Block`-wrapped AST and bare IR `Instruction`s, so both case
   families fire.  The resulting interference graph covers `_ir_*` names
   alongside named locals.

The two call sites share every line of dataflow, CFG-walking, and
interference-computation code.  Only the body shape passed in differs.

### Slot coalescing pass

After the analyzer returns the interference adjacency dict, the new pass:

1. **Eligibility filter.**  A local is eligible iff all of:
   - Lives in `self.locals` at a positive offset (i.e. was placed via
     `allocate_local`).
   - Not in `self.pinned_register` (those don't occupy a frame slot).
   - Not in `self.local_stack_arrays` (array storage can't be aliased).
   - Not in `self.virtual_long_locals` (long pairs have their own layout
     discipline).
   - Not address-taken.  The `address_taken` set is already computed for
     register-pin selection in `compute_safe_pin_registers`; the slot pass
     reuses that set verbatim.
   - Not a parameter whose offset was assigned via the register-convention
     stack-spill path (those use negative offsets and a different layout).
2. **Group by size.**  Eligible names partition by slot width (1, 2, or 4 bytes
   — read from the existing per-name allocation). Different sizes never
   coalesce; same-size coalescing keeps alignment trivially correct.
3. **Greedy colouring.**  Within each size group, sort names by descending
   interference degree, tie-break by name for determinism.  For each name, pick
   the lowest-numbered colour whose existing members are all non-interfering
   with the candidate.  If none qualifies, allocate a new colour.  Each colour
   corresponds to one frame slot.
4. **Rewrite the layout.**  Walk the colours in a stable order (e.g. discovery
   order within each size group, larger sizes first to keep alignment monotone)
   and assign each colour a fresh offset using the existing
   `allocate_local`-style accumulator.  Update `self.locals[name]` for every
   name in the colour to that offset.  Recompute `self.frame_size` from the new
   high-water mark.

Downstream emission consults `self.locals[name]` for every load and store, so no
emission-side change is needed — coalesced names naturally read and write the
same `[ebp-N]` cell.

## Risks and mitigations

- **Silent miscompile.**  Two locals that actually do overlap sharing a slot
  would produce a use-after-free on the stack. Mitigations: aggressive
  raise-on-unknown in the generalised analyzer; address-taken filter excludes
  anything whose address may escape; same-size restriction.
- **Analyzer coverage drift.**  Adding a new IR or AST node kind without
  updating `_collect_use_def` now fails compilation instead of returning
  conservative answers.  This is the intended trade-off but means every IR/AST
  surface change must thread through the analyzer.
- **Interaction with peepholes.**  Some peepholes (notably the
  `_last_byte_store` collapse and the dead-frame elision) inspect `self.locals`
  offsets to identify candidate slots.  When two names now share an offset,
  those peepholes must still operate on the correct name.  The implementation
  plan verifies each peephole pass against this assumption.
- **Address-taken precision.**  Underestimating the address-taken set would let
  a slot containing a live pointer target get reused. Reusing the existing
  pin-selection set is conservative — anything that pin selection refused to
  consider pinning also refuses to coalesce.

## Verification

- `tests/test_asm.py`, `tests/test_bboefs.py`, and `tests/test_programs.py`
  (both `--filesystem bbfs` default and `--filesystem ext2`).
- `tests/unit/test_cc_codegen.py` and the analyzer's own unit tests; add new
  coverage for the IR-instruction use/def cases.
- Boot the OS in QEMU with `-serial stdio` and run a representative shell
  session including audio (`/dev/audio`), networking, and the assembler smoke
  tests.
- Size measurement: record `kernel.bin` and `asm.bin` sizes before and after;
  cite the `sb16_open` frame-size delta in the commit message.
- Build-side smoke: run `./make_os.sh` and ensure `KERNEL_RESERVED_BASE +
  0x23000 < 0xA0000` still holds (the build script asserts this anyway).

## Out of scope

- Coalescing across different sizes (e.g. fitting a byte local inside a 4-byte
  slot whose live range tolerates it).  Possible follow-up; risks alignment
  subtleties on 16-bit code.
- Coalescing with parameters that arrive on the stack at fixed positive offsets
  from BP.  Different layout discipline; not worth the bookkeeping.
- Moving the pin-selection pass to post-IR so both passes share the same body
  shape.  Bigger refactor; the current pre-IR/post-IR split is fine because the
  shared analyzer handles both inputs.
