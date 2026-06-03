# Multidimensional Array Initializers — Implementation Plan

> REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Goal:** Support initializing contiguous multidim arrays — nested
`int m[2][3] = {{1,2,3},{4,5,6}}` and flat `int m[2][3] = {1,2,3,4,5,6}` — for
local and global scope, scalar element types (int/char/unsigned short), with
zero-fill for partial/`{0}` initializers. Lifts the two "multidimensional array
initializers are not yet supported" guards.

**Context (merged Stage 4):** multidim arrays get contiguous row-major storage;
`self.array_types[name]` holds the nested `ArrayType`; row-major subscript
addressing works. The guards are at generator.py:3868 (global) and :5966 (local).

**Out of scope (keep erroring / unchanged):** arrays-of-structs that are ALSO
multidim; string-literal init of `char[N][M]`; designated initializers.

## Strategy

- **Parser:** when the declarator is multidim (`dimensions` has ≥2 entries) and the
  element type is NOT a struct, parse the initializer with a multidim-aware path
  that accepts nested `{...}` as sub-array initializers (recursively, depth =
  number of dimensions) AND a flat list. Produce a **nested `ArrayInit`** (an
  `ArrayInit` whose `elements` may themselves be `ArrayInit`) or a flat `ArrayInit`.
  Do NOT change the existing struct-array nested-brace handling.
- **Codegen flatten helper:** one shared `_flatten_array_init(init, *, total)` that
  walks a (possibly nested) `ArrayInit` into a flat row-major list of constant
  element nodes, validates count ≤ total element count, and is used by both
  emission sites.
- **Global:** emit one `dd`/`dw`/`db` directive of the flattened constants, then
  `times (total - count)*stride db 0` zero-fill when short (mirrors the struct-array
  padding already at _emit_global_array).
- **Local:** the multidim local array is inline stack storage. Emit element stores
  `mov [<base> + i*elem], value` for each flattened constant, then zero the
  remaining bytes. (Single-dim local init keeps its pointer-to-rodata path,
  untouched.)

## Invariants at every task
- `tests/test_cc_function_sizes.py` PASS byte-identical; `tests/test_cc_place.py`
  PASS byte-identical (single-dim/struct init paths untouched).
- `tests/test_cc_bits.py` 16+32-bit all pass (any new program assembles at both).
- `pytest tests/unit/ -q` green; `ruff check` clean. Alphabetical ordering.
- Commit per task.

---

### Task 1: Parser — nested + flat multidim initializer

**Files:** `cc/parser.py` (the local + global decl init paths that call
`parse_array_init`, ~lines 745 and 1350; and `parse_array_init` itself ~1894).
Test: `tests/unit/test_cc_multidim_init_parser.py`.

- Add a multidim-aware initializer parse used when the declarator has ≥2
  dimensions and a non-struct element type. It recursively parses nested
  `{...}` into nested `ArrayInit` nodes; a non-brace element is parsed as an
  expression; a flat list (no nested braces) yields a flat `ArrayInit`. Trailing
  commas accepted. The existing `parse_array_init` (flat + struct nested) is left
  intact for single-dim and struct arrays.
- Tests (alphabetical): nested `int m[2][3] = {{1,2,3},{4,5,6}}` → `ArrayInit`
  with two `ArrayInit` children each holding three `Int`s; flat
  `int m[2][3] = {1,2,3,4,5,6}` → flat `ArrayInit` of six `Int`s; 3-D nested;
  `= {0}` → `ArrayInit([Int(0)])`.
- TDD: write the AST-shape tests first (drive via `tokenize`+`Parser`), watch fail,
  implement, pass. Byte gates stay identical (parser-only, no codegen yet — but a
  multidim init now PARSES and would hit the codegen guard; that's fine, guard
  still raises until Task 2/3).

---

### Task 2: Global multidim initializer codegen

**Files:** `cc/codegen/x86/generator.py` — `_emit_global_array` and the global
guard (3868). Add the shared `_flatten_array_init`. Test:
`tests/unit/test_cc_multidim_init_runtime.py` (asm-grep + later QEMU).

- Add `_flatten_array_init(init, *, total) -> list[Node]`: recursively flatten
  nested `ArrayInit` to row-major constants; raise `CompileError("too many
  initializers for '<name>'")` if the flattened count exceeds `total`.
- Remove the global initializer guard. For a multidim global with `init`: register
  the ArrayType (already done), compute `total = product(dims)`, flatten, emit a
  single `directive v0, v1, …` line (directive from element stride: db/dw/dd), then
  `times (total-count)*stride db 0` when count < total. Element strings via
  `_constant_expression` / `new_string_label` exactly as the single-dim path.
- Tests: `int g[2][3] = {{1,2,3},{4,5,6}};` → asm contains `dd 1, 2, 3, 4, 5, 6`;
  flat form same; partial `int g[2][3] = {1,2,3};` → `dd 1, 2, 3` + `times (6-3)*4
  db 0`; `char c[2][2] = {{1,2},{3,4}};` → `db 1, 2, 3, 4`.
- Byte gates identical (single-dim/struct global init untouched). Run
  `test_cc_bits.py`.

---

### Task 3: Local multidim initializer codegen + runtime

**Files:** `cc/codegen/x86/emission.py` (local ArrayDecl init handling ~3995) and
`cc/codegen/x86/generator.py` local guard (5966). Reuse `_flatten_array_init`.

- Remove the local initializer guard (so scan_locals registers + allocates inline
  storage for the multidim array as today, even with an init).
- In the local ArrayDecl handling: if the array is multidim (in `self.array_types`)
  and has an init, flatten it and emit element stores into the inline slots:
  `mov [<base> + i*elem], <const>` at element width for each flattened element,
  then zero the remaining `(total-count)` elements (emit `mov [...], 0`). Do NOT
  use the pointer-to-rodata path for multidim. Single-dim local init unchanged.
- Pick the correct base address form for both bit widths (reuse the local-address
  helper; constant offsets `i*elem` fold into the displacement — `[bp-base+disp]`
  is valid at both widths since there's no index register here).
- Runtime test: add a QEMU program to `tests/programs/` + a `tests/test_programs.py`
  entry that declares `int m[2][3] = {{1,2,3},{4,5,6}}` (local) and a global, reads
  cells back, prints values that prove correct row-major init incl. a partial-init
  zero-fill cell. Use `while`/`i++` loops (NOT `for(;;i=i+1)`).
- Update `tests/unit/test_cc_multidim_codegen_guard.py`: multidim init now compiles
  (local + global); remove/repurpose the "initializer rejected" assertions.
- Full local matrix: byte gates, `test_cc_bits.py`, `pytest tests/unit/`,
  `test_asm.py`, `test_programs.py`.

## Self-review
- Coverage: parser nested/flat (T1), global emit+zerofill (T2), local emit+zerofill
  +runtime (T3). Out-of-scope (struct-multidim, string-init char[][]) stay
  guarded/erroring.
- Byte-safety: only the two guarded multidim-init paths change; single-dim and
  struct-array init are untouched → byte gates identical.
- Types: `_flatten_array_init(init, *, total) -> list[Node]` shared by T2/T3.
