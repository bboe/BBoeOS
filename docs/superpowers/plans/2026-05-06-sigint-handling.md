# SIGINT Handling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Linux-shaped SIGINT delivery so a Ctrl+C from PS/2 kills runaway user programs (default), the shell can opt out via `SIG_IGN`, and programs can register a handler that runs at ring 3 with sigreturn-based resume.

**Architecture:** PS/2 IRQ 1 path and `fd_read_console` serial path set a global `pending_sigint` byte. Every kernel-to-user IRET epilogue (5 IRQ handlers + 3 syscall exits) checks the byte and, if set, dispatches to one of {`SIG_DFL` kill via `address_space_destroy + shell_reload`, `SIG_IGN` clear-and-resume, user handler via stack-built sigcontext + vDSO trampoline + `SYS_SYS_SIGRETURN`}. Cooperative interruption: blocking syscalls (`fd_read_console` poll, `MIDI_IOCTL_DRAIN` `sti+hlt`) check `pending_sigint` in their wait loop and bail with `CF=1, AL=ERROR_INTERRUPTED`.

**Tech Stack:** NASM (kernel asm), `cc.py` (kernel/userland C, with inline `asm()` for tight register contracts), clang (libc shim), QEMU i386 (boot + manual + automated test), Python `tests/test_programs.py` (serial-fifo-driven QEMU smoke tests).

**Spec:** `docs/superpowers/specs/2026-05-06-sigint-handling-design.md`

---

## File Structure

**New files:**
- `src/arch/x86/signal.c` — `signal_dispatch_kill`, sigcontext-build helper, `signal_resume_after_handler` (sigreturn restore). Mostly inline `asm()` because IRET-frame manipulation has tight register contracts.
- `tools/libc/include/signal.h` — `sighandler_t`, `sig_atomic_t`, `SIG_DFL`/`SIG_IGN`/`SIG_ERR`, `SIGINT`, `signal()` prototype.
- `tools/libc/signal.c` — userland `signal()` shim around `SYS_SYS_SIGNAL`.
- `tests/sigint_handler_test.c` — end-to-end automated test program.

**Modified:**
- `src/include/constants.asm` — `SYS_SYS_SIGNAL` (0xF5), `SYS_SYS_SIGRETURN` (0xF6), `SIGINT` (2), `SIG_DFL` (0), `SIG_IGN` (1), `ERROR_INTERRUPTED` (8h), `VDSO_SIGRETURN` (vDSO trampoline offset).
- `src/arch/x86/entry.asm` — new BSS slots (`sigint_handler`, `pending_sigint`, `in_sigint_handler`); zero them in `program_enter`; tail-check + dispatch in IRQ 0/1/4/5/6 handler epilogues; vDSO trampoline byte writes during vDSO page init.
- `src/arch/x86/syscall.asm` — tail-check + dispatch in `.iret_cf` / `.iret_cf_eax` / `.iret_no_cf` exits; new `.sys_signal` and `.sys_sigreturn` entries.
- `src/drivers/ps2.c` — set `pending_sigint = 1` in `ps2_handle_scancode` when cooked byte is `0x03`.
- `src/fs/fd/console.c` — set `pending_sigint = 1` when serial poll reads `0x03`; check `pending_sigint` in `fd_read_console` wait loop and bail with `CF=1, AL=ERROR_INTERRUPTED`.
- `src/fs/fd/midi.c` — check `pending_sigint` in `.fd_ioctl_midi_drain_wait` `sti+hlt` loop and bail with `CF=1, AL=ERROR_INTERRUPTED`.
- `src/c/shell.c` — call `signal(SIGINT, SIG_IGN)` at startup.
- `tools/libc/include/errno.h` — add `EINTR` (= 4).
- `tools/libc/errno.c` (or `tools/libc/syscall.c`) — extend `_errno_from_al` to map `ERROR_INTERRUPTED` (8h) → `EINTR`.
- `tools/libc/Makefile` — add `signal.c` to `C_SRCS`.
- `tests/test_programs.py` — add `sigint_handler` entry that drives the new test program via the serial fifo.
- `docs/syscalls.md` — add `SYS_SYS_SIGNAL` and `SYS_SYS_SIGRETURN` rows; add `ERROR_INTERRUPTED`.
- `docs/architecture.md` — short subsection documenting SIGINT delivery model + serial-runaway limitation.
- `docs/CHANGELOG.md` — Unreleased entry.

---

## Phase 1 — Foundation

### Task 1: Add asm + libc constants

**Files:**
- Modify: `src/include/constants.asm`
- Modify: `tools/libc/include/errno.h`

- [ ] **Step 1: Add kernel constants in alphabetical/numeric order**

In `src/include/constants.asm`, find the existing `ERROR_*` block (lines ~13-19) and insert in numeric order:

```nasm
%assign ERROR_INTERRUPTED  08h     ; Cooperative-interrupt return (SIGINT) — maps to EINTR in libc
```

Find the existing `SYS_SYS_*` block (lines ~149-153 area, after `SYS_SYS_SHUTDOWN`) and add (numeric order — these slot in after the existing F0..F4):

```nasm
%assign SYS_SYS_SIGNAL    0F5h    ; EBX = signum (SIGINT only); ECX = handler (SIG_DFL/SIG_IGN/user-virt); EAX = previous handler; CF on bad signum / handler
%assign SYS_SYS_SIGRETURN 0F6h    ; restore from sigcontext on user stack; never returns to caller
```

Add a new SIGNAL section near the end (alphabetical with other groupings is fine — pick a spot consistent with surrounding style):

```nasm
;;; Signal numbers (POSIX-numbered).  Currently only SIGINT is delivered.
%assign SIGINT 2

;;; signal() handler sentinels (POSIX-valued).
%assign SIG_DFL 0
%assign SIG_IGN 1
```

Add the vDSO trampoline offset alongside the existing `VDSO_VIRT` constant:

```nasm
%assign VDSO_SIGRETURN_OFFSET 0100h           ; trampoline lives at VDSO_VIRT + 0x100
```

- [ ] **Step 2: Add libc EINTR**

In `tools/libc/include/errno.h`, add (in numeric order, between EIO and EBADF):

```c
#define EINTR    4
```

- [ ] **Step 3: Build and verify clean compile**

Run: `./make_os.sh`
Expected: builds `drive.img` with no errors. No behavioral change yet.

- [ ] **Step 4: Commit**

```bash
git add src/include/constants.asm tools/libc/include/errno.h
git commit -m "kernel+libc: SIGINT/SYS_SIGNAL constants + EINTR/ERROR_INTERRUPTED"
```

---

### Task 2: Add per-program signal state and zero on program_enter

**Files:**
- Modify: `src/arch/x86/entry.asm`

- [ ] **Step 1: Add BSS slots**

In `src/arch/x86/entry.asm`, find the existing program-state BSS block (search for `current_pd_phys` or `current_program_break` to locate the area) and add nearby:

```nasm
;;; SIGINT delivery state.  One global slot suffices because only one
;;; user program runs at a time — program_enter zeroes the lot on every
;;; load so it behaves as if it were per-program.  sigint_handler is a
;;; user-virt address (or SIG_DFL=0 / SIG_IGN=1); the address is only
;;; valid in the active PD, hence the zero-on-transition rule.
sigint_handler        dd 0
pending_sigint        db 0
in_sigint_handler     db 0
align 4
```

- [ ] **Step 2: Zero them in program_enter**

In `program_enter` (around the existing `current_pd_phys` / `current_program_break` setup), add:

```nasm
        ;; Reset SIGINT state — every new program starts in SIG_DFL with
        ;; no pending signal and no handler frame on its stack.
        mov dword [sigint_handler],  SIG_DFL
        mov byte  [pending_sigint],    0
        mov byte  [in_sigint_handler], 0
```

Place this near the `current_program_break` initialisation (it's part of "fresh program state").

- [ ] **Step 3: Build and verify**

Run: `./make_os.sh`
Expected: builds clean. No behavior change (no reader yet).

- [ ] **Step 4: Commit**

```bash
git add src/arch/x86/entry.asm
git commit -m "kernel: per-program SIGINT state in BSS, reset by program_enter"
```

---

## Phase 2 — Default kill path (the runaway-program fix)

### Task 3: Implement signal_dispatch_kill

**Files:**
- Create: `src/arch/x86/signal.c`
- Modify: `make_os.sh` (or wherever the C-file list lives)

- [ ] **Step 1: Locate the C-file build list**

Run: `grep -n "fd/midi\|fs/fd/midi\|fd_init" make_os.sh`
Identify how the existing C kernel files (`src/fs/fd/midi.c`, `src/drivers/sb16.c`, etc.) are passed to `cc.py` and added to the build.

- [ ] **Step 2: Create the signal-dispatch source**

Create `src/arch/x86/signal.c`:

```c
// signal.c — SIGINT dispatch primitives.  Two entry points:
//   signal_dispatch_kill  — reset to a known kernel ESP, tear down the
//                           dying program's PD, jump to shell_reload.
//                           Reused by the SIG_DFL path and by handler-
//                           validation failures in SYS_SYS_SIGRETURN.
//   signal_dispatch_user  — built in Task 11; left as a stub here so
//                           the Phase 2 path links cleanly.
//
// signal_dispatch_kill never returns.  It is reachable only from kernel
// context (IRQ epilogue or syscall epilogue), so it can clobber every
// register and reset ESP without consulting the caller's frame.

extern uint32_t current_pd_phys;
extern uint32_t kernel_stack_top;
void address_space_destroy(uint32_t pd_phys);
void put_character(char byte);
void shell_reload();        // entry.asm symbol
asm("shell_reload equ _g_shell_reload" "\n");

void signal_dispatch_kill();

asm("signal_dispatch_kill:\n"
    "        mov esp, kernel_stack_top\n"
    "        mov al, '^'\n"
    "        call put_character\n"
    "        mov al, 'C'\n"
    "        call put_character\n"
    "        mov al, 0x0A\n"
    "        call put_character\n"
    "        mov eax, [_g_current_pd_phys]\n"
    "        test eax, eax\n"
    "        jz .signal_dispatch_kill_no_pd\n"
    "        push eax\n"
    "        call address_space_destroy\n"
    "        add esp, 4\n"
    "        mov dword [_g_current_pd_phys], 0\n"
    ".signal_dispatch_kill_no_pd:\n"
    "        jmp shell_reload\n");
```

Note: the `shell_reload equ _g_shell_reload` aliasing line follows the same pattern as `midi_head equ _g_midi_head` in `src/fs/fd/midi.c`; it lets cc.py's name-mangled symbol point at the asm label. Verify the actual symbol-mangling pattern by reading the top of `src/fs/fd/midi.c` first.

- [ ] **Step 3: Add the new C file to the build**

Add `src/arch/x86/signal.c` to whatever list `make_os.sh` uses for the kernel C files (look at where `src/fs/fd/midi.c` appears).

- [ ] **Step 4: Build and verify clean compile + link**

Run: `./make_os.sh`
Expected: builds clean. No reachable caller yet, but the symbol must link.

- [ ] **Step 5: Commit**

```bash
git add src/arch/x86/signal.c make_os.sh
git commit -m "kernel: signal_dispatch_kill — teleport-to-kernel-ESP teardown"
```

---

### Task 4: PS/2 detects Ctrl+C and sets pending_sigint

**Files:**
- Modify: `src/drivers/ps2.c`

- [ ] **Step 1: Declare the kernel-side flag in ps2.c scope**

Near the top of `src/drivers/ps2.c` (with the other `extern` declarations), add:

```c
extern uint8_t pending_sigint;
```

- [ ] **Step 2: Hook the cooked-byte path**

In `ps2_handle_scancode` (`src/drivers/ps2.c:341` area), the cooked-byte block computes `ascii` for Ctrl+letter via `upper = ascii & 0x5F; ...`. After the cooked byte is finalised but before it is enqueued onto the per-fd ring, add:

```c
if (ascii == 0x03) {
    pending_sigint = 1;
}
```

The 0x03 byte still flows into the per-fd ring as today — programs that want only the byte (not the signal) will install `SIG_IGN` once Phase 3 lands.

- [ ] **Step 3: Build and verify**

Run: `./make_os.sh`
Expected: builds clean. The flag will be set on Ctrl+C but no consumer exists yet.

- [ ] **Step 4: Commit**

```bash
git add src/drivers/ps2.c
git commit -m "drivers/ps2: set pending_sigint when cooked Ctrl+C is detected"
```

---

### Task 5: IRQ epilogue dispatch check

**Files:**
- Modify: `src/arch/x86/entry.asm`

- [ ] **Step 1: Decide the dispatch macro**

Each IRQ handler currently ends in `popad / iretd`. Insert a tail check that:
1. Examines the iret frame's saved CS to know whether we're returning to user code.
2. Reads `pending_sigint` and `in_sigint_handler`.
3. Branches to dispatch or falls through to the original `iretd`.

Define the macro once at the top of `entry.asm`, alphabetical with existing macros if there are any:

```nasm
;;; SIGINT dispatch tail — invoke from IRQ / syscall handler before iretd.
;;; Stack at invocation: the popad has already executed, so [esp] is
;;; iret EIP, [esp+4] is iret CS.  Skips dispatch when:
;;;   - interrupted CS is not user code (we'd kill kernel context),
;;;   - pending_sigint is clear (nothing to do),
;;;   - in_sigint_handler is set (already running a handler — block
;;;     re-entry until SYS_SYS_SIGRETURN clears the flag).
;;; On dispatch: SIG_DFL → signal_dispatch_kill (never returns).
;;;              SIG_IGN → clear pending_sigint, fall through to iretd.
;;;              user-virt → signal_dispatch_user (Task 11; stub for now).
%macro SIGINT_TAIL_CHECK 0
        cmp word [esp + 4], USER_CODE_SELECTOR
        jne %%no_dispatch
        cmp byte [pending_sigint], 0
        je  %%no_dispatch
        cmp byte [in_sigint_handler], 0
        jne %%no_dispatch
        mov eax, [sigint_handler]
        cmp eax, SIG_DFL
        je  signal_dispatch_kill            ; never returns
        cmp eax, SIG_IGN
        jne %%user_handler
        mov byte [pending_sigint], 0
        jmp %%no_dispatch
%%user_handler:
        ;; Phase 4 fills this in.  Until then, treat unknown handler
        ;; values like SIG_DFL — a bare `call signal_dispatch_user`
        ;; wouldn't compile yet because the symbol doesn't exist.
        jmp signal_dispatch_kill
%%no_dispatch:
%endmacro
```

`USER_CODE_SELECTOR` should already be defined in `constants.asm` (search for it). If it's not, use whichever symbolic name the existing `iretd`-to-user code uses.

- [ ] **Step 2: Insert the check in every IRQ handler**

For each of `pmode_irq0_handler`, `pmode_irq1_handler`, `pmode_irq4_handler` (if it exists; otherwise skip), `pmode_irq5_handler`, `pmode_irq6_handler`: insert `SIGINT_TAIL_CHECK` between `popad` and `iretd`.

Example (IRQ 0):

```nasm
pmode_irq0_handler:
        pushad
        inc dword [system_ticks]
        call midi_drain_due
        mov al, PIC_EOI
        out PIC1_CMD_PORT, al
        popad
        SIGINT_TAIL_CHECK
        iretd
```

Apply the same pattern to the other handlers. If `USER_CODE_SELECTOR` is something like `0x1B` (RPL=3 user code segment), confirm by reading the GDT setup near the top of `entry.asm`.

- [ ] **Step 3: Build and verify clean compile**

Run: `./make_os.sh`
Expected: builds clean. The macro should not error on `signal_dispatch_kill` reference (it's defined in Task 3).

- [ ] **Step 4: Manual verification — runaway program killed by Ctrl+C**

Write a 3-line user test program. In a scratch dir:

```c
// /tmp/spin.c — paste then assemble via the OS
int main(void) { while (1) { } }
```

Add it to the disk image:
```bash
# Compile via the on-OS cc.py — easier from inside the OS once shell.
# For this manual test, simpler: write a tiny asm spin.
```

Or, faster: write a tiny asm program that loops forever:

```nasm
; static/spin.asm
%include "constants.asm"
        org PROGRAM_BASE
.spin:  jmp .spin
```

Add it to the disk image:
```bash
./add_file.py static/spin.asm
# or whatever the asm-add path is for the test corpus
```

Then in QEMU:
```bash
./make_os.sh
qemu-system-i386 -drive file=drive.img,format=raw
```

At the shell prompt, run the spin program. Press Ctrl+C in the QEMU window. **Expected:** within ~1 ms (visually instant) the shell reloads with `^C\n` followed by a fresh prompt.

- [ ] **Step 5: Commit**

```bash
git add src/arch/x86/entry.asm
git commit -m "kernel: SIGINT_TAIL_CHECK in all IRQ epilogues; default-kill on Ctrl+C"
```

---

### Task 6: Syscall epilogue dispatch check

**Files:**
- Modify: `src/arch/x86/syscall.asm`

- [ ] **Step 1: Identify the syscall iret exits**

Run: `grep -n "iretd\|.iret_cf\|.iret_no_cf\|.iret_cf_eax" src/arch/x86/syscall.asm`
Identify the central `.iret_*` exit labels. The dispatch's standard exit pattern is the place to splice the check.

- [ ] **Step 2: Insert SIGINT_TAIL_CHECK before each syscall iretd**

For each of the centralised iret exit labels (likely `.iret_cf`, `.iret_cf_eax`, `.iret_no_cf`), insert the same macro before the final `iretd`. The macro is defined in `entry.asm`; if `syscall.asm` and `entry.asm` are assembled into one unit (which they are — the build concatenates flat binaries), the macro is visible. Otherwise, copy the macro definition into `syscall.asm` too.

Example:

```nasm
.iret_cf:
        ...
        popad
        SIGINT_TAIL_CHECK
        iretd
```

- [ ] **Step 3: Build and verify**

Run: `./make_os.sh`
Expected: builds clean.

- [ ] **Step 4: Manual verification — runaway program over a syscall-rich workload**

Run an asm test program that loops calling `SYS_RTC_MILLIS` to exercise the syscall path. Press Ctrl+C; the syscall epilogue check fires the kill. Expected: shell reloads.

- [ ] **Step 5: Commit**

```bash
git add src/arch/x86/syscall.asm
git commit -m "kernel: SIGINT_TAIL_CHECK in syscall iret epilogues"
```

---

## Phase 3 — SIG_IGN + shell

### Task 7: SYS_SYS_SIGNAL syscall (DFL/IGN only)

**Files:**
- Modify: `src/arch/x86/syscall.asm`

- [ ] **Step 1: Add the SYS_ENTRY**

In `src/arch/x86/syscall.asm`, near the existing `SYS_ENTRY SYS_SYS_*` block (around line 118), add:

```nasm
SYS_ENTRY SYS_SYS_SIGNAL,     .sys_signal
SYS_ENTRY SYS_SYS_SIGRETURN,  .sys_sigreturn
```

- [ ] **Step 2: Implement .sys_signal (DFL/IGN/user-virt validation)**

Add the handler in alphabetical position among `.sys_*` labels:

```nasm
        ;; SYS_SYS_SIGNAL: register a signal handler.
        ;; In:  EBX = signum (must be SIGINT)
        ;;      ECX = handler — SIG_DFL (0), SIG_IGN (1), or user-virt
        ;;            address (PROGRAM_BASE ≤ ECX < KERNEL_VIRT_BASE).
        ;; Out: EAX = previous handler value, CF clear on success.
        ;;      CF set + AL = ERROR_INVALID on bad signum or out-of-range
        ;;      handler address.
        ;; Phase 3 only validates DFL / IGN; the user-virt branch is
        ;; allowed but signal_dispatch_user is a stub until Task 11.
        .sys_signal:
        cmp ebx, SIGINT
        jne .sys_signal_bad
        cmp ecx, SIG_IGN
        jbe .sys_signal_ok                  ; ECX in {0, 1}
        cmp ecx, PROGRAM_BASE
        jb  .sys_signal_bad
        cmp ecx, KERNEL_VIRT_BASE
        jae .sys_signal_bad
.sys_signal_ok:
        mov eax, [sigint_handler]            ; previous handler -> EAX
        mov [sigint_handler], ecx
        jmp .iret_no_cf_eax                  ; or whichever path preserves full EAX
.sys_signal_bad:
        mov al, ERROR_INVALID
        jmp .iret_cf
```

If `ERROR_INVALID` does not exist, add it to `constants.asm` (alphabetical insertion in the `ERROR_*` block) using the next free number. If `.iret_no_cf_eax` does not exist, use `.iret_cf_eax` with a `clc` predecessor or whichever existing exit preserves the full 32-bit EAX without sign-extending AX.

- [ ] **Step 3: Build and verify clean compile**

Run: `./make_os.sh`
Expected: builds clean. `.sys_sigreturn` not yet defined — add a temporary stub:

```nasm
        .sys_sigreturn:
        jmp .iret_cf                          ; Phase 4 fills this in
```

- [ ] **Step 4: Commit**

```bash
git add src/arch/x86/syscall.asm src/include/constants.asm
git commit -m "kernel: SYS_SYS_SIGNAL — DFL/IGN/user-virt registration"
```

---

### Task 8: libc signal() wrapper

**Files:**
- Create: `tools/libc/include/signal.h`
- Create: `tools/libc/signal.c`
- Modify: `tools/libc/Makefile`

- [ ] **Step 1: Create the header**

Create `tools/libc/include/signal.h`:

```c
#ifndef BBOEOS_LIBC_SIGNAL_H
#define BBOEOS_LIBC_SIGNAL_H

typedef void (*sighandler_t)(int);
typedef volatile int sig_atomic_t;

#define SIG_DFL ((sighandler_t)0)
#define SIG_IGN ((sighandler_t)1)
#define SIG_ERR ((sighandler_t)-1)

#define SIGINT 2

sighandler_t signal(int signum, sighandler_t handler);

#endif
```

- [ ] **Step 2: Create the wrapper source**

Create `tools/libc/signal.c`:

```c
#include <signal.h>

#include "include/errno.h"

sighandler_t signal(int signum, sighandler_t handler) {
    unsigned int eax_out;
    unsigned int cf;
    __asm__ volatile (
        "mov %[handler], %%ecx\n\t"
        "mov %[signum], %%ebx\n\t"
        "mov $0xF5, %%ah\n\t"            /* SYS_SYS_SIGNAL */
        "int $0x30\n\t"
        "setc %b[cf]\n\t"
        : "=a"(eax_out), [cf]"=&q"(cf)
        : [signum]"g"((unsigned int)signum),
          [handler]"g"((unsigned int)handler)
        : "ebx", "ecx");
    if (cf & 1) {
        errno = EINVAL;
        return SIG_ERR;
    }
    return (sighandler_t)eax_out;
}
```

- [ ] **Step 3: Add to Makefile**

In `tools/libc/Makefile`, append `signal.c` to `C_SRCS` (alphabetical):

```makefile
C_SRCS  := builtins.c ctype.c errno.c math.c signal.c stdio.c stdlib.c string.c syscall.c
```

- [ ] **Step 4: Build libc and verify**

```bash
cd tools/libc && make clean && make
```
Expected: `libbboeos.a` builds with no errors.

- [ ] **Step 5: Commit**

```bash
git add tools/libc/include/signal.h tools/libc/signal.c tools/libc/Makefile
git commit -m "libc: signal() wrapper around SYS_SYS_SIGNAL"
```

---

### Task 9: Shell installs SIG_IGN

**Files:**
- Modify: `src/c/shell.c`

- [ ] **Step 1: Find the shell startup**

Run: `grep -n "int main\|^int main\|FUNCTION_PRINT\|set_exec_arg" src/c/shell.c | head`
Identify the shell's `main` (or equivalent entry).

- [ ] **Step 2: Add the signal install**

Near the top of the shell's main, add a SYS_SYS_SIGNAL call. Because the shell is a userland program built with `cc.py` (not the libc shim), call the syscall directly via inline asm, mirroring how other shell syscalls are done:

```c
// Ignore SIGINT — the shell prefers to keep its line editor alive when
// the user types Ctrl+C at the prompt.  Cooked 0x03 still arrives in
// the byte stream so the line editor can choose to display ^C and
// reset its input buffer; without SIG_IGN, the kernel-side default is
// to kill the program (which here would mean reloading the shell).
int previous_handler;
asm("mov ebx, 2\n"            // SIGINT
    "mov ecx, 1\n"            // SIG_IGN
    "mov ah, 0xF5\n"          // SYS_SYS_SIGNAL
    "int 0x30\n"
    "mov [ebp-4], eax\n"      // stash previous_handler (cc.py local)
    : : : "eax", "ebx", "ecx");
(void)previous_handler;
```

If the shell is structured differently (no `int main` but a different entry point), add the call wherever the existing initialisation sits.

- [ ] **Step 3: Build and verify**

Run: `./make_os.sh`
Expected: clean build.

- [ ] **Step 4: Manual verification — shell ignores Ctrl+C**

Boot in QEMU. At the shell prompt with no child running, press Ctrl+C. **Expected:** shell continues running; no `^C` kill banner; prompt remains active.

Then exec a child (e.g. `cat` or `ls`); while it's running, Ctrl+C. **Expected:** child dies, shell prompt returns. (Children run with `SIG_DFL` because `program_enter` resets the slot.)

- [ ] **Step 5: Commit**

```bash
git add src/c/shell.c
git commit -m "shell: install SIG_IGN at startup so its own Ctrl+C is benign"
```

---

## Phase 4 — Real handler delivery (sigcontext + sigreturn)

### Task 10: vDSO trampoline

**Files:**
- Modify: `src/arch/x86/entry.asm`

- [ ] **Step 1: Find the vDSO page initialisation**

Run: `grep -n "vDSO\|VDSO\|FUNCTION_TABLE\|0x10000" src/arch/x86/entry.asm`
Identify where the shared vDSO page is allocated and its function table populated.

- [ ] **Step 2: Write the trampoline at VDSO_VIRT + VDSO_SIGRETURN_OFFSET**

After the existing function-table writes, add (kernel writes 7 bytes of code into the vDSO page):

```nasm
        ;; vDSO sigreturn trampoline.  Sigreturn handlers `ret` into here
        ;; and we immediately re-enter the kernel via SYS_SYS_SIGRETURN.
        ;; Bytes:
        ;;   B8 F6 00 00 00     mov eax, 0x000000F6
        ;;   CD 30              int 0x30
        mov edi, [vdso_page_kernel_virt]
        add edi, VDSO_SIGRETURN_OFFSET
        mov byte  [edi + 0], 0xB8                ; mov eax, imm32
        mov dword [edi + 1], SYS_SYS_SIGRETURN
        mov word  [edi + 5], 0x30CD              ; int 0x30
```

The actual variable name holding the vDSO page kernel address (`vdso_page_kernel_virt` above) needs to be substituted with whatever the existing init code uses — read the surrounding lines to pick the right symbol.

The `mov eax, imm32` form (5 bytes) instead of `mov ah, 0xF6` (2 bytes) is used so the full SYSCALL number occupies AH; for AH-only this would be `B4 F6 CD 30` (4 bytes). Either works; the 7-byte form keeps it explicit. If using the AH form, adjust the offset arithmetic.

- [ ] **Step 3: Build and verify clean compile**

Run: `./make_os.sh`
Expected: builds clean. The trampoline is reachable from user-virt 0x10100 but no caller yet.

- [ ] **Step 4: Commit**

```bash
git add src/arch/x86/entry.asm
git commit -m "kernel: vDSO sigreturn trampoline at VDSO_VIRT + 0x100"
```

---

### Task 11: signal_dispatch_user — build sigcontext + redirect IRET

**Files:**
- Modify: `src/arch/x86/signal.c`
- Modify: `src/arch/x86/entry.asm` (the `SIGINT_TAIL_CHECK` macro's `%%user_handler` branch)

- [ ] **Step 1: Implement signal_dispatch_user**

In `src/arch/x86/signal.c`, add `signal_dispatch_user`. It runs in kernel context immediately after the IRQ/syscall handler's `popad` but before `iretd`. Convention: caller jumps here (no return — we rewrite the iret frame and `iretd` ourselves).

The iret frame at `[esp]` is: `EIP, CS, EFLAGS, ESP, SS` (5 dwords pushed by the CPU when transitioning user→kernel).

The dispatch:
1. Read `user_esp = [esp + 12]`.
2. Subtract 48 from it.
3. Build 12 dwords at `[user_esp_new..user_esp_new+44]`:
   - +0 trampoline_addr (= `VDSO_VIRT + VDSO_SIGRETURN_OFFSET`)
   - +4 signum (= SIGINT = 2)
   - +8 saved_eip (from iret frame +0)
   - +12 saved_eflags (from iret frame +8)
   - +16 saved_esp (the original user ESP, before the new -48)
   - +20..+44 saved_eax..edi (in pushad order, minus the ESP slot — these came from the pushad we just popped, so we need them to have been preserved; see Step 2)
4. Rewrite iret frame: `EIP ← sigint_handler`, `ESP ← user_esp_new`.
5. Set `in_sigint_handler = 1`, `pending_sigint = 0`.
6. `iretd`.

Critical detail: the macro pops pushad before invoking the dispatch, which destroys the saved registers. Restructure: the dispatch takes its own snapshot. Easiest approach — inline the snapshot in the macro itself.

Revise `SIGINT_TAIL_CHECK` (Task 5) so that the user-handler branch jumps **before** popad, with pushad slots still on stack:

```nasm
%macro SIGINT_TAIL_CHECK 0
        ;; pushad still live on entry (caller hasn't popad'd yet).
        cmp word [esp + 32 + 4], USER_CODE_SELECTOR    ; iret CS = pushad(8 dwords) + iret EIP
        jne %%no_dispatch
        cmp byte [pending_sigint], 0
        je  %%no_dispatch
        cmp byte [in_sigint_handler], 0
        jne %%no_dispatch
        mov eax, [sigint_handler]
        cmp eax, SIG_DFL
        je  signal_dispatch_kill
        cmp eax, SIG_IGN
        jne %%user_handler_dispatch
        mov byte [pending_sigint], 0
        jmp %%no_dispatch
%%user_handler_dispatch:
        jmp signal_dispatch_user                  ; never returns to here
%%no_dispatch:
        popad
%endmacro
```

Then update every IRQ + syscall handler to NOT call `popad` themselves before the macro — the macro now owns the popad.

For the IRQ 0 example:

```nasm
pmode_irq0_handler:
        pushad
        inc dword [system_ticks]
        call midi_drain_due
        mov al, PIC_EOI
        out PIC1_CMD_PORT, al
        SIGINT_TAIL_CHECK
        iretd
```

The pushad slots are at `[esp + 0..28]` (8 dwords); iret frame starts at `[esp + 32]`. The CS check uses `[esp + 32 + 4]`, EIP at `[esp + 32 + 0]`, EFLAGS at `[esp + 32 + 8]`, ESP at `[esp + 32 + 12]`.

Now the dispatch (in signal.c) can read pushad slots and iret-frame slots in one shot:

```c
void signal_dispatch_user();
asm("signal_dispatch_user:\n"
    // ESP points at: pushad(8 dwords, EDI..EAX in popad order) + iret(EIP, CS, EFLAGS, ESP, SS)
    // Pushad layout (offset from esp):  EDI=0, ESI=4, EBP=8, ESP_unused=12, EBX=16, EDX=20, ECX=24, EAX=28
    // iret layout:                     EIP=32, CS=36, EFLAGS=40, ESP=44, SS=48
    "        mov edi, [esp + 44]\n"               // user ESP
    "        sub edi, 48\n"                       // make room for sigcontext
    // Write sigcontext at [edi + 0..44]
    "        mov dword [edi + 0],  VDSO_VIRT + VDSO_SIGRETURN_OFFSET\n"
    "        mov dword [edi + 4],  SIGINT\n"
    "        mov eax, [esp + 32]\n"               // saved EIP
    "        mov [edi + 8], eax\n"
    "        mov eax, [esp + 40]\n"               // saved EFLAGS
    "        mov [edi + 12], eax\n"
    "        mov eax, [esp + 44]\n"               // saved ESP (original)
    "        mov [edi + 16], eax\n"
    // Saved registers (pushad order minus ESP slot)
    "        mov eax, [esp + 28]\n"               // EAX
    "        mov [edi + 20], eax\n"
    "        mov eax, [esp + 24]\n"               // ECX
    "        mov [edi + 24], eax\n"
    "        mov eax, [esp + 20]\n"               // EDX
    "        mov [edi + 28], eax\n"
    "        mov eax, [esp + 16]\n"               // EBX
    "        mov [edi + 32], eax\n"
    "        mov eax, [esp + 8]\n"                // EBP
    "        mov [edi + 36], eax\n"
    "        mov eax, [esp + 4]\n"                // ESI
    "        mov [edi + 40], eax\n"
    "        mov eax, [esp + 0]\n"                // EDI
    "        mov [edi + 44], eax\n"
    // Rewrite iret frame: EIP ← handler, ESP ← edi
    "        mov eax, [_g_sigint_handler]\n"
    "        mov [esp + 32], eax\n"
    "        mov [esp + 44], edi\n"
    // Mark in-handler, clear pending
    "        mov byte [_g_in_sigint_handler], 1\n"
    "        mov byte [_g_pending_sigint], 0\n"
    // Skip popad — its values are already captured in sigcontext.
    // ESP needs to advance past pushad slots before iretd.
    "        add esp, 32\n"
    "        iretd\n");
```

The kernel writes to user-stack memory at `[edi]`. If the user stack is exhausted (page guarded), the write triggers `#PF` which the existing `exc_common` handler routes to `EXC0D` — the program dies. This matches the spec's stack-overflow note.

- [ ] **Step 2: Build and verify clean compile**

Run: `./make_os.sh`
Expected: builds clean.

- [ ] **Step 3: Commit**

```bash
git add src/arch/x86/signal.c src/arch/x86/entry.asm
git commit -m "kernel: signal_dispatch_user — sigcontext build + iret-frame rewrite"
```

---

### Task 12: SYS_SYS_SIGRETURN — restore sigcontext

**Files:**
- Modify: `src/arch/x86/syscall.asm`
- Modify: `src/arch/x86/signal.c`

- [ ] **Step 1: Implement signal_resume_after_handler**

Add to `src/arch/x86/signal.c`:

```c
void signal_resume_after_handler();

// Reads sigcontext from user stack at [user_esp + 4] (the trampoline's
// `int 0x30` left ESP one dword past the original sigcontext start
// because the handler's `ret` popped the trampoline_addr).  Validates
// saved_eip / saved_esp are in user range.  On valid: rewrites the
// syscall iret frame to resume at saved_eip with saved_esp / saved_eflags
// / saved_eax..edi.  On invalid: jump to signal_dispatch_kill.
//
// ESP at entry points at the syscall's pushad-and-iret frame:
//   pushad EDI..EAX = [esp + 0..28]
//   iret EIP / CS / EFLAGS / ESP / SS = [esp + 32..48]
asm("signal_resume_after_handler:\n"
    "        mov edi, [esp + 44]\n"               // user ESP at trampoline entry
    "        add edi, 4\n"                        // skip the popped trampoline_addr → start of saved registers
    // Validate saved_eip
    "        mov eax, [edi + 0]\n"                // saved EIP
    "        cmp eax, PROGRAM_BASE\n"
    "        jb  signal_dispatch_kill\n"
    "        cmp eax, KERNEL_VIRT_BASE\n"
    "        jae signal_dispatch_kill\n"
    // Validate saved_esp
    "        mov eax, [edi + 8]\n"                // saved ESP
    "        cmp eax, PROGRAM_BASE\n"
    "        jb  signal_dispatch_kill\n"
    "        cmp eax, KERNEL_VIRT_BASE\n"
    "        ja  signal_dispatch_kill\n"          // == KERNEL_VIRT_BASE OK (USER_STACK_TOP)
    // Restore iret frame
    "        mov eax, [edi + 0]\n"                // saved EIP
    "        mov [esp + 32], eax\n"
    "        mov eax, [edi + 4]\n"                // saved EFLAGS
    "        mov [esp + 40], eax\n"
    "        mov eax, [edi + 8]\n"                // saved ESP
    "        mov [esp + 44], eax\n"
    // Restore pushad slots
    "        mov eax, [edi + 12]\n"               // saved EAX
    "        mov [esp + 28], eax\n"
    "        mov eax, [edi + 16]\n"               // saved ECX
    "        mov [esp + 24], eax\n"
    "        mov eax, [edi + 20]\n"               // saved EDX
    "        mov [esp + 20], eax\n"
    "        mov eax, [edi + 24]\n"               // saved EBX
    "        mov [esp + 16], eax\n"
    "        mov eax, [edi + 28]\n"               // saved EBP
    "        mov [esp + 8], eax\n"
    "        mov eax, [edi + 32]\n"               // saved ESI
    "        mov [esp + 4], eax\n"
    "        mov eax, [edi + 36]\n"               // saved EDI
    "        mov [esp + 0], eax\n"
    // Clear in-handler; re-check pending_sigint (a Ctrl+C arriving
    // during the handler should fire immediately on resume).
    "        mov byte [_g_in_sigint_handler], 0\n"
    "        cmp byte [_g_pending_sigint], 0\n"
    "        je  .signal_resume_no_pending\n"
    // Pending SIGINT — re-dispatch instead of returning to user code.
    "        mov eax, [_g_sigint_handler]\n"
    "        cmp eax, SIG_DFL\n"
    "        je  signal_dispatch_kill\n"
    "        cmp eax, SIG_IGN\n"
    "        jne signal_dispatch_user\n"
    "        mov byte [_g_pending_sigint], 0\n"
    ".signal_resume_no_pending:\n"
    // popad + iretd
    "        popad\n"
    "        iretd\n");
```

The `EDI` index dword offsets above assume sigcontext layout:
- [edi + 0..40] = saved registers (eip, eflags, esp, eax, ecx, edx, ebx, ebp, esi, edi) — 10 dwords = 40 bytes after the trampoline pop.

This matches the layout written by `signal_dispatch_user` from offset +8 onward (which becomes +0 here after the trampoline-addr pop).

- [ ] **Step 2: Wire .sys_sigreturn to call signal_resume_after_handler**

Replace the stub `.sys_sigreturn` from Task 7 in `src/arch/x86/syscall.asm`:

```nasm
        .sys_sigreturn:
        jmp signal_resume_after_handler          ; never returns through .iret_*
```

- [ ] **Step 3: Build and verify clean compile**

Run: `./make_os.sh`
Expected: builds clean.

- [ ] **Step 4: Manual verification — handler runs and resumes**

Write a small test program (asm or C via cc.py):

```asm
; static/sigtest.asm
%include "constants.asm"
        org PROGRAM_BASE
.entry:
        ;; signal(SIGINT, on_sigint)
        mov ebx, SIGINT
        mov ecx, on_sigint
        mov ah, SYS_SYS_SIGNAL
        int 0x30
.spin:
        cmp dword [caught_flag], 0
        je .spin
        ;; print "GOT IT\n" via FUNCTION_PRINT_STRING then exit
        mov esi, msg
        call [VDSO_VIRT + FUNCTION_PRINT_STRING]
        mov ah, SYS_SYS_EXIT
        int 0x30
on_sigint:
        mov dword [caught_flag], 1
        ret
caught_flag dd 0
msg db "GOT IT", 10, 0
```

Add to image, boot QEMU, run sigtest, press Ctrl+C. **Expected:** "GOT IT" prints, then shell prompt returns.

If "GOT IT" does NOT print (handler never ran) or system hangs (sigreturn broken), the dispatch / restore logic needs debugging. Use `-serial stdio` to capture both screen and serial output for triage.

- [ ] **Step 5: Commit**

```bash
git add src/arch/x86/signal.c src/arch/x86/syscall.asm
git commit -m "kernel: SYS_SYS_SIGRETURN — restore sigcontext, redeliver pending SIGINT"
```

---

## Phase 5 — Cooperative interruption

### Task 13: fd_read_console checks pending_sigint + detects serial 0x03

**Files:**
- Modify: `src/fs/fd/console.c`

- [ ] **Step 1: Add the kernel-side flag declaration**

Near the existing `extern` declarations at the top of `src/fs/fd/console.c`:

```c
extern uint8_t pending_sigint;
```

- [ ] **Step 2: Hook the wait loop and serial detection**

Modify `fd_read_console` (around line 131-148):

```c
__attribute__((carry_return))
int fd_read_console(int *bytes_read __attribute__((out_register("ax"))),
                    uint8_t *destination __attribute__((in_register("edi"))),
                    int max_bytes __attribute__((in_register("ecx")))) {
    char byte;
    if (max_bytes == 0) {
        *bytes_read = 0;
        return 1;
    }
    asm("sti");
    while (1) {
        if (pending_sigint) {
            // Cooperative interrupt: bail with EINTR.  The syscall
            // epilogue's tail check will dispatch the signal on iret.
            *bytes_read = ERROR_INTERRUPTED;
            return 0;                    // CF set via carry_return convention
        }
        byte = ps2_getc();
        if (byte != '\0') {
            break;
        }
        if ((kernel_inb(0x3FD) & 0x01) != 0) {
            byte = kernel_inb(0x3F8);
            if (byte == 0x03) {
                // Serial Ctrl+C — set the flag so the next IRQ epilogue
                // (or this same syscall's epilogue, after we return)
                // delivers.  The byte is also returned in the buffer
                // so programs that ignore SIGINT (shell with SIG_IGN)
                // see it as a normal cooked input.
                pending_sigint = 1;
            }
            break;
        }
    }
    if (byte == '\r') {
        byte = '\n';
    }
    destination[0] = byte;
    *bytes_read = 1;
    return 1;
}
```

Verify the `carry_return` convention by reading `cc.py`'s docs or other functions using it (e.g., `fd_write_midi` in `src/fs/fd/midi.c`). If the convention is `return 0 → CF=1`, the above is correct; if inverted, swap the `return 0`/`return 1`.

- [ ] **Step 3: Build and verify clean compile**

Run: `./make_os.sh`
Expected: clean build.

- [ ] **Step 4: Manual verification**

Boot in QEMU with `-serial stdio`. From the serial console, type `cat` (or any program that reads stdin), then press Ctrl+C in the terminal. **Expected:** the read returns EINTR (cat sees 0 bytes / -1), then the syscall epilogue's tail check fires SIGINT default-kill, shell prompt returns.

- [ ] **Step 5: Commit**

```bash
git add src/fs/fd/console.c
git commit -m "fs/fd/console: cooperative SIGINT bail in read; detect serial 0x03"
```

---

### Task 14: MIDI_IOCTL_DRAIN checks pending_sigint

**Files:**
- Modify: `src/fs/fd/midi.c`

- [ ] **Step 1: Add the cooperative check to the drain wait loop**

In `src/fs/fd/midi.c`, modify the inline asm `.fd_ioctl_midi_drain_wait` block:

```c
asm("...\n"
    ".fd_ioctl_midi_drain_wait:\n"
    "        cli\n"
    "        cmp byte [_g_pending_sigint], 0\n"
    "        jne .fd_ioctl_midi_drain_eintr\n"
    "        mov al, [_g_midi_head]\n"
    "        cmp al, [_g_midi_tail]\n"
    "        je .fd_ioctl_midi_drain_done\n"
    "        sti\n"
    "        hlt\n"
    "        jmp .fd_ioctl_midi_drain_wait\n"
    ".fd_ioctl_midi_drain_done:\n"
    "        sti\n"
    "        xor eax, eax\n"
    "        clc\n"
    "        ret\n"
    ".fd_ioctl_midi_drain_eintr:\n"
    "        sti\n"
    "        mov al, ERROR_INTERRUPTED\n"
    "        stc\n"
    "        ret\n"
    "...");
```

Apply this as an `Edit` to the existing `asm(...)` block in `fd_ioctl_midi`. Keep the surrounding structure intact.

Add the `pending_sigint` extern at the top of the file if not already present:

```c
extern uint8_t pending_sigint;
```

- [ ] **Step 2: Build and verify**

Run: `./make_os.sh`
Expected: clean build.

- [ ] **Step 3: Manual verification**

Run a program (e.g. doom or a small test) that opens `/dev/midi`, queues commands with delays, then calls `MIDI_IOCTL_DRAIN`. While drain is blocked, press Ctrl+C. **Expected:** drain returns -1/EINTR and the SIGINT epilogue check fires.

- [ ] **Step 4: Commit**

```bash
git add src/fs/fd/midi.c
git commit -m "fs/fd/midi: cooperative SIGINT bail in MIDI_IOCTL_DRAIN sti+hlt loop"
```

---

### Task 15: libc EINTR translation in syscall wrappers

**Files:**
- Modify: `tools/libc/syscall.c`

- [ ] **Step 1: Locate _errno_from_al**

Run: `grep -n "_errno_from_al" tools/libc/syscall.c`

- [ ] **Step 2: Add the ERROR_INTERRUPTED → EINTR mapping**

In the `_errno_from_al` switch / table, add a case for `0x08` returning `EINTR`. Update the comment block at the top to include the new mapping:

```c
 *   08h ERROR_INTERRUPTED     -> EINTR
```

The existing wrappers (`read`, `ioctl`, `open`, etc.) all funnel CF=1 through `_errno_from_al`, so they pick up EINTR translation automatically — no per-wrapper change needed.

- [ ] **Step 3: Build libc and verify**

```bash
cd tools/libc && make clean && make
```
Expected: clean build.

- [ ] **Step 4: Commit**

```bash
git add tools/libc/syscall.c
git commit -m "libc: map ERROR_INTERRUPTED (0x08) -> EINTR in syscall wrappers"
```

---

## Phase 6 — Tests + docs

### Task 16: Automated handler test

**Files:**
- Create: `tests/sigint_handler_test.c`
- Modify: `tests/test_programs.py`

- [ ] **Step 1: Write the test program**

Create `tests/sigint_handler_test.c`:

```c
#include <signal.h>
#include <stdio.h>
#include <unistd.h>

volatile sig_atomic_t got_sigint = 0;

void on_sigint(int signum) {
    (void)signum;
    got_sigint = 1;
}

int main(void) {
    char byte;
    signal(SIGINT, on_sigint);
    write(1, "READY\n", 6);                      /* test driver waits for this */
    /* Read from stdin until either Ctrl+C is delivered (handler sets
     * got_sigint and the read returns -1/EINTR) or 'q' arrives. */
    while (!got_sigint) {
        if (read(0, &byte, 1) > 0 && byte == 'q') {
            break;
        }
        /* On EINTR, read returns -1 and we loop back; the handler has
         * set got_sigint by now. */
    }
    if (got_sigint) {
        write(1, "CAUGHT\n", 7);
    } else {
        write(1, "QUIT\n", 5);
    }
    return 0;
}
```

Add to disk image. The exact mechanism depends on how `tests/test_programs.py` builds tests. Read the existing test entries for a C-based program (e.g. anything that uses libc) and mirror its build steps.

- [ ] **Step 2: Add the test_programs.py entry**

In `tests/test_programs.py`, add an entry (alphabetically sorted with existing entries):

```python
    ("sigint_handler", {
        "filesystems": {"bbfs", "ext2"},
        "run_commands": [
            ("sigint_handler", "READY", "\x03", "CAUGHT"),
        ],
        # The "\x03" string is sent on the serial fifo after seeing
        # "READY"; the test driver must support this multi-step pattern.
        # If the existing fixture only supports one input + one output,
        # extend it minimally rather than over-engineering.
    }),
```

The exact dict shape depends on `tests/test_programs.py`'s framework — read other entries first to copy the pattern. The driver must:
1. Wait for the `READY` marker on serial output.
2. Send the byte `0x03` on serial input.
3. Wait for `CAUGHT` on serial output.
4. Wait for the shell prompt `$ ` to confirm clean return.

If the test driver doesn't support sending raw bytes mid-stream, add a small helper (~10 lines) before adding this test.

- [ ] **Step 3: Run the test**

```bash
./tests/test_programs.py sigint_handler
```
Expected: passes.

- [ ] **Step 4: Run the full suite to verify no regressions**

```bash
./tests/test_programs.py
./tests/test_asm.py
./tests/test_bboefs.py
```
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add tests/sigint_handler_test.c tests/test_programs.py
git commit -m "tests: SIGINT handler delivery + sigreturn end-to-end"
```

---

### Task 17: Documentation

**Files:**
- Modify: `docs/syscalls.md`
- Modify: `docs/architecture.md`
- Modify: `docs/CHANGELOG.md`

- [ ] **Step 1: Add syscall entries**

In `docs/syscalls.md`, add to the main table (in numeric order, after `F4h sys_shutdown`):

```markdown
| F5h   | sys_signal   | Register SIGINT handler, EBX = signum (SIGINT only), ECX = handler (SIG_DFL=0, SIG_IGN=1, or user-virt addr); EAX = previous handler, CF on bad signum/addr |
| F6h   | sys_sigreturn| Restore sigcontext from user stack; never returns normally — resumes the saved EIP |
```

Add an "Error codes" subsection if one doesn't exist, listing `ERROR_INTERRUPTED` (08h, mapped to `EINTR` in libc).

- [ ] **Step 2: Document SIGINT in architecture.md**

Add a short subsection to `docs/architecture.md` describing:
- The two-axis model (detection in IRQ context, delivery at IRET epilogue).
- `SIG_DFL` / `SIG_IGN` / user-handler dispatch.
- Cooperative-interruption convention (`pending_sigint` check in `sti+hlt` loops returning `ERROR_INTERRUPTED`).
- Limitation: serial-only Ctrl+C does not kill runaway programs (PS/2 only); future work is IRQ-driven serial input.

Aim for ~60-100 lines.

- [ ] **Step 3: CHANGELOG entry**

Add to `docs/CHANGELOG.md` under Unreleased:

```markdown
### Added
- SIGINT handling: Ctrl+C on PS/2 kills runaway user programs by
  default; programs may register a handler via `signal(SIGINT, ...)`
  with sigcontext-on-stack delivery and `SYS_SYS_SIGRETURN` resume.
  Cooperative-interruption convention added to `fd_read_console` and
  `MIDI_IOCTL_DRAIN`. See `docs/architecture.md` for the model and
  `docs/superpowers/specs/2026-05-06-sigint-handling-design.md` for
  the full design.
```

- [ ] **Step 4: Commit**

```bash
git add docs/syscalls.md docs/architecture.md docs/CHANGELOG.md
git commit -m "docs: SIGINT handling — syscalls, architecture, changelog"
```

---

## Self-review

This plan covers the spec sections as follows:

| Spec section | Tasks |
|---|---|
| Architecture | Tasks 1–17 (entire plan) |
| Kernel-side state | 2 |
| Detection (PS/2) | 4 |
| Detection (serial) | 13 |
| Sigcontext layout | 11 |
| IRET-frame rewrite locations | 5, 6 |
| Re-entry | 11 (`in_sigint_handler` set), 12 (cleared by sigreturn) |
| Stack overflow | covered by existing `exc_common` (no new code) |
| Sigreturn (vDSO trampoline) | 10 |
| Sigreturn (syscall) | 12 |
| Cooperative interruption (`fd_read_console`) | 13 |
| Cooperative interruption (`MIDI_IOCTL_DRAIN`) | 14 |
| Default kill path | 3 |
| Shell integration | 9 |
| API surface (asm constants) | 1 |
| API surface (`SYS_SYS_SIGNAL`) | 7 (DFL/IGN), 7 again validates user-virt range |
| API surface (`SYS_SYS_SIGRETURN`) | 12 |
| libc surface (`signal.h` + wrapper) | 8 |
| libc EINTR translation | 15 |
| Handler reentrancy expectations | docs only — covered in 17 |
| Testing (manual) | 5, 6, 9, 12, 13, 14 (each task includes a manual-verify step) |
| Testing (automated) | 16 |

No spec requirement is unaddressed.

**Type / signature consistency check:**
- `pending_sigint`, `in_sigint_handler` — `db` (1 byte), checked as `byte`.
- `sigint_handler` — `dd` (4 bytes), checked as `dword`.
- `signal_dispatch_kill` — never returns, no args, kernel-context only.
- `signal_dispatch_user` — never returns to caller (rewrites iret + iretd).
- `signal_resume_after_handler` — never returns to caller.
- `SYS_SYS_SIGNAL` register contract: `EBX=signum, ECX=handler, EAX=prev`.
- `SYS_SYS_SIGRETURN`: no in args; reads from user stack at `[user_esp + 4]`.
- `ERROR_INTERRUPTED = 0x08`, maps to `EINTR = 4`.
- vDSO trampoline at `VDSO_VIRT + VDSO_SIGRETURN_OFFSET` (`= 0x10100`).

All names consistent across tasks.

**Placeholder scan:** Every step has either runnable code, an exact command, or a `grep -n` lookup with explicit follow-up instructions.

**Open assumptions** (worth flagging during execution):
- `cc.py`'s `__attribute__((carry_return))` exact convention — verified by reading existing users in `src/fs/fd/midi.c` before writing Task 13.
- `tests/test_programs.py` test-driver dict shape and serial-fifo input plumbing — verified by reading existing entries before writing Task 16.
- `USER_CODE_SELECTOR` exact symbolic name in `constants.asm` or `entry.asm` — verified by `grep` before writing Task 5.
- Whether `entry.asm` and `syscall.asm` are assembled together so the `SIGINT_TAIL_CHECK` macro is visible across files — verified by reading `make_os.sh` before writing Task 6.

If any assumption is wrong, the affected task gets a single follow-up edit; no architectural change.
