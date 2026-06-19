# cc.py native-Address emission — phase 3 implementation plan

**For agentic workers:** REQUIRED SUB-SKILL: Use
superpowers:subagent-driven-development (recommended) or
superpowers:executing-plans to implement this plan task-by-task. Steps use
checkbox (`- [ ]`) syntax for tracking.

**Goal:** Re-admit the last ledger class (class 2: `Cast` store RHS) into
`_is_byte_safe_store_rhs` at byte parity (`readdir` → 0-delta), by extending
phase 2's store-RHS sink to the cast's `ir.Block(Assign(Cast))` def shape.

**Architecture (corrected — see errata below):** The approved design's
`Store.width` premise is **wrong** and is NOT implemented here. Reconnaissance
(2026-06-19, on `main` = 6f545794) established: (a) `ir.Store` already has a
vestigial `width: int` field, constructed `width=0` everywhere and unused at
emission — store width is derived from the lvalue's field layout
(`AddressPlan`/`MemoryOperand`), never from `Store.width`; (b) the `readdir` +6
is a **cast-temp spill/reload across address resolution**, not a width drop —
the store stays `mov [ebx], eax` at the same dword width before and after (the
cast is `(ino_t)d_ino`, `ino_t`=`uint32_t`, into a dword field); (c) cc.py's
cast codegen is **identity** (no truncation, `emission.py:5010`) and the cast
width already equals the field width at all 3 corpus sites. The actual
regression is the *exact* class-1/3/4 anatomy phase 2 fixed with the store-RHS
**sink** — but a `Cast` RHS lowers to `ir.Block(Assign(Cast))`
(`ir.py:551-554`), and the sink (`_collect_ir_sunk_store_values`) only collects
`ir.Index`/`ir.BinaryOperation` defs, so it cannot reach it. Phase 3 extends the
value sink to the `Block(Assign(Cast))` def shape — mirroring how
`_collect_ir_sunk_index_terms` already sinks `ir.Block(Assign)` defs for the
compound-conditional index — keeping the cast node intact (the future
narrowing-cast fix, a separate deferred gap, belongs in `generate_expression`'s
`Cast` codegen, not on `Store`).

**Tech stack:** Python 3 (cc.py), NASM output verified by
`tests/test_cc_function_sizes.py` (the 361-function/49-file byte gate),
`tests/test_cc_bits.py` (16/32), `tests/test_cc_place.py` (golden),
`tests/unit/` (pytest).

**Branch:** `bboe/cc-naddr-phase3-cast` off `main` (6f545794).

**Gate discipline (every commit):** `python3 tests/test_cc_function_sizes.py`
must print `PASS  per-function byte-size gate (361 functions, 49 files)` with
zero GREW/shrank (refresh + record a shrank only with the asm diff confirming
it). Plus per commit: `pre-commit run codesorter --all-files` (PASS — main now
has the tree-wide codesorter hook), `python3 -m pytest tests/unit/ -q`, `python3
tests/test_cc_bits.py` (122/122), `python3 tests/test_cc_place.py` (golden).
Pre-existing failure to ignore: `tests/test_cc_member_index_address.py` 2
failures on main.

**Conventions:** strict-alphabetical ordering (functions, methods, dataclass
fields, isinstance tuples, match arms, class statements, test functions); no
abbreviations in any identifier; `from __future__ import annotations` makes
forward references free. Commits end with `Co-Authored-By: Claude Opus 4.8
<noreply@anthropic.com>`. pytest only against `tests/unit/`, never bare
`tests/`.

**Measured facts (2026-06-19 recon):**
- `_is_byte_safe_store_rhs` at `cc/ir.py:1547`; admitted tuple at `:1599` is
  `(Index, Int, PlaceLoad, String, Var)` + `_is_byte_safe_binary_operation` +
  the `PlaceAddressOf(VariablePlace)` clause. `ast_nodes.Cast` is NOT admitted.
- `ast_nodes.Cast` (`cc/ast_nodes.py:136-148`): fields `expression: Node`,
  `target_type: str`. Typedefs resolved at parse time, so the builder sees
  canonical scalar names (`(ino_t)`→`'unsigned int'`, `(char)`→`'char'`).
- A `Cast` store RHS lowers via the default `case _:` in `_build_expr`
  (`cc/ir.py:551-554`) to `Block(node=Assign(expr=<the Cast>, name=temp))`.
- The sink `_collect_ir_sunk_store_values` (`emission.py:1131`) collects
  `ir.Index`/`ir.BinaryOperation` defs in the strict triple `[def; ir.Address;
  ir.Store]` where the def's single-use `_ir_*` destination is the store's
  `value`, the store's plan exists, and the plan does NOT preserve the
  accumulator (`_plan_preserves_accumulator_for_store_rhs` False). Replay:
  `_emit_sunk_store_value` (Index/BinaryOperation cases). Suppression: the `case
  ir.Index` / `case ir.BinaryOperation` arms skip when
  `self._ir_sunk_store_values.get(destination) is instruction`.
- The Block-def template: `_collect_ir_sunk_index_terms` collects
  `ir.Block(Assign)` defs guarded by a `reads_ir_temp(node)` helper (rejects
  defs whose expression reads any `_ir_*` temp — operand home unknowable in the
  pre-allocation scan) and replays via `generate_expression(expr)`. Its Block
  suppression is `case ir.Block(node=Assign(name=name)) if
  self._ir_sunk_index_terms.get(name) is instruction: pass`.
- The regressing store is `directory->entry.d_ino = (ino_t)d_ino` in `dirent.c`
  `readdir`; its plan is the AX-clobbering chained-member arm (so the value
  sink's preserve=False gate fires). Admitting `Cast` today (no sink extension)
  grows ONLY `readdir` +6; `grow_heap`/`strtol` are 0-delta (pointer-width
  casts).

---

### Task 1: Extend the store-RHS value sink to `Block(Assign(Cast))` defs

**Files:** Modify `cc/codegen/x86/emission.py` (`_collect_ir_sunk_store_values`,
`_emit_sunk_store_value`, the def-suppression arms); Test
`tests/unit/test_cc_address_plan.py`.

This is byte-neutral: no `Cast` store RHS is admitted yet, so no `[Block(Cast);
Address; Store]` triple exists in the corpus (cast store RHS stays on
`ir.Access`). The sink extension collects nothing until Task 2 admits `Cast`.
Gate must be 0-delta.

- [ ] **Step 1: Read the templates.** Read `_collect_ir_sunk_store_values`
  (`emission.py:1131`), `_collect_ir_sunk_index_terms` (its `reads_ir_temp`
  helper + `ir.Block(Assign)` arm), `_emit_sunk_store_value`, and the `case
  ir.Index` / `case ir.BinaryOperation` suppression arms plus the existing `case
  ir.Block(node=Assign(name=name)) if self._ir_sunk_index_terms...` suppression
  in the IR dispatch loop. The extension mirrors the index-term Block handling
  exactly, but for store VALUES.

- [ ] **Step 2: Failing unit test** in `tests/unit/test_cc_address_plan.py`
  (alphabetical placement; reuse the `_generate_with_ir_bodies` harness the
  existing sink tests use; ruff D205 one-line summary). Add a
  `CAST_STORE_SOURCE` fixture (alphabetical among the SOURCE constants) whose
  store RHS is a width-bearing cast through an AX-clobbering (chained-member or
  arrow) store, e.g.:
```python
CAST_STORE_SOURCE = """
struct inner { unsigned int id; };
struct outer { int pad; struct inner entry; };
void store_id(struct outer *out, unsigned int value) {
    out->entry.id = (unsigned int)value;
}
"""
```
Test `test_cast_store_rhs_sinks_block_def`:
```python
def test_cast_store_rhs_sinks_block_def() -> None:
    """A (T)x cast store RHS lowers to a Block(Assign(Cast)) def that the
    value sink collects and replays at the terminal RHS slot -- no frame
    slot, no register home (ledger class 2)."""
```
Assert: compiling the fixture, the sole sunk temp is in
`generator._ir_sunk_store_values`, its def is an `ir.Block`, and the temp is
`not in generator.locals` and `not in generator.temp_pinned_registers`. **This
test depends on Task 2's admission to produce the triple — so write it to FAIL
now** (at HEAD the cast store stays on Access, the dict is empty, the unpacking
raises). If the harness makes a Task-1-only test awkward (no admission yet),
instead assert directly against `_collect_ir_sunk_store_values` on a hand-built
`[ir.Block(Assign(Cast)); ir.Address; ir.Store]` body (mirror how the regalloc
tests hand-build IR) so Task 1 is independently testable; pick whichever is
robust and note the choice in the docstring.

- [ ] **Step 3: Run it, watch it fail.** `python3 -m pytest
  tests/unit/test_cc_address_plan.py::test_cast_store_rhs_sinks_block_def -x
  -q`.

- [ ] **Step 4: Implement.** In `_collect_ir_sunk_store_values`:
  - Widen the return type to `dict[str, ir.BinaryOperation | ir.Block |
    ir.Index]` (alphabetical in the union).
  - Add a `reads_ir_temp` guard (extract/share the one
    `_collect_ir_sunk_index_terms` uses, or add a private module helper used by
    both — do NOT duplicate the recursive walker; if sharing is awkward, mirror
    it with an identical body and a comment pointing at the twin).
  - In the def-shape check, also accept `ir.Block` whose `node` is `Assign` and
    whose `node.expr` is an `ast_nodes.Cast`, with the destination being
    `node.name`; reject when `reads_ir_temp(node.expr)` (operand home unknowable
    pre-allocation). Keep every other gate identical (single-use `_ir_*`
    destination == store.value, strict `[def; Address; Store]` adjacency, plan
    exists and does NOT preserve the accumulator).

  In `_emit_sunk_store_value`: widen the parameter type and add a `case
  ir.Block(node=Assign(expression=expression)):` arm (match `ast_nodes.Assign`'s
  actual field name — verify; the recon shows `Assign(expr=...)` is used in
  builder construction but confirm the dataclass field) that replays
  `self.generate_expression(<the cast expression>)`. Replaying the Cast node
  itself is correct (identity codegen lands the inner value in AX); document
  why.

  In the IR dispatch loop: extend the existing `case
  ir.Block(node=Assign(name=name))` suppression so a Block whose name is in
  `_ir_sunk_store_values` (identity-checked, `is instruction`) is also
  suppressed (currently only `_ir_sunk_index_terms` is checked). Keep the
  index-term check; the two are mutually exclusive by construction but both must
  be honored. Preserve all existing comments.

- [ ] **Step 5: Verify.** Byte gate 0-delta (no admission yet); `pre-commit run
  codesorter --all-files` PASS; `python3 -m pytest tests/unit/ -q`; `cc_bits`
  122/122; `cc_place` golden.

- [ ] **Step 6: Commit.** `feat(cc): sink cast store-RHS Block defs into
  AX-clobbering store terminals (phase 3)`.

---

### Task 2: Re-admit `Cast` store RHS (ledger class 2)

**Files:** Modify `cc/ir.py` (`_is_byte_safe_store_rhs`); Test
`tests/unit/test_cc_ir.py`.

- [ ] **Step 1: Failing lowering test** in `tests/unit/test_cc_ir.py`
  (alphabetical; mirror
  `test_binary_operation_rhs_member_store_lowers_to_store`; ruff D205):
  `p->field = (char)x;` lowers onto `ir.Address` + `ir.Store(value=temp)` with a
  `Block(Assign(Cast))` def for the temp, and **no** `ir.Access` producer
  remains.
```python
def test_cast_rhs_member_store_lowers_to_store() -> None:
    """p->member = (char)x lowers onto Address + Store with a sunk cast def.

    No ir.Access producer remains for the shape (ledger class 2 re-admitted
    in phase 3).
    """
```

- [ ] **Step 2: Run it, watch it fail** (at HEAD the cast store stays on
  `ir.Access`).

- [ ] **Step 3: Implement.** Add `ast_nodes.Cast` to the
  `_is_byte_safe_store_rhs` isinstance tuple (alphabetical: `Cast` first, before
  `Index`). Update the docstring: class 2 (`Cast`) re-admitted in phase 3 via
  the cast Block-def sink (replace the stale "phase 3's `Store.width` field is
  the planned fix" sentence — `Store.width` was never the cause; see the
  design-specs errata). One concise paragraph; do not rewrite the whole
  docstring.

- [ ] **Step 4: THE LEDGER CHECK.** `python3 tests/test_cc_function_sizes.py`:
  - Required: `readdir` 0-delta or shrank; **nothing else changes**.
  - Hand-diff `readdir` regardless (compile `dirent.c` the way the gate test
    does — read it for the invocation) and paste the before/after into the
    commit message: the +6 spill/reload (`mov [ebp-N], eax` … `mov eax,
    [ebp-N]`) must be gone, the store back to the legacy inline sequence.
  - Any GREW → STOP, report BLOCKED with the asm diff and which sink gate failed
    to engage (was the Block def collected? did the plan's preserve verdict
    differ? was the cast expr rejected by `reads_ir_temp`?). Never ship a
    workaround.
  - Any unexpected shrank elsewhere → investigate before accepting.

- [ ] **Step 5: Runtime verification.** `python3 tests/test_programs.py`
  (default bbfs) — `readdir`/`dirent.c` is exercised by `ls`; confirm green.
  Plus `python3 -m pytest tests/unit/ -q`, `cc_bits`, `cc_place`, `pre-commit
  run codesorter --all-files`.

- [ ] **Step 6: Commit.** `feat(cc): re-admit Cast store RHS (ledger class 2,
  phase 3)` — ledger check verbatim + measured result + the `readdir` diff in
  the body.

---

### Task 3: Docs — ledger close-out, errata, changelog

**Files:** Modify `cc/ir.py` (verify the `_is_byte_safe_store_rhs` docstring is
current), `docs/CHANGELOG.md`; and on the `design-specs` branch
(controller-owned): the ledger doc + design errata.

- [ ] **Step 1: CHANGELOG.** One Unreleased entry: cc.py phase 3 — re-admit
  `Cast` store RHS (ledger class 2) via the cast Block-def store sink; `readdir`
  0-delta; the four ledger classes are now all cashed.

- [ ] **Step 2 (controller, design-specs):**
  - Mark ledger class 2 **CASHED** in
    `2026-06-06-cc-native-address-emission-expected-byte-reductions.md` with the
    measured result and commit reference; note the corrected mechanism (sink,
    not `Store.width`).
  - Append a **phase-3 errata** to
    `2026-06-06-cc-native-address-emission-design.md`: the `Store.width` premise
    was falsified by recon (width was already correct; the +6 was a cast-temp
    spill); `Store.width` stays vestigial and is a candidate for **removal**,
    not population; narrowing-cast truncation is a separate,
    deliberately-deferred latent gap whose correct home is
    `generate_expression`'s `Cast` codegen, not `Store`. Mark the phase table's
    row 3 done (mechanism: sink, not width).

- [ ] **Step 3: Final verification.** Full local sweep: `pytest tests/unit/`,
  byte gate, `cc_bits`, `cc_place`, `tests/test_programs.py` bbfs (+ ext2 if
  time-boxed). Confirm the byte gate's four-class ledger is fully cashed.

- [ ] **Step 4: Commit.** `docs: phase-3 close-out — Cast store RHS cashed
  (ledger class 2)`.

---

## Execution notes for the controller

- Implementer model: opus for Task 1 (sink mechanism, parity-critical,
  ordering-sensitive); sonnet acceptable for Tasks 2–3. Two-stage review per
  task (spec, then quality); the sink-gate and `reads_ir_temp` reuse get
  adversarial review.
- Task 2 is the falsifiable claim. A persistent `readdir` GREW after mechanism
  iteration is a design-level finding — stop and bring it to Bryce, do not work
  around.
- This is a 3-task phase; consider a single PR (it's small and the three commits
  stack trivially), or the established 1-PR-per-logical-unit if review appetite
  prefers. The whole-branch review + sorting audit (the phase-2 lesson) still
  apply before opening the PR.
