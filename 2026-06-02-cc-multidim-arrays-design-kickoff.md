# cc.py Multidimensional Arrays — Design Kickoff (decisions + open axes)

**Status:** Design not started — this is a checkpoint capturing the decisions and
open design axes so the foundational design can begin in a dedicated session with
fresh context. No spec or plan yet.

## How we got here

The Plan 5 `Place` refactor reached its merged Stage 3, decomposed (recursion-first)
into sub-stage **3a = recursive address resolver**. 3a's codegen-only design was
approved and **Task 1 (`MemOperand` + `resolve_address` skeleton) was implemented**
(committed, inert, byte-identical, on branch `bboe/cc-place-plan5-stage3a`).

Implementing further revealed the **parser caps lvalue depth**: it rejects true
multidimensional array decls (`int m[2][3]`), pointer-to-array types
(`int (*p)[3]`), and triple+ subscripts (`m[i][j][k]`), and member-index is limited
to element sizes 1–2. So the codegen-only recursive resolver cannot deliver
arbitrary depth end-to-end — the parser never produces those trees.

Decision (user): **expand scope to include the parser/type/codegen work** for full
arbitrary-depth lvalues. The expanded scope splits along the type-system fault line:

- **A — pointer-chain depth + resolver unification:** lift the parser subscript cap
  to N; finish the resolver unification (absorb the five bespoke emitters, retire the
  double-index hack, lift the element-size-1/2 member-index gate). Delivers
  `a[i][j][k]` on pointer arrays, chained `a->b->c`, deep dot chains, `cells[i].f[j]`
  with int elements. **No type-system change.** Reuses Task 1.
- **B — true multidimensional arrays:** `int m[2][3]`, `int (*p)[3]`, row-major
  `(i*cols+j)*elem` layout, multidim `sizeof`, decay. Foundational; needs a real
  type representation.

Decision (user): **design B first**, with a **structured `Type` object** as the
representation (not nested-bracket strings, not a dims side-table).

## Why B is foundational (the type-system reality)

cc.py has **no structured type system today**. Types are flat strings
(`variable_types[name]` holds the *element* type), with array metadata in separate
side-channels (`variable_arrays` set, `local_stack_arrays` byte-count, `global_arrays`
→ `ArrayDecl`); struct fields bake the count into the string (`"char[15]"`). Type
strings are consumed in dozens of places: `_type_size` (generator.py ~4801), struct
field layout (~3709), `sizeof` (emission.py ~2717), array decay (emission.py
~2773/2782), declarator parsing (parser.py), and codegen addressing. A structured
`Type` for arrays is therefore a **partial type-system migration** — every site
reading a type string must handle the structured form (or go through a bridge).

## Open design axes for B (resolve in the dedicated session, in order)

1. **Type-model shape** — a single `kind`-tagged dataclass
   (`Type(kind, element/pointee, dimensions, struct_tag, ...)`) vs a class hierarchy
   (`ScalarType` / `PointerType` / `ArrayType` / `StructType`). Pointer-to-array and
   array-of-array must both be expressible.
2. **Migration strategy** — big-bang replacement of flat strings everywhere vs
   introduce `Type` with a `to_string()`/`from_string()` bridge and migrate consumers
   incrementally (arrays/pointer-to-array use the structured form where layout/sizeof/
   decay/addressing need it; scalars/pointers can stay string-backed until touched).
   This decision governs tractability and risk.
3. **Parser** — multi-`[N]` declarators (local/global VarDecl + struct field + param),
   pointer-to-array grammar (`T (*p)[N]`), and N-dimensional subscript expressions
   (lift the 2-subscript cap at parser.py ~840 / ~999).
4. **Layout & sizeof** — row-major stride (`m[i][j]` = base + (i*cols + j)*elem);
   multidim `sizeof`; struct multidim-array fields. Fix the latent single-dim
   `sizeof` stride bug for `unsigned short` while here.
5. **Decay** — `int m[2][3]`: `m` → `int(*)[3]`, `m[i]` → `int[3]` → `int*`;
   passing multidim arrays / pointer-to-array to functions.
6. **Codegen** — true contiguous multidim addressing via the recursive resolver
   (`resolve_address`, Task 1 substrate): a `SubscriptPlace` over a `SubscriptPlace`
   with **no** intervening deref accumulates nested stride (contiguous), distinct from
   the array-of-pointers shape (deref between dims). Also lift the element-size-1/2
   member-index restriction (generator.py ~2138).

## Gate

The byte-efficiency gate (`tests/test_cc_function_sizes.py`, per-function bytes ≤
baseline, or notable perf win) and the `cc_place` golden remain the codegen oracles;
new multidim shapes get compiled-and-run runtime tests in `tests/test_programs.py`.

## Preserved work

- **Task 1** (`MemOperand` + `resolve_address` skeleton, inert/byte-identical) on
  `bboe/cc-place-plan5-stage3a` — the codegen substrate for both A and B.
- The 3a resolver architecture (deref-breaks-the-chain, terminal bitfield/width,
  register-freedom, Clang `EmitLValue` / GCC `get_inner_reference` consistency) in
  `2026-06-02-cc-place-refactor-plan5-stage3a-design.md`.
- A (pointer-chain depth + resolver unification) remains available to do independently
  of B if priorities shift.
