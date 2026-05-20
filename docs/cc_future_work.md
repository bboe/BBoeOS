---
title: cc.py future work
nav_order: 55
---

# cc.py future work

A running wishlist of cc.py improvements and optimizations.  Nothing here is
committed work — items are recorded because "it would be cool if..." captures a
real limitation we hit, and writing it down makes future investigations cheaper
than rediscovering the same trade-off.

Items are grouped by subsystem, then roughly by size of the change. Small items
are concrete fixes for limitations we've already hit; larger items are
speculative redesigns.

## Register allocation

### Complex-expression cycle break in the parallel-move scheduler

**Size:** small.

`_emit_builtin_arg_moves` (and `_emit_register_arg_moves` for user calls) breaks
cycles by spilling a *simple Var* arg to AX and re-loading it from AX when
emitted.  If the only candidate inside a cycle is a `BinaryOperation` (or any
non-`Var` shape), both paths give up and raise.  No real-world syscall has
tripped this yet, but it's a latent foot-gun.

The fix: evaluate the complex expression into a local frame slot (or push/pop
it), then continue scheduling with the slot as the new source. Mechanical
extension of the existing logic — adds a temp slot pool but no new architecture.

### Call-site-aware pin hints

**Size:** medium.

Today's auto-pin allocator
([`_select_auto_pin_candidates`](https://github.com/bboe/BBoeOS/blob/main/cc/codegen/x86/generator.py))
ranks locals by reference count and zips the ranked list with registers sorted
by clobber cost.  It doesn't know which call sites will consume the value or
which register that consumer wants.  Two local heuristics would cover most
observed cycle cases without a full redesign:

- **Move-coalescing hint.** If a value's primary use is as a specific syscall
  arg (e.g. `fd` always feeds `read(fd, ...)`), bias the allocator toward the
  consumer's target register (here, BX).  When successful, no move is needed at
  the call site.
- **Anti-pin hint.** If a value is referenced *inside* a syscall arg's
  sub-expression at a call site that clobbers register R, bias the allocator
  *against* pinning that value to R.  This avoids the setup-time conflict the
  parallel-move scheduler currently has to resolve.

Each hint is ~20 lines on top of the existing rank-based selector. Together they
would eliminate most cycles before the scheduler ever sees them; the scheduler
would remain as the correctness backstop.

### Graph-coloring register allocator

**Size:** large.  Intentionally deferred — see the trade-off discussion below.

A proper Chaitin/Briggs-style allocator with pre-coloring for calling-convention
constraints would eliminate essentially all move-scheduling cycles by
construction.  Steps:

1. Build the **interference graph** from live-range analysis (which cc.py
   doesn't currently have — values that are simultaneously live become edges).
2. Pre-color nodes that must occupy a specific register at some program point
   (the read builtin's `fd` argument must be in BX, etc.) so the constraint
   enters the graph directly.
3. Color the graph with N colors = N available registers, spilling the
   highest-cost node to memory if N-coloring fails, and retry.
4. Coalesce moves: when a `mov reg_a, reg_b` exists and the endpoints don't
   interfere, merge their nodes — the move dissolves.

Real backends emit one or zero instructions for many syscall arg setups under
this scheme.

**Why it's deferred:** cc.py's design stance is "simple allocator +
parallel-move scheduler at call sites."  Graph coloring is a different
architecture, not a refinement — it doubles the compiler's size and requires
live-range analysis cc.py doesn't have.  We've seen exactly one cycle in the
entire userland corpus.  The cost/benefit isn't there unless cycles become
chronic or we add live-range analysis for some other reason (e.g.
register-allocated `unsigned long`).

If we ever go this direction, the move-coalescing pin hint above is the natural
precursor — it's effectively a one-pass approximation of the same idea.

## Pointer support

### Pointer-to-pointer for arbitrary struct types

**Size:** small.

The pointer classifier covers `char**`, `uint8_t**`, and `int**` but not `struct
foo**` or other pointer-to-pointer types we may grow.  Each new shape needs a
tuple-literal addition in `_type_of_operand` until we replace the hardcoded set
with a "ends in `**`" check.

### BinaryOperation type inference beyond `+` / `-`

**Size:** small.

`_type_of_operand` propagates pointer type through `+` and `-` so `ptr + n`
classifies as pointer.  Other arithmetic (`*`, `/`, `%`, `<<`, `>>`, `&`, `|`,
`^`) always returns integer.  That's correct for the common cases — multiplying
two pointers isn't meaningful — but `(ptr & ~mask)` (pointer alignment) and
`(ptr | tag)` (tagged pointers) classify as integer and so won't compare against
another pointer.  Worth fixing when a real caller hits it.

## Address-of pinned scalars

### On-demand spill instead of pin disqualification

**Size:** medium.

Today an `&local` anywhere in a function disqualifies *local* from auto-pin —
the value lives in a frame slot for the entire function lifetime so
`_local_address` can hand back a real pointer.  Hot loops where the only `&` is
at a single boundary pay register-allocation cost they don't need to.

Spill-on-demand would keep the value pinned in a register and only materialize
it into a frame slot at the `&` site: emit `mov [slot], reg` just before handing
out `&slot`, then reload `mov reg, [slot]` after any operation that might have
written through the pointer.  The hot path keeps the pin savings; only the `&`
boundaries (and any subsequent calls that could touch the slot) pay memory cost.

The load-bearing part is the analysis: "which calls could have written through
this pointer" is easy to get subtly wrong, and disqualification is trivially
correct.  Worth revisiting when a real workload's hot path needs both a pin and
an `&`.

## Multi-translation-unit linkage

### Complex-arg call-site scheduler

**Size:** medium.

The aggressive form of this work — letting any call site with `arr[i]`, `s->f`,
`*p`, or another `Call` use the implicit register-passing default — is still
open. What landed instead are two narrower relaxations that cover every
production call site:

- `_is_simple_arg` admits `BinaryOperation(+ - | & ^, leaf, leaf)` plus shifts
  with an Int RHS (all AX-only lowerings — `* / %` and shifts with a Var RHS
  still touch EDX/CL and stay rejected).
- `has_complex_call` only triggers when the call has more than one argument:
  single-arg fastcalls already route through `emit_register_from_argument`,
  which handles arbitrary expressions through AX without any other arg to
  clobber.

The deferred piece is the multi-arg complex case — `f(arr[i], s->field)` still
falls back to cdecl.  Closing it requires extending
`cc/codegen/x86/generator.py:_emit_register_arg_moves` to evaluate complex args
into AX, push, run the simple-arg topological pass, and pop into target
registers at the end.  No current `.c` source needs it; mostly relevant for
future programs that pass struct fields / dereferences as multiple args.

## Language / C subset

Each item below is a feature `user/libbboeos/*.c` uses today that cc.py rejects.
Closing this section would let `user/libbboeos/` sources compile under cc.py
directly (per-program inlining, no linker), without the parallel-rewrite tax of
maintaining a header-only mirror.

### Vertical tab / form feed escape sequences

**Size:** trivial.

`CHARACTER_ESCAPES` in `cc/tokens.py` covers `\b`, `\e`, `\n`, `\r`, `\t`, `\0`,
`\\`, `\"`, `\'`, plus `\xNN` hex.  `\v` (vertical tab, 0x0B) and `\f` (form
feed, 0x0C) are missing, so `<ctype.h>`'s `isspace` deliberately drops them. Add
two entries to the table.

### `unsigned` shorthand on built-in integer types

**Size:** small.

Today only `unsigned long` is in the type table; `unsigned int`, `unsigned
char`, and `unsigned short` parse as `expected type, got IDENT ('unsigned')`.
The cleanest fix is to treat `unsigned` as a modifier that the type parser
consumes ahead of `char`/`short`/`int`/`long`, then dispatches to the existing
`uint8_t`/`uint16_t`/`uint32_t`/`unsigned long` slots.  No new codegen — the
type names already exist; only the spelling changes.

Lands as a prerequisite for compiling any libc-style source that uses the
standard short names instead of the fixed-width aliases.

### `++` / `--` operators

**Size:** small.

Both prefix and postfix forms are unsupported; `x += 1` and `x -= 1` are the
idioms.  `user/libbboeos/string.c` and `stdlib.c` lean on `*p++ = *s++` and
`n--`-style loops on every page.  The implementation choice is whether to
desugar in the parser (turn `p++` into an explicit temp + `p += 1` + read of the
temp) or carry a dedicated AST node and lower in codegen.  Parser-level desugar
is simpler and matches how the rest of cc.py's value/effect ordering is already
structured.

The postfix-vs-prefix subtlety (`*p++` reads `*p` then increments, `*++p`
increments first then reads) survives the desugar as long as the temp is
captured before the increment runs.

### `static` storage class

**Size:** small.

`static` on file-scope arrays (`static char buf[N];`), file-scope scalars
(`static int counter = 0;`), and functions (`static int helper(...)`) all reject
with `expected type, got IDENT ('static')`.  Plain forms work identically at the
codegen level (file-scope declarations land in BSS, functions get the `_name`
label either way), so the parser's storage-class table just needs to accept and
ignore `static` — matching how it already accepts and ignores `const` and
`volatile`.

Saves explaining the limitation to each new author and is required for the
file-static helpers that libc uses heavily.

### `for` loops

**Size:** small.

cc.py is while-only; `for` parses as `expected type, got IDENT ('for')`.  Any
real C source uses `for` constantly, including most of `user/libbboeos/`.

The desugaring is mechanical: `for (init; cond; step) body` becomes `{ init;
while (cond) { body; step; } }`.  The only subtlety is that `continue` must
reach the step expression before re-evaluating the condition, so the loop body
needs a synthetic label the parser hands to its `continue` handler — same shape
as the existing while support but with an extra step hook.

`break` and `continue` inside `for` work identically once the desugar is hooked
into the same loop-context stack while currently uses.

### `typedef`

**Size:** medium.

cc.py doesn't recognise `typedef` as a keyword, so `typedef struct foo foo;` or
`typedef unsigned int size_t;` reject as an identifier.  Adding it means
threading a "type alias" table through the type parser so a `foo` identifier
resolves to its underlying type at every declaration / cast / sizeof site.

A simple aliasing table is enough; cc.py doesn't need full C typedef semantics
(no anonymous structs to name, no incomplete-type forward decls yet).  Two cases
to cover:

- Type aliases (`typedef unsigned int size_t;`) — substitute the alias name for
  its target everywhere a type name is expected.
- Function-pointer typedefs (`typedef void (*sighandler_t)(int);`) — slot into
  cc.py's existing function-pointer machinery without storage emission.

Once typedef works, `size_t`/`ptrdiff_t`/`uintptr_t` from `<stddef.h>` parse
correctly and most of `user/libbboeos/*.c` becomes type-checkable.

### Variadic functions / `va_list` / `va_arg`

**Size:** large.

Today `printf` is a cc.py builtin with custom codegen (the variadic call site
lowers to a known `FUNCTION_PRINTF` jump through the libbboeos).  No user-defined
variadic function compiles — `int my_log(const char *fmt, ...)` rejects in the
parser, and there's no machinery for `va_start` / `va_arg` / `va_end`.

For libc, the cost shows up in `stdio.c` (~316 lines) where `printf`, `vprintf`,
`snprintf`, `vsnprintf`, etc. all assume `va_list` is real. Lowering needs:

- Parser: accept `...` in the parameter list and the stdarg.h macros.
- Codegen: `va_list` is a pointer into the caller's stack frame above the named
  args (Linux SysV i386 ABI).  `va_arg(ap, T)` reads `*(T*)ap; ap += sizeof(T)`
  (with alignment quirks for types larger than `int`).
- Calling convention: variadic callees can't share the implicit register default
  / pinned-register path — the unnamed args have to live on the stack so
  `va_list` can walk them.  Either gate the existing register-passing
  optimisations on "not variadic", or accept that variadic functions pay full
  cdecl.

The benefit is symmetric: every libc function that today exists only as a cc.py
builtin (printf, putchar, getchar, …) could move out to a header body or
out-of-line in `stdio.c`, shrinking `cc/codegen/x86/builtins.py`. Worth doing
once the rest of the C-subset gaps above are closed and a real workload needs
user-defined variadics.

## How to add to this list

When you hit a cc.py limitation and decide not to fix it now:

1. If it's something you'll want to *recall* the next time you hit it (a known
   sharp edge with a workaround), add a memory entry under
   `~/.claude/projects/-home-ubuntu-bboeos/memory/` and link to it from
   `MEMORY.md`.
2. If it's something you'd want to *consider for future work* (an improvement,
   an optimization, a redesign) add an entry here. Memory is for "remember to
   apply when relevant"; this file is for "browse when planning."
