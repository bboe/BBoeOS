# Changelog

All notable changes to BBoeOS are documented in this file. Dates reflect
when changes landed, grouped under the version that was (or will be) current
at the time.

## [Unreleased](https://github.com/bboe/BBoeOS/compare/0.8.1...main)

## [0.8.1](https://github.com/bboe/BBoeOS/compare/0.8.0...0.8.1) (2026-04-28)

- **Bugfix:** floppy boot (`qemu-system-i386 -drive
  file=drive.img,format=raw,if=floppy`) works again after being
  silently broken for some time.  `protected_mode_entry` was running
  the driver init chain *before* unmasking IRQ 0 (PIT) and issuing
  `sti`, so `vfs_init` → `fdc_motor_start` → `rtc_sleep_ms` would
  busy-wait forever on `system_ticks`.  The fix moves the IRQ 0
  unmask + `sti` ahead of the driver inits.  A regression test
  (`tests/test_floppy_boot.py`) now exercises the floppy path on
  every CI run so this can't slip again.

- Rename the MBR-offset-508 size field from `stage2_bytes` →
  `kernel_bytes` (and `STAGE2_BYTES_OFFSET` → `KERNEL_BYTES_OFFSET` in
  `add_file.py`).  The `stage2_*` name was a fossil from the pre-merge
  stage1/stage2/kernel split that no longer exists; the post-MBR
  region is just "the kernel".  Drop adjacent stale prose from
  `CLAUDE.md`, `README.md`, `cc/target.py`, and `bboeos.asm` (vDSO
  base address comment was still showing the pre-relocation
  `0x08046000`).

### Paging prep (2026-04-28)
- Move the FUNCTION_TABLE and `shared_*` helpers (`lib/print.asm`,
  `lib/proc.asm`) into a separately-assembled vDSO blob (`src/vdso/vdso.asm`)
  loaded at virtual `0x00010000`.  The kernel embeds `vdso.bin` via
  `incbin` and copies it to physical `FUNCTION_TABLE` once at boot via
  `vdso_install`.  Helper bodies relocate their `SECTOR_BUFFER` and
  internal-static references to per-AS data at `0x00011000`.  User
  programs call `FUNCTION_DIE` / `FUNCTION_PRINT_STRING` / etc. exactly
  as before — only the addresses change.  Decouples user-side helper
  code from kernel-virt addressing ahead of paging.  Design at
  `docs/superpowers/specs/2026-04-28-vdso-design.md`.
- Probe the BIOS memory map via INT 15h AX=E820 in the MBR and stash
  24-byte entries at physical 0x500 (terminated by a zero entry).
  Result is unconsumed at this point — the post-paging bitmap frame
  allocator will use it to mark free vs reserved physical RAM.
- Widen the BSS trailer from 16 to 32 bits.  Programs now declare BSS
  via the new 6-byte trailer (`dd bss_size; dw 0xB032`); the kernel
  loader still accepts the legacy 4-byte form (`dw bss_size; dw
  0xB055`) for back-compat.  Lifts the per-program BSS cap from 64 KB
  to 4 GB ahead of paging, where `edit`'s 1 MB gap buffer becomes
  ordinary BSS.
- Add design + implementation plan documents in
  `docs/superpowers/specs/2026-04-28-paging-design.md`,
  `docs/superpowers/specs/2026-04-28-vdso-design.md`, and
  `docs/superpowers/plans/2026-04-28-paging.md` describing the
  Linux-shaped high-half-kernel + per-AS layout.

### Kernel
- Move userland programs to ring 3.  Add user code (0x18) and user data
  (0x20) GDT descriptors, a 32-bit TSS at selector 0x28 with SS0/ESP0
  pointing at the kernel stack, and raise the INT 30h gate to DPL=3.
  `program_enter` now reloads DS/ES/FS/GS to the user data selector and
  `iretd`s into a fresh ring-3 stack at `USER_STACK_TOP` (0x8FFF0)
  instead of `jmp PROGRAM_BASE`.  IRQ handlers, exception stubs, the
  syscall dispatcher, `sys_exit`, and `sys_exec` are all already
  cross-priv correct (the CPU auto-switches to TSS.ESP0 on ring-3 → 0
  and `iretd` pops the right number of dwords on the way back).
  Privileged instructions (`cli`/`sti`/`in`/`out`/CR writes) now #GP
  from user code.

### Programs
- Close the self-hosted `asm` assembler's 32-bit codegen gaps so its output is byte-identical to NASM under `[bits 32]`, and move `tests/test_asm.py` from its `--bits 16` pin to `--bits 32` (matching `make_os.sh`'s production path).  Six fixes in `src/c/asm.c`: 32-bit displacement (rel32) for `call`, `jmp`, and conditional-jump near forms (plus the convergence loop's short-vs-long sizing math); 3-character e-prefixed register names like `esi`/`edi` parse at the trailing `[disp+reg]` position; `handle_movzx` handles the direct-memory operand (`type2 == 2`); operand-size-prefix emission for `unary_f6f7` (mul/div/neg/not), `adc_sbb_handler`, and the 16-bit string ops (`lodsw`/`movsw`/`stosw`); and `emit_alu_mem_imm` (`add/and/or/sub/xor [mem], imm`) gains `dword` support and bits-aware encoding via `emit_modrm_direct`.  All 35 self-host programs assemble byte-identical to NASM at `--bits 32`.

### Toolchain
- `cc.py` now defaults to `--bits 32`.  The protected-mode merge made 32-bit the only production target (kernel + user programs both pass `--bits 32` explicitly in `make_os.sh`); the 16-bit default was a holdover.  `--bits 16` stays a working option (`tests/test_cc_bits.py` exercises both modes for cc.py front-end coverage), but production user programs and the self-host assembler regression both run at `--bits 32`.


## [0.8.0](https://github.com/bboe/BBoeOS/compare/0.7.0...0.8.0) (2026-04-27)

### Boot
- Port the kernel to flat 32-bit ring-0 protected mode.  The MBR keeps
  its real-mode preamble through the disk read, BIOS font load, PIC
  remap, A20, and 32-bit GDT load, then far-jumps into
  `protected_mode_entry` and stays in protected mode.  The previous
  `stage1.asm` / `stage1_5.asm` / `stage2.asm` / `kernel.asm` split
  collapses into one flat-binary `src/arch/x86/boot/bboeos.asm` whose
  [bits 16] MBR fronts a [bits 32] stage 2.  CPU exceptions vector
  through `idt.asm`'s `exc_common` and print `EXCnn` on COM1.
- Wire IRQ 0 (PIT @ 100 Hz) and IRQ 6 (FDC) handlers into the
  protected-mode IDT in `entry.asm`; the BIOS IVT-based handlers retire.
- `program_enter` resets fds, zeros the program's BSS region, snapshots
  ESP, and `jmp PROGRAM_BASE`.  `sys_exit` from any program restores
  ESP and re-enters `shell_reload`.

### Drivers
- Port ATA, FDC, NE2000, PS/2, RTC, and VGA to flat 32-bit addressing.
  Segment register loads removed; framebuffer / device-buffer writes
  use linear addresses (`0xB8000` for VGA text, `0xA0000` for mode 13h).
- Restore the boot-time `vga_font_load` that copies the BIOS ROM 8x16
  font into char-gen plane 2 offset 0x4000 — required for
  `vga_set_mode(VIDEO_MODE_TEXT_80x25)` to render glyphs (mode 03h's
  SR03=0x05 selects that slot).  Lives in
  `src/arch/x86/boot/vga_font.asm`, included in the MBR's [bits 16]
  region so it runs while INT 10h is still mapped.
- Drop the dead `rtc_tick_init` / `rtc_tick_irq0` from `drivers/rtc.asm`
  — the IVT-based PIT handler was orphaned by the protected-mode port
  (`protected_mode_entry` does the equivalent setup against the
  protected-mode IDT).
- Extract the COM1 driver from `ansi.asm` and `entry.asm` into
  `drivers/serial.asm`; rename `drivers/ansi.asm` to
  `drivers/console.asm`.

### Filesystem
- Port block I/O, VFS, bbfs, and ext2 to flat 32-bit addressing.

### Syscalls
- 32-bit INT 30h dispatcher.  Handlers receive args in E-regs with the
  same semantic shape as the 16-bit ABI (BX=fd, ESI/EDI=buffer,
  ECX=count, AL=flags).  Saved-EFLAGS CF propagates to the user via
  the iretd frame.
- Widen `io_read` / `io_write` to return full 32-bit byte counts so
  e.g. `edit` can read its 1 MB gap buffer in one call.

### Programs
- Port the self-hosted `asm` assembler to protected mode.  The symbol
  and jump tables move from a dedicated ES segment to flat extended
  memory at `SYMBOL_BASE = 0x300000`; flat 32-bit DS reaches everywhere
  so far-memory accessors no longer need a segment override.  Symbol
  values widen to 4 bytes for `JUMP_TABLE = 0x30F000` to round-trip
  cleanly.  All 35 self-host programs assemble byte-identical to NASM.
- Port `edit` to protected mode.  The 1 MB gap buffer and 2.5 KB kill
  buffer move from segment 0 to extended memory above the 1 MB mark
  (`EDIT_BUFFER_BASE` = 0x100000, `EDIT_KILL_BUFFER` = 0x200000).
  `archive/edit.asm` retired — the 16-bit C build can't represent a
  256 KB buffer base.
- Port `draw` and `vga_fill_block` to protected mode.  Drop the ES
  reload (real-mode segment 0xA000 #GPs in protected mode), widen DI
  to EDI, fold the framebuffer base 0xA0000 into the offset.

### Toolchain
- cc.py gains a `--bits 32` target backing the protected-mode port,
  along with a tide of orthogonal improvements: kernel-only port-I/O
  builtins (`kernel_inb` / `kernel_outb` / `kernel_inw` / `kernel_outw`
  / `kernel_insw` / `kernel_outsw`), `__attribute__((naked))` with
  if/else tail-call dispatch, double-pointer types, unsigned
  conditional jumps where comparisons have unsigned operands,
  `&array[i]` parse desugaring, `MemberIndex` AST node for
  `ptr->array[i]` reads, `far_read32` / `far_write32` for the wider
  symbol-table accessors, and four new peephole passes
  (`peephole_dead_temp_slots`, `peephole_register_arithmetic`,
  `peephole_self_move`, `peephole_redundant_register_swap`) that
  collectively shrink the user-program corpus by ~80 bytes per program.
- Fix `return` from `main` when `main` has a local stack array — now
  always emits `jmp FUNCTION_EXIT` regardless of `elide_frame`.

### Tests
- Add `tests/test_draw.py` covering draw + post-exit font restoration.
- Restore the full CI matrix (`test_archive`, `test_asm`,
  `test_bboefs`, `test_cc`, `test_draw`, `test_ext2`, `test_programs`).

## [0.7.0](https://github.com/bboe/BBoeOS/compare/0.6.0...0.7.0) (2026-04-23)

### Boot
- Shrink stage 1 MBR to the minimum required to load stage 2 and jump: set DS/ES/SS:SP, reset disk, INT 13h read, jump to `boot_shell`.  `clear_screen`, `WELCOME` / `DISK_FAILURE` strings, the `put_string` call, the dead geometry-query variables (`sectors_per_track` / `heads_per_cylinder`, written but never read), and the `pic_remap` / `rtc_tick_init` / `install_syscalls` / `network_initialize` calls all move into stage 2's `boot_shell` where the full console driver (`drivers/ansi.asm`) is available.  On disk error stage 1 now prints `!` via INT 10h AH=0Eh and halts instead of pulling in a string printer.
- Drop `src/arch/x86/boot/ansi_minimal.asm` entirely.  Its `put_string` and `serial_character` move into `drivers/ansi.asm` alongside `put_character` (their natural home — `serial_character` is the COM1 write primitive that `put_character` already called; `put_string` is a thin wrapper around `put_character`).  `put_character_raw` is removed — it only existed because `put_string` predated the full ANSI parser and needed an escape-free output routine; the new `put_string` calls `put_character` directly, which handles `\n → \r\n` the same way.  The file name was misleading anyway ("minimal" suggested a stage-1-only helper, but drivers/vga.asm and drivers/ansi.asm were calling `serial_character` out of it).
- `sys_exit` no longer re-prints the welcome banner on every shell reload.  Split `boot_shell` so `kernel_init`, `WELCOME`, and the one-time driver inits (`vga_font_load`, `ps2_init`, `fdc_init`, `vfs_init`) stay in the boot path, and a new `shell_reload` entry handles just `fd_init` plus the shell VFS load.  `sys_exit` now `jmp shell_reload` instead of `jmp boot_shell`.

### Tree layout
- Reorganize `src/kernel/` into Linux-style subtrees.  Only genuinely x86/PC-specific code lives under `src/arch/x86/`: `boot/` (bootloader: `bboeos.asm`, `stage1.asm`, `stage1_5.asm` — the protected mode switch, née `protected mode.asm`; `stage2.asm`), `idt.asm`, `pic.asm`, `syscall.asm` (INT 30h dispatcher), `system.asm` (8042 reboot + ACPI shutdown), and a new `kernel.asm` aggregator.  Hardware drivers lift to `src/drivers/` (`ata.asm`, `fdc.asm`, `ps2.asm`, `rtc.asm`, `vga.asm`, plus the NE2000 NIC moved out of `net/`, and `ansi.asm` as the console driver delegating to vga).  Filesystem code consolidates under `src/fs/` (`bbfs.asm`, `ext2.asm`, `fd.asm` + `fd/`, `block.asm` block dispatcher, `vfs.asm`).  Network stack in `src/net/` keeps the protocol layer only (`arp.asm`, `icmp.asm`, `ip.asm`, `udp.asm`).  Shared utilities in `src/lib/`, syscall handlers in `src/syscall/`.  `make_os.sh` adds `-i src/` so `%include "drivers/ata.asm"` / `"fs/fd.asm"` / `"net/net.asm"` / … resolve at the top level.  `src/arch/x86/boot/stage2.asm` no longer `%include`s the kernel itself — it contains only the boot handoff (jump table, `boot_shell`, `bss_setup`).  `bboeos.asm` now composes the flat binary as `stage1 + stage2 + kernel.asm`, where `kernel.asm` is the new aggregator that lists every subsystem in one place.  Motivation: the protected mode port is about to land on a dedicated `protectedmode` branch cut from `main`; the subtree is its natural home, and the boot / kernel split keeps `stage2.asm` focused on the boot-to-shell handoff instead of doubling as a kernel catalog.  `tests/test_pmode.sh` and `tests/test_idt.sh` both run green again (they had broken on the earlier `arch/` sub-move)

### Kernel
- New `pic.asm` / `pic_remap`: reprograms both 8259s so master IRQ 0-7 vector to 0x20-0x27 and slave IRQ 8-15 to 0x28-0x2F, leaving every line masked.  Called from `stage1.asm` right before `rtc_tick_init`, i.e. after the last BIOS INT 13h read but before any IRQ handler installs.  `rtc_tick_init` moves its IVT slot from 8*4 to 0x20*4 and now unmasks IRQ 0 at the master PIC itself (pic_remap leaves it masked); `fdc_install_irq` moves from 0Eh*4 to 26h*4.  Prerequisite for the upcoming protected mode flip — CPU exceptions 0-31 overlap the legacy BIOS PIC vectors, so IRQ 0 under BIOS defaults would alias onto the double-fault vector and IRQ 5 onto #GP
- `rtc_tick_init` reprograms the PIT from the BIOS default ~18.2 Hz to 100 Hz (10 ms/tick), giving `rtc_sleep_ms` 10 ms granularity (was 55 ms) and `uptime` sub-second precision underneath the `HH:MM:SS` display.  `TICKS_PER_SECOND` becomes 100; `rtc_sleep_ms` rounds to whole 10 ms ticks
- `fd_read_console`: `sti` at the top of the idle polling loop so PIT IRQ 0 can advance `system_ticks` while the shell is waiting for input.  Prior behaviour held IF=0 for the entire wait (syscalls enter with IF=0 and nothing re-enabled it), which silently starved the tick counter and kept `uptime` pinned at `00:00:00`
- New `SYS_RTC_MILLIS` (31h) returns `DX:AX` = milliseconds since boot, derived from `system_ticks × MS_PER_TICK` so the ms count is exact.  Existing `SYS_RTC_SLEEP` / `SYS_RTC_UPTIME` shift up to 32h / 33h to keep the group alphabetical.  cc.py's `ticks()` builtin (which emitted `int 1Ah`, dead since `rtc_tick_init` replaced the BIOS IRQ 0 handler) is replaced by `uptime_ms()` — full 32-bit `DX:AX` return when the caller assigns to `unsigned long`, low 16 bits when assigned to `int`.  `ping` prints `time=N ms` accordingly
- Extract `kernel_init` out of `boot_shell` into a new `src/arch/x86/init.asm`: single-entry routine running `pic_remap` / `rtc_tick_init` / `install_syscalls` / `network_initialize`.  Motivation is protected mode prep — once the flip lands, `rtc_tick_init` / `install_syscalls` become IDT-dependent and either move post-flip or gain 32-bit variants; encapsulating the sequence means that refactor edits `init.asm`, not `stage2.asm`.
- Rename `src/arch/x86/protected mode.asm` → `src/arch/x86/boot/stage1_5.asm` and colocate it under `boot/`.  The file is already the stage-1.5 of the boot flow (16→32-bit mode switch between the MBR and the protected mode kernel), so give it the positional name.  `tests/pmode_test.asm` and `tests/idt_test.asm` `%include` paths and their shell wrappers' `nasm -i` search paths follow.

### Drivers
- New native VGA mode-set driver (`vga_set_mode`) replaces the last INT 10h in stage 2 (the former `SYS_VIDEO_MODE`).  Table-driven register writer covering modes 03h (80x25 text) and 13h (320x200 256-colour): programs Misc Output, Sequencer 1-4, CRTC 0-18h, GC 0-8, and AC 0-14h in the standard unlock / reset / re-enable sequence.  New `vga_fill_block` writes an 8x8 tile into the mode-13h framebuffer at A000h:0 at a grid position with a palette-index colour.  `draw.c` rewritten to use mode 13h with real pixel tiles: 40x25 grid, WASD navigation, J/K palette cycle across 16 standard VGA colours, Q to quit back to text mode.
- Fix VGA cursor column always zero in `vga_set_cursor` / `vga_teletype` / `vga_write_attribute`.  `mul bx` clobbered DX before `movzx bx, dl` could read the column, so `col` was always 0 and every glyph wrote to column 0 of its row.  Switched to `imul ax, ax, VGA_COLS` (186+ three-operand form), which leaves DX intact.

### Filesystem
- ext2 correctness sweep targeted at `e2fsck` cleanliness.  `ext2_alloc_block` / `ext2_free_block` apply the `s_first_data_block` offset to the block-index ↔ bitmap-bit mapping.  Six new BGD/superblock counter helpers (`ext2_bgd_{block,inode,dir}_{alloc,free}`) called from every alloc/free path keep `bg_free_blocks_count` / `bg_free_inodes_count` / `bg_used_dirs_count` / superblock free counts in sync with the bitmaps.  `ext2_add_dir_entry` records the filetype byte (1 = regular, 2 = directory; `ext2_rename` carries it over from the old inode's `i_mode`).  `ext2_delete` / `ext2_rmdir` zero `i_links_count` alongside `i_dtime` so fsck no longer treats deleted inodes as in-use.  `ext2_update_size` updates `i_blocks = keep_blocks * sectors_per_block (+ indirect pointer block)` on shrink before flushing the inode.  `test_ext2.py` runs e2fsck after each test; all write-path tests (1 KB and 2 KB block sizes) pass with clean fsck.
- ext2 doubly-indirect block support across read, write, and shrink.  Read path was already in place; the write path (`ext2_prepare_write_sec` `.epws_alloc_doubly`) allocates and zero-fills the top pointer block at `i_block[13]`, the sub-singly pointer block at `outer_idx`, and the data block at `inner_idx` when `block_idx >= 12 + ptrs_per_blk`, each time updating `i_blocks` (top + sub-singly blocks count as `sectors_per_block` each, matching e2fsck).  The shrink path in `ext2_update_size` extends its saved block array to 14 entries, implements a partial-sub-singly inner loop for the fractional first entry, and guards the top-block free behind `dbl_keep == 0`; fixes four earlier bugs that saved `ptrs_per_blk` instead of the doubly-indirect block number, fell through into `.eus_grow` with stale inode data, orphaned all doubly-indirect blocks on partial-doubly shrinks, and miscounted `i_blocks`.  New `ext2_free_ind_block` helper replaces the inline indirect loops in `ext2_delete` / `ext2_rmdir` / `ext2_update_size` — uses index-based re-reads to avoid `SECTOR_BUFFER` clobbering.  Max file size becomes 268 KB for 1 KB blocks, 1028 KB for 2 KB blocks.
- ext2 `i_size` now stored as a full 32-bit value in `vfs_found_size`.  `ext2_find` previously took only the low 16 bits (hardcoding the high word to 0), so a 280 KB file with `i_size = 0x46000` read back as `0x6000` (24 KB) and `fd_read_file` hit EOF after the first 24 KB.
- `ext2_add_dir_entry` / `ext2_remove_dir_entry` scan all sectors of a directory block (the lookup path in `ext2_search_blk` already did).  Previously entries at block offsets ≥ 512 were skipped on writes, creating a read/write mismatch in directories that spanned past 512 bytes.  The "last entry in block" test now compares the absolute block offset against `block_size` instead of the sector-relative offset.
- ext2 frees orphaned blocks when a file is overwritten shorter (e.g. `edit` save-over or `cp` over a larger file).  `ext2_update_size`'s shrink path computes `keep_blocks = ceil(new_pos / block_size)`, zeroes the freed `i_block[]` entries, flushes the inode, then frees direct blocks `[keep_blocks..11]` and the singly-indirect block and its entries (partial or full).
- ext2 records timestamps on create / write / chmod via two helpers: `ext2_set_timestamps_now` (atime = mtime = ctime = now; called from `ext2_create` / `ext2_mkdir`) and `ext2_set_mtime_ctime_now` (mtime = ctime = now; called from `ext2_update_size` on both grow and shrink, and `ext2_chmod`).  atime is not updated on reads (relatime).
- ext2 cross-parent directory rename.  When `mv` relocates a directory to a different parent, `ext2_rename` updates the `..` entry in the moved directory's data block (offset 12, block 0, sector 0), decrements the old parent's `i_links_count`, and increments the new parent's.  File renames were already correct; the new logic is guarded by `filetype == 2 && old_dir != new_dir`.
- `ext2_mkdir` supports nested subdirectories via `ext2_resolve_path`: resolves the path to a `(parent_inode, basename)` pair before allocating the inode / block, so the `..` entry and `ext2_add_dir_entry` call both use the resolved parent inode rather than `EXT2_ROOT_INODE`.  ext2-only; bbfs retains its single-level limit.
- ext2 gains variable block size (1 KB / 2 KB), chmod, and subdirectory creation.
- New tests: `doubly_indirect_cat` / `doubly_indirect_cp_shrink` inject a 280 KB file at test build time and exercise the doubly-indirect read, write, and shrink paths.  `BLOCK_SIZE_TESTS` expanded from 23 to 33 entries so every write-path and directory-op test (`cat_large`, `chmod`, `cp_overwrite_shrink`, `mkdir`, `mkdir_ls_root`, `rename`, `rename_dir`, `rm`, `rmdir`, `rmdir_nonempty`) runs at both 1 KB and 2 KB block sizes.
- Rename `fs/fs.asm` → `fs/block.asm`.  The file is a 14-line block-device dispatcher that routes `read_sector` / `write_sector` to fdc or ata based on `boot_disk`; the old name was neither a filesystem nor the `fs/` orchestrator, so it's now named for its actual role.

### Syscalls
- `SYS_IO_IOCTL` (15h): device-control dispatch keyed on fd type.  `/dev/vga` (new `FD_TYPE_VGA`) is a synthetic device — `open("/dev/vga", O_WRONLY)` allocates an fd of that type without touching the filesystem, and `fd_ioctl` routes through `fd_ioctl_ops` to per-type handlers.  The VGA handler rejects fds that weren't opened writable and supports three cmds: `VGA_IOCTL_MODE` (DL=mode, also clears screen+serial), `VGA_IOCTL_FILL_BLOCK` (CL=col, CH=row, DL=color), `VGA_IOCTL_SET_PALETTE` (CL=index, CH=r, DL=g, DH=b).  The palette write lives in a new kernel `vga_set_palette_color` driver function instead of cc.py inlining `out dx, al` in every caller.
- Retire `SYS_VIDEO_MODE` (40h) and the `FUNCTION_VGA_FILL_BLOCK` jump-table slot: `video_mode` / `fill_block` / `set_palette_color` cc.py builtins now take an fd as the first argument and emit a single `int 30h` to SYS_IO_IOCTL.  `src/c/shell.c`, `edit.c`, and `draw.c` each open `/dev/vga` once in `main()` and pass the fd through.
- `SYS_FS_UNLINK` (04h): new syscall for deleting a file.  `vfs_delete` dispatches to `bbfs_delete` (zeroes the 32-byte directory entry, freeing the slot for reuse) or `ext2_delete` (frees direct and singly-indirect data blocks via `ext2_free_block`, frees the inode via `ext2_free_inode`, removes the directory entry).  New `ext2_free_bit` / `ext2_free_block` / `ext2_free_inode` helpers (inverses of `ext2_alloc_bit`).  The shell binary is protected from deletion.  cc.py gains an `unlink()` builtin; `src/c/rm.c` added.
- `SYS_FS_RMDIR` (03h): new syscall for removing an empty directory.  `vfs_rmdir` dispatches to `bbfs_rmdir` (finds the directory entry, verifies `FLAG_DIRECTORY`, scans all `DIRECTORY_SECTORS` of the subdirectory's data for occupied entries, then zeroes the parent directory entry) or `ext2_rmdir` (resolves the path, verifies `EXT2_S_IFDIR`, scans direct blocks via new `ext2_check_dir_empty` helper — skipping `.` and `..` — then frees direct+indirect blocks, frees the inode, removes the directory entry).  New `ERROR_NOT_EMPTY` (06h) returned when the directory is non-empty.  cc.py gains an `rmdir()` builtin; `src/c/rmdir.c` added.  `DIRECTORY_SECTOR` bumps 26 → 28 → 30 → 31 across this release to fit the expanding kernel.

### Userspace programs
- New `rm` and `rmdir` C programs built on `SYS_FS_UNLINK` / `SYS_FS_RMDIR`.
- `arp` / `cat` / `dns` / `edit` / `ls` / `netinit` / `netrecv` / `netsend` / `ping` now allocate their own BSS instead of reaching into kernel-shared buffers.

### Tooling
- cc.py: extend compound-assignment lexer to cover `-=`, `*=`, `/=` so the arithmetic family matches the bitwise/shift family (`+=`, `&=`, `|=`, `^=`, `<<=`, `>>=`).  Normalize every `var = var op rhs;` site across `src/c/*.c` to the compound form.  Two multi-term `x = x + a + b` sites in `dns.c` / `ping.c` stay as-is because the left-associative chain emits a tighter sequence than `x += a + b` (which parenthesizes the RHS and needs a scratch register)
- cc.py: add `%=` and fix a latent codegen bug it exposed — `peephole_dx_to_memory` folds the `mov ax, dx / mov [mem], ax` pair that a `%` expression emits into a direct `mov [mem], dx`, leaving AX holding the pre-fold value (the quotient from the preceding `div`).  Separately, `peephole_store_reload` was deleting the defensive reload `emit_store_local` emits, trusting the tracked `ax_local == name` invariant that the dx-to-memory fold silently violated.  Fix: teach `_peephole_will_strand_ax` to recognize the `mov ax, dx / mov [mem], ax` shape so `ax_local` gets cleared at store time, and reorder the pipeline so `dx_to_memory` runs before `store_reload`.  `bits.c` picks up a `y %= 13` smoke test, and `test_programs.py`'s `bits` regex matches that output so a regression is caught at the runtime layer too
- cc.py: extract the peephole pass into a standalone `Peepholer` class (née `PeepholeMixin`).  The pass only reads `self.lines` and `self.target` and shares no per-statement state, so the mixin was obscuring rather than expressing that boundary.  Call site becomes `self.lines = Peepholer(lines=..., target=...).run()` at emission.py:115.  Methods sorted to the canonical layout used by `cc/codegen/base.py` (dunder → underscore helpers → public).  Byte-identical output on all 35 self-hosting tests.
- cc.py: parse `constants.asm` at compile time via `parse_asm_constants()` and thread the resolved dict through `cli.py` → `X86CodeGenerator` → `CodeGenerator`, replacing the stale hardcoded `NAMED_CONSTANT_VALUES` class variable (which had `DIRECTORY_NAME_LENGTH=27` instead of the correct 25).
- cc.py: exclude BP from the pin pool when `main` has stack arrays, avoiding the register allocator claiming a register the frame-array code needs.
- cc.py: guard SI and invalidate `ax_local` around constant-base indexing.  Two related codegen bugs bit functions that used an `asm_register("si")`-pinned global (`source_cursor` in `asm.c`) alongside a constant-base (`_g_foo[...]`) array index on a non-constant index: (1) `_emit_constant_base_index_addr` clobbered SI without the `_si_scratch_guard_begin` / `_si_scratch_guard_end` pair the variable-index path emits, leaving the pinned `source_cursor` pointing at array-internal garbage; (2) `emit_comparison` with a pinned-register left operand against a memory-backed right operand set `ax_local = left.name` after `mov ax, reg / cmp ax, [mem]`, but `peephole_compare_through_register` then rewrote the pair as `cmp reg, [mem]`, leaving `ax_local` claiming AX held the pinned value even though the load was gone.  `ax_clear()` after the cmp forces the reload.  Asm and shell pick up a small size bump where they'd been relying on the stale AX value.
- cc.py: factor `emit_register_from_argument`, `emit_store_local`'s pinned-destination fast path, and `emit_si_from_argument` onto a shared `_try_direct_load(*, argument, register, optimize_zero)` helper covering integer literals, string literals, named constants, constant aliases, global arrays, local stack arrays, and constant-folded expressions.  Each caller retains only its truly-special branches (width-aware pinned / aliased-global loads and `ax_local` shortcut; generic expression fallback).  Array Vars dispatch through `_try_direct_load` before `_is_memory_scalar` so the base address is loaded (via `lea` / `mov _l_name`) instead of the contents.
- Self-hosted assembler (`src/c/asm.c`): factor the `<op> byte|word [disp16], imm` parsing and emission into a shared `emit_alu_mem_imm(rfield)` helper and extend coverage from `sub` (the only op the old inline in `handle_sub` knew about) to `add`, `and`, `or`, `sub`, `xor` at both byte and word widths.  Byte width always emits `80 /r ib` (5 bytes); word width picks the 5-byte `83 /r ib` sign-extended short form when the immediate fits signed 8-bit and falls back to the 6-byte `81 /r iw` form otherwise.  All shapes match NASM byte-for-byte.  `bits.c` exercises them via `y -= 5` (memory-allocated local), an `int counter` global stepping through `+=` / `|=` / `&=` / `^=`, and a `uint8_t bcounter` global stepping through `+=` / `|=` / `&=` / `^=` / `-=`; a printf between each op clobbers AX so the reload/op/store triple forms and `peephole_memory_arithmetic` / `_byte` fuses it into the memory-direct shape
- Self-hosted assembler: `%macro` / `%endmacro` support.  Single-parameter-token macros shaped to match `idt.asm`'s needs: `macro_names[]` / `macro_argcounts[]` / `macro_body_starts[]` / `macro_body_lengths[]` / `macro_body_buffer[]` hold the table; `macro_args_text[]` / `macro_arg_starts[9]` are per-invocation scratch.  `define_macro` (from `parse_directive`'s `%macro` branch) slurps lines into the body buffer until `%endmacro`; `find_macro` linear-scans the name table at `parse_mnemonic`'s top; `expand_macro` substitutes `%1..%9` into `line_buffer` and re-runs `parse_line` on each expanded line, so labels (`exc_%1:`) and directives (`dw`, `db`) work without special handling.  `static/macro_sm.asm` smoke-tests an `IDT_ENTRY` data macro and an `EXC_NOERR` label-defining / push / jump macro.
- Self-hosted assembler: add `in al, dx` / `in ax, dx` / `out dx, al` / `out dx, ax` (opcodes EC/ED/EE/EF).  Each handler validates that one operand is DX and the other is AL/AX, then the data-register size picks between byte and word encodings.  Needed so the self-hosted assembler can reassemble programs that talk directly to ports (e.g. `draw.c`'s DAC writes to 3C8h/3C9h).
- Self-hosted assembler: add `lea` and fix the alu-binop `[reg+disp]` encoding.
- Self-hosted assembler (`src/c/asm.c`): protected-mode extension (phase 5).  `parse_register` accepts the `e`-prefixed 32-bit general register file (eax / ecx / edx / ebx / esp / ebp / esi / edi); a dedicated `parse_creg` handles cr0..cr7; `emit_sized` prepends the 0x66 operand-size prefix for 32-bit widths; new `emit_dword` emits little-endian imm32 / disp32.  `handle_mov` gains `mov crN, r32` / `mov r32, crN` (0F 22 /r, 0F 20 /r) and `mov r32, imm32` with the 0x66 prefix; `emit_alu_reg_imm` extends to 32-bit operand size for the `or eax, 1` style encodings.  New `handle_lgdt` / `handle_lidt` (0F 01 /2, /3) and `jmp dword SEL:OFS` (0x66 0xEA ptr16:32) round out the protected mode bootstrap encodings.  `static/pmode_sm.asm` exercises the full set against NASM; byte-identical on the self-host test
- Self-hosted assembler phase 5.4: `push [word|dword] imm` is bits-aware.  Optional `word` / `dword` size token overrides `default_bits`; the imm tail widens to imm32 when the push is 32-bit.  `0x6A ib` short form still applies whenever the value fits ±128, independent of push width; only the 0x66 operand-size prefix reflects the push size.
- Self-hosted assembler phase 5.5: `mov` and `lgdt` / `lidt` direct-memory encodings are now bits-aware.  ModR/M `rm` flips 110 ↔ 101 for mod=00, and the displacement widens 16 ↔ 32.  Refactor `emit_modrm_direct` to pick both off `default_bits`; add `emit_address_disp` for the accumulator-direct `moffs` short form (A0/A1/A2/A3).  Adds the missing 0x66 operand-size prefix on the accumulator-short form so `mov eax, [foo]` under bits=16 emits `66 A1 disp16` instead of the old `A1 disp16`.
- Self-hosted assembler phase 5.6: 32-bit addressing — `[eax]..[edi]` base registers (with ESP's mandatory SIB byte and EBP's disp8=0 quirk for mod=00), plus the 0x67 address-size prefix when the address size disagrees with `default_bits`.  New state `parse_operand_address_size` set by `parse_operand`; new helpers `emit_address_size_prefix` / `emit_sized_mem` / `emit_indexed_mem`.  Ten call sites across `emit_alu_binop` / `handle_call` / `handle_cmp` / `handle_mov` / `handle_movzx` / `handle_test` / `inc_dec_handler` / `handle_lgdt` / `handle_lidt` route through them.  `parse_operand` learns the `dword` size prefix alongside `byte` / `word` for shapes like `cmp dword [reg], imm` and `inc dword [reg]`; `emit_sized_imm` widens to imm32 when requested.
- Self-hosted assembler phase 5.6 follow-up: `push [mem]` via the `FF /6` encoding (previously fell through to `resolve_value`, which silently evaluated `[foo]` as a 0 immediate and emitted `6A 00`), and `resolve_value` now recognises a leading `-` / `+` as a unary sign on the first term so `[bp-4+1]` evaluates left-associatively to `bp-3` (matching NASM) instead of `bp-5`.  Needed to restore self-host parity with NASM once phase 5.6 shifted cc.py's output to shapes that exposed the miscompiles.


## [0.6.0](https://github.com/bboe/BBoeOS/compare/0.5.0...0.6.0) (2026-04-21)

### Networking
- ICMP sockets via `(SOCK_DGRAM, IPPROTO_ICMP)`; ICMP echo requests now live in userspace
- `net_open` takes a protocol argument (Linux-style `(type, protocol)` API)
- Remove `SYS_NET_ARP` and `SYS_NET_PING` syscalls — both protocols migrated to userspace — and collapse the `SYS_NET_*` numbering

### Userspace programs
- Rewrite `shell`, `dns`, `ping`, `edit`, and `asm` (the self-hosted assembler) in C; `arp` / `netinit` / `netrecv` / `netsend` join them
- `edit` moves its gap buffer to fixed addresses — new `EDIT_BUFFER_BASE` / `EDIT_BUFFER_SIZE` / `EDIT_KILL_BUFFER` / `EDIT_KILL_BUFFER_SIZE` constants replace the former float-on-`program_end` layout
- `edit.c`: lift `gap_start` / `gap_end` to file-scope globals and factor 10 copies of the gap-buffer cursor-move idiom into `gap_move_left` / `gap_move_right` helpers

### Tooling
- Self-hosted assembler (`src/c/asm.c`): NASM → pure C migration completed in this cycle — every `handle_*` mnemonic handler, every `parse_*` stage, the symbol table, the include / file-I/O machinery, and the driver loop all live in C.  A trailing file-scope `asm(...)` block retains only the kernel-syscall wrapper, the mnemonic / register data tables, and the `STR_*` keyword strings.  The in-OS assembler also picked up `pusha` / `popa` / `lodsw` / `adc` / `not` so cc.py-emitted programs can be re-assembled in-place
- asm.c: collapse `emit_byte` sequences behind four helpers (`emit_word`, `emit_sized`, `emit_modrm_disp`, `emit_modrm_direct`) — shrinks the binary ~700 bytes and removes ~130 lines of near-duplicate operand emission
- asm.c: fold shared-body handler families onto regparm(1) helpers — `unary_f6f7` (mul/neg/not/div), `shift_handler` (shl/shr), `inc_dec_handler` (inc/dec) — another ~300 bytes off the binary
- asm.c: unify `add` / `and` / `or` / `sub` / `xor` onto one `emit_alu_binop(rfield)` helper — every opcode the instruction emits is a derivable function of rfield, so five near-identical 30-line bodies become one.  Another ~950 bytes off the binary, and `or ax, imm16` / similar shapes now encode with the proper short forms (matching NASM instead of the previous 81 /r iw long form)
- asm.c: smaller cleanups — `is_ident_char` / `scan_ident_dot` helpers retire the five open-coded `[a-zA-Z0-9_]` / `[a-zA-Z0-9_.]` loops; `parse_directive`'s `dw` / `dd` bodies share one operand loop
- asm.c: fold `handle_adc` / `handle_sbb` onto `adc_sbb_handler(modrm_base)` (they differed only in /r field 2 vs 3)
- cc.py: `emit_condition` wraps bare expressions (`Call`, `Var`, `Index`, …) as `expr != 0` when they reach it inside `&&` / `||`, so `while (foo() || x == 0)` compiles naturally alongside `if (foo())`; `return <expr>` in `carry_return` functions lowers the expression into CF via the same two-leg pattern the if form uses
- cc.py: tail-call optimization for frameless functions — a trailing statement-level call to a user function becomes `jmp name` instead of `call name; ret` when the call site has no stack args and no pinned registers to save.  Shrinks `asm.c` another 50 bytes (handle_clc, handle_mul and the other single-call-body handlers collapse to `mov ax, N ; jmp target`)
- cc.py: `peephole_dead_ah` scans forward across AX-preserving instructions (register moves not touching AX, pushes/pops of non-AX regs, `cmp` / `test` on non-AX operands) to find the AL-only consumer of a zero-extended byte load.  Catches patterns like `xor ah, ah ; pop si ; test ax, ax` that were previously missed because the immediate-neighbor check stopped at `pop si`.  31 bytes off across asm / edit / shell
- Host-side C compiler (`cc.py`): feature and codegen work in support of the above — file-scope globals, inline `asm(...)` escape, `#include` directive, `regparm(1)` / `carry_return` / `always_inline` / `asm_register` attributes, `uint8_t` type with byte-codegen for byte-typed globals and body locals, `far_read8/16` / `far_write8/16` builtins, new user-callable builtins (`checksum`, `ticks`, `exec`, `reboot`, `shutdown`, `set_exec_arg`), and many peephole / calling-convention improvements

## [0.5.0](https://github.com/bboe/BBoeOS/compare/0.4.0...0.5.0) (2026-04-16)

### [2026-04-16](https://github.com/bboe/BBoeOS/compare/84a1efe...5156ae9)

- Add CHANGELOG.md with full project history
- Add UDP socket support (`SOCK_DGRAM`) to `net_open`
- Add `net_recvfrom` and `net_sendto` syscalls with cc.py builtins
- Refactor cc.py: extract helpers, consistent `_` prefix, delete dead code, sort methods

### [2026-04-15 – 2026-04-16](https://github.com/bboe/BBoeOS/compare/8797ed7...84a1efe)

- Convert `arp` from assembly to C using raw Ethernet sockets
- Port `netinit`, `netsend`, and `netrecv` to C
- Add `SYS_NET_OPEN` and `FD_TYPE_NET` for raw Ethernet socket file descriptors
- Replace `SYS_NET_INIT` with `SYS_NET_MAC`; probe NIC at boot
- Add 60-second TTL aging for ARP cache entries
- Add shared `ARGV` buffer and `FUNCTION_PARSE_ARGV` for argument validation
- Use `int main` in C programs, rename `putc`/`getc`, support return expressions
- Add GitHub Actions CI workflow with clang syntax checking
- Configure pre-commit hooks
- Refactor test infrastructure: `run_qemu.py` driver, temp directory isolation, shared helpers
- Move test files to `tests/` directory
- Add `test_programs.py` runtime smoke suite
- Extensive cc.py compiler optimizations:
  - Constant folding and constant-pointer alias tracking
  - Peephole passes for redundant BX reloads, cld dedup, and memory arithmetic
  - Fuse argc checks into argv startup
  - Direct memory addressing for constant-base indexing and comparisons
  - Byte-indexed and word-fused comparison optimization

### [2026-04-14](https://github.com/bboe/BBoeOS/compare/fde140f...8797ed7)

- Rewrite `draw` program in C with ANSI escape output
- Rewrite `ls` and `mv` commands in C
- Rewrite `chmod` in C with `char*` byte indexing support
- Add `FUNCTION_PRINTF` with cdecl calling convention
- Add `rtc_datetime` epoch syscall and `FUNCTION_PRINT_DATETIME`
- Add `rtc_sleep` syscall; drop last user-land `INT 15h`
- Replace `SYS_SCREEN_CLEAR` with `SYS_VIDEO_MODE`
- Expand ANSI parser for draw and visual bell
- Add `fstat()` builtin to cc.py
- Add unsigned long support to cc.py
- Refactor C compiler AST from tuples to dataclasses
- Add `#define` object-like macros, `&&`, `||`, bitwise `&` to cc.py
- Compiler optimizations: constant folding, immediate-form instructions, redundant zero-init elimination, direct store peephole

### [2026-04-13](https://github.com/bboe/BBoeOS/compare/c2b7ace...fde140f)

- Add kernel jump table; migrate all programs off direct syscall includes
- Remove `SYS_IO_PUT_STRING`, `SYS_IO_PUT_CHARACTER`, `SYS_IO_GET_CHARACTER` syscalls
- Add `write_stdout` helper and convert all programs to use it
- Move argv parsing into kernel `FUNCTION_PARSE_ARGV`
- Move assembler symbol and jump tables to ES segment
- Console read now returns escape sequences for special keys
- Expand abbreviated identifiers and sort constants
- Rename `DISK_BUFFER` to `SECTOR_BUFFER`

### [2026-04-12](https://github.com/bboe/BBoeOS/compare/de77fc5...c2b7ace)

- Add file descriptor table infrastructure with `sys_open`, `sys_close`, `sys_read`, `sys_write`, `sys_fstat`
- Add `O_CREAT` flag and close writeback for file creation via fd
- Add directory fd support; rewrite `ls` with `open`/`read`/`close`
- Rewrite `cat`, `cp`, and `edit` to use file descriptor syscalls
- Rewrite `asm.asm` to use file descriptor syscalls
- Remove deprecated FS syscalls
- Add block scoping for variables in the C compiler

### [2026-04-10 – 2026-04-11](https://github.com/bboe/BBoeOS/compare/0c55591...de77fc5)

- Rewrite `date` in C with register tracking and lazy spill optimization
- Rewrite `mkdir` in C, beating hand-written assembly by 5 bytes
- Rewrite `cat` in C, beating hand-written assembly by 5 bytes
- Archive retired assembly sources replaced by C programs

### [2026-04-09](https://github.com/bboe/BBoeOS/compare/c35892d...0c55591)

- Add `cc.py` C subset compiler with variables, while loops, arrays, char literals
- Add `sizeof`, `*`, `/`, `%` operators
- Add `argc`/`argv` support
- Write `echo.c` and `uptime.c` as first C programs
- Grow `DIRECTORY_SECTORS` to 3 (48 entries)

### [2026-04-08](https://github.com/bboe/BBoeOS/compare/ed8159f...c35892d)

- Reorganize segment-0 memory layout and isolate the stack
- Add 32-bit file sizes and 16-bit sector numbers to filesystem
- Self-host: assemble `asm.asm` with the OS assembler
- Add `%define` directive and floating buffers on `program_end`
- Convert `test_asm.sh` to `test_asm.py`
- Assemble `edit`: many new instruction forms and parser features

### [2026-04-07](https://github.com/bboe/BBoeOS/compare/34e105d...ed8159f)

- Move binaries into `bin/` subdirectory
- Move static `.asm` reference files into `src/`
- Assemble network programs (`netinit`, `arp`, `netsend`, `netrecv`, `ping`, `dns`)
- Convert `add_file.sh` to `add_file.py`

### [2026-04-05 – 2026-04-06](https://github.com/bboe/BBoeOS/compare/3573832...34e105d)

- Add subdirectory support to the filesystem (one level under root)
- List subdirectory contents; fix `scan_dir_entries` CX clobber
- Cross-directory `cp`, same-directory `mv`, directory guards
- Detect drive geometry for floppy and IDE boot support

### [2026-04-04](https://github.com/bboe/BBoeOS/compare/3704a1a...3573832)

- Add LBA-to-CHS conversion for sectors beyond 63
- Add test script for self-hosted assembler
- Phase 2 of self-hosted assembler: assemble `chmod`, `date`, `uptime`, `cp`, `mv`, `ls`, `draw`

### [2026-04-01 – 2026-04-03](https://github.com/bboe/BBoeOS/compare/0e1aefc...3704a1a)

- Add Phase 1 self-hosted x86 assembler (two-pass, byte-identical to NASM)

### [2026-03-31](https://github.com/bboe/BBoeOS/compare/57193f9...0e1aefc)

- Add text editor with gap buffer, Ctrl+S save, Ctrl+Q quit
- Add `SYS_FS_CREATE` syscall for file creation; support new files in editor
- Show save messages in editor status bar
- Increase filename limit from 10 to 26 characters

### [2026-03-30](https://github.com/bboe/BBoeOS/compare/102c83a...57193f9)

- DNS lookup for arbitrary hostnames with CNAME chain and all A records
- Allow hostnames in `ping` command
- Add executable file flag, `chmod`, `mv`, and `cp` commands
- Protect shell from being modified; prevent duplicate filenames
- Support arrow keys via serial console

### [2026-03-29](https://github.com/bboe/BBoeOS/compare/5f36e11...102c83a)

- Add NE2000 NIC driver: probe, init, ring buffer, MAC programming
- Raw Ethernet frame transmission and polled packet reception
- ARP protocol for IP-to-MAC resolution
- ICMP echo (ping) with IPv4 header and checksum
- UDP send/receive with DNS lookup

### [2026-03-28](https://github.com/bboe/BBoeOS/compare/a0a0980...5f36e11)

- Automatic `\n` to `\r\n` conversion — strings no longer need `\r\n`

## [0.4.0](https://github.com/bboe/BBoeOS/compare/0.3.0...0.4.0) (2026-03-28)

### [2026-03-28](https://github.com/bboe/BBoeOS/compare/6ca690e...a0a0980)

- General cleanup across the project

## [0.3.0](https://github.com/bboe/BBoeOS/compare/0.2.0...0.3.0) (2026-03-27)

### [2026-03-27](https://github.com/bboe/BBoeOS/compare/f2af0a6...6ca690e)

- Major revival of the project after 8 years
- Full command-line editor: left/right arrows, delete, Ctrl+A/E/K/F/B/Y, kill buffer
- Cap input length to 256 characters
- Add `shutdown`, `reboot`, `date`, and `uptime` commands
- Use command dispatch table for shell commands
- Display date and time at boot
- Add special character handling
- Serial console support: mirror output to COM1, poll input from both keyboard and serial
- Add trivial read-only filesystem on the floppy with `cat` and `ls` commands
- Add syscall interface (`INT 30h`)
- Load shell as a program from filesystem
- Extract programs (`draw`, `date`, `uptime`, `cat`, `ls`) from kernel into standalone executables

## [0.2.0](https://github.com/bboe/BBoeOS/compare/0.1.0...0.2.0) (2018-08-12)

### [2018-08-12](https://github.com/bboe/BBoeOS/compare/4ec1217...f2af0a6)

- Two-stage bootloader: load second stage from disk
- Proper backspace handling at the command prompt
- Fix bug where short `g` matched `graphics`

## [0.1.0](https://github.com/bboe/BBoeOS/compare/0.0.3dev...0.1.0) (2018-07-27)

### [2018-07-29](https://github.com/bboe/BBoeOS/compare/95a9a1a...4ec1217)

- Move input string buffer to beginning of usable address space

### [2018-07-27 – 2018-07-28](https://github.com/bboe/BBoeOS/compare/1e2a995...95a9a1a)

- Add `help`, `clear`, `color`, and `time` commands
- Color output mode with multiple color commands
- Extract code into functions and protect most registers
- Update version string to 0.1.0

## [0.0.3dev](https://github.com/bboe/BBoeOS/compare/0.0.2dev...0.0.3dev) (2018-07-26)

### [2018-07-26](https://github.com/bboe/BBoeOS/compare/21f5d53...1e2a995)

- Add simple user-input loop
- Auto-advance cursor row
- Advance cursor on carriage return
- Clear screen on escape
- Echo typed commands
- Detect whether something was entered

## [0.0.2dev](https://github.com/bboe/BBoeOS/compare/99f9894...0.0.2dev) (2018-07-26)

### [2018-07-26](https://github.com/bboe/BBoeOS/compare/99f9894...21f5d53)

- Add one more line of output
- Improve formatting and assembly readability
- Save bytes through origin specification and row-increment optimization

## [0.0.1dev](https://github.com/bboe/BBoeOS/commit/8180e0f) (2012-08-22)

### [2012-08-22](https://github.com/bboe/BBoeOS/commit/8180e0f)

- Initial BBoeOS code: minimal bootloader with welcome message
