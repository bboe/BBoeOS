# cc.py Register Allocator — PR 2 Design (wire locals/params; delete auto-pin)

**Status:** Approved (2026-06-03). Implementation pending.
**Parent design:** `2026-06-03-cc-register-allocator-design.md` (overall four-PR plan).
**PR 1:** the unwired engine `cc/regalloc.py` — merged (#586).

## Goal

Make the graph-coloring engine in `cc/regalloc.py` the **single authority** for
the register homes of **user locals and parameters**, and **delete** the
hand-tuned auto-pin heuristic (`_select_auto_pin_candidates` and its tally
helpers). `_ir_*` IR temporaries still spill to frame slots in this PR — putting
them in registers is PR 3. This is the **parity step and dominant risk**: the
byte gate (`tests/test_cc_function_sizes.py`) forbids any per-function `.text`
increase, so coloring must match-or-beat the tuned heuristic on every function.

## Decisions (this PR)

- **Parity policy:** aim for full parity. For the few functions that cannot reach
  `≤ baseline` after reasonable cost-model tuning, **re-bless the baseline** only
  when each regression is small, individually justified in the PR description,
  and the corpus total stays `≤ baseline`. auto-pin is **fully deleted** at
  landing regardless (no coexistence interim).
- **Convergence evidence:** **capture the heuristic's per-function
  `{name: register}` map as a golden snapshot** before deleting it, and assert
  the allocator reproduces it (with a documented exceptions list). Bytes remain
  the hard gate; the golden is the register-assignment tripwire.

## Scope & end state

- `cc/regalloc.py` colors **user locals + params only**. `allocatable` excludes
  `_ir_*` temps in this PR.
- The **emission wiring is unchanged**: `Allocation.homes` populates the existing
  `self.pinned_register` dict; spilled locals/params take frame slots via the
  existing `allocate_local` path. No instruction-selection / emission rewrite —
  emission stays accumulator-based with AX scratch.
- Deleted at landing: `_select_auto_pin_candidates`, `_tally_auto_pin_counts`,
  `_tally_pre_store_clobbers`, `_rank_candidates`, `_collect_auto_pin_body_candidates`,
  `AutoPinTallyState`, and the liveness-based sharing pass. The pure *counting*
  logic they contained is retained (extracted into reusable helpers) because it
  feeds the engine's cost model.

## The cost-input seam (AST economics -> engine inputs)

The engine colors over `ir.Function` (IR instructions + CFG, via
`build_interference`), but the economics live in the AST today. PR 2 keeps the
AST-derived economics as the *cost inputs* and uses the engine's IR-CFG
interference graph for *coloring*. For each user function we construct:

- **`allocatable: frozenset[str]`** = user locals + params, **minus** the
  heuristic's exclusions (address-taken vars, array params, complex-init
  expression-temps). Mirrors the old candidate collection exactly.
- **`CostModel.spill_benefit: dict[str, int]`** = the existing per-variable
  reference counts (the auto-pin ref tally, kept as a pure counting helper). A
  value whose benefit does not exceed its chosen register's save cost is spilled.
- **`CostModel.register_save_cost: dict[str, dict[str, int]]`** = per-variable
  `{register: push/pop save cost}`, derived from `register_clobber_counts` minus
  the pre-first-store `push`/`pop` elision (the PR #454 economics, retained).
- **`RegisterConstraints`:**
  - `pool` = the target's allocatable register pool (and `BP` for `main`, as the
    heuristic does today).
  - `precolored` = regparm / `in_register` parameters that arrive in a fixed
    register (EAX/EDX/ECX).
  - `allowed` = per-value legal register subset encoding **byte-alias** legality
    and **16-bit index** legality (a 16-bit index must be `SI`/`DI`; a byte value
    may not live in `SI`/`DI`).

Names of locals/params are stable between the AST and IR forms, so the AST-keyed
cost dicts key correctly against the engine's IR-keyed interference graph.

## Golden-homes parity harness

- Add an env-gated capture hook (e.g. `BBOE_DUMP_HOMES=1`) that, **while auto-pin
  still drives homes**, dumps per-function `{name: register}` for the whole
  userland corpus to `tests/golden/cc_register_homes_baseline.json`. Commit it.
- A new test (`tests/test_cc_register_homes.py`) asserts the **allocator**
  reproduces that map. Register-identity churn (a var landing in `DI` vs `SI`,
  byte-identical) is permitted via a small **documented-exceptions** list inside
  the test; anything that changes *bytes* is caught by `test_cc_function_sizes.py`
  (the hard gate).
- After cutover the golden is regenerable from the allocator (`BBOE_UPDATE_*`),
  so it persists as a normal register-assignment regression tripwire.

## Cutover sequence (commit order; each commit green)

1. **Extract pure counting helpers.** Split the ref-count / clobber-cost /
   candidate-eligibility tallies out of `_select_auto_pin_candidates` into small
   pure functions returning plain dicts/sets. No behavior change; auto-pin still
   drives homes.
2. **Capture + freeze the golden.** Env-gated dump of the heuristic's per-function
   homes; generate and commit `cc_register_homes_baseline.json`.
3. **Build the input adapter.** A function that turns a function's AST/IR + the
   counting helpers into `allocatable` / `CostModel` / `RegisterConstraints`,
   unit-tested in `tests/unit/` against synthetic functions. Not yet wired into
   emission.
4. **Wire the allocator behind a flag.** `generate_function` calls
   `regalloc.allocate(...)` and maps `homes` -> `pinned_register`, spilled ->
   frame slots, selected by an env/attribute flag (default stays heuristic). Add
   the parity test; run the byte gate in allocator mode.
5. **Converge.** Tune `allowed` / pool ordering / cost inputs and the `color()`
   register preference until byte gate `≤ baseline` AND golden matches (or each
   diff is byte-neutral and listed). Justify + re-bless any small straggler.
6. **Flip default + delete auto-pin.** Make the allocator the only path; delete
   the heuristic and helpers; remove the flag. Re-bless `test_cc_place.py` golden
   if it churns.

## Risks (priority order) & mitigations

1. **Register-identity churn** breaks the golden but not bytes — handled by the
   documented-exceptions list + bytes-as-hard-gate; if churn is broad, steer
   `color()`'s select-phase preference to ascending save-cost (the engine is
   already cost-aware).
2. **Lost special-case wins** (`main`-pins-`BP`, the cmp-left AX-resident fast
   path, the sharing pass) surface as specific byte regressions — fold the
   economics into `CostModel` / `allowed` (`BP` as a zero-save-cost pool member
   for `main`; sharing is intrinsic to coloring's interference-based reuse).
   Residual stragglers fall to the re-bless policy.
3. **Precolor / 16-bit legality** — a byte value colored to `SI`/`DI`, or a
   16-bit index not in `SI`/`DI`, is a NASM reject or miscompile.
   `tests/test_cc_bits.py` (both bit widths) is the net.
4. **Call-clobber save correctness** for homed locals live across calls — a
   missed `push`/`pop` save miscompiles. The runtime matrix is the net.

## Testing & gate

- **Per-commit:** `tests/unit/` + `tests/test_cc_function_sizes.py` byte gate +
  the new `tests/test_cc_register_homes.py` parity test.
- **Before landing (full CI matrix locally):** `tests/test_asm.py` (incl.
  self-host), `tests/test_programs.py` bbfs + ext2 incl. `e2fsck`,
  `tests/test_bboefs.py`, `tests/test_cc_bits.py` (`--bits 16` and 32), the
  `rep_loops` runtime test, and the full `tests/unit/` suite.

## Out of scope

- IR-temp register residency (PR 3).
- Any virtual-register instruction-selection rewrite (emission stays
  accumulator-based).
- Renaming `pinned_register` to a unified home map and retiring the AST
  `LivenessAnalyzer` (PR 4 cleanup).
