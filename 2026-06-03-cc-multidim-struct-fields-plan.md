# Multidimensional Array Struct Fields — Implementation Plan

> REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Goal:** Support multidim array struct fields — `struct grid { int cells[2][3]; };` —
including layout (contiguous row-major field) and access `g.cells[i][j]` /
`p->cells[i][j]` (load + store), for scalar element types (int/char/unsigned short).
Lifts the `multidimensional array field ... not yet supported` guard.

**Context (merged):** struct-field type is baked as the flat string `"int[2][3]"`;
`Type.from_string("int[2][3]")` → nested `ArrayType`; the multidim variable subscript
machinery `_emit_multidim_subscript_address(base_name, indices)` does row-major Horner
addressing (frame/label base). Single-dim member-array access uses the bespoke
`_emit_member_index_access` path (with a 1/2-byte element gate). resolve_address already
composes `MemberPlace`+`SubscriptPlace` but is NOT the live member path.

**Out of scope (keep erroring/unchanged):** multidim fields of struct element type;
multidim field initializers inside a struct initializer; taking a multidim field as a
whole (decay). Single-dim member access stays exactly as today.

## Strategy
- **Parser:** after a member subscript `base.member[i]`, apply the existing
  `_extend_subscript_chain` so `g.cells[i][j]` parses to
  `SubscriptPlace(SubscriptPlace(MemberPlace(VariablePlace(g),"cells"), i), j)` — a
  uniform nested SubscriptPlace over a MemberPlace, no deref. Single subscript after a
  member is unchanged.
- **Layout:** in the struct-layout builder, replace the `count("[")>1` guard: for a
  multidim field, compute `field_size = Type.from_string(ftype).sizeof(...)` and
  `element_size = sizeof(innermost element)`. Keep single-dim/scalar fields byte-identical.
- **Addressing:** detect a uniform nested `SubscriptPlace` chain whose innermost base is
  a `MemberPlace` naming a MULTIDIM array field; compute the struct base address +
  field offset, then row-major over the FIELD's dimensions (reuse the Horner stride
  logic, rooted at field offset). This bypasses the 1/2-byte member gate (it handles any
  element size, like the variable multidim path). Single-subscript member access and the
  existing bespoke path are untouched.

## Invariants at every task
- `test_cc_function_sizes` / `test_cc_place` byte-identical; `test_cc_bits` 16+32 pass;
  `pytest tests/unit/` green; `ruff` clean; alphabetical ordering; commit per task.

---

### Task 1: Parser — nested subscript after a struct member

**Files:** `cc/parser.py` (member-then-subscript branch, ~lines 914-931, and any
statement-context twin). Test: `tests/unit/test_cc_multidim_struct_field_parser.py`.

- After parsing the first `[i]` following `.member`/`->member`, call
  `self._extend_subscript_chain(SubscriptPlace(base=member_place, index=index), line=...)`
  to consume any further `[j]...` brackets into a uniform nested SubscriptPlace — exactly
  as the bare-array path already does. Apply to load, store, `++`/`--`, and (if present)
  the statement-context member-subscript site. Single subscript still yields the same
  one-level SubscriptPlace (so existing single-dim member access is byte-identical).
- Tests (alphabetical, drive tokenize+Parser): `g.cells[i][j]` →
  `SubscriptPlace(SubscriptPlace(MemberPlace(VariablePlace("g"),"cells"), i), j)`;
  `p->cells[i][j]` → same over `MemberPlace(DereferencePlace(...),"cells")`;
  single `g.cells[i]` → unchanged one-level shape; assignment `g.cells[i][j]=x` parses.
- TDD. Byte gates identical (parser-only; a multidim field still hits the layout guard).

---

### Task 2: Struct layout — multidim field size + lift the guard

**Files:** `cc/codegen/x86/generator.py` struct-layout builder (~line 4020, the
`ftype.count("[") > 1` guard) + `FieldInfo`. Test: extend the struct-field parser/asm
tests or a new `tests/unit/test_cc_multidim_struct_field_layout.py`.

- Remove the `count("[")>1` guard. For a field whose `type_name` has ≥2 `[`:
  `array_type = Type.from_string(ftype)`;
  `field_size = array_type.sizeof(pointer_width=self.target.int_size, scalar_width=self._type_size)`;
  `element_size = self._type_size(<innermost element type string>)` (walk the ArrayType
  to the scalar, or `_type_size` on the de-bracketed base). Store `FieldInfo(..., element_size,
  field_size, type_name=ftype)`. Single-`[` and scalar fields keep the existing exact
  computation (byte-identical).
- Test: a struct `{ int cells[2][3]; int after; }` — assert the layout offsets: `cells`
  at 0 with field_size 24, `after` at 24 (via a compiled program's `sizeof`/offset or a
  white-box layout check). `sizeof(struct)` correct.
- Byte gates identical (no existing struct uses a multidim field). `test_cc_bits`.

---

### Task 3: Codegen — row-major member access + runtime

**Files:** `cc/codegen/x86/generator.py` (the PlaceLoad/PlaceStore dispatch that handles
SubscriptPlace, and the multidim row-major helper). Test: asm-shape unit tests +
QEMU runtime.

- Add a classifier for a uniform nested `SubscriptPlace` chain whose innermost base is a
  `MemberPlace(VariablePlace, field)` (or over a `DereferencePlace` for `->`) where
  `field` is a MULTIDIM array field. Recover `(struct base, field_offset, field dims,
  element_size, [indices])`.
- Generalize the existing `_emit_multidim_subscript_address` (or add a sibling) to take a
  base address + starting displacement (the field offset) + per-dimension counts +
  element size, and emit the row-major address (Horner; constants fold into displacement;
  dynamic indices summed into the index register; materialize into SI for a frame base so
  16-bit `[bp+...]`+index stays legal — same fix as the variable multidim path). Load and
  store at element width.
- Wire load (`_emit_place_load`) and store (`_emit_place_store`) to dispatch member-rooted
  multidim chains to this path BEFORE the bespoke `_emit_member_index_access`. Non-multidim
  member subscripts fall through to the existing bespoke path unchanged (byte-identical).
- Subscript-count must equal the field's dimension count else `CompileError`.
- Tests: asm-shape (constant `g.cells[1][2]` of `int[2][3]` → displacement field_offset +
  (1*3+2)*4); QEMU program `tests/programs/multidim_struct_test.c`: a struct with
  `int cells[2][3]`, fill via `g.cells[i][j]=i*3+j` in `while`/`i++` loops, read back and
  print values proving row-major; also a `->` (pointer) access via `&g`. Add a
  `tests/test_programs.py` entry. Run it — values must be correct.
- Update `tests/unit/test_cc_multidim_codegen_guard.py` struct-field case: multidim struct
  field now compiles (keep param multidim still guarded).
- Full matrix: byte gates, `test_cc_bits`, `pytest tests/unit/`, `test_asm`, `test_programs`.

## Self-review
- Coverage: parser nested member-subscript (T1), layout+size (T2), row-major access
  load/store + runtime (T3). Out-of-scope (struct-element fields, whole-field decay) stay
  unchanged/guarded.
- Byte-safety: single-dim member access + existing struct layouts untouched → byte gates
  identical. 16-bit: reuse the SI-materialization for frame-base+index.
