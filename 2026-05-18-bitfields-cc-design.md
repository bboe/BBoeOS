# 2026-05-18 — cc.py bitfields + type casts

Add bitfield struct members and type-cast expressions to cc.py, and
convert every bit-twiddly driver in the tree to use the new syntax.

## Motivation

Hardware-register access in BBoeOS today is manual mask-and-shift:

```c
uint8_t cr = 0;
cr |= 1 << 1;          // start
cr |= 2 << 6;          // page
cr &= ~(0x07 << 3);    // clear rd
kernel_outb(0x300, cr);
```

The bit positions live in `#define`s scattered across each driver, or
inline as magic numbers.  Bitfields turn this into a declarative
struct, and type casts let a byte read from a port be reinterpreted as
that struct without copying.

```c
struct ne2k_cr cr = { 0 };
cr.start = 1;
cr.page  = 2;
kernel_outb(NE2K_BASE, *(uint8_t *)&cr);
```

## Language additions

### Bitfield struct members

Syntax: `uint8_t name : N;` for a named bitfield, `uint8_t : N;` for
anonymous padding.

Constraints:

- Container type must be `uint8_t`.  Other types are a compile error.
  (`uint16_t` / `uint32_t` containers are deferred until a use case
  appears; nothing in the current tree needs them.  See "Out of
  scope".)
- Bit width `N` must be a positive integer literal in `1..8`.
- Unsigned only.  No signed bitfields.

### Layout rules

A struct's fields are emitted in declaration order.  A **bitfield run**
is a maximal sequence of consecutive bitfield members.  Each run:

- Starts at the next available byte offset (0 if first; otherwise after
  the prior member).
- Packs LSB-first: the first bitfield occupies bit 0, the next occupies
  the next-higher bits, etc.
- Total bits in a run must be `<= 8`.  Sum `> 8` is a compile error.
- Total bits in a run may be `< 8`; the unfilled high bits are
  unspecified storage (effectively anonymous padding).
- Anonymous bitfields (`uint8_t : N;`) contribute to the run width but
  cannot be read or written.

A regular (non-bitfield) field encountered after a bitfield run ends
the run, advances to the next byte boundary, and starts a fresh run
position counter.  Two runs separated by a regular field do not share
a byte.

`sizeof(struct foo)` is the sum of:

- 1 byte per bitfield run.
- The declared size of each non-bitfield member.

Field address-of (`&s.bitfield`) is a compile error, matching standard
C.  `sizeof(s.bitfield)` is also disallowed (would be ambiguous).

### Type-cast expressions

Syntax: `(T)expr`, where `T` is one of `uint8_t`, `uint16_t`,
`uint32_t`, `int`, `char`, `struct NAME`, or any of those followed by
one or more `*` pointer suffixes.

Semantics:

- Same-size value cast (`(uint8_t)int_expr`, `(int)uint8_t_expr`): the
  runtime value is unchanged.  When the result narrows during a memory
  store (e.g. `*(uint8_t *)p = (uint8_t)int_expr;`) cc.py uses the
  destination's width as it already does today for assignments.
- Pointer cast (`(uint8_t *)&cr`, `(struct ne2k_isr *)&raw`): the
  runtime value is the pointer unchanged.  Cast nodes are purely
  type-system bookkeeping for downstream member-access and deref
  codegen.
- No support for function-pointer casts, casts inside compound
  initializers, or `(T)&` in lvalue contexts beyond what already
  parses today.

Precedence: unary, same as the existing `&` / `*` / `-` ops.  The
parser disambiguates `(T)expr` from a parenthesized expression by
inspecting the first token after `(`: if it's a type keyword or
`struct`, it's a cast.

## Codegen

### Bitfield read

For `s.field` or `p->field` where `field : N` lies at bit offset `B`
in the byte at struct offset `O`:

```
mov  al, [base + O]
shr  al, B           ; omitted when B == 0
and  al, (1<<N) - 1  ; omitted when N == 8
```

Result is a `uint8_t` rvalue.

### Bitfield write

For `s.field = expr;` (or `p->field = expr;`) with the same shape:

```
mov  al, [base + O]
and  al, ~((((1<<N) - 1) << B)) & 0xff   ; clear the field's bits
; ... compute expr into bl ...
and  bl, (1<<N) - 1                       ; omitted when expr is a constant in-range
shl  bl, B                                ; omitted when B == 0
or   al, bl
mov  [base + O], al
```

Peephole specialisations:

- 1-bit field assigned literal `0`: collapses to
  `and [base+O], ~(1<<B)`.
- 1-bit field assigned literal `1`: collapses to
  `or  [base+O], (1<<B)`.
- N-bit field assigned literal `0`: skips the `or`; just clears.
- N-bit field assigned a literal that fits: skips the defensive mask
  on `bl`.

### Anonymous bitfields

Occupy run width.  Have no read/write codegen path.  The parser
records them as `field_name=None, bit_width=N`.

### Cast expressions

- Same-size casts: emit nothing.  Type information attached to the
  result node only.
- Pointer casts: emit nothing.  Type information attached to the
  result node only.  Downstream `*(T *)expr` or `(T *)expr->field`
  uses the cast's target type to pick the right load/store width and
  the right field offsets.
- Narrowing casts to narrower than register (`(uint8_t)int_expr`): no
  immediate truncation.  The destination store already narrows at
  write time; intermediate uses tolerate the high bits (matching
  existing behaviour for uint8_t locals).

## Driver conversions

Land alongside the cc.py feature in the same PR.  Each driver's
register structs go in `src/include/registers.h` (new), `#include`d
where used.

| Driver | File | Registers |
| ------ | ---- | --------- |
| PIC    | `src/drivers/pic.c` if present, else `entry.asm` / `syscall.asm` | IMR (`irq0..irq7`), ISR, IRR |
| NE2000 | `src/drivers/ne2k.c` | CR, ISR, IMR, RCR, TCR, DCR |
| FDC    | `src/drivers/fdc.c` | MSR, DOR |
| RTC    | `src/drivers/rtc.c` | Status Reg A, Status Reg B |
| DMA    | `src/drivers/fdc.c` (chan 2), `src/drivers/sb16.c` (chan 1) | Mode byte |
| SB16   | `src/drivers/sb16.c` | DSP status (`data_avail` + reserved) |
| PS/2   | `src/drivers/ps2.c` | Controller status |

For each register: replace `kernel_outb(port, (X << shift) | (Y <<
shift) | ...)` with a typed struct literal + `*(uint8_t *)&` bridge.
Replace `kernel_inb(port) & mask` reads with a struct-pointer cast on
the byte and field access.

Per the existing `archive-byte-parity` rule, after any driver `.c`
rewrite update the matching `archive/<name>.asm` if one exists to keep
`archive/README.md` size comparisons honest.

## Testing

### cc.py unit coverage

New file `tests/test_cc_bitfields.py` drives cc.py through:

- Single-bit named field at bit 0 (read + write codegen).
- Multi-bit named field at non-zero offset (read + write).
- Anonymous padding between named fields (offset advance).
- Two bitfield runs separated by a regular `uint8_t` field
  (`sizeof == 3`).
- 1-bit literal-0 / literal-1 peephole shapes.
- Negative: bit width > 8 → compile error.
- Negative: container type other than `uint8_t` → compile error.
- Negative: run sum > 8 → compile error.
- Negative: `&s.bitfield` → compile error.

Cast cases land in the same file or a sibling `tests/test_cc_casts.py`:

- `(uint8_t)int_expr` and `(int)uint8_t_expr` parse and emit no
  truncation instructions.
- `*(uint8_t *)&struct_var` reads the struct's first byte.
- `(struct foo *)&byte_var` followed by `->field` reads via the
  bitfield codegen path.

### Driver regression coverage

The existing test matrix exercises every converted driver:

- NE2000 → `test_programs.py` networking smoke tests (icmp, dns,
  ping).
- FDC → `test_bboefs.py` (every fs op rides FDC) and most of
  `test_programs.py`.
- PIC → any IRQ-driven test fails if IMR shape regresses (console
  input, FDC, NE2000 IRQs all hit IMR).
- RTC → `test_programs.py` covers `sleep`, `date`, `uptime`.
- PS/2 → console input on every test boot.
- SB16 → less coverage; manual QEMU smoke if the diff touches the
  audio path.

Per the `run_full_ci_matrix_locally` rule, run every suite in
`.github/workflows/test.yml` locally before opening the PR.

## Out of scope

- `uint16_t` / `uint32_t` bitfield containers.  Every status register
  in the tree is byte-sized; the two existing 16-bit port operations
  (`ata.c`, `ne2k.c` data ports) are bulk transfers, not bit-flag
  registers.
- Signed bitfields.  No use case; would require sign-extension on
  read.
- Zero-width separator `uint8_t : 0;`.  Redundant with `uint8_t : N;`
  anonymous padding in a uint8_t-only world.
- Designated initializers (`struct foo x = { .a = 1, .b = 2 };`).
  Users assign field-by-field after declaration.
- Function-pointer casts, cast in lvalue position beyond `*(T *)&x`.

## Risks

- **Parser ambiguity for casts:** `(name)expr` could be a cast or a
  parenthesised call/expression.  Mitigation: cc.py knows the type
  keyword set at parse time; first token after `(` must be a type
  keyword or `struct` for the cast branch to take.  No user-defined
  typedefs today, so no ambiguity with identifier-typed casts.
- **Bit-ordering surprise:** LSB-first matches x86 GCC convention.
  Documented in the codegen section.  Anyone writing a struct for a
  hardware spec that lists bits MSB-first must mentally invert the
  order; calling this out in `docs/syscalls.md` or wherever bitfields
  are documented avoids confusion.
- **Driver migration footprint:** the all-drivers-in-one-PR scope per
  the brainstorming answer is the biggest single-PR risk.  Mitigation:
  full local CI matrix per `run_full_ci_matrix_locally` and a
  per-driver commit sequence inside the PR so any regression is easy
  to bisect.
