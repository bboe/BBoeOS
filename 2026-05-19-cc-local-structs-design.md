# 2026-05-19 — cc.py stack-local struct values + designated init + bitfield constant folding

Add stack-local struct value declarations to cc.py, including arrays
of struct locals, dot-access read/write, address-of, sizeof, and
`{ 0 }` / designated-field initializer syntax.  Pair this with a
constant-folding peephole in the bitfield write helpers and a
last-write-wins collapse peephole so the resulting driver pattern
emits a single `mov byte [ebp-N], <imm>` per fully-literal register
setup instead of N read-modify-write sequences.

## Motivation

PR #428 (NE2000 + PIC IMR bitfield register structs) grew the kernel
by 864 bytes (~+2.1%) — almost entirely from NE2000 init/probe/send
code where `kernel_outb(port, 0x21)` (one `out` with a literal) became
six-to-eight instructions of read-modify-write through a struct
pointer.  The current pattern requires the struct to live somewhere
addressable, which (because cc.py doesn't allow dot-access on local
struct values) means a file-scope global plus a pointer cast at each
use site.  Every field write is then `mov al, [ebx]; and al, mask;
or al, bl; mov [ebx], al` instead of a constant-folded byte store.

Allowing the struct to live on the stack as a local enables both
clearer driver code and a peephole optimization that recovers most of
the size loss.  The peephole only fires when (a) the storage is a
direct stack-local byte slot and (b) every field write has a literal
rhs — exactly the hardware-register-init case.

## Language additions

### Local struct declarations

`struct foo c;` declares a stack-local struct value.  Frame allocation
uses the existing `allocate_local(name, size=struct_sizes[tag])` API.
`variable_types[name]` records `"struct foo"`.

`struct foo arr[N];` declares an array of struct locals.  Frame slot
size is `N * struct_sizes[tag]`.  `variable_types[name]` records
`"struct foo[N]"`.

### Dot-access, address-of, sizeof

The four sites in `cc/codegen/x86/generator.py` that currently bail
with "dot member access on local struct values is not yet supported"
(`:1012`, `:1144`, `:1169`, `:1274`) lose the `if object_name not in
self.global_scalars` guard.  Each becomes a dispatch:

- `name in global_scalars` → `_g_<name>` (existing path, unchanged).
- `name in locals` → `[ebp - locals[name]]` (new path).

For arrays, `arr[i].field` reuses the existing indexed-member emitter,
which currently bases off the global symbol.  It learns to base off
`[ebp - locals[arr] + i * struct_size]` when the array lives in the
frame.

`&local_struct` emits `lea {acc}, [ebp - locals[name]]`.
`&local_struct.field` for a regular (non-bitfield) field emits
`lea {acc}, [ebp - locals[name] + field.byte_offset]`.
`&local_struct.bitfield` keeps the existing
`cannot take address of bitfield` rejection.

`sizeof(local_struct)` reads `struct_sizes[tag]` exactly as it does
for global struct values.

### Designated initializers at declaration

Two forms:

```c
struct ne2k_cr c = { 0 };
struct ne2k_cr c = { .start = 1, .rd = 4, .page = 2 };
```

Semantics match C99 §6.7.8/19:

- Omitted fields are zero-initialized.
- Field order in the initializer does not matter; the field name picks
  the slot.
- Both bitfield and regular fields are valid initializer targets.

Codegen lowers the initializer to a zero-store of every byte of the
local's slot, followed by a normal field assignment per designated
entry.  The const-fold + collapse peepholes (below) collapse the
sequence when every right-hand-side is literal.

Parser changes:

- After `=` at a struct-typed declaration, accept `LBRACE ... RBRACE`.
- Inside the braces: either `NUMBER:0 RBRACE` (zero-init), or
  one-or-more `DOT IDENT EQUALS <assignment-expression>` separated by
  commas with optional trailing comma.
- AST: extend `VarDecl` with an optional `struct_init: dict[str, Node]
  | None` field.  Empty dict represents `{ 0 }`; populated dict
  represents designated entries.

## Out of scope

- Positional struct initializers (`{ a, b, c }` without designators).
- Mixed designated + positional initializers (`{ 0, .foo = X }`).
- Nested struct initializers and array-of-struct initializer lists.
- Struct-to-struct assignment after declaration (`c = other_c;`).
- Passing struct values by value into functions, or returning them.
- `memcpy` / `memset` builtin recognition on local structs.  Pointer
  indirection through `&local` still works for these.
- Constant folding across basic-block boundaries.
- Constant folding for stores wider than one byte.  All optimisations
  here target the byte-sized bitfield register idiom.

## Codegen — constant-fold peephole

A per-function `known_local_bytes: dict[int, int]` mapping
frame-offset → known byte value.  Reset on function entry.

Updates:

| Emitted shape | Effect on `known_local_bytes` |
|---|---|
| `mov byte [ebp-K], <imm>` | `known_local_bytes[K] = imm` |
| `or byte [ebp-K], <imm>` | if K is known, OR into it; else invalidate K |
| `and byte [ebp-K], <imm>` | if K is known, AND into it; else invalidate K |
| `mov byte [ebp-K], <reg>` | invalidate K |
| any indirect store (`mov ... [<reg>...]`) | invalidate all |
| function call | invalidate all |
| jump target / label boundary | invalidate all |

Fold sites:

- `_emit_bitfield_write_literal(info, addr, value)`: if `addr` resolves
  to a `known_local_bytes` slot K, compute
  `new = (known_local_bytes[K] & clear_mask) | (value << bit_offset)`
  and emit `mov byte [ebp-K], <new>`.  Otherwise emit the existing
  one-instruction `and byte` / `or byte` peephole.

- `_emit_bitfield_write(info, addr)` (general path): only fold if the
  rhs was just emitted as `mov eax, <literal>`.  Reuse the existing
  `ax_value` tracking if present; otherwise add an `ax_literal: int |
  None` shadow set by `mov eax, <imm>` and cleared by anything else.

For struct-typed locals, the const-fold tracker is initialised on the
emit of the `= { 0 }` zero-store prelude.  All later designated-init
writes feed straight into the fold path.

## Codegen — last-write-wins collapse peephole

When the emit pipeline is about to add a `mov byte [ebp-K], <imm>` and
the most recently emitted instruction is also `mov byte [ebp-K],
<other_imm>` for the same address, replace the previous instruction
rather than appending.  The combined effect with the const-fold pass
is:

- Designated-init zero-store: emits `mov byte [ebp-N], 0`.
- First `.field = literal`: fold computes new byte, collapse replaces.
- Each subsequent `.field = literal`: same.

End state: exactly one `mov byte [ebp-N], <folded_byte>` per
bitfield-struct local that's only initialised by literals.

Implementation: an `_last_emitted_byte_store: tuple[int, int] | None`
shadow holding `(K, imm)` of the most recent qualifying emit, plus a
short conditional in `emit()` that replaces the prior line when the
shadow matches and the new emit qualifies.  Cleared on any non-byte-
store emit.

## Worked example

```c
struct ne2k_cr c = { .start = 1, .rd = 4, .page = 2 };
kernel_outb(0x300, *(uint8_t *)&c);
```

Emit walk:

1. Zero-init prelude: `mov byte [ebp-N], 0`.
   `known_local_bytes[N] = 0`; last-store shadow records the line.
2. `.start = 1` (1-bit field, bit 1): fold to `0 | (1<<1) = 2`.
   Collapse replaces the prior line.  `known_local_bytes[N] = 2`.
3. `.rd = 4` (3-bit field, bits 2..4): fold to
   `2 | (4<<2) = 0x12`.  Collapse replaces.
   `known_local_bytes[N] = 0x12`.
4. `.page = 2` (2-bit field, bits 5..6): fold to
   `0x12 | (2<<5) = 0x52`.  Collapse replaces.
   `known_local_bytes[N] = 0x52`.
5. `*(uint8_t *)&c`: emit `mov al, [ebp-N]`.  Last-store shadow
   cleared.
6. `kernel_outb`: normal port-out sequence.

Final body for the init + bridge:

```
mov byte [ebp-N], 0x52
mov al, [ebp-N]
```

Two instructions instead of ~30.

## Testing

### `tests/test_cc_local_structs.py` (new)

Positive cases — compile then grep the emitted asm:

- Bitfield struct local: dot-write a field, byte-bridge through
  `*(uint8_t *)&c`.  Assert the byte read uses `[ebp-N]` direct
  addressing.
- Regular (non-bitfield) struct local: dot-write a regular int field,
  read it back.  Assert offset arithmetic and the right load width.
- Array of struct locals: write `arr[2].field` and read it back.
  Assert indexed addressing off the frame pointer with `2 *
  struct_size` offset.
- `&local_struct` and `&local_struct.field` (regular field) emit
  `lea` against the frame.
- `sizeof(local_struct)` matches `struct_sizes[tag]`.

Initializer cases:

- `struct foo c = { 0 };` emits exactly N zero-stores covering every
  byte of the struct's slot.
- `struct foo c = { .field = X };` for a single bitfield: post-collapse,
  exactly one `mov byte [ebp-N], <folded>`.
- Multi-field designated init: post-collapse, still exactly one
  `mov byte [ebp-N], <folded>` per byte of the struct.

Negative cases — compile expected to fail with specific message:

- Positional init `struct foo c = { 1 };` (not designated, not `{0}`)
  → "positional struct initializers not supported".
- Mixed init `struct foo c = { 0, .x = 1 };`
  → "cannot mix positional and designated initializers".
- `&local.bitfield` still hits the existing
  `cannot take address of bitfield`.

Regression matrix (must stay green):
`tests/test_cc_bitfields.py`, `tests/test_cc_casts.py`,
`tests/test_cc_compatibility.py`, `tests/test_cc_bits.py`,
`tests/test_asm.py`, `tests/test_bboefs.py`,
`tests/test_programs.py`.

### Kernel-size verification

Once this lands and the NE2000 driver is rewritten to use the new
stack-local pattern (separate PR or follow-up commit on the same
branch), rebuild `kernel.bin` and compare against the post-PR-#428
baseline (42,072 bytes).  Expectation: shrink by most of the 864-byte
NE2000 growth.  Document the actual delta in the PR description.

## Risks

- **Alias invalidation too narrow.**  If the folder thinks a byte is
  still a known constant after the user has written to it through a
  pointer that aliased the local, codegen will emit a wrong byte.
  Mitigation: invalidate all of `known_local_bytes` on any indirect
  store, function call, or jump-target boundary.  False negatives
  (the peephole sometimes doesn't fire when it could) are fine;
  false positives would silently miscompile drivers.
- **Designated-init parser ambiguity.**  `{ .field = expr }` where
  `expr` contains a comma at the top level would tangle with the
  initializer's comma separator.  Mitigation: parse the rhs with
  `parse_assignment_expression`, not anything that accepts
  comma-expressions.  cc.py doesn't currently support comma-expressions
  outside `for(;;)` so this is largely academic.
- **`{ 0 }` vs positional `{ 0 }` ambiguity.**  Disambiguate at parse
  time: if the brace-list is exactly the token sequence
  `LBRACE NUMBER:"0" RBRACE`, it's the zero-init shorthand; otherwise
  expect `DOT IDENT EQUALS ...`.
- **`&local_struct` leaks a pointer to a stack-local.**  Once allowed,
  callers can pass `&c` to functions that expect `struct foo *` and
  the callee can read/write through it just fine — but the local goes
  out of scope at function return.  This is standard C; the risk is
  no different than `int x; foo(&x);`.  Document as a normal
  consideration; no special enforcement needed.
- **Frame-size growth.**  Allocating struct locals enlarges
  `frame_size`.  The existing accounting handles it; ensure
  `allocate_local` is called with the struct's full byte size, not
  just the int default.
- **`= { 0 }` for large structs emits N zero-stores per declaration.**
  For a 64-byte struct that's 64 `mov byte` instructions.  Acceptable
  because (a) the driver use case is byte-sized structs and (b) larger
  zero-inits could later be optimised into a `rep stosb` loop in a
  follow-up.

## Spec-related work that this enables

- Cleaner NE2000 driver: a follow-up commit can rewrite ne2k.c to use
  stack-local `struct ne2k_cr c = { .stop = 1, .page = 1 };` etc.,
  recovering most of the kernel-size growth introduced in PR #428.
- Subsequent Phase 3 driver conversions (FDC, RTC, DMA mode, SB16,
  PS/2) start from the cleaner pattern with no retroactive rewrites.
