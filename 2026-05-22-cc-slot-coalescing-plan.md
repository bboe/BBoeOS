# cc.py Stack-Slot Liveness Coalescing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
superpowers:subagent-driven-development (recommended) or
superpowers:executing-plans to implement this plan task-by-task. Steps use
checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generalise `cc.codegen.liveness.LivenessAnalyzer` to walk both AST and
post-IR `Instruction` bodies, then run a new slot-coalescing pass after IR-temp
allocation that lets same-size, non-interfering, non-address-taken frame locals
share a slot.

**Architecture:** Phase 1 extends the analyzer with IR-instruction use/def + CFG
cases (the existing register-pin pass keeps consuming pre-IR AST and stays
byte-identical; new instantiations on post-IR bodies see `_ir_*` temps). Phase 2
adds a slot-coalescing pass in `generate_function` after `_collect_ir_temps`
allocation: build the interference graph from the post-IR body,
eligibility-filter, group by size, greedy-colour, rewrite `self.locals` and
`self.frame_size`.

**Tech Stack:** Python 3 (no new deps). Touches `cc/codegen/liveness.py`,
`cc/codegen/x86/emission.py`, `cc/codegen/x86/generator.py`,
`tests/unit/test_cc_liveness.py`, plus a new
`tests/unit/test_cc_slot_coalescing.py`.

**Spec:** `design-specs:2026-05-22-cc-slot-coalescing-design.md`.

---

## Notes for the implementing engineer

- The IR instruction kinds are defined in `cc/ir.py`: `BinaryOperation`, `Copy`,
  `Call`, `Index`, `IndexAssign`, `Label`, `Jump`, `BranchFalse`, `CarryBranch`,
  `Return`, `InlineAsm`, `Block`.  `Value` operands are `int | str |
  ast_nodes.AddressOf` — a `str` operand is a variable name (either a named
  local or an `_ir_*` temp).
- The function body passed to `generate_function` may be the AST body (no IR
  built — currently only `main`) or the IR-lowered `ir_body` (a
  `list[ir.Instruction]`).  Slot coalescing runs only when an `ir_body` is
  present.
- The register-pin call site at `cc/codegen/x86/generator.py:3239` passes the
  pre-IR AST body.  After the analyzer is generalised, that path must still
  produce a byte-identical interference dict; existing tests in
  `tests/unit/test_cc_liveness.py` are the regression net.
- Commits are small and frequent.  The smoke-test driver `./make_os.sh` plus
  `python3 cc.py --target kernel kernel/drivers/sb16.c` should be run after each
  end-of-task commit to catch silent miscompiles early.
- Pre-commit hook config is absent in this repo; commits need
  `PRE_COMMIT_ALLOW_NO_CONFIG=1 git commit ...` or `git config --global
  init.templateDir` workaround.  All commits below use the env-var form.

---

## Phase 1 — Generalize `LivenessAnalyzer`

### Task 1: Add unit tests for `Block(node=AST)` delegation

**Files:**
- Modify: `tests/unit/test_cc_liveness.py`

- [ ] **Step 1: Add IR imports + Block test**

Append to `tests/unit/test_cc_liveness.py`:

```python
from cc import ir
from cc.ast_nodes import Function as AstFunction


def _block(node: object) -> ir.Block:
    return ir.Block(node=node)


def test_liveness_block_delegates_to_ast_cases() -> None:
    # Two named locals; the second is assigned via a Block-wrapped
    # AST Assign.  Liveness must see the write to b and the read of a.
    body: list[object] = [
        _declaration("a", 1),
        _block(_assign("b", _var("a"))),
    ]
    analyzer = LivenessAnalyzer(body=body)
    interference = analyzer.interference()
    # a is live across the Block (it's read inside), b is defined.
    # No overlap with anything else: a and b don't both live at the
    # same point because a is defined, then read+killed, then b is
    # defined.  Still, the Block delegate must not raise.
    assert "a" in {*interference} | set().union(*interference.values()) or interference == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest
tests/unit/test_cc_liveness.py::test_liveness_block_delegates_to_ast_cases -v`

Expected: FAIL with `LivenessAnalysisError: liveness: unhandled statement node
Block`.

- [ ] **Step 3: Commit the failing test**

```bash
PRE_COMMIT_ALLOW_NO_CONFIG=1 git add tests/unit/test_cc_liveness.py
PRE_COMMIT_ALLOW_NO_CONFIG=1 git commit -m "test(cc): expect LivenessAnalyzer to handle ir.Block delegation"
```

### Task 2: Implement `ir.Block` use/def + numbering

**Files:**
- Modify: `cc/codegen/liveness.py`

- [ ] **Step 1: Add the IR import**

In the import block at top of `cc/codegen/liveness.py` (after the `cc.ast_nodes`
import):

```python
from cc import ir
```

- [ ] **Step 2: Handle `ir.Block` in `_number_statement`**

In `_number_statement` (around line 346), after the `Compound` branch and before
the method ends, add:

```python
        elif isinstance(statement, ir.Block):
            self._number_statement(statement.node)
```

Rationale: the inner AST node needs its own statement id so AST cases (which
look up `self.node_to_id[id(statement)]`) can find it when recursed-into during
`_collect_use_def`.

- [ ] **Step 3: Handle `ir.Block` in `_wire_statement`**

In `_wire_statement` (around line 370), before the default fall-through at the
end, add:

```python
        if isinstance(statement, ir.Block):
            # Block delegates to its wrapped AST node for CFG.
            inner_id = self.node_to_id[id(statement.node)]
            self._wire_statement(statement.node, fallthrough=fallthrough)
            statement_info.successors = [inner_id]
            return
```

- [ ] **Step 4: Handle `ir.Block` in `_collect_use_def`**

In `_collect_use_def` (around line 232), before the final `raise
LivenessAnalysisError`, add:

```python
        if isinstance(statement, ir.Block):
            # Delegate use/def to the wrapped AST node.
            self._collect_use_def(statement.node)
            inner_info = self.statements[self.node_to_id[id(statement.node)]]
            statement_info.uses |= inner_info.uses
            statement_info.definitions |= inner_info.definitions
            return
```

- [ ] **Step 5: Run the Block test + the existing suite**

Run: `python3 -m pytest tests/unit/test_cc_liveness.py -v`

Expected: all tests including `test_liveness_block_delegates_to_ast_cases` PASS.

- [ ] **Step 6: Commit**

```bash
PRE_COMMIT_ALLOW_NO_CONFIG=1 git add cc/codegen/liveness.py
PRE_COMMIT_ALLOW_NO_CONFIG=1 git commit -m "feat(cc): LivenessAnalyzer delegates ir.Block to wrapped AST"
```

### Task 3: Add unit tests for IR use/def shapes

**Files:**
- Modify: `tests/unit/test_cc_liveness.py`

- [ ] **Step 1: Add tests covering each IR instruction kind that names
  operands**

Append to `tests/unit/test_cc_liveness.py`:

```python
def test_liveness_ir_copy_def_and_use() -> None:
    # _ir_0 = a  →  _ir_0 def, a use.
    body: list[object] = [
        _declaration("a", 1),
        ir.Copy(destination="_ir_0", source="a"),
        _block(_assign("b", _var("_ir_0"))),
    ]
    interference = LivenessAnalyzer(body=body).interference()
    # _ir_0 is live across the Copy → Block edge; a is killed at the
    # Copy because the Copy reads it, then the value lives in _ir_0.
    # The test just asserts the analyzer doesn't raise and produces
    # a non-empty graph naming _ir_0.
    assert "_ir_0" in {*interference} | set().union(*interference.values(), set())


def test_liveness_ir_binary_operation_collects_str_operands() -> None:
    # _ir_1 = a + b  →  _ir_1 def, a + b uses.
    body: list[object] = [
        _declaration("a", 1),
        _declaration("b", 2),
        ir.BinaryOperation(destination="_ir_1", left="a", operation="+", right="b"),
        _block(_assign("c", _var("_ir_1"))),
    ]
    interference = LivenessAnalyzer(body=body).interference()
    # a and b are both live entering the BinaryOperation — they must
    # interfere with each other.
    assert "b" in interference.get("a", set())


def test_liveness_ir_call_uses_args_and_defs_destination() -> None:
    # _ir_2 = f(a, b)
    body: list[object] = [
        _declaration("a", 1),
        _declaration("b", 2),
        ir.Call(args=("a", "b"), destination="_ir_2", name="f"),
        _block(_assign("c", _var("_ir_2"))),
    ]
    interference = LivenessAnalyzer(body=body).interference()
    assert "b" in interference.get("a", set())
    assert "_ir_2" in {*interference} | set().union(*interference.values(), set())


def test_liveness_ir_index_assign_reads_base_and_source() -> None:
    body: list[object] = [
        _declaration("a", 1),
        _declaration("i", 0),
        # arr[i] = a — uses arr, i, a; no def.
        ir.IndexAssign(base="arr", index="i", source="a"),
    ]
    # arr is a free variable (not declared); the analyzer should still
    # collect it as a use and not crash.
    LivenessAnalyzer(body=body).interference()


def test_liveness_ir_return_uses_str_value() -> None:
    body: list[object] = [
        _declaration("a", 1),
        ir.Return(value="a"),
    ]
    LivenessAnalyzer(body=body).interference()
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `python3 -m pytest tests/unit/test_cc_liveness.py -k "ir_" -v`

Expected: each new test FAILs with `LivenessAnalysisError: liveness: unhandled
statement node <kind>`.

- [ ] **Step 3: Commit the failing tests**

```bash
PRE_COMMIT_ALLOW_NO_CONFIG=1 git add tests/unit/test_cc_liveness.py
PRE_COMMIT_ALLOW_NO_CONFIG=1 git commit -m "test(cc): expect LivenessAnalyzer to handle IR instruction use/def"
```

### Task 4: Implement IR use/def in `_collect_use_def`

**Files:**
- Modify: `cc/codegen/liveness.py`

- [ ] **Step 1: Add a helper for IR `Value` operands**

In `cc/codegen/liveness.py`, near `_add_expression_uses`, add:

```python
    @staticmethod
    def _ir_value_use(value: object, accumulator: set[str]) -> None:
        """Record an IR ``Value`` operand as a use if it names a variable.

        IR ``Value`` is ``int | str | ast_nodes.AddressOf``.  Integers
        are ignored, strings name a local or an ``_ir_*`` temp,
        ``AddressOf`` references its inner var.
        """
        if isinstance(value, str):
            accumulator.add(value)
            return
        if isinstance(value, AddressOf) and isinstance(value.var, Var):
            accumulator.add(value.var.name)
            return
```

- [ ] **Step 2: Add IR use/def cases in `_collect_use_def`**

In `_collect_use_def`, before the `if isinstance(statement, ir.Block):` branch
added in Task 2, add (insert these checks right after the AST cases and before
the final `raise`):

```python
        if isinstance(statement, ir.Copy):
            statement_info.definitions.add(statement.destination)
            self._ir_value_use(statement.source, statement_info.uses)
            return
        if isinstance(statement, ir.BinaryOperation):
            statement_info.definitions.add(statement.destination)
            self._ir_value_use(statement.left, statement_info.uses)
            self._ir_value_use(statement.right, statement_info.uses)
            return
        if isinstance(statement, ir.Index):
            statement_info.definitions.add(statement.destination)
            statement_info.uses.add(statement.base)
            self._ir_value_use(statement.index, statement_info.uses)
            return
        if isinstance(statement, ir.IndexAssign):
            statement_info.uses.add(statement.base)
            self._ir_value_use(statement.index, statement_info.uses)
            self._ir_value_use(statement.source, statement_info.uses)
            return
        if isinstance(statement, ir.Call):
            if statement.destination is not None:
                statement_info.definitions.add(statement.destination)
            for argument in statement.args:
                self._ir_value_use(argument, statement_info.uses)
            return
        if isinstance(statement, ir.Return):
            self._ir_value_use(statement.value, statement_info.uses)
            return
        if isinstance(statement, ir.BranchFalse):
            self._ir_value_use(statement.left, statement_info.uses)
            self._ir_value_use(statement.right, statement_info.uses)
            return
        if isinstance(statement, ir.CarryBranch):
            # ``call_ast`` is an AST Call with the original argument
            # expressions; defer to the AST walker to collect uses.
            self._add_expression_uses(statement.call_ast, statement_info.uses)
            return
        if isinstance(statement, (ir.Label, ir.Jump, ir.InlineAsm)):
            # Label / Jump carry no operands.  InlineAsm is opaque —
            # it cannot reference IR temp names directly (its content
            # is a raw string), so no operand collection.
            return
```

- [ ] **Step 3: Add IR numbering in `_number_statement`**

Bare `ir.Instruction` nodes need ids too.  Extend `_number_statement` so the
`_new_id(statement)` call at its top fires for any IR instruction (it already
does — but add `Label` registration alongside the AST `Label`):

```python
        if isinstance(statement, ir.Label):
            self.labels[statement.name] = statement_id
```

(Place this next to the existing AST `Label` registration around line 351.)

- [ ] **Step 4: Run the IR use/def tests**

Run: `python3 -m pytest tests/unit/test_cc_liveness.py -k "ir_" -v`

Expected: all new IR tests PASS.  Run the full `tests/unit/test_cc_liveness.py`
suite to confirm no regression: `python3 -m pytest
tests/unit/test_cc_liveness.py -v`.

- [ ] **Step 5: Commit**

```bash
PRE_COMMIT_ALLOW_NO_CONFIG=1 git add cc/codegen/liveness.py
PRE_COMMIT_ALLOW_NO_CONFIG=1 git commit -m "feat(cc): LivenessAnalyzer handles IR Copy/BinOp/Index/Call/Return/Branch"
```

### Task 5: Implement IR CFG wiring

**Files:**
- Modify: `cc/codegen/liveness.py`, `tests/unit/test_cc_liveness.py`

- [ ] **Step 1: Add a CFG test for `ir.Jump` + `ir.Label`**

Append to `tests/unit/test_cc_liveness.py`:

```python
def test_liveness_ir_jump_and_label_disjoint_branches_no_interference() -> None:
    # if-style hand-built CFG:
    #   _ir_0 = a
    #   BranchFalse a == 0 -> L_else
    #   _ir_0 = b   (then arm)
    #   Jump L_end
    #   Label L_else
    #   _ir_0 = c   (else arm)
    #   Label L_end
    # ``b`` and ``c`` are on disjoint arms and must NOT interfere.
    body: list[object] = [
        _declaration("a", 1),
        _declaration("b", 2),
        _declaration("c", 3),
        ir.BranchFalse(left="a", operation="==", right=0, target="L_else"),
        ir.Copy(destination="_ir_0", source="b"),
        ir.Jump(target="L_end"),
        ir.Label(name="L_else"),
        ir.Copy(destination="_ir_0", source="c"),
        ir.Label(name="L_end"),
    ]
    interference = LivenessAnalyzer(body=body).interference()
    assert "c" not in interference.get("b", set())
    assert "b" not in interference.get("c", set())
```

Run: `python3 -m pytest
tests/unit/test_cc_liveness.py::test_liveness_ir_jump_and_label_disjoint_branches_no_interference
-v`.  Expected: FAIL — likely with `b` and `c` interfering because Jump/Label
fall through linearly today.

- [ ] **Step 2: Implement IR CFG wiring in `_wire_statement`**

Add the following cases in `_wire_statement` (place them before the `ir.Block`
branch added in Task 2 / before the default fallthrough):

```python
        if isinstance(statement, ir.Jump):
            target_id = self.labels.get(statement.target, EXIT_ID)
            statement_info.successors = [target_id]
            return
        if isinstance(statement, ir.BranchFalse):
            target_id = self.labels.get(statement.target, EXIT_ID)
            statement_info.successors = [target_id, fallthrough]
            return
        if isinstance(statement, ir.CarryBranch):
            target_id = self.labels.get(statement.target, EXIT_ID)
            statement_info.successors = [target_id, fallthrough]
            return
        if isinstance(statement, ir.Return):
            statement_info.successors = [EXIT_ID]
            return
        if isinstance(statement, ir.Label):
            statement_info.successors = [fallthrough]
            return
        if isinstance(statement, (ir.Copy, ir.BinaryOperation, ir.Index, ir.IndexAssign, ir.Call, ir.InlineAsm)):
            statement_info.successors = [fallthrough]
            return
```

The label table is built by `_collect_labels` → `_number_statement` (Task 4 step
3 already registers `ir.Label` names there), so `self.labels` is populated by
the time `_wire_statement` runs.

- [ ] **Step 3: Run the CFG test + full liveness suite**

Run: `python3 -m pytest tests/unit/test_cc_liveness.py -v`.  Expected: every
test PASS, including the new disjoint-branches test.

- [ ] **Step 4: Commit**

```bash
PRE_COMMIT_ALLOW_NO_CONFIG=1 git add cc/codegen/liveness.py tests/unit/test_cc_liveness.py
PRE_COMMIT_ALLOW_NO_CONFIG=1 git commit -m "feat(cc): LivenessAnalyzer wires IR Jump/BranchFalse/Label CFG edges"
```

### Task 6: Regression-test the existing pin pass

**Files:** none modified.

- [ ] **Step 1: Run the full unit / integration matrix**

Run, in order:

```bash
python3 -m pytest tests/unit/test_cc_liveness.py tests/unit/test_cc_codegen.py -v
python3 tests/test_asm.py
python3 tests/test_programs.py
python3 tests/test_bboefs.py
```

Expected: all pass.  The pin-pass call site (`_choose_pin_assignments`) still
feeds the analyzer a pre-IR AST body; no IR cases fire, so the output is
byte-identical to before this branch.

- [ ] **Step 2: Build the OS image + boot smoke**

```bash
./make_os.sh
qemu-system-i386 -drive file=drive.img,format=raw -serial stdio -display none -no-reboot &
QEMU_PID=$!
sleep 5
kill $QEMU_PID 2>/dev/null
```

Expected: the prompt banner reaches serial; no panic.

- [ ] **Step 3: Tag the phase-1 boundary**

```bash
PRE_COMMIT_ALLOW_NO_CONFIG=1 git tag -a slot-coalescing-phase-1 -m "LivenessAnalyzer generalised to walk IR bodies"
```

(Local tag for bisection convenience; not pushed.)

---

## Phase 2 — Slot-coalescing pass

### Task 7: Expose the address-taken set as a reusable method

**Files:**
- Modify: `cc/codegen/x86/generator.py`

- [ ] **Step 1: Locate the existing address-taken collection**

In `cc/codegen/x86/generator.py`, around line 2904, the
`_select_auto_pin_candidates` method computes `address_taken: set[str]`.
Refactor that collection into a small helper that can be called twice (once from
the pin pass, once from the slot pass) without redoing the walk.

Add a new method on the same class, placed alphabetically — find the right spot
by `grep -n "    def " cc/codegen/x86/generator.py` and slot it between methods
whose names sandwich `_collect_address_taken`:

```python
    def _collect_address_taken(self, body: list[Node], /) -> set[str]:
        """Return the set of local names whose address is taken anywhere in *body*.

        Walks the AST recursively, recording any ``AddressOf`` whose
        target is a ``Var``.  Conservative: only ``Var`` targets are
        recognised, which matches what :meth:`_select_auto_pin_candidates`
        already records.
        """
        names: set[str] = set()
        def walk(node: Node) -> None:
            if isinstance(node, AddressOf) and isinstance(node.var, Var):
                names.add(node.var.name)
            for node_field in fields(node):
                value = getattr(node, node_field.name)
                if isinstance(value, Node):
                    walk(value)
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, Node):
                            walk(item)
        for statement in body:
            walk(statement)
        return names
```

- [ ] **Step 2: Switch `_select_auto_pin_candidates` to use the helper**

Find the existing in-line `address_taken` collection in
`_select_auto_pin_candidates` (the `address_taken.add(node.var.name)` line at
~3032).  Replace the local accumulation with a single call to
`self._collect_address_taken(body)` near the top of the method, and remove the
redundant in-line collection.  Run `python3 -m pytest
tests/unit/test_cc_codegen.py -v` to confirm the pin pass still matches.

- [ ] **Step 3: Commit**

```bash
PRE_COMMIT_ALLOW_NO_CONFIG=1 git add cc/codegen/x86/generator.py
PRE_COMMIT_ALLOW_NO_CONFIG=1 git commit -m "refactor(cc): extract _collect_address_taken from auto-pin candidate scan"
```

### Task 8: Write the slot-coalescing module — failing test first

**Files:**
- Create: `tests/unit/test_cc_slot_coalescing.py`

- [ ] **Step 1: Write the first behavioural test**

Create `tests/unit/test_cc_slot_coalescing.py`:

```python
"""Slot-coalescing pass tests.

End-to-end check: compile a small kernel function whose frame is
dominated by IR temps spilled around side-effecting calls, and assert
the prologue's frame size shrinks compared to the no-coalesce
baseline.
"""

from __future__ import annotations

import re
import subprocess


def _compile_kernel_source(source: str) -> str:
    """Return the assembly text produced by `python3 cc.py --target kernel -` for *source*."""
    result = subprocess.run(
        ["python3", "cc.py", "--target", "kernel", "-"],
        input=source,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _frame_size(assembly: str, function_name: str) -> int:
    """Parse the ``sub esp, N`` immediately following ``<function_name>:``."""
    pattern = re.compile(rf"^{function_name}:\n(?:.*\n)+?\s+sub esp, (\d+)\b", re.MULTILINE)
    match = pattern.search(assembly)
    if match is None:
        return 0
    return int(match.group(1))


def test_slot_coalescing_shrinks_call_heavy_frame() -> None:
    # Three named ints used in disjoint phases — each could in
    # principle share a slot with a later IR temp.
    source = """
extern void sink(int);
void hot(void) {
    int a; int b; int c;
    a = 1; sink(a);
    b = 2; sink(b);
    c = 3; sink(c);
}
"""
    assembly = _compile_kernel_source(source)
    # Frame size must accommodate the live variables but should be
    # smaller than the naive sum (3 * 4 = 12 bytes) — at minimum, a
    # and b should coalesce because they have disjoint live ranges.
    frame = _frame_size(assembly, "hot")
    assert frame < 12, f"expected frame < 12 bytes, got {frame}"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m pytest tests/unit/test_cc_slot_coalescing.py -v`

Expected: FAIL — frame is currently 12 (or larger with IR temps).

- [ ] **Step 3: Commit the failing test**

```bash
PRE_COMMIT_ALLOW_NO_CONFIG=1 git add tests/unit/test_cc_slot_coalescing.py
PRE_COMMIT_ALLOW_NO_CONFIG=1 git commit -m "test(cc): expect slot coalescing on disjoint named locals"
```

### Task 9: Implement the slot-coalescing pass

**Files:**
- Modify: `cc/codegen/x86/emission.py`, `cc/codegen/x86/generator.py`

- [ ] **Step 1: Add the coalescing method to the generator class**

Find an alphabetically-appropriate spot in `cc/codegen/x86/generator.py` (e.g.
between methods whose names sandwich `_coalesce_frame_slots`).  Add:

```python
    def _coalesce_frame_slots(self, *, ir_body: list[Instruction], parameters: list[Param], ast_body: list[Node]) -> None:
        """Pack frame-allocated locals onto shared slots by liveness.

        Run after ``scan_locals`` and the IR-temp allocation loop in
        :meth:`generate_function`.  Builds an interference graph from
        *ir_body* (which contains both bare IR instructions and
        ``Block``-wrapped AST), filters to coalescing-eligible names,
        groups by slot width, greedy-colours, and rewrites
        ``self.locals`` plus ``self.frame_size``.

        Returns silently (no rewrite) if the analyzer rejects the
        body.  Per the spec's "fail loudly" rule, raises propagate.
        """
        try:
            analyzer = LivenessAnalyzer(body=ir_body, parameters=parameters)
            interference = analyzer.interference()
        except LivenessAnalysisError:
            raise
        address_taken = self._collect_address_taken(ast_body)
        eligible: list[tuple[str, int]] = []
        for name, offset in self.locals.items():
            if offset <= 0:
                continue  # parameters / register-convention slots
            if name in self.pinned_register:
                continue
            if name in self.local_stack_arrays:
                continue
            if name in self.virtual_long_locals:
                continue
            if name in address_taken:
                continue
            # Slot width: the high-water mark minus the previous
            # name's offset would be ideal, but ``self.locals``
            # only records the offset (running total) per name.
            # Width is recoverable by sorting names by offset and
            # diffing.  Build width map below.
            eligible.append((name, offset))
        if not eligible:
            return
        # Reconstruct per-name slot width from sorted offsets.
        widths: dict[str, int] = {}
        sorted_by_offset = sorted(self.locals.items(), key=lambda pair: pair[1])
        previous = 0
        for name, offset in sorted_by_offset:
            if offset <= 0:
                continue
            widths[name] = offset - previous
            previous = offset
        # Restrict to eligible names with widths in {1, 2, 4}.
        coalescable: list[str] = [name for name, _ in eligible if widths.get(name) in (1, 2, 4)]
        if not coalescable:
            return
        # Group by width; greedy colour within each group.
        by_width: dict[int, list[str]] = {}
        for name in coalescable:
            by_width.setdefault(widths[name], []).append(name)
        # Track colour assignment: name -> colour id, scoped per width.
        name_to_colour: dict[str, tuple[int, int]] = {}  # name -> (width, colour)
        colour_members: dict[tuple[int, int], list[str]] = {}
        for width, names in by_width.items():
            # Sort by descending interference degree, tie-break by name.
            names_sorted = sorted(names, key=lambda n: (-len(interference.get(n, set())), n))
            next_colour = 0
            for name in names_sorted:
                neighbours = interference.get(name, set())
                placed = False
                for colour in range(next_colour):
                    members = colour_members.get((width, colour), [])
                    if any(member in neighbours for member in members):
                        continue
                    colour_members.setdefault((width, colour), []).append(name)
                    name_to_colour[name] = (width, colour)
                    placed = True
                    break
                if not placed:
                    colour_members.setdefault((width, next_colour), []).append(name)
                    name_to_colour[name] = (width, next_colour)
                    next_colour += 1
        # Names that aren't coalescable keep their existing offsets;
        # all positive-offset entries get rebuilt below from scratch.
        positive_locals = {name: offset for name, offset in self.locals.items() if offset > 0}
        non_coalescable = {name: widths.get(name, 0) for name in positive_locals if name not in name_to_colour}
        # Re-layout: walk colour groups largest-width first to keep
        # alignment monotone, then non-coalescable names in their
        # original offset order.
        new_offsets: dict[str, int] = {}
        running = 0
        # First: coalesced colours, biggest width first, stable
        # ordering within each width.
        for width in sorted({w for w, _ in colour_members}, reverse=True):
            colour_ids = sorted({c for w, c in colour_members if w == width})
            for colour in colour_ids:
                running += width
                for member in colour_members[(width, colour)]:
                    new_offsets[member] = running
        # Then: non-coalescable, original-offset order.
        for name, _ in sorted(non_coalescable.items(), key=lambda pair: positive_locals[pair[0]]):
            width = non_coalescable[name] or self.target.int_size
            running += width
            new_offsets[name] = running
        # Apply: overwrite positive-offset entries; leave negatives untouched.
        for name, offset in self.locals.items():
            if offset > 0 and name in new_offsets:
                self.locals[name] = new_offsets[name]
        self.frame_size = running
```

- [ ] **Step 2: Wire it into `generate_function`**

In `cc/codegen/x86/emission.py`, after the existing IR-temp allocation block
(after line 2178, the `self.allocate_local(temp)` loop) and before the
parameter-pin sharing block (around line 2180), add:

```python
        # Slot coalescing: pack frame-allocated locals onto shared
        # slots by liveness.  Only runs when an IR body is present
        # (the AST-only path — currently just ``main`` — keeps the
        # naive layout).
        if ir_body is not None:
            self._coalesce_frame_slots(
                ir_body=ir_body,
                parameters=parameters,
                ast_body=body,
            )
```

- [ ] **Step 3: Run the failing test**

Run: `python3 -m pytest tests/unit/test_cc_slot_coalescing.py -v`

Expected: PASS — the disjoint-named-locals frame shrinks below 12 bytes.

- [ ] **Step 4: Run the full unit suite**

Run: `python3 -m pytest tests/unit/ -v`

Expected: all tests pass.  If any liveness-adjacent test fails, the analyzer is
producing a different answer for the post-IR body than expected — debug by
printing the interference dict for the failing case.

- [ ] **Step 5: Commit**

```bash
PRE_COMMIT_ALLOW_NO_CONFIG=1 git add cc/codegen/x86/generator.py cc/codegen/x86/emission.py
PRE_COMMIT_ALLOW_NO_CONFIG=1 git commit -m "feat(cc): coalesce frame slots by liveness after IR-temp allocation"
```

### Task 10: Integration test — sb16_open frame size shrinks

**Files:**
- Modify: `tests/unit/test_cc_slot_coalescing.py`

- [ ] **Step 1: Add the regression test pinned to sb16_open's known frame size**

Append to `tests/unit/test_cc_slot_coalescing.py`:

```python
def test_slot_coalescing_shrinks_sb16_open_frame() -> None:
    # sb16_open is the motivating case: a 76-byte frame dominated by
    # IR temps spilled around kernel_outb / sb16_dsp_out calls.
    # After coalescing the frame must shrink.  The exact post-pass
    # number is implementation-dependent — assert a meaningful drop.
    result = subprocess.run(
        ["python3", "cc.py", "--target", "kernel", "kernel/drivers/sb16.c"],
        capture_output=True,
        text=True,
        check=True,
    )
    frame = _frame_size(result.stdout, "sb16_open")
    assert 0 < frame < 76, f"expected sb16_open frame between 1 and 75 bytes, got {frame}"
```

- [ ] **Step 2: Run it**

Run: `python3 -m pytest
tests/unit/test_cc_slot_coalescing.py::test_slot_coalescing_shrinks_sb16_open_frame
-v`

Expected: PASS.  Capture the actual frame number from the test output — that's
the number to cite in the final commit message.

- [ ] **Step 3: Commit**

```bash
PRE_COMMIT_ALLOW_NO_CONFIG=1 git add tests/unit/test_cc_slot_coalescing.py
PRE_COMMIT_ALLOW_NO_CONFIG=1 git commit -m "test(cc): pin sb16_open frame shrink as slot-coalescing regression"
```

### Task 11: Full test matrix + size measurement + push

**Files:** none modified.

- [ ] **Step 1: Record pre-pass binary sizes from main**

```bash
git stash --keep-index --include-untracked  # safety; should be a no-op
git checkout main -- kernel/  # ensure pristine kernel sources (not strictly needed; the change is in cc/)
./make_os.sh
KERNEL_BEFORE=$(stat -c%s kernel.bin)
ASM_BEFORE=$(python3 -c "import os; print(os.path.getsize('asm.bin'))")  # adjust path if asm.bin lives elsewhere
echo "kernel.bin before: $KERNEL_BEFORE"
echo "asm.bin    before: $ASM_BEFORE"
```

Note: `./make_os.sh` writes the binary blob into `drive.img`; if there's no
standalone `kernel.bin` artefact, instead diff `drive.img` size, or read the
`KERNEL_SIZE` constant the build script computes.

- [ ] **Step 2: Run the entire CI matrix locally**

```bash
python3 tests/test_asm.py
python3 tests/test_programs.py
python3 tests/test_programs.py --filesystem ext2
python3 tests/test_bboefs.py
python3 -m pytest tests/unit/ -v
./make_os.sh
```

Expected: all suites pass.  The memory note "Run full CI matrix locally on big
changes" applies — this is a kernel-architecture-adjacent change.

- [ ] **Step 3: Record post-pass binary sizes**

```bash
KERNEL_AFTER=$(stat -c%s kernel.bin)
ASM_AFTER=$(python3 -c "import os; print(os.path.getsize('asm.bin'))")
echo "kernel.bin delta: $((KERNEL_AFTER - KERNEL_BEFORE)) bytes"
echo "asm.bin    delta: $((ASM_AFTER - ASM_BEFORE)) bytes"
```

- [ ] **Step 4: QEMU boot smoke**

```bash
qemu-system-i386 -drive file=drive.img,format=raw -serial stdio -display none -no-reboot < /dev/null &
QEMU_PID=$!
sleep 8
kill $QEMU_PID 2>/dev/null
```

Expected: serial output reaches the shell prompt.

- [ ] **Step 5: Open the PR**

```bash
git push origin HEAD
gh pr create --title "perf(cc): coalesce frame slots by liveness" --body "$(cat <<'EOF'
## Summary
- Generalise `LivenessAnalyzer` to walk both AST and post-IR `Instruction` bodies (existing register-pin call site keeps consuming pre-IR AST and stays byte-identical)
- New slot-coalescing pass after IR-temp allocation lets same-size, non-interfering, non-address-taken frame locals share a slot
- sb16_open frame: 76 → <FILL IN> bytes; kernel.bin <FILL IN> bytes; asm.bin <FILL IN> bytes

Spec: design-specs:2026-05-22-cc-slot-coalescing-design.md

## Test plan
- [x] `tests/unit/test_cc_liveness.py` (new IR cases + existing AST coverage)
- [x] `tests/unit/test_cc_slot_coalescing.py` (sb16_open frame regression + disjoint named locals)
- [x] `tests/test_asm.py`
- [x] `tests/test_programs.py` (bbfs + ext2)
- [x] `tests/test_bboefs.py`
- [x] QEMU boot smoke

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Fill in the actual byte numbers from Steps 1 / 3 before the PR is opened.

---

## Self-review checklist (run after writing this plan)

- Spec coverage: every spec section maps to a task (Generalize → Tasks 1–5; Two
  call sites → Tasks 5 + 9; Slot pass eligibility / coloring / rewrite → Task 9;
  Verification → Tasks 6, 10, 11).
- Placeholder scan: every code block contains real content.
- Type consistency: `_coalesce_frame_slots` always called with three named args;
  `_collect_address_taken` returns `set[str]`; `LivenessAnalyzer` constructor
  signature unchanged across the plan.
- Risk re-check: address-taken set extracted from AST (Task 7) is used by the
  slot pass (Task 9); raise-on-unknown applies uniformly (Tasks 2, 4, 5);
  same-size restriction enforced in Task 9 step 1.
