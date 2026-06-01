# cc.py `rep`-string Loop Recognition

## Summary

Teach cc.py to recognize hand-written element-wise init/copy loops and
rewrite them into `rep stos` / `rep movs` string operations. A loop like

```c
for (i = 0; i < n; i++) dst[i] = 0;        /* -> rep stos{b,w,d} */
for (i = 0; i < n; i++) dst[i] = src[i];   /* -> rep movs{b,w,d} */
```

collapses from a per-element branchy body into a single string
instruction. Element widths 1, 2, and 4 bytes are all supported (the
array's element type selects `b` / `w` / `d`).

This is item #3 of the historical assembler-extension backlog. The
assembler (`asm.c`) and the codegen builtins (`memcpy` -> `rep movsb`,
`memset` -> `rep stosb`, `memcmp` -> `repe cmpsb`, `strlen` ->
`repne scasb`) already emit `rep`-string forms; the missing piece is
recognizing the *hand-written loop* idiom rather than an explicit
`memcpy` / `memset` call.

## Motivation and scope

Direct payoff is modest — most bulk copies in the tree already call the
builtins, and the hand-rolled loops that exist are mostly non-rep-able
(reversals, overlapping gap-buffer moves, table lookups). The decision
to build it anyway is a long-term-architecture one: loop optimization is
an active growth area in cc.py (LICM, GVN, and strength reduction all
landed recently at the IR/SSA level), and idiom recognition belongs with
those passes. The induction-variable analysis it needs already exists.

## Architecture: full IR/SSA recognition

cc.py's codegen is IR-driven: `For` / `While` / `DoWhile` are lowered by
the TAC IR builder (`cc/ir.py`) into flat IR — an init prologue, a header
`Label`, a `BranchFalse(left, operation, right, target)` exit test, the
body (`Index` / `IndexAssign` / `Copy` / ...), a step block, and a
back-`Jump`. The natural-loop machinery in `cc/loops.py`
(`natural_loops`, `insert_preheaders`, `_find_induction_variables`)
already runs over exactly these loops for LICM and strength reduction.

Recognition therefore happens as a new IR pass over natural loops, *not*
as an AST pattern match. Doing it in the IR layer means:

- One matcher covers `for`, `while`, and `do/while` uniformly — they all
  funnel through `_build_cond_false` -> `BranchFalse`.
- It reuses `_find_induction_variables` (the IV + step analysis built for
  strength reduction) instead of re-deriving induction variables.
- It lives where the rest of the loop optimizations live and composes
  with them.

The AST-level `generate_for` fallback path (used only when a function
body is not lowered to IR) does not get the optimization. That path is
rare; the loops simply stay scalar. This is an accepted limitation.

### Pass placement and ordering

A new `recognize_string_loops(body)` function in `cc/loops.py`, modeled
on the existing `reduce_loop_strength` driver: build the CFG, find
natural loops, match each, and rewrite the flat instruction list
(excise the loop region, splice in the preheader setup + the new
intrinsic).

It is invoked from `Optimizer.optimize` in `cc/ir_optimize.py`
**before** `reduce_loop_strength` and `hoist_loop_invariants`. Ordering
matters: the matcher must see the clean `dst[i] = src[i]` body before
strength reduction rewrites the index into a pointer-walk accumulator,
and before LICM hoists the base-address loads out of the body.

## The matcher

A natural loop qualifies only when **all** of the following hold;
anything that fails leaves the loop untouched (it lowers as a normal
loop):

1. **One induction variable, unit step.** `_find_induction_variables`
   returns exactly `{i: +1}`, and `i` is initialized to `0` before the
   loop. (Start/step are fixed at `0` / `+1` for the first cut; a later
   generalization can handle start `s` / step `k`.)
2. **Header test shape.** The header terminator is
   `BranchFalse(left=i, operation, right=n, target=end)` with
   `operation` in `{<, <=, !=}`, and `n` loop-invariant (a name defined
   outside the loop, or a literal).
3. **Body is exactly one recognized shape:**
   - *Fill:* a single `IndexAssign(base=D, index=i, source=V)` where `V`
     is loop-invariant (literal or invariant name).
   - *Copy:* `Index(destination=t, base=S, index=i)` immediately
     followed by `IndexAssign(base=D, index=i, source=t)`, where `t` is a
     temp used nowhere else, and `S` / `D` have the same element size.
4. **Element size E in {1, 2, 4}**, taken from the base variable's
   element type. Copy additionally requires `S` and `D` to be equal
   width.
5. **No other body instructions** — no extra stores, calls, or
   side-effecting operations. The IV step `i = i + 1`, the
   `LoopBoundary` push/pop markers, and the back-`Jump` are expected
   scaffolding, not "other" body.
6. **`i` is not address-taken.**

## Trip count and the signed-count guard

The iteration count goes in `ECX`. For start `0`: `<` gives `n`, `<=`
gives `n + 1`, `!=` gives `n`.

The critical correctness subtlety: a hand-written **signed**
`for (i = 0; i < n; i++)` runs `max(0, n)` times, but `rep` reads `ECX`
as **unsigned** — so a negative `n` would run ~4 billion iterations and
corrupt memory. The rewrite therefore guards by the counter's
signedness:

- `n` is a known non-negative literal -> no guard.
- `n` is signed -> emit
  `mov ecx, <n>; test ecx, ecx; jle .skip; cld; rep ...; .skip:`.
- `n` is unsigned -> no guard (`rep` with `ECX = 0` is a clean no-op,
  matching C's zero-iteration case).

This guard is the single place a naive implementation silently corrupts
memory, and it gets dedicated tests.

## New IR node: `RepString`

```
RepString(
    operation,        # "fill" | "copy"
    element_size,     # 1 | 2 | 4
    dest,             # base name (pointer / array)
    source,           # base name for copy; None for fill
    count,            # Value: the iteration count n
    fill_value,       # Value for fill; None for copy
    counter_signed,   # bool: drives the §guard
    final_iv,         # (iv_name, post_loop_value) | None
)
```

`RepString` is side-effecting: it must be exempt from dead-code
elimination, and `_instruction_value_operands` must surface `count` /
`fill_value` (and the base names) so the optimizer keeps them live.

### Codegen

Dispatched alongside `ir.IndexAssign`. Emits: load `EDI = dest`
(`ESI = source` for copy, `EAX = fill_value` for fill), `ECX = count`,
the signedness guard, `cld`, then `rep movs{b,w,d}` / `rep stos{b,w,d}`.

The existing `memcpy` / `memset` builtins already emit exactly this
register-setup + `cld` + `rep` shape, so a shared
`_emit_rep_move` / `_emit_rep_fill` helper is factored out and used by
both the builtins and `RepString` codegen — one emission path, no
duplication.

`RepString` clobbers `EDI`, `ESI`, `ECX`, `EAX`, the flags, and `DF`
(left clear by `cld`). It is registered with the same clobber pre-pass
that calls use (`_clobbers_for_call`), so any pinned-register variable
live across it spills and reloads correctly.

`final_iv` materializes the IV's post-loop value (`mov [i], <value>`)
only when `i` is read after the loop, preserving programs that use the
counter's terminal value.

## Assembler support: `movsd` / `stosd`

The assembler already encodes the byte and word string ops byte-for-byte
identically to NASM (`movsb`/`stosb` -> `A4`/`AA`; `movsw`/`stosw` ->
`66 A5`/`66 AB` in 32-bit mode via `emit_operand_size_prefix(16)`). It
has **no** `movsd` / `stosd` mnemonics, and in 32-bit mode the bare `A5`
/ `AB` (the 4-byte form a dword loop needs) is otherwise unreachable
because `movsw` always forces the `0x66` prefix.

Add two handlers, mirroring `handle_movsw`'s structure so they are
byte-identical to NASM in both modes:

```c
void handle_movsd() { emit_operand_size_prefix(32); emit_byte(0xA5); }
void handle_stosd() { emit_operand_size_prefix(32); emit_byte(0xAB); }
```

`emit_operand_size_prefix(32)` emits `0x66` under `bits 16` and nothing
under `bits 32` — the exact inverse of the existing `...w` handlers.
Add `STR_MOVSD` / `STR_STOSD` constants and dispatch-table entries in
sorted position, in **both** `user/programs/asm.c` and the self-host
reference `user/static/asm.c`. No `lodsd` / `cmpsd` / `scasd` — nothing
emits them (YAGNI).

## Testing

- **`tests/unit/test_cc_codegen.py`** — fill and copy loops at each width
  emit the correct `rep`; the signed guard is present for signed `n`,
  absent for unsigned and literal `n`; non-matching loops (non-unit
  stride, extra body statement, index that is not the IV, mismatched
  element widths, address-taken counter) stay scalar.
- **`tests/unit/test_cc_loops.py`** — the `recognize_string_loops` pass
  in isolation over hand-built IR.
- **`tests/test_asm.py`** — a new `user/static/rep_movsd.asm` smoke test
  exercises `movsd` / `stosd` / `rep movsd` / `rep stosd`, diffed
  byte-for-byte against NASM.
- **`tests/test_programs.py`** — a program that fills and copies `char` /
  `short` / `int` arrays and prints a checksum: the real runtime check in
  QEMU, especially the signed-guard and forward-overlap cases.
- Run the full CI matrix (`.github/workflows/test.yml`) before declaring
  done — this touches both codegen and the IR pipeline.

## Out of scope (future generalizations)

- Non-zero start / non-unit step induction variables.
- Pointer-walking idioms (`while (n--) *d++ = *s++;`) — a different IR
  shape; recognizable later by the same pass.
- Recognition on the AST `generate_for` fallback path.
- `rep`-izing aggregate struct copies (`struct S a = b;`).
</content>
