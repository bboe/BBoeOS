# Pointer-to-Array & Multidim Params/Decay — Implementation Plan

> REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Goal:** Support pointer-to-array `int (*p)[3]` (local/global vars and params), passing
multidim arrays to functions (`void f(int m[][3])` / `int m[2][3]` decays to `int(*)[3]`),
subscripting them `p[i][j]`, and their `sizeof`. Lifts the LAST multidim guard (the
parameter guard). Final chunk (3 of 3) of the deferred multidim work.

**The unifying insight:** a multidim param `int m[][3]` IS a pointer-to-array `int(*)[3]`.
Subscripting a pointer-to-array `p[i0][i1]...[in]` (where `p` points to `E[d1]..[dn]`) =
**load the pointer value as the base**, then row-major over dims `[d1,..,dn]`: stride[k] =
elemsize * product(dims[k:]) for k in 0..n (n+1 strides for n+1 indices — the outermost
index strides over the whole pointee array). This is the existing row-major Horner with a
loaded-pointer base instead of an array address (deref-breaks-the-chain).

**Representation:** a new dict `self.pointer_array_types: dict[str, Type]` mapping a name →
`PointerType(ArrayType(...))` (the structured type cc.types already supports). `variable_types[name]`
keeps a pointer-ish string so legacy pointer sites (pointer width, pointer-ness) still work;
the structured dict drives subscript addressing and sizeof. Subscript/sizeof codegen consults
`pointer_array_types` BEFORE the legacy string path.

**Out of scope (keep erroring/unchanged):** pointer-to-array of struct element; `int *p[3]`
(array of pointers — already works via existing string path, leave it); function returning
pointer-to-array; multi-level `int (**p)[3]`. Existing plain pointers/arrays byte-identical.

## Invariants at every task
- `test_cc_function_sizes` / `test_cc_place` byte-identical; `test_cc_bits` 16+32 pass;
  `pytest tests/unit/` green; `ruff` clean; alphabetical ordering; commit per task.

---

### Task 1: Parser — pointer-to-array declarator `(*name)[N]...`

**Files:** `cc/parser.py` local (`_parse_one_declarator` ~1339, the `LPAREN STAR` branch),
global (~660-770), and param declarator. Test: `tests/unit/test_cc_pointer_to_array_parser.py`.

- In the `(` `*` `name` ... declarator paths, after `(*name)`, if the next token is `[`
  (NOT `(` which is the function-pointer case and NOT `[` immediately after `name` inside
  the parens which is array-of-fnptr), parse one-or-more `[N]` brackets as the POINTEE
  array dimensions and record the declarator as a pointer-to-array. Concretely: `int (*p)[3]`
  → a VarDecl/Param marked as pointer-to-array with pointee dims `[3]` and element `int`.
  Choose the AST carrier: simplest is to set `type_name` to a flat marker the codegen can
  re-parse, e.g. store the pointee array dims on a new optional field. RECOMMENDED: add an
  optional `pointer_array_dimensions: list[Node] | None` field to `VarDecl` and `Param`
  (alphabetical), set to the pointee `[N]` list when the `(*name)[N]` form is seen, else None;
  `type_name` stays the element type (e.g. "int"). (Mirror how `ArrayDecl.dimensions` /
  `Param.dimensions` already work.)
- The existing function-pointer `(*name)(params)` and array-of-fnptr `(*name[N])(params)`
  paths must be UNCHANGED — only the `(*name)[N]` (bracket, not paren, and no trailing
  `(params)`) is the new case.
- Tests (alphabetical): `int (*p)[3];` (global) → VarDecl with pointer_array_dimensions
  [Int(3)], type_name "int"; local `int (*p)[3];`; `int (*p)[3][4];` → dims [Int(3),Int(4)];
  param `int f(int (*p)[3]){...}` → Param with pointer_array_dimensions [Int(3)]; and confirm
  `int (*fp)(int);` (function pointer) and `int *q[3];` (array of pointers) still parse
  UNCHANGED.
- TDD. Byte gates identical (parser-only; codegen for the new shape comes next, and an
  unsupported use still errors cleanly).

---

### Task 2: Pointer-to-array variables — registration, subscript addressing, sizeof

**Files:** `cc/codegen/x86/generator.py` (register `pointer_array_types`, subscript dispatch,
addressing), `cc/codegen/x86/emission.py` (sizeof + decl handling). Test: asm-shape unit +
QEMU.

- Add `self.pointer_array_types: dict[str, Type] = {}` (alphabetical) in `__init__`. When a
  VarDecl (local/global) has `pointer_array_dimensions`, build
  `PointerType(ArrayType(d1, ArrayType(d2, ... element)))` and store it; allocate a
  POINTER-sized slot (it's a pointer, not inline storage); set `variable_types[name]` to a
  pointer string (e.g. element+"*") so legacy pointer-ness holds.
- **Addressing:** add a classifier recognizing a uniform nested `SubscriptPlace` over a
  `VariablePlace` whose name is in `pointer_array_types`. Recover `(name, [indices])`. Emit:
  load the pointer VALUE of `name` into the base register (it's a variable holding an
  address — read its slot), then row-major Horner over the pointee dims with strides
  `elemsize * product(dims[k:])` (n+1 strides for n+1 indices), materializing into SI for
  16-bit legality, terminal load/store at element width. Wire into `_emit_place_load` /
  `_emit_place_store` BEFORE the contiguous-array multidim path (which keys on `array_types`).
  `p[i]` with a single subscript on `int(*p)[3]` yields an address of an `int[3]` (decay) —
  support at least the full-depth `p[i][j]` (n+1 subscripts); fewer subscripts may
  CompileError "unsupported partial subscript of pointer-to-array" if it complicates things
  (document).
- **sizeof:** `sizeof(p)` = pointer width; `sizeof(*p)` and `sizeof(p[i])` = pointee array
  size (`ArrayType.sizeof`). Wire via `pointer_array_types` where the sizeof code already
  special-cases `array_types`.
- Tests: asm-shape — global `int (*p)[3]; ... p[1][2]` (dynamic base load + (1*3+2)*4
  offset); QEMU program: `int m[2][3]` + `int (*p)[3] = m;` then read `p[1][2]`, and a
  loop filling via `p[i][j]`. (Assigning `p = m` decays the array to its address — ensure
  that decay produces `m`'s base address; reuse existing array-decay.)
- Byte gates identical; `test_cc_bits`.

---

### Task 3: Multidim params + decay + lift the guard + runtime matrix

**Files:** `cc/codegen/x86/emission.py` (param lowering + the guard ~3097),
`cc/codegen/x86/generator.py` (arg push / decay). Test: QEMU + guard-test update.

- Remove the param guard. For a param with `dimensions` (multidim, e.g. `int m[][3]` →
  `[None, Int(3)]`) OR `pointer_array_dimensions`: register it in `pointer_array_types` as
  `PointerType(ArrayType(<inner dims, dropping a leading unsized outer>, element))`. The
  param is pointer-sized in its slot and holds the passed address; subscripting `m[i][j]`
  inside the callee goes through the Task-2 pointer-to-array addressing.
  - For `int m[2][3]` as a param (sized outer): C decays it to `int(*)[3]` — drop the outer
    dim, register pointee dims `[3]`.
- **Decay at call site:** passing a multidim array `int m[2][3]` as an argument pushes its
  base address (the existing array-arg push already does this for arrays — confirm it fires
  for multidim arrays in `array_types`/`local_stack_arrays`/`global_arrays`; if a multidim
  array isn't recognized by the arg-push fast path, add it so its address is pushed).
- Runtime program `tests/programs/multidim_param_test.c`: a function `int sum(int m[][3], int rows)`
  that loops `m[i][j]` (while/i++), called with a local and a global `int m[2][3]`; print the
  sum; also exercise an explicit `int (*p)[3]` local pointing at the array. Add a
  `tests/test_programs.py` entry. RUN it — values must be correct (report if not).
- Update `tests/unit/test_cc_multidim_codegen_guard.py`: the param multidim case now COMPILES
  (no guards left — confirm `grep "not yet supported in codegen"` returns nothing).
- FULL matrix (per CLAUDE.md): byte gates, `test_cc_bits`, `pytest tests/unit/`, `test_asm`,
  `test_programs` (bbfs + try ext2 if available), `test_cc_compatibility`.

## Self-review
- Coverage: parser (T1), pointer-to-array vars+addressing+sizeof (T2), params+decay+guard
  lift+runtime (T3). Out-of-scope shapes stay erroring.
- Byte-safety: new dict + new syntax; plain pointers/arrays/existing params untouched →
  byte gates identical. 16-bit via SI-materialization.
- Type consistency: `pointer_array_types: dict[str, Type]` holds `PointerType(ArrayType(...))`;
  `VarDecl.pointer_array_dimensions` / `Param.pointer_array_dimensions` carry the pointee dims.
