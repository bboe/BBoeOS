# cc.py `Place` Refactor — Plan 1: Infrastructure + IndexMember\* Family

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Introduce a recursive `Place` (addressable-location) AST abstraction
plus five operation nodes, build the recursive address-resolution core, and
convert the four `IndexMember*` nodes to it — proving the conversion is
byte-for-byte identical to today's output.

**Architecture:** cc.py emits most access expressions straight from the AST: the
IR builder wraps them in `ir.Block(node=…)` ("escape hatch: lower this AST node
via the existing statement codegen", `cc/ir.py:76`) and `lower_ir_body` calls
`generate_expression` / `generate_statement` on them
(`cc/codegen/x86/emission.py:1794`). Only `Index` / `IndexAssign` have
*dedicated* IR nodes that feed SSA + the loop optimizer. This plan touches
**only the Block-emitted path** (parser → generator/emission), so it never
perturbs the IR/SSA/optimizer and stays byte-exact-checkable. The existing
per-shape methods keep the address *symbolic* — a `(const_base, static_offset,
dynamic_index_in_BX)` triple handed to `_build_address` +
`_emit_field_load`/`_emit_field_store` — rather than materializing a pointer in
a register. The new core reproduces that triple via a recursive
`_resolve_place`, so no extra `lea` is emitted and output is identical.

**Tech Stack:** Python 3 (no third-party deps), NASM, the existing cc.py codegen
helpers. Verification via a checked-in golden-asm snapshot test plus the QEMU
byte-diff suite (`tests/test_asm.py`) and runtime suite
(`tests/test_programs.py`).

---

## Roadmap (full unification, staged)

The agreed end-state is full unification — eventually `Index` / `IndexAssign`
also become `Place`, which ripples into `cc/ir.py`, `cc/ssa.py`,
`cc/ir_optimize.py`, `cc/loops.py`. That IR-touching work is the riskiest and is
sequenced **last**. Plans are independently shippable:

- **Plan 1 (this doc):** `Place` + op nodes + `_resolve_place` core; convert the
  `IndexMember*` family (`IndexMemberAccess`, `IndexMemberIndex`,
  `IndexMemberAssign`, `IndexMemberIndexAssign`). Byte-exact proof-of-concept.
- **Plan 2:** Member family (`MemberAccess`, `MemberAssign`, `MemberAddressOf`,
  `MemberIndex`, `MemberIndexAssign`, `MemberIncrementDecrement`) including the
  `base_expr` chained form.
- **Plan 3:** Deref family (`PointerDereference`, `PointerDereferenceAssign`,
  `DerefAssign`, `DerefIncrement`, `DerefIncrementAssign`) and `DoubleIndex`.
- **Plan 4:** Unify the scattered axes — `AddressOf` / `IncrementDecrement` /
  `IndexedCall` over arbitrary `Place`; enable the new shapes that fall out for
  free (`&arr[i]`, `a[i]++`, `a[i][j].f = x`).
- **Plan 5 (highest risk):** Fold `Index` / `IndexAssign` into `Place` through a
  `Place`-aware IR builder + SSA + loop/rep-string optimizer.

Each plan ends with the same gate: golden snapshot unchanged for converted
shapes, full `tests/test_asm.py` and `tests/test_programs.py` (bbfs + ext2)
green.

---

## File Structure

- `cc/ast_nodes.py` — **modify.** Add `Place` base + `VariablePlace`,
  `DereferencePlace`, `SubscriptPlace`, `MemberPlace`, and the five op nodes
  `PlaceLoad`, `PlaceStore`, `PlaceAddressOf`, `PlaceIncDec`, `PlaceCall`. (The
  op nodes are `Place`-prefixed to avoid colliding with the existing `AddressOf`
  / `IncrementDecrement` / `IndexedCall` nodes, which Plan 4 retires.)
- `cc/codegen/x86/generator.py` — **modify.** Add the `_PlaceAddress` descriptor
  dataclass and the recursive `_resolve_place`, plus `_emit_place_load`,
  `_emit_place_store`, `_emit_place_address`. Rewrite the four
  `generate_index_member_*` methods to delegate to these.
- `cc/codegen/x86/emission.py` — **modify (Task 7 only).** Add dispatch arms for
  `PlaceLoad`/`PlaceStore`; remove the four `IndexMember*` arms.
- `cc/parser.py` — **modify (Task 7 only).** Reroute the four `IndexMember*`
  construction sites to build `Place` + op nodes.
- `tests/test_cc_place.py` — **create.** Golden-asm snapshot test for the
  `IndexMember*` fixture.
- `tests/golden/cc_place_index_member.asm` — **create (Task 2).** Captured
  golden output from the pre-refactor compiler.
- `tests/bboeos.h` — **no change needed** (no new builtins).

Design note on naming: the AST already has an `AddressOf` node (`&var`). To keep
Plan 1 additive and avoid a same-name clash, the new operation nodes are named
`PlaceLoad` / `PlaceStore` / `PlaceAddressOf` / `PlaceIncDec` / `PlaceCall`.
Plan 4 collapses the legacy `AddressOf` / `IncrementDecrement` / `IndexedCall`
into the `Place*` ops and can drop the prefix then if desired.

---

### Task 1: Add `Place` and operation AST nodes

**Files:**
- Modify: `cc/ast_nodes.py` (insert classes in alphabetical position)
- Test: `tests/unit/test_cc_place_nodes.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_cc_place_nodes.py`:

```python
"""Construction + structural-equality checks for the Place node family."""

from cc import ast_nodes


def test_variable_place_holds_name():
    place = ast_nodes.VariablePlace(name="arr")
    assert place.name == "arr"
    assert isinstance(place, ast_nodes.Place)


def test_subscript_place_recurses():
    inner = ast_nodes.VariablePlace(name="arr")
    place = ast_nodes.SubscriptPlace(base=inner, index=ast_nodes.Int(value=2))
    assert place.base is inner
    assert isinstance(place.base, ast_nodes.Place)


def test_member_place_recurses_over_subscript():
    place = ast_nodes.MemberPlace(
        base=ast_nodes.SubscriptPlace(
            base=ast_nodes.VariablePlace(name="arr"),
            index=ast_nodes.Var(name="i"),
        ),
        member_name="field",
    )
    assert place.member_name == "field"
    assert isinstance(place.base, ast_nodes.SubscriptPlace)


def test_dereference_place_takes_any_expression():
    place = ast_nodes.DereferencePlace(pointer=ast_nodes.Var(name="p"))
    assert isinstance(place.pointer, ast_nodes.Node)


def test_place_load_is_integer_operand():
    load = ast_nodes.PlaceLoad(place=ast_nodes.VariablePlace(name="x"))
    assert isinstance(load, ast_nodes.IntegerOperand)


def test_place_store_carries_value():
    store = ast_nodes.PlaceStore(
        place=ast_nodes.VariablePlace(name="x"),
        value=ast_nodes.Int(value=5),
    )
    assert store.value == ast_nodes.Int(value=5)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/test_cc_place_nodes.py -q` Expected: FAIL —
`AttributeError: module 'cc.ast_nodes' has no attribute 'VariablePlace'`.

- [ ] **Step 3: Add the node classes**

In `cc/ast_nodes.py`, every node is `@dataclass(kw_only=True, slots=True)` and
subclasses `Node` (base at `cc/ast_nodes.py:16`; `IntegerOperand` marker mixin
at `cc/ast_nodes.py:32`). Insert these in alphabetical position among the
existing classes (the file is sorted by class name). `DereferencePlace` goes
near the existing `Deref*` cluster; `MemberPlace` near the `Member*` cluster;
`Place`/`PlaceLoad`/`PlaceStore`/`PlaceAddressOf`/`PlaceIncDec`/`PlaceCall`
after `PointerDereferenceAssign`; `SubscriptPlace` before `Switch`;
`VariablePlace` near `Var`.

```python
@dataclass(kw_only=True, slots=True)
class Place(Node):
    """Base class for an addressable location ("place" / lvalue).

    A recursive description of *where* a value lives.  Operation nodes
    (:class:`PlaceLoad`, :class:`PlaceStore`, :class:`PlaceAddressOf`,
    :class:`PlaceIncDec`, :class:`PlaceCall`) say *what* to do there.
    """


@dataclass(kw_only=True, slots=True)
class VariablePlace(Place):
    """A named local or global: ``x``."""

    name: str


@dataclass(kw_only=True, slots=True)
class DereferencePlace(Place):
    """The pointee of an arbitrary pointer expression: ``*pointer``."""

    pointer: Node


@dataclass(kw_only=True, slots=True)
class SubscriptPlace(Place):
    """A subscript ``base[index]``; *base* recurses into another :class:`Place`."""

    base: Place
    index: Node


@dataclass(kw_only=True, slots=True)
class MemberPlace(Place):
    """A member ``base.member_name``; ``base->m`` is ``.m`` on a :class:`DereferencePlace`."""

    base: Place
    member_name: str


@dataclass(kw_only=True, slots=True)
class PlaceLoad(IntegerOperand, Node):
    """Read the value at *place* (rvalue)."""

    place: Place


@dataclass(kw_only=True, slots=True)
class PlaceStore(Node):
    """Store *value* into *place*: ``place = value;``."""

    place: Place
    value: Node


@dataclass(kw_only=True, slots=True)
class PlaceAddressOf(Node):
    """Take the address of *place*: ``&place``."""

    place: Place


@dataclass(kw_only=True, slots=True)
class PlaceIncDec(IntegerOperand, Node):
    """``++place`` / ``place++`` / ``--place`` / ``place--``.

    ``delta`` is ``+1`` or ``-1``; ``is_postfix`` selects pre- vs post-value.
    """

    delta: int
    is_postfix: bool
    place: Place


@dataclass(kw_only=True, slots=True)
class PlaceCall(Node):
    """Call through a function-pointer *place*: ``place(args)``."""

    args: list[Node]
    place: Place
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/test_cc_place_nodes.py -q` Expected: PASS — 6
passed.

- [ ] **Step 5: Commit**

```bash
git add cc/ast_nodes.py tests/unit/test_cc_place_nodes.py
git commit -m "feat(cc): add recursive Place AST nodes + Place* operation nodes"
```

---

### Task 2: Capture the byte-exact golden snapshot for `IndexMember*`

This locks the current compiler output so every later step can prove
byte-identity. Do this **before** any codegen change.

**Files:**
- Create: `tests/test_cc_place.py`
- Create: `tests/golden/cc_place_index_member.asm` (generated, then committed)

- [ ] **Step 1: Write the snapshot test (initially captures, then asserts)**

Create `tests/test_cc_place.py` (modeled on `tests/test_cc_local_structs.py:46`
`compile_snippet`):

```python
#!/usr/bin/env python3
"""Byte-exact golden snapshot for IndexMember* codegen through the Place core.

Compiles a fixture exercising arr[i].field, arr[i].field[j],
arr[i].field = v, and arr[i].field[j] = v, and asserts the cc.py-emitted
assembly is identical to a checked-in golden file.  Regenerate the golden
deliberately with BBOE_UPDATE_GOLDEN=1 only when output is intended to change.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CC = REPO_ROOT / "cc.py"
LIBBBOEOS_INCLUDE = REPO_ROOT / "user" / "libbboeos" / "include"
GOLDEN = REPO_ROOT / "tests" / "golden" / "cc_place_index_member.asm"

FIXTURE = """\
struct point { int x; int y; char tag; char path[4]; };
struct point points[8];

int probe(int i, int j, int v) {
    points[i].x = v;
    points[i].path[j] = v;
    int a = points[i].y;
    int b = points[i].path[j];
    return a + b;
}
"""


def emit_asm(*, work: Path) -> str:
    source_path = work / "index_member.c"
    asm_path = work / "index_member.asm"
    source_path.write_text(FIXTURE)
    subprocess.run(
        ["python3", str(CC), "--bits", "32", "-I", str(LIBBBOEOS_INCLUDE),
         str(source_path), str(asm_path)],
        capture_output=True, check=True, text=True,
    )
    return asm_path.read_text()


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="test_cc_place_") as temporary_directory:
        asm = emit_asm(work=Path(temporary_directory))
    if os.environ.get("BBOE_UPDATE_GOLDEN") == "1":
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(asm)
        print(f"WROTE golden {GOLDEN}")
        return 0
    expected = GOLDEN.read_text()
    if asm == expected:
        print("PASS  index_member golden byte-identical")
        return 0
    print("FAIL  index_member golden differs")
    for line_number, (got, want) in enumerate(zip(asm.splitlines(), expected.splitlines()), 1):
        if got != want:
            print(f"  line {line_number}: got {got!r} want {want!r}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Generate the golden from the current (pre-refactor) compiler**

Run: `BBOE_UPDATE_GOLDEN=1 python3 tests/test_cc_place.py` Expected: `WROTE
golden .../tests/golden/cc_place_index_member.asm`.

- [ ] **Step 3: Verify the snapshot now passes against itself**

Run: `python3 tests/test_cc_place.py` Expected: PASS — `index_member golden
byte-identical`.

- [ ] **Step 4: Sanity-check the golden actually exercises all four shapes**

Run: `grep -c 'imul\|lea\|movzx\|mov ' tests/golden/cc_place_index_member.asm`
Expected: a non-zero count (confirms struct-array indexing was emitted, not a
compile error producing an empty body). Eyeball the file: it must contain the
`probe` label and field loads/stores.

- [ ] **Step 5: Commit**

```bash
git add tests/test_cc_place.py tests/golden/cc_place_index_member.asm
git commit -m "test(cc): golden snapshot locking IndexMember* codegen"
```

---

### Task 3: Build the recursive `_resolve_place` address core

Additive only — nothing calls it yet. The descriptor mirrors the existing
symbolic triple so later delegation is byte-identical.

**Files:**
- Modify: `cc/codegen/x86/generator.py` (add near `_build_address` at line 496
  and the `_resolve_index_member_layout` helper at line 2790)
- Test: exercised indirectly in Task 4 via the golden snapshot (no standalone
  unit test — the core only has meaning as emitted asm).

Reference helpers (exact signatures, all in `cc/codegen/x86/generator.py` unless
noted):
- `_resolve_index_member_layout(name, member_name, line, /) -> (const_base,
  struct_size, field_offset, field_size, element_size)` — line 2790.
- `_build_address(base, offset, /, *, index="") -> str` — line 496 (static).
- `_emit_struct_element_offset(index, struct_size, /)` — line 1886; leaves
  `index * struct_size` in BX using AX as scratch.
- `_emit_field_load(*, addr, field_size)` — line 1270; `_emit_field_store(*,
  addr, field_size)` — line 1286.
- `emit_byte_load_zx(mem_operand, /)` — line 4065; `ax_clear()` — line 3756.
- `_bx_holds_pinned_var() -> bool` — line 513.
- `self.target.acc`, `self.target.bx_register` — `cc/codegen/target.py`.

- [ ] **Step 1: Add the address-descriptor dataclass**

Near the top-of-class helpers in `generator.py` (the `FieldInfo` NamedTuple
lives at line 115 — put this dataclass beside it). Use a frozen dataclass:

```python
@dataclass(frozen=True, slots=True)
class _PlaceAddress:
    """Symbolic address of a Place: ``[const_base + offset (+ BX)]``.

    Mirrors the triple the legacy generate_index_member_* methods build, so
    emitting through it is byte-identical: *const_base* is a label or
    frame-relative base string, *offset* a static displacement folded into the
    operand, and *indexed* True when BX holds a dynamic byte offset.  *field_size*
    / *element_size* size the terminal load/store.
    """

    const_base: str
    offset: int
    indexed: bool
    field_size: int
    element_size: int
```

(Add `from dataclasses import dataclass` to the imports if not already present;
`field`/`dataclass` usage already exists in the module.)

- [ ] **Step 2: Add `_resolve_place` (recursive) for the IndexMember\* shapes**

This handles exactly the `Place` trees Task 7 will produce for `IndexMember*`:
`MemberPlace(base=SubscriptPlace(VariablePlace, i), member)` and
`SubscriptPlace(base=MemberPlace(base=SubscriptPlace(VariablePlace, i), member),
j)`. It replicates the BX accounting of the legacy methods exactly
(push/gen/pop/add for the second index; BX-pin protection at the outermost
call).

```python
def _resolve_place(self, place, /):
    """Emit dynamic-offset code (into BX when needed); return a _PlaceAddress.

    Plan 1 scope: MemberPlace over SubscriptPlace(VariablePlace) for struct
    arrays, optionally wrapped in one more SubscriptPlace for an array-typed
    member.  Other Place shapes raise (later plans extend this).
    """
    acc = self.target.acc
    bx = self.target.bx_register
    # Shape A: arr[i].member   (scalar or array-typed member, no element index)
    if (
        isinstance(place, MemberPlace)
        and isinstance(place.base, SubscriptPlace)
        and isinstance(place.base.base, VariablePlace)
    ):
        name = place.base.base.name
        const_base, struct_size, field_offset, field_size, element_size = (
            self._resolve_index_member_layout(name, place.member_name, place.line)
        )
        self._emit_struct_element_offset(place.base.index, struct_size)  # BX = i*stride
        return _PlaceAddress(
            const_base=const_base, offset=field_offset, indexed=True,
            field_size=field_size, element_size=element_size,
        )
    # Shape B: arr[i].member[j]   (element of an array-typed member)
    if (
        isinstance(place, SubscriptPlace)
        and isinstance(place.base, MemberPlace)
        and isinstance(place.base.base, SubscriptPlace)
        and isinstance(place.base.base.base, VariablePlace)
    ):
        member = place.base
        name = member.base.base.name
        const_base, struct_size, field_offset, _field_size, element_size = (
            self._resolve_index_member_layout(name, member.member_name, place.line)
        )
        if element_size not in (1, 2):
            raise CompileError(
                f"indexing '{member.member_name}' (element size {element_size}) not supported",
                line=place.line,
            )
        self._emit_struct_element_offset(member.base.index, struct_size)  # BX = i*stride
        self.emit(f"        push {bx}")
        self.generate_expression(place.index)  # AX = j
        if element_size == 2:
            self.emit(f"        shl {acc}, 1")
        self.emit(f"        pop {bx}")
        self.emit(f"        add {bx}, {acc}")  # BX = i*stride + j*element_size
        return _PlaceAddress(
            const_base=const_base, offset=field_offset, indexed=True,
            field_size=element_size, element_size=element_size,
        )
    raise CompileError("unsupported Place shape in _resolve_place", line=getattr(place, "line", 0))
```

- [ ] **Step 3: Add the terminal emitters**

```python
def _emit_place_load(self, place, /):
    """Load the value at *place* into the accumulator (rvalue)."""
    self.ax_clear()
    protect_bx = self._bx_holds_pinned_var()
    if protect_bx:
        self.emit(f"        push {self.target.bx_register}")
    address = self._resolve_place(place)
    index = self.target.bx_register if address.indexed else ""
    addr = self._build_address(address.const_base, address.offset, index=index)
    is_array_member = address.field_size != address.element_size
    if is_array_member:
        # Bare array-typed member yields its address (matches legacy lea path).
        self.emit(f"        lea {self.target.acc}, {addr}")
    else:
        self._emit_field_load(addr=addr, field_size=address.field_size)
    if protect_bx:
        self.emit(f"        pop {self.target.bx_register}")
    self.ax_clear()


def _emit_place_store(self, place, value, /):
    """Store the result of *value* into *place*."""
    allowed = (1, 2, 4) if self.target.int_size == 4 else (1, 2)
    self.ax_clear()
    protect_bx = self._bx_holds_pinned_var()
    if protect_bx:
        self.emit(f"        push {self.target.bx_register}")
    self.generate_expression(value)               # AX = value
    self.emit(f"        push {self.target.acc}")   # save value on top of stack
    address = self._resolve_place(place)           # may use BX/AX as scratch
    if address.field_size not in allowed:
        raise CompileError(
            f"writing field (size {address.field_size}) not yet supported; use asm()",
            line=getattr(place, "line", 0),
        )
    self.emit(f"        pop {self.target.acc}")     # AX = value
    self.ax_clear()
    index = self.target.bx_register if address.indexed else ""
    addr = self._build_address(address.const_base, address.offset, index=index)
    self._emit_field_store(addr=addr, field_size=address.field_size)
    if protect_bx:
        self.emit(f"        pop {self.target.bx_register}")
```

(`_emit_place_address` for `PlaceAddressOf` is deferred to Plan 4 — the
`IndexMember*` family has no address-of form, so Plan 1 does not need it.)

- [ ] **Step 4: Make sure the module still imports**

Run: `python3 -c "import cc.codegen.x86.generator"` Expected: no output, exit 0
(no syntax/name errors; `MemberPlace`, `SubscriptPlace`, `VariablePlace`,
`CompileError` must be imported in `generator.py` — add them to the existing
`from cc.ast_nodes import (...)` and `from cc.errors import CompileError` lines
if missing).

- [ ] **Step 5: Commit**

```bash
git add cc/codegen/x86/generator.py
git commit -m "feat(cc): add recursive _resolve_place address core (unwired)"
```

---

### Task 4: Delegate `generate_index_member_access` to the core

Prove byte-identity one method at a time, **without** touching the parser yet —
the method keeps receiving an `IndexMemberAccess` node, builds the equivalent
`Place`, and calls the core. The golden snapshot must stay identical.

**Files:**
- Modify: `cc/codegen/x86/generator.py:4420` (`generate_index_member_access`)

- [ ] **Step 1: Replace the body with a Place delegation**

The current method (lines 4420–4449) resolves layout, protects BX, computes
`i*stride`, and either `lea`s (array field) or loads. Replace its body with:

```python
def generate_index_member_access(self, expression, /):
    """Generate ``arr[i].field`` as an rvalue, via the Place core."""
    base = SubscriptPlace(
        line=expression.line,
        base=VariablePlace(line=expression.line, name=expression.name),
        index=expression.index,
    )
    place = MemberPlace(line=expression.line, base=base, member_name=expression.member_name)
    self._emit_place_load(place)
```

(`IndexMemberAccess` exposes `name`, `index`, `member_name`, `arrow` — see
`cc/ast_nodes.py:419`. The `arrow` flag does not affect this struct-array case;
the legacy method ignores it too.)

- [ ] **Step 2: Run the golden snapshot**

Run: `python3 tests/test_cc_place.py` Expected: PASS — byte-identical. If it
FAILS, the per-line diff prints the first mismatch; reconcile `_resolve_place` /
`_emit_place_load` against the legacy sequence (common culprits: missing
`ax_clear()`, BX push/pop ordering, `lea` vs load for array members) and re-run.
Do **not** edit the golden.

- [ ] **Step 3: Confirm no other suite regressed for this shape**

Run: `python3 tests/test_cc_local_structs.py` Expected: all PASS (it exercises
array-of-struct indexed access at `test_array_of_structs_indexed_access`).

- [ ] **Step 4: Commit**

```bash
git add cc/codegen/x86/generator.py
git commit -m "refactor(cc): route generate_index_member_access through Place core"
```

---

### Task 5: Delegate `generate_index_member_index` to the core

**Files:**
- Modify: `cc/codegen/x86/generator.py:4485` (`generate_index_member_index`)

- [ ] **Step 1: Replace the body with a Place delegation**

`IndexMemberIndex` exposes `name`, `index`, `member_name`, `elem_index`, `arrow`
(`cc/ast_nodes.py:440`). Build `arr[i].member[elem_index]`:

```python
def generate_index_member_index(self, expression, /):
    """Generate ``arr[i].field[n]`` as an rvalue, via the Place core."""
    member = MemberPlace(
        line=expression.line,
        base=SubscriptPlace(
            line=expression.line,
            base=VariablePlace(line=expression.line, name=expression.name),
            index=expression.index,
        ),
        member_name=expression.member_name,
    )
    place = SubscriptPlace(line=expression.line, base=member, index=expression.elem_index)
    self._emit_place_load(place)
```

- [ ] **Step 2: Run the golden snapshot**

Run: `python3 tests/test_cc_place.py` Expected: PASS — byte-identical. Reconcile
if not (the Shape-B branch of `_resolve_place` must match the legacy
push/`shl`/pop/add sequence at `generator.py:4501–4512`).

- [ ] **Step 3: Commit**

```bash
git add cc/codegen/x86/generator.py
git commit -m "refactor(cc): route generate_index_member_index through Place core"
```

---

### Task 6: Delegate the two assignment methods to the core

**Files:**
- Modify: `cc/codegen/x86/generator.py:4451` (`generate_index_member_assign`)
- Modify: `cc/codegen/x86/generator.py:4515`
  (`generate_index_member_index_assign`)

- [ ] **Step 1: Rewrite `generate_index_member_assign`**

`IndexMemberAssign` exposes `name`, `index`, `member_name`, `expr`, `arrow`
(`cc/ast_nodes.py:429`):

```python
def generate_index_member_assign(self, statement, /):
    """Generate ``arr[i].field = expr;`` via the Place core."""
    base = SubscriptPlace(
        line=statement.line,
        base=VariablePlace(line=statement.line, name=statement.name),
        index=statement.index,
    )
    place = MemberPlace(line=statement.line, base=base, member_name=statement.member_name)
    self._emit_place_store(place, statement.expr)
```

- [ ] **Step 2: Rewrite `generate_index_member_index_assign`**

`IndexMemberIndexAssign` exposes `name`, `index`, `member_name`, `elem_index`,
`expr`, `arrow` (`cc/ast_nodes.py:451`):

```python
def generate_index_member_index_assign(self, statement, /):
    """Generate ``arr[i].field[n] = expr;`` via the Place core."""
    member = MemberPlace(
        line=statement.line,
        base=SubscriptPlace(
            line=statement.line,
            base=VariablePlace(line=statement.line, name=statement.name),
            index=statement.index,
        ),
        member_name=statement.member_name,
    )
    place = SubscriptPlace(line=statement.line, base=member, index=statement.elem_index)
    self._emit_place_store(place, statement.expr)
```

- [ ] **Step 3: Run the golden snapshot**

Run: `python3 tests/test_cc_place.py` Expected: PASS — byte-identical. The store
path's value-save ordering (`push acc` before `_resolve_place`, `pop acc` after)
must match the legacy methods at `generator.py:4473–4481` and `4528+`. Reconcile
if needed.

- [ ] **Step 4: Commit**

```bash
git add cc/codegen/x86/generator.py
git commit -m "refactor(cc): route IndexMember* assignment codegen through Place core"
```

---

### Task 7: Flip the parser to emit `Place` nodes; delete the legacy `IndexMember*` path

Now the four AST classes and their bespoke methods are removed; the parser emits
`Place` + `PlaceLoad`/`PlaceStore`, and dispatch routes them to the core.

**Files:**
- Modify: `cc/parser.py:805, 813, 985, 996` (the four construction sites)
- Modify: `cc/codegen/x86/emission.py` (dispatch: expressions at 2862/2864,
  statements at 4309/4312)
- Modify: `cc/codegen/x86/generator.py` (delete the four
  `generate_index_member_*` methods)
- Modify: `cc/ast_nodes.py` (delete `IndexMemberAccess`, `IndexMemberAssign`,
  `IndexMemberIndex`, `IndexMemberIndexAssign`)

- [ ] **Step 1: Add `PlaceLoad`/`PlaceStore` dispatch arms**

In `cc/codegen/x86/emission.py` `generate_expression` (isinstance chain starting
line 2768), add near the other `Place`-related arms:

```python
        elif isinstance(expression, PlaceLoad):
            self._emit_place_load(expression.place)
```

In the statement dispatch (the chain containing the `IndexMemberAssign` arm at
emission.py:4309), add:

```python
        elif isinstance(statement, PlaceStore):
            self._emit_place_store(statement.place, statement.value)
```

Import `PlaceLoad` / `PlaceStore` in `emission.py` alongside the existing `from
cc.ast_nodes import (...)`.

- [ ] **Step 2: Reroute the parser construction sites**

Replace each `IndexMember*` construction with the `Place` + op equivalent. The
helper variables (`name`, `index`, `member_name`, `elem_index`, `arrow`, `expr`)
already exist at each site.

`cc/parser.py:813` (`_parse_ident_primary`, `arr[i].field` rvalue):

```python
            return PlaceLoad(
                line=line,
                place=MemberPlace(
                    line=line,
                    base=SubscriptPlace(line=line, base=Var(line=line, name=name), index=index),
                    member_name=member_name,
                ),
            )
```

Note `SubscriptPlace.base` must be a `Place`, but the parser already has a `Var`
expression here. For Plan 1, wrap it: `base=VariablePlace(line=line, name=name)`
(use `VariablePlace`, not `Var`). Apply the same `VariablePlace` wrapping at all
four sites.

`cc/parser.py:805` (`arr[i].field[j]` rvalue):

```python
            return PlaceLoad(
                line=line,
                place=SubscriptPlace(
                    line=line,
                    base=MemberPlace(
                        line=line,
                        base=SubscriptPlace(line=line, base=VariablePlace(line=line, name=name), index=index),
                        member_name=member_name,
                    ),
                    index=elem_index,
                ),
            )
```

`cc/parser.py:996` (`_parse_index_assignment_no_semi`, `arr[i].field = expr;`):

```python
            return PlaceStore(
                line=token[2],
                place=MemberPlace(
                    line=token[2],
                    base=SubscriptPlace(line=token[2], base=VariablePlace(line=token[2], name=name), index=index),
                    member_name=member_name,
                ),
                value=expr,
            )
```

`cc/parser.py:985` (`arr[i].field[j] = expr;`):

```python
            return PlaceStore(
                line=token[2],
                place=SubscriptPlace(
                    line=token[2],
                    base=MemberPlace(
                        line=token[2],
                        base=SubscriptPlace(line=token[2], base=VariablePlace(line=token[2], name=name), index=index),
                        member_name=member_name,
                    ),
                    index=elem_index,
                ),
                value=expr,
            )
```

Import `PlaceLoad`, `PlaceStore`, `MemberPlace`, `SubscriptPlace`,
`VariablePlace` in `parser.py`.

- [ ] **Step 3: Run the golden snapshot (still byte-identical end-to-end)**

Run: `python3 tests/test_cc_place.py` Expected: PASS — byte-identical. This now
proves the parser→Place→core path equals the original IndexMember\* path.

- [ ] **Step 4: Delete the dead legacy code**

Remove from `cc/codegen/x86/emission.py`: the `IndexMemberAccess`,
`IndexMemberIndex` arms (lines 2862–2865) and the `IndexMemberAssign`,
`IndexMemberIndexAssign` statement arms (4309–4312). Remove from
`cc/codegen/x86/generator.py`: `generate_index_member_access` (4420),
`generate_index_member_index` (4485), `generate_index_member_assign` (4451),
`generate_index_member_index_assign` (4515). Remove from `cc/ast_nodes.py` the
four `IndexMember*` classes (419–459). Remove their now-unused imports.

- [ ] **Step 5: Grep proves the classes are gone with no stragglers**

Run: `grep -rn
"IndexMemberAccess\|IndexMemberIndex\|IndexMemberAssign\|IndexMemberIndexAssign"
cc/` Expected: no output (exit 1). If any reference remains, it's a missed call
site — remove or reroute it.

- [ ] **Step 6: Re-run the golden + import checks**

Run: `python3 tests/test_cc_place.py && python3 -c "import cc.parser,
cc.codegen.x86.generator, cc.codegen.x86.emission"` Expected: PASS + clean
import.

- [ ] **Step 7: Commit**

```bash
git add cc/parser.py cc/codegen/x86/emission.py cc/codegen/x86/generator.py cc/ast_nodes.py
git commit -m "refactor(cc): emit Place nodes for arr[i].field shapes; delete IndexMember* zoo"
```

---

### Task 8: Full byte-exact regression + runtime gate

**Files:** none (verification only).

- [ ] **Step 1: cc.py unit + snapshot suites**

Run:
```bash
python3 -m pytest tests/unit/ -q
python3 tests/test_cc_local_structs.py
python3 tests/test_cc_bitfields.py
python3 tests/test_cc_casts.py
python3 tests/test_cc_place.py
```
Expected: all PASS.

- [ ] **Step 2: Self-hosted assembler byte-diff (the real programs)**

Run: `python3 tests/test_asm.py` Expected: all PASS — every program in
`user/static/` reassembles byte-for-byte. This is the strongest guarantee that
no real program's codegen shifted.

- [ ] **Step 3: Runtime smoke tests, both filesystems**

Run:
```bash
python3 tests/test_programs.py
python3 tests/test_programs.py --filesystem ext2
```
Expected: all PASS (per the "run full CI matrix locally on big changes"
convention — this is a codegen change, so do not stop at the snapshot).

- [ ] **Step 4: clang compatibility (no new builtins, expected clean)**

Run: `python3 tests/test_cc_compatibility.py` Expected: all PASS.

- [ ] **Step 5: Final commit if any verification artifact changed**

Nothing should change here; if a suite forced an edit, commit it with a
`test(cc): …` message describing what moved.

---

## Self-Review

**Spec coverage.** Plan 1's scope = `Place` infra + `_resolve_place` core +
convert the four `IndexMember*` nodes byte-exactly. Tasks: 1 (nodes), 2 (golden
lock), 3 (core), 4–6 (delegation, one method group at a time, each
golden-gated), 7 (parser flip + legacy deletion), 8 (full regression). Every
converted shape has a delegation task and a golden check. The broader roadmap
(member, deref, double-index, axis-unification, IR `Index` fold) is explicitly
out of Plan 1 and listed under Roadmap.

**Placeholder scan.** No `TBD`/`handle edge cases`/`similar to Task N`. Each
code step shows complete code; each verify step shows the exact command and
expected result. The one acknowledged iterate-loop (Tasks 4–6 "reconcile if the
golden differs") is real engineering, not a placeholder — it names the likely
culprits and the reference line numbers.

**Type consistency.** Node names are stable across tasks: `Place`,
`VariablePlace`, `DereferencePlace`, `SubscriptPlace`, `MemberPlace`,
`PlaceLoad`, `PlaceStore`, `PlaceAddressOf`, `PlaceIncDec`, `PlaceCall`.
`SubscriptPlace.base` is always a `Place`, so the parser wraps the leaf as
`VariablePlace` (not the expression-level `Var`) — called out explicitly in Task
7 Step 2. `_resolve_place` returns `_PlaceAddress`; `_emit_place_load` /
`_emit_place_store` consume it. The store helper validates `field_size` against
the same `(1,2,4)`/`(1,2)` sets the legacy method used.

**Known risk (documented, not a gap):** `_resolve_place` reproduces the legacy
BX/AX scratch sequence by hand; the golden snapshot in Task 2 is the safety net
that turns "did I match the asm exactly?" into a deterministic pass/fail at
every delegation step. The full `test_asm.py` run in Task 8 extends that
guarantee from the synthetic fixture to every shipping program.
