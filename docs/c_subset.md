---
title: C subset reference
nav_order: 50
---

# C subset reference

`cc.py` is a from-scratch C compiler that translates `src/c/*.c` to NASM-compatible 32-bit assembly. It is small enough to read end-to-end (`cc/parser.py`, `cc/codegen/`), and it accepts only the slice of C the kernel and userland actually need. This page is the reader's-eye summary of that slice — what compiles, what doesn't, and the conventions the runtime expects.

The compiler is itself written in Python. The userland program `bin/asm` is a self-hosted x86 assembler; the in-OS toolchain is C source → `cc.py` → NASM-syntax assembly → `nasm` → flat binary.

## Invocation

```sh
python3 cc.py <input.c> [<output.asm>] [--bits 16|32] [--target user|kernel]
```

- `--bits 32` (default): emits 32-bit protected-mode assembly. `--bits 16` is for the bootloader stage.
- `--target user` (default): emits a stand-alone user program with `org 08048000h`, including `constants.asm` and a BSS trailer.
- `--target kernel`: emits bare assembly suitable for `%include` into the kernel blob — no `org`, no constants, no trailer.

Errors land on stderr as `<file>:<line>: error: <message>` and the process exits 1.

## Types

| Type | Notes |
|------|-------|
| `int` | Signed, native machine word: 32-bit under `--bits 32`, 16-bit under `--bits 16`. |
| `char` | 8-bit signed; zero-extends on load. |
| `uint8_t`, `uint16_t`, `uint32_t` | Fixed-width unsigned. |
| `unsigned long` | Always 32-bit unsigned (held in `DX:AX` under `--bits 16`). Locals only — file-scope `unsigned long` globals are rejected. Bare `long` is rejected; use `unsigned long`. |
| `void` | Function return only; or `void *`. |
| `struct NAME` | Layout matches positional declaration. Member access uses `.` for values, `->` through pointers. |

Pointer depth tops out at `T **` for scalars and `struct T *` for structs. Multi-dimensional arrays (`int grid[5][10]`) are supported with positional initializers; designated initializers (`{[2] = 5}`) are not.

The keywords `const` and `volatile` are accepted in declarations and ignored — they exist only so POSIX-style prototypes (`int strcmp(const char *a, const char *b)`) parse.

Not supported: `float`, `double`, `long long`, `_Bool` / `bool`, bit-fields, VLAs, K&R function syntax, typedef'd function-pointer aliases.

## Storage classes and attributes

- File-scope `static` lands in BSS (zero-filled) and gives internal linkage.
- File-scope plain declarations land in the data section if they have an initializer, BSS if not.
- `extern` at file scope declares a symbol resolved at link time (e.g., kernel constants from `constants.asm`); no storage is emitted. Local `extern` is not supported.
- Two `__attribute__` forms are recognised on file-scope globals:
  - `__attribute__((asm_register("si")))` aliases the global to a CPU register (`si` is the only commonly used one).
  - `__attribute__((asm_symbol("name")))` overrides the emitted symbol name, useful when the kernel exports a name that isn't a valid C identifier.

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

Not supported: `++`, `--`, the ternary `?:`, the comma operator (commas only separate function arguments). Idiomatic substitutes are `x += 1`, an `if`/`else` rewrite of the ternary, and a separate statement for the second comma operand.

The parser folds compile-time-constant subexpressions (`COLUMNS - 1` becomes a single `Int` node) and rewrites division/modulo by powers of two into shifts/masks at parse time, so you get the optimisation for free.

## Control flow

Supported keywords: `if` / `else`, `while`, `do` / `while`, `break`, `continue`, `return`.

**Not supported**: `for`, `switch` / `case`, `goto`, computed gotos, labels. The kernel and the 3 700-line self-hosted assembler are written entirely with `while` and `if` — your code can be too.

A non-comparison condition is auto-wrapped: `if (x)` is treated as `if (x != 0)`.

## Functions

A function definition uses ANSI prototype syntax; K&R is rejected:

```c
int strcmp(const char *a, const char *b) {
    /* ... */
}
```

Forward declarations (a prototype with `;` instead of a body) are kept in the AST so the compiler can record metadata (calling convention overrides, register pinning) before the body is parsed. Recursion works.

`main` is special: if its parameter list is `int argc, char *argv[]` the runtime fills it from the program's `EXEC_ARG` string before entering ring 3. A `main` that takes no arguments is allowed.

Variadic user functions are not supported; the only variadic call site in the language is the builtin `printf`.

### Calling convention

The default is a cdecl variant: arguments are pushed right-to-left, the callee returns its scalar in `eax` (or `ax` in 16-bit mode), and the caller cleans up the stack. The compiler offers a few annotation-driven overrides for performance-critical or kernel-bridge functions:

- `regparm(1)` — first argument arrives in `ax` and is spilled into a stack slot at the prologue.
- `carry_return` — return value is a boolean delivered in the carry flag (`CF=0` → true). Lets the caller branch with `jnc` / `jc` instead of materialising the result.
- `out_register("cx")` — caller passes `&local`; instead of pushing the address, the callee places the result in the named register and the caller stores it back.
- `preserve_register("reg")` — the prologue pushes the register and every epilogue pops it, so the function can clobber it freely.
- `naked` — no prologue or epilogue. Parameters and locals are not declared; the body is hand-written assembly inside `asm("…")`.

## Preprocessor

Token-level expansion only:

- `#include "<path>"` — double-quoted only; resolved relative to the source's directory; recursive includes are detected and rejected.
- `#define NAME tokens…` — object-like only; no function-like macros (no `#define MAX(a, b) …`), no `#` stringification, no `##` token paste.

Not supported: `#ifdef` / `#ifndef` / `#if` / `#else` / `#endif`, `#error`, `#warning`, `#pragma`, `<…>` system includes.

## Literals

- Integer: decimal (`42`), hex (`0xABCD`). No `0b…` binary, no `U`/`L` suffix.
- Char: `'c'`, including escapes `\n \r \t \b \e \0 \\ \' \" \xHH`.
- String: same escape set; adjacent literals concatenate at parse time (`"foo" "bar"` → `"foobar"`); no line-continuation.

Each unique string literal becomes its own `_str_NN` label in the read-only data section and is null-terminated.

## Initializers

- Scalars: `int x = 5;` works at file scope and in locals.
- Arrays: `int a[3] = {1, 2, 3};` for both globals and stack-allocated locals; trailing commas allowed; missing entries zero-fill.
- Struct array elements: `struct Point pts[2] = {{1, 2}, {3, 4}};` (positional).

Not supported: designated initializers, compound literals, nested struct-of-struct initializers, runtime-sized local arrays.

## The runtime: vDSO and builtin functions

There is **no libc**. Instead, the kernel maps a read-only **vDSO** at user-virt `0x10000`. Each entry is a 5-byte stub that thunks into the matching `INT 30h` syscall. Userland reaches it through the `FUNCTION_*` constants in `src/include/constants.asm` (`FUNCTION_PRINT_STRING`, `FUNCTION_WRITE_STDOUT`, `FUNCTION_DIE`, etc.).

On top of the vDSO, the compiler recognises a fixed set of **builtin function names** and emits inline syscall sequences for them. The authoritative list is `BUILTIN_CLOBBERS` in `cc/codegen/x86/generator.py`:

| Group | Builtins |
|-------|----------|
| Console / I/O | `printf`, `putchar`, `getchar`, `read`, `write`, `strlen`, `die`, `exit` |
| Filesystem | `open`, `close`, `unlink`, `rename`, `mkdir`, `rmdir`, `chmod`, `fstat` |
| Memory | `memcpy`, `memcmp`, `memset`, `fill_block` |
| Networking | `net_open`, `sendto`, `recvfrom`, `mac`, `parse_ip`, `print_ip`, `print_mac`, `checksum` |
| Time | `uptime`, `uptime_ms`, `sleep`, `datetime`, `print_datetime` |
| System | `exec`, `set_exec_arg`, `reboot`, `shutdown` |
| Video | `video_mode`, `set_palette_color` |
| Kernel-only port I/O | `kernel_inb`, `kernel_inw`, `kernel_insw`, `kernel_outb`, `kernel_outw`, `kernel_outsw` |
| Far memory access | `far_read8`, `far_read16`, `far_read32`, `far_write8`, `far_write16`, `far_write32` |
| Inline asm | `asm("…")` (raw NASM lines spliced into the output) |

A subset (`chmod`, `mac`, `mkdir`, `parse_ip`, `rename`, `rmdir`, `unlink`) is treated as **error-returning**: the codegen pattern-matches `if (error) { … }` against their result so a bad return short-circuits to the error path without an extra register-to-register move.

Anything not in `BUILTIN_CLOBBERS` and not declared with `extern` is treated as a user-defined function and lowered to a `call _<name>`. Programs are single-file: there is no linker. To share helpers, `#include "shared.c"` directly from your source — every program is its own translation unit.

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
python3 cc.py src/c/greet.c /tmp/greet.asm
nasm -f bin /tmp/greet.asm -o bin/greet
```

In practice you don't need the manual path: `./make_os.sh` discovers every `*.c` under `src/c/` and adds its compiled output to the disk image at `bin/<name>` automatically.
