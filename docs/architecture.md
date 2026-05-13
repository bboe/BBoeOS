---
title: Architecture
nav_order: 60
---

# Architecture

The kernel is split across two flat binaries (`nasm -f bin`) concatenated on
disk:

- **`boot.bin`** (`org 0x7C00`, `src/arch/x86/boot/boot.asm`): MBR + post-MBR
  real-mode bootstrap + early-PE bootstrap. Loaded by BIOS at `0x7C00`. The MBR
  does DS/ES/SS:SP setup, disk reset, and an `INT 13h` read that pulls the
  post-MBR portion of `boot.bin` into `0x7E00`. The post-MBR real-mode code
  issues a second `INT 13h` read to load `kernel.bin` directly into physical
  `0x20000` (its final home — no later relocation copy), walks the BIOS memory
  map via `INT 15h AX=E820` (entries stashed at `0x500` for the bitmap
  allocator), copies the BIOS ROM 8x16 font, remaps the PIC, enables A20, loads
  the 32-bit GDT, flips `CR0.PE`, and far-jumps into `early_pe_entry`.
  `early_pe_entry` (32-bit, low physical) builds the boot PD + first kernel PT
  (identity-mapped at PDE[0] and direct-mapped at PDE[FIRST_KERNEL_PDE = 1022]),
  enables paging (`CR0.PG | CR0.WP`), and far-jumps to `high_entry` at virt
  `0xFF820000`. No IDT in `boot.asm` — an exception during early-PE
  triple-faults; the bootstrap is short and tested. On disk error the MBR prints
  `!` via `INT 10h AH=0Eh` and halts; an `INT 13h` failure on the `kernel.bin`
  read prints `K`.
- **`kernel.bin`** (`org 0xFF820000`, `src/arch/x86/kernel.asm`): post-paging
  high-half kernel. The `org` equals `DIRECT_MAP_BASE + KERNEL_LOAD_PHYS`, so
  the kernel runs at its direct-map alias and PDE[FIRST_KERNEL_PDE = 1022]'s 4
  MB direct map is the only mapping it needs. The very first byte is
  `high_entry`, which lgdts the kernel GDT, lidts the kernel IDT (`idt_init`
  patches the high-half handler offsets at boot — see `src/arch/x86/idt.asm` for
  why the IDT_ENTRY macro can't fold them at assemble time), drops the boot
  identity mapping at PDE[0], initializes the bitmap frame allocator from E820,
  allocates the kernel direct-map PTs (no-op at FIRST_KERNEL_PDE = 1022 — the
  auto-grow loop's bound `FIRST_KERNEL_PDE + 1` already equals `LAST_KERNEL_PDE
  = 1023`), brings up the kmap window via `kmap_init`, and falls through into
  `protected_mode_entry`. Locating the kernel in conventional RAM (above the
  vDSO target at phys `0x10000`, below the VGA aperture at phys `0xA0000`) keeps
  the entire kernel-side reserved region under 1 MB so the OS boots under QEMU
  `-m 1`.

## Post-flip kernel bring-up

- **Post-flip entry** (`protected_mode_entry` in `src/arch/x86/entry.asm`): TSS
  base patch + `SS0`/`ESP0`/IOPB-offset init + `ltr`, PIT @ 100 Hz, 32-bit IRQ 0
  / IRQ 6 handlers via `idt_set_gate32`, driver inits (`ata_init`, `fd_init`,
  `fdc_init`, `ps2_init`, `vfs_init`, `network_initialize`), unmask IRQ 0/6,
  `sti`, welcome banner, then falls into `shell_reload`. Segment reload, ESP,
  GDT, and IDT are already in place from `high_entry`. Any post-flip CPU
  exception lands in `idt.asm`'s `exc_common` and prints `EXCnn EIP=h CR2=h
  ERR=h` on COM1.

## Ring-3 userland

- **Ring-3 userland**: GDT has user code (`0x18`, `DPL=3`) and user data
  (`0x20`, `DPL=3`) descriptors plus a TSS at `0x28` whose `SS0:ESP0` points at
  the kernel stack. The `INT 30h` gate is `DPL=3` so ring-3 programs can call
  it; CPU exceptions and IRQs stay `DPL=0` (hardware bypasses the gate-DPL
  check) so user code can't synthesise fake fault frames. `program_enter`
  reloads DS/ES/FS/GS to `USER_DATA_SELECTOR` (`0x23`) and `iretd`s into ring 3
  at `PROGRAM_BASE` (`0x08048000`) with `ESP=USER_STACK_TOP` (`0xFF800000`,
  sitting exactly at the user/kernel boundary = `KERNEL_VIRT_BASE`) and
  `EFLAGS=0x202` (`IF=1`, `IOPL=0`). Privileged instructions
  (`cli`/`sti`/`in`/`out`/CR writes) `#GP` from userland.
- **Shell respawn** (`shell_reload` → `program_enter`): `vfs_find` + `vfs_load`
  for `bin/shell`, then `program_enter` resets the fd table, zeroes the
  program's BSS region per the trailer-magic protocol (`dw bss_size; dw
  0xB055`), snapshots the ring-0 ESP into `[shell_esp]`, and `iretd`s the
  program at ring 3. `sys_exit` from any program restores `[shell_esp]` (the CPU
  has already auto-switched to TSS.ESP0 on the ring-3 → 0 transition) and
  re-enters `shell_reload`.
- **Shell** (`src/c/shell.c`): Loaded from filesystem at `PROGRAM_BASE`
  (`0x08048000`, the Linux ELF-shaped user-virt load address). Provides CLI
  loop, command dispatch, and built-in commands using `INT 30h` syscalls.

## Kernel-side runtime data

- **Input buffer** at linear address `0x500`, max 256 characters.
- **Disk buffer** (`sector_buffer`, 512 B) is the offset-0 slice of the FS
  scratch frame that `vfs_init` allocates from the bitmap on every boot.
  `bbfs.asm` and `ext2.asm` load the kernel-virt pointer indirectly: `mov ebx,
  [sector_buffer]`. `ext2_sd_buffer` (the 1 KB sliding directory window used
  only by `ext2_search_blk`) is the offset-512 slice of the same frame on ext2
  mounts; on bbfs the pointer stays 0 since no caller reaches the ext2-only
  paths.
- **FD table** is allocated as kernel BSS (`struct fd fd_table[FD_MAX]` in
  `src/fs/fd.c`), so it lives inside `kernel.bin` like any other kernel global;
  no fixed-phys reservation needed.  `sys_exec` inherits the parent's `fd_table`
  into the child's `program_state` slot rather than re-running `fd_init`, so
  open file descriptors cross exec boundaries; `child_terminate` walks the
  outgoing `fd_table` and calls `fd_close` on each non-free slot so per-type
  teardown (file size flush, audio/MIDI close) runs regardless of how the
  program exits.  A per-fd `dirty` bit (set by `fd_write` and `O_TRUNC` open)
  gates the size-flush in `fd_close`, so an unwritten writable open doesn't
  clobber the file's directory entry on close.  `SYS_IO_DUP` (11h) and
  `SYS_IO_DUP2` (12h) expose the fd table to userland for the bash save / dup2 /
  restore redirection pattern.
- **Boot-time stash** is embedded inside `kernel.bin` at offset
  `BOOT_STASH_OFFSET` (= 2): `boot_disk` (1 byte) and `directory_sector` (2
  bytes). The kernel binary's first instruction is `jmp short high_entry`, which
  skips past these bytes; `boot.asm` writes them through `ES:BOOT_STASH_OFFSET`
  *after* the `kernel.bin` `INT 13h` read so the load doesn't clobber them.
  Embedding inside `kernel.bin` lets the bitmap allocator hand out the IVT/BDA
  region (phys `0x000-0x4FF`), the `0x600-0x7BFF` gap, the MBR landing zone
  (`0x7C00-0x7DFF`), and the dead post-MBR boot bytes.
- **Kernel stack** at phys `KERNEL_RESERVED_BASE..KERNEL_RESERVED_BASE+0x1000`
  (4 KB; currently ~`0x28000..0x29000`, shifts with `kernel.bin` size).
  `KERNEL_RESERVED_BASE = page_align(0x20000 + sizeof(kernel.bin))` is computed
  by `make_os.sh` and passed via `-DKERNEL_RESERVED_BASE=N` to the second
  `kernel.asm` pass and to `boot.asm`. Lives outside `kernel.bin` to avoid 4 KB
  of zero padding on disk; reachable immediately after paging because
  PDE[FIRST_KERNEL_PDE]'s direct map covers phys `0..0x3FFFFF`; reserved via
  `frame_reserve_range` at boot. Sized at ~10× the measured peak (~412 B across
  bbfs / ext2 / fault kill / network paths). `kernel_stack` / `kernel_stack_top`
  are `equ`s in `kernel.asm`. `high_entry` poison-fills the region with
  `0xDEADBEEF` at boot so a future stack-depth probe can find the high-water
  mark by scanning upward.

## Paging and address spaces

- **Resident kernel** (`kernel.bin`) is loaded at physical `0x20000` and runs at
  virtual `0xFF820000`. The kernel direct map at `0xFF800000..0xFFBFFFFF` (PDE
  1022, 4 MB) mirrors low physical RAM 1:1; the auto-grow PT loop in
  `high_entry` is a no-op at the current `FIRST_KERNEL_PDE = 1022` (a single PT
  covers the entire direct-map region). The resident kernel image plus reserved
  cluster is ~170 KB worst case, so 4 MB of direct map has 25× headroom;
  everything past 4 MB phys reaches the kernel through the kmap window.
- **Kmap window:** PDE 1023 (virt `0xFFC00000..0xFFFFFFFF`) is reserved for a
  kernel-only window of demand-mapped slots. `kmap_init`
  (`src/memory_management/kmap.asm`, called by `high_entry` after the kernel
  idle PD takes over) allocates one frame as the window PT and installs it at
  `kernel_idle_pd[1023]`. Every per-program PD inherits PDE 1023 verbatim
  through `address_space_create`'s kernel-half copy-image. `kmap_map(eax = phys)
  → eax = kernel_virt` fast-paths to `phys + DIRECT_MAP_BASE` when the frame is
  below the direct-map ceiling; for higher frames it claims one of
  `KMAP_SLOT_COUNT` (= 4) slots in the window, writes a PTE, and `invlpg`s the
  slot. `kmap_unmap` releases the slot (no-op for the direct-map fast path). 4
  slots is sized for the deepest concurrent nesting in the tree
  (`address_space_destroy` walks a PD slot and a PT slot at once); slot
  exhaustion panics. Every "phys → kernel-virt to read/write" path in the kernel
  goes through `kmap_map`/`kmap_unmap`, so the bitmap allocator can hand out
  frames anywhere in `[0, FRAME_PHYSICAL_LIMIT)` (~4 GB) and the kernel still
  reaches them.
- **Per-program address spaces:** each program runs in its own page directory
  built by `address_space_create` from `program_enter`. The PD's kernel half
  (PDEs `FIRST_KERNEL_PDE..1023` = 1022..1023) is copy-imaged from
  `kernel_idle_pd` (a 4 KB kernel-only PD built once at boot — see below) so the
  kernel direct map and kmap window are reachable from every address space. The
  user half (PDEs 0..1021) is populated only with the program's own pages plus a
  shared vDSO PTE marked with the `ADDRESS_SPACE_PTE_SHARED` AVL bit (so
  `address_space_destroy` skips `frame_free` on it). Program binaries are
  streamed directly from disk into the freshly-allocated user frames (via
  `vfs_read_sec` + `sector_buffer` + a private `program_fd` slot in entry.asm
  BSS) — there is no kernel-side staging buffer. See
  [`memory_map.md`](memory_map.html) for the user-side virtual layout table.
- **Kernel idle PD:** a 4 KB kernel-only page directory allocated by
  `high_entry` after the kernel-PT-alloc loop runs. Built by copy-imaging the
  boot PD's kernel half (PDEs `FIRST_KERNEL_PDE..1023`) into a frame_alloc'd
  frame and leaving PDEs 0..`FIRST_KERNEL_PDE - 1` zero. Triple-roled: (1)
  canonical kernel-half PDE source for `address_space_create`, (2) CR3 between
  programs (e.g. `shell_reload` runs on it), (3) CR3-swap target in `sys_exit` /
  kill-path teardown (which cannot run on the dying user PD it is about to
  `frame_free`). Lives wherever the bitmap allocator returned a frame, so it
  isn't pinned in the kernel-side reserved cluster — `kernel_idle_pd_phys`
  (entry.asm BSS) holds its phys. Once the idle PD takes over, the boot PD's 4
  KB frame is freed back to the bitmap pool: that 4 KB cluster slot becomes a
  regular conventional frame the allocator can hand out for user pages.

## Build-time derivation

- Kernel sector count and reserved-region base are both derived at build time:
  `make_os.sh` measures `kernel.bin`, passes the sector count to `boot.asm` as
  `-DKERNEL_SECTORS=N`, computes `KERNEL_RESERVED_BASE = page_align(0x20000 +
  sizeof(kernel.bin))`, then re-assembles `kernel.asm` and `boot.asm` with
  `-DKERNEL_RESERVED_BASE=N`. A size-invariant check between the two
  `kernel.asm` passes confirms the change cannot shift the binary. A separate
  VGA-hole assert verifies that `KERNEL_RESERVED_BASE + reserved-region-size <
  0xA0000` so the kernel-side fixed-phys regions never cross the VGA aperture
  (which is what lets the OS boot under QEMU `-m 1`). The boot-time
  `kernel_bytes` word at MBR offset 508 holds `(BOOT_SECTORS + KERNEL_SECTORS) *
  512` so `add_file.py`'s host-side `compute_directory_sector` arithmetic still
  works.

## Signal delivery

Signal delivery is split into two independent axes — detection and delivery —
and three dispatch modes depending on the handler registered by the program.
Three signals share this path: SIGINT (Ctrl+C), SIGPIPE (write to a pipe with no
readers), and SIGALRM (interval-timer expiry, see [SIGALRM and interval
timers](#sigalrm-and-interval-timers) below).

### Detection

Four paths set per-signal pending bits (single bytes in the per-slot
`program_state` — `pending_sigint`, `pending_sigpipe`, `pending_sigalrm`):

- **PS/2 IRQ 1** (`src/drivers/ps2.c`): the cooked-byte path recognises the
  Ctrl+C scancode sequence and sets `pending_sigint` before returning from the
  IRQ handler. Because IRQ 1 fires for every keypress regardless of what the CPU
  is executing, this path works unconditionally — even a tight compute loop in
  user code is interrupted.
- **Serial 0x03 read** (`src/fs/fd/console.c`, `fd_read_console`): the serial
  poll branch checks each received byte; if it equals `0x03` (ASCII ETX, the
  byte a terminal sends for Ctrl+C) it sets `pending_sigint` and does not
  enqueue the byte into the line buffer.
- **`fd_write_pipe`** (`src/fs/fd.c`): when a writer resumes from
  `kernel_yield_write` and observes `pipe_reader_open(p) == 0`, it sets
  `pending_sigpipe` on the current slot before returning -1 to userspace.  The
  syscall epilogue's `SIGNAL_TAIL_CHECK` then delivers SIGPIPE — `SIG_DFL` kills
  the writer before the -1 surfaces; `SIG_IGN` clears the pending bit and lets
  the caller see the -1 (`EPIPE`).
- **PIT IRQ 0** (`src/arch/x86/entry.asm`, `pmode_irq0_handler`): when an alarm
  is armed (`alarm_deadline != 0`) and `system_ticks` reaches the deadline, the
  handler sets `pending_sigalrm` and either re-arms (`alarm_interval != 0`) or
  clears the deadline (one-shot).  IRQ 0 fires every ms so latency is sub-tick.

### Delivery

Every interrupt and syscall return path passes through the `SIGNAL_TAIL_CHECK`
macro (defined in `src/include/irq_tail.inc`, inlined into the IRQ 0/5/6
handlers in `src/arch/x86/entry.asm`, the IRQ 1 handler `ps2_irq1_handler` in
`src/drivers/ps2.c`, the IRQ 6 handler `fdc_irq6_handler` in
`src/drivers/fdc.c`, and the INT 30h handler in `src/arch/x86/syscall.asm`). The
macro:

1. Checks the IRET frame's CS: if `RPL != 3` the signal is suppressed (the
   kernel itself does not receive signals — only user programs do).
2. Tests `in_signal_handler`; if set, falls through to popad + IRET (block
   re-entry until SYS_SYS_SIGRETURN clears the flag — POSIX-default same-mask
   behavior, single flag covers both signals).
3. Tests pending bits in signum order (lower = higher priority): `pending_sigint`
   (SIGINT, 2), then `pending_sigpipe` (SIGPIPE, 13), then `pending_sigalrm`
   (SIGALRM, 14).  If all clear, falls through to popad + IRET.
4. Loads `EAX` with the picked signal's handler (`sigint_handler`,
   `sigpipe_handler`, or `sigalrm_handler` — per-slot dwords in `program_state`,
   reset to `SIG_DFL` by `program_enter`) and `EDX` with the signum.
5. Branches on EAX: SIG_DFL → `signal_dispatch_kill` (does NOT clear the pending
   bit — the program is dying anyway); SIG_IGN → clear the pending bit
   corresponding to EDX, fall through; user-virt → `signal_dispatch_user` (which
   clears the pending bit itself and writes EDX into sigcontext+4 so the user
   handler signature is `void h(int signum)`).

### Dispatch modes

- **`SIG_DFL` (0)** — `signal_dispatch_kill`: resets ESP to the current slot's
  per-slot kernel stack top (so pipeline children don't trample slot_a's stack
  while it's parked at `kernel_yield_to_pipeline_start`), calls
  `address_space_destroy` on the current program's PD, prints a signum-specific
  banner to the console (`^C` for SIGINT, `^P` for SIGPIPE, `^A` for SIGALRM,
  `^?` for the corrupt-sigcontext kill from
  `signal_resume_after_handler`'s validation failure), and falls into
  `child_terminate`. This is the out-of-the-box terminate behaviour: a runaway
  program is killed and the shell prompt reappears.
- **`SIG_IGN` (1)** — clear the corresponding pending bit (already done by
  `SIGNAL_TAIL_CHECK`), resume the IRET path unchanged. The signal is silently
  discarded; the program continues as if nothing happened.
- **User handler (virt addr ≥ `PROGRAM_BASE`)** — `signal_dispatch_user`: builds
  a 52-byte `sigcontext` record on the user stack (pushed below the current user
  ESP), rewrites the IRET frame so the CPU returns to the handler address at
  ring 3, and leaves `[user_esp]` pointing at the `sigcontext`. The saved
  context captures EIP, EFLAGS, ESP (pre-signal), the signum at offset +4 (so
  the handler reads it as its int argument after the trampoline `ret`), and the
  8-dword pushad register block (EDI, ESI, EBP, ESP_pushad, EBX, EDX, ECX, EAX
  in pushad's natural order so build/restore can use a single `rep movsd` each)
  — transparent to the interrupted code.

### Handler resume via vDSO trampoline

The vDSO page (mapped read-only at user-virt `0x10000`) contains a
two-instruction trampoline `__kernel_sigreturn` at user-virt `0x10450`
(`FUNCTION_TABLE + VDSO_SIGRETURN_OFFSET`):

```nasm
mov ah, SYS_SYS_SIGRETURN   ; AH = F6h
int 30h
```

`signal_dispatch_user` writes the trampoline address as the first dword of the
on-stack sigcontext so the handler executes a plain `ret` to reach it. After the
trampoline pops that return address, the user ESP points one dword into the
sigcontext (so saved_eip lives at `[user_esp + 4]`, saved_eflags at `[user_esp +
8]`, saved_esp at `[user_esp + 12]`, etc.). `sys_sigreturn` (INT 30h AH=F6h)
then:

1. Validates that the sigcontext's saved_eip and saved_esp are both within the
   user address space (`PROGRAM_BASE..KERNEL_VIRT_BASE`); failure routes to
   `signal_dispatch_kill`.
2. Restores EIP, ESP, and a sanitized subset of EFLAGS (arithmetic flags + DF +
   TF; IF forced on, IOPL/VM/NT/RF cleared per `USER_EFLAGS_MASK` in
   `src/include/constants.asm`), plus the general-purpose registers from the
   sigcontext.
3. Clears `in_signal_handler` and (if either `pending_sigint` or
   `pending_sigalrm` is set — a signal arrived while the handler was running)
   re-dispatches in priority order (SIGINT first) before the final `iretd`, so
   back-to-back signals are not lost.

### Cooperative interruption of blocking syscalls

Long-blocking syscalls poll both pending bits between iterations and bail out
early rather than forcing a delivery through the IRET path:

- **`fd_read_console`** (the console read loop in `src/fs/fd/console.c`): checks
  `pending_sigint || pending_sigalrm` after each character poll cycle. If either
  is set, returns immediately with `CF=1, AL=ERROR_INTERRUPTED` without
  consuming the flag (the `SIGNAL_TAIL_CHECK` epilogue handles final delivery).
- **`rtc_sleep_ms`** (the busy-wait loop in `src/drivers/rtc.c`, called from
  `SYS_RTC_SLEEP`): checks both pending bits each tick. Same early-exit
  convention; `SYS_RTC_SLEEP` propagates as `CF=1, AL=ERROR_INTERRUPTED` so
  libc's `sleep()` wrapper can surface `EINTR`.
- **`MIDI_IOCTL_DRAIN`** (the `sti`/`hlt` drain loop in `src/fs/fd/midi.c`):
  checks both pending bits after each `hlt` wakeup. Same early-exit convention.

The libc `errno` layer in `tools/libc/syscall.c` maps `ERROR_INTERRUPTED` to
`EINTR`, so portable C programs using `read()` / `sleep()` get the standard
POSIX interrupted-call semantics.

### Known limitation (v1)

Serial Ctrl+C is detected only while `fd_read_console` is actively polling the
serial port — a program that never calls `read(0, ...)` over serial cannot be
killed via serial Ctrl+C. PS/2 Ctrl+C has no such restriction because IRQ 1
fires unconditionally from hardware.

## SIGALRM and interval timers

SIGALRM (signum 14) is armed by user programs via `SYS_RTC_ALARM` (30h): `EBX =
ms_until_first_fire` (0 = cancel any pending alarm), `ECX = ms_interval` (0 =
one-shot; non-zero = repeating).  The syscall stores `system_ticks + EBX` into
`alarm_deadline` and `ECX` into `alarm_interval`; both reset to zero on
`program_enter` so alarms do not survive `exec` (matching POSIX `setitimer`
semantics). The return value is the ms remaining on the previously-armed alarm
(0 if none was armed).

PIT IRQ 0 fires SIGALRM by setting `pending_sigalrm` when `system_ticks` reaches
`alarm_deadline`, then either re-arms (`alarm_interval != 0`) or clears the
deadline (one-shot). Coalescing is automatic: if `pending_sigalrm` is already 1
when the deadline hits again (handler hasn't run yet), the second fire is
dropped — same single-bit POSIX standard-signal contract as SIGINT.

Delivery is identical to SIGINT — `SIGNAL_TAIL_CHECK` picks SIGINT first then
SIGALRM (see the Delivery section above).  Default action is terminate (matches
Linux `signal(7)`); the `^A` kill banner distinguishes it from SIGINT's `^C`.

Userland surface: `unsigned int alarm_ms(unsigned int delay_ms, unsigned int
interval_ms)` (BBoeOS extension) and the POSIX `unsigned int alarm(unsigned int
seconds)` wrapper, both in `tools/libc/signal.c`.  cc.py-compiled programs call
`alarm_ms()` directly via the matching builtin.

## Cooperative pipes (`cmd1 | cmd2`)

BBoeOS v1 supports a single-pipe two-command pipeline via `SYS_SYS_PIPELINE2` (F3h).
The shell parses `cmd1 | cmd2` at the top level of each command segment (after
chain operators `;`, `&&`, `||` split the command line): a single unquoted `|`
triggers pipeline mode; multiple `|` or a `|` combined with `<`/`>`/`>>`
redirections are rejected at parse time.

### Slot layout

The kernel maintains three `program_state` slots in BSS (`entry.asm`):

- **slot_a** — always the shell. `SYS_SYS_PIPELINE2` is only callable from slot_a
  (nested pipelines are rejected with `ERROR_INVALID`).
- **slot_b** — cmd1 (writer side). Its `fd[STDOUT]` is replaced with an
  `FD_TYPE_PIPE_W` entry pointing at the allocated pipe pool slot.
- **slot_c** — cmd2 (reader side). Its `fd[STDIN]` is replaced with an
  `FD_TYPE_PIPE_R` entry pointing at the same pipe pool slot.

### Pipe pool

`src/fs/pipe.c` maintains a static pool of `MAX_PIPES = 4` `struct pipe` objects,
each occupying exactly one 4 KB frame (sized to match `PIPE_SIZE` in
`constants.asm`). The ring buffer inside the struct is `PIPE_BUFFER_BYTES = 4076`
bytes. Fields at known offsets (`PIPE_OFFSET_*` in `constants.asm`) let both
kernel C code and `syscall.asm` reach the same struct.

### Cooperative scheduling

`SYS_SYS_PIPELINE2` builds both child slots atomically — allocates the pipe, builds
slot_b (cmd1 writer), builds slot_c (cmd2 reader), marks both `STATE_RUNNING`, and
calls `kernel_yield_to_pipeline_start` to hand off to the first child.

`kernel_yield` (`src/arch/x86/entry.asm`) is the cooperative scheduler:

1. Saves the current slot's kernel ESP to its `program_state.saved_esp`.
2. Marks the current slot with the caller-supplied state (`STATE_BLOCKED_READ`,
   `STATE_BLOCKED_WRITE`, or `STATE_EXITED`) and parks the slot on the pipe if
   blocking.
3. Scans slot_b then slot_c for the first `STATE_RUNNING` slot; switches CR3 and
   loads the target's saved ESP.
4. If neither child is `STATE_RUNNING` and both are `STATE_EXITED`, falls back to
   slot_a — resuming the shell inside `SYS_SYS_PIPELINE2`'s epilogue, which reads
   cmd2's wait status, wipes both slots, and returns to the shell.
5. If neither child is `STATE_RUNNING` and at least one is not `STATE_EXITED`, the
   scheduler panics (prints `*` on COM1 and halts) — this is a deadlock condition
   that should be unreachable with a single pipe.

### Block and wake

`fd_read_pipe` (`src/fs/fd.c`) loops: try to drain the ring buffer; if empty and
the writer end is still open, call `kernel_yield_read(p)` to park the reader and
yield.  `fd_write_pipe` loops symmetrically: try to fill the ring buffer; if full
and the reader end is still open, call `kernel_yield_write(p)` to park the writer
and yield.  Each successful `pipe_buffer_read` or `pipe_buffer_write` also calls
`pipe_wake_writer` or `pipe_wake_reader` respectively to unpark the blocked peer.

### Exit and teardown

When a pipeline child calls `sys_exit`, `child_terminate` runs the fd-close loop
(which decrements the per-end open refcount and wakes the peer if the last writer
or reader closes), marks the slot `STATE_EXITED` with `kernel_yield`, and the
scheduler resumes the peer or the shell as described above. The shell's
`SYS_SYS_PIPELINE2` epilogue wipes both child slots and clears `pipeline_active`.

### v1 limitations

- Only one pipe (`cmd1 | cmd2`); chains of three or more commands are rejected.
- Pipe combined with I/O redirection (`cmd1 > file | cmd2`) is rejected.
- `SYS_SYS_PIPELINE2` can only be called from the shell (slot_a); programs cannot
  spawn nested pipelines.

### Per-child arguments

`SYS_SYS_PIPELINE2`'s ABI carries four user-virt pointers: `SI = left_path`,
`DI = right_path`, `DX = left_args`, `CX = right_args`.  The shell splits each
side at the first unquoted space, stashes the command name into
`pipe_left_path` / `pipe_right_path` (`bin/`-prefixed), and writes a
Linux-style `name args` string (program name followed by the user arg tail)
into `pipe_left_args` / `pipe_right_args` (256-byte BSS arrays).  The
`name args` shape ensures the child's `argv[0]` resolves to the basename
after `FUNCTION_PARSE_ARGV` runs.

For each child, immediately before `.populate_handoff_from_shell` runs (with
the shell's PD active, so the BSS pointer + `BUFFER` both resolve), the kernel
helper `.stage_pipeline_child_args` copies the NUL-terminated args bytes into
the shell's `BUFFER` slot (user-virt 0x1500) and writes `EXEC_ARG` =
`BUFFER`.  The subsequent handoff-frame copy carries both into the child's
new user_data frame at matching offsets, so the child's `FUNCTION_PARSE_ARGV`
prologue resolves the pointer through the child's PD just like it does for
`exec()`.
