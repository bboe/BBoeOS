# cc.py native-Address emission — phase 2 implementation plan

**For agentic workers:** REQUIRED SUB-SKILL: Use
superpowers:subagent-driven-development (recommended) or
superpowers:executing-plans to implement this plan task-by-task. Steps use
checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the register allocator consume the per-AddressPlan clobber facts
declared in phase 1, then re-admit ledger classes 1, 3, and 4 (`BinaryOperation`
and `Index` store RHS, compound-index stores) with the byte gate reporting
0-delta or shrank on every ledger-listed function.

**Architecture:** Three mechanism slices land first, each byte-gated: (a) plans
are computed eagerly per function before IR-temp coloring so clobber facts exist
at allocation time; (b) `regalloc.build_interference` exports per-instruction
live-across sets, and `_allocate_ir_temps` converts plan clobbers +
terminal-owned extras + division's EDX into `RegisterConstraints.allowed`
restrictions; (c) the IR-temp pinning heuristics become accumulator-aware across
folded `ir.Address` phantoms. The three ledger re-admissions then land as
one-predicate slices, each verified against the ledger's named functions.
Design: `2026-06-06-cc-native-address-emission-design.md` (phase table row 2).
Measured current state: re-admitting class 1 today grows 9 functions (+59 total,
worst `gettimeofday` +21); class 3 grows `readdir` +2; class 4 grows `_emit` +6
and `vsnprintf` +12; classes compose with no interaction terms (combined = exact
union, +75).

**Tech stack:** Python 3 (cc.py compiler), NASM output verified by
`tests/test_cc_function_sizes.py` (the per-function byte gate, 361 functions /
49 files), `tests/test_cc_bits.py` (16/32-bit matrix), `tests/test_cc_place.py`
(golden), `tests/unit/` (pytest).

**Branch:** `bboe/cc-native-address-phase2` off `main` (3e4160fe), worktree
`/home/ubuntu/bboeos/.claude/worktrees/next`.

**Gate discipline (every commit):** `python3 tests/test_cc_function_sizes.py`
must print `PASS  per-function byte-size gate (361 functions, 49 files)`. Where
a task legitimately shrinks a function, refresh the baseline in the same commit
and record the shrink in the commit message. Any GREW line is a stop-the-line
failure: report BLOCKED to the controller with the asm diff; never admit a
regression. Also per commit: `tests/unit/` (pytest, never bare `tests/`),
`python3 tests/test_cc_bits.py` (122/122), `python3 tests/test_cc_place.py`
(byte-identical golden, re-bless only for benign label renumbers and say so).
Pre-existing failures to ignore: `tests/test_cc_member_index_address.py` has 2
failures on main ("indexing 'row' (element size 12) not supported").

**Sorting conventions:** functions, methods, dataclass fields, constants, match
arms, isinstance tuples, AND class statements are strict-alphabetical within
their scope. `from __future__ import annotations` makes forward references free
— never order classes by dependency. No abbreviations in any identifier in any
language.

**Measured facts this plan builds on (2026-06-07 recon, HEAD = 3e4160fe):**

- Class-1 worst case anatomy (`gettimeofday`, `tv->tv_sec = total_ms / 1000`
  then `tv->tv_usec = (total_ms % 1000) * 1000`): the RHS temp gets pinned to
  EDX by the deferred-single-use heuristic (the folded `ir.Address` between def
  and `ir.Store` defeats the def+1 adjacency test at emission.py
  `_collect_ir_deferred_single_use_temps`); an EDX-homed pin makes `dx_pinned`
  true at the `/`/`%` arm (emission.py `_generate_binary_operation_expression`,
  the `/`/`%` arm), which (i) emits `push edx`/`pop edx` around the `div` and
  (ii) sets `division_remainder = None`, killing the div/mod remainder fusion
  and forcing a second `div` plus a spill/reload of `total_ms`.
- Class-4 anatomy (`_emit`, `s->buffer[s->length] = c`): the compound index temp
  is evaluated first and spilled to a new frame slot (+3 store, +3 reload); the
  legacy walk evaluated the index inline inside the protect-BX store terminal.
- `_ir_value_to_ast` (emission.py) maps an `_ir_*` temp to `Var(name=temp)`; the
  store terminal reads it through `generate_expression`'s Var arm — `mov eax,
  <reg>` when pinned, frame-slot load otherwise. A temp's spill-vs-register fate
  is decided entirely by `_allocate_ir_temps`.
- `push edx`/`pop edx` around `div`/`mul` is conditional on `dx_pinned` — any
  value in `self.pinned_register` homed in EDX, function-global, NOT
  live-range-aware.
- The allocator pool is `target.register_pool` = `("dx", "cx", "bx", "di")`
  16-bit / `("edx", "ecx", "ebx", "edi")` 32-bit; ECX and the frame pointer are
  blanket-reserved in `_allocate_ir_temps`; EBX is reserved only when
  `_body_has_member_index_access` matches an `Access`/`Block` payload — native
  member-store plans that clobber BX are NOT covered by that predicate (see Task
  3's adversarial test).
- `regalloc.build_interference`'s backward per-block walk already maintains the
  exact live set at every instruction (it counts `live_across_call` from it);
  exporting per-instruction live sets is a snapshot, no new dataflow.
- "Index RHS" (ledger class 3) is the distinct `ast_nodes.Index` node (bare
  `arr[i]` over a named variable, parser.py); `PlaceLoad(SubscriptPlace(...))`
  chains are already admitted. Re-admitting class 3 = adding `ast_nodes.Index`
  to the cc/ir.py `_is_byte_safe_store_rhs` tuple.
- Class 4's gate is `_is_mixed_subscript_chain_store` (cc/ir.py), three
  conditions: `_is_simple_index` per subscript, `subscript_count >= 2`, chain
  root must be `VariablePlace`. Reproducing the ledger requires relaxing all
  three (compound indices, count >= 1, `DereferencePlace(VariablePlace)` roots).

---

### Task 1: Export per-instruction live-across sets from `build_interference`

**Files:**
- Modify: `cc/regalloc.py` (`InterferenceResult`, `build_interference`)
- Test: `tests/unit/test_cc_regalloc.py`

- [ ] **Step 1: Write the failing test.** Read `tests/unit/test_cc_regalloc.py`
  first to copy its existing `ir.Function`-construction style exactly. Add (in
  alphabetical position):

```python
def test_build_interference_reports_live_across_sets():
    """live_across[id(I)] is the set of allocatable names live through I.

    t0 is defined before the BinaryOperation and used after it, so it is
    live across; t1 is the BinaryOperation's own destination and must NOT
    be in its live-across set (it is written by I, not live through it).
    """
    instructions = [
        ir.Copy(destination="t0", source=1, line=1),
        ir.BinaryOperation(destination="t1", left="t0", operation="+", right=2, line=2),
        ir.Return(value="t0", line=3),
        ir.Return(value="t1", line=4),
    ]
    function = _make_function(instructions)  # use the file's existing helper/style
    result = regalloc.build_interference(allocatable=frozenset({"t0", "t1"}), function=function)
    binary_operation = instructions[1]
    assert result.live_across[id(binary_operation)] == frozenset({"t0"})
```

Adapt the construction to the file's actual helper (if it builds `ir.Function`
inline, do the same; two `Return`s may need a `Label`/`Jump` shape the CFG
accepts — mirror an existing test's control-flow trick, or use `Copy` reads
instead of a second `Return`).

- [ ] **Step 2: Run it, watch it fail.** `python3 -m pytest
  tests/unit/test_cc_regalloc.py -x -q` — expect `AttributeError: ... no
  attribute 'live_across'`.

- [ ] **Step 3: Implement.** In `InterferenceResult`, add the field in
  alphabetical order (`graph`, `live_across`, `live_across_call`, `moves`) and
  document it:

```python
    live_across: dict[int, frozenset[str]]
```

Docstring addition: "``live_across`` maps ``id(instruction)`` to the allocatable
names live *through* that instruction (live-out minus its own defs). Keyed by
identity, not value — value-equal instructions at different program points must
not collide. Valid only while the analyzed ``ir.Function`` object is alive." In
`build_interference`'s backward walk, after `defs` is computed and before edges
are added:

```python
            live_across[id(instruction)] = frozenset(live - set(defs))
```

with `live_across: dict[int, frozenset[str]] = {}` initialized beside
`adjacency`, and threaded into the `InterferenceResult(...)` constructor
(keyword args alphabetical).

- [ ] **Step 4: Verify.** The new test passes; the whole file passes; byte gate
  PASS (nothing consumes the field yet).

- [ ] **Step 5: Commit.** `feat(cc): export per-instruction live-across sets
  from build_interference (phase 2)` + Co-Authored-By trailer.

---

### Task 2: Eager per-function Address planning before IR-temp coloring

**Files:**
- Modify: `cc/codegen/x86/emission.py` (`generate_function` IR section; the
  `case ir.Address` arm; new method `_plan_ir_addresses`)
- Test: `tests/unit/test_cc_address_plan.py`

The planner arms are pure given `scan_locals`/auto-pin state, which is final by
emission.py:4796 (`register_homes` snapshot). Planning must move to that point
so Task 3 has plans at allocation time. The `case ir.Address` arm becomes
consume-only.

- [ ] **Step 1: Read the current recording arm.** Read the `case ir.Address` arm
  (emission.py ~2270–2290) and note exactly: when the plan is recorded, when the
  op is recorded (plan is None OR `plan.bitfield is not None` — the bitfield
  ride-along), and where `self._ir_address_plans` / the op dict are reset per
  function. The eager pass must reproduce this recording logic verbatim.

- [ ] **Step 2: Write the failing test.** In
  `tests/unit/test_cc_address_plan.py` (alphabetical position; reuse the file's
  `_generate` harness):

```python
def test_plans_recorded_before_emission():
    """Every ir.Address plan exists before the first body instruction emits.

    Spy on _plan_ir_address: after generate_function returns, every call
    must have happened before the first emitted body line.  Mechanically:
    monkeypatch _plan_ir_address to record the generator's output length
    at call time; assert all recorded lengths equal the length captured
    at the start of body emission (i.e., planning happened in the
    pre-emission section of generate_function).
    """
```

Implement with the file's established monkeypatch style (the
`_generate_with_reseat_spy` helper is the template). A simpler equivalent
assertion is also acceptable: monkeypatch the `case ir.Address` arm's lazy path
to raise if it ever plans (e.g. assert the dict already contains the
destination), then compile a member-store source.

- [ ] **Step 3: Run it, watch it fail.**

- [ ] **Step 4: Implement.** New method (alphabetical placement among
  `_plan_*`):

```python
    def _plan_ir_addresses(self, body: list[ir.Instruction], /) -> None:
        """Plan every ``ir.Address`` in *body* (and Switch arms) eagerly.

        Runs in ``generate_function`` after auto-pin finalizes
        locals/params and before ``_allocate_ir_temps``, so the declared
        clobber facts exist at allocation time.  Recording semantics are
        identical to the (now consume-only) ``case ir.Address`` arm:
        plannable ops record their plan; unplannable ops and bitfield
        ride-alongs record the op for the legacy / diagnostic arms.
        """
        for instruction in body:
            if isinstance(instruction, ir.Switch):
                for switch_case in instruction.cases:
                    self._plan_ir_addresses(switch_case.body)
                continue
            if not isinstance(instruction, ir.Address):
                continue
            # ... exact copy of the recording logic from the case arm ...
```

Call it from `generate_function` immediately before
`self._allocate_ir_temps(ir_function=ir_function)` (guarded by `ir_function is
not None`; also handle the `ir_body is not None` non-allocator path if the dicts
are consumed there — check who reads the dicts when `BBOE_REGALLOC`-style paths
differ). Rewrite the `case ir.Address` arm to a consume-only assert (destination
must already be in one of the two dicts; raise `CompileError`-free internal
`AssertionError` text is fine — it is an invariant, not a user error).

**CompileError-ordering caveat:** planning can raise place-anchored
`CompileError`s (undefined names etc.). Moving planning earlier may reorder
which of several errors in one function surfaces first. Run the unit suite; if a
diagnostics test asserts a specific first-error and now sees a different one,
report DONE_WITH_CONCERNS with the test name — the controller decides (likely:
accept and update the test, since both errors are real).

- [ ] **Step 5: Verify.** New test passes;
  `test_residual_address_census_matches_allowlist` still passes; byte gate PASS
  0-delta; cc_bits 122/122; cc_place golden identical.

- [ ] **Step 6: Commit.** `refactor(cc): plan ir.Address ops eagerly before
  IR-temp coloring (phase 2)`.

---

### Task 3: Allocator consumes plan clobbers + terminal-owned extras

**Files:**
- Modify: `cc/codegen/x86/emission.py` (`_allocate_ir_temps`; new helper
  `_instruction_clobber_registers`)
- Test: `tests/unit/test_cc_address_plan.py`

A temp live across (or read by) a terminal whose plan materialization writes BX
must not be homed in BX. Today nothing enforces this for *native*
member/subscript terminals — `_body_has_member_index_access` only matches
`Access`/`Block` payloads. This task closes that hole and is the substrate for
Tasks 5–7.

- [ ] **Step 1: Adversarial probe (TDD for a possibly-pre-existing bug).** Write
  a C source that forces a BX-homed temp live across an arrow store:

```c
struct pair { int first; int second; };
int provoke(struct pair *pair_pointer, int seed) {
    int held = seed * 3;           /* IR temp chain; >= 3 uses below */
    pair_pointer->first = 7;       /* arrow store: materialization writes BX */
    return held + held * held;     /* uses after the store */
}
```

Iterate on the shape (add locals to occupy DX/DI via auto-pin, or more div-free
uses) until `python3 -m cc` output shows an `_ir_*` temp homed in `ebx` with a
`mov ebx, [...]` member-base load between its def and a use. Then determine
ground truth: does HEAD miscompile this (the temp's register is overwritten)?
Inspect the asm by hand. Record the verdict in the eventual test docstring and
commit message. If you cannot construct a BX-homed-across-store temp at all (the
heuristics never produce one), say so in the test docstring and keep the test as
a regression lock for the new constraint — report DONE_WITH_CONCERNS either way
so the controller can decide whether a pre-existing-miscompile note belongs in
the design doc errata.

- [ ] **Step 2: Write the failing unit test.** In
  `tests/unit/test_cc_address_plan.py`:

```python
def test_temp_homes_avoid_member_store_base_clobber():
    """A temp live across an arrow-store terminal is never homed in BX.

    The store's plan declares clobbers={'bx'} (the _load_member_base
    write); the allocator must exclude BX from that temp's allowed set.
    <verdict line from Step 1: pre-existing miscompile fixed here, or
    constraint lock for a shape the heuristics currently never produce.>
    """
```

Assert via the generator's `temp_pinned_registers` after compiling the probe
source: no temp that the interference result reports live-across the Store is
homed in the BX register name.

- [ ] **Step 3: Implement the clobber-fact helper.** New method (alphabetical
  placement):

```python
    def _instruction_clobber_registers(self, instruction: ir.Instruction, /) -> frozenset[str]:
        """Return the canonical 16-bit registers *instruction*'s emission writes.

        The write-set regalloc must keep live-through temps away from:

        - ``Load`` / ``Store`` / ``AddressOf`` / ``IncrementDecrement``
          consuming a planned address: the plan's declared ``clobbers``
          plus the subscript terminals' unconditional BX guard
          (``subscript_terminal`` plans emit it even with no dynamic
          terms).  IncrementDecrement's triple re-materialization repeats
          the same set, so no widening is needed.
        - the same terminals consuming an UNPLANNED address (the residual
          census + bitfield ride-alongs): conservative {'ax', 'bx', 'si'}
          — the legacy walk's full scratch footprint.
        - ``BinaryOperation`` with a DX-writing operation: {'dx'} (see
          the audit note below for which operations qualify).
        - ``IndirectCall``: NOTHING here by documented phase-1 convention
          (call_slot plans declare empty clobbers; the conservative
          full-pool call-site save governs the whole sequence, and the
          live-across-call cost model already prices it).
        - everything else: empty.

        Terminal-owned writes deliberately NOT listed: the accumulator
        (AX is not in the allocatable pool) and the RHS evaluation of a
        leaf (AX only).  Reads are not clobbers; the consuming terminal's
        own operand loads are its business.
        """
```

Implementation: match on instruction type; for the four address-consuming
terminals, look up `self._ir_address_plans.get(address_operand)`; planned →
`plan.clobbers | (frozenset({"bx"}) if plan.subscript_terminal else
frozenset())`; unplanned → `frozenset({"ax", "bx", "si"})`. **Audit step for the
BinaryOperation arm:** read `_generate_binary_operation_expression`'s `*`, `/`,
`%` arms and list exactly which operations have the `dx_pinned` push/pop guard;
those are the DX-writers (expect `/`, `%`, and the one-operand `mul` path of `*`
— confirm from the code, do not trust this parenthetical).

- [ ] **Step 4: Wire constraints into `_allocate_ir_temps`.** After `temp_pool`
  is computed and `result = regalloc.build_interference(...)` returns, before
  `constraints`:

```python
        # Clobber-aware homes (phase 2): a temp live across — or read
        # by — an instruction whose emission writes register R must not
        # be homed in R.  Reads are included conservatively because a
        # terminal may write its materialization registers before it
        # reads its operands (the subscript store evaluates the RHS,
        # then seeds BX, then reads the index term).
        allowed: dict[str, frozenset[str]] = {}
        pool_set = frozenset(temp_pool)
        for instruction in self._iterate_ir_instructions(body):
            clobbered = self._widen_clobber_registers(self._instruction_clobber_registers(instruction)) & pool_set
            if not clobbered:
                continue
            endangered = result.live_across.get(id(instruction), frozenset()) | frozenset(
                name for name in regalloc.instruction_uses(instruction=instruction) if name in allocatable_temps
            )
            for temp in endangered:
                if temp in allocatable_temps:
                    allowed[temp] = allowed.get(temp, pool_set) - clobbered
```

`_iterate_ir_instructions` is a small new helper (or reuse an existing flat
walker if one exists — check `_collect_ir_temps`'s recursion) yielding body
instructions including Switch arms. `_widen_clobber_registers` maps canonical
16-bit names to the pool's width — find the existing widening idiom near the
`BUILTIN_CLOBBERS` consumers (generator.py, the `_pinned_registers_to_save`
neighborhood) and reuse or extract it; do not hand-roll a second mapping table.
Then `constraints = regalloc.RegisterConstraints(allowed=allowed,
pool=temp_pool, precolored={})`.

**Interaction note:** a temp whose `allowed` set becomes empty spills — that is
correct and safe (memory homes are always sound). The DX restriction may
RELOCATE temps that today sit in EDX across a `div` paying `push`/`pop edx`;
relocation to another register is byte-identical per instruction, and the
vanished push/pop is a SHRINK. Both outcomes are acceptable; growth is not.

- [ ] **Step 5: Verify.** Unit tests pass (including the Task 1/2 tests); byte
  gate — expect PASS with possible `shrank` lines. For every shrank function,
  produce the asm diff, confirm it is exactly a vanished `push edx`/`pop edx` or
  a relocated home, refresh the baseline, and list each in the commit message.
  Any GREW → BLOCKED with the diff. cc_bits, cc_place, unit suite.

- [ ] **Step 6: Commit.** `feat(cc): regalloc consumes AddressPlan clobber facts
  (phase 2)`.

---

### Task 4: Preserve div/mod remainder fusion (function-global `dx_pinned`)

**Files:**
- Modify: `cc/codegen/x86/emission.py` (`_allocate_ir_temps` or the `/`/`%` arm
  — probe decides)
- Test: `tests/unit/test_cc_address_plan.py`

Task 3's constraint keeps temps *live across* a div out of EDX, but `dx_pinned`
is a function-global check over `self.pinned_register` values: a temp homed in
EDX anywhere in the function still kills `division_remainder` fusion at every
div. Class 1's `gettimeofday` claim needs the fusion alive.

- [ ] **Step 1: Probe.** Temporarily admit `BinaryOperation` in
  `_is_byte_safe_store_rhs` (do NOT commit), run the gate, and diff
  `gettimeofday`. With Task 3 in place, determine: is the RHS temp still pinned
  (the deferred-single-use def+1 test still sees the folded Address as a
  separator — expected), and if so to which register, and does the fusion
  survive? Record the diff.

- [ ] **Step 2: Choose the mechanism (decision rule).**
  - **(a) Pool-drop precedent** (mirrors the member-index BX reservation): in
    `_allocate_ir_temps`, when any body instruction is a DX-writing
    BinaryOperation, add the DX register to `reserved_registers`. Simple,
    function-scoped, costs one pool register only in div-containing functions.
  - **(b) Liveness-aware `dx_pinned`**: change the `/`/`%`/`*` arms to consult
    only values live at that point. Touches emission's hot paths and needs
    liveness at emission time — markedly riskier.

  Default to (a) unless the Step 1 probe shows (a) regressing existing functions
  (a temp that productively lives in EDX today in a div-containing function
  would spill or relocate). Run the gate with (a) alone (no re-admissions) to
  check exactly that.

- [ ] **Step 3: Write the failing test.**

```python
def test_division_remainder_fusion_survives_temp_allocation():
    """A div-containing function never homes an IR temp in EDX, so the
    div/mod remainder fusion (division_remainder) and the push/pop-free
    div sequence are preserved."""
```

Compile a two-statement div+mod source through the harness; assert no `_ir_*`
temp in `temp_pinned_registers` maps to the DX register and the emitted asm
contains exactly one `div` and no `push edx`.

- [ ] **Step 4: Implement, verify, commit.** Byte gate: 0-delta expected; shrank
  acceptable with diff review + baseline refresh; GREW → BLOCKED. Commit:
  `feat(cc): keep IR temps out of EDX in div/mod functions (phase 2)`.

---

### Task 5: Accumulator-aware pinning across folded Address phantoms

**Files:**
- Modify: `cc/codegen/x86/emission.py` (`_collect_ir_deferred_single_use_temps`)
- Test: `tests/unit/test_cc_address_plan.py`

A folded `ir.Address` emits nothing, but it occupies an instruction slot, so a
store-RHS temp's lone use looks like def+2 and the deferred-single-use heuristic
pins it — adding a `mov reg, eax` def bounce and a `mov eax, reg` read where the
legacy inline sequence kept the value in EAX throughout. That is the residual
class-1 growth once Tasks 3–4 land. The fix must be plan-fact-gated, NOT
blanket: for the already-admitted `PlaceLoad`-RHS sites (`release`/`malloc`
relinks) the store's chained base materialization CLOBBERS the accumulator
before the RHS read, so the pin is what makes them 0-delta today — blanket
phantom-skipping would regress them.

- [ ] **Step 1: Confirm the ordering facts.** Read
  `_emit_planned_member_store`'s four orderings and record, per ordering,
  whether the RHS is read from the accumulator BEFORE any materialization step
  that writes AX. Expected (verify, don't trust): `base_is_static` and
  `base_preserves_accumulator` orderings preserve AX up to the RHS read; the
  chained (`base_kind="plan"`) ordering does not. Also confirm the folded `case
  ir.Address` arm does not reset `ax_local` (it must not — it emits nothing).

- [ ] **Step 2: Write the failing test.**

```python
def test_single_use_store_rhs_temp_rides_accumulator():
    """A store-RHS temp whose consuming store preserves the accumulator
    up to the RHS read is NOT pinned: the folded ir.Address between def
    and use is transparent to the def+1 adjacency test, so the value
    rides ax_local with zero extra bytes (the gettimeofday class-1
    shape).  A chained-base store (accumulator-clobbering) keeps the
    pin (the release/malloc relink shape)."""
```

Two compile-and-inspect assertions: arrow-store-RHS temp absent from
`temp_pinned_registers`; chained-store-RHS temp present (or its emitted bytes
unchanged from HEAD).

- [ ] **Step 3: Implement.** In `_collect_ir_deferred_single_use_temps`, compute
  *effective* positions in which an `ir.Address` instruction is skipped **only
  when** (a) its destination is consumed by the temp's use instruction, and (b)
  its recorded plan exists and preserves the accumulator up to the RHS read per
  Step 1's table (express as a small predicate on the plan's `base_is_static` /
  `base_preserves_accumulator` / `base_kind` facts — name it
  `_plan_preserves_accumulator_for_store_rhs` and document the ordering table in
  its docstring). This requires the plans — which exist, because Task 2 made
  planning eager and this heuristic runs inside `_allocate_ir_temps`, after
  `_plan_ir_addresses`.

- [ ] **Step 4: Verify.** Byte gate: 0-delta expected (no admissions yet —
  currently-admitted leaf/PlaceLoad RHS shapes must not change; the predicate's
  chained-arm carve-out is what protects `release`/`malloc`). Any delta →
  investigate against the Step 1 ordering table before reporting.

- [ ] **Step 5: Commit.** `feat(cc): accumulator-aware single-use pinning across
  folded Address ops (phase 2)`.

---

### Task 6: Re-admit `Index` store RHS (ledger class 3)

**Files:**
- Modify: `cc/ir.py` (`_is_byte_safe_store_rhs`)
- Test: `tests/unit/test_cc_ir.py`

- [ ] **Step 1: Write the failing lowering test** in `tests/unit/test_cc_ir.py`
  (mirror the file's existing source→IR assertion style): a `p->member =
  arr[i];` source lowers to `Index` (RHS temp) + `Address` + `Store(value=temp)`
  with no `Access` producer.

- [ ] **Step 2: Implement.** Add `ast_nodes.Index` to the
  `_is_byte_safe_store_rhs` isinstance tuple (alphabetical: `Index` before
  `ast_nodes.Int`). Do NOT rewrite the docstring yet (Task 8 owns the rewrite);
  add one line noting class 3 re-admitted in phase 2.

- [ ] **Step 3: Ledger check.** Byte gate: `readdir` must be 0-delta or shrank;
  nothing else may change. The ledger's pre-phase-2 measurement was +2 (the
  Index-load temp spill). If it still grows: diff `readdir`, identify which
  mechanism (Task 3/4/5) failed to engage, and report BLOCKED with the diff —
  the mechanisms get fixed; the admission is never reverted-and-shipped-around.

- [ ] **Step 4: Commit** (baseline refresh if shrank): `feat(cc): re-admit Index
  store RHS (ledger class 3, phase 2)`. Copy the ledger check + measured result
  into the commit message.

---

### Task 7: Re-admit `BinaryOperation` store RHS (ledger class 1)

**Files:**
- Modify: `cc/ir.py` (`_is_byte_safe_store_rhs`)
- Test: `tests/unit/test_cc_ir.py`

- [ ] **Step 1: Failing lowering test**: `p->member = a / b;` lowers to
  `BinaryOperation` + `Address` + `Store(value=temp)`, no `Access`.

- [ ] **Step 2: Implement.** Add `ast_nodes.BinaryOperation` to the tuple (first
  position alphabetically).

- [ ] **Step 3: Ledger check.** Byte gate over the nine measured functions —
  `readdir`, `_emit_str`, `release`, `malloc`, `gettimeofday`, `symbol_add`,
  `strtol` ×3 — every one 0-delta or shrank, nothing else changed. Diff
  `gettimeofday` by hand regardless of the gate verdict and paste the
  before/after into the commit message: the claim is one `div`, no `push edx`,
  no `[ebp-12]` spill, the exact legacy sequence (or better).

- [ ] **Step 4: Runtime verification.** This admission touches arithmetic stores
  broadly: run `python3 tests/test_programs.py` (default bbfs) in addition to
  the standard per-commit suites.

- [ ] **Step 5: Commit**: `feat(cc): re-admit BinaryOperation store RHS (ledger
  class 1, phase 2)`.

---

### Task 8: Re-admit compound-index stores (ledger class 4)

**Files:**
- Modify: `cc/ir.py` (`_is_mixed_subscript_chain_store`),
  `cc/codegen/x86/emission.py` (term materialization)
- Test: `tests/unit/test_cc_ir.py`, `tests/unit/test_cc_address_plan.py`

The hardest slice: the index temp is consumed mid-materialization, after the RHS
evaluation has clobbered AX, so neither `ax_local` nor a plain pin reaches the
legacy byte count. The winning sequence is register-direct term accumulation: a
register-homed index temp accumulates as `add ebx, <reg>` directly, which also
makes the protect-BX RHS push/pop spill unnecessary for that term. Confined to
`_ir_*` temp index values so every existing shape (simple `Var`/`Int` indices
are bare locals, not temps) keeps its legacy bytes.

- [ ] **Step 1: Failing lowering test**: `s->buffer[s->length] = value;` (deref
  root, one subscript, compound index) lowers onto `Address` (index temp in
  `indices`) + `Store`, no `Access`.

- [ ] **Step 2: Relax the predicate.** In `_is_mixed_subscript_chain_store`:
  accept a `DereferencePlace(VariablePlace)` chain root alongside
  `VariablePlace`; lower the subscript count gate to `>= 1`; drop the
  `_is_simple_index` requirement for stores (the load-side
  `_is_nested_named_subscript_chain` keeps its gate — load relaxation is not in
  this phase). Keep the byte-safe-RHS gate as-is (classes 1/3 already admitted
  by Tasks 6–7). Update the predicate's docstring to describe the widened family
  precisely. **Planner coverage check:** the widened family must either plan
  (extend `_plan_mixed_subscript_chain` / `_plan_subscript_chain` for deref
  roots if they do not already accept them) or fall back safely to the legacy
  re-seat — compile the corpus and check the residual census test; if new
  residual sites appear, extend the allowlist ONLY if they emit byte-identically
  (gate proves it), otherwise extend the planner.

- [ ] **Step 3: Probe the gate.** Expect `_emit` and `vsnprintf` to grow (+6/+12
  measured) before the materializer work. Capture the diffs.

- [ ] **Step 4: Register-direct term accumulation.** In the term-accumulation
  path the subscript store terminal uses (follow `_emit_subscript_operand_store`
  → the accumulate helper): when a term's `index_value` is an `_ir_*` temp with
  a register home (`self.temp_pinned_registers`), emit the accumulate against
  that register directly (`add <base_register>, <temp_home>`), skipping the AX
  round-trip and the associated RHS protect spill for that term. Write the unit
  test first:

```python
def test_compound_index_store_accumulates_from_temp_register():
    """s->buffer[s->length] = c: the index temp's register home feeds
    the address accumulation directly (add ebx, <reg>), with no frame
    spill and no RHS push/pop protect for the term."""
```

This changes bytes ONLY on shapes admitted by Step 2 (temp-valued terms cannot
occur in previously-admitted shapes). The deferred-single-use heuristic must see
these index temps as pin-worthy — verify the Task 5 predicate does not
mistakenly suppress the pin here (the use is a term read, not an
accumulator-riding RHS; if suppression occurs, scope Task 5's predicate to
`Store.value` uses only).

- [ ] **Step 5: Ledger check.** Byte gate: `_emit` and `vsnprintf` 0-delta or
  shrank; nothing else changed. Hand-diff `_emit` and paste into the commit
  message. If the sequence cannot reach legacy parity, shrank is also success;
  if it still grows, BLOCKED with diffs — candidate next moves for the
  controller: direct register store of the RHS (`mov [ebx], <reg>`-class
  sequences), or narrowing the admitted family to the two ledger shapes.

- [ ] **Step 6: Runtime verification.** `python3 tests/test_programs.py` (stdio
  is on the hot path of every program).

- [ ] **Step 7: Commit**: `feat(cc): re-admit compound-index stores (ledger
  class 4, phase 2)`.

---

### Task 9: Documentation, ledger close-out, changelog

**Files:**
- Modify: `cc/ir.py` (`_is_byte_safe_store_rhs` docstring), `docs/CHANGELOG.md`
- Modify (design-specs branch, controller-owned):
  `2026-06-06-cc-native-address-emission-expected-byte-reductions.md`, design
  doc phase table

- [ ] **Step 1: Rewrite `_is_byte_safe_store_rhs`'s docstring.** It is no longer
  a wall: classes 1 and 3 are admitted; the remaining exclusions are class 2
  (`Cast`, phase 3 — `Store.width`) and any never-measured exotic RHS. State
  what is admitted, what phase 3 owns, and keep the pointer to the ledger. Same
  treatment for `_is_mixed_subscript_chain_store` if Task 8 left notes.

- [ ] **Step 2: CHANGELOG.** One Unreleased entry: clobber-aware IR-temp
  allocation + the three re-admissions, with the measured per-function outcomes
  (0-delta/shrank list).

- [ ] **Step 3 (controller, design-specs):** mark ledger classes 1/3/4 as CASHED
  with the measured results and commit references; phase table row 2 marked
  done; errata section if any mechanism deviated from the design (e.g. pool-drop
  vs liveness-aware `dx_pinned`).

- [ ] **Step 4: Final verification.** Full local suite sweep as in phase 1 (the
  28-suite matrix); byte gate summary; `tests/test_programs.py` both filesystems
  if time-boxed runs allow, default bbfs at minimum.

- [ ] **Step 5: Commit**: `docs: phase-2 close-out — clobber-aware allocation +
  ledger 1/3/4 cashed`.

---

## Execution notes for the controller

- Implementer model: opus for Tasks 3, 5, 8 (parity-critical,
  ordering-sensitive); sonnet acceptable for Tasks 1, 2, 6, 7, 9. Two-stage
  review per task (spec, then quality); clobber-set and ordering-table
  derivations get adversarial review as in phase 1.
- Tasks 6–8 are the design's falsifiable claims. A persistent GREW after
  mechanism iteration is a design-level finding: stop, write it up, bring it to
  Bryce — do not ship a workaround.
- The plan's byte-sequence predictions (Task 5's ordering table, Task 8's
  accumulate sequence) are *hypotheses with measured grounding*; the gate is the
  arbiter. Deviations go in DONE_WITH_CONCERNS reports, and material ones become
  design-doc errata in Task 9.
