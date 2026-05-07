---
title: File structure
nav_order: 90
---

# File structure

## Host-side build tooling

- `add_file.py` — Host-side script to add files to the drive image filesystem
- `cc.py` — Host-side C subset compiler (translates `src/c/*.c` to NASM-compatible assembly)
- `make_os.sh` — Build script (assembles kernel, compiles C programs via `cc.py`, creates floppy image)

## Shared includes

- `src/include/constants.asm` — Shared constants (`BUFFER`, `DIRECTORY_SECTOR`, `SECTOR_BUFFER`, `EXEC_ARG`, `NE2K_BASE`, `PROGRAM_BASE`, `SYS_*` syscall numbers, etc.)
- `src/include/dns_query.asm`, `encode_domain.asm`, `parse_ip.asm` — Shared DNS/IP helpers; see source headers for calling conventions.

## Boot and kernel core

- `src/arch/x86/boot/boot.asm` — Pre-paging boot binary (`org 0x7C00`): MBR + post-MBR real-mode bootstrap (second `INT 13h` read of `kernel.bin` directly into phys `0x20000`, E820 probe, PIC remap, A20, GDT load, `CR0.PE` flip) + 32-bit `early_pe_entry` (build boot PD + first kernel PT, enable paging, far-jump to `high_entry` at virt `0xC0020000`). No real-mode-to-PE relocation copy — `KERNEL_LOAD_PHYS = 0x20000` is the kernel's final home. Post-MBR region pads to the next 512-byte boundary; `BOOT_SECTORS` is derived (`(boot_end - post_mbr_continue) / 512`) so the count auto-grows when the boot code crosses a sector. `make_os.sh` only has to measure `kernel.bin`'s sector count for the `-DKERNEL_SECTORS=N` second-pass nasm invocation.
- `src/arch/x86/kernel.asm` — Post-paging high-half kernel (`org 0xC0020000` = `DIRECT_MAP_BASE + KERNEL_LOAD_PHYS`): `high_entry` (segment / GDT / IDT / stack setup, identity-drop, bitmap init, kernel-PT allocation, kernel_idle_pd build, boot-PD freeback) followed by `%include`s of every kernel subsystem (drivers, fs, helpers, net stack, syscall dispatcher, IDT, post-flip entry, frame allocator, address-space helpers) and the kernel GDT + vDSO blob (`incbin "vdso.bin"`).
- `src/arch/x86/idt.asm` — 32-bit IDT with CPU exception stubs and `INT 30h` gate; `idt_init` (called from `high_entry`) patches the high-half handler offsets at boot since the IDT_ENTRY macro can only emit the low 16 bits in `nasm -f bin` mode (section-relative labels reject `& 0FFFFh` / `>> 16` arithmetic). Any post-flip exception lands in `exc_common` and prints `EXCnn EIP=h CR2=h ERR=h` on COM1.
- `src/arch/x86/entry.asm` — `protected_mode_entry` (TSS patch, PIT + IRQ handler install, driver / VFS / NIC inits, banner) flowing into `shell_reload` (loads `bin/shell` and jumps), `program_enter` (fd reset, BSS zero via the trailer-magic protocol, ESP snapshot, `iretd` to ring 3 at `PROGRAM_BASE`), and the IRQ 0 / IRQ 6 handlers. Segment / GDT / IDT / ESP setup happens in `kernel.asm`'s `high_entry` before falling into `protected_mode_entry`; the ring-0 stack lives at virt `0xC0000000 + KERNEL_RESERVED_BASE` (~`0xC0028000` for the current kernel size — see Kernel stack note in CLAUDE.md), not in entry.asm.

## Memory management

- `src/memory_management/frame.asm` — Bitmap physical-frame allocator: `frame_alloc` / `frame_free` / `frame_init` (two-pass E820 walker — first pass finds the highest type=1 frame base, between passes sizes the bitmap, second pass marks free regions) / `frame_reserve_range`. Bitmap is sized at boot from E820, clamped to FRAME_PHYSICAL_LIMIT (~4 GB, the 32-bit phys ceiling), and lives in the post-kernel cluster at FRAME_BITMAP_PHYS. Frames above FRAME_DIRECT_MAP_LIMIT (the kernel direct-map ceiling) reach the kernel via the kmap window — see `kmap.asm`.
- `src/memory_management/access.asm` — `access_ok` user-buffer pointer-validation helper (rejects ranges that span the user/kernel boundary or wrap), invoked by syscall handlers before touching userspace memory.
- `src/memory_management/address_space.asm` — Per-program PD lifecycle: `address_space_create` (allocate PD, copy-image kernel half from `kernel_idle_pd`, build user PTs for handoff frame + vDSO + program text/BSS + stack), `address_space_destroy` (free user frames, skipping shared-AVL PTEs, then free PTs and PD), and the page-mapping primitives (`address_space_map_page`, etc.) those two drive. All PD/PT reads and writes go through `kmap_map`/`kmap_unmap` so a high-physical PD or PT frame stays addressable.
- `src/memory_management/kmap.asm` — kernel temporary-mapping window at PDE 1023 (virt `0xFFC00000..0xFFFFFFFF`). `kmap_init` (called from `high_entry` after the idle PD takes over) allocates one frame as the window PT and installs it at `kernel_idle_pd[1023]`; per-program PDs inherit it via the kernel-half copy-image in `address_space_create`. `kmap_map(eax = phys) → eax = kernel_virt` fast-paths to the direct-map alias when phys is below FRAME_DIRECT_MAP_LIMIT and falls back to a slot in the window for higher frames. `kmap_unmap` releases the slot. `KMAP_SLOT_COUNT = 4` covers the deepest concurrent nesting in the tree (PD + PT walks in `address_space_destroy`); slot exhaustion panics.

## Syscalls and system

- `src/arch/x86/syscall.asm` — `INT 30h` dispatcher: per-handler bodies inlined directly (fs_*, io_*, rtc_*, sys_*) and tail-jump shims into `src/syscall/syscalls.c` for the four net_* handlers. The `.iret_cf` path sign-extends AX into EAX before iret; `.iret_cf_eax` is the explicit-32-bit variant used by `io_read` / `io_write`, whose byte counts can exceed 32 767.
- `src/syscall/syscalls.c` — C bodies for the four non-trivial network handlers: `sys_net_mac`, `sys_net_open`, `sys_net_recvfrom`, `sys_net_sendto`. Reached via `call sys_net_X; jmp .iret_cf` shims in `syscall.asm`.
- `src/arch/x86/system.c` — `reboot` (8042 reset), `shutdown` (APM / QEMU / Bochs shutdown ports).

## Drivers

- `src/drivers/console.c` — Unified output: `put_character` (ANSI parser + screen + serial mirror with auto `\n` → `\r\n`) and `put_string`. Delegates raw bytes to `serial_character` (`drivers/serial.c`) and ANSI cursor / palette commands to the VGA helpers in `drivers/vga.c`.
- `src/drivers/serial.c` — COM1 driver: `serial_character` (output) and `serial_check` / `serial_read` (input, polled by `fd_read_console`).
- `src/drivers/ata.c`, `src/drivers/fdc.c` — Hardware disk drivers (ATA PIO and floppy DMA); called via `fs/block.asm`'s `read_sector` / `write_sector` dispatch (AX = 0-based sector number).
- `src/drivers/ne2k.c` — NE2000 ISA NIC driver (polled-mode Ethernet); I/O base `0x300`, IRQ 3.
- `src/drivers/opl3.c` — SB16 OPL3 register-write driver: chip probe + outb-based register writes used by `/dev/midi` (no IRQ; the kernel drains the queue from IRQ 0).
- `src/drivers/ps2.c` — PS/2 keyboard driver: `ps2_init`, `ps2_check`, `ps2_read`.
- `src/drivers/rtc.c` — RTC / PIT timer: tick counter, `rtc_sleep_ms` busy-wait, CMOS date read.
- `src/drivers/vga.c` — VGA driver: text and mode-13h helpers (`vga_set_mode`, `vga_clear_screen`, `vga_fill_block`, `vga_set_palette_color`, …) plus `fd_ioctl_vga` (the `/dev/vga` ioctl dispatcher for `VGA_IOCTL_MODE` / `VGA_IOCTL_FILL_BLOCK` / `VGA_IOCTL_SET_PALETTE`).

## Filesystem and VFS

- `src/fs/fd.c` — File descriptor table and dispatch: `fd_open` (synthesizes `/dev/vga` into `FD_TYPE_VGA` without touching the filesystem), `fd_read`, `fd_write`, `fd_close`, `fd_fstat`, `fd_ioctl`. Per-fd-type handlers live under `src/fs/fd/` (`audio.c`, `console.c`, `fs.c`, `midi.c`, `net.c`).
- `src/fs/fd/midi.c` — `/dev/midi` event-ring + dispatch (`FD_TYPE_MIDI = 6`).  256-slot ring of 6-byte `(delay_lo, delay_hi, bank, reg, value, reserved)` commands; the IRQ 0 drainer pops up to 16 events per 1 ms tick and forwards register writes through `src/drivers/opl3.c`.  Implements `fd_read_midi`, `fd_write_midi`, `fd_close_midi`, and `fd_ioctl_midi` (`MIDI_IOCTL_DRAIN` / `MIDI_IOCTL_FLUSH` / `MIDI_IOCTL_QUERY`).  Single-opener.
- `src/fs/block.asm` — Block I/O dispatcher: `read_sector`, `write_sector` (dispatches to fdc/ata based on `boot_disk`).
- `src/fs/bbfs.asm` — BBoeOS filesystem (VFS backend): `bbfs_chmod`, `bbfs_create`, `bbfs_find`, `bbfs_init`, `bbfs_load`, `bbfs_mkdir`, `bbfs_rename`, `bbfs_update_size`, plus internal helpers (`find_file`, `scan_directory_entries`, etc.).
- `src/fs/ext2.asm` — ext2 filesystem (second VFS backend, auto-detected by `vfs_init`).
- `src/fs/vfs.c` — VFS layer: runtime function-pointer table (`vfs_find_fn`, etc.), `vfs_found_*` state struct, thin wrapper functions (`vfs_find`, `vfs_create`, `vfs_rmdir`, …). Detects bbfs vs ext2 at boot and points the function pointers at the corresponding backend.

## Networking

- `src/net/net.asm` — Four-line orchestrator that `%include`s the protocol modules: `net/arp.asm`, `net/udp.asm`, plus `build/kernel-c/net/icmp.kasm` and `build/kernel-c/net/ip.kasm` (cc.py-compiled from `src/net/icmp.c` and `src/net/ip.c`). The NE2000 hardware driver itself lives in `drivers/ne2k.c`.

## Userland programs

- `src/c/` — user-facing programs written in the C subset: `arp`, `asm`, `cat`, `chmod`, `cp`, `date`, `dns`, `draw`, `echo`, `edit`, `ls`, `mkdir`, `mv`, `ping`, `rm`, `rmdir`, `shell`, `uptime`.
- `src/c/edit.c` — Full-screen text editor with gap buffer, Ctrl+S save, Ctrl+Q quit. All editor state is file-scope so cc.py parks it in BSS rather than auto-pinning to registers that `buffer_character_at` clobbers (it uses EDX/ECX as scratch). The 448 KB gap buffer (`edit_buffer[EDIT_BUFFER_SIZE]`, sized at `0x70000` to fit under the `-m 1` user-pool ceiling) and 2.5 KB kill buffer (`edit_kill_buffer[EDIT_KILL_BUFFER_SIZE]`) are BSS arrays — the per-program PD that `address_space_create` builds gets enough zero-filled user pages to back them via the trailer-magic protocol. Any source file in the tree fits with room to spare (the largest, `src/c/asm.c`, is ~131 KB). A single `read(fd, edit_buffer, EDIT_BUFFER_SIZE)` fills the buffer in one call: `SYS_IO_READ` returns the full 32-bit byte count via `EAX` (the dispatcher routes io_read through `.iret_cf_eax`, skipping the AX sign-extend that the rest of the syscall surface uses), so no chunking is needed up to the 448 KB buffer cap.
- `src/c/asm.c` — Self-hosted x86 assembler (two-pass; byte-identical to NASM for everything in `static/`). Phase 1 port: the driver and handlers still live inside a single file-scope `asm("...")` block that wraps `archive/asm.asm`'s original NASM source; follow-up PRs extract pieces into pure C one family at a time. Supported directives and mnemonics are documented in the inline-asm body.

## Doom port

- `tools/fetch_chocolate_opl.sh` — pinned-commit fetcher for Chocolate Doom's OPL music sources (`i_oplmusic.c`, `mus2mid.c`, `memio.c`, `opl_queue.c`, `midifile.c`, `opl.h`).  Drops them into `third_party/chocolate-doom-opl/` (gitignored) so the build can compile them.  Drives Doom's MIDI playback through the `music_opl_module` interface; `tools/build_doom.py` invokes the script before compiling.
- `tools/doom/chocolate_compat.h` — narrowly-scoped (~95 line) shim that papers over the chocolate-vs-doomgeneric drift (a few macros + typedefs Chocolate's OPL stack expects but the doomgeneric tree doesn't expose).
- `tools/doom/opl_bboeos.c` — OPL backend bridging Chocolate's `i_oplmusic.c` to the kernel's `/dev/midi`.  Translates `OPL_WriteRegister` / `OPL_AdjustCallbacks` / `OPL_SetPaused` calls into 6-byte midi commands; `OPL_Init` returns `OPL_INIT_OPL3` so `OPL_InitRegisters` enables the second register bank.

## Smoke tests

- `tests/programs/` — smoke tests written in the same C subset, kept separate from user programs because they exist solely to exercise specific kernel paths from `tests/test_programs.py` that need a real boot to verify: `bigbss` (256 KB BSS allocation across per-program PD pages — also kmap window end-to-end smoke), `bits` (bitwise operators `|` `^` `~` `<<` `>>` `&` and compound-assignment forms), `booltest` (booleanized comparison BinOps used as values), `cftest` / `fctest` (call-flow / function-call edge cases), `gptest` (user-fault `#GP` kill path in `idt.asm`'s `exc_common` — executes `cli` at CPL=3, expects shell respawn), `loop` (basic while-loop control flow with per-character `printf`), `loop_array` (`sizeof(arr)/sizeof(elt)` folding + indexed string-array reads via `write`), `nullderef` (`#PF` on virt 0 with the unmapped PTE[0] guard), `okptest` (user-buffer pointer validation via `access_ok`), `play_midi` (opens `/dev/midi`, queries presence, writes a few short notes — smoke test for the kernel queue + drainer), `stackbomb` (16-page user-stack overflow into the unmapped guard region), `stacktop` (asserts `USER_STACK_TOP = KERNEL_VIRT_BASE` to catch constant drift). Pure cc.py codegen tests (file-scope `asm()` escape, `#include` directive, `asm_register` global aliasing, file-scope BSS globals, brace-initialized global arrays) live in `tests/test_kernel_cc.py` as compile-and-inspect-asm pytest cases — no QEMU boot needed. Built and packaged into the drive image's `bin/` only when `make_os.sh` is run with `--with-test-programs`; the default build keeps them out of the image so a normal boot ships only the user programs. `tests/test_programs.py` passes the flag automatically.
- `tests/unit/test_midi_queue.py` — pytest unit tests for the kernel midi event ring + drain.  Compiles `tests/programs/midi_queue_harness.c` in host-native mode and exercises ring push / drain bounds, timing accumulator, FLUSH semantics, and overflow handling without booting QEMU.
- `tests/test_doom_music_qemu.py` — Doom OPL3 music integration test.  Boots Doom in QEMU with `wads/doom1.wad` and grep's the serial log for `BBoe_MusicInit`'s `OPL music enabled` / `OPL music unavailable` marker.  Doesn't capture audio (QEMU's `sb16` device doesn't emit OPL FM) — see `docs/requirements.md` for the QEMU OPL caveat.
