---
title: Shell survives child kills (synchronous spawn-and-wait)
date: 2026-05-09
status: draft
---

# Shell survives child kills (synchronous spawn-and-wait)

## Summary

Today, every transition between the shell and a user program destroys the
shell's PD. `SYS_SYS_EXIT` and the SIGINT / SIGALRM / exception kill paths all
jump to `shell_reload`, which re-reads `bin/shell` from disk and starts a fresh
shell process. This means the shell has no memory across commands, and any
in-process state (open file descriptors, line history, future config) is lost on
every cycle.

This spec replaces the kill-and-reload model with a synchronous spawn-and-wait
model:

- `sys_exec` keeps the parent's PD alive across the child's run.
- When the child terminates (clean exit, signal-default-kill, or CPU exception),
  the kernel restores the parent's PD and returns to the parent's `int 30h`
  instruction with EAX = POSIX-shaped wait status.
- Recursive exec from a child is rejected with `ERROR_INVALID` — the kernel
  tracks at most one suspended parent.
- The shell loses its kill-and-reload identity; its main loop becomes `read line
  → expand $? → exec → loop`.
- A new shell feature, `$?` argument expansion, surfaces the wait status to
  userland via the existing `echo` program.

`shell_reload` is preserved for the boot path and as a panic-recovery fallback
when the shell itself dies.

## Non-goals

- **No SIGCHLD, no waitpid.** Single-program-at-a-time means the parent is
  always blocked on the spawn syscall — there is no work for SIGCHLD to wake and
  no ambiguity for waitpid to disambiguate. The syscall return value carries the
  wait status directly.
- **No fork.** This spec adds zero general-purpose process model. There is
  exactly one parent (the program that called `sys_exec`) and exactly one child
  at any given moment.
- **No nested exec.** The child cannot itself call `sys_exec`. If it tries, the
  syscall returns `CF=1, AL=ERROR_INVALID`.
- **No preemptive multitasking.** The child runs to completion (or is killed)
  while the parent is suspended; the parent does not run concurrently.

## Architecture

### Two simultaneous live PDs

After this change, the kernel may hold two ring-3-program PDs alive at once: the
parent (typically the shell) and the child. Per-program kernel-side state that
today lives in single-instance globals (`current_pd_phys`, `sigint_handler`,
`alarm_deadline`, `current_program_break`, the `fd_table`, etc.) moves into a
`ProgramState` struct. Two BSS-resident slots (`program_state_a`,
`program_state_b`) hold these structs. A pointer `current_program_state`
indicates the running slot; `parent_program_state` points at the suspended slot
when a child is live, or is null otherwise.

CR3 is switched at exactly two transitions: just before `iretd` into the child
(in `sys_exec`) and just before `iretd` back into the parent (in the new
`child_terminate` routine). `current_program_state` flips at the same points.

### `ProgramState` struct (548 bytes per slot)

Fields are sorted strict alphabetical per project convention. Five bytes of
intra-struct padding around the byte fields are accepted; total kernel BSS
budget for both slots is 1096 bytes.

| Offset | Field                | Size | Source today                                |
|--------|----------------------|------|---------------------------------------------|
| 0x000  | `alarm_deadline`     | 4    | `alarm_deadline` (entry.asm:796)            |
| 0x004  | `alarm_interval`     | 4    | `alarm_interval` (entry.asm:797)            |
| 0x008  | `fd_table`           | 512  | `fd_table[FD_MAX]` (fs/fd.c:177); 8 × 64    |
| 0x208  | `in_signal_handler`  | 1    | `in_signal_handler` (entry.asm:798)         |
| 0x209  | (pad)                | 3    | —                                           |
| 0x20C  | `pd_phys`            | 4    | `current_pd_phys` (entry.asm:781)           |
| 0x210  | `pending_sigalrm`    | 1    | `pending_sigalrm` (entry.asm:799)           |
| 0x211  | `pending_sigint`     | 1    | `pending_sigint` (entry.asm:800)            |
| 0x212  | (pad)                | 2    | —                                           |
| 0x214  | `program_break`      | 4    | `current_program_break` (syscall.asm:781)   |
| 0x218  | `program_break_min`  | 4    | `current_program_break_min` (syscall.asm:782)|
| 0x21C  | `sigalrm_handler`    | 4    | `sigalrm_handler` (entry.asm:802)           |
| 0x220  | `sigint_handler`     | 4    | `sigint_handler` (entry.asm:803)            |
| 0x224  | (end / total)        | —    | 548 bytes                                   |

### State that does NOT move into `ProgramState`

- **Transient PD-build globals** (used only during `program_enter`'s
  PD-construction phase, no live across `iretd`): `last_binary_frame_phys`,
  `user_image_end`, `virt_cursor`, `pending_frame_phys`, `program_fd`,
  `next_handoff_frame_phys`. Stay as kernel-wide globals.
- **Kernel-wide singletons**: `kernel_idle_pd_phys`, `vdso_code_phys`,
  `loading_shell_flag`, `system_ticks`. Stay as kernel-wide globals.
- **New kernel-wide additions**: `parent_iret_frame` (52 bytes — pushad+iret
  save area for the suspended parent), `parent_program_state` (pointer).
- **Removed**: `shell_esp`. Its sole consumer was `sys_exit`'s kernel-stack
  reset, which is replaced by the parent_iret_frame restore in
  `child_terminate`. Boot's call to `shell_reload` does not need it.

## Syscall changes

### `SYS_SYS_EXEC` (0xF1)

Old semantics: tear down parent PD, jump to `program_enter`, never return.

New semantics: block until the child terminates, then return wait status in EAX.
All existing pre-load failure paths (`ERROR_FAULT`, `ERROR_NOT_FOUND`,
`ERROR_NOT_EXECUTE`) keep their CF=1+AL behavior; the parent stays alive on
those paths (already true today, since teardown only happens after vfs_find
succeeds).

Entry guard:

- If `parent_program_state != 0` → CF=1, AL=`ERROR_INVALID`. This catches
  recursive exec from a child, since only the running slot's caller can reach
  `sys_exec`, and a parent is suspended in this case.

On the success path, before any teardown:

1. Snapshot the parent's pushad+iret kernel-stack frame (13 dwords = 52 bytes)
   into `parent_iret_frame` BSS buffer. The child's syscalls will reset ESP to
   `kernel_stack_top` via TSS.ESP0 and would otherwise clobber it.
2. `parent_program_state = current_program_state`.
3. Allocate the child's handoff frame and populate from the parent's user pages
   (existing `.exec_load` body).
4. Initialize the child's slot (whichever of `program_state_a`,
   `program_state_b` is not in use): zero the slot, set handlers to `SIG_DFL`,
   leave `program_break` at 0 for `program_enter` to fill in.
5. `current_program_state = &child_slot`.
6. Switch CR3 to `kernel_idle_pd_phys`, `jmp program_enter`. Parent's PD is not
   destroyed.

### `SYS_SYS_EXIT` (0xF2)

Old: no arguments, all exits implicitly status 0.

New: AL = exit code (0..255). Wait status encoded as `(AL & 0xFF) << 8`. Falls
through to `child_terminate` (below).

libc's `_exit(int)` wrapper grows an int argument; existing call sites are
updated to pass 0 explicitly. Asm programs that issue `int 30h` directly with
`AH = 0xF2` are audited; the kernel masks AL to 0..255 defensively. The disk
image is rebuilt as part of the change, so there is no compatibility window with
old binaries.

### Wait-status encoding (16-bit, POSIX-shaped)

- bits 0..6: signum (0 if exited cleanly, otherwise SIGINT=2, SIGALRM=14, or
  0x7F = "killed by CPU exception").
- bit 7: reserved (always 0; POSIX uses it for "core dumped").
- bits 8..15: exit code (only meaningful when bits 0..6 are 0).

libc gains POSIX-shaped macros in `src/include/wait.h` (or folded into
`libc.h`):

```c
#define WIFEXITED(s)   (((s) & 0x7F) == 0)
#define WIFSIGNALED(s) (((s) & 0x7F) != 0 && ((s) & 0x7F) != 0x7F)
#define WIFCRASHED(s)  (((s) & 0x7F) == 0x7F)
#define WEXITSTATUS(s) (((s) >> 8) & 0xFF)
#define WTERMSIG(s)    ((s) & 0x7F)
```

## Kill paths

All three kill paths converge on a single new routine, `child_terminate`, that
handles parent restoration. Each caller loads EAX = wait status and jumps.

### `child_terminate` (new, in entry.asm or syscall.asm)

1. `cli`.
2. If `parent_program_state == 0` → `jmp shell_reload` (the shell itself died;
   old behavior preserved for this case).
3. Switch CR3 to `kernel_idle_pd_phys`.
4. `address_space_destroy` on the child's PD. Zero the child's `ProgramState`
   slot (incl. its 512-byte `fd_table`).
5. `current_program_state = parent_program_state; parent_program_state = 0`.
6. Switch CR3 to `current_program_state.pd_phys` (parent PD).
7. `mov esp, kernel_stack_top - 52`.
8. Copy `parent_iret_frame` (13 dwords) into `[esp..esp+52]`.
9. Poke wait status into the saved-EAX slot in pushad: `[esp + 28] = wait`.
10. Clear CF in saved EFLAGS: `and dword [esp + 40], ~1`.
11. Run `SIGNAL_TAIL_CHECK` macro — delivers any signal whose pending bit was
    set in the parent's slot during the child's run (e.g., a SIGALRM armed by
    the parent before exec, fired by the PIT during the child's execution).
12. `popad ; iretd`. Lands in the parent's user code at the instruction after
    `int 30h`, with EAX = wait status.

### Path 1: `signal_dispatch_kill` (SIGINT/SIGALRM, default kill)

Today (signal.c lines 60-95): prints `^C\n` or `^A\n`, switches CR3 off the
dying PD, calls `address_space_destroy`, jumps to `shell_reload`.

After: the prelude (print + CR3 swap to kernel_idle_pd) stays. Replace
`address_space_destroy + jmp shell_reload` with: load EAX = `(EDX & 0x7F)` (EDX
already holds signum at entry), `jmp child_terminate`. `child_terminate` itself
does the destroy + restore.

### Path 2: `exc_common` (CPU exception in user code)

Today (idt.asm): prints `EXCnn`, tears down the dying PD, jumps to
`shell_reload`.

After: same prelude (print, CR3 swap, no destroy yet), then load EAX = `0x7F`
(sentinel signum), `jmp child_terminate`.

### Path 3: `program_enter .oom` / disk error during child load

Spawn never started — child never `iretd`'d into ring 3; there is no "wait
status" semantic. Sibling routine `spawn_failed_unwind`:

1. Tear down partial child PD.
2. Zero the child's `ProgramState` slot.
3. `current_program_state = parent_program_state; parent_program_state = 0`.
4. Switch CR3 to parent PD.
5. Restore `parent_iret_frame` to kernel stack, set saved EAX = `ERROR_FAULT`
   and OR bit 0 of saved EFLAGS (set CF).
6. `popad ; iretd`. Parent's `exec()` returns the standard syscall-failure
   shape.

The kernel-side `exec: out of memory` print to COM1 is dropped; surfacing the
error in EAX is sufficient and the parent (shell) decides how loud to be.

### Boot path / shell-itself-died

`shell_reload` keeps its current behavior: re-read `bin/shell` from disk, call
`program_enter`. One required tweak: it must initialize `program_state_a` (the
shell's slot), set `current_program_state = &program_state_a`,
`parent_program_state = 0`, and zero `parent_iret_frame`. This makes
`shell_reload` the canonical "boot a fresh shell" point for both power-on and
shell-crashed-with-no-parent scenarios.

## PIT alarm iteration

Today the PIT IRQ checks a single `[alarm_deadline]` symbol against
`system_ticks`. After the migration to per-program state, the PIT IRQ walks both
alive slots so a parent's alarm fires at wall-clock time even while a child is
running. Pseudocode:

```
for slot in (program_state_a, program_state_b):
    if slot.pd_phys == 0: continue            # slot unused
    deadline = slot.alarm_deadline
    if deadline == 0: continue                # no alarm armed
    if system_ticks < deadline: continue
    slot.pending_sigalrm = 1
    if slot.alarm_interval != 0:
        slot.alarm_deadline = system_ticks + slot.alarm_interval
    else:
        slot.alarm_deadline = 0
```

When the alarm fires for the suspended parent, its `pending_sigalrm` is set but
the running program (the child) does not see the SIGALRM — only its own slot's
bit is checked by `SIGNAL_TAIL_CHECK`. The deferred signal is delivered the
moment the parent resumes, because `child_terminate` runs `SIGNAL_TAIL_CHECK`
before its `popad + iretd`.

## Migration of per-program state access

Every existing access to a per-program global is rewritten to reach through
`current_program_state`. The mechanical pattern is a 2-instruction load:

```
        mov edx, [current_program_state]
        mov eax, [edx + OFFSET_SIGINT_HANDLER]
```

When multiple fields are touched in sequence, the base load happens once.

C-side accesses (in `signal.c`, `fs/fd.c`, paging code that reads
`current_pd_phys`, `sys_break`'s `current_program_break` references) are
rewritten to go through accessor functions or struct field reads. The asm shim
`_g_current_pd_phys` becomes `_g_current_program_state` plus an inline
field-offset add.

`fd_table` in `fs/fd.c` is currently `struct fd fd_table[FD_MAX]` at file scope.
It becomes either a function returning `&current_program_state->fd_table[0]` or,
for the asm consumers in `ps2.c`, the bare `fd_table` symbol resolves indirectly
through `current_program_state`. Implementation plan will pick whichever is
simpler.

`SIGNAL_TAIL_CHECK` (used at every IRQ tail and the new `child_terminate`)
becomes one extra L1 load per fire. Acceptable.

## libc and shell changes

### libc

- `int exec(const char *path)`: returns the wait-status word on a completed
  child run (≥0), or `-error` (e.g., `-ERROR_NOT_FOUND`) on syscall failure.
  Caller sites in `shell.c::try_exec` are audited and updated.
- `void _exit(int status)`: grows a status argument. Compiles to `mov al, status
  ; mov ah, SYS_SYS_EXIT ; int 30h`.
- New `src/include/wait.h` with the POSIX-shaped W*-macros above.

### Shell (`src/c/shell.c`)

- Tracks last wait status in a file-scope int `last_status` (initial 0, before
  any exec).
- After a successful exec, derives a bash-shaped int:
  ```
  last_status_bash = WIFEXITED(s)   ? WEXITSTATUS(s)
                   : WIFSIGNALED(s) ? 128 + WTERMSIG(s)
                   : /* WIFCRASHED */ 128 + 0x7F   /* = 255 */
  ```
- Adds a `$?` substitution pass over the EXEC_ARG region of `BUFFER` (everything
  *after* the first space — the command word is never expanded). Pass runs after
  the first-space split sets `[EXEC_ARG]`, before any dispatch. In-place
  replace; bounds check against `MAX_INPUT` since `$?` (2 chars) → up to 3 chars
  (`255`), so worst-case growth is +1 byte per occurrence.
- The shell's `int vga_fd = open("/dev/vga", O_WRONLY)` line, currently at
  `main()` scope, now opens once at boot and stays valid across commands —
  verify it lives outside the `while(1)` command loop (today it re-opens
  implicitly because the shell is reloaded every cycle).
- The shell stays silent on signal-kill — the kernel's existing `^C\n` / `^A\n`
  print in `signal_dispatch_kill` is the source of truth. Prints uniformly
  across the "shell-as-parent" and "shell-itself-died → reload" cases.
- No `echo` built-in needed — `src/c/echo.c` already exists as a real userland
  program. `echo $?` exercises the substitution end-to-end.

## Error handling summary

| Failure                                       | Path                       | Parent sees                       |
|-----------------------------------------------|----------------------------|-----------------------------------|
| Child path/name invalid                       | `sys_exec` early return    | CF=1, AL=`ERROR_FAULT` / `_NOT_FOUND` / `_NOT_EXECUTE` |
| Recursive exec from child                     | `sys_exec` entry guard     | CF=1, AL=`ERROR_INVALID`          |
| OOM during child PD build                     | `spawn_failed_unwind`      | CF=1, AL=`ERROR_FAULT`            |
| Child clean exit with code N                  | `sys_exit` → `child_terminate` | EAX = `(N & 0xFF) << 8`, CF=0 |
| Child killed by SIGINT/SIGALRM (default)      | `signal_dispatch_kill` → `child_terminate` | EAX = signum (2 or 14), CF=0 |
| Child killed by CPU exception                 | `exc_common` → `child_terminate` | EAX = `0x7F`, CF=0          |
| Shell itself dies (no parent)                 | any kill path              | falls back to `shell_reload` from disk |

## Testing strategy

### New tests

- `tests/programs/exit_status.c`: a tiny program that calls `_exit(N)` where N
  comes from its argv. Test driver runs `exit_status 42`, then `echo $?`,
  expects `42` in output. Repeat for several values.
- Ctrl+C-into-running-program test: drive a long-running program (e.g., `sleep
  60`), send `\x03`, expect `^C` from kernel + `$ ` prompt within a tight
  deadline, then `echo $?` → `130` (= 128 + SIGINT).
- Recursive-exec test: a program that calls `exec("cat")` and prints the return
  value. Test asserts the return is `-ERROR_INVALID`.
- Shell-state-survives test: run a sequence of commands (e.g., `echo a` ; `echo
  b` ; `echo c`) and assert the shell process did not reload between them. The
  test surface is a serial-log marker: the shell prints a `[shell:start]` line
  exactly once on first entry to `main()` (gated by a file-scope `static int
  started = 0`); the test asserts the marker appears exactly once across N
  commands. Simple, deterministic, no new ioctl needed.
- Wait-status round-trip in libc: a userland test that calls `exec("true")` and
  asserts `WIFEXITED(rc) && WEXITSTATUS(rc) == 0`. Then `exec("false")` (a new
  program that `_exit(1)`s) and asserts `WEXITSTATUS == 1`.

### Existing tests

- `tests/test_asm.py`: should keep passing — asm regression suite is agnostic to
  wait status.
- `tests/test_bboefs.py`: same — fs regression.
- `tests/test_programs.py`: existing program runs (cat, ls, doom, etc.) keep
  passing — they all exit cleanly, shell re-prompts the same way. No
  expected-output changes for the unchanged programs, modulo the shell's new
  behavior of staying alive (which is invisible to single- command tests).

### CI matrix coverage

This is a kernel-architecture change (signal paths, IRQ tails, syscall return
path, page-table lifecycle). Per the project memory note about big changes, run
the full CI matrix in `.github/workflows/test.yml` locally before declaring done
— not just asm + bboefs + programs.

## Phasing recommendation

This spec is sized for a single implementation plan but naturally decomposes
into two sequential PRs that can each be reviewed and tested independently. The
implementation plan should reflect this split:

**Phase A — `ProgramState` migration (no behavior change).** Introduce the
`ProgramState` struct with a single slot. Migrate every per-program access (the
table in §"Migration of per-program state access") to go through
`current_program_state`. Keep the kill+reload model intact: every kill path
still jumps to `shell_reload`. The PIT IRQ iterates the single slot (same as
today). All existing tests pass with no expected-output changes. This phase is
mechanical and the win is purely structural — it sets up Phase B without
coupling the migration to the spawn-and-wait semantics.

**Phase B — synchronous spawn-and-wait.** Add the second slot,
`parent_program_state`, `parent_iret_frame`, `child_terminate`,
`spawn_failed_unwind`. Modify `sys_exec` and `sys_exit` per §"Syscall changes".
Update kill paths to dispatch through `child_terminate`. Add the PIT alarm
iteration over both slots. Add the libc `wait.h`, `_exit(int)`, `exec()` return
value change. Add the shell's `$?` expansion and `last_status` tracking. All new
tests in §"Testing strategy" run in this phase.

If Phase B is found to be too large in practice, sub-splitting along the
kill-path / sys_exec boundary is acceptable, but the kernel-side primitives
(child_terminate, parent slot) need to land before the libc / shell changes that
depend on them.

## Open implementation-time considerations

These are NOT design questions — they are decisions deferred to the
implementation plan because they are mechanical and do not affect the
architecture:

- Whether to centralize the `current_program_state`-relative loads into a macro
  (`PS_LOAD reg, OFFSET`) vs leaving them as 2-instruction sequences.
- Whether to migrate `fd_table` access in `fs/fd.c` and `drivers/ps2.c` to go
  through a function or to keep the bare symbol resolved indirectly.
- Whether `wait.h` lives as its own header or inlines into `libc.h`.
- Concrete naming for the new BSS symbols (`parent_iret_frame`,
  `parent_program_state`, `program_state_a`, `program_state_b`) vs more
  POSIX-flavored ones.

Bryce's "no opaque abbreviations" rule applies: no `as_*`, `cur_*`, `ps_*`
shorthands without spelling out.
