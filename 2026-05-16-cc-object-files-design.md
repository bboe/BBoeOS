# cc.py object files and a Python linker

**Date:** 2026-05-16
**Status:** Approved (brainstorm) — ready for implementation plan
**Predecessor:** `docs/cc_future_work.md` (the inlining roadmap this design coexists with)
**Related:** `docs/posix.md` § "System calls and C library" (the table whose ⚠️ libc-only rows this design will eventually convert to ✅)

## Context

`cc.py`-built programs ship in `bin/` as flat binaries assembled directly by
NASM (`-f bin`).  They reach the OS through the in-kernel vDSO `FUNCTION_*`
helpers (resolved to absolute addresses at NASM-assemble time) and through
raw `INT 30h` wrappers.  No linker exists in the cc.py pipeline: there is no
notion of an unresolved symbol, no object file, no archive.

A parallel world exists in `tools/libc/` — a clang-built static library
(`libbboeos.a`) with a `program.ld` linker script.  Today only the standalone
`hello` test binary (and the Doom port via `build_doom.py`) uses it.
cc.py-built programs cannot link against it, and there is no plan to make
them: `tools/libc/` is the clang-side library and stays that way.

`docs/cc_future_work.md` frames the cc.py-side libc story differently:
expand cc.py's accepted C subset until libc sources compile under cc.py
directly, with each program inlining whatever it uses.  That roadmap stands.
It is the default direction.

This document specifies the *complementary* mechanism: a cc.py-native object
file format and a small Python linker, so that helpers shared by multiple
cc.py-built programs can live in a single archive and be pulled in by name
rather than copy-pasted into each `src/c/*.c`.  The motivating wins are
modest today (the vDSO has already factored out most easy duplication —
only 1 of 34 shipped programs defines its own `die()`), but the
infrastructure becomes load-bearing once the inlining roadmap lands and
programs start carrying their own copies of `printf`, `malloc`, `qsort`,
etc.

The two link worlds (cc.py-native and clang/tools-libc) do not need to
interoperate.  They share only the loader: both produce flat binaries at
`PROGRAM_BASE = 0x08048000` with the 6-byte BSS trailer that
`program_enter` understands.

## Goals and non-goals

**Goals:**

- cc.py can emit object files (`.ccobj`) with sections, symbols, and
  relocations.
- A new Python linker (`tools/ccld.py`) consumes one or more `.ccobj` files
  plus archives (`.ccar`) and produces flat binaries loadable by
  `program_enter`.
- A small runtime archive (`libbboeruntime.ccar`) holds POSIX-named helpers
  shared by cc.py-built programs.
- Per-program opt-in via a manifest.  Programs not in the manifest are built
  exactly as today (no behavior change, no risk).
- Each piece (cc.py object mode, linker, archive packer, runtime members,
  per-program migrations) ships in an independent PR.  Stop at any point if
  priorities shift.

**Non-goals:**

- No interop with `tools/libc/libbboeos.a`.  cc.py-built programs cannot
  link against clang-built libc, and vice versa.  These are two separate
  toolchains feeding two separate (but identically-formatted) outputs.
- No ELF object files.  cc.py never emits ELF; the format is JSON-with-
  base64 (see "Object file format" below).
- No dead-code elimination finer than one object file.  If
  `string.ccobj` defines both `strcpy` and `strcmp` and a program calls
  only `strcpy`, both end up in the output.  The workaround is one
  function per `.ccobj` member (the v1 archive follows this rule).
- No PIC, no `.so`, no incremental linking, no debug info, no COMDAT, no
  weak symbols, no version scripts.
- No "common runtime" extracted speculatively.  v1 archive members are
  chosen because programs already share them informally, or because they
  are the obvious thing the next migration will want.

## Architecture overview

```
src/c/wc.c                                          src/runtime/strtol.c
     │                                                      │
     │ cc.py --bits 32 --object                             │ cc.py --bits 32 --object
     ▼                                                      ▼
build/c/wc.asm    (NASM with CCREL_ABS32 markers)    build/runtime/strtol.asm
     │                                                      │
     │ nasm -f bin -l wc.lst                                │ nasm -f bin -l strtol.lst
     ▼                                                      ▼
build/c/wc.bin + wc.lst                              build/runtime/strtol.bin + strtol.lst
     │                                                      │
     │ cc.py --pack-ccobj                                   │ cc.py --pack-ccobj
     ▼                                                      ▼
build/c/wc.ccobj                                     build/runtime/strtol.ccobj  (+ errno, die, …)
     │                                                      │       build/runtime/_start.ccobj
     │                                                      │ tools/ccar.py        │ (passed explicitly,
     │                                                      ▼                       not archived)
     │                                               build/runtime/libbboeruntime.ccar
     │                                                      │                       │
     │                          tools/ccld.py               │                       │
     └──────────────────────────────────────────────────────┴───────────────────────┤
                                                                                    ▼
                                                                              build/c/wc   (flat binary)
                                                            │
                                                            │ add_file.py -x -d bin
                                                            ▼
                                                       drive.img  bin/wc
```

The default flat path (programs not in the opt-in manifest) is unchanged:

```
src/c/cat.c → cc.py → cat.asm → nasm -f bin → build/c/cat → drive.img bin/cat
```

The two paths share NASM as the byte producer.  cc.py never invokes NASM
itself; orchestration stays in `make_os.sh`, matching today's structure.

## Object file format (`.ccobj`)

One file per translation unit.  JSON with base64-encoded section bytes.
JSON is chosen because the volumes are tiny (kilobytes), the format is
diffable in tests, and inspection requires no special tools.

```json
{
  "version": 1,
  "source": "src/c/wc.c",
  "sections": {
    "text":   { "bytes": "<base64>", "align": 16 },
    "rodata": { "bytes": "<base64>", "align": 4 },
    "data":   { "bytes": "<base64>", "align": 4 },
    "bss":    { "size": 128, "align": 4 }
  },
  "symbols": {
    "main":   { "section": "text",   "offset": 0,   "binding": "global" },
    "buf":    { "section": "data",   "offset": 0,   "binding": "global" },
    "helper": { "section": "text",   "offset": 96,  "binding": "local"  }
  },
  "extern": ["die", "read_line", "errno"],
  "relocations": [
    { "section": "text", "offset": 12, "symbol": "die",       "type": "rel32" },
    { "section": "text", "offset": 47, "symbol": "read_line", "type": "rel32" },
    { "section": "text", "offset": 89, "symbol": "errno",     "type": "abs32" }
  ]
}
```

(`FUNCTION_*` and `SYS_*` constants from `src/include/constants.asm` are
NOT extern references — they're fixed addresses defined at NASM-assemble
time and don't appear in the reloc table.  Only references to
runtime-archive symbols and cross-translation-unit symbols generate
relocations.)

**Relocation types.**  Two from PR 1:

- `rel32` — patch a 4-byte location with the signed displacement from
  `(patch_site + 4)` to the symbol's address.  Used for direct CALL and
  JMP to external functions: `call die` → `E8 <rel32 displacement>`.
  This is the form x86 actually uses for direct calls (there is no
  `call abs32`).
- `abs32` — patch a 4-byte location with the absolute address of a
  symbol.  Used for data references: `mov eax, [errno]` →
  `A1 <abs32 of errno>`.  Less common in cc.py output today (no extern
  data refs ship), but needed once `errno` lands in PR 3.

Adding more reloc types later (e.g. relative data-load forms for PIC)
remains an easy extension; the marker macros are type-tagged.

**Why not NASM `extern`?**  `nasm -f bin` rejects `extern` declarations
outright ("binary output format does not support external references").
Object-mode programs still assemble with `-f bin` so the rest of the
toolchain stays format-agnostic.  Instead, cc.py emits marker macros
that produce raw bytes (a known opcode plus a 4-byte zero placeholder),
so NASM never needs to know any symbol is unresolved — to NASM, the
macro just emits five or six fixed bytes per call site.

**How cc.py produces this.**  cc.py keeps emitting NASM as it does today.
For object mode it adds:

- `%include "ccobj_markers.inc"` at the top of every emitted `.asm`.
  The include defines a small finite set of marker macros, one per
  (instruction form, register choice) combination cc.py emits:
  `CCREL_CALL <sym>`, `CCREL_JMP <sym>`, `CCREL_MOVABS_LOAD_EAX <sym>`,
  `CCREL_MOVABS_STORE_EAX <sym>`, `CCREL_MOV_IMM32_<REG> <sym>`, etc.
  Each macro emits the matching opcode bytes followed by `dd 0`.
- Section directives — `section .text`, `section .rodata`, `section
  .data`, `section .bss` — replacing today's monolithic `org 08048000h`
  prologue.
- Top-level `global <name>` declarations for every function and data
  symbol with external linkage (today: every C function, since cc.py
  has no `static`).
- The BSS-trailer emission that cc.py does today (the `_program_end`
  label and `_bss_end equ _program_end` lines) is suppressed in object
  mode; the linker emits the final trailer.

cc.py replaces every emission of `call <extern>`, `jmp <extern>`,
`mov reg, <extern>`, `mov [<extern>], reg`, `mov reg, [<extern>]` with
the corresponding `CCREL_*` macro invocation.  Local symbols (jumps
within the same function, calls between same-translation-unit functions)
are NOT wrapped — they resolve at NASM-assemble time as today.

After NASM assembles to `-f bin`, cc.py's `--pack-ccobj` subcommand
reads the NASM listing (`-l file.lst`) to recover:

- Section-relative offsets of each defined symbol (from `global` labels
  in the listing).
- Section-relative offsets of each relocation site (from `CCREL_*`
  macro invocations in the listing — the macro name encodes the patch
  offset within the instruction and the reloc type).
- Section sizes.

It then writes the `.ccobj` JSON.  **cc.py never has to learn ELF, and
never has to compute byte offsets itself** — NASM stays the source of
truth for instruction encoding.

**PR-phasing of relocation types.**  PR 1 ships `CCREL_CALL`,
`CCREL_JMP`, and `--pack-ccobj` handling for both `rel32` and `abs32`
in fixtures (so the linker and runtime PRs don't have to revisit
`--pack-ccobj`).  Hand-crafted fixture `.asm` files exercise both
types.  The CCREL_MOVABS_* / CCREL_MOV_IMM32_* macros — needed only
when cc.py compiles C that references extern data (e.g. `errno`) —
land in PR 3 alongside `errno.c`, because they require new C-level
support for `extern int x;` declarations in cc.py that's out of scope
for PR 1.

**Risk and fallback.**  The marker-macro approach localizes the
listing-parsing risk: pack-ccobj only needs to find `CCREL_*` invocations
in the source column, which is a simple text match.  Recovering symbol
offsets for `global` labels is similarly local — match label lines and
read the offset column.  If a corner case appears (e.g. NASM splits a
macro across multiple listing lines in unexpected ways), the fallback is
to emit one section per NASM invocation and rely on file sizes for the
offsets — clunkier but bulletproof.  Local swap in `--pack-ccobj`, no
spec change.

## Linker (`tools/ccld.py`)

A single Python script, single-pass, no optimization.  Probably ~300
lines.

**Invocation:**

```
tools/ccld.py --output bin/wc \
              --base 0x08048000 \
              build/runtime/_start.ccobj \
              build/c/wc.ccobj \
              build/runtime/libbboeruntime.ccar
```

(Positional order matters for section layout: `_start.ccobj` is first so
`_start` lands at offset 0.  The archive comes last because its members
are pulled in lazily as references resolve.)

**Algorithm:**

1. **Load inputs.**  Each positional arg is either a `.ccobj` (single
   object, always pulled in) or a `.ccar` (archive — scanned for symbols,
   members pulled in only when referenced).
2. **Symbol resolution.**  Single global symbol table.  For each
   unresolved `extern` in any pulled-in object, look it up in (a) symbols
   already defined by pulled-in objects, (b) archive members.  Pulling in
   an archive member can itself add new unresolved externs — iterate to
   fixed point.  Multiple definitions of the same global → hard error.
   Unresolved symbols at end → hard error with a clear list.
3. **Section layout.**  Concatenate sections in fixed order: `text` →
   `rodata` → `data` → (BSS reserved at end).  Each section starts at the
   next 16-byte boundary (or per-object `align`, whichever is larger).
   Compute final address of every symbol (`base + section_offset +
   symbol_offset_in_section`).
4. **Relocation.**  Walk every reloc in every pulled-in object.  For
   `abs32`, patch the 4 bytes at `final_section_base + reloc.offset` with
   the 32-bit final address of the named symbol.
5. **Emit flat binary.**  Write `text || rodata || data` to the output.
   Append the 6-byte BSS trailer (`<bss_size:le32><0xB032:le16>`) that
   `program_enter` already understands.  Done.

**Entry-point convention.**  `_start.ccobj` (a separately-passed
positional object, not an archive member — see Runtime archive section)
provides `_start` and references undefined `main`.  cc.py-built programs
in object mode provide `main` as their entry point (cc.py emits the
user's `main` function as a regular global symbol; today it's implicitly
the program-start since cc.py emits it first).  The linker doesn't care
which object provides what — it just resolves symbols.  Section layout
is determined by the order positional objects appear on the command
line, so `make_os.sh` passes `_start.ccobj` first, putting `_start` at
offset 0 of the `text` section and thus at `PROGRAM_BASE`.

**Explicit non-features** (vs GNU ld):

- No dead-code elimination at function granularity.  Granularity is one
  whole `.ccobj`.  The v1 archive's one-function-per-member rule keeps
  this from mattering.
- No section merging beyond text/rodata/data/bss.  No COMDAT, no weak
  symbols, no version scripts.
- No incremental linking, no `.so`, no PIC.  Flat binary at fixed base
  only.

## Runtime archive (`libbboeruntime.ccar`)

Thin wrapper: a JSON manifest plus a directory of sibling `.ccobj` files.
The format is internal to the toolchain — choosing JSON-manifest-plus-files
over a tarball makes it easy to inspect and diff during development.

```json
{
  "version": 1,
  "members": [
    { "file": "errno.ccobj",     "provides": ["errno"] },
    { "file": "die.ccobj",       "provides": ["die"] },
    { "file": "read_line.ccobj", "provides": ["read_line"] },
    { "file": "strtol.ccobj",    "provides": ["strtol"] },
    { "file": "usage.ccobj",     "provides": ["usage"] }
  ]
}
```

**Granularity rule.**  One function (or one data symbol) per `.ccobj`
member by default.  This aligns with the linker's one-object-granularity
DCE — pulling in one symbol doesn't drag in unrelated neighbours.
Multi-function members are allowed (and useful for tightly-coupled
helpers) but cost link-time bloat.

**`_start` is not in the archive.**  Archive members are pulled in only
when referenced, and nothing in user code references `_start` (it's the
other way around — `_start` references `main`).  So `_start.ccobj` lives
in `src/runtime/` alongside the archive members but is passed to the
linker as an explicit positional `.ccobj`, not bundled into the archive.
Every object-mode build passes it first; this puts `_start` at offset 0
of the `text` section and thus at `PROGRAM_BASE`, matching where
`program_enter` jumps.

**v1 members.**  Deliberately tiny — the proving ground, not a full libc.

| Member | Signature / purpose | Notes |
|--------|---------------------|-------|
| `errno.ccobj` | `int errno;` — single global, ~4 bytes BSS | Lets `strtol` and future runtime helpers set errno without divergence from POSIX. |
| `die.ccobj` | `void die(const char *msg)` — `puts(msg); exit(1);` | Only `cp.c` defines this inline today; the rest call `FUNCTION_DIE` (vDSO).  Included for POSIX-naming completeness, not byte savings. |
| `read_line.ccobj` | The shared line-reader from the `wc` commit | 3 programs use it today; will grow as more line-oriented utilities migrate. |
| `strtol.ccobj` | POSIX `long strtol(const char *nptr, char **endptr, int base)`, sets `errno = ERANGE` on overflow | Used by `head`, `tail`, `seq`, etc. for `-n N` argument parsing.  endptr support relies on cc.py's pointer-to-pointer support (landed in PR #371). |
| `usage.ccobj` | `void usage(const char *prog, const char *msg)` — standardized "Usage: <prog> <msg>\n" + exit(2) | Tiny standardization helper. |

Five archive members plus the separately-passed `_start.ccobj`, each well
under 100 bytes of code.

**Source location.**  `src/runtime/*.c` (new directory).  Mixing cc.py-built
and clang-built sources in `tools/libc/` would be confusing; the new
directory keeps the two link models cleanly separated.

## Build wiring

Per-program opt-in via `src/runtime/object_mode.txt`:

```
# Programs built via cc.py --object + ccld.
# Each name must match src/c/<name>.c.
wc
seq
```

`make_os.sh` reads this file and routes each program through one of two
paths.

**Flat path (default, unchanged):**

```sh
python3 cc.py --bits 32 src/c/cat.c build/c/cat.asm
nasm -f bin -i src/include/ -o build/c/cat build/c/cat.asm
```

**Object path (opt-in):**

```sh
python3 cc.py --bits 32 --object src/c/wc.c build/c/wc.asm
nasm -f bin -l build/c/wc.lst -i src/include/ -o build/c/wc.bin build/c/wc.asm
python3 cc.py --pack-ccobj build/c/wc.bin build/c/wc.lst build/c/wc.ccobj
python3 tools/ccld.py --output build/c/wc \
                      --base 0x08048000 \
                      build/runtime/_start.ccobj \
                      build/c/wc.ccobj \
                      build/runtime/libbboeruntime.ccar
```

Two cc.py invocations per program because the first emits `.asm` with
relocation markers and the second post-processes the listing + bin into a
`.ccobj`.  Keeping `--pack-ccobj` as a separate subcommand means cc.py
never has to invoke NASM as a subprocess; orchestration stays in
`make_os.sh`.

**Runtime archive build** (new section in `make_os.sh`, runs once before
any programs):

```sh
for src in src/runtime/*.c; do
  name=$(basename "$src" .c)
  python3 cc.py --bits 32 --object "$src" "build/runtime/$name.asm"
  nasm -f bin -l "build/runtime/$name.lst" -o "build/runtime/$name.bin" "build/runtime/$name.asm"
  python3 cc.py --pack-ccobj "build/runtime/$name.bin" "build/runtime/$name.lst" "build/runtime/$name.ccobj"
done
python3 tools/ccar.py --output build/runtime/libbboeruntime.ccar build/runtime/*.ccobj
```

**Test integration.**  `tests/test_programs.py` already runs every shipped
program.  Programs migrated to the object path get the same regression
coverage automatically — no test changes needed if the runtime helpers
behave identically to the inlined versions they replace.

New: `tests/test_ccld.py` exercises the linker directly with hand-crafted
multi-object fixtures (single object → flat binary, two objects with
cross-references, archive selection, unresolved-symbol error,
multiple-definition error).  This is fast unit-level coverage that doesn't
need QEMU.

**Subtlety to flag.**  `make_os.sh` today is a flat bash script with no
notion of dependencies — every program is rebuilt every time.  The
object-path additions follow the same pattern (rebuild everything per
invocation).  If/when the build moves to incremental, the manifest-driven
routing will need a Make- or Ninja-style dependency graph; for now, full
rebuild matches existing behavior.

## PR breakdown

Five PRs, each independently mergeable and verifiable.

**PR 1 — `cc.py --object` mode** (~400 lines)

Adds the `--object` flag and the `--pack-ccobj` subcommand.  cc.py emits
NASM with `CCREL_CALL` / `CCREL_JMP` marker macros for external function
references (the rel32 reloc forms — see Object file format).  Ships a
`src/include/ccobj_markers.inc` header with the macro definitions.
`--pack-ccobj` reads NASM's `.lst` output + the raw `.bin`, walks the
listing for symbol offsets and reloc sites, writes the `.ccobj` JSON.
`--pack-ccobj` recognizes both `rel32` and `abs32` reloc forms even
though `--object` only emits `rel32` in PR 1 — hand-crafted `.asm`
fixtures using `CCREL_MOVABS_*` macros prove the abs32 path works,
keeping later PRs from having to revisit `--pack-ccobj`.

No archive, no linker, no programs migrated.  Verification: a fixture C
file is compiled with `--object`, the JSON is asserted to contain the
expected symbols/relocs, and the section bytes match the same source
compiled in flat mode (modulo placeholder zeros at reloc sites).  Plus
hand-crafted `.asm` fixtures exercising every CCREL_* macro form.  New
test: `tests/test_ccobj.py`.

**PR 2 — `tools/ccld.py` + `tools/ccar.py`** (~500 lines)

The linker and the archive packer.  Linker reads `.ccobj` files and `.ccar`
archives, resolves symbols, lays out sections, applies relocations, emits
flat binary + BSS trailer.  Archive packer wraps a directory of `.ccobj`
files with a symbol-index manifest.

Still no programs migrated, no runtime archive.  Verification:
`tests/test_ccld.py` exercises hand-crafted multi-object fixtures.  A
known-good fixture's flat binary output is compared byte-for-byte against
an expected file checked into the test data.

**PR 3 — `src/runtime/` + `libbboeruntime.ccar`** (~200 lines C + build
wiring)

Adds the six v1 archive members under `src/runtime/`.  `make_os.sh` builds
the archive.  Empty `object_mode.txt` manifest is added.

Still no programs migrated.  Verification: each runtime member has a unit
test under `tests/programs/` that exercises it through a tiny driver
program built via the object path.  This is the first end-to-end exercise
of `cc.py --object → ccld → flat binary` running in QEMU.  Adding the
manifest mechanism with zero entries means the existing flat path still
serves every shipped program — total regression surface is zero.

**PR 4 — Migrate first program (`wc`)**

`wc` already uses the shared `read_line` and parses `-l`/`-w`/`-c` with
`getopt`.  Move it to the object path (one line in `object_mode.txt`).
Inlined helpers in `wc.c` are deleted and replaced with calls to runtime
members (`read_line`, `strtol`, `die`/`usage` for errors).  Size delta is
reported in the PR description (likely negative — `wc` loses some bytes
by dropping its inlined helpers).  The existing `tests/test_programs.py`
entry for `wc` provides regression coverage with no test changes.

This PR is the real proof-of-concept.  If anything's wrong in the linker
or runtime, this is where it surfaces.

**PR 5 — Migrate `seq`, `head`, `tail`**

Three programs that all want `strtol` for `-n N` parsing.  Each is one
line added to `object_mode.txt` and the inlined `atoi`-equivalent helper
removed from the source.  Reports aggregate floppy-byte delta.

After PR 5, ~4 of 33 shipped programs use the linker path.  The remaining
29 stay on the flat path indefinitely — they don't share enough to
benefit.  The infrastructure is in place for the cc_future_work.md
inlining roadmap to land on top: when inlining adds real per-program
duplication, programs move to the object path one at a time and the
runtime archive grows.

**No PR migrates a program just to migrate it.**  Each migration has to
show a concrete byte delta (or removal of a hand-rolled helper).  If a
migration nets out neutral or worse, that program stays on the flat
path.

**Rollback story.**  Any program is rolled back by removing its line
from `object_mode.txt` — no source changes.  The runtime archive members
are pure C and remain useful as documentation even if no program
currently links them.

## Open questions

None blocking.  Two minor things to confirm during PR 1 / PR 3
implementation:

1. **NASM listing parsing with macro expansion.**  Need to confirm
   experimentally that NASM's `-l` listing shows `CCREL_*` macro
   invocations as a single source-column line (with the macro's emitted
   bytes alongside), rather than splitting macro expansion across
   multiple listing rows in a way that obscures the (offset, symbol)
   mapping.  Validated by Task 1 of the PR 1 plan.  If ambiguous, fall
   back to the one-section-per-NASM-invocation alternative described in
   "Object file format § Risk and fallback."
2. **`strtol` ergonomics with `&end`.**  PR #371 added pointer-to-pointer
   support; PR 3 should confirm that the exact `char *end; strtol(s,
   &end, 10);` pattern compiles cleanly under cc.py.  If a residual
   double-pointer gap is found, file it as a cc.py followup rather than
   blocking this work — `strtol(s, NULL, 10)` is a usable fallback for
   v1.

## Out of scope (followups)

- Migrating any of the 29 remaining cc.py programs that don't have
  obvious shared helpers — not until inlining creates the duplication
  that justifies the linker hop.
- Linking against `tools/libc/libbboeos.a` from cc.py-built programs.
  The two link worlds remain separate.
- ELF object file output from cc.py.  No use case yet; would only matter
  if we wanted to feed cc.py output into GNU ld directly.
- Incremental builds in `make_os.sh`.  Required only when full-rebuild
  becomes painful; not on the path today.
