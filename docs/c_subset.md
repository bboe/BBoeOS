---
title: C subset reference
nav_order: 50
---

# C subset reference

`cc.py` is a from-scratch C compiler that translates `user/programs/*.c` to
NASM-compatible 32-bit assembly. It is small enough to read end-to-end
(`cc/parser.py`, `cc/codegen/`), and it accepts only the slice of C the kernel
and userland actually need. This page is the reader's-eye summary of that slice
— what compiles, what doesn't, and the conventions the runtime expects.

The compiler is itself written in Python. The userland program `bin/asm` is a
self-hosted x86 assembler; the in-OS toolchain is C source → `cc.py` →
NASM-syntax assembly → `nasm` → flat binary.

## Invocation

```sh
python3 cc.py [compile] <input.c> [<output.asm>] [--bits 16|32] [--target user|kernel] [--object]
python3 cc.py pack-ccobj <input.bin> <input.lst> <output.ccobj>
```

`compile` is the default subcommand and is inferred when no subcommand verb
appears in argv, preserving the legacy `cc.py <input.c> [<output.asm>]`
invocation.

- `--bits 32` (default): emits 32-bit protected-mode assembly. `--bits 16` is
  for the bootloader stage.
- `--target user` (default): emits a stand-alone user program with `org
  08048000h`, including `constants.asm` and a BSS trailer.
- `--target kernel`: emits bare assembly suitable for `%include` into the kernel
  blob — no `org`, no constants, no trailer.
- `--object`: emit object-mode NASM intended for `nasm -f bin -l file.lst`
  followed by `cc.py pack-ccobj` (see [Object-file emission
  mode](#object-file-emission-mode) below).  Incompatible with `--target
  kernel`.

`pack-ccobj` is a separate subcommand that packages a NASM-assembled `.bin` +
`.lst` pair into a `.ccobj` JSON object file consumable by the (still
in-progress) `tools/ccld.py` linker.  See [Object-file emission
mode](#object-file-emission-mode).

Errors land on stderr as `<file>:<line>: error: <message>` and the process exits
1.

## Types

| Type | Notes |
|------|-------|
| `int` | Signed, native machine word: 32-bit under `--bits 32`, 16-bit under `--bits 16`. |
| `char` | 8-bit signed; zero-extends on load. |
| `uint8_t`, `uint16_t`, `uint32_t` | Fixed-width unsigned. |
| `unsigned long` | Always 32-bit unsigned (held in `DX:AX` under `--bits 16`). Locals only — file-scope `unsigned long` globals are rejected. Bare `long` is rejected; use `unsigned long`. |
| `void` | Function return only; or `void *`. |
| `struct NAME` | Layout matches positional declaration. Member access uses `.` for values, `->` through pointers. |

Pointer depth tops out at `T **` for scalars and `struct T *` for structs.
Multi-dimensional arrays (`int grid[5][10]`) are supported with positional
initializers; designated initializers (`{[2] = 5}`) are not.

The keywords `const` and `volatile` are accepted in declarations and ignored —
they exist only so POSIX-style prototypes (`int strcmp(const char *a, const char
*b)`) parse.

File-scope `typedef <type> <name>;` registers an alias that expands inline
wherever a type specifier is expected (variable declarations, parameters, casts,
`sizeof`, `*(T *)expr`).  Caller-side pointer stars compose on top of the
alias's own stars — `typedef char *str; str *pp;` becomes `char **`, capped at
the usual 2-star pointer depth.  Local-scope typedefs and function-pointer
typedefs (`typedef void (*sig_t)(int);`) are not yet supported.

Not supported: `float`, `double`, `long long`, `_Bool` / `bool`, bit-fields,
VLAs, K&R function syntax, typedef'd function-pointer aliases.

## Storage classes and attributes

- File-scope `static` lands in BSS (zero-filled) and gives internal linkage.
- File-scope plain declarations land in the data section if they have an
  initializer, BSS if not.
- `extern` at file scope declares a symbol resolved at link time (e.g., kernel
  constants from `constants.asm`); no storage is emitted. Local `extern` is not
  supported.
- Two `__attribute__` forms are recognised on file-scope globals:
  - `__attribute__((asm_register("si")))` aliases the global to a CPU register
    (`si` is the only commonly used one).
  - `__attribute__((asm_symbol("name")))` overrides the emitted symbol name,
    useful when the kernel exports a name that isn't a valid C identifier.

## Operators

Supported:

| Class | Operators |
|-------|-----------|
| Arithmetic | `+`, `-`, `*`, `/`, `%` |
| Bitwise | `&`, `\|`, `^`, `~`, `<<`, `>>` |
| Comparison | `==`, `!=`, `<`, `<=`, `>`, `>=` |
| Logical (short-circuit) | `&&`, `\|\|`, `!` |
| Assignment | `=`, `+=`, `-=`, `*=`, `/=`, `%=`, `&=`, `\|=`, `^=`, `<<=`, `>>=` |
| Unary | `&` (address-of), `*` (dereference), unary `-`, `~`, `!` |
| Postfix | `[]`, `()`, `.`, `->` |
| Other | `sizeof(type)` and `sizeof(expr)`, casts |

Not supported: `++`, `--`, the comma operator (commas only separate function
arguments). Idiomatic substitutes are `x += 1` and a separate statement for the
second comma operand.  The ternary `?:` IS supported (with a peephole that fuses
guarded-update shapes like `x = cond ? x : y` into a tight `cmp / jcc / mov`
sequence).

The parser folds compile-time-constant subexpressions (`COLUMNS - 1` becomes a
single `Int` node) and rewrites division/modulo by powers of two into
shifts/masks at parse time, so you get the optimisation for free.

## Control flow

Supported keywords: `if` / `else`, `while`, `do` / `while`, `break`, `continue`,
`return`.

**Not supported**: `for`, `switch` / `case`, `goto`, computed gotos, labels. The
kernel and the 3 700-line self-hosted assembler are written entirely with
`while` and `if` — your code can be too.

A non-comparison condition is auto-wrapped: `if (x)` is treated as `if (x !=
0)`.

## Functions

A function definition uses ANSI prototype syntax; K&R is rejected:

```c
int strcmp(const char *a, const char *b) {
    /* ... */
}
```

Forward declarations (a prototype with `;` instead of a body) are kept in the
AST so the compiler can record metadata (calling convention overrides, register
pinning) before the body is parsed. Recursion works.

The `extern` keyword on a function declaration is accepted (and ignored —
function declarations are extern by default in C). It exists for parity with C
and because object-mode source files use `extern void foo(args);` to mark
cross-translation-unit references explicitly, so the codegen knows to emit
`CCREL_CALL` instead of `call` for that callee. In flat mode it is a no-op.

`main` is special: if its parameter list is `int argc, char *argv[]` the runtime
reads them off the Linux SysV i386 startup frame the kernel writes onto the user
stack before iretd — `argc` from `[esp]`, `argv` from `[esp+4]` (a real pointer
into the kernel-supplied argv slot array). A `main` that takes no arguments is
allowed.

Variadic user functions are not supported; the only variadic call site in the
language is the builtin `printf`.

### Calling convention

For plain-int callees the compiler implicitly passes args 0..2 in
`eax`/`edx`/`ecx` (any remaining args caller-push); each register-passed param
is spilled into a stack slot at the prologue. Returns land in `eax` (or `ax` in
16-bit mode); the caller cleans up any stack-passed args. The default is
suppressed for `main` (the loader pushes argc/argv) and for callees whose call
sites pass complex arguments — those fall back to cdecl. The compiler offers a
few annotation-driven overrides for kernel-bridge functions:

- `carry_return` — return value is a boolean delivered in the carry flag (`CF=0`
  → true). Lets the caller branch with `jnc` / `jc` instead of materialising the
  result.
- `out_register("cx")` — caller passes `&local`; instead of pushing the address,
  the callee places the result in the named register and the caller stores it
  back.
- `preserve_register("reg")` — the prologue pushes the register and every
  epilogue pops it, so the function can clobber it freely.
- `naked` — no prologue or epilogue. Parameters and locals are not declared; the
  body is hand-written assembly inside `asm("…")`.

## Preprocessor

Token-level expansion only.

- `#include "<path>"` — double-quoted only; resolved relative to the source's
  directory first, then against any search paths the CLI adds. `cc/cli.py` walks
  up from the source looking for a sibling `include/` directory and adds it to
  the search list, so `#include "strtol.h"` from `user/programs/foo.c` finds
  `kernel/include/strtol.h`.  Recursive includes are detected and rejected.
- `#define NAME tokens…` — object-like macro.  Body extends to end of line;
  replacement is token-level so the value is retokenized at the call site and
  the call site's line number is used in errors.
- `#define NAME(p1, p2, …) tokens…` — function-like macro.  Parens must
  immediately follow the name (per C: `#define FOO (x)` is an object-like macro
  whose value is `(x)`).  Arguments are pre-tokenized at definition time, then
  re-stamped with the call-site's line at expansion.  See
  `kernel/include/macros.h` for examples.
- `#ifndef NAME` / `#endif` — conditional inclusion.  If `NAME` is already
  defined when the `#ifndef` is seen, every line up to the matching `#endif` is
  dropped (including any nested `#define` / `#include`).  Nests freely.

Not supported (would each be a defined extension):

- Stringification (`#x`), token pasting (`a ## b`), variadic macros (`...` /
  `__VA_ARGS__`).
- `#undef`.
- `#ifdef`, `#if`, `#else`, `#elif` — only the `#ifndef` half of conditional
  inclusion is implemented (sufficient for header guards).
- `#error`, `#warning`, `#pragma`.
- Angle-bracket includes (`<stdio.h>`).

## Literals

- Integer: decimal (`42`), hex (`0xABCD`). No `0b…` binary, no `U`/`L` suffix.
- Char: `'c'`, including escapes `\n \r \t \b \e \0 \\ \' \" \xHH`.
- String: same escape set; adjacent literals concatenate at parse time (`"foo"
  "bar"` → `"foobar"`); no line-continuation.

Each unique string literal becomes its own `_str_NN` label in the read-only data
section and is null-terminated.

## Initializers

- Scalars: `int x = 5;` works at file scope and in locals.
- Arrays: `int a[3] = {1, 2, 3};` for both globals and stack-allocated locals;
  trailing commas allowed; missing entries zero-fill.
- Struct array elements: {% raw %}`struct Point pts[2] = {{1, 2}, {3, 4}};`{%
  endraw %} (positional).

Not supported: designated initializers, compound literals, nested
struct-of-struct initializers, runtime-sized local arrays.

## The runtime: libbboeos and builtin functions

There is **no libc**. Instead, the kernel maps a read-only **libbboeos** at
user-virt `0x10000`. Each entry is a 5-byte stub that thunks into the matching
`INT 30h` syscall. Userland reaches it through the `FUNCTION_*` constants in
`kernel/include/constants.asm` (`FUNCTION_PRINT_STRING`,
`FUNCTION_WRITE_STDOUT`, `FUNCTION_DIE`, etc.).

On top of the libbboeos, the compiler recognises a fixed set of **builtin
function names** and emits inline syscall sequences for them. The authoritative
list is `BUILTIN_CLOBBERS` in `cc/codegen/x86/generator.py`:

| Group | Builtins |
|-------|----------|
| Console / I/O | `printf`, `putchar`, `getchar`, `read`, `write`, `strlen`, `die`, `exit`, `_exit` |
| Filesystem | `open`, `close`, `unlink`, `rename`, `mkdir`, `rmdir`, `chmod`, `fstat`, `seek`, `dup`, `dup2` |
| Memory | `memcpy`, `memcmp`, `memset`, `fill_block`, `sys_break` |
| Networking | `net_open`, `sendto`, `recvfrom`, `mac`, `parse_ip`, `print_ip`, `print_mac`, `checksum` |
| Time | `uptime`, `uptime_ms`, `sleep`, `datetime`, `print_datetime`, `alarm_ms` |
| Signals | `signal` |
| System | `exec`, `reboot`, `shutdown`, `pipeline2` |
| Video | `video_mode`, `set_palette_color` |
| Kernel-only port I/O | `kernel_inb`, `kernel_inw`, `kernel_insw`, `kernel_outb`, `kernel_outw`, `kernel_outsw` |
| Far memory access | `far_read8`, `far_read16`, `far_read32`, `far_write8`, `far_write16`, `far_write32` |
| Inline asm | `asm("…")` (raw NASM lines spliced into the output) |

A subset (`chmod`, `mac`, `mkdir`, `parse_ip`, `rename`, `rmdir`, `unlink`) is
treated as **error-returning**: the codegen pattern-matches `if (error) { … }`
against their result so a bad return short-circuits to the error path without an
extra register-to-register move.

Anything not in `BUILTIN_CLOBBERS` and not declared with `extern` is treated as
a user-defined function and lowered to a `call _<name>`. Programs are
single-file: there is no linker. Helpers are shared by putting the function
definition (not just a prototype) in a header under `kernel/include/` and
`#include`-ing it from each consumer — see `line_helpers.h`, `strtol.h`,
`ctype.h`, `getopt.h` for the convention. Every program is its own translation
unit, so each consumer inlines a private copy of the function body; when a real
libc lands you swap the inlined definition for an `extern` declaration and the
bodies disappear from each binary.

## Object-file emission mode

`cc.py --object` emits NASM intended for assembly with `nasm -f bin -l file.lst`
and packaging via `cc.py pack-ccobj`.  The resulting `.ccobj` is consumed by the
(still in-progress) `tools/ccld.py` linker, which combines per-translation unit
`.ccobj` files with a runtime archive to produce a flat binary.  Object mode is
opt-in per program and entirely separate from `user/libbboeos/libbboeos.a` (the
clang-built libc used by the Doom port) — the two link worlds do not
interoperate.

Differences from the default flat-binary mode:

- Emits `section .text` / `section .rodata` / `section .data` / `section .bss`
  instead of `org 08048000h`.
- Emits `global <name>` before each defined function (so the linker can resolve
  cross-translation-unit calls).
- Emits `%include "ccobj_markers.inc"` for the `CCREL_*` marker macros (defined
  in `kernel/include/`).  Calls to functions declared `extern` become
  `CCREL_CALL
  <name>` macro invocations — raw bytes the linker will patch at link time.
- Suppresses the flat-mode `_program_end:` label and `_bss_end equ` trailer; the
  linker emits the final BSS trailer when producing the flat binary.

Object mode does not yet support programs with non-zero BSS (`int big[1024];`
and friends fail at compile time with `NotImplementedError`); BSS support lands
when the runtime archive does (PR 3 of the broader design).  `--object` is
incompatible with `--target kernel` — the kernel build doesn't link, so object
files have nothing to do.

The full design lives at
`docs/superpowers/specs/2026-05-16-cc-object-files-design.md` (local-only spec
branch).

### Linker pipeline (`tools/ccld.py`)

`cc.py --object` output is not directly executable.  The companion linker
`tools/ccld.py` consumes one or more `.ccobj` files (plus optional `.ccar`
archives) and produces the flat binary that `program_enter` loads at
`PROGRAM_BASE`.

Invocation:

```sh
tools/ccld.py --output bin/foo \
              --base 0x08048000 \
              build/runtime/_start.ccobj \
              build/c/foo.ccobj \
              build/runtime/libbboeruntime.ccar
```

Positional order matters: the linker concatenates each section (`text → rodata →
data → BSS-trailer`) in input order, so an object that must land at offset 0 of
`text` (typically `_start`) is passed first.  `.ccar` archives are scanned for
symbols on demand — members are pulled in only when an unresolved extern matches
a member's `provides` list, and pull-in iterates to fixed point so a pulled-in
member can in turn drag in further members.  The final image is `text || rodata
|| data || <bss_size:le32><B032:le16>`, with section starts aligned to the
larger of the default per-section alignment and each contributing object's
per-section `align` field.

An optional `--emit-map <path>` writes a JSON symbol map (`{"symbols": {name:
address, ...}}`) for debugging; the map records every global symbol's final
absolute address (BSS symbols included).

Hard-fail errors (written to stderr, exit 1):

- Unresolved extern after archive pull-in
- Multiple objects define the same global symbol
- Relocation references an unknown symbol or unknown relocation type (only
  `rel32` and `abs32` are recognised today)
- `.ccobj` version field is not `1`, or required top-level keys are missing
- A `rel32` displacement does not fit in a signed 32-bit integer
- A `.ccar` member file referenced in the manifest is missing on disk

### Archive packer (`tools/ccar.py`)

```sh
tools/ccar.py --output build/runtime/libbboeruntime.ccar \
              build/runtime/*.ccobj
```

Writes a JSON manifest at `--output`; each member entry records the file's
basename and its `provides` list (the global symbols it defines — local symbols
are object-private and never appear).  Members must live in the same directory
as the manifest: the linker resolves member paths as siblings of the manifest
file, so packer and linker agree on layout without an absolute path baked into
the archive.

## Quick example

A minimal program:

```c
int main(int argc, char *argv[]) {
    if (argc != 1) {
        die("usage: greet <name>\n");
    }
    printf("hello, %s\n", argv[0]);
    return 0;
}
```

Compile and assemble manually:

```sh
python3 cc.py user/programs/greet.c /tmp/greet.asm
nasm -f bin /tmp/greet.asm -o bin/greet
```

In practice you don't need the manual path: `./make_os.sh` discovers every `*.c`
under `user/programs/` and adds its compiled output to the disk image at
`bin/<name>` automatically.
