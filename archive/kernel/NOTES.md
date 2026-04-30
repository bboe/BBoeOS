# Kernel Port Notes

Running notes on which kernel asm files have been investigated for a C
port and which haven't fit cleanly yet.  Useful as context for the next
session — what to pick up, what to leave alone.

## Ported

| File | Commit | Δ |
|------|--------|---|
| `archive/kernel/arch/x86/system.asm` | `reboot` (8042) + `shutdown` (ACPI / Bochs) | +24 |
| `archive/kernel/drivers/ata.asm` | LBA28 PIO disk driver | +272 |
| `archive/kernel/drivers/console.asm` | full ANSI escape parser | +480 |
| `archive/kernel/drivers/fdc.asm` | DMA + IRQ-6 floppy controller driver | +224 |
| `archive/kernel/drivers/ne2k.asm` | NE2000 ISA NIC (polled mode) | +392 |
| `archive/kernel/drivers/ps2.asm` | first kernel port | +408 |
| `archive/kernel/drivers/rtc.asm` | CMOS RTC reads, PIT tick counter, sleep | +368 |
| `archive/kernel/drivers/serial.asm` | thin polled COM1 driver | +32 |
| `archive/kernel/drivers/vga.asm` | text + mode-13h driver, ioctl backend | +320 |
| `archive/kernel/syscall/{fs,io,net,rtc,sys}.asm` | dispatcher consolidation + 4 net_* handlers in C | +200 |
| `archive/kernel/fs/fd/net.asm` | raw-NE2000 fd read/write + `fd_write_buffer` extraction | +8 |
| `archive/kernel/fs/fd/console.asm` | PS/2 + COM1 polled console fd read/write | +48 |
| `archive/kernel/fs/fd/fs.asm` | fd_read_dir / fd_read_file / fd_write_file (4-byte struct fields, vfs sector loop) | +192 |
| `src/fs/fd.c` (in-place) | fd_alloc / fd_close / fd_fstat / fd_init / fd_lookup promoted from asm() body to C bodies | +208 |
| `src/fs/fd.c` (in-place) | fd_read / fd_write dispatchers + fd_ops table to C via cc.py `__tail_call` | +184 |
| `src/fs/fd.c` (in-place) | fd_ioctl dispatcher + fd_ioctl_ops table to C via `pinned_register("ebx")` so AL=cmd survives | +21 |
| `src/fs/fd.c` (in-place) | fd_open dispatcher to C using extern struct vfs_found dot-access; retires last asm dispatcher | -125 |
| `archive/kernel/fs/vfs.asm` | 13 thunks + `vfs_init` + per-fs function-pointer globals to C via file-scope function_pointer | +504 |

## Investigated, not ported (and why)

### `src/fs/block.asm` (14 lines)

Two-instruction tail-call dispatchers that pass register state through
unchanged:

    read_sector:  cmp byte [boot_disk], 80h ; jb fdc_read_sector ; jmp ata_read_sector
    write_sector: cmp byte [boot_disk], 80h ; jb fdc_write_sector ; jmp ata_write_sector

C with `__attribute__((naked))` plus an if/else tail-call would work,
but reading `boot_disk` from C requires either an `extern` declaration
or an `asm()` block — and `boot_disk` is defined in
`src/arch/x86/boot/bboeos.asm` (no header file).  The whole function
would devolve to a small `asm()` block; not a real C port.

### `src/lib/proc.asm` (83 lines)

`shared_die`, `shared_exit`, `shared_get_character` are mostly
`int 30h` syscall wrappers — register marshalling around an
inline-asm escape, with no real C content.  The one C-shaped function
is `shared_parse_argv` (split `[EXEC_ARG]` into argv-style pointer
array).  Worth porting eventually, but the file fights the
"snapshot + replace .asm with .c" pattern because three of its four
functions are ~95 % `asm()` block.

### `src/drivers/rtc.asm` (297 lines) — ported

Done; see `drivers/rtc.c`.  Constants moved to constants.asm in
the same commit.  Multi-byte returns (CH:CL/DH:DL date pairs and
DX:AX epoch) used `out_register` parameter-capture for the
internal helpers and a thin asm-shim wrapper for the public
`rtc_read_epoch` symbol.

### `src/drivers/fdc.asm` (374 lines) — ported

Done; see `drivers/fdc.c`.  Three structural notes for future
DMA+IRQ ports that follow this pattern:

- The IRQ stub is a file-scope `asm()` block that EOIs and
  `iretd`s — same shape as `ps2_irq1_handler`.  cc.py can't
  express `iretd` as a function return.
- Multi-byte register I/O helpers (`fdc_send` / `fdc_recv`) and
  the multi-byte CHS return from `fdc_lba_to_chs_internal`
  stay in `asm()` blocks; the C surface picks up the latter
  via two `out_register("cx")` / `out_register("dx")`
  parameters.
- Floppy boot was silently broken until the buffer-move PR
  refactor flushed it out: `fdc_motor_start` calls
  `rtc_sleep_ms` during `vfs_init`, which spins on
  `system_ticks` (advanced by IRQ 0).  IRQ 0 had been
  unmasked AFTER `vfs_init`, hanging the floppy-boot path.
  Fixed in PR #237 (move IRQ 0 unmask + `sti` ahead of the
  driver init chain) plus a `tests/test_floppy_boot.py`
  regression test.

### `src/drivers/console.asm` (229 lines) — ported

Done; see `drivers/console.c`.

### `src/syscall/{fs,io,net,rtc,sys}.asm` (419 lines combined) — ported

Done.  The five subfiles were all `%include`d into
`syscall_handler:`'s scope and used local labels (`.fs_chmod`,
`.iret_cf`, …) that broke when extracted as C functions with global
names.  Approach (mirroring the old pre-pmode branch):

- The trivial handlers (`fs_*`, `io_*`, `rtc_*`, `sys_exec` /
  `sys_exit` / `sys_reboot` / `sys_shutdown`) were inlined directly
  into `arch/x86/syscall.asm` body — each is 2-4 lines (`call
  <existing_function>; jmp .iret_cf`) plus the occasional
  `mov [esp + SYSCALL_SAVED_EDX], dx` for syscalls that return DX:AX.
  No `%include` subfiles, no C wrappers — they gained nothing from
  either.
- The four non-trivial network handlers (`sys_net_mac`,
  `sys_net_open`, `sys_net_recvfrom`, `sys_net_sendto`) ported to
  `src/syscall/syscalls.c`.  Real branching, fd-table inspection,
  per-protocol dispatch into `udp_send` / `udp_receive` /
  `icmp_receive` / `ip_send`.  cc.py's `carry_return` plus
  `out_register("ax")` keeps the asm-side return convention (`AX =
  result`, `CF = error`).
- The dispatcher invokes each net handler with a thin
  `call sys_net_X; jmp .iret_cf` shim from inside
  `syscall_handler:`'s scope.  `sys_net_sendto`'s shim pre-loads EAX
  from the saved-EBP slot at `[esp+8]` because the user passes
  `dst_port` via EBP (every other register was already taken).
- `check_shell` stays in the dispatcher as a local helper — it uses
  `repe cmpsb` over a local `db "bin/shell"` literal and shares the
  saved-regs frame; no real C content.

The `+200` byte cost is mostly cc.py's per-function frame setup on
the four C handlers plus the `call sys_net_X; jmp .iret_cf` shim
indirection at each table entry.  Trivial handlers stayed asm so
they pay zero cc.py overhead.

### `src/fs/fd/fs.asm` (147 lines) — ported

Done; see `fs/fd/fs.c`.  `fd_read_dir` / `fd_read_file` /
`fd_write_file` use a `struct fd { … int size; int position; … }`
layout where the 4-byte size and position fields needed cc.py to
grow new struct-field codegen (see landmines below).  The three
trailing `dd 0` slots (`fd_rw_descriptor_pointer`, `fd_rw_done`,
`fd_rw_left`) lifted to plain file-scope C globals.  `sector_buffer`
(an `equ` constant in `kernel.asm`) is in cc.py's `NAMED_CONSTANTS`,
so a bare `sector_buffer + byte_offset` reference in C emits the
symbol literal directly — usable as a pointer in `memcpy` arguments.

### `src/fs/vfs.asm` (85 lines)

Mostly two-instruction `jmp dword [fn_ptr]` thunks — would all become
`__attribute__((naked))` C functions with `asm("jmp dword [fn_ptr]")`
bodies.  The one substantive function (`vfs_init`) sets 13 function
pointers based on `ext2_init`'s result.  Tractable but, like
`fs/block.asm`, ends up as mostly `asm()` blocks with little real C.

## cc.py landmines discovered during these ports

Future ports should look out for these:

1. **`carry_return` CF mapping is inverted from intuition.**  cc.py
   emits ``return 1`` as CF clear and ``return 0`` as CF set.  asm
   conventions where CF=0 means success / CF=1 means error align if
   the C functions ``return 1`` for success — but this reads
   backwards if you're translating from asm.  See `drivers/ata.c`
   for the documented inversion.
2. **`preserve_register("ax")` only saves the low 16 bits.**  In
   `--bits 32` mode cc.py emits 16-bit `push ax`/`pop ax`.  bbfs
   holds full 32-bit ECX file-size counters that get silently
   corrupted; switch to `preserve_register("eax")` etc.
3. **`#define` in C clashes with asm `equ`.**  cc.py emits
   `#define` as `%define`; if any included asm file defines the
   same symbol with `equ`, NASM throws a parse error.  Use bare
   integer literals in C when the same constant exists in any of
   the asm files in the include chain.
4. **`asm_name` on globals tells cc.py the storage lives elsewhere
   — no `_g_<name>` is emitted.**  If the C file is the actual
   owner, drop `asm_name` and add an `asm("name equ _g_name")`
   shim for asm callers to use the bare name.  For C↔C globals
   (storage owned by another `.c` file) use `extern T name;`
   instead — it skips the asm_name boilerplate and resolves
   directly to the owning file's `_g_<name>` storage (PR #258).
   `asm_name` stays the right tool for asm-side targets:
   referencing an asm `equ`/symbol that isn't in `NAMED_CONSTANTS`,
   or aliasing a C name to an asm symbol with an offset
   (`asm_name("vfs_found_size+2")`).
5. **`out_register("dx")` is a parameter attribute, not a function
   attribute.**  Declare functions that return via DX as
   `void f(int *out __attribute__((out_register("dx"))))` and
   call as `f(&var)` so cc.py emits the `mov [var], dx` capture.
6. ~~**Struct fields wider than 2 bytes are unsupported.**~~  Fixed
   while porting `fs/fd/fs.asm`: 32-bit struct field reads/writes
   now emit clean `mov eax, [bx+N]` / `mov [bx+N], eax`.  16-bit
   fields in 32-bit mode also got a width fix — they were
   accidentally compiling as 4-byte loads/stores; `movzx eax, word
   [...]` and `mov word [...], ax` now do the right thing.
7. ~~**`out_register("bx")` captured into a pinned 32-bit register
   emits `mov ebx, bx`.**~~  Fixed in the same port: width-mismatch
   captures now emit `movzx <wide>, <narrow>` so the upper bytes of
   the destination are clean.  Symmetric with the `in_register`
   prologue's existing widening path.
8. ~~**`out_register` works only on declarations, not C bodies.**~~
   Fixed in PR #241 (cc: partial-width register marshalling for
   in_register / out_register).  ``vga_get_cursor`` is now a C body;
   ``fdc_lba_to_chs_internal`` could be promoted similarly.  Same PR
   also corrected the symmetric ``in_register`` prologue bug where
   the spill into a 4-byte slot left the upper bytes uninitialised —
   prior kernel ports masked with ``& 0xFFFF`` / ``& 0xFF`` at every
   use site to paper over it.  The masks are no longer required for
   correctness but stay in place for clarity at byte-extraction
   sites (e.g. extracting DH/DL from a packed DX register).

## Suggested next ports

All identified ports landed.  PR #260 added three cc.py primitives
(``pinned_register`` for function_pointer locals, dot member access
on file-scope struct globals, file-scope function_pointer globals)
that unblocked the remaining cases:

1. **`fd_open`** ported via dot member access on an extern struct
   (lifted the `vfs_found_*` cluster into a single C-visible struct).
2. **`fd_ioctl`** ported via `pinned_register("ebx")` on the
   function_pointer local — `__tail_call` jumps via EBX so AL=cmd
   survives through to the handler.
3. **`fs/vfs.asm`** ported via file-scope function_pointer globals —
   the 13 thunks become carry_return tail-calls through the matching
   `vfs_X_fn` global, and `vfs_init` is a real C body that swaps the
   pointers when ext2 is detected.

Inline asm that remains in `src/`:
- The `vfs_found_*` data slots and `%include "fs/bbfs.asm"` /
  `%include "fs/ext2.asm"` directives in `src/fs/vfs.c`'s trailing
  `asm()` block.  bbfs.asm and ext2.asm reference the bare labels
  (`[vfs_found_size]`, etc.); rewriting both would be a much larger
  change with no real benefit.
- `vfs_chmod` thunk in `src/fs/vfs.c` — same AL/EAX collision
  `fd_ioctl` hits.  cc.py's `__tail_call` through a function_pointer
  global routes through EAX, which would clobber AL=mode before the
  handler reads it.  `fd_ioctl` works around this with
  `pinned_register("ebx")` on a function_pointer **local**; the
  attribute isn't yet supported on file-scope function_pointer
  **globals**, so this one thunk stays as a 1-instruction
  `jmp dword [_g_vfs_chmod_fn]`.  A future cc.py PR could extend
  `pinned_register` to globals.
- `src/fs/block.asm` (14 lines) — two-instruction tail-call
  dispatchers; the C version would devolve to mostly `asm()` with
  little real C content.
- `src/lib/proc.asm` (83 lines) — three `int 30h` syscall wrappers
  (~95% asm() blocks by nature) plus one C-shaped function
  (`shared_parse_argv`) that's tractable but low-value to extract.
- `src/fs/{bbfs,ext2}.asm` — the actual filesystem implementations.
  Substantial bodies of asm; outside the scope of "wire up the
  dispatch layer".

This concludes the port arc.

## Follow-up cleanup

Minor items surfaced during PR #239 review.  Each is small enough
that fixing it in-arc would have churned the commit history without
material benefit; recorded here so a future cleanup pass can pick
them up.

1. **`vfs.c` thunks emit a useless `mov [ebp-4], esi` spill before
   `jmp eax`.**  cc.py's frame setup over-eagerly spills the
   `in_register` param even though the body is just a `__tail_call`
   that doesn't read the spill back.  Adds a few bytes per thunk
   (already counted in the +504 vfs.asm port delta).  cc.py codegen
   refinement, not a kernel-side change.

2. **`put_string` in `src/drivers/console.c` is dead code** — no
   caller in `src/`.  Inherited from the asm version, not introduced
   by the port arc.  Worth a one-line deletion commit on main.

3. **`"Pure C: ..."` comments in `src/drivers/vga.c`** (lines 209,
   297, 310, 501) describe the porting process rather than code
   purpose; they signal a transient organizational distinction that
   won't age well now that the arc has closed.

4. **`archive/kernel/README.md` "C (bytes)" column** has the same
   value across all rows (the column is informational, but the
   uniform value is easy to miss as a stale snapshot).  Worth
   confirming `tools/measure_kernel_ports.sh` regenerates the
   column correctly when the underlying baseline changes.
