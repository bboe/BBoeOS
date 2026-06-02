# Plan 5 Stage 3a — Recursive Address Resolver Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the five bespoke place-codegen emitters in `cc/codegen/x86/generator.py` with one recursive `resolve_address(place) -> MemOperand`, delivering arbitrary-depth lvalues and retiring the `_emit_double_index_place_*` hack, with per-function bytes ≤ baseline (or a notable perf win).

**Architecture:** Build the generalized `MemOperand` descriptor and a recursive `resolve_address` *alongside* the existing emitters. Migrate one Place shape at a time — route that shape's dispatch arm through the resolver, verify byte-size + golden + runtime parity, then delete the bespoke emitter it replaced. Each task leaves the build green and every other shape on its existing path. `ir.Index`/`ir.IndexAssign` (named-array `a[i]`, emitted in `emission.py`) are out of scope and must not be touched.

**Tech Stack:** Python 3.13; `cc.py` self-hosting C compiler; `nasm`/`readelf`; bare-Python QEMU test drivers under `tests/`.

---

## Oracle & gate (read before any task)

Because register choice may now legitimately differ, the byte-*exact* golden is no longer the efficiency oracle. Use this layered acceptance after **every** migration task:

1. **`python3 tests/test_cc_function_sizes.py`** — per-function bytes. **Must be ≤ baseline for every function.** A larger function is allowed ONLY with a stated notable-perf-win justification recorded in the commit; otherwise iterate the resolver until it's ≤. (Baseline is regenerated once, in Task 9, after the migration settles.)
2. **`python3 tests/test_cc_place.py`** — the byte-exact golden. It *will* change as shapes migrate; treat a diff as a prompt to (a) confirm the size gate still passes for the affected functions, then (b) regenerate it with `BBOE_UPDATE_GOLDEN=1` and **eyeball the regenerated diff in the commit** so the change is deliberate, never a rubber stamp.
3. **`python3 tests/test_programs.py`** (bbfs) — runtime correctness; the migrated shapes must still execute correctly.

Full matrix (`--filesystem ext2`, `test_asm`, `test_bboefs`, `tests/unit/`) runs in the final task. Env note: a shell hook rewrites commands through `rtk`; prefix `grep`/`git` with `rtk proxy ` if output looks mangled.

---

## File Structure

- Modify `cc/codegen/x86/generator.py`: add `MemOperand` (near `PlaceAddress`, line ~132); add `resolve_address` + a thin terminal load/store; shrink `_emit_place_load`/`_emit_place_store` to dispatch through the resolver; delete the five bespoke emitters as their shapes migrate; keep `_resolve_index_member_layout` / `_match_struct_array_member` / `_resolve_member_index_layout` / layout helpers as per-`Member` lookups; keep the bitfield helpers (`_emit_bitfield_read`/`_write`/`_write_literal`) and `_emit_field_load`/`_emit_field_store`/`_emit_store_accumulator_at_width`/`emit_byte_load_zx` as terminals.
- Modify `tests/test_programs.py`: add arbitrary-depth runtime tests (Task 2, Task 8).
- Regenerate `tests/golden/cc_place_index_member.asm` (blessed per task) and `tests/golden/cc_function_sizes_baseline.json` (once, Task 9).
- Do **not** touch `cc/codegen/x86/emission.py` `_generate_index_expression` (line 1163) or `generate_index_assign` (line 3473) — the `ir.Index` boundary.

---

### Task 1: `MemOperand` descriptor + `resolve_address` skeleton (static bases)

Introduce the descriptor and the recursion for the **static-base, deref-free** shapes only (`VariablePlace`, `MemberPlace`, constant + dynamic `SubscriptPlace`), wired into *nothing* yet. This reproduces what `_resolve_place` does for struct-array shapes A/B, generalized.

**Files:** Modify `cc/codegen/x86/generator.py`.

- [ ] **Step 1: Add the `MemOperand` dataclass** immediately after `PlaceAddress` (line ~148):

```python
@dataclass(slots=True)
class MemOperand:
    """A machine memory operand ``[base (+displacement) (+index)]``.

    base_kind selects how *base* reads: a NASM label ("_g_arr"), a
    frame-relative string ("ebp-12"), or a register holding an address
    materialized by a prior dereference.  *displacement* sums member
    offsets and constant subscripts; *index* (when not None) is a register
    holding the summed dynamic byte-offset.  *field_size* / *element_size*
    size the terminal load/store (field_size != element_size marks an
    array-typed member whose load decays to its address via ``lea``).
    """

    base_kind: str  # "label" | "frame" | "register"
    base: str
    displacement: int = 0
    index: str | None = None
    field_size: int = 0
    element_size: int = 0
```

- [ ] **Step 2: Add `resolve_address` covering VariablePlace, MemberPlace, and Subscript (static + dynamic index) over a static base.** Place it next to `_resolve_place` (line ~3879). This is the deref-free recursion; the `DereferencePlace` case (register base) and the array-of-pointers/double-index handling arrive in Tasks 3–5. Until then it raises for shapes it doesn't yet cover, so it can be wired in incrementally.

```python
def resolve_address(self, place: Place, /) -> MemOperand:
    """Resolve *place* to a MemOperand, emitting any dynamic-index / pointer
    code as a side effect (GCC get_inner_reference / LLVM EmitLValue model).

    Deref-free segments fold into one operand; a DereferencePlace breaks the
    chain into a fresh register base (Task 3).  Member offsets and constant
    subscripts sum into displacement; dynamic subscripts are scaled and
    summed into a single index register.
    """
    match place:
        case VariablePlace(name=name):
            base_kind, base = self._variable_base(name, line=place.line)
            return MemOperand(base_kind=base_kind, base=base)
        case MemberPlace(base=base, member_name=member_name):
            operand = self.resolve_address(base)
            field_offset, field_size, element_size = self._member_layout_on(base, member_name, line=place.line)
            operand.displacement += field_offset
            operand.field_size = field_size
            operand.element_size = element_size
            return operand
        case SubscriptPlace(base=base, index=index):
            operand = self.resolve_address(base)
            self._accumulate_subscript(operand, index=index, element_size=operand.element_size or operand.field_size)
            operand.field_size = operand.element_size
            return operand
        case DereferencePlace():
            return self._resolve_dereference(place)  # Task 3
        case _:
            message = "unsupported Place shape in resolve_address"
            raise CompileError(message, line=place.line)
```

- [ ] **Step 3: Add the three helper methods** `_variable_base` (returns `("label", "_g_<name>")` for a global / `("frame", "ebp-<off>")` for a local, reusing `_resolve_index_member_layout`'s base logic and `_resolve_struct_value_base`), `_member_layout_on` (per-`Member` layout: returns `(field_offset, field_size, element_size)`, reusing `_resolve_index_member_layout` for struct-array bases and the struct-value path for dot bases), and `_accumulate_subscript` (constant index → `displacement += value*element_size`; dynamic index → evaluate, scale via the existing `_emit_scale_index`, and `add` into a freshly chosen index register, summing if one is already present). Mirror the exact instruction choices (`imul`/`shl`/`add`) the current `_emit_struct_element_offset` / shape-B path uses; differences in *which* scratch register are fine (gate is bytes).

- [ ] **Step 4: Add a `_resolve_dereference` stub** that raises `CompileError("dereference not yet in resolve_address", line=place.line)` so the file imports and `resolve_address` is callable for the static shapes. (Task 3 implements it.)

- [ ] **Step 5: Verify it imports and existing tests are untouched** (nothing routes through `resolve_address` yet):

```bash
rtk proxy python3 -c "from cc.codegen.x86 import generator"
python3 tests/test_cc_place.py        # PASS, byte-identical (no routing change yet)
python3 tests/test_cc_function_sizes.py
```
Expected: import OK; golden byte-identical; size gate PASS (unchanged).

- [ ] **Step 6: Commit**

```bash
git add cc/codegen/x86/generator.py
git commit -m "feat(cc): MemOperand + resolve_address skeleton (static bases, Stage 3a)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Arbitrary-depth runtime tests (added first, expected to FAIL/partial)

Add the correctness oracle for the *new* capability before migrating, so each later task can show progress. These compile-and-run via `tests/test_programs.py`.

**Files:** Modify `tests/test_programs.py`.

- [ ] **Step 1: Add four runtime probes** to the test list in `tests/test_programs.py` (follow the existing entry format — a C program string + a shell command + an expected-output regex). Use these programs (each prints a computed value the regex checks):

```c
/* depth_triple: a[i][j][k] over int** -style nesting via arrays of pointers */
int r0[2]; int r1[2]; int *rows[2]; int **grid[1];
int main(){ r0[0]=10; r0[1]=11; r1[0]=20; r1[1]=21; rows[0]=r0; rows[1]=r1; grid[0]=rows;
            printf("%d\n", grid[0][1][0]); return 0; }   /* expect 20 */
```

```c
/* depth_arrow_index: a->b[1][2] */
struct inner { int v[3][4]; };
struct inner obj; struct inner *p = &obj;
int main(){ obj.v[1][2] = 77; printf("%d\n", p->v[1][2]); return 0; }   /* expect 77 */
```

```c
/* depth_deref_index: (*pp)[i][j] */
int m[2][3]; int (*pm)[2][3] = &m;
int main(){ m[1][2]=42; printf("%d\n", (*pm)[1][2]); return 0; }   /* expect 42 */
```

```c
/* depth_struct_grid: s.grid[i][j].f[k] */
struct cell { int f[2]; };
struct board { struct cell grid[2][2]; };
struct board b;
int main(){ b.grid[1][0].f[1] = 99; printf("%d\n", b.grid[1][0].f[1]); return 0; }   /* expect 99 */
```

(Confirm each compiles under clang with `tests/bboeos.h` semantics if `test_cc_compatibility` covers new C; if any uses a construct cc.py's parser doesn't yet accept, note it as DONE_WITH_CONCERNS — parser gaps are out of 3a's codegen scope and may need a separate parser fix or a simpler equivalent fixture.)

- [ ] **Step 2: Run them, expect failures or compile errors** (the resolver doesn't yet handle these depths):

```bash
python3 tests/test_programs.py depth_triple depth_arrow_index depth_deref_index depth_struct_grid
```
Expected: FAIL (wrong value, crash, or "unsupported Place shape"). Record which fail and how — this is the progress baseline.

- [ ] **Step 3: Commit the tests (red)**

```bash
git add tests/test_programs.py
git commit -m "test(cc): arbitrary-depth lvalue runtime probes (Stage 3a, red)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Migrate the `DereferencePlace` shape (register base)

Implement `_resolve_dereference` and route standalone `*p` through the resolver, then delete `_emit_dereference_place_load`/`_store`. This introduces the register-base segment and the deref chain-break.

**Files:** Modify `cc/codegen/x86/generator.py`.

- [ ] **Step 1: Implement `_resolve_dereference`** — evaluate `place.pointer` to a register and return `MemOperand(base_kind="register", base=<reg>, field_size=self._dereference_place_width(place), element_size=<same>)`. Preserve the two existing fast paths as resolver outcomes: the **frame-direct** case (`pointer` is `&local`/`Cast(&local)`) returns `MemOperand(base_kind="frame", base=self._local_address(name), ...)` with *no* pointer load; the **general** case emits `generate_expression(pointer)` and uses the accumulator (self-load) — matching `_emit_dereference_place_load` lines 1320–1340. Width via `_dereference_place_width`.

- [ ] **Step 2: Make the terminal load handle a register/frame base.** In `_emit_place_load`, after `operand = self.resolve_address(place)`, build the address with `_build_address` (base + displacement + index) and call `_emit_field_load(addr, operand.field_size)` (which already routes byte/word/full via `emit_byte_load_zx`/`movzx`/`mov`). For a `register` base the address is `[reg+disp+index]`; for `frame`/`label` as today.

- [ ] **Step 3: Make the terminal store handle the deref store specials.** In `_emit_place_store`, the terminal must preserve `_emit_dereference_place_store`'s three behaviors: (a) `out_register` / named-pointer write (`mov REG, acc`) when `place.pointer` is a `Var` in `out_register_locals`; (b) the `&local` fast store via `_emit_store_accumulator_at_width`; (c) general value-save / pointer-eval / store via `_emit_store_accumulator_at_width`. Keep these as a terminal-store helper keyed on the resolved `MemOperand` + the original `DereferencePlace`. Width via `_dereference_place_width`.

- [ ] **Step 4: Route the shape through the resolver** — in `_emit_place_load` and `_emit_place_store`, delete the `if isinstance(place, DereferencePlace): self._emit_dereference_place_*` dispatch arms (lines 2613–2615 / 2651–2653) so `DereferencePlace` falls through to the resolver path.

- [ ] **Step 5: Gate** (see Oracle section):

```bash
python3 tests/test_cc_function_sizes.py
python3 tests/test_cc_place.py
python3 tests/test_programs.py
```
Expected: size gate ≤ baseline for every function (iterate `_resolve_dereference` until the `probe_deref_*` / `probe_cast_deref_*` functions match size); golden diff confined to deref probes — regenerate with `BBOE_UPDATE_GOLDEN=1` and eyeball; runtime green.

- [ ] **Step 6: Delete `_emit_dereference_place_load` and `_emit_dereference_place_store`** (lines 1301–1395) now that nothing calls them. Re-run the gate to confirm no caller remains (`rtk proxy grep -n "_emit_dereference_place" cc/`).

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor(cc): migrate DereferencePlace to resolve_address; drop bespoke deref emitters

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Migrate the double-index hack (`name[outer][inner]`)

With the deref + subscript recursion in place, `SubscriptPlace(DereferencePlace(Index(Var)))` resolves naturally (register base from the pointer load, then index). Route it through the resolver and **delete the hack**.

**Files:** Modify `cc/codegen/x86/generator.py`.

- [ ] **Step 1: Confirm the resolver handles the shape.** The inner `name[outer]` is an `ir.Index`/`Index` AST that `generate_expression` evaluates to the element pointer (a register), which `_resolve_dereference` already turns into a register base; the outer `[inner]` is `_accumulate_subscript` on that register base. Verify `resolve_address` produces the right operand for `names[1][2]`, `names[i][j]`, `names[i][j+1]` (the golden's `probe_double_index_*`).

- [ ] **Step 2: Delete the double-index dispatch arms** in `_emit_place_load` (lines 2601–2612) and `_emit_place_store` (lines 2641–2650) so the shape falls through to the resolver.

- [ ] **Step 3: Gate.**

```bash
python3 tests/test_cc_function_sizes.py
python3 tests/test_cc_place.py
python3 tests/test_programs.py depth_triple depth_deref_index
```
Expected: size gate ≤ baseline for the `probe_double_index_*` functions (iterate `_accumulate_subscript` / `_resolve_dereference` register choices to match); golden diff confined to double-index probes — regenerate + eyeball; `depth_triple` / `depth_deref_index` now PASS (or move closer — full depth may need Task 5/8).

- [ ] **Step 4: Delete `_emit_double_index_place_load` and `_emit_double_index_place_store`** (lines 1397–1500). Confirm no callers (`rtk proxy grep -n "_emit_double_index_place" cc/`). This is the **retired hack** the spec calls out.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor(cc): fold double-index into resolve_address; retire the 2-level hack

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Migrate member-scalar and member-index shapes

Route the `MemberPlace` scalar shapes (dot / arrow / chained, incl. arrow = `MemberPlace(DereferencePlace(...))` register base) and the member-index shape (`base.field[i]`) through the resolver. Preserve bitfield and array/struct-value terminals.

**Files:** Modify `cc/codegen/x86/generator.py`.

- [ ] **Step 1: Ensure the terminal load handles bitfield + array-decay + struct-value.** After `resolve_address`, the terminal load checks: array-typed member (`field_size != element_size`) → `lea acc, addr` (decay); struct-value member → `lea`; bitfield member → `_emit_bitfield_read(info, addr=addr)`; else `_emit_field_load`. Carry the bitfield `info` (bit_width/bit_offset) through resolution — add it to `MemOperand` (a `bitfield: BitfieldInfo | None = None` field set by the `MemberPlace` case when the field is a bitfield) so the terminal can branch on it.

- [ ] **Step 2: Ensure the terminal store handles bitfield writes.** The store terminal must preserve `_emit_member_dot_store`'s bitfield dispatch: 1-bit literal → `_emit_bitfield_write_literal`; const-fold known-local-byte + literal → folded `mov byte`; general → `_emit_bitfield_write` (using CL, not BL). Key off `operand.bitfield`.

- [ ] **Step 3: Route member-scalar** — delete the `_emit_member_scalar_*` dispatch arms (lines 2594–2596 / 2638–2639). The arrow base (`MemberPlace(DereferencePlace(...))`) now resolves via the `DereferencePlace` register-base case from Task 3.

- [ ] **Step 4: Route member-index** — delete the `_is_member_index_place` dispatch arms (lines 2591–2593 / 2635–2637). `SubscriptPlace(MemberPlace(...))` resolves via Member then Subscript. Pointer-field members (the field itself is a pointer that must be dereferenced before indexing) resolve via the field load producing a register base — confirm `_member_layout_on` + `_accumulate_subscript` handle the pointer-field case (it's a deref: the field's pointer value becomes a register base).

- [ ] **Step 5: Gate.**

```bash
python3 tests/test_cc_function_sizes.py
python3 tests/test_cc_place.py
python3 tests/test_programs.py
python3 tests/test_programs.py depth_arrow_index depth_struct_grid
```
Expected: size gate ≤ baseline for `probe_dot_*`/`probe_arrow_*`/`probe_chain_*`/`probe_bitfield_*`/`probe_inline_index_*`/`probe_pointer_index_*`/`probe_member_addr*` (iterate to match); golden regenerated + eyeballed; runtime green; `depth_arrow_index`/`depth_struct_grid` PASS (or progress).

- [ ] **Step 6: Delete the now-unused member emitters** — `_emit_member_scalar_load`/`_store` and sub-emitters (`_emit_member_dot_store`/`_chained_store`/`_arrow_store`), `_emit_member_index_load`/`_store`/`_emit_member_index_access`, and `_is_member_index_place` — once `rtk proxy grep` shows no callers. Keep `_resolve_index_member_layout`, `_resolve_member_index_layout`, `_match_struct_array_member`, `_resolve_struct_value_base`, and the bitfield/field/width terminals (still used by the resolver + terminals).

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor(cc): migrate member-scalar and member-index shapes to resolve_address

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Migrate the struct-array shape and retire `_resolve_place`

Fold the original shape-A/B struct-array path into the recursion and delete `_resolve_place`; `_emit_place_load`/`_store` now resolve everything through `resolve_address`.

**Files:** Modify `cc/codegen/x86/generator.py`.

- [ ] **Step 1: Confirm `resolve_address` covers shape A (`arr[i].member`) and shape B (`arr[i].member[j]`).** These are `MemberPlace(SubscriptPlace(VariablePlace))` and `SubscriptPlace(MemberPlace(SubscriptPlace(VariablePlace)))` — already handled by the VariablePlace + Subscript + Member cases (Task 1) plus the inner-index accumulation. Verify against `probe` / `probe_word_member`.

- [ ] **Step 2: Replace the default `_resolve_place` path in `_emit_place_load`/`_store`** (lines 2616–2631 / 2654–2669) with the unified `operand = self.resolve_address(place)` + terminal load/store. Preserve the `_bx_holds_pinned_var` protection by having the resolver/terminal avoid clobbering a pinned base register, or save/restore around the resolve when it must use that register (register freedom lets it usually pick a free scratch — verify the size gate doesn't regress pinned-BX functions).

- [ ] **Step 3: Delete `_resolve_place`** (lines 3879–3926) and the now-unused `PlaceAddress` dataclass if nothing else references it (`rtk proxy grep -n "PlaceAddress\|_resolve_place" cc/`).

- [ ] **Step 4: Gate.**

```bash
python3 tests/test_cc_function_sizes.py
python3 tests/test_cc_place.py
python3 tests/test_programs.py
```
Expected: size gate ≤ baseline (esp. `probe`/`probe_word_member`/`probe_sizeof` and any auto-pinned-BX function); golden regenerated + eyeballed; runtime green.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor(cc): unify struct-array shapes into resolve_address; delete _resolve_place

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Thin the dispatchers + final emitter audit

With every shape on the resolver, reduce `_emit_place_load`/`_store` to their minimal form and confirm the five bespoke emitters are gone.

**Files:** Modify `cc/codegen/x86/generator.py`.

- [ ] **Step 1: Reduce `_emit_place_load`** to: `operand = self.resolve_address(place)`; build address; terminal load (array-decay `lea` / bitfield read / `_emit_field_load`); with pinned-register protection as needed. Reduce `_emit_place_store` to: terminal store (out_register / bitfield / width) over `resolve_address(place)`.

- [ ] **Step 2: Audit deletions** — confirm none of these remain as defined methods:

```bash
rtk proxy grep -n "_emit_dereference_place\|_emit_double_index_place\|_emit_member_scalar\|_emit_member_index\|_emit_member_dot_store\|_emit_member_chained_store\|_emit_member_arrow_store\|_is_member_index_place\|_resolve_place\b" cc/codegen/x86/generator.py
```
Expected: no definitions (only `resolve_address`, the layout helpers, and the terminals survive).

- [ ] **Step 3: Confirm the `ir.Index` boundary is intact** — `emission.py` `_generate_index_expression` and `generate_index_assign` are unchanged:

```bash
rtk proxy git diff --stat origin/main -- cc/codegen/x86/emission.py
```
Expected: no changes to `emission.py` (3a touches only `generator.py` + tests + goldens).

- [ ] **Step 4: Gate + commit.**

```bash
python3 tests/test_cc_function_sizes.py && python3 tests/test_cc_place.py && python3 tests/test_programs.py
git add -A
git commit -m "refactor(cc): thin _emit_place_load/store to the unified resolver

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Arbitrary-depth tests green + edge cases

Bring the Task-2 probes to green and add the explicit triple-index acceptance the spec names.

**Files:** Modify `tests/test_programs.py`.

- [ ] **Step 1: Run the Task-2 depth probes; all four must now PASS.**

```bash
python3 tests/test_programs.py depth_triple depth_arrow_index depth_deref_index depth_struct_grid
```
Expected: all PASS (values 20, 77, 42, 99). If any fails, the resolver's recursion for that depth is incomplete — fix `resolve_address` (likely a missing materialization at a deref boundary) and re-gate.

- [ ] **Step 2: Add a `&`-at-depth and write-at-depth probe** (address-of and store through arbitrary depth, exercising the `lea` terminal and store terminal recursively):

```c
/* depth_addr_write: write then &-read at depth */
struct cell { int f[2]; };
struct board { struct cell grid[2][2]; };
struct board b; int *q;
int main(){ b.grid[0][1].f[0] = 5; q = &b.grid[0][1].f[0]; *q = *q + 7;
            printf("%d\n", b.grid[0][1].f[0]); return 0; }   /* expect 12 */
```
Add it, run, expect PASS.

- [ ] **Step 3: Commit.**

```bash
git add tests/test_programs.py
git commit -m "test(cc): arbitrary-depth lvalue probes green + addr/write-at-depth

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: Settle baselines + full-matrix gate

Regenerate the size baseline and golden to their final 3a state and run the whole suite.

**Files:** Regenerate `tests/golden/cc_function_sizes_baseline.json`, `tests/golden/cc_place_index_member.asm`.

- [ ] **Step 1: Confirm the size gate passes against the *current* baseline with zero GREW** (only IMPROVED/equal). If any function is larger, it must carry a recorded perf-win justification; otherwise return to the owning task and shrink it. Then regenerate to capture improvements:

```bash
python3 tests/test_cc_function_sizes.py            # must show no GREW; note IMPROVED
BBOE_UPDATE_SIZES=1 python3 tests/test_cc_function_sizes.py
BBOE_UPDATE_GOLDEN=1 python3 tests/test_cc_place.py
```

- [ ] **Step 2: Full matrix.**

```bash
python3 tests/test_cc_place.py
python3 tests/test_cc_function_sizes.py
python3 tests/unit/test_cc_codegen.py
tests/test_asm.py
tests/test_programs.py --filesystem bbfs
tests/test_programs.py --filesystem ext2
tests/test_bboefs.py
```
Expected: all green (`test_asm` 49/49; programs both filesystems; bboefs 6; unit pass).

- [ ] **Step 3: Commit the settled baselines.**

```bash
git add tests/golden/cc_function_sizes_baseline.json tests/golden/cc_place_index_member.asm
git commit -m "test(cc): settle Stage 3a size baseline + golden (recursive resolver)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:** recursive `resolve_address` + `MemOperand` (Task 1, 3, 5, 6); deref chain-break / register base (Task 3); retire double-index hack (Task 4); bitfield + width terminals (Task 5 steps 1–2); struct-array fold + delete `_resolve_place` (Task 6); thin dispatchers + delete all five bespoke emitters (Task 7); `ir.Index` boundary untouched (Task 7 step 3); arbitrary-depth compiled-and-run tests incl. triple-index (Task 2, 8); oracle shift to size-gate + regenerated golden + runtime (Oracle section, every task, Task 9); register freedom (Task 3/6 register-choice notes); target-boundary is documentation-only (no task — correct, nothing to implement). All spec sections map to tasks.

**Placeholder scan:** no TBD/TODO; the per-shape emission steps specify the algorithm + the concrete acceptance gate (byte-size parity + golden + runtime) rather than pre-authored byte sequences, because byte-parity reproduction is the *acceptance criterion* and is developed against the gate — this is the honest shape of a parity-preserving codegen refactor, not a placeholder. C fixtures are complete.

**Type consistency:** `MemOperand` fields (`base_kind`, `base`, `displacement`, `index`, `field_size`, `element_size`, + `bitfield` added in Task 5) are used consistently; `resolve_address` returns `MemOperand` everywhere; helper names (`_variable_base`, `_member_layout_on`, `_accumulate_subscript`, `_resolve_dereference`) are introduced in Task 1/3 and reused thereafter.

## Risk register (from the spec)

1. Byte-size parity per shape — size gate + regenerated golden are the per-task backstop; iterate register/fold choices.
2. Bitfield RMW preserved at the terminal (Task 5 steps 1–2).
3. Pinned-register safety across the recursion (Task 6 step 2; register freedom + the `_bx_holds_pinned_var` discipline generalized).
4. Deref↔expression mutual-recursion termination + `*(p+1)` (Task 3; covered by golden `probe_cast_deref_*` + runtime).
5. `lea`-terminal at arbitrary depth (Task 8 step 2 `depth_addr_write`).
