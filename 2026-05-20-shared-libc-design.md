# 2026-05-20 — shared libc: unify the vDSO + tools/libc surfaces, drop per-program reimplementations

Promote `user/libc/` from "static archive linked only into Doom" to
"shared libc mapped into every user program."  Replace today's
hand-written 13-entry vDSO blob (`user/vdso/vdso.asm`) with a real C
libc whose exports are auto-discovered into a `FUNCTION_POINTER_TABLE`.
Both cc.py-built and clang-built user programs call into the same
shared page via indirect calls; per-program reimplementations of
`strcmp` / `strchr` / etc. in `user/programs/*.c` go away.

This spec assumes the [tree-reorg](./2026-05-19-tree-reorg-design.md)
has shipped (PR #437) — `user/libc/`, `user/vdso/`, `user/programs/`,
and `lib/libbboeos` (on-disk filename) all already exist in their
final locations.

## Motivation

Two parallel libc surfaces exist today and they don't share code:

**vDSO (`user/vdso/vdso.asm`)** — 13 hand-rolled asm helpers
(`die`, `exit`, `get_character`, `print_*`, `printf`, `write_stdout`).
Mapped at user-virt `0x10000` in every program PD as a shared page.
Used by cc.py-built programs via `call FUNCTION_<name>` (direct) or
`call [FUNCTION_<name>_PTR]` (indirect, post-PR #424).

**`user/libc/`** — clang-built static archive (`libbboeos.a`) with a
full C libc: `memcmp/memcpy/memmove/memset/strcat/strchr/strcmp/strcpy/
strlen/strncmp/strncpy/strstr, atoi, qsort, malloc/free/calloc/realloc,
snprintf/sprintf/sscanf, fputc/fwrite, puts`.  **Only linked into
`bin/doom`** today — every byte is statically baked in.

The fallout, in concrete bytes:

- **`strcmp` is reimplemented from scratch in six `user/programs/*.c`
  files**: `fd_helpers.c`, `ls.c`, `pipe_consumer.c`, `pipe_producer.c`,
  `pipe_spam.c`, `shell.c`, plus a forward decl in `sort.c`.  Every
  user program that needs even one string op pays the bytes.
- **cc.py-built programs have no shared `strcmp/strchr/strstr/strncmp/
  strcpy/strncpy/strcat/memmove/atoi/qsort/malloc`** — they either roll
  their own or live without.  `printf` / `getchar` / `putchar` route
  through the vDSO; everything else doesn't.
- **Doom's `tools/libc/` (now `user/libc/`) `stdio.c` has its own
  `printf`/`vsnprintf`/`fputc`/`puts`** that go through `write()` —
  not the vDSO's `FUNCTION_PRINTF`.  314 lines of format parser
  baked into `bin/doom` when a shared one already exists.

Unifying the two surfaces collapses the duplication.  The mechanism
already exists: PR #423 added `FUNCTION_POINTER_TABLE` for
linker-friendly indirect calls; PR #424 taught cc.py to emit them
under `--object`; PR #429 moved libbboeos to its own on-disk file;
PR #431 added multi-page mapping.  The remaining work is wiring the
libc source through that pipe instead of hand-writing it in asm.

## North star

cc.py compiles every line of the libc.  cc.py is itself written in C
(eventually) and compiled by cc.py.  BBoeOS builds BBoeOS from inside
BBoeOS.

That goal is several years away.  This spec is the first concrete
step: build the libc the way it naturally wants to be built (real C,
varargs, function pointers, etc.) using clang as the temporary
bootstrap, and treat every clang-only file as a tracked cc.py feature
gap to close.  The cc.py-feature backlog driven by "what's still
clang-only in `user/libc/`?" is a strict subset of the backlog driven
by "compile cc.py-in-C" — so libc work and self-hosting work share one
forcing function.

## End-state architecture

```
user/libc/                        # C source — one .c per logical group
  include/                        # public headers — found by both compilers
    string.h, stdio.h, stdlib.h, …
    syscalls.h                    # auto-generated from kernel/include/constants.asm
  string.c, stdio.c, stdlib.c, math.c, ctype.c, dirent.c, signal.c, errno.c
  syscall.c, builtins.c           # the syscall ABI surface + clang compiler-rt shim
  _start.S, setjmp.S
  libc.ld                         # linker script: flat blob at vDSO base, exports the pointer table
  bootstrap.c                     # die / exit / shared_print_* (eventually replaces vdso.asm)

lib/libbboeos                     # on-disk artifact: multi-page flat blob
                                  # = libc .text + .rodata + .bss + FUNCTION_POINTER_TABLE
                                  # mapped read-only at user-virt 0x10000 in every PD

build/libbboeos_stubs.a           # tiny clang archive: one `jmp [PTR]` per export
                                  # links into clang-built user programs

cc.py                             # unknown-symbol → `call [FUNCTION_<name>_PTR]` indirect
                                  # known shared symbol set comes from libc/exports.txt or .ld
```

A user program — cc.py or clang — sees `strcmp` as a normal function.
The compiler emits a call that resolves at link time to an indirect
call through the libbboeos page; the call lands in the shared `.text`
mapped into the calling program's PD.

## Build pipeline (target)

```
user/libc/*.c   →  clang -c (for now)  →  user/libc/*.o
user/libc/*.S   →  clang -c             →  user/libc/*.o
                                          ↓
user/libc/libc.ld   ←  ld.lld --script ──┐
                                         →  build/libbboeos (flat bin)
user/libc/exports   ←  generated from .o symbol tables
                     →  FUNCTION_POINTER_TABLE at well-known offset
                                         ↓
tools/gen_libc_constants.py
       →  kernel/include/libc_exports.inc
          (FUNCTION_strcmp        equ 0x10800 + N*4, …)
                                         ↓
cc.py user programs: call [FUNCTION_strcmp]
clang user programs: link against build/libbboeos_stubs.a (jmp stubs)
```

Both compilers end up calling the same byte offset in the same shared
page.  The libc source itself is the single source of truth for what's
exported.

## Per-program state

The shared `.text` is read-only and identical across all programs.
Stateful libc surfaces (malloc arena, `errno`, `FILE*` table, signal
table, `atexit` slots) need per-program backing.

**Convention:** `_start` calls `libc_init()` (libc-side, in shared
page) which allocates per-program state via `sys_break` and stashes
the result pointer at a fixed user-virt slot (e.g. `0x11000`).  All
libc functions that touch state load the pointer from that slot and
index into the per-program block.  No state lives in libc `.bss` —
that would be shared across programs, which would silently corrupt.

This is the same trick glibc plays with `__libc_init_first` + TLS,
adapted to BBoeOS's simpler single-threaded model.

## Migration phases

Each phase is shippable on its own, leaves the tree green, and
delivers a measurable win.

### Phase 1 — Promote shared C headers out of `kernel/include/`

Today the reorg left a `user/include → ../kernel/include` symlink so
cc.py's walk-up include search finds shared headers (`getopt.h`,
`ctype.h`, `macros.h`, `line_helpers.h`, `pipe.h`, `program_state.h`,
`registers.h`, `shell_lex.h`, `strtol.h`, `wait.h`) from user-program
sources.  Two problems:

- The symlink is a hack — these headers semantically belong with the
  libc, not the kernel.
- `getopt.h`, `strtol.h`, `ctype.h`, `wait.h` *are* libc, full stop.
  The others are mixed — some have kernel-only uses today (e.g.
  `program_state.h`).

**Action:** Move pure-userspace headers from `kernel/include/` to
`user/libc/include/`.  Keep kernel-only headers and shared asm
`%include`s (`constants.asm`, `arp_frame.asm`, `dns_query.asm`,
`encode_domain.asm`, `parse_ip.asm`, `irq_tail.inc`, `ccobj_markers.inc`)
in `kernel/include/`.  Delete the `user/include` symlink.  cc.py user
programs pick up the libc headers via the existing walk-up logic from
`user/programs/*.c → user/libc/include/`.

This is preparatory cleanup — no new functionality.  Establishes
"libc owns its headers" before the runtime work in Phase 2.

### Phase 2 — Extend the libc build to produce a multi-page flat blob

Today `make -C user/libc` builds a static archive `libbboeos.a`.
Keep that target (Phase 4 retires it), and **add** a new target:
`build/libbboeos` — a flat binary suitable for `vdso_install` to
load and map into every program PD.

Concretely:

- New `user/libc/libc.ld` linker script: text at `0x10000`, then
  `.rodata`, then `.bss`, then `FUNCTION_POINTER_TABLE` at the
  smallest offset that doesn't collide with the helpers.  Matches the
  current vDSO layout (today the table is at `0x10800`) so existing
  cc.py-emitted indirect calls keep working unchanged across the cutover.
- `tools/gen_libc_pointers.py` (rename of `gen_vdso_pointers.py`):
  reads the linker's map file, emits the `FUNCTION_POINTER_TABLE`
  bytes + a NASM `%assign` include (`kernel/include/libc_exports.inc`)
  that maps `FUNCTION_<name>` → offset.
- `make_os.sh`: the existing vDSO build steps (lines 30-58) become
  the libc-blob build.  Currently the libc archive is built only when
  Doom is requested; the new blob is unconditional (every build needs
  it on the disk image as `lib/libbboeos`).

At the end of Phase 2, the blob *contains* both the original 13
helpers (still hand-written, ported from `vdso.asm` to bootstrap.c)
and *any other libc functions clang can compile*.  The exports table
auto-grows as the libc gains entries.

### Phase 3 — cc.py: extern-call fallback for unknown function names

cc.py today: unknown function name → compile error.  After this phase:
unknown function name → `call [FUNCTION_<name>_PTR]` indirect, with
`FUNCTION_<name>` resolved at NASM time from `libc_exports.inc`.

Concretely:

- Add to `cc/codegen/x86/emission.py` (the `Call` AST visitor): if
  `name` isn't a builtin AND isn't a TU-local function, emit
  `call [FUNCTION_<name>_PTR]` instead of raising `CompileError`.
- Preserve the existing inline-asm fast paths for `memcpy/memset/
  memcmp/strlen` (3-6 byte `rep`-prefix idioms cheaper than the
  6-byte indirect call) — those stay cc.py builtins.
- Delete the per-program `strcmp` reimplementations in
  `user/programs/{fd_helpers,ls,pipe_consumer,pipe_producer,
  pipe_spam,shell,sort}.c` — they're now satisfied by the libc
  export.

Measurable: total bytes in `user/programs/*.c` shrink by ~6 ×
`sizeof(strcmp)`.  More important: the shape "user program uses a
libc function" becomes a real shape, unlocking everything in Phase 5+.

### Phase 4 — Replace `libbboeos.a` with a thin stub archive

`bin/doom` today links the *full* libc statically (~50 KB inside the
Doom binary).  After Phase 4 it links against a *stub* archive
(`libbboeos_stubs.a`): one `jmp [FUNCTION_<name>_PTR]` per export,
each ~6 bytes.  Doom shrinks by tens of KB; future clang-built
programs share the same shared page.

Concretely:

- `user/libc/Makefile` target `libbboeos_stubs.a`: generated from
  the exports list; each entry is a one-line `.text` section
  containing `jmp [FUNCTION_<name>_PTR]`.
- `ports/doom/build.py` swaps `libbboeos.a` → `libbboeos_stubs.a` in
  the link line.
- `libbboeos.a` (the static archive) is kept as a build target
  during the migration window — useful for unit tests in `tests/unit/
  test_libbboeos.py` that compile + link individual functions on the
  host.  Eventually retire it once the unit tests have an
  alternative.

### Phase 5 — Retire `user/vdso/vdso.asm`

The 13 hand-written asm helpers become C source files inside
`user/libc/`:

| asm function                | C home                                          |
|-----------------------------|-------------------------------------------------|
| `shared_die`                | `user/libc/bootstrap.c` (bboeos-specific)       |
| `shared_exit`               | `user/libc/stdlib.c` (`_exit`)                  |
| `shared_get_character`      | `user/libc/stdio.c` (`getchar`)                 |
| `shared_print_character`    | `user/libc/stdio.c` (`putchar`)                 |
| `shared_print_string`       | `user/libc/stdio.c` (`puts` minus trailing `\n`)|
| `shared_printf`             | `user/libc/stdio.c` (`printf`)                  |
| `shared_write_stdout`       | `user/libc/syscall.c` (`write`)                 |
| `shared_print_byte_decimal` | `user/libc/bootstrap.c` (bboeos debug helper)   |
| `shared_print_decimal`      | `user/libc/bootstrap.c`                         |
| `shared_print_hex`          | `user/libc/bootstrap.c`                         |
| `shared_print_datetime`     | `user/libc/bootstrap.c`                         |
| `shared_print_ip`           | `user/libc/bootstrap.c`                         |
| `shared_print_mac`          | `user/libc/bootstrap.c`                         |

The `shared_print_*` debug helpers stay as bboeos-specific extensions
in `bootstrap.c` — POSIX has no equivalent.  Their exports keep their
existing `FUNCTION_*` names for compatibility.

Once the C versions land and produce byte-equivalent output to the
asm versions, delete `user/vdso/`.  The blob is now 100% C-defined,
which is the gate to Phase 6.

### Phase 6 — cc.py compiles libc files one at a time

Now the libc is normal C source; the only thing preventing cc.py from
compiling it is missing cc.py features.  Each unblocked file moves
from clang to cc.py, monotonically shrinking the clang dependency.

The cc.py feature ordering follows from what the libc uses.  Rough
priority (highest first):

1. **Real `va_list` / varargs** — blocks `vsnprintf`, `printf`,
   `sscanf` in `stdio.c`.  Highest priority: printf is the most-used
   libc function.
2. **Function pointers as values** — blocks `qsort` (callback), `signal`
   (handler), `atexit` (callback list).  Partial cc.py support already
   landed in PR #418 (NULL compare); needs full extension to "pass as
   argument" and "store in struct field."
3. **Struct returns** — blocks anything in `dirent.c` that returns a
   `struct dirent *` if not handled today.  Verify.
4. **Soft-float / x87 inline asm** — blocks `math.c`.  Lowest priority
   for self-hosting since math isn't used by cc.py-in-C.

These feature gaps are the seed of a separate doc:
`design-specs/2026-05-20-cc-self-hosting-roadmap.md` (TBD), which will
track the full backlog driven by both (a) "compile libc/*.c" and (b)
"compile cc.py-in-C."

## Open questions

1. **Naming.** "vDSO" is a Linux term for kernel-mapped shared pages.
   BBoeOS's libbboeos is conceptually different (userspace code,
   loaded from disk, mapped into PDs).  Once `user/vdso/` is gone in
   Phase 5, drop "vDSO" from prose entirely?  Use "libbboeos" or
   "shared libc" throughout?  Recommend yes — clean break.
2. **`build/libbboeos` size cap.**  Today the blob asserts `<= 0x800`
   bytes (one page minus the pointer table) at build time.  Once
   real libc functions land it'll routinely exceed one page; PR #431
   already added multi-page mapping support.  Decide the new cap —
   a single page (4 KB) is tight; recommend two pages (8 KB) as the
   Phase 2 ceiling and grow only when a real export crosses it.
3. **Backwards-compat with on-disk `bin/*` from previous boot images.**
   Programs already on a user's drive image were built against the
   13-entry table layout.  If we re-order or renumber `FUNCTION_*`
   slots, those programs break.  Recommend: append-only export
   numbering forever — new functions get new slots at the end of the
   table, existing slots never move.  The 13 vDSO entries keep their
   numbers across the cutover; new libc exports start at slot 13.
4. **Per-program state location.**  Spec proposes fixed user-virt
   slot at `0x11000` for the libc-state pointer.  Alternatives:
   per-program TLS via GS, or stash in the program's BSS at a known
   offset.  The fixed-slot option is simplest; pick unless something
   blocks it.
5. **Phase 1 header migration scope.**  Mixed-use headers
   (`pipe.h`, `program_state.h`, `registers.h`, `shell_lex.h`,
   `macros.h`, `line_helpers.h`) have callers on both sides.  Move
   to `user/libc/include/` and add a `kernel/include/` symlink for
   kernel callers, or split each into a kernel and user variant?
   Recommend move + symlink for the first pass — splitting is
   churnier and the kernel doesn't care where the file physically
   lives as long as `-I` paths resolve.
6. **Stub-archive granularity.**  One stub per export means N tiny
   `.text` sections in the archive.  Most linkers handle this fine
   but it bloats the symbol table.  Alternative: one stub file with
   all exports, linker garbage-collects unused.  Decide during
   Phase 4.

## Non-goals

- **No new libc functions in this spec.**  Whatever's already in
  `user/libc/` is the starting point; gaps (`getline`, `freopen`,
  …) get added incrementally as user programs ask for them.
- **No cc.py-in-C work.**  That's its own multi-quarter effort
  living behind the self-hosting roadmap; the libc spec only goes
  far enough to make cc.py-in-C *possible* (Phase 6 makes libc
  compilable; cc.py-in-C is a separate project that consumes that
  capability).
- **No replacement of the syscall ABI.**  `INT 30h` stays.  Libc
  wraps it as it does today.

## Sequencing recommendation

Phase 1 first (header cleanup) — small, low-risk, retires the symlink
hack.  Then a beat to confirm nothing broke.  Phases 2-4 are the
runtime work and naturally land as separate PRs.  Phase 5 (asm → C)
needs care because byte-equivalence regression risk is high — each
helper ports individually, and `test_asm.py` / `test_programs.py`
catch regressions.  Phase 6 is open-ended and continues for as long
as cc.py keeps gaining features.
