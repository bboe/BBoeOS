# vDSO — Design

## Summary

Move the FUNCTION_TABLE and the `shared_*` helper bodies (`src/lib/print.asm`, `src/lib/proc.asm`) out of the kernel binary into a separately-assembled blob (`vdso.bin`) loaded at fixed user-virtual address `0x00010000`. The kernel embeds the blob and copies it into RAM at boot.

This is a Linux-vDSO-shaped design: a small, self-contained code blob that user programs call as if it were a library. All per-call scratch state lives on the user's stack, so the vDSO is code-only — no shared mutable memory. It exists to unblock Phase 3 of the paging milestone — once paging is on, kernel-virt code at the old FUNCTION_TABLE address (`0x7E00`) is unreachable from CPL=3 user code; the vDSO solves that by living entirely in user space.

The vDSO also gains us:

- A clean separation between "kernel" and "user-side library" code.
- A path forward for adding lightweight library routines without re-architecting (the FUNCTION_TABLE pattern was already a primitive vDSO; this just makes it explicit).
- A kernel binary that shrinks by ~700 bytes once `print.asm` + `proc.asm` move out.

## Goals

- Preserve the existing user-program ABI: programs continue to emit `call FUNCTION_PRINT_STRING`, `jmp FUNCTION_DIE`, etc. cc.py output is unchanged.
- Make the vDSO blob self-contained: no references to kernel-private addresses (`SECTOR_BUFFER`, `EXEC_ARG`).
- Ship vDSO migration as a **pre-paging** PR. After this PR lands, Phase 3 of the paging milestone can map the vDSO into per-address-space user PDs without further code changes.
- Multi-process compatible: code page is shared across processes; per-call state lives on the calling process's own stack, so concurrent calls in different processes (or different threads of the same process, when threads land) cannot collide.

## Non-goals

- Symbol resolution at program load (Linux uses ELF auxv `AT_SYSINFO_EHDR`; we hardcode the address in `constants.asm`).
- ASLR / randomized vDSO base.
- Per-thread vDSO state (no thread support yet).
- Adding new helper routines beyond what FUNCTION_TABLE already exposes.

## Layout

### Virtual address layout (post-paging; the design also works pre-paging using physical addresses)

| Range | Use | Permissions | Lifecycle |
|---|---|---|---|
| `0x00010000..0x00010FFF` | vDSO code page | R-X user | Shared physical frame, mapped into every user PD |
| `0x08048000..` | User program code | RW(X) user | Per-address-space private |

**No vDSO data page.** All per-call scratch state — the byte transit for char I/O, `printf`'s pad/width flags, `print_datetime`'s intermediate fields — lives on the user's stack, scoped to the calling helper. The vDSO is code-only.

**Why `0x00010000`?** The vDSO needs a fixed user-virt base because cc.py emits absolute addresses to `FUNCTION_DIE` etc. The address must (1) be within RAM pre-paging — physical `0x00010000` is well inside QEMU's default 128 MB; (2) not collide with kernel binary (`0x7C00..~0x12000`), user `PROGRAM_BASE` (`0x600`), kernel stack at `0x9FFF0`, or disk/NIC buffers (`0xE000..0xEE00`); (3) leave a NULL guard below it. `0x00010000` (64 KB) sits in a clean hole that satisfies all three. (An earlier draft picked `0x08046000` — adjacent to the eventual user-virt at `0x08048000` — but that physical address is past QEMU's default 128 MB RAM and writes to it failed.)

### Code-page contents (`vdso.bin`, position-dependent at `org 0x00010000`)

```
0x00010000  jmp shared_die                 ; FUNCTION_DIE
0x00010005  jmp shared_exit                ; FUNCTION_EXIT
0x0001000A  jmp shared_get_character       ; FUNCTION_GET_CHARACTER
0x0001000F  jmp shared_parse_argv          ; FUNCTION_PARSE_ARGV
0x00010014  jmp shared_print_byte_decimal  ; FUNCTION_PRINT_BYTE_DECIMAL
0x00010019  jmp shared_print_character     ; FUNCTION_PRINT_CHARACTER
0x0001001E  jmp shared_print_datetime      ; FUNCTION_PRINT_DATETIME
0x00010023  jmp shared_print_decimal       ; FUNCTION_PRINT_DECIMAL
0x00010028  jmp shared_print_hex           ; FUNCTION_PRINT_HEX
0x0001002D  jmp shared_print_ip            ; FUNCTION_PRINT_IP
0x00010032  jmp shared_print_mac           ; FUNCTION_PRINT_MAC
0x00010037  jmp shared_print_string        ; FUNCTION_PRINT_STRING
0x0001003C  jmp shared_printf              ; FUNCTION_PRINTF
0x00010041  jmp shared_write_stdout        ; FUNCTION_WRITE_STDOUT
0x00010046  ; (helpers begin here, ~1.1 KB total including read-only month-lengths)
```

5-byte `jmp strict near` stride matches today's table exactly. `FUNCTION_*` constants in `constants.asm` change from `0x7E00 + offset` to `0x00010000 + offset` — same offsets, new base.

### Per-call state (stack-allocated)

Each helper that needs scratch space allocates it inside its own stack frame:

- `shared_print_character` / `shared_get_character` reserve 4 bytes (1-byte transit + alignment) at top of frame; `ESI`/`EDI` points there for the `SYS_IO_WRITE` / `SYS_IO_READ` syscall.
- `shared_printf` builds an `EBP`-frame with 4 bytes of locals: `[ebp - 1]` = pad char, `[ebp - 2]` = field width.
- `shared_print_datetime` builds an `EBP`-frame with 12 bytes of locals (10 used, 2 alignment padding): year (word), days (dword), month / hours / minutes / seconds (each 1 byte). Day-of-month is read inline from `[print_datetime_days] + 1` at print time, so no dedicated field.
- `shared_parse_argv`, `shared_print_decimal`, `shared_print_byte_decimal`, `shared_print_hex`, `shared_print_ip`, `shared_print_mac`, `shared_print_string`, `shared_die`, `shared_exit`, `shared_write_stdout` need no scratch beyond saved registers.

The only read-only data the vDSO holds is `print_datetime_month_lengths` (24 bytes, 12 words), embedded in the code page below the helper bodies. Reads from R-X user pages are fine on x86 without NX.

**`EXEC_ARG` is NOT migrated in this milestone.** `shared_parse_argv` continues to read from `[EXEC_ARG]` where `EXEC_ARG = 0x4FC` (kernel-side, unchanged). Pre-paging this works because all memory is reachable from CPL=3. Post-paging, `EXEC_ARG`'s cross-address-space handoff is a Phase 4 concern — the vDSO migration is intentionally narrow and lands before paging.

## Build pipeline

A new `src/vdso/vdso.asm`:

- Top-of-file: `org 0x00010000`.
- Body: the 14-entry FUNCTION_TABLE jump block, then the ported `shared_*` helpers from `lib/print.asm` and `lib/proc.asm`. All scratch state — formerly `SECTOR_BUFFER`-backed (now stack), formerly module-static `printf_*` and `.print_datetime_*` (now `EBP`-frame locals).
- `shared_parse_argv` continues to read `[EXEC_ARG]` (= `0x4FC`, unchanged this milestone).
- Read-only `print_datetime_month_lengths` (12 words = 24 bytes) sits at the end of the helper bodies inside the code page.
- No 4 KB padding pre-paging — the binary is whatever size NASM emits (~1.1 KB today) and the kernel copies exactly that many bytes via `(vdso_image_end - vdso_image) / 4`. Phase 3+ adds padding (or zeroes the destination frame) when the blob gets mapped as a 4 KB user-readable code page, to avoid leaking trailing kernel bytes.

`make_os.sh` adds:

```sh
# Build the vDSO blob first; the kernel embeds it.
nasm -f bin -i src/include/ -o vdso.bin src/vdso/vdso.asm || exit 1
```

The kernel binary embeds `vdso.bin` via `%incbin`:

```nasm
        ;; vDSO image — copied to physical 0x00010000 at boot so user
        ;; programs can call FUNCTION_TABLE entries at the user-virt
        ;; address that's been baked into constants.asm.
vdso_image:
        incbin "vdso.bin"
vdso_image_end:
```

`vdso.bin` is whatever size NASM emits (~1.1 KB today). `print.asm` + `proc.asm` together were ~700 bytes; the net kernel-binary-size change is roughly +400 bytes.

## Kernel-side wiring

### Boot-time setup (one-time)

In `protected_mode_entry` (or its post-paging successor `high_entry`), before drivers/VFS init:

1. Copy `vdso_image` (~1.1 KB) to physical `0x00010000` — the user-virt address baked into `FUNCTION_TABLE`. Pre-paging this is a literal `rep movsd` to physical `0x00010000`; post-paging it's a copy through the direct map at `0xC0010000`.

That's it. There's no data page to zero — all per-call state is on the user stack.

### Per-program-load setup (post-paging — Phase 3+)

In `prog_load`, after `address_space_create` and before mapping the user program:

1. `address_space_map_page(pd, 0x00010000, vdso_code_phys, P|U)` — code page, R/W=0 (read-only executable).

`vdso_code_phys` is the shared frame allocated at boot. Stored in a kernel global.

`EXEC_ARG` handoff is **not** part of this design; the vDSO milestone leaves it at `0x4FC` and Phase 4 reworks it.

### sys_exit / kill path

The vDSO code page's frame is shared across all PDs, so it must NOT be freed when an address space is destroyed. The simplest invariant: the boot-time frame allocator marks the vDSO code frame as "permanent" before the bitmap initializes (or is allocated outside the bitmap range entirely). Then `frame_free(vdso_code_phys)` is a no-op. `address_space_destroy` walks user-half PTEs and calls `frame_free` on each; the code-page entry happens to free a permanent frame, which the bitmap silently ignores.

Alternative: have `address_space_destroy` skip the code-page virt address. Cleaner but introduces a special case. We pick the "permanent frame, free is a no-op" path.

## `constants.asm` changes

```nasm
;; FUNCTION_TABLE relocates from 0x7E00 (kernel-side) to 0x00010000 (vDSO).
%assign FUNCTION_TABLE 08046000h
%assign FUNCTION_DIE              FUNCTION_TABLE
%assign FUNCTION_EXIT             FUNCTION_DIE + 5
%assign FUNCTION_GET_CHARACTER    FUNCTION_EXIT + 5
%assign FUNCTION_PARSE_ARGV       FUNCTION_GET_CHARACTER + 5
%assign FUNCTION_PRINT_BYTE_DECIMAL FUNCTION_PARSE_ARGV + 5
%assign FUNCTION_PRINT_CHARACTER  FUNCTION_PRINT_BYTE_DECIMAL + 5
%assign FUNCTION_PRINT_DATETIME   FUNCTION_PRINT_CHARACTER + 5
%assign FUNCTION_PRINT_DECIMAL    FUNCTION_PRINT_DATETIME + 5
%assign FUNCTION_PRINT_HEX        FUNCTION_PRINT_DECIMAL + 5
%assign FUNCTION_PRINT_IP         FUNCTION_PRINT_HEX + 5
%assign FUNCTION_PRINT_MAC        FUNCTION_PRINT_IP + 5
%assign FUNCTION_PRINT_STRING     FUNCTION_PRINT_MAC + 5
%assign FUNCTION_PRINTF           FUNCTION_PRINT_STRING + 5
%assign FUNCTION_WRITE_STDOUT     FUNCTION_PRINTF + 5
```

`EXEC_ARG` is **unchanged** (`0x4FC`). The shell continues to write to it via `set_exec_arg()`; the vDSO's `shared_parse_argv` reads from it. Pre-paging this works because user code (CPL=3) can reach `0x4FC` in flat memory. Post-paging, this read becomes a kernel-page access from CPL=3 and will need to be reworked — but that's Phase 4's scope, not the vDSO migration's.

## Multi-process correctness

The design is trivially multi-process-safe:

- **Code page**: one shared frame, R-X user, mapped into every PD. Identical content visible to every process. Safe because code is immutable.
- **Per-call state**: lives on the user's own stack inside the helper's frame. Each process has its own stack; each call has its own frame. Two processes (or two calls from the same process) can't see each other's transient state because the bytes don't outlive the helper return.
- **Scheduler context switch** (future): each user thread already has its own stack; the vDSO doesn't add any shared mutable state on top of that.

Even multi-threading within a single process is safe at the vDSO layer: each thread has its own stack, so concurrent calls into `shared_printf` etc. don't race. (Threading would still need higher-level coordination for `int 30h` / `SYS_IO_*` — but that's a kernel concern, not a vDSO concern.)

## Pre-paging vs post-paging behavior

| | Pre-paging (this milestone) | Post-paging (Phase 3+) |
|---|---|---|
| Code page | Static at physical `0x00010000`, copied from `vdso_image` at boot | Same physical frame; mapped at user-virt `0x00010000` in every PD |
| Per-call state | User stack | User stack (unchanged) |
| `EXEC_ARG` | At physical `0x4FC` (unchanged) | Phase 4 concern — vDSO milestone doesn't touch this |
| Programs see... | FUNCTION_TABLE at user-virt = physical `0x00010000` (no paging, virt = phys) | FUNCTION_TABLE at user-virt `0x00010000` mapped to shared physical frame |

The transition is smooth: the same blob, same addresses, only the underlying mapping changes.

## Testing

### Pre-paging acceptance (this PR)

- `tests/test_asm.py` (35 self-host tests) — every test program calls FUNCTION_TABLE entries via cc.py-emitted code; if any byte in the new vDSO is wrong, programs break and tests fail.
- `tests/test_bboefs.py` (5 fs tests) — exercise shell + helpers under realistic disk operations.
- Manual QEMU sweep: `cat`, `cp`, `edit`, `ls`, `mkdir`, `mv`, `rm`, `rmdir`, `netinit`, `netsend`, `netrecv`, `dns`, `ping`, `arp`, `date`, `uptime`, `chmod`, `hello`. All FUNCTION_TABLE-using programs should produce identical output to pre-vDSO.

### Validation that the vDSO is actually being called

A one-liner: `xxd -s 0x00010000 -l 16 /proc/<qemu-pid>/mem` (or equivalent QEMU monitor command) shows the relocated jump table. Or simpler: verify by inspecting the build's `os.bin` to confirm `vdso.bin` is `%incbin`'d at the right kernel offset.

### Out of scope (covered by paging milestone)

- Per-address-space data isolation between concurrent programs (only one program runs at a time today).
- vDSO mapping into multiple PDs (no per-address-space PDs yet).

## Follow-up work

- **Phase 3 paging integration**: `prog_load` maps the shared vDSO code page (R-X user) into every new PD. The boot-time static-physical-address path is retired in favor of the per-address-space mapping path; the underlying physical frame is unchanged.
- **vDSO ELF metadata** (long-term): if we ever add a real dynamic linker, expose vDSO symbols via auxv `AT_SYSINFO_EHDR` instead of hardcoded `constants.asm` values.
- **EXEC_ARG handoff** (Phase 4): when the per-address-space layout lands, replace the kernel-side `[EXEC_ARG]` slot with a mechanism the user can read across the address-space transition (pass via syscall register, or introduce a vDSO data page just for this slot, or copy through a kernel buffer).
