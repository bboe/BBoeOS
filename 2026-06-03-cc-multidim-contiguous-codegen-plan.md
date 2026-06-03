# Contiguous Multidimensional Array Codegen — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make contiguous multidimensional arrays (`int m[2][3]`, local and global,
2-D and 3-D, `int`/`char`/`unsigned short` elements) compile and run end-to-end —
storage, `sizeof`, and row-major subscript load/store — lifting the codegen guards
for what now works.

**Architecture:** Track B Stage 4 of the multidim design. The parser already records
`ArrayDecl.dimensions` (merged). This plan: (1) register each multidim array's
structured `cc.types.ArrayType` in a codegen-side registry; (2) allocate row-major
contiguous storage and compute `sizeof` from it; (3) change the parser to emit one
**uniform nested `SubscriptPlace`** shape for `name[i][j]…` (no baked deref); (4) lower
that shape by **dispatching on the base variable's registered type** — a contiguous
multidim array gets new row-major addressing, while a pointer / array-of-pointers base
is reconstructed into the *legacy* `SubscriptPlace(DereferencePlace(Index(...)))` node
and handed to the existing `_emit_double_index_place_*` emitters, byte-identical by
construction.

**Tech Stack:** Python (`cc/parser.py`, `cc/codegen/x86/generator.py`,
`cc/codegen/x86/emission.py`, `cc/types.py`), pytest unit tests under `tests/unit/`,
QEMU runtime tests in `tests/test_programs.py`, byte oracles
`tests/test_cc_function_sizes.py` + `tests/test_cc_place.py`.

**Out of scope (explicit follow-ups, keep their guards):** pointer-to-array
(`int (*p)[3]`), multidim *struct fields*, multidim *parameters* and array→pointer
decay of multidim arrays when passed to functions, and N-dim subscripts on genuine
array-of-pointers beyond the existing 2-deep shape.

---

## Invariants enforced at EVERY task

- After every task: `python3 tests/test_cc_function_sizes.py` prints
  `PASS  per-function byte-size gate` and `python3 tests/test_cc_place.py` prints
  `PASS … byte-identical`. These prove existing (non-multidim) output is unchanged.
- `python3 -m pytest tests/unit/ -q` stays green.
- `ruff check <changed files>` clean. Functions/classes/fields sorted alphabetically
  (underscore considered); call-site kwargs alphabetical.
- Commit after each task.

---

## File Structure

- `cc/types.py` — add a recursive `sizeof` to the `Type` hierarchy (element widths
  injected by the caller so the module stays target-agnostic).
- `cc/codegen/x86/generator.py` — array-type registry + row-major addressing +
  dispatch; remove local/global multidim guards.
- `cc/codegen/x86/emission.py` — `sizeof` multidim branch + the `unsigned short`
  stride fix; the array-of-pointers reconstruction dispatch lives here if the
  PlaceLoad/PlaceStore handlers are here (confirm during Task 4).
- `cc/parser.py` — uniform nested `SubscriptPlace` for `name[i][j]…`.
- `tests/unit/test_cc_multidim_types_sizeof.py` — new, `Type.sizeof` unit tests.
- `tests/unit/test_cc_multidim_parser.py` — extend: N-dim subscript AST shape.
- `tests/unit/test_cc_multidim_runtime.py` — new, compile-and-run via the existing
  subprocess `_compile` helper pattern, asserting program exit codes / output.
- `tests/test_programs.py` — add multidim runtime entries (final task).

---

### Task 1: `Type.sizeof` on the type hierarchy

**Files:**
- Modify: `cc/types.py` (add `sizeof` method to `Type` base + each subclass)
- Test: `tests/unit/test_cc_multidim_types_sizeof.py` (create)

The module must stay target-agnostic (no knowledge of `int`=4). The caller passes a
`scalar_width` callable mapping a scalar/struct/pointer name → bytes. `ArrayType.sizeof`
multiplies `count * pointee.sizeof(...)`; `PointerType` returns the pointer width;
`ScalarType`/`StructType` defer to the callable.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_cc_multidim_types_sizeof.py
"""Recursive sizeof on the cc Type hierarchy (target widths injected by caller)."""

from __future__ import annotations

from cc.types import ArrayType, PointerType, ScalarType, StructType


def _width(name: str, /) -> int:
    """Test stand-in for the target's scalar/struct/pointer byte widths."""
    return {"char": 1, "unsigned short": 2, "int": 4, "struct point": 8}[name]


def test_scalar_sizeof_uses_callable() -> None:
    assert ScalarType(name="unsigned short").sizeof(scalar_width=_width) == 2


def test_pointer_sizeof_is_pointer_width() -> None:
    assert PointerType(pointee=ScalarType(name="char")).sizeof(scalar_width=_width, pointer_width=4) == 4


def test_array_sizeof_is_count_times_element() -> None:
    array = ArrayType(count=10, pointee=ScalarType(name="int"))
    assert array.sizeof(scalar_width=_width, pointer_width=4) == 40


def test_multidim_sizeof_is_product_of_dimensions() -> None:
    array = ArrayType(count=2, pointee=ArrayType(count=3, pointee=ScalarType(name="int")))
    assert array.sizeof(scalar_width=_width, pointer_width=4) == 24


def test_struct_sizeof_uses_callable() -> None:
    assert StructType(tag="point").sizeof(scalar_width=_width, pointer_width=4) == 8
```

- [ ] **Step 2: Run test, verify it fails**

Run: `python3 -m pytest tests/unit/test_cc_multidim_types_sizeof.py -q`
Expected: FAIL — `AttributeError: 'ScalarType' object has no attribute 'sizeof'`.

- [ ] **Step 3: Implement `sizeof`**

Add to `cc/types.py`. Signature on every class:
`def sizeof(self, *, scalar_width, pointer_width: int = 0) -> int:`

- `Type` (base): `raise NotImplementedError`.
- `ScalarType`: `return scalar_width(self.name)`.
- `StructType`: `return scalar_width(f"struct {self.tag}")`.
- `PointerType`: `return pointer_width`.
- `ArrayType`: `return self.count * self.pointee.sizeof(scalar_width=scalar_width, pointer_width=pointer_width)`
  (raise `CompileError`-free `ValueError` if `count is None`, since sizeof of an
  unsized array is ill-formed; the codegen caller guards before calling).

Keep methods alphabetical-within-class is N/A (one method); keep the class order
already sorted.

- [ ] **Step 4: Run test, verify PASS**

Run: `python3 -m pytest tests/unit/test_cc_multidim_types_sizeof.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Invariants + commit**

```bash
python3 tests/test_cc_function_sizes.py   # PASS (unchanged: nothing wired in yet)
python3 tests/test_cc_place.py            # PASS
ruff check cc/types.py tests/unit/test_cc_multidim_types_sizeof.py
git add cc/types.py tests/unit/test_cc_multidim_types_sizeof.py
git commit -m "feat(cc): recursive Type.sizeof for multidim layout"
```

---

### Task 2: Array-type registry in the generator

**Files:**
- Modify: `cc/codegen/x86/generator.py` — add `self.array_types: dict[str, ArrayType]`
  in `__init__` (alphabetical among the existing `self.*` dicts); add a helper
  `_register_array_type(name, *, type_name, dimensions, line)`; call it where the
  global-array and local-array guards currently sit (generator.py:3678 and :5714).
- Test: `tests/unit/test_cc_multidim_runtime.py` (create — reused by later tasks)

The registry maps an array variable name → its `cc.types.ArrayType`. Single-dim arrays
register a one-level `ArrayType`; multidim register the nested form. The element type
comes from `Type.from_string(type_name)` (the element/scalar), and dimensions are the
*evaluated* `int` sizes (via the existing `_eval_local_array_size` machinery / constant
folding — dims must be compile-time constants; raise `CompileError` if not).

This task only *registers*; it does not yet allocate or address. The guards stay for now
EXCEPT we must not regress: keep raising for multidim until Task 3 (storage) lands, so
add registration BEFORE the guard but leave the guard in place. Net behavior unchanged →
byte gate stays identical.

- [ ] **Step 1: Write the failing test (unit, white-box on the generator)**

```python
# tests/unit/test_cc_multidim_runtime.py
"""End-to-end + white-box checks for contiguous multidim arrays."""

from __future__ import annotations

from cc.codegen.x86.generator import X86CodeGenerator
from cc.lexer import tokenize
from cc.parser import Parser
from cc.types import ArrayType, ScalarType


def _registry_for(source: str, /) -> dict:
    program = Parser(tokenize(source)).parse_program()
    generator = X86CodeGenerator(bits=32)
    generator.register_declarations(program)  # confirm real entry name in Step 3
    return generator.array_types


def test_two_dim_global_registers_nested_array_type() -> None:
    registry = _registry_for("int m[2][3];\nint main(void) { return 0; }\n")
    assert registry["m"] == ArrayType(count=2, pointee=ArrayType(count=3, pointee=ScalarType(name="int")))
```

- [ ] **Step 2: Run, verify it fails**

Run: `python3 -m pytest tests/unit/test_cc_multidim_runtime.py -q`
Expected: FAIL — `AttributeError` (no `array_types`) or the multidim guard raising.
(During Step 3, discover the real method that walks globals — likely
`generate`/`register_globals`; adjust `_registry_for` to drive it, or build the registry
by calling the registration helper directly on the `ArrayDecl`.)

- [ ] **Step 3: Implement the registry + helper**

In `__init__`: `self.array_types: dict[str, ArrayType] = {}` (place alphabetically).
Add:

```python
def _register_array_type(self, name: str, /, *, type_name: str, dimensions: list | None, line: int) -> None:
    """Record the structured ArrayType for array variable *name* (row-major)."""
    element = Type.from_string(type_name)
    sizes = [self._eval_constant_dimension(d, line=line) for d in (dimensions or [])]
    array_type: Type = element
    for count in reversed(sizes):
        array_type = ArrayType(count=count, pointee=array_type)
    self.array_types[name] = array_type
```

Where `_eval_constant_dimension` reuses the existing constant evaluator the array-size
path already uses (find it near `_eval_local_array_size`, generator.py:2970; factor a
small helper that returns an `int` or raises `CompileError` for non-constant dims).

Call `_register_array_type(...)` immediately *before* the existing multidim guard at the
global site (~3678) and local site (~5714), passing `declaration.type_name` /
`statement.type_name` and `.dimensions`. Leave the guards in place this task.

- [ ] **Step 4: Run, verify PASS** (`pytest tests/unit/test_cc_multidim_runtime.py -q`).
- [ ] **Step 5: Invariants + commit** (byte gate identical — guard still active).

```bash
git commit -am "feat(cc): register structured ArrayType for array decls"
```

---

### Task 3: Contiguous storage + lift the storage guards

**Files:**
- Modify: `cc/codegen/x86/generator.py` — global-array byte size (`_emit_global_array`
  ~1580 + BSS path ~1649) and local allocation (~5713). Replace the multidim guards with
  real `byte_count = array_type.sizeof(...)` allocation.

Local: `self.local_stack_arrays[name] = byte_count` and `allocate_local(name, size=byte_count)`.
Global: emit `byte_count` zero bytes (BSS) or fold the initializer (initializers for
multidim are out of scope this task — raise a clean `CompileError` "multidim array
initializers not yet supported" if `declaration.init is not None`).

`array_type.sizeof` needs element widths — pass `scalar_width=self._type_size`,
`pointer_width=self.target.int_size`.

- [ ] **Step 1: Failing runtime test — sizeof of a multidim local**

```python
def test_multidim_local_sizeof_is_product(tmp_path) -> None:
    # _compile defined in this file (copy the subprocess helper from the guard test)
    source = "int main(void) { int m[2][3]; return sizeof(m); }\n"
    assert _run_exit_code(source, tmp_path) == 24
```

`_run_exit_code` compiles with the existing `cc.py` subprocess pattern, assembles +
links into the OS image is heavy — instead assert via the **compiler succeeding** and a
follow-up QEMU run in Task 7. For Task 3, assert the compile now *succeeds* (returncode
0) where it used to be guarded, and assert the emitted `.asm` contains the right byte
reservation. Concretely:

```python
def test_multidim_local_compiles_and_reserves_bytes(tmp_path) -> None:
    result = _compile("int main(void) { int m[2][3]; m[0][0]=0; return 0; }\n", tmp_path)
    assert result.returncode == 0, result.stderr
```

(Will still fail here because subscript codegen lands in Task 4/5; so for Task 3 use a
program that declares + `sizeof`s but does NOT subscript:)

```python
def test_multidim_local_storage_compiles(tmp_path) -> None:
    result = _compile("int main(void) { int m[2][3]; return sizeof(m); }\n", tmp_path)
    assert result.returncode == 0, result.stderr
```

- [ ] **Step 2: Run, verify FAIL** — currently the local multidim guard raises
  `multidimensional … not yet supported`; returncode != 0.

- [ ] **Step 3: Implement storage; remove the two storage guards**

Replace the local guard body (generator.py:5714) with:
```python
array_type = self.array_types[statement.name]
byte_count = array_type.sizeof(scalar_width=self._type_size, pointer_width=self.target.int_size)
self.variable_types[statement.name] = statement.type_name
self.variable_arrays.add(statement.name)
self.allocate_local(statement.name, size=byte_count)
self.local_stack_arrays[statement.name] = byte_count
```
Replace the global guard (generator.py:3678) similarly: register into `global_arrays`
and have `_emit_global_array` reserve `array_type.sizeof(...)` bytes (BSS) or raise the
initializer-not-supported error. Keep the **struct-field** guard (3772) and **param**
guard (emission.py:3078) untouched — out of scope.

- [ ] **Step 4: Run, verify PASS** (and add a global variant
  `int g[2][3]; int main(void){ return sizeof(g); }`).
- [ ] **Step 5: Invariants + commit.** Byte gate: still identical (no existing program
  declares a multidim array). Commit `feat(cc): contiguous storage for multidim arrays`.

---

### Task 4: `sizeof` multidim branch + `unsigned short` stride fix

**Files:**
- Modify: `cc/codegen/x86/emission.py` — `SizeofVar` branch (~2717).

Add: if `vname in self.array_types` and it's an `ArrayType`, emit
`mov eax, {array_type.sizeof(...)}`. While here, fix the latent single-dim stride bug:
the global-array `sizeof` branch hardcodes `stride = 1 if byte else int_size`; replace
with `self._type_size(element_type)` so `unsigned short arr[10]` yields 20, not 40.

- [ ] **Step 1: Failing unit test** asserting the emitted `.asm` for
  `sizeof(unsigned short arr[10])` contains `mov eax, 20` (use the subprocess compile +
  grep the output `.asm`). Also `int m[2][3]` → `mov eax, 24`.
- [ ] **Step 2: Run, verify FAIL** (current output is 40 / guarded).
- [ ] **Step 3: Implement** the multidim branch and the stride fix.
- [ ] **Step 4: Run, verify PASS.**
- [ ] **Step 5: Invariants.** ⚠️ The `unsigned short` fix CHANGES output for any existing
  program that does `sizeof(unsigned short array)`. Run the byte gate; if a function
  shifts, that is the *intended* correctness fix — confirm the only deltas are
  `unsigned short`/narrow sizeof sites, record them in the commit body, and update the
  `test_cc_function_sizes` baseline if the gate stores one. Commit
  `fix(cc): sizeof uses real element stride (unsigned short) + multidim sizeof`.

---

### Task 5: Parser — uniform nested `SubscriptPlace` for N-D subscripts

**Files:**
- Modify: `cc/parser.py` — the `name[...]` postfix path (~809–899) and the
  statement-context subscript path (~1005). Replace the 2-subscript
  `SubscriptPlace(DereferencePlace(Index(...)))` construction with: build
  `Index(array=Var, index=first)` for ONE subscript (unchanged), and for TWO OR MORE,
  build a uniform left-nested `SubscriptPlace` chain over a `VariablePlace` base with NO
  `DereferencePlace`:
  `SubscriptPlace(base=SubscriptPlace(base=VariablePlace(name), index=i), index=j)…`
- Test: `tests/unit/test_cc_multidim_parser.py` (extend, keep sorted).

Single-subscript `a[i]` stays an `Index` node (untouched — byte-critical for the whole
corpus). Only 2+ subscripts change shape.

- [ ] **Step 1: Failing AST tests**

```python
def test_two_subscripts_build_uniform_nested_subscript_place() -> None:
    expr = _parse_expression("a[i][j]")     # add a small expr-parse helper
    assert expr == ast_nodes.PlaceLoad(place=ast_nodes.SubscriptPlace(
        base=ast_nodes.SubscriptPlace(base=ast_nodes.VariablePlace(name="a"), index=ast_nodes.Var(name="i")),
        index=ast_nodes.Var(name="j")))


def test_three_subscripts_parse_to_triple_nested_subscript_place() -> None:
    expr = _parse_expression("a[i][j][k]")
    assert isinstance(expr.place, ast_nodes.SubscriptPlace)
    assert isinstance(expr.place.base, ast_nodes.SubscriptPlace)
    assert isinstance(expr.place.base.base, ast_nodes.SubscriptPlace)
```

- [ ] **Step 2: Run, verify FAIL** (current shape has `DereferencePlace`; triple is
  rejected). ⚠️ This step changes the AST for *existing* array-of-pointers `a[i][j]`
  programs — Task 6 makes codegen reproduce their bytes. Between Task 5 and Task 6 the
  byte gate MAY break for array-of-pointers corpus files; that is expected mid-flight.
  Tasks 5 and 6 land in a **single commit** so the tree is never byte-broken.

- [ ] **Step 3: Implement** the uniform-shape construction in both parser sites; also
  handle postfix `++`/`--`, call, and assignment forms over the new shape (mirror the
  existing branches). Lift the "no third `[`" cap.

- [ ] **Step 4: Run AST tests, verify PASS.** Do NOT run the byte gate yet (expected red
  until Task 6). Do NOT commit yet.

---

### Task 6: Codegen dispatch — multidim row-major vs. legacy reconstruction

**Files:**
- Modify: `cc/codegen/x86/generator.py` (and/or `emission.py` where PlaceLoad/PlaceStore
  for `SubscriptPlace` is lowered — confirm via the resolve_address call sites at
  generator.py:2665 / :2706 and the double-index dispatch).

Add a classifier `_subscript_chain(place)` that, for a uniform nested `SubscriptPlace`
over a `VariablePlace`, returns `(base_name, [index_nodes…])` else `None`. Lowering of a
uniform `SubscriptPlace` load/store:

1. If `base_name in self.array_types` and it is a multidim `ArrayType`: emit **row-major**
   addressing. Linear element index via Horner over the dims
   `lin = ((i0*d1 + i1)*d2 + i2)…`; byte offset `lin * element_size`; address
   `base + offset`; load/store at `element_size` width. Prefer routing through
   `resolve_address` + `_accumulate_subscript` (already sums scaled indices into the BX
   index register and folds constants into displacement) by feeding it the per-dimension
   strides; otherwise emit directly mirroring `_generate_index_expression`'s dynamic-index
   block. The number of indices must equal the array's dimension count — else
   `CompileError` "wrong subscript count for 'm'".
2. Else (base is a pointer / single-dim array / array-of-pointers): reconstruct the
   **legacy** node and call the existing emitter, byte-identical:
   - exactly 2 indices → `SubscriptPlace(base=DereferencePlace(pointer=Index(array=Var(base_name), index=i0)), index=i1)`
     → `_emit_double_index_place_load` / `_emit_double_index_place_store`.
   - exactly 1 index → `Index(array=Var(base_name), index=i0)` (shouldn't occur — 1
     stays an `Index` from the parser — assert).
   - 3+ indices on a non-multidim base → `CompileError` "triple subscript requires a
     multidimensional array" (matches the pre-existing limitation).

- [ ] **Step 1: Failing tests** — reuse the Task 5 AST tests (already green) PLUS a
  runtime/compile test:

```python
def test_two_dim_array_of_pointers_still_compiles(tmp_path) -> None:
    # a representative existing array-of-pointers program from the corpus, inline
    src = "int row0[3]; int row1[3]; int* grid[2];\n"          \
          "int main(void){ grid[0]=row0; grid[1]=row1; grid[1][2]=9; return grid[1][2]; }\n"
    assert _compile(src, tmp_path).returncode == 0
```
and a multidim one:
```python
def test_two_dim_contiguous_compiles(tmp_path) -> None:
    assert _compile("int main(void){ int m[2][3]; m[1][2]=7; return m[1][2]; }\n", tmp_path).returncode == 0
```

- [ ] **Step 2: Run, verify FAIL** (multidim subscript unhandled / array-of-pointers
  now hits the new uniform shape with no dispatch yet).
- [ ] **Step 3: Implement** the classifier + dispatch (both load and store).
- [ ] **Step 4: Run, verify PASS** (unit + runtime-compile tests).
- [ ] **Step 5: Invariants — the byte-exactness checkpoint.**

```bash
python3 tests/test_cc_function_sizes.py   # MUST be byte-identical: array-of-pointers
python3 tests/test_cc_place.py            #   corpus routes through the legacy emitter
python3 -m pytest tests/unit/ -q
```
If the byte gate shows ANY array-of-pointers function delta, the legacy reconstruction is
not exact — fix until identical. Then commit Tasks 5+6 together:
`feat(cc): row-major addressing for contiguous multidim arrays`.

---

### Task 7: QEMU runtime tests + lift remaining-in-scope guards

**Files:**
- Modify: `tests/test_programs.py` — add entries that boot the OS and run a program
  exercising `int m[2][3]` and `char`/3-D variants, checking printed output.
- Modify: `tests/unit/test_cc_multidim_codegen_guard.py` — the local/global multidim
  cases now COMPILE; move them from "rejected" to "compiles", keep struct-field and param
  cases as still-guarded.

- [ ] **Step 1:** Write a runtime program (e.g. `user/static/` or an inline test program)
  that fills `int m[2][3]` in a loop and prints a checksum; add a `test_programs.py`
  entry with the expected regex.
- [ ] **Step 2:** Run `python3 tests/test_programs.py <name>` → verify it FAILS before the
  program exists / PASSES after.
- [ ] **Step 3:** Update the guard tests to the new reality (local+global multidim compile;
  struct-field + param still error). Run `pytest tests/unit/test_cc_multidim_codegen_guard.py`.
- [ ] **Step 4:** Full local matrix per CLAUDE.md "Run full CI matrix locally on big
  changes": `test_asm`, `test_bboefs`, `test_programs.py --filesystem ext2`,
  `test_cc_function_sizes`, `test_cc_place`, `pytest tests/unit/`.
- [ ] **Step 5:** Commit `test(cc): runtime coverage for contiguous multidim arrays`.

---

## Self-Review notes

- **Spec coverage:** storage (T3), sizeof (T1,T4), parser N-D (T5), row-major addressing
  load+store (T6), decay — *intentionally deferred* (passing multidim to functions stays
  guarded via the param guard; document in T7). Pointer-to-array, struct multidim fields,
  multidim params: out of scope, guards retained.
- **Byte-exactness:** the only intended byte-output change in the whole plan is the
  `unsigned short` sizeof fix (T4); every other task is additive on never-before-compiled
  multidim shapes, and T6 routes array-of-pointers through the untouched legacy emitter.
  The T5+T6 single-commit rule prevents a byte-broken intermediate tree.
- **Type consistency:** registry is `self.array_types: dict[str, ArrayType]`;
  `_register_array_type(name, *, type_name, dimensions, line)`; `Type.sizeof(*, scalar_width, pointer_width)`;
  classifier `_subscript_chain(place) -> tuple[str, list[Node]] | None`. Names used
  identically across tasks.
- **Open verification risk:** Task 6's "route through resolve_address vs. emit directly"
  is left to the implementer to pick based on which yields correct row-major code; the
  runtime tests (T7) are the oracle, the byte gate guards the legacy path.
