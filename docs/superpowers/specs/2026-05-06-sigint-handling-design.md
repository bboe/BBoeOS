# SIGINT handling — design

Date: 2026-05-06
Status: Draft

## Motivation

A user program executing in ring 3 cannot currently be interrupted from the
keyboard. The shell catches Ctrl+C as a cooked byte (`0x03`) on its own input
stream, but once the shell has exec'd a child via `SYS_SYS_EXEC`, the child
owns the keyboard. A program that enters a tight loop (`while (1) {}`) or
parks on a wait that will never complete (a network read with no inbound
traffic, a `MIDI_IOCTL_DRAIN` waiting for the kernel ring to empty) hangs the
OS until reboot. The only escape today is the QEMU monitor.

This spec adds SIGINT delivery — Linux-shaped, single-signal — so that:

- A runaway user loop is killed within ~1 ms by the IRQ 0 timer tick.
- Programs blocked in cooperative kernel waits abort early with `-EINTR` and
  return to ring 3 where the signal is delivered.
- Programs that need graceful cleanup (close a socket, flush an OPL queue)
  may register a handler that runs at ring 3 before the program dies — or
  that lets the program continue running.
- The shell installs `SIG_IGN` so its own `Ctrl+C` does not kill it.

The mechanism (deflect the IRET frame on the way back to user mode) is the
same primitive that an async timer-callback feature (deferred to a separate
PR) will reuse, so this work is intentionally a stepping stone.

Out of scope: any signal other than SIGINT, signal masks beyond
"current-signal-blocked-while-handler-runs", `siginfo_t`/`ucontext_t`,
`sigaltstack`, real-time signals, signal queueing beyond a single
pending bit. POSIX function naming (`signal`, `SIG_DFL`, `SIG_IGN`,
`SIGINT`, `EINTR`) is preserved so the API is familiar.

## Architecture

Two axes, after Linux:

- **Detection.** The PS/2 IRQ 1 cooked-byte path and the COM1 serial read
  path see byte `0x03` and set a per-program `pending_sigint` flag. Setting
  the flag is the only thing that happens in interrupt context.
- **Delivery.** Every kernel-to-user transition — IRQ handler `iretd`,
  `INT 30h` syscall `iretd` — runs a tail check of `pending_sigint`. If
  set, dispatch fires.

Dispatch consults the per-program `sigint_handler` slot:

| Slot value | Meaning | Action |
|---|---|---|
| `SIG_DFL` (0) | default — no handler | tear down the program PD, jump to `shell_reload` |
| `SIG_IGN` (1) | ignore | clear `pending_sigint`, return to user code |
| user-virt addr (≥ `PROGRAM_BASE`) | handler registered | build sigcontext on user stack, redirect IRET to handler |

When a handler is registered, the kernel rewrites the IRET frame so the
return resumes at the handler's address. The handler runs as a normal C
function, returns into the vDSO trampoline, and the trampoline executes
`SYS_SYS_SIGRETURN` to restore the original register state. While a handler
is on the stack, `in_sigint_handler = 1` blocks re-entry; sigreturn clears
the flag and re-checks `pending_sigint` so a `Ctrl+C` arriving during
handler execution is delivered immediately on resume.

Cooperative interruption: blocking syscalls (`fd_read_console` polling,
`MIDI_IOCTL_DRAIN` `sti+hlt` loop, future blocking I/O) check
`pending_sigint` between iterations and bail out with `CF=1, EAX=-EINTR`.
The syscall epilogue's same `pending_sigint` check then fires dispatch.
Genuinely uninterruptible waits (FDC sector wait — IRQ 6 is guaranteed)
are left alone; the signal lands when the wait completes. This matches
Linux's `TASK_UNINTERRUPTIBLE` (D-state) — uncommon today and acceptable
to defer.

## Kernel-side state

Six new BSS bytes (one dword + two bytes) tracking the current program's
signal state. Only one program runs at a time today, so a single global
slot suffices; the slot is zeroed on every program transition so it
behaves as if it were per-program (handler addresses are user-virt and
only valid in the active PD anyway):

```
sigint_handler        dd 0      ; 0=SIG_DFL, 1=SIG_IGN, else user-virt address
pending_sigint        db 0      ; set in IRQ context, consumed at delivery
in_sigint_handler     db 0      ; 1 while a signal frame is on the user stack
```

`program_enter` (`src/arch/x86/entry.asm`) zeroes all three on every program
load (boot, `sys_exec` handoff, `sys_exit` shell reload). Each new program
therefore starts in `SIG_DFL` with no pending signal — runaway-program fix
is automatic.

## Detection

### PS/2 path

Hook in `ps2_handle_scancode` (`src/drivers/ps2.c:341`). After the existing
Ctrl+letter cooked-byte computation produces `ascii`, add:

```c
if (ascii == 0x03) {
    pending_sigint = 1;
}
```

The byte is **also** enqueued into the per-fd console ring as today. This
matches Linux behavior under `stty -isig` mode (signal delivery without
swallowing the byte). BBoeOS does not have terminal modes; programs that
want only the signal can ignore the byte, programs that want only the byte
can install `SIG_IGN`.

### Serial path

Hook in `fd_read_console` (`src/fs/fd/console.c:131`) where the serial LSR
read returns a byte. Before returning the byte to the caller, if it is
`0x03`, set `pending_sigint = 1`.

This is poll-driven (only fires when something is reading from the console
fd) — a known limitation. A future move of COM1 to IRQ-driven input would
let the kernel set the flag asynchronously like PS/2 does. Sufficient for v1
because the typical case (a runaway program) eventually pumps IRQ 0 and
delivers via the IRQ epilogue path; the serial-only case (a program parked
on a non-console syscall while the user is on a serial terminal) is rare.

## Delivery

### Sigcontext layout on user stack

When dispatch fires for a registered handler, the kernel reads user ESP
from the IRET frame, decrements it by 48 bytes, writes the following
struct, and rewrites the IRET frame:

```
offset  field
------  -----
+0      trampoline_addr      ; vDSO __kernel_sigreturn (handler's return address)
+4      signum               ; = 2 (SIGINT) — handler's int argument
+8      saved_eip            ; original interrupt-frame EIP
+12     saved_eflags
+16     saved_esp            ; original user ESP before signal frame
+20     saved_eax
+24     saved_ecx
+28     saved_edx
+32     saved_ebx
+36     saved_ebp            ; pushad order minus its ESP slot (saved separately at +16)
+40     saved_esi
+44     saved_edi
```

Total: 48 bytes (12 dwords). Seven register slots, not eight: pushad's
own ESP slot is redundant because we save the user ESP explicitly at
offset +16.

After the write, the kernel rewrites the IRET frame:

- `EIP ← sigint_handler`
- `ESP ← user_esp - 48`
- `EFLAGS` ← unchanged (handler runs with same flags as interrupted code)
- `CS / SS` ← unchanged (still ring-3)

It also sets `in_sigint_handler = 1`, clears `pending_sigint`, and `iretd`s.
The handler runs as a normal C function with `signum` on its stack at
`[ESP+4]`, returns via standard `ret` which pops `trampoline_addr` into EIP.

### IRET-frame rewrite locations

Two epilogue points need the dispatch check:

1. **IRQ handlers** (`src/arch/x86/entry.asm`) — IRQ 0 (PIT), IRQ 1 (PS/2),
   IRQ 4 (serial), IRQ 5 (SB16), IRQ 6 (FDC). All currently end in
   `popad / iretd`. Insert before `iretd`: if interrupted CS is user code
   AND `pending_sigint != 0` AND `in_sigint_handler == 0`, jump to
   `signal_dispatch`. Otherwise `iretd` as before.

2. **Syscall handler** (`src/arch/x86/syscall.asm`) — the `.iret_cf` /
   `.iret_cf_eax` / `.iret_no_cf` exits from `INT 30h`. Same check inserted
   before `iretd`.

The dispatch routine (in a new `src/arch/x86/signal.asm` or in C in
`src/kernel/signal.c`) reads `sigint_handler`:

- `0` (SIG_DFL): `mov esp, kernel_stack_top`, call `address_space_destroy`,
  jump to `shell_reload`. Same teleport pattern used by `program_enter`'s
  OOM cleanup (`entry.asm:530-548`). Optionally print `^C\n` to console
  before teardown for UX.
- `1` (SIG_IGN): clear `pending_sigint`, restore registers, `iretd`.
- otherwise: build the sigcontext, rewrite IRET frame, `iretd`.

### Re-entry

While `in_sigint_handler == 1`, the epilogue check skips dispatch — the
flag stays pending. This matches Linux's default of blocking the same
signal during its handler. Re-delivery happens on `SYS_SYS_SIGRETURN`,
which clears `in_sigint_handler` and re-runs the dispatch check before
its own `iretd`.

### Stack overflow

The handler runs on the same stack as the interrupted code. The 48-byte
sigcontext plus the handler's own frame eat into the 64 KB user stack. If
the user is near the stack guard (PTEs `0xFF7E0..0xFF7EF` are unmapped),
the kernel's write of the sigcontext faults — the kernel detects the page
fault on its `[esi]` write and converts to a kill (same path as SIG_DFL).
`sigaltstack` is out of scope; existing user programs use a fraction of
their stack.

## Sigreturn

### vDSO trampoline

Add a 7-byte trampoline at a fixed offset in the vDSO page (user-virt
`0x10000`). Suggested name: `__kernel_sigreturn`, suggested offset:
`VDSO_VIRT + 0x100` (well past the existing `FUNCTION_*` table).

```nasm
__kernel_sigreturn:
        mov     eax, SYS_SYS_SIGRETURN
        int     0x30
        ; never returns
```

The trampoline address is exposed to the kernel as a constant in
`src/include/constants.asm`; the kernel writes it into the sigcontext
unconditionally. Userland programs do not reference the trampoline
directly — `signal()` and the handler return path are the only entry
points.

### `SYS_SYS_SIGRETURN`

The handler's `ret` pops `trampoline_addr` into EIP and increments user
ESP by 4. The trampoline immediately enters the kernel via `INT 30h`. At
syscall entry, user ESP points at `signum` (offset +4 of the original
sigcontext layout); the saved registers begin at user ESP + 4
(`saved_eip`).

The syscall:

1. Reads `saved_eip`, `saved_eflags`, `saved_esp`, `saved_eax..edi` from
   `[user_esp + 4 .. user_esp + 44]`.
2. Validates: `saved_eip` is in user range (`PROGRAM_BASE ≤ x <
   KERNEL_VIRT_BASE`); `saved_esp` is in user range. On failure, kill the
   program (same as `SIG_DFL` — handler corrupted its frame).
3. Rewrites the syscall's IRET frame: `EIP ← saved_eip`,
   `EFLAGS ← saved_eflags`, `ESP ← saved_esp`, and the eight pushad slots
   from `saved_eax..saved_edi`.
4. Clears `in_sigint_handler`.
5. Re-checks `pending_sigint`. If set, fires dispatch immediately (so a
   `Ctrl+C` arriving during handler execution is honored without waiting
   for the next IRQ).
6. `iretd`.

The syscall does not return through the standard `.iret_cf` path because
the IRET frame has been rewritten; control resumes at `saved_eip`, not
at the trampoline's `int 0x30` follow-up.

## Cooperative interruption

Three blocking syscall paths gain a `pending_sigint` check in their wait
loop. Each bails with `CF=1, EAX = (uint32_t)-EINTR`. The propagated value
is `0xFFFFFFFC` (= `-4`); libc wrappers translate to `errno = EINTR` (see
"libc surface" below).

### `fd_read_console` (`src/fs/fd/console.c:131`)

```c
asm("sti");
while (1) {
    if (pending_sigint) {
        return /* CF set, AX = 0xFFFC */;
    }
    byte = ps2_getc();
    if (byte != '\0') break;
    if ((kernel_inb(0x3FD) & 0x01) != 0) {
        byte = kernel_inb(0x3F8);
        break;
    }
}
```

The check is placed before the polling iteration so a Ctrl+C arriving via
PS/2 IRQ 1 during the previous spin sees the flag on the next loop top.

### `MIDI_IOCTL_DRAIN` (`src/fs/fd/midi.c`)

The `.fd_ioctl_midi_drain_wait` `sti+hlt` loop currently reads
`midi_head/tail` and waits for them to converge. Insert a
`pending_sigint` check after the `sti; hlt; cli` wakeup:

```nasm
.fd_ioctl_midi_drain_wait:
        cli
        cmp byte [_g_pending_sigint], 0
        jne .fd_ioctl_midi_drain_eintr
        mov al, [_g_midi_head]
        cmp al, [_g_midi_tail]
        je  .fd_ioctl_midi_drain_done
        sti
        hlt
        jmp .fd_ioctl_midi_drain_wait
.fd_ioctl_midi_drain_eintr:
        sti
        mov eax, 0xFFFFFFFC
        stc
        ret
```

### Future blocking waits

Any new `sti+hlt`-based wait inherits the same pattern: check
`pending_sigint` before each `hlt`, bail with `-EINTR` when set. Document
this as a kernel-coding convention in `docs/architecture.md` after the PR
lands.

### Uninterruptible waits

`fd_read_floppy` / FDC sector wait: IRQ 6 is guaranteed by the FDC
state machine on any in-flight command, so the wait will terminate
without help. The `Ctrl+C` lands when the wait completes and the
syscall epilogue runs. This matches Linux `TASK_UNINTERRUPTIBLE`
behavior. No change needed.

## Default kill path

When `sigint_handler == SIG_DFL` and dispatch fires:

```nasm
signal_dispatch_kill:
        mov     esp, kernel_stack_top
        ; Optional: print ^C\n before teardown
        mov     al, '^'
        call    put_character
        mov     al, 'C'
        call    put_character
        mov     al, 0x0A
        call    put_character
        mov     eax, [current_pd_phys]
        call    address_space_destroy
        mov     dword [current_pd_phys], 0
        jmp     shell_reload
```

This is the same teardown sequence as the OOM path in `program_enter`
(`entry.asm:530-548`); both are valid because `kernel_idle_pd` has the
kernel direct map and the kmap window, so all kernel data is reachable
without the user PD. `address_space_destroy` walks user PDEs, frees user
pages, frees PTs, frees the PD. `shell_reload` rebuilds the shell.

The signal source (IRQ vs syscall epilogue) does not matter for the kill
path because we teleport to a known kernel ESP and fall through the
shell-reload entry point.

## Shell integration

`src/c/shell.c` calls `signal(SIGINT, SIG_IGN)` at startup. Effect:

- Ctrl+C still sets `pending_sigint`.
- IRQ epilogue dispatch sees `SIG_IGN`, clears `pending_sigint`, returns.
- The `0x03` byte still arrives in the shell's console reads — the shell's
  existing line editor can choose to display `^C\n` and reset the input
  buffer (small UX touch; not strictly required).

When the shell `sys_exec`s a child, `program_enter` zeroes the per-program
state, so the child starts with `SIG_DFL`. Killable by default — runaway
fix.

## API surface

### asm constants (`src/include/constants.asm`)

```nasm
%assign SYS_SYS_SIGNAL    0xF5     ; alphabetical fit in F0..F4 group
%assign SYS_SYS_SIGRETURN 0xF6
%assign SIGINT            2        ; matches Linux
%assign SIG_DFL           0
%assign SIG_IGN           1
%assign EINTR             4        ; matches Linux
```

### `SYS_SYS_SIGNAL` — register handler

| Reg | Direction | Meaning |
|---|---|---|
| `EBX` | in | signum (must be `SIGINT`; CF set on others) |
| `ECX` | in | handler — `SIG_DFL`, `SIG_IGN`, or user-virt address ≥ `PROGRAM_BASE` |
| `EAX` | out | previous handler value |
| CF | out | clear on success; set on bad signum or out-of-range handler |

Handler-address validation: `ECX = 0`, `ECX = 1`, or `PROGRAM_BASE ≤ ECX <
KERNEL_VIRT_BASE`. The kernel does not verify the address points to
executable code; a bogus handler that triggers an exception kills the
program via the existing exception path.

### `SYS_SYS_SIGRETURN` — restore from sigcontext

| Reg | Direction | Meaning |
|---|---|---|
| (none) | | reads sigcontext from user stack at `[user_esp + 4]` |

Never returns in the normal sense: it rewrites its own IRET frame and
resumes the saved EIP. On validation failure, kills the program (no
return to caller).

### libc surface (`tools/libc/`)

Add `tools/libc/include/signal.h`:

```c
#ifndef _SIGNAL_H
#define _SIGNAL_H

typedef void (*sighandler_t)(int);
typedef volatile int sig_atomic_t;

#define SIG_DFL ((sighandler_t)0)
#define SIG_IGN ((sighandler_t)1)
#define SIG_ERR ((sighandler_t)-1)

#define SIGINT 2

sighandler_t signal(int signum, sighandler_t handler);

#endif
```

Add `tools/libc/signal.c`:

```c
sighandler_t signal(int signum, sighandler_t handler) {
    /* INT 30h, AH = SYS_SYS_SIGNAL, EBX = signum, ECX = handler.
     * Returns previous handler in EAX, CF set on failure → SIG_ERR. */
    /* asm body elided in spec — see writing-plans for the asm shim. */
}
```

Add `EINTR` to `tools/libc/include/errno.h` if not already present. Wrap
`read`, `ioctl`, and any future blocking syscalls so that `CF=1` with
`EAX = 0xFFFFFFFC` translates to a return value of `-1` and
`errno = EINTR`.

## Handler reentrancy expectations

Signal handlers fire at arbitrary user-code instructions, so the same
async-signal-safety rules as POSIX apply. The standard pattern is:

```c
volatile sig_atomic_t got_sigint = 0;
void on_sigint(int s) { (void)s; got_sigint = 1; }

int main(void) {
    signal(SIGINT, on_sigint);
    while (running) {
        if (got_sigint) {
            printf("interrupted\n");
            cleanup();
            break;
        }
        do_work();
    }
}
```

The handler does the minimum (a single flag write, atomic on x86) and
returns. The main loop sees the flag at its next check point and does
the actual work in a context where stdio and heap are safe.

Per-function safety in BBoeOS libc:

| Function | Safe from handler? | Reason |
|---|---|---|
| `write(fd, buf, n)` | yes (visual interleaving possible but no crash) | direct syscall; kernel ANSI parser state may interleave |
| `_exit` / direct `SYS_SYS_EXIT` | yes | kills program; no shared state to corrupt |
| `signal` | yes | registration syscall is idempotent |
| `printf` / `fprintf` / `puts` | **no** (visual + parser interleave) | kernel-side ANSI parser is stateful across writes |
| `malloc` / `free` / `fopen` / `fclose` | **no** (heap corruption) | global heap state |
| `read` | conditional | OK in isolation; not on an fd a main-thread `read` is parked on |

## Testing

### Manual

- **Runaway loop, PS/2.** Build a program containing `int main(void) {
  while (1) {} }`, place it on the disk image, run from shell, press
  Ctrl+C on the QEMU window. Expect: shell prompt returns within a frame
  or two (≤ 1 ms after Ctrl+C, plus shell reload time).
- **Runaway loop, serial.** Same program, run via `qemu -serial stdio`,
  send `^C` on the serial input. Expected outcome in v1: **does not
  fire**. Serial detection only happens inside `fd_read_console`, which
  the runaway program is not calling. PS/2 detection runs from IRQ 1
  regardless of what user code is doing, so a Ctrl+C on the QEMU window
  works; serial users on a wedged program have no recourse until a
  future PR moves COM1 to IRQ-driven input. Document this limitation in
  `docs/architecture.md`.
- **Handler runs.** A program that registers a handler, writes a marker,
  spins until a `volatile` flag is set by the handler, then prints "got
  it" and exits. Press Ctrl+C; expect the marker, then "got it", then
  shell prompt.
- **Drain interrupted.** Run a program that opens `/dev/midi`, queues a
  long sequence with delays, calls `MIDI_IOCTL_DRAIN`. While drain is
  blocked, press Ctrl+C. Expect: drain returns `-1 / errno = EINTR`,
  program either exits (default) or its handler runs and decides what
  to do.
- **Shell ignores Ctrl+C.** At a shell prompt with no child running,
  press Ctrl+C. Expect: shell continues running; input buffer optionally
  resets; no program death.

### Automated

`tests/test_programs.py` already drives QEMU via a serial fifo and waits
for the shell prompt. Two new test programs and entries:

- `tests/sigint_handler_test.c` — registers handler, sets flag, exits
  cleanly. Test driver sends `\x03` on the serial input mid-spin, expects
  output marker + handler-confirms marker + shell prompt.
- `tests/sigint_runaway_test.c` — `while (1) {}`. Test driver sends
  `\x03`, expects shell prompt within a bounded time (e.g., 500 ms).

### Regression

`tests/test_programs.py` and `tests/test_asm.py` should pass with no
behavioral changes for any existing program. The cooperative-interruption
hooks add a single conditional branch on the no-signal-pending path —
zero functional change when `pending_sigint == 0`.

## Risks and limitations

- **Stack pressure.** The 48-byte sigcontext plus handler frame can push
  a near-overflowing user stack into the guard page. Failure mode is a
  page fault that the kernel converts to program kill. Documented; no
  mitigation in v1.
- **Single signal.** Only `SIGINT` is supported. Adding `SIGTERM`,
  `SIGSEGV`, `SIGCHLD` later requires extending the per-program state
  to a small array indexed by signum. The IRET-rewrite mechanism already
  generalizes.
- **No queueing.** A second Ctrl+C arriving while one is already pending
  is coalesced (single bit). Linux does the same for non-realtime
  signals; not a regression.
- **Serial Ctrl+C is poll-driven.** Only fires when something is reading
  the console fd. A runaway program over serial cannot be killed in v1.
  A future move of COM1 to IRQ-driven input fixes this.
- **Cooperative-only interruption.** Programs blocked in
  `TASK_UNINTERRUPTIBLE`-equivalent waits (FDC sector wait) miss the
  signal until the wait completes. Same as Linux D-state. No mitigation
  in v1.

## Implementation footprint

| Area | Files | Approx. LOC |
|---|---|---|
| State + reset | `src/arch/x86/entry.asm` (`program_enter`), new BSS | ~10 |
| Detection | `src/drivers/ps2.c`, `src/fs/fd/console.c` | ~10 |
| Dispatch routine | new `src/arch/x86/signal.asm` (or `.c`) | ~120 |
| IRQ epilogue checks | `src/arch/x86/entry.asm` (5 IRQ handlers) | ~30 |
| Syscall epilogue check | `src/arch/x86/syscall.asm` (3 iret exits) | ~15 |
| `SYS_SYS_SIGNAL` | `src/arch/x86/syscall.asm` | ~30 |
| `SYS_SYS_SIGRETURN` | `src/arch/x86/syscall.asm` | ~50 |
| vDSO trampoline | wherever the vDSO page is initialised | ~10 |
| Cooperative checks | `src/fs/fd/console.c`, `src/fs/fd/midi.c` | ~20 |
| libc | `tools/libc/include/signal.h`, `tools/libc/signal.c`, `tools/libc/include/errno.h`, wrappers | ~50 |
| Shell hookup | `src/c/shell.c` | ~5 |
| Constants | `src/include/constants.asm` | ~10 |
| Tests | `tests/sigint_*.c`, entries in `tests/test_programs.py` | ~80 |
| Docs | `docs/syscalls.md`, `docs/architecture.md` | ~30 |

Total: ~470 lines of new code + tests + docs.
