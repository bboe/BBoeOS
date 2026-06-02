# cc.py `Place` Refactor — Plan 2: the Member\* family

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert the six legacy `Member*` AST nodes (`MemberAccess`,
`MemberAssign`, `MemberAddressOf`, `MemberIndex`, `MemberIndexAssign`,
`MemberIncrementDecrement`) onto the recursive `Place` core introduced in Plan 1
(PR #573), extending `_resolve_place` + `_emit_place_*` to cover every member
shape **byte-for-byte**, then delete the legacy nodes and their
`generate_member_*` methods.

**Gate:** BYTE-EXACT. The golden snapshot must stay byte-identical for converted
shapes; any asm change is a bug to investigate, never to bless. One plan, one PR,
on branch `bboe/cc-place-plan2`.

---

## Overview

Plan 1 (commit `367e9990`) introduced a recursive `Place` lvalue abstraction and
converted only the struct-array `IndexMember*` shapes (`arr[i].member`,
`arr[i].member[j]`). The codegen for those flows through a symbolic
`PlaceAddress` dataclass and a recursive `_resolve_place` + `_emit_place_load` /
`_emit_place_store` pair in `cc/codegen/x86/generator.py`.

Plan 2 converts the six remaining legacy member nodes so the parser emits
`Place` / `PlaceLoad` / `PlaceStore` / `PlaceAddressOf` / `PlaceIncDec`, extends
`_resolve_place` + `_emit_place_*` to cover every member shape byte-for-byte,
then deletes the six legacy nodes and their `generate_member_*` methods.

The six nodes (definitions in `cc/ast_nodes.py`):

- `MemberAccess` — read; `object_name` OR `base_expr`
- `MemberAddressOf` — `&obj.field` / `&ptr->field`; `object_name` only
- `MemberAssign` — store; `object_name` OR `base_expr`
- `MemberIncrementDecrement` — `object_name` only; postfix only
- `MemberIndex` — `object_name` only; has `address_of` flag; inline-array AND
  pointer-field
- `MemberIndexAssign` — `object_name` only; inline-array AND pointer-field

Final acceptance: `tests/test_asm.py` 49/49, `tests/test_programs.py` (bbfs) and
`--filesystem ext2` green, plus the cc unit suites.

### Place mapping the parser must produce

- `obj.field` (dot, struct value) → `MemberPlace(VariablePlace("obj"), "field")`
- `ptr->field` (arrow) → `MemberPlace(DereferencePlace(VariablePlace("ptr")),
  "field")`   (since `p->f == (*p).f`)
- `a->b.c` → `MemberPlace(MemberPlace(DereferencePlace(VariablePlace("a")),"b"),"c")`
- `((struct T*)e)->f` → `MemberPlace(DereferencePlace(<cast expr>), "f")`
- `ptr->field[i]` (inline array) →
  `SubscriptPlace(MemberPlace(DereferencePlace(VariablePlace("ptr")),"field"), i)`
- `ptr->parr[i]` (pointer field) → same `SubscriptPlace` shape; codegen must
  preserve the legacy "load pointer then index" path
- `&ptr->field` / `&obj.field[i]` → `PlaceAddressOf(...)`
- `ptr->field++` → `PlaceIncDec(delta, is_postfix=True, place=MemberPlace(...))`

---

## Reconnaissance findings (verbatim legacy contracts)

All line numbers are pre-change, against the worktree at commit `367e9990`.

### The Place primitives already in place

`PlaceAddress` dataclass — `cc/codegen/x86/generator.py:136-150`:

```python
class PlaceAddress:
    const_base: str
    element_size: int
    field_size: int
    indexed: bool
    offset: int
```

It models `[const_base + offset (+ BX)]`. `const_base` is a label (`_g_arr`) or
frame string (`ebp-12`); `indexed` True means BX holds a dynamic byte offset.
`field_size != element_size` is the "array-typed member yields its address"
marker (`_emit_place_load` line 1765-1768 lea's instead of loading).

`_emit_place_load` (1756-1773) and `_emit_place_store` (1775-1794): both push BX
when `_bx_holds_pinned_var()` (532-543), call `_resolve_place`, then
`_build_address(const_base, offset, index=BX_or_"")` (515-530), then
`_emit_field_load`/`_emit_field_store` (1289-1320). The store evaluates the value
FIRST into AX, `push acc`, resolves the place (scratch BX/AX), `pop acc`, stores.
The load is_array_member → `lea acc, addr`.

`_resolve_place` (2962-3008): currently only Shape A (`arr[i].member`) and Shape
B (`arr[i].member[j]`), both via `_match_struct_array_member` (2450-2461) and
`_resolve_index_member_layout` (2880-2927). Raises `"unsupported Place shape in
_resolve_place"` otherwise.

`_build_address` (515-530): returns `[base+offset+index]`, dropping zero offset
and empty index.

`_emit_field_load` (1289-1303): size 1 → `emit_byte_load_zx(addr)`; size 2 on
int_size==4 → `movzx acc, word addr`; else `mov acc, addr`.

`_emit_field_store` (1305-1320): size 1 → `mov byte addr, al`; size 2 on
int_size==4 → `mov word addr, ax`; else `mov addr, acc`.

### Shared base-register rule (CRITICAL byte-exact detail)

Two distinct helpers reduce to the **same** observable rule. For an arrow access
of `object_name`:

- **SI fast path:** if `self.si_local == object_name`, use
  `self.target.si_register` directly (no load emitted).
- **Otherwise:** load into BX. The load is `_emit_load_var(object_name,
  register=bx)` (1640-1668).

`_load_member_base` (2381-2395) implements exactly this: SI if
`si_local==object_name`, else `_emit_load_var(name, register=bx)` and return bx.
Used by `generate_member_access` (scalar arrow field, 4658) and
`generate_member_assign` (scalar arrow field, 4818/4830, and the bitfield arrow
paths 4808/4818).

`_emit_member_index_base` (1725-1741) is the index-family equivalent: arrow →
`_emit_load_var(object_name, register=register)`; dot+local → `lea register,
[local_addr]`; dot+global_scalar → `lea register, [global_addr]`.
`generate_member_index`/`generate_member_index_assign` wrap it with an explicit
`si_local==object_name → mov bx, si  /  use si` test (4879-4883, 4908-4911,
4962-4966, 4991-4994).

Note the two index generators emit `mov bx, si` when SI holds the var **only on
the variable-index path** (4909, 4992) but use SI **directly** (`base_reg = si`)
on the constant-index path (4880, 4963). This asymmetry MUST be reproduced.

`_emit_load_var(arrow)` and `_emit_member_index_base(arrow=True)` emit identical
bytes for the arrow case (both call `_emit_load_var`). For the **dot** index
form, `_emit_member_index_base` lea's the local/global address into BX. For the
**dot scalar** access/assign form, the legacy code never enters
`_load_member_base`: it resolves a memory operand via `_resolve_struct_value_base`
(3010-3025) → `_local_address` (label or `ebp-N`) and folds the offset into the
address directly (no register).

### Per-shape byte-output contracts

**1. `obj.field` dot scalar read** (`generate_member_access` dot path,
4585-4628): validate struct value type. `base_operand =
_resolve_struct_value_base(obj)` → `_g_obj` (global) or `ebp-N` (local).
`is_array_field` (field_size != element_size) or `is_struct_value` (type starts
`struct ` not ending `*`): **yields address**. Global → `mov acc,
base_operand[+offset]` (label arithmetic, immediate). Local → `lea acc,
[base_operand[+offset]]`. Else scalar: `addr = _build_address(base_operand,
offset)`. Bitfield → `_emit_bitfield_read`. Plain → `_emit_field_load`.

**2. `ptr->field` arrow scalar read** (4630-4664): `info =
_resolve_member_index_layout(arrow=True, ...)` (2929-2959). Array/struct-value
field: `base_reg = _load_member_base(obj)`; `offset` → `lea acc,
[base_reg+offset]` else `mov acc, base_reg`. Scalar: `base_reg =
_load_member_base(obj)`; `addr = _build_address(base_reg, offset)`; bitfield →
`_emit_bitfield_read`; plain → `_emit_field_load`.

**3. `((struct T*)e)->f` and `a->b.c` read** (`_generate_member_access_via_expr`,
2133-2217): `base_type = _expression_type(base_expr)`; derive `tag`; lookup
`info`. **Fast path** (2173-2195): base is `Cast(AddressOf(local))` →
`direct_address = _local_address(local)`. Array/struct → `lea acc,
[direct_address+offset]` (or `[direct_address]`). Scalar → `addr =
_build_address(direct_address, offset)`; bitfield → `_emit_bitfield_read`; plain
→ `_emit_field_load`. **General path** (2196-2217): `generate_expression(base)`;
`mov bx, acc`; array/struct → `offset` ? `lea acc, [bx+offset]` : `mov acc, bx`;
scalar → `addr = _build_address(bx, offset)`; bitfield → `_emit_bitfield_read`;
plain → `_emit_field_load`.

**4. `obj.field = v` dot scalar store** (`generate_member_assign` dot path,
4748-4795): `base_operand = _resolve_struct_value_base`; `addr =
_build_address(base_operand, offset)`. Bitfield: (a) 1-bit + Int rhs in {0,1} →
`_emit_bitfield_write_literal`. (b) Int rhs + known_local_byte slot → inline
const-fold `mov byte addr, new_byte` (4774-4783). (c) else
`generate_expression(expr)` then `_emit_bitfield_write`. Plain:
`generate_expression(expr)`; `_emit_field_store`. **No `push`/`pop` of the
value** — dot store evaluates rhs directly then stores to the static address.

**5. `ptr->field = v` arrow scalar store** (4796-4832): Bitfield 1-bit literal:
`base_reg = _load_member_base`; `addr`; `_emit_bitfield_write_literal`. Bitfield
general: `generate_expression(expr)` FIRST, then `base_reg = _load_member_base`,
`_emit_bitfield_write`. (rhs-before-base ordering — comment at 4812.) Plain:
`generate_expression(expr)` FIRST, then `base_reg = _load_member_base`,
`_emit_field_store`. (rhs-before-base, comment 4828.) Again **no push/pop**: rhs
lands in AX, base loads into BX/SI, store reads AX.

**6. `a->b.c = v` chained store** (`_generate_member_assign_via_expr`,
2219-2258): `generate_expression(base_expr)`; `mov bx, acc`;
`generate_expression(expr)`; `addr = _build_address(bx, offset)`;
`_emit_field_store`. (base-first here, unlike the named-arrow store.)

**7. `&obj.field` / `&ptr->field`** (`generate_member_address_of`, 4666-4727):
reject bitfield. Arrow: `_emit_load_var(obj, register=acc)`; `offset` → `add acc,
offset`. Dot global: `lea acc, [base_label+offset]` (or `[base_label]`). Dot
local: `lea acc, [ebp-N+offset]` (or `[ebp-N]`).

**8. `ptr->field[i]` / `obj.field[i]` read** and **`&...[i]`**
(`generate_member_index`, 4834-4922): reject bitfield. `element_size,
is_pointer_field = _member_index_element_size(info)` (2473-2491). `emit_load(addr)`:
address_of → `lea acc, addr`; size 1 → `emit_byte_load_zx`; else `mov acc, addr`.
**Constant index** (4877-4895): base_reg = SI (if arrow & si_local) else BX via
`_emit_member_index_base`. Pointer field → `mov bx, ptr_addr`; `disp =
index*element_size`; `addr=[bx+disp]`. Inline → `total = field_offset +
index*element_size`; `addr=[base_reg+total]`. **Variable index** (4896-4922):
`generate_expression(index)`; scale (`shl acc,1`/`shl acc,2` for 2/4, `imul
acc,N` else); `push acc`; base into BX (arrow&si → `mov bx, si`, else
`_emit_member_index_base`); pointer field → `mov bx, [bx+field_offset]`; `pop
acc`; `add bx, acc`; pointer → `addr=[bx]`, inline → `addr=[bx+field_offset]`;
`emit_load`.

**9. `ptr->field[i] = v` / inline / pointer** (`generate_member_index_assign`,
4924-5006): mirror of #8 on store side. Constant index:
`generate_expression(expr)` first, then base, then store. Variable index:
`generate_expression(expr)`; `push acc`; `generate_expression(index)`; scale;
`push acc`; base; pointer → `mov bx,[bx+field_offset]`; `pop acc` (scaled index);
`add bx, acc`; `pop acc` (rhs); store. `emit_store`: size1 → `mov byte addr,al`;
size2&int4 → `mov word addr,ax`; else `mov addr,acc`.

**10. `ptr->field++` etc.** (`MemberIncrementDecrement` dispatch,
`emission.py:2874-2914`): synthesizes `MemberAssign(MemberAccess + delta)`, calls
`generate_member_assign`, then `generate_expression(MemberAccess)` to reload,
then postfix `sub/add acc, delta`. Statement-position (4328-4332) routes through
`generate_expression` and discards.

### Auxiliary paths that must keep working

- `_expression_type` (`generator.py:2046-2131`): `MemberAccess` branch
  (2074-2098) resolves the field type from `object_name`/`base_expr` for
  `sizeof`. `PlaceLoad` branch (2099-2117) only handles the struct-array
  `_match_struct_array_member` shape today. **After conversion, every
  member-read sizeof becomes a `PlaceLoad` and must resolve through the same
  field-type logic.**
- `_is_pure_expression` (`emission.py:1612-1644`): treats `MemberAddressOf`
  (1624), `MemberAccess`/`MemberIndex` (1634), `PlaceLoad` (1636) as pure.
  **`PlaceAddressOf` must be added.**
- AssignExpr inner chain (`emission.py:866-928`): `MemberAssign` (915-919, with
  bitfield rejection via `_member_assign_targets_bitfield` 1795-1819),
  `MemberIndexAssign` (920-921), `PlaceStore` (922-923). **The converted member
  stores become `PlaceStore`; the bitfield-as-expression rejection must be
  preserved inside the PlaceStore arm.**
- Synthetic struct-initializer assignments (`emission.py:793-801`): build
  `MemberAssign(arrow=False, object_name=name, ...)` per field. **These must be
  rewritten to emit `PlaceStore(MemberPlace(VariablePlace(name), field),
  value)`.**
- Dispatch arms: expression (`emission.py:2870-2918`) and statement
  (`emission.py:4325-4341`). `PlaceLoad`/`PlaceStore` arms already exist;
  **`PlaceAddressOf` and `PlaceIncDec` arms must be added** (defined in
  `ast_nodes.py` but NOT dispatched yet — verified by grep returning zero
  matches for `PlaceIncDec`/`PlaceAddressOf` in codegen).

---

## Design: extending PlaceAddress + _resolve_place

### New PlaceAddress shape

The member family needs three things the current shape lacks: a **register base**
(`ptr->field` puts the pointer in SI/BX), a **bitfield terminal** (load/store is
not a plain mov), and a way to mark **address-yielding** members distinctly from
the size hack. Extend `PlaceAddress` (kw_only, slots; preserve existing field
names and alphabetical order) with:

```python
@dataclass(frozen=True, kw_only=True, slots=True)
class PlaceAddress:
    base_is_register: bool = False   # const_base names a register (SI/BX), not a label/frame string
    bitfield: FieldInfo | None = None  # non-None => terminal is a bitfield; load/store via _emit_bitfield_*
    const_base: str
    element_size: int
    field_size: int
    indexed: bool
    offset: int
    yields_address: bool = False     # bare array/struct-value member => lea/mov-address instead of value load
```

Rationale:

- `base_is_register` lets `_build_address` keep producing `[si+12]` / `[bx]`
  (register bases need no special syntax, but the flag lets `_emit_place_*` know
  the global-label-arithmetic case (`mov acc, _g_obj+offset`) does NOT apply, and
  tells the address-of path whether to `lea` vs `mov`-immediate).
- `bitfield` carries the `FieldInfo` so `_emit_place_load`/`_emit_place_store`
  route to `_emit_bitfield_read` / `_emit_bitfield_write` /
  `_emit_bitfield_write_literal` / the const-fold peepholes.
- `yields_address` replaces the brittle `field_size != element_size` heuristic
  for the address-yielding case and additionally covers `is_struct_value` (which
  has `field_size == element_size` but still yields an address). The struct-array
  shapes keep working because for them `yields_address` is set from `field_size
  != element_size`.

**Important byte-exact note on the address-of/struct-value yield form:** the
legacy emits three distinct sequences for a yielded address — global label
immediate (`mov acc, _g_obj+offset`), local frame lea (`lea acc,
[ebp-N+offset]`), and register-base (`lea acc, [bx+offset]` OR `mov acc, bx` when
offset==0). The `mov acc, bx` (offset 0, register base) form differs from `lea
acc, [bx]`. Therefore the yield emit cannot be unified into a single `lea`;
`_emit_place_load` must branch on `base_is_register` + offset and on whether the
base is a global label.

Because these yield-address sequences and the pointer-field-index and
bitfield-const-fold sequences are genuinely heterogeneous, the plan
**special-cases inside `_emit_place_load`/`_emit_place_store` keyed on the
resolved `PlaceAddress` flags** rather than forcing a single uniform address
string. This is explicitly acceptable per the constraints, provided output is
byte-identical.

### Recursive `_resolve_place` cases

`_resolve_place` is extended to recognize, in this priority order (each returns a
`PlaceAddress`; the existing struct-array Shapes A/B are kept first so
already-converted output is untouched):

1. **Shape A / B** (existing): `arr[i].member` and `arr[i].member[j]` —
   unchanged.
2. **`MemberPlace(VariablePlace(name), field)` — dot scalar/array/struct-value**:
   resolve `base_operand` via `_resolve_struct_value_base`; `const_base =
   base_operand`, `base_is_register=False`, `indexed=False`, `offset=field_offset`.
   Set `yields_address = is_array_field or is_struct_value`, `bitfield = info if
   info.bit_width else None`.
3. **`MemberPlace(DereferencePlace(VariablePlace(name)), field)` — arrow**: emit
   the base into SI (no-op) or BX via the shared rule (`_load_member_base`).
   `const_base = register`, `base_is_register=True`, `offset=field_offset`, flags
   as above.
4. **`MemberPlace(DereferencePlace(<expr>), field)` — cast/general arrow**:
   reproduce `_generate_member_access_via_expr`'s base materialization. Fast
   path: `Cast(AddressOf(local))` → `const_base = _local_address(local)`,
   `base_is_register=False`. General: `generate_expression(expr)`; `mov bx, acc`;
   `const_base = bx`, `base_is_register=True`. Resolve `info` from
   `_expression_type(expr)`-derived tag.
5. **`MemberPlace(MemberPlace(...), field)` — chained `a->b.c`**: model as:
   `generate_expression(PlaceLoad(place.base))` (which lea's the intermediate
   struct address into AX), `mov bx, acc`, then resolve `info` for `field`
   against the intermediate's struct type, `const_base=bx`,
   `base_is_register=True`. This reproduces `_generate_member_assign_via_expr`
   byte-for-byte (base-first, `mov bx, acc`, then store). **Open verification
   item:** confirm during Task 8 whether `o->pin->a`'s inner `o->pin` must be
   wrapped as `PlaceLoad(MemberPlace(...))` inside the outer `DereferencePlace`
   (pointer load) vs a bare nested `MemberPlace` (struct-value address). The
   golden captured in Task 2 is the oracle that decides this.
6. **`SubscriptPlace(MemberPlace(...), index)` — inline-array OR pointer-field**:
   the most entangled. Resolve `info`, compute `element_size, is_pointer_field`.
   Reproduce `generate_member_index`'s constant-vs-variable-index and
   pointer-vs-inline branching. Because this path interleaves
   `generate_expression(index)`, `push`/`pop`, and base materialization in an
   order that does not fit the "resolve then emit" contract, **special-case it**:
   `_resolve_place` for this shape emits the full address computation into BX and
   returns a `PlaceAddress(const_base=bx, base_is_register=True, indexed=False,
   offset=<disp or 0>, element_size, field_size=element_size,
   yields_address=<address_of context>)`. The pointer-field index distinctly
   performs `mov bx, [base+field_offset]` (load the pointer) before adding the
   scaled index; the inline-array form keeps `field_offset` in the final
   displacement. This distinction lives entirely inside the `_resolve_place`
   SubscriptPlace arm.

### `_emit_place_load` / `_emit_place_store` branching

`_emit_place_load(place)`:

1. `ax_clear`; push BX if `_bx_holds_pinned_var()`.
2. `address = _resolve_place(place)`.
3. If `address.bitfield is not None`: build `addr`,
   `_emit_bitfield_read(address.bitfield, addr=addr)`, restore BX, return.
4. If `address.yields_address`: emit the global-label-immediate / local-lea /
   register `mov`-or-`lea` sequence (see byte-exact note), restore BX, return.
5. Else plain: `_emit_field_load`.

`_emit_place_store(place, value)`: mirror, but reproduce the rhs-before-base
ordering for arrow scalar stores and base-first for chained/via-expr. Because
`_resolve_place` for the arrow scalar case loads the base register *without*
evaluating any rhs, and the legacy emits `generate_expression(expr)` BEFORE
`_load_member_base`, the store path must: for the **dot-static** and
**register-base** scalar cases, evaluate rhs first (no push/pop — the legacy
dot/arrow scalar stores do NOT push), then resolve place, then store. For the
struct-array indexed shapes (Shapes A/B) the existing push/pop ordering is
retained. Dispatch inside `_emit_place_store` on the resolved/`Place` shape.

A `PlaceAddressOf(place)` dispatch (new) calls `_emit_place_address_of(place)`
that resolves the place's address and lea's/adds it (reproducing
`generate_member_address_of` + `generate_member_index` address_of).
`PlaceIncDec(place)` dispatch (new) reproduces the `MemberIncrementDecrement`
synthesize-store-reload-postfix pattern but over `PlaceStore`/`PlaceLoad`.

---

## Tasks

> Conventions (enforce in every diff): no abbreviations (spell out `index`,
> `expression`, `register`, `member`, `object`); methods/nodes sorted
> alphabetically within their group; dataclasses are `@dataclass(kw_only=True,
> slots=True)` (frozen where the original was); preserve all existing
> docstrings/comments. Use `self.target.bx_register` / `self.target.si_register`
> / `self.target.acc`, never literal `bx`/`eax`.

> Test commands (run from repo root
> `/home/ubuntu/bboeos/.claude/worktrees/parser`):
> - Golden: `python3 tests/test_cc_place.py` → expect `PASS  index_member golden byte-identical`
> - Place node units: `python3 -m pytest tests/unit/test_cc_place_nodes.py -q`
> - Full asm gate: `python3 tests/test_asm.py` → expect `49/49`
> - Programs (bbfs): `python3 tests/test_programs.py`
> - Programs (ext2): `python3 tests/test_programs.py --filesystem ext2`
> - Codegen regression: `python3 -m pytest tests/unit/test_cc_codegen.py -q`

### Task 1 — Branch and baseline

- [ ] Confirm on branch `bboe/cc-place-plan2`: `git rev-parse --abbrev-ref HEAD`.
- [ ] Capture baseline green: `python3 tests/test_cc_place.py` (expect `PASS`),
  `python3 tests/test_asm.py` (expect `49/49`), `python3 -m pytest
  tests/unit/test_cc_place_nodes.py tests/unit/test_cc_codegen.py -q`.
- [ ] Record baseline outputs in the PR description. Do NOT commit yet.

### Task 2 — Extend the golden fixture to cover EVERY member shape (capture BEFORE any conversion)

This is the Task-6.5-before-Task-7 ordering from Plan 1: prove the legacy output
is captured byte-exactly while the legacy code is still live, so the conversion
can be validated against it.

- [ ] Edit `tests/test_cc_place.py` `FIXTURE` (lines 27-60). Keep the existing
  five probe functions verbatim (struct-array shapes) and append new functions
  covering each member shape. Add structs and functions exercising: dot scalar
  read/store, dot array/struct-value yield, arrow scalar read/store, chained
  `o->pin->a` read/store, cast-base `->hi`, inline-array index var/const,
  pointer-field index read/store, word inline index, `&member`, `&member[i]`,
  bitfield read, bitfield general write, 1-bit-literal write, const-fold write,
  postfix `++`, prefix `--`, address-of struct-value member. Suggested fixture
  additions:

```c
struct inner { int a; char tag; };
struct outer { struct inner in; struct inner *pin; };
struct flags { int hi; unsigned char a : 1; unsigned char b : 3; unsigned char c : 4; };
struct buf { int n; char data[8]; char *p; unsigned short w[4]; };

struct outer g_outer;
struct flags g_flags;

int probe_dot_read(void) { return g_outer.in.a; }
int probe_dot_store(int v) { g_outer.in.a = v; return g_outer.in.a; }
int probe_arrow_read(struct buf *b) { return b->n; }
int probe_arrow_store(struct buf *b, int v) { b->n = v; return b->n; }
int probe_chain_read(struct outer *o) { return o->pin->a; }
int probe_chain_store(struct outer *o, int v) { o->pin->a = v; return o->pin->a; }
int probe_cast_base(unsigned char raw) { return ((struct flags *)&raw)->hi; }
int probe_inline_index_read(struct buf *b, int i) { return b->data[i]; }
int probe_inline_index_store(struct buf *b, int i, int v) { b->data[i] = v; return 0; }
int probe_inline_index_const(struct buf *b) { return b->data[3]; }
int probe_pointer_index_read(struct buf *b, int i) { return b->p[i]; }
int probe_pointer_index_store(struct buf *b, int i, int v) { b->p[i] = v; return 0; }
int probe_word_inline_index(struct buf *b, int i) { return b->w[i]; }
char *probe_member_addr(struct buf *b) { return &b->n; }
char *probe_member_elem_addr(struct buf *b, int i) { return &b->data[i]; }
int probe_bitfield_read(struct flags *f) { return f->b; }
int probe_bitfield_store(struct flags *f, int v) { f->b = v; return 0; }
int probe_bitfield_one_literal(struct flags *f) { f->a = 1; return 0; }
int probe_bitfield_constfold(void) { struct flags local; local.a = 0; local.b = 5; return local.b; }
int probe_member_incdec(struct buf *b) { int pre = b->n++; return pre + b->n; }
int probe_member_predec(struct buf *b) { return --b->n; }
int probe_addr_of_dot(void) { return (int)&g_outer.in; }
```

  If any constructed line does not currently compile under the **legacy** code,
  fix the C until it compiles — the legacy output is the oracle. Document any
  shape that legacy cannot compile (it must be dropped from scope or flagged).
- [ ] Update the module docstring (lines 2-11) to describe expanded coverage.
- [ ] Regenerate the golden against the LEGACY code: `BBOE_UPDATE_GOLDEN=1
  python3 tests/test_cc_place.py` → expect `WROTE golden ...`.
- [ ] Inspect the regenerated `tests/golden/cc_place_index_member.asm` by eye:
  confirm every new probe appears and the sequences match the Reconnaissance
  contracts. This golden is the byte-exact oracle for the rest of the plan.
- [ ] Verify: `python3 tests/test_cc_place.py` → `PASS`.
- [ ] **Commit:** `test(cc): expand Place golden fixture to cover the full
  Member* family (legacy oracle)`.

### Task 3 — Add Place-node construction unit tests for the member shapes

- [ ] Edit `tests/unit/test_cc_place_nodes.py`. Append constructor tests
  asserting the Place trees for arrow (`MemberPlace(DereferencePlace(Var),f)`),
  chained (`MemberPlace(MemberPlace(DereferencePlace(Var),b),c)`), and
  pointer-field index (`SubscriptPlace(MemberPlace(...),i)`) shapes, mirroring
  the existing style (lines 8-56).
- [ ] Run: `python3 -m pytest tests/unit/test_cc_place_nodes.py -q` → all pass.
- [ ] **Commit:** `test(cc): Place node construction tests for the Member*
  shapes`.

### Task 4 — Extend `PlaceAddress` and add resolution helpers (no behavior change)

- [ ] In `cc/codegen/x86/generator.py`, extend `PlaceAddress` (136-150) with the
  three new fields with defaults, alphabetical order, updated docstring. Confirm
  `FieldInfo` is importable (referenced at 2473).
- [ ] If `_load_member_base` is byte-identical to the needed arrow base rule,
  reuse it directly from the new `_resolve_place` arms (don't duplicate).
- [ ] Add a layout helper `_resolve_member_place_info(self, place, /) -> tuple[str,
  bool, FieldInfo]` dispatching on `place.base` (VariablePlace dot,
  DereferencePlace(VariablePlace) arrow, DereferencePlace(other) via-expr,
  MemberPlace chained). Reuse `_resolve_struct_value_base`,
  `_resolve_member_index_layout`, `_expression_type`. Copy the exact legacy
  `message = f"..."` error strings.
- [ ] Run `python3 tests/test_cc_place.py` (still PASS) and `python3
  tests/test_asm.py` (49/49).
- [ ] **Commit:** `feat(cc): extend PlaceAddress with
  register/bitfield/yields-address fields`.

### Task 5 — Extend `_resolve_place` + `_emit_place_*` to cover every member shape (pre-flip, verified against golden)

Validate by adding **temporary** node-level tests (in a new
`tests/unit/test_cc_place_member_codegen.py`) that construct each
`Place`/`PlaceLoad`/`PlaceStore`/`PlaceAddressOf`/`PlaceIncDec` tree by hand, run
them through the generator, and assert the emitted lines equal the corresponding
slice of the golden. This proves byte-exactness before the parser produces these
trees.

- [ ] Extend `_resolve_place` (2962-3008) with new arms AFTER Shape A/B and
  BEFORE the final `raise`, in the design's order (cases 2-6).
- [ ] Extend `_emit_place_load` (1756-1773): branch on `bitfield` →
  `_emit_bitfield_read`; `yields_address` → global-immediate / local-lea /
  register (`mov acc, base` when offset==0 and base_is_register, else `lea acc,
  [base+offset]`); else `_emit_field_load`. Keep the existing BX-protect; verify
  push/pop placement against the golden for the SubscriptPlace shapes (the legacy
  index generators own BX and do NOT push around their own use).
- [ ] Extend `_emit_place_store` (1775-1794) to reproduce the THREE legacy
  orderings (dot-static no-push; arrow register-base rhs-first; chained
  base-first) plus the struct-array Shapes A/B push/pop, plus the four bitfield
  paths (1-bit literal, arbitrary-width Int + known_local_byte const-fold,
  general RMW, arrow rhs-before-base). Dispatch on the resolved
  `PlaceAddress`/`Place` shape.
- [ ] Add `_emit_place_address_of(self, place, /)` reproducing
  `generate_member_address_of` AND the `MemberIndex(address_of=True)`
  element-address form.
- [ ] Add `_emit_place_increment_decrement(self, node, /)` reproducing the
  `MemberIncrementDecrement` lowering (synthesize
  `PlaceStore(place, BinaryOperation(PlaceLoad(place), op, Int(delta)))`, call
  store, `generate_expression(PlaceLoad(place))`, postfix `sub/add acc, delta`).
- [ ] Run the temporary tests; fix until each byte-matches the golden slice.
- [ ] Run `python3 tests/test_cc_place.py` (still PASS) and `python3
  tests/test_asm.py` (49/49).
- [ ] **Commit:** `feat(cc): _resolve_place + _emit_place_* cover the full
  Member* family`.

### Task 6 — Add dispatch arms for PlaceAddressOf and PlaceIncDec

- [ ] In `cc/codegen/x86/emission.py` expression dispatch (alphabetical:
  `PlaceAddressOf` before `PlaceIncDec` before `PlaceLoad`): add
  `_emit_place_address_of(expression.place)` and
  `_emit_place_increment_decrement(expression)` arms.
- [ ] In statement dispatch (near 4336), add a `PlaceIncDec` statement arm (route
  through `generate_expression`, then `ax_clear`). Confirm `PlaceStore` statement
  arm already exists.
- [ ] Add `PlaceAddressOf` to `_is_pure_expression`.
- [ ] Add `PlaceAddressOf`, `PlaceIncDec` to the imports (alphabetical).
- [ ] Run `python3 -m pytest tests/unit/test_cc_codegen.py -q` and `python3
  tests/test_asm.py` (49/49).
- [ ] **Commit:** `feat(cc): dispatch PlaceAddressOf and PlaceIncDec in codegen`.

### Task 7 — Extend `_expression_type` PlaceLoad branch for all member shapes

- [ ] In `generator.py:2099-2117`, generalize the `PlaceLoad` branch with a
  recursive `_place_type(self, place, /) -> str` resolving the declared type for
  `VariablePlace`/`SubscriptPlace`/`DereferencePlace`/`MemberPlace`. Preserve the
  struct-array behavior (`sizeof(arr[i].field)` → `mov eax, 4`).
- [ ] Verify `probe_sizeof` still emits `mov eax, 4`: `python3
  tests/test_cc_place.py` → PASS.
- [ ] **Commit:** `feat(cc): _expression_type resolves field type for every
  member Place`.

### Task 8 — Flip the parser construction sites

Convert each parser site (`cc/parser.py`) to emit Place trees:

- [ ] `MemberIndex` read (852-858) → `PlaceLoad(SubscriptPlace(MemberPlace(...),
  index))`.
- [ ] `MemberIncrementDecrement` postfix expression (864-871) →
  `PlaceIncDec(...)`.
- [ ] `MemberAccess` simple read (872-877) → `PlaceLoad(MemberPlace(...))`.
- [ ] `MemberAccess` chained read (883-889 loop) → nested `MemberPlace`; final
  `PlaceLoad`.
- [ ] `MemberAddressOf` (2311-2316) → `PlaceAddressOf(MemberPlace(...))`.
- [ ] `MemberIndex(address_of=True)` (2303-2310) →
  `PlaceAddressOf(SubscriptPlace(MemberPlace(...), index))`.
- [ ] `MemberAccess` cast-base (2340-2346) →
  `PlaceLoad(MemberPlace(DereferencePlace(cast), member))`.
- [ ] `MemberAssign` simple (1070-1077, 1094-1100, 1208-1214) →
  `PlaceStore(MemberPlace(...), expr)`.
- [ ] `MemberAssign` chained (1146-1153, 1060-1066 base chain) → nested
  `MemberPlace`; `PlaceStore`.
- [ ] `MemberAssign` compound (1186-1204) → `PlaceStore(place,
  BinaryOperation(PlaceLoad(place), op, rhs))`.
- [ ] `MemberIndexAssign` (1084-1091, 1173-1180) →
  `PlaceStore(SubscriptPlace(MemberPlace(...), index), expr)`.
- [ ] `MemberIncrementDecrement` statement (1158-1165) → `PlaceIncDec(...)`.

  Add a parser helper `_member_place(...)`. **Arrow chain note:** `o->pin->a` =
  `MemberPlace(DereferencePlace(<load of o->pin>), a)`; the inner `o->pin` as a
  *pointer value* is `PlaceLoad(MemberPlace(DereferencePlace(Var(o)), pin))`. So
  the outer DereferencePlace wraps a `PlaceLoad`, not a bare MemberPlace. Verify
  against the golden (`probe_chain_read` uses `o->pin->a`, arrow-then-arrow).
- [ ] Update parser imports (add the Place node names; keep legacy imports until
  Task 9).
- [ ] Run `python3 tests/test_cc_place.py` → **PASS, byte-identical**. If it
  differs, the diff names the exact line; investigate as a bug per the gate.
- [ ] Run `python3 -m pytest tests/unit/test_cc_codegen.py -q` and `python3
  tests/test_asm.py` (49/49).
- [ ] **Commit:** `refactor(cc): parser emits Place trees for the Member*
  family`.

### Task 9 — Delete legacy nodes, generators, and dispatch arms

- [ ] Delete the six `generate_member_*` methods and the two
  `_generate_member_*_via_expr` helpers. Keep helpers now called by
  `_resolve_place` (check with `git grep`).
- [ ] Delete the six legacy dispatch arms (expression + statement) and the
  `MemberAssign`/`MemberIndexAssign` AssignExpr arms. Move the bitfield-rejection
  into the `PlaceStore` arm via a `_place_targets_bitfield(self, place)`
  predicate.
- [ ] Delete `_member_assign_targets_bitfield` if unused.
- [ ] Rewrite the synthetic struct-initializer (793-801) to emit
  `PlaceStore(MemberPlace(VariablePlace(name), field), value)`.
- [ ] Remove `MemberAccess`/`MemberIndex`/`MemberAddressOf` from
  `_is_pure_expression` (covered by `PlaceLoad`/`PlaceAddressOf`).
- [ ] Remove the `MemberAccess` branch from `_expression_type` (superseded by
  Task 7).
- [ ] Delete the six legacy node classes from `ast_nodes.py` and remove imports
  everywhere (`git grep` of the six names returns zero hits in `cc/`).
- [ ] Run `python3 tests/test_cc_place.py` (PASS), `python3 -m pytest
  tests/unit/test_cc_place_nodes.py tests/unit/test_cc_codegen.py -q`, `python3
  tests/test_asm.py` (49/49).
- [ ] Remove the temporary Task-5 tests (subsumed) or keep a curated subset; note
  the choice in the commit.
- [ ] **Commit:** `refactor(cc): delete legacy Member* nodes, generators, and
  dispatch`.

### Task 10 — Final byte-exact gate

- [ ] `python3 tests/test_cc_place.py` → byte-identical PASS.
- [ ] `python3 -m pytest tests/unit/test_cc_place_nodes.py
  tests/unit/test_cc_codegen.py -q` → all pass.
- [ ] `python3 tests/test_asm.py` → `49/49`.
- [ ] `python3 tests/test_programs.py` (bbfs) → green.
- [ ] `python3 tests/test_programs.py --filesystem ext2` → green.
- [ ] `git grep -nE
  'Member(Access|Assign|AddressOf|Index|IndexAssign|IncrementDecrement)' cc/
  tests/` → zero in `.py` source.
- [ ] If any asm differs at any step: stop, treat as a bug, bisect against the
  legacy oracle golden — do NOT regenerate the golden to make it pass.
- [ ] Open ONE PR from `bboe/cc-place-plan2` with baseline-vs-final test outputs.

---

## Critical files

- `cc/codegen/x86/generator.py`
- `cc/codegen/x86/emission.py`
- `cc/parser.py`
- `cc/ast_nodes.py`
- `tests/test_cc_place.py`
