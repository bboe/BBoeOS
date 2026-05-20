# 2026-05-19 — tree reorg: kernel/, user/, ports/, tools/

Reorganize the repository so the privilege boundary between kernel
code and userspace code is visible in the top-level tree, and so
upstream-wrapping ports (Doom today, future ports tomorrow) have a
distinct home from our own in-tree userland.  No functional change —
this is a pure mechanical rename that lands before the shared-libc
work so libc/ lands cleanly in its new home.

## Motivation

The current layout has the wrong sign:

- `src/c/*.c` is **userspace** (shell, ls, cat, sort, pipe_*) — the
  name suggests "the C portion of the kernel."
- `src/{fs,net,drivers,syscall,memory_management}/*.c` is **kernel** C
  — the kernel has plenty of C now too.
- `make_os.sh` encodes the split as
  `find src -name '*.c' -not -path 'src/c/*'`, which is a smell —
  the file path alone should answer "is this kernel or user?"
- `src/vdso/` is userspace (the shared blob mapped into every program)
  but lives next to kernel code.
- `tools/` is a grab bag of host-side Python (ccld, ccar,
  generate_syscalls_h, wrap_md), userspace target code (`libc/`,
  `doom/`), and port-specific build recipes (`build_doom.py`,
  `fetch_doom.sh`, `install_doom.sh`, `record_doom.py`,
  `fetch_chocolate_opl.sh`).  Three different charters under one
  directory.

A new contributor reading `ls` should see the ring boundaries.
Linux's tree doesn't help here (Linux deliberately ships almost no
userland — see CLAUDE-conversation notes); the projects BBoeOS most
resembles (Plan 9, xv6, minix) each invented their own conventions.
This spec picks one and applies it.

## Target layout

```
kernel/                  # ring 0 — was src/, minus userspace dirs
  arch/x86/
  drivers/
  fs/
  include/               # shared kernel headers + constants.asm
  memory_management/
  net/
  syscall/

user/                    # ring 3, our code, in-tree
  programs/              # was src/c/         (shell, ls, cat, sort, pipes, …)
  libc/                  # was tools/libc/    (shared libc, expanded later)
  vdso/                  # was src/vdso/      (until libc subsumes it)
  static/                # was static/        (asm test corpus for self-hosted assembler)

ports/                   # ring 3 — glue around upstream third-party code
  doom/                  # was tools/doom/ + tools/{build,fetch,install,record}_doom*
                         # + tools/fetch_chocolate_opl.sh

tools/                   # host-side build/dev tooling ONLY
  ccld.py, ccar.py, generate_syscalls_h.py, gen_vdso_pointers.py,
  wrap_md.py, record_demo.py, calibrate_bigbss.py,
  measure_kernel_ports.sh, fetch_wad.sh

third_party/             # upstream sources, untouched
  (fetched on demand by ports/doom/fetch_*.sh)

tests/                   # unchanged — tests cross the boundary by design
docs/                    # unchanged
archive/                 # unchanged
add_file.py, cc.py, make_os.sh   # unchanged — repo-root entrypoints

cc/                      # cc.py package — unchanged
```

## Why three siblings (`kernel/`, `user/`, `ports/`) and not nested?

- **Top-level beats `src/{kernel,user,ports}/`.**  The `src/` prefix
  adds nothing when essentially everything in the repo is source.
  Top-level reads better and shortens every path in build scripts,
  tests, and CI matrix entries.
- **`ports/` separate from `user/`** because the lifecycles differ.
  `user/programs/` is "our code, evolves with the OS."
  `ports/doom/` is "glue against an upstream snapshot, evolves with
  the upstream."  Mixing them obscures what's locally maintained vs.
  what tracks an external project.  Mirrors BSD `ports/` and Gentoo
  `portage` conventions.
- **`third_party/` separate from both** so upstream stays pristine and
  diffable against the original.  `ports/doom/` holds *our* glue;
  `third_party/doomgeneric/` holds *their* untouched source.

## File-by-file move table

### `src/` → `kernel/` (kernel subtrees only)

| Old path                       | New path                          |
|--------------------------------|-----------------------------------|
| `src/arch/x86/`                | `kernel/arch/x86/`                |
| `src/drivers/`                 | `kernel/drivers/`                 |
| `src/fs/`                      | `kernel/fs/`                      |
| `src/include/`                 | `kernel/include/`                 |
| `src/memory_management/`       | `kernel/memory_management/`       |
| `src/net/`                     | `kernel/net/`                     |
| `src/syscall/`                 | `kernel/syscall/`                 |

### `src/` → `user/` (userspace subtrees)

| Old path        | New path           |
|-----------------|--------------------|
| `src/c/`        | `user/programs/`   |
| `src/vdso/`     | `user/vdso/`       |

### Root → `user/`

| Old path   | New path         |
|------------|------------------|
| `static/`  | `user/static/`   |

### `tools/` → `user/libc/`

| Old path        | New path        |
|-----------------|-----------------|
| `tools/libc/`   | `user/libc/`    |

### `tools/` → `ports/doom/`

| Old path                          | New path                          |
|-----------------------------------|-----------------------------------|
| `tools/doom/`                     | `ports/doom/src/` (or flat — TBD) |
| `tools/build_doom.py`             | `ports/doom/build.py`             |
| `tools/fetch_doom.sh`             | `ports/doom/fetch.sh`             |
| `tools/install_doom.sh`           | `ports/doom/install.sh`           |
| `tools/record_doom.py`            | `ports/doom/record.py`            |
| `tools/fetch_chocolate_opl.sh`    | `ports/doom/fetch_chocolate.sh`   |

Open: keep the `_doom` suffix or rename to `build.sh`/`fetch.sh`
inside the per-port directory.  Per-port directories make the suffix
redundant; recommend dropping it.

### `tools/` — stays put

`ccld.py, ccar.py, generate_syscalls_h.py, gen_vdso_pointers.py,
wrap_md.py, record_demo.py, calibrate_bigbss.py,
measure_kernel_ports.sh, fetch_wad.sh` — all host-side or
cross-cutting.  Note `fetch_wad.sh` is doom-adjacent (fetches the WAD
the user runs Doom against, not the source); arguably moves to
`ports/doom/`.  Open question.

## Path fixups (the actual work)

Every reference to an old path needs updating.  Sources, by category:

### `make_os.sh`

- `find src -name '*.c' -not -path 'src/c/*'` → `find kernel -name '*.c'`
- `find src -name '*.c'` (in the userspace cc.py loop) → `find user/programs -name '*.c'`
- All NASM `-i` paths:
  `-i src/include/ -i src/ -i src/arch/x86/ -i src/arch/x86/boot/`
  → `-i kernel/include/ -i kernel/ -i kernel/arch/x86/ -i kernel/arch/x86/boot/`
- VDSO build paths: `src/vdso/vdso.asm` → `user/vdso/vdso.asm`,
  `build/vdso.bin` / `build/vdso.map` / `build/libbboeos`
  (kept under `build/` — no rename needed there).
- Output paths (`kernel.bin`, `boot.bin`, drive image) — unchanged.

### `add_file.py`

- Search references: `static/` → `user/static/`, `src/` → as
  appropriate.  No semantic changes.

### `tools/*.py`

- `ccld.py`, `ccar.py`, `gen_vdso_pointers.py` — check for hardcoded
  `src/` references.
- `generate_syscalls_h.py` — reads `src/include/constants.asm`, must
  update to `kernel/include/constants.asm`.
- `build_doom.py` → `ports/doom/build.py` — paths to `tools/libc/`,
  `tools/doom/`, `third_party/` all change.
- `wrap_md.py` — unaffected (operates on arg paths).

### `cc.py` / `cc/`

- Check `cc/cli.py` and any include-path resolution for `src/include/`
  references → `kernel/include/`.
- cc.py's `%include` resolution is relative to the source file
  (CLAUDE.md notes this), so the assembler test corpus moving from
  `static/` to `user/static/` works as long as the test driver passes
  the right base path.

### `tests/`

- `tests/test_asm.py` — `static/` references → `user/static/`.
- `tests/test_bboefs.py`, `tests/test_programs.py` — any hardcoded
  source paths.
- `tests/unit/test_libbboeos.py`, `tests/test_libbboeos_qemu.py` —
  `tools/libc/` references → `user/libc/`.
- All test_*.py files: scan for `'src/'`, `'tools/'`, `'static/'`
  string literals.

### `.github/workflows/`

- Path filters (`paths:`/`paths-ignore:`) for each job — `src/**` →
  `kernel/**` + `user/**`, `tools/**` → split by destination.
- The `test_pipeline` matrix entry mentioned in memory — verify path
  filters still trigger for the right files.

### Documentation

- `CLAUDE.md` — the entire "File Structure" section and most code
  examples reference `src/`.  Bulk path rewrite.
- `docs/architecture.md`, `docs/memory_map.md`,
  `docs/file_structure.md`, `docs/syscalls.md` — full sweep for
  `src/`, `tools/libc/`, `tools/doom/`, `static/` references.
- `README.md` — likely a few references.

### NASM `%include` directives

- Every `%include "..."` inside `kernel/` (was `src/`) resolves
  against the `-i` paths set in `make_os.sh`, so as long as those
  flags are updated, the includes themselves don't need editing.
- Cross-tree includes (e.g. `user/vdso/vdso.asm` `%includ`ing
  `kernel/include/constants.asm`) need `-i` flags pointing at
  `kernel/include/` from the user-side build commands.
  Spec: every NASM invocation gets `-i kernel/include/` so shared
  constants remain accessible from both rings.

## Sequencing

One mechanical PR.  Splitting the rename across multiple commits
within the PR is fine if it helps review:

1. **Commit 1 — `git mv` only**, no path-fixup edits.  Build is
   broken at this point but the diff is pure renames and reviewable.
2. **Commit 2 — path fixups** across `make_os.sh`, `tools/*.py`,
   `tests/*.py`, `.github/workflows/*.yml`, docs.  Build comes back.
3. **Commit 3 (optional) — strip the `_doom` suffix** inside
   `ports/doom/` (`build_doom.py` → `build.py` etc.).

Verify the full CI matrix passes locally before the PR per
`feedback_run_full_ci_matrix_locally`.  This touches the build path,
the test drivers, and every NASM include — exactly the
"kernel-architecture-shaped" change that demands the full local
matrix.

## Non-goals

- **No libc work in this PR.**  The shared-libc design
  (`2026-05-19-shared-libc-design.md`, separate spec) lands after.
  Mixing the two PRs would make review harder and bisect impossible
  if something breaks.
- **No vDSO collapse into libc/.**  `user/vdso/` keeps its current
  contents; the libc spec proposes replacing it later.
- **No tests/ reorganization.**  `tests/` stays flat; its test
  drivers cross the kernel/user boundary by design.
- **No `cc/` package move.**  The cc.py package is host-side build
  tooling — could arguably live under `tools/cc/` — but moving a
  Python package changes import paths in many places.  Defer to a
  separate cleanup if desired.

## Open questions

1. **Suffix stripping inside `ports/doom/`** — `build_doom.py` →
   `build.py`?  Recommend yes, the per-port directory makes the
   suffix redundant.  Decided in this spec; defer the diff to
   commit 3.
2. **`tools/fetch_wad.sh` placement** — `tools/` or `ports/doom/`?
   It fetches the user-facing WAD, not source; could be either.
   Recommend `ports/doom/` since Doom is the only consumer.
3. **`memory_management/` rename to `mm/`** — Linux convention is
   `mm/`.  Worth piggy-backing on this rename PR, or keep verbatim?
   Recommend defer (no charter change — pure abbreviation question,
   and `memory_management` follows the project's
   no-abbreviations rule for Python; the kernel C/asm side could go
   either way).
4. **`add_file.py` and `cc.py` at repo root** — both are convenience
   shims.  Stay at root, or move under `tools/`?  Recommend stay —
   they're the documented entry points and breaking those URLs in
   READMEs is more pain than it's worth.
