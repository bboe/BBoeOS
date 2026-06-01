# cc.py `rep`-string Loop Recognition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps
> use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recognize hand-written element-wise init/copy loops
(`for (i=0;i<n;i++) dst[i]=V;` and `dst[i]=src[i];`) and rewrite them
into `rep stos{b,w,d}` / `rep movs{b,w,d}`, for element widths 1/2/4.

**Architecture:** A new IR pass `recognize_string_loops` runs over
natural loops (reusing the strength-reduction induction-variable
analysis), matches the fill/copy idiom, and rewrites the loop region
into a new `RepString` IR node. Codegen lowers `RepString` through a
helper shared with the existing `memcpy`/`memset` builtins. The
self-hosted assembler gains `movsd`/`stosd` so 4-byte loops byte-match
NASM.

**Tech Stack:** Python (the `cc/` compiler package), C
(`user/programs/asm.c`), NASM, pytest (`tests/unit/`), QEMU integration
tests (`tests/test_asm.py`, `tests/test_programs.py`).

**Spec:** `2026-06-01-cc-rep-string-loops-design.md` (this branch).

---

## File map

- `user/programs/asm.c` — add `handle_movsd` / `handle_stosd`, their
  `STR_*` constants and dispatch entries (sorted). Mirror to
  `user/static/asm.c` (kept byte-identical).
- `user/static/rep_movsd.asm` — new byte-roundtrip smoke test.
- `cc/ir.py` — new `RepString` instruction dataclass; add to the
  `Instruction` union.
- `cc/ir_optimize.py` — teach `_has_side_effects`,
  `_instruction_destination`, `_instruction_value_operands` about
  `RepString`; call `recognize_string_loops` in `_optimize_body`.
- `cc/loops.py` — `recognize_string_loops` pass + match/rewrite helpers.
- `cc/codegen/x86/builtins.py` — factor `_emit_rep_move` / `_emit_rep_fill`
  shared helpers; route `builtin_memcpy` / `builtin_memset` through them.
- `cc/codegen/x86/generator.py` — dispatch `ir.RepString` in the
  instruction loop.
- `cc/codegen/x86/emission.py` — `generate_rep_string` emitter.
- `tests/unit/test_cc_ir_optimize.py`, `tests/unit/test_cc_loops.py`,
  `tests/unit/test_cc_codegen.py` — unit coverage.
- `user/programs/rep_loops_test.c` (or extend an existing test program) +
  `tests/test_programs.py` — runtime check.

---

## Task 1: Assembler `movsd` / `stosd` support

**Files:**
- Modify: `user/programs/asm.c` (handlers near `handle_movsw` ~1924 /
  `handle_stosw` ~2147; `STR_*` block ~4213; dispatch block ~4120)
- Sync: `user/static/asm.c`
- Create: `user/static/rep_movsd.asm`

- [ ] **Step 1: Write the failing roundtrip test program**

Create `user/static/rep_movsd.asm`:

```nasm
        ;; rep_movsd.asm — smoke test for the self-hosted assembler's
        ;; ``movsd`` / ``stosd`` dword string mnemonics (and ``rep``
        ;; over them) that cc.py emits for 4-byte element fill/copy
        ;; loops.  test_asm.py diffs asm.c's output against NASM's;
        ;; byte identity is the only contract.

        [bits 32]
        org 08048000h

main:
        mov ecx, 4
        cld
        rep movsd
        rep stosd
        movsd
        stosd
        movsb
        stosb
        movsw
        stosw
        ret
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `python3 tests/test_asm.py rep_movsd`
Expected: FAIL — `movsd` falls through to `handle_unknown_word`, so the
self-hosted output diverges from NASM (or the program is rejected).

- [ ] **Step 3: Add the two handlers in `user/programs/asm.c`**

Insert `handle_movsd` between `handle_movsb` and `handle_movsw`
(alphabetical: movsb < movsd < movsw):

```c
void handle_movsd() {
    emit_operand_size_prefix(32);
    emit_byte(0xA5);
}
```

Insert `handle_stosd` between `handle_stosb` and `handle_stosw`:

```c
void handle_stosd() {
    emit_operand_size_prefix(32);
    emit_byte(0xAB);
}
```

`emit_operand_size_prefix(32)` emits `0x66` under `bits 16` and nothing
under `bits 32` — the inverse of the existing `...w` handlers, giving
byte parity with NASM in both modes (NASM: `movsd`=A5, `stosd`=AB in
32-bit; `66 A5`/`66 AB` in 16-bit).

- [ ] **Step 4: Add `STR_*` constants (sorted)**

In the `STR_*` definition block, insert between `STR_MOVSB` and
`STR_MOVSW`:

```c
    "STR_MOVSD   db 'movsd',0\n"
```

Between `STR_STOSB` and `STR_STOSW`:

```c
    "STR_STOSD   db 'stosd',0\n"
```

- [ ] **Step 5: Add dispatch-table entries (sorted)**

In the dispatch block, insert between the `STR_MOVSB` and `STR_MOVSW`
rows:

```c
    "        dd STR_MOVSD, handle_movsd\n"
```

Between the `STR_STOSB` and `STR_STOSW` rows:

```c
    "        dd STR_STOSD, handle_stosd\n"
```

- [ ] **Step 6: Sync the static copy and verify identity**

Run:
```bash
cp user/programs/asm.c user/static/asm.c
diff -q user/programs/asm.c user/static/asm.c
```
Expected: no output (files identical).

- [ ] **Step 7: Run the roundtrip tests, verify pass**

Run: `python3 tests/test_asm.py rep_movsd && python3 tests/test_asm.py asm`
Expected: both PASS (the new mnemonics byte-match NASM, and asm.c still
reassembles itself byte-for-byte).

- [ ] **Step 8: Commit**

```bash
git add user/programs/asm.c user/static/asm.c user/static/rep_movsd.asm
git commit -m "feat(asm): movsd/stosd dword string mnemonics"
```

---

## Task 2: `RepString` IR node

**Files:**
- Modify: `cc/ir.py` (new dataclass + `Instruction` union)
- Modify: `cc/ir_optimize.py` (`_has_side_effects`,
  `_instruction_destination`, `_instruction_value_operands`)
- Test: `tests/unit/test_cc_ir_optimize.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_cc_ir_optimize.py`:

```python
def test_rep_string_is_not_dead_code_eliminated() -> None:
    """RepString is side-effecting: DCE must keep it even with no dest."""
    from cc import ir
    from cc.ir_optimize import Optimizer

    body = [
        ir.RepString(
            operation="fill", element_size=1, dest="buffer", source=None,
            count="n", fill_value=0, counter_signed=False, final_iv=None,
        ),
        ir.Return(value=None),
    ]
    out = Optimizer()._dead_code_elimination(list(body))
    assert any(isinstance(instruction, ir.RepString) for instruction in out)


def test_rep_string_value_operands_keep_count_live() -> None:
    from cc import ir
    from cc.ir_optimize import _instruction_value_operands

    node = ir.RepString(
        operation="copy", element_size=4, dest="d", source="s",
        count="n", fill_value=None, counter_signed=True, final_iv=None,
    )
    assert "n" in _instruction_value_operands(node)
```

- [ ] **Step 2: Run, verify it fails**

Run: `python3 -m pytest tests/unit/test_cc_ir_optimize.py -k rep_string -q`
Expected: FAIL — `ir.RepString` does not exist.

- [ ] **Step 3: Define the dataclass in `cc/ir.py`**

Add (alphabetically among the instruction dataclasses, after `Return`
and before `Switch`):

```python
@dataclass(frozen=True, kw_only=True, slots=True)
class RepString:
    """``rep movs``/``rep stos`` over an element-wise loop region.

    Produced by :func:`cc.loops.recognize_string_loops` when a natural
    loop is a unit-stride fill or copy.  Side-effecting (a store); never
    eliminated by DCE.  ``count`` is the iteration count n; for a signed
    counter the emitter guards ``n <= 0`` before the ``rep``.  ``final_iv``
    materializes the induction variable's post-loop value when it is read
    after the loop.
    """

    VALUE_FIELDS: ClassVar[tuple[str, ...]] = ("count", "fill_value")

    operation: str           # "fill" | "copy"
    element_size: int        # 1 | 2 | 4
    dest: str                # base name (pointer / array)
    source: str | None       # base name for copy; None for fill
    count: Value             # iteration count n
    fill_value: Value | None # fill value; None for copy
    counter_signed: bool
    final_iv: tuple[str, Value] | None
```

Add `RepString` to the `Instruction` union alias in this file (the
`| Return | RepString | Switch | ...` line — keep it alphabetical).

- [ ] **Step 4: Teach the optimizer helpers in `cc/ir_optimize.py`**

In `_has_side_effects`, add `RepString` to the side-effecting set so it
returns `True`:

```python
    if isinstance(instruction, ir.RepString):
        return True
```

In `_instruction_destination`, return `None` for `RepString` (it has no
SSA destination — it stores through `dest` but does not define a temp);
the default fall-through already returns `None`, so only add a case if
the function does not already default. Confirm by reading the function.

In `_instruction_value_operands`, surface its value fields so `count` /
`fill_value` stay live:

```python
    if isinstance(instruction, ir.RepString):
        operands = [instruction.count]
        if instruction.fill_value is not None:
            operands.append(instruction.fill_value)
        return tuple(operands)
```

- [ ] **Step 5: Run, verify pass**

Run: `python3 -m pytest tests/unit/test_cc_ir_optimize.py -k rep_string -q`
Expected: PASS (2 tests).

- [ ] **Step 6: Commit**

```bash
git add cc/ir.py cc/ir_optimize.py tests/unit/test_cc_ir_optimize.py
git commit -m "feat(cc/ir): RepString instruction node"
```

---

## Task 3: Shared rep emitter + `RepString` codegen

**Files:**
- Modify: `cc/codegen/x86/builtins.py` (extract helpers; reroute
  `builtin_memcpy`/`builtin_memset` at ~793/~814)
- Modify: `cc/codegen/x86/emission.py` (`generate_rep_string`)
- Modify: `cc/codegen/x86/generator.py` (dispatch `ir.RepString`)
- Test: `tests/unit/test_cc_codegen.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_cc_codegen.py` (use the module's existing
IR-to-asm helper; if the file compiles C end-to-end, defer the rep
assertion to Task 8 and instead unit-test the emitter through a tiny
constructed IR program — match the file's existing harness style):

```python
def test_rep_string_fill_byte_emits_rep_stosb() -> None:
    asm = compile_ir_function([
        ir.RepString(operation="fill", element_size=1, dest="buf",
                     source=None, count="n", fill_value=0,
                     counter_signed=False, final_iv=None),
        ir.Return(value=None),
    ])
    assert "rep stosb" in asm
    assert "cld" in asm


def test_rep_string_copy_dword_emits_rep_movsd() -> None:
    asm = compile_ir_function([
        ir.RepString(operation="copy", element_size=4, dest="d",
                     source="s", count="n", fill_value=None,
                     counter_signed=False, final_iv=None),
        ir.Return(value=None),
    ])
    assert "rep movsd" in asm


def test_rep_string_signed_counter_emits_guard() -> None:
    asm = compile_ir_function([
        ir.RepString(operation="fill", element_size=2, dest="d",
                     source=None, count="n", fill_value=7,
                     counter_signed=True, final_iv=None),
        ir.Return(value=None),
    ])
    assert "rep stosw" in asm
    assert "jle" in asm  # signed guard skips when n <= 0
```

If `compile_ir_function` does not already exist in the test module, add a
thin helper that runs a one-function `ir.Program` through `Optimizer` +
the x86 generator and returns the emitted text (mirror the harness the
other tests in this file use to obtain asm from IR/C).

- [ ] **Step 2: Run, verify it fails**

Run: `python3 -m pytest tests/unit/test_cc_codegen.py -k rep_string -q`
Expected: FAIL — no `RepString` emission (no `rep stosb`/`movsd`).

- [ ] **Step 3: Extract shared helpers in `builtins.py`**

Add a suffix helper and two emitters:

```python
    @staticmethod
    def _rep_width_suffix(element_size: int, /) -> str:
        """Map element size 1/2/4 to the string-op mnemonic suffix."""
        return {1: "b", 2: "w", 4: "d"}[element_size]

    def _emit_rep_move(self, *, element_size: int) -> None:
        """Emit ``cld`` + ``rep movs{b,w,d}``.  EDI/ESI/ECX preloaded."""
        self.emit("        cld")
        self.emit(f"        rep movs{self._rep_width_suffix(element_size)}")

    def _emit_rep_fill(self, *, element_size: int) -> None:
        """Emit ``cld`` + ``rep stos{b,w,d}``.  EDI/EAX/ECX preloaded."""
        self.emit("        cld")
        self.emit(f"        rep stos{self._rep_width_suffix(element_size)}")
```

Reroute the builtins (preserving their existing arg-move setup):

```python
        # builtin_memcpy tail (replacing the cld / rep movsb lines):
        self._emit_rep_move(element_size=1)
        self.ax_clear()
```
```python
        # builtin_memset tail (replacing the cld / rep stosb lines):
        self._emit_rep_fill(element_size=1)
        self.ax_clear()
```

- [ ] **Step 4: Add `generate_rep_string` in `emission.py`**

```python
    def generate_rep_string(self, instruction: RepString, /) -> None:
        """Lower a RepString: load EDI/ESI/ECX (+EAX for fill), guard a
        signed count, then cld + rep movs/stos."""
        di = self.target.di_register
        si = self.target.si_register
        cx = self.target.count_register
        acc = self.target.acc
        self._emit_load_value(di, instruction.dest)            # EDI = dest ptr
        if instruction.operation == "copy":
            self._emit_load_value(si, instruction.source)      # ESI = src ptr
        else:
            self._emit_load_value(acc, instruction.fill_value) # EAX = fill
        self._emit_load_value(cx, instruction.count)           # ECX = n
        skip_label = None
        if instruction.counter_signed:
            skip_label = self._fresh_label("rep_skip")
            self.emit(f"        test {cx}, {cx}")
            self.emit(f"        jle {skip_label}")
        if instruction.operation == "copy":
            self._emit_rep_move(element_size=instruction.element_size)
        else:
            self._emit_rep_fill(element_size=instruction.element_size)
        if skip_label is not None:
            self.emit(f"{skip_label}:")
        if instruction.final_iv is not None:
            name, value = instruction.final_iv
            self._emit_store_value(name, value)
        self.ax_clear()
```

Use the module's actual primitives for loading a `Value` into a register
and storing into a named local — read `emission.py` for the existing
spellings (e.g. the helpers `builtin_memcpy` uses via
`_emit_builtin_arg_moves`, and the `mov`/label idioms used elsewhere) and
substitute the real method names for `_emit_load_value` /
`_emit_store_value` / `_fresh_label`. Do not invent names; match what
the file already provides.

- [ ] **Step 5: Dispatch `ir.RepString` in `generator.py`**

In the instruction-emission loop (alongside the `ir.IndexAssign`
handling), add:

```python
        if isinstance(instruction, ir.RepString):
            self.generate_rep_string(instruction)
            continue
```

Match the surrounding loop's control-flow idiom (`continue` vs
`elif`/`return`) exactly as the neighbouring cases use it.

- [ ] **Step 6: Run, verify pass**

Run: `python3 -m pytest tests/unit/test_cc_codegen.py -k rep_string -q`
Expected: PASS (3 tests).

- [ ] **Step 7: Confirm builtins unbroken**

Run: `python3 -m pytest tests/unit/test_cc_codegen.py -k "memcpy or memset" -q`
Expected: PASS (the refactor preserved `rep movsb`/`rep stosb`).

- [ ] **Step 8: Commit**

```bash
git add cc/codegen/x86/builtins.py cc/codegen/x86/emission.py cc/codegen/x86/generator.py tests/unit/test_cc_codegen.py
git commit -m "feat(cc): RepString codegen + shared rep emit helpers"
```

---

## Task 4: Matcher — fill recognition

**Files:**
- Modify: `cc/loops.py` (`recognize_string_loops` + helpers)
- Test: `tests/unit/test_cc_loops.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_cc_loops.py` a hand-built IR for
`for (i=0;i<n;i++) buf[i]=0;` (mirror how the existing strength-reduction
tests in this file construct loop IR — same Label/BranchFalse/IndexAssign
/step/Jump shape) and assert the pass replaces it:

```python
def test_recognize_fill_loop_rewrites_to_rep_string() -> None:
    from cc import ir
    from cc.loops import recognize_string_loops

    body = build_canonical_fill_loop(dest="buf", counter="i", bound="n",
                                      fill=0, element_size=1)
    out = recognize_string_loops(body)
    reps = [x for x in out if isinstance(x, ir.RepString)]
    assert len(reps) == 1
    assert reps[0].operation == "fill"
    assert reps[0].dest == "buf"
    assert reps[0].count == "n"
    # the original IndexAssign / back-jump are gone
    assert not any(isinstance(x, ir.IndexAssign) for x in out)
```

`build_canonical_fill_loop` is a test helper you write in this module
that emits exactly what `cc.ir.Builder._build_for` produces for that
source (init `Copy(i,0)`, `Label(loop)`,
`BranchFalse(left="i", operation="<", right="n", target=end)`,
`LoopBoundary(push=True)`, `IndexAssign(base="buf", index="i", source=0)`,
`LoopBoundary(push=False)`, `Label(step)`,
`BinaryOperation(destination="i", left="i", operation="+", right=1)`,
`Jump(loop)`, `Label(end)`).

- [ ] **Step 2: Run, verify it fails**

Run: `python3 -m pytest tests/unit/test_cc_loops.py -k recognize_fill -q`
Expected: FAIL — `recognize_string_loops` does not exist.

- [ ] **Step 3: Implement the pass skeleton + fill match**

Add to `cc/loops.py`, modeled on `reduce_loop_strength` (build CFG,
`natural_loops`, then per-loop attempt a rewrite of the flat list):

```python
def recognize_string_loops(body: list[ir.Instruction], /) -> list[ir.Instruction]:
    """Rewrite unit-stride fill/copy natural loops into ir.RepString.

    Pipeline mirrors reduce_loop_strength: build the CFG, find natural
    loops, and for each loop that matches the fill or copy idiom, splice
    a RepString (plus IV-final fixup) in place of the loop region.
    Non-matching loops are left untouched.
    """
    cfg = build_cfg(body)
    loops_in_function = natural_loops(cfg)
    if not loops_in_function:
        return body
    rewrites = {}  # loop -> (RepString, final_iv-or-None)
    for loop in loops_in_function:
        match = _match_string_loop(loop)
        if match is not None:
            rewrites[loop] = match
    if not rewrites:
        return body
    return _apply_string_rewrites(body, cfg=cfg, rewrites=rewrites)
```

Implement `_match_string_loop(loop)` returning a `RepString` (fill path
only for this task; raise/return `None` for anything not matching):

- Read the header terminator; require
  `BranchFalse(left=IV, operation in {"<","<=","!="}, right=bound)`.
- `induction_variables = _find_induction_variables(body_block_order, ...)`;
  require exactly one IV equal to the BranchFalse `left`, with step `+1`,
  initialized to 0 in the preheader/pre-loop.
- Collect the loop body's non-scaffolding instructions (everything that
  is not the IV step, `LoopBoundary`, `Label`, or the back-`Jump`).
  Require exactly one `IndexAssign(base=D, index=IV, source=V)` with `V`
  loop-invariant.
- Element size from `D`'s type (the generator's `variable_types` /
  `_index_pointee_size` analog available to the pass — pass the type map
  in if needed); require it in {1,2,4}.
- Build `count` from the operator (`<` -> bound, `<=` -> bound+1 as a
  small BinaryOperation temp, `!=` -> bound) and `counter_signed` from
  the IV/bound type.
- Return `RepString(operation="fill", element_size=E, dest=D,
  source=None, count=count, fill_value=V, counter_signed=signed,
  final_iv=_final_iv_if_live(IV, count, body))`.

Implement `_apply_string_rewrites` to delete each loop's blocks from the
flat instruction list and insert the preheader IV-init (if any
non-trivial) + the `RepString` at the loop's position, preserving all
non-loop instructions. Reuse `insert_preheaders` semantics as the model
for locating block boundaries in the flat list.

- [ ] **Step 4: Run, verify pass**

Run: `python3 -m pytest tests/unit/test_cc_loops.py -k recognize_fill -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cc/loops.py tests/unit/test_cc_loops.py
git commit -m "feat(cc/loops): recognize unit-stride fill loops as RepString"
```

---

## Task 5: Matcher — copy recognition

**Files:**
- Modify: `cc/loops.py` (`_match_string_loop` copy branch)
- Test: `tests/unit/test_cc_loops.py`

- [ ] **Step 1: Write the failing test**

```python
def test_recognize_copy_loop_rewrites_to_rep_string() -> None:
    from cc import ir
    from cc.loops import recognize_string_loops

    body = build_canonical_copy_loop(dest="d", source="s", counter="i",
                                     bound="n", element_size=4)
    out = recognize_string_loops(body)
    reps = [x for x in out if isinstance(x, ir.RepString)]
    assert len(reps) == 1
    assert reps[0].operation == "copy"
    assert reps[0].source == "s"
    assert reps[0].element_size == 4
```

`build_canonical_copy_loop` emits the copy body as the two-instruction
TAC `Index(destination=t, base="s", index="i")` then
`IndexAssign(base="d", index="i", source=t)`.

- [ ] **Step 2: Run, verify it fails**

Run: `python3 -m pytest tests/unit/test_cc_loops.py -k recognize_copy -q`
Expected: FAIL — copy body shape is rejected (only fill matches so far).

- [ ] **Step 3: Add the copy branch to `_match_string_loop`**

When the non-scaffolding body is exactly two instructions
`Index(destination=t, base=S, index=IV)` then
`IndexAssign(base=D, index=IV, source=t)`:

- Require `t` used only by that `IndexAssign` (a single-use temp).
- Require `S` and `D` element sizes equal and in {1,2,4}.
- Return `RepString(operation="copy", element_size=E, dest=D, source=S,
  count=count, fill_value=None, counter_signed=signed,
  final_iv=_final_iv_if_live(IV, count, body))`.

Note in a code comment: a forward element-wise copy is semantically
identical to forward `rep movs` even when `S`/`D` overlap (both ascend),
so overlap is **not** a rejection condition.

- [ ] **Step 4: Run, verify pass**

Run: `python3 -m pytest tests/unit/test_cc_loops.py -k recognize_copy -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cc/loops.py tests/unit/test_cc_loops.py
git commit -m "feat(cc/loops): recognize unit-stride copy loops as RepString"
```

---

## Task 6: Rejection cases (conservative matcher)

**Files:**
- Modify: `cc/loops.py` (guards in `_match_string_loop`)
- Test: `tests/unit/test_cc_loops.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_reject_non_unit_stride() -> None:
    body = build_canonical_fill_loop(dest="buf", counter="i", bound="n",
                                     fill=0, element_size=1, step=2)
    assert not _has_rep_string(recognize_string_loops(body))


def test_reject_extra_body_statement() -> None:
    body = build_fill_loop_with_extra_store(dest="buf", other="log")
    assert not _has_rep_string(recognize_string_loops(body))


def test_reject_index_not_the_iv() -> None:
    body = build_fill_loop_indexed_by("j")  # body stores buf[j], loop on i
    assert not _has_rep_string(recognize_string_loops(body))


def test_reject_width_mismatch_copy() -> None:
    body = build_canonical_copy_loop(dest="d", source="s", counter="i",
                                     bound="n", element_size=4,
                                     source_element_size=1)
    assert not _has_rep_string(recognize_string_loops(body))
```

`_has_rep_string` is a one-line local helper:
`any(isinstance(x, ir.RepString) for x in out)`.

- [ ] **Step 2: Run, verify they fail**

Run: `python3 -m pytest tests/unit/test_cc_loops.py -k reject -q`
Expected: FAIL for any case the current matcher accepts too eagerly.

- [ ] **Step 3: Tighten `_match_string_loop`**

Ensure each guard returns `None`: step `!= 1`; body has any instruction
beyond the recognized fill/copy shape; the `IndexAssign`/`Index` index is
not exactly the IV; copy source/dest element sizes differ; the IV is
address-taken (scan the function for `AddressOf`/`&i` analog in the IR).

- [ ] **Step 4: Run, verify pass**

Run: `python3 -m pytest tests/unit/test_cc_loops.py -k reject -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add cc/loops.py tests/unit/test_cc_loops.py
git commit -m "test(cc/loops): conservative rejection of non-idiom loops"
```

---

## Task 7: Wire the pass into the optimizer

**Files:**
- Modify: `cc/ir_optimize.py` (`_optimize_body`, import)
- Test: `tests/unit/test_cc_codegen.py`

- [ ] **Step 1: Write the failing end-to-end test**

Add to `tests/unit/test_cc_codegen.py` (using the file's C-to-asm
harness):

```python
def test_fill_loop_compiles_to_rep_stosb() -> None:
    asm = compile_c("""
        void clear(unsigned char *buf, int n) {
            int i;
            for (i = 0; i < n; i++) buf[i] = 0;
        }
    """)
    assert "rep stosb" in asm
    assert "jle" in asm  # signed n -> guard


def test_copy_loop_compiles_to_rep_movsd() -> None:
    asm = compile_c("""
        void copy(unsigned int *d, unsigned int *s, unsigned int n) {
            unsigned int i;
            for (i = 0; i < n; i++) d[i] = s[i];
        }
    """)
    assert "rep movsd" in asm
    assert "jle" not in asm  # unsigned n -> no guard
```

- [ ] **Step 2: Run, verify it fails**

Run: `python3 -m pytest tests/unit/test_cc_codegen.py -k "rep_stosb or rep_movsd" -q`
Expected: FAIL — the pass is not invoked, so the loop lowers scalar.

- [ ] **Step 3: Invoke the pass in `_optimize_body`**

Import at the top of `cc/ir_optimize.py`:

```python
from cc.loops import hoist_loop_invariants, recognize_string_loops, reduce_loop_strength
```

In `_optimize_body`, insert the call **after** the SSA block and
**before** the LICM block (so the matcher sees the clean `dst[i]=src[i]`
body, before strength reduction rewrites indices):

```python
        if (after_rep := recognize_string_loops(current)) != current:
            current = self._scalar_fixed_point(after_rep)
```

- [ ] **Step 4: Run, verify pass**

Run: `python3 -m pytest tests/unit/test_cc_codegen.py -k "rep_stosb or rep_movsd" -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Run the full codegen + loops + IR unit suites**

Run:
```bash
python3 -m pytest tests/unit/test_cc_codegen.py tests/unit/test_cc_loops.py \
  tests/unit/test_cc_ir_optimize.py tests/unit/test_cc_ssa.py -q
```
Expected: all PASS (no regression from wiring the pass into the pipeline).

- [ ] **Step 6: Commit**

```bash
git add cc/ir_optimize.py tests/unit/test_cc_codegen.py
git commit -m "feat(cc): enable rep-string loop recognition in the optimizer"
```

---

## Task 8: Runtime correctness test in QEMU

**Files:**
- Create: `user/programs/rep_loops_test.c`
- Modify: `tests/test_programs.py` (new entry)
- Modify: `tests/bboeos.h` if the program uses any builtin needing a
  prototype there (see the cc-compat memory; add prototypes
  alphabetically or `test_cc_compatibility` fails).

- [ ] **Step 1: Write the test program**

Create `user/programs/rep_loops_test.c` exercising all three widths,
fill and copy, plus the n<=0 signed-guard edge:

```c
#include "stdio.h"

void main(void) {
    unsigned char b[8];
    unsigned short h[8];
    unsigned int  w[8];
    unsigned char cb[8];
    int i;

    for (i = 0; i < 8; i++) b[i] = 0x41;        /* rep stosb */
    for (i = 0; i < 8; i++) h[i] = 0x1234;      /* rep stosw */
    for (i = 0; i < 8; i++) w[i] = 0xdeadbeef;  /* rep stosd */
    for (i = 0; i < 8; i++) cb[i] = b[i];       /* rep movsb */
    for (i = 0; i < -3; i++) b[i] = 0;          /* signed guard: no-op */

    printf("%x %x %x %x %x\n", b[7], h[7], w[7], cb[0], b[0]);
}
```

Expected printed line: `41 1234 deadbeef 41 41`.

- [ ] **Step 2: Add the `tests/test_programs.py` entry**

Add an entry running `rep_loops_test` and matching the output with a
regex (match the file's existing entry shape):

```python
    ("rep_loops_test", r"41 1234 deadbeef 41 41"),
```

- [ ] **Step 3: Run, verify pass (bbfs + ext2)**

Run:
```bash
python3 tests/test_programs.py rep_loops_test
python3 tests/test_programs.py --filesystem ext2 rep_loops_test
```
Expected: PASS — the printed checksum line matches, proving the rewrite
is semantically correct at runtime (including the signed-guard no-op).

- [ ] **Step 4: Commit**

```bash
git add user/programs/rep_loops_test.c tests/test_programs.py tests/bboeos.h
git commit -m "test(programs): runtime check for rep-string loop rewrite"
```

---

## Task 9: Full CI matrix + changelog

**Files:**
- Modify: `docs/CHANGELOG.md` (Unreleased / Changed)

- [ ] **Step 1: Add a changelog entry** under `## Unreleased` ->
`### Changed`:

```markdown
- **cc.py recognizes hand-written init/copy loops as `rep` string ops.**
  A unit-stride `for (i=0;i<n;i++) dst[i]=V;` / `dst[i]=src[i];` now
  lowers to `rep stos{b,w,d}` / `rep movs{b,w,d}` (element widths 1/2/4)
  via a new IR pass over natural loops.  Signed loop bounds are guarded
  so a negative count is a no-op (matching C), not a 4 GB `rep`.  The
  self-hosted assembler gained `movsd`/`stosd` to round-trip the 4-byte
  forms.
```

- [ ] **Step 2: Reflow the changelog**

Run: `python3 tools/wrap_md.py docs/CHANGELOG.md`
Expected: idempotent (writes only if reflow changed anything).

- [ ] **Step 3: Run every suite in `.github/workflows/test.yml`**

This change touches both codegen and the IR pipeline, so run the full
matrix locally (per project convention for kernel/codegen-architecture
changes), not just the asm subset. At minimum:

```bash
python3 -m pytest tests/unit -q
python3 tests/test_asm.py
python3 tests/test_bboefs.py
python3 tests/test_programs.py
python3 tests/test_programs.py --filesystem ext2
```
Expected: all green. Investigate and fix any failure before proceeding.

- [ ] **Step 4: Commit**

```bash
git add docs/CHANGELOG.md
git commit -m "docs(changelog): rep-string loop recognition"
```

---

## Self-review notes

- **Spec coverage:** Task 1 = asm.c movsd/stosd; Tasks 2–3 = RepString
  node + codegen + shared helper; Tasks 4–6 = the IR matcher (fill, copy,
  rejection); Task 7 = pass placement before LICM/SR; Task 8 = runtime
  correctness incl. the signed guard; the signed-count guard appears in
  Tasks 3, 7, 8. All design sections map to a task.
- **Known integration risk to watch during execution:** the placeholder
  method names in Task 3 Step 4 (`_emit_load_value` / `_emit_store_value`
  / `_fresh_label`) and Task 4/6 type-map access must be reconciled with
  the real `emission.py` / generator APIs while implementing — read the
  surrounding code and substitute the actual spellings rather than
  introducing new helpers. This is the one spot the plan defers to
  in-situ discovery, because the exact private-helper names are an
  implementation detail of files that change frequently.
- **Clobber integration:** verify pinned-register variables live across a
  `RepString` spill correctly (EDI/ESI/ECX/EAX). If the generator's
  clobber pre-pass (`_clobbers_for_call`) does not already cover
  non-`Call` instructions, register `RepString`'s clobber set there; add
  a unit test pinning a variable across a recognized loop if the QEMU
  test surfaces a clobber bug.
</content>
