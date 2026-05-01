# BBoeOS

A minimal x86 operating system with a real-mode bootloader (`boot.bin`) and a paged high-half kernel (`kernel.bin`) concatenated on disk, plus a shell, VFS (bbfs + ext2), networking stack, self-hosted assembler, and C compiler.  Boots in 16-bit real mode, flips into flat 32-bit protected mode with paging, runs the kernel at ring 0 and userland programs at ring 3 in per-program page directories (privileged instructions trap to `exc_common` with `EXC0D`).

## Build and Run

```sh
./make_os.sh                                           # assemble and create floppy image
qemu-system-i386 -drive file=drive.img,format=raw     # run in QEMU
```

Optional flags: `-serial stdio` for serial console, `if=floppy` on the drive
for floppy mode, and `-netdev user,id=net0 -device
ne2k_isa,netdev=net0,irq=3,iobase=0x300` for the NE2000 NIC.

Requires `nasm` (`brew install nasm`).

## Architecture

The kernel is split across two flat binaries (`nasm -f bin`) concatenated on disk:

- **`boot.bin`** (`org 0x7C00`, `src/arch/x86/boot/boot.asm`): MBR + post-MBR real-mode bootstrap + early-PE bootstrap.  Loaded by BIOS at `0x7C00`.  The MBR does DS/ES/SS:SP setup, disk reset, and an INT 13h read that pulls the post-MBR portion of `boot.bin` into `0x7E00`.  The post-MBR real-mode code issues a second INT 13h read to load `kernel.bin` directly into physical `0x20000` (its final home — no later relocation copy), walks the BIOS memory map via INT 15h `AX=E820` (entries stashed at `0x500` for the bitmap allocator), copies the BIOS ROM 8x16 font, remaps the PIC, enables A20, loads the 32-bit GDT, flips CR0.PE, and far-jumps into `early_pe_entry`.  `early_pe_entry` (32-bit, low physical) builds the boot PD + first kernel PT (identity-mapped at PDE[0] and direct-mapped at PDE[FIRST_KERNEL_PDE = 1022]), enables paging (CR0.PG | CR0.WP), and far-jumps to `high_entry` at virt `0xFF820000`.  No IDT in `boot.asm` — an exception during early-PE triple-faults; the bootstrap is short and tested.  On disk error the MBR prints `!` via INT 10h AH=0Eh and halts; an INT 13h failure on the kernel.bin read prints `K`.
- **`kernel.bin`** (`org 0xFF820000`, `src/arch/x86/kernel.asm`): post-paging high-half kernel.  The `org` equals `DIRECT_MAP_BASE + KERNEL_LOAD_PHYS`, so the kernel runs at its direct-map alias and PDE[FIRST_KERNEL_PDE = 1022]'s 4 MB direct map is the only mapping it needs.  The very first byte is `high_entry`, which lgdts the kernel GDT, lidts the kernel IDT (`idt_init` patches the high-half handler offsets at boot — see `src/arch/x86/idt.asm` for why the IDT_ENTRY macro can't fold them at assemble time), drops the boot identity mapping at PDE[0], initializes the bitmap frame allocator from E820, allocates the kernel direct-map PTs (no-op at FIRST_KERNEL_PDE = 1022 — the auto-grow loop's bound `FIRST_KERNEL_PDE + 1` already equals `LAST_KERNEL_PDE = 1023`), brings up the kmap window via `kmap_init`, and falls through into `protected_mode_entry`.  Locating the kernel in conventional RAM (above the vDSO target at phys `0x10000`, below the VGA aperture at phys `0xA0000`) keeps the entire kernel-side reserved region under 1 MB so the OS boots under QEMU `-m 1`.

- **Post-flip entry** (`protected_mode_entry` in `src/arch/x86/entry.asm`): TSS base patch + `SS0`/`ESP0`/IOPB-offset init + `ltr`, PIT @ 100 Hz, 32-bit IRQ 0 / IRQ 6 handlers via `idt_set_gate32`, driver inits (`ata_init`, `fd_init`, `fdc_init`, `ps2_init`, `vfs_init`, `network_initialize`), unmask IRQ 0/6, `sti`, welcome banner, then falls into `shell_reload`.  Segment reload, ESP, GDT, and IDT are already in place from `high_entry`.  Any post-flip CPU exception lands in `idt.asm`'s `exc_common` and prints `EXCnn EIP=h CR2=h ERR=h` on COM1.
- **Ring-3 userland**: GDT has user code (0x18, DPL=3) and user data (0x20, DPL=3) descriptors plus a TSS at 0x28 whose `SS0:ESP0` points at the kernel stack.  The INT 30h gate is DPL=3 so ring-3 programs can call it; CPU exceptions and IRQs stay DPL=0 (hardware bypasses the gate-DPL check) so user code can't synthesise fake fault frames.  `program_enter` reloads DS/ES/FS/GS to `USER_DATA_SELECTOR` (0x23) and `iretd`s into ring 3 at `PROGRAM_BASE` (0x08048000) with `ESP=USER_STACK_TOP` (0xFF800000, sitting exactly at the user/kernel boundary = `KERNEL_VIRT_BASE`) and `EFLAGS=0x202` (IF=1, IOPL=0).  Privileged instructions (`cli`/`sti`/`in`/`out`/CR writes) `#GP` from userland.
- **Shell respawn** (`shell_reload` → `program_enter`): `vfs_find` + `vfs_load` for `bin/shell`, then `program_enter` resets the fd table, zeroes the program's BSS region per the trailer-magic protocol (`dw bss_size; dw 0xB055`), snapshots the ring-0 ESP into `[shell_esp]`, and `iretd`s the program at ring 3.  `sys_exit` from any program restores `[shell_esp]` (the CPU has already auto-switched to TSS.ESP0 on the ring-3 → 0 transition) and re-enters `shell_reload`.
- **Shell** (`src/c/shell.c`): Loaded from filesystem at `PROGRAM_BASE` (`0x08048000`, the Linux ELF-shaped user-virt load address).  Provides CLI loop, command dispatch, and built-in commands using INT 30h syscalls.
- **Input buffer** at linear address `0x500`, max 256 characters.
- **Disk buffer** (`sector_buffer`, 512 B) is the offset-0 slice of the FS scratch frame that `vfs_init` allocates from the bitmap on every boot.  `bbfs.asm` and `ext2.asm` load the kernel-virt pointer indirectly: `mov ebx, [sector_buffer]`.  `ext2_sd_buffer` (the 1 KB sliding directory window used only by `ext2_search_blk`) is the offset-512 slice of the same frame on ext2 mounts; on bbfs the pointer stays 0 since no caller reaches the ext2-only paths.
- **FD table** is allocated as kernel BSS (`struct fd fd_table[FD_MAX]` in `src/fs/fd.c`), so it lives inside `kernel.bin` like any other kernel global; no fixed-phys reservation needed.
- **Boot-time stash** is embedded inside `kernel.bin` at offset `BOOT_STASH_OFFSET` (= 2): `boot_disk` (1 byte) and `directory_sector` (2 bytes).  The kernel binary's first instruction is `jmp short high_entry`, which skips past these bytes; `boot.asm` writes them through `ES:BOOT_STASH_OFFSET` *after* the kernel.bin INT 13h read so the load doesn't clobber them.  Embedding inside `kernel.bin` lets the bitmap allocator hand out the IVT/BDA region (phys `0x000-0x4FF`), the `0x600-0x7BFF` gap, the MBR landing zone (`0x7C00-0x7DFF`), and the dead post-MBR boot bytes.
- **Kernel stack** at phys `KERNEL_RESERVED_BASE..KERNEL_RESERVED_BASE+0x1000` (4 KB; currently ~`0x28000..0x29000`, shifts with `kernel.bin` size).  `KERNEL_RESERVED_BASE = page_align(0x20000 + sizeof(kernel.bin))` is computed by `make_os.sh` and passed via `-DKERNEL_RESERVED_BASE=N` to the second `kernel.asm` pass and to `boot.asm`.  Lives outside `kernel.bin` to avoid 4 KB of zero padding on disk; reachable immediately after paging because PDE[FIRST_KERNEL_PDE]'s direct map covers phys `0..0x3FFFFF`; reserved via `frame_reserve_range` at boot.  Sized at ~10× the measured peak (~412 B across bbfs / ext2 / fault kill / network paths).  `kernel_stack` / `kernel_stack_top` are `equ`s in `kernel.asm`.  `high_entry` poison-fills the region with `0xDEADBEEF` at boot so a future stack-depth probe can find the high-water mark by scanning upward.
- **Resident kernel** (`kernel.bin`) is loaded at physical `0x20000` and runs at virtual `0xFF820000`.  The kernel direct map at `0xFF800000..0xFFBFFFFF` (PDE 1022, 4 MB) mirrors low physical RAM 1:1; the auto-grow PT loop in `high_entry` is a no-op at the current `FIRST_KERNEL_PDE = 1022` (a single PT covers the entire direct-map region).  The resident kernel image plus reserved cluster is ~170 KB worst case, so 4 MB of direct map has 25× headroom; everything past 4 MB phys reaches the kernel through the kmap window.
- **Kmap window:** PDE 1023 (virt `0xFFC00000..0xFFFFFFFF`) is reserved for a kernel-only window of demand-mapped slots. `kmap_init` (`src/memory_management/kmap.asm`, called by `high_entry` after the kernel idle PD takes over) allocates one frame as the window PT and installs it at `kernel_idle_pd[1023]`. Every per-program PD inherits PDE 1023 verbatim through `address_space_create`'s kernel-half copy-image. `kmap_map(eax = phys) → eax = kernel_virt` fast-paths to `phys + DIRECT_MAP_BASE` when the frame is below the direct-map ceiling; for higher frames it claims one of `KMAP_SLOT_COUNT` (= 4) slots in the window, writes a PTE, and `invlpg`s the slot. `kmap_unmap` releases the slot (no-op for the direct-map fast path). 4 slots is sized for the deepest concurrent nesting in the tree (`address_space_destroy` walks a PD slot and a PT slot at once); slot exhaustion panics. Every "phys → kernel-virt to read/write" path in the kernel goes through `kmap_map`/`kmap_unmap`, so the bitmap allocator can hand out frames anywhere in `[0, FRAME_PHYSICAL_LIMIT)` (~4 GB) and the kernel still reaches them.
- **Per-program address spaces:** each program runs in its own page directory built by `address_space_create` from `program_enter`.  The PD's kernel half (PDEs `FIRST_KERNEL_PDE..1023` = 1022..1023) is copy-imaged from `kernel_idle_pd` (a 4 KB kernel-only PD built once at boot — see below) so the kernel direct map and kmap window are reachable from every address space.  The user half (PDEs 0..1021) is populated only with the program's own pages plus a shared vDSO PTE marked with the `ADDRESS_SPACE_PTE_SHARED` AVL bit (so `address_space_destroy` skips `frame_free` on it).  Program binaries are streamed directly from disk into the freshly-allocated user frames (via `vfs_read_sec` + `sector_buffer` + a private `program_fd` slot in entry.asm BSS) — there is no kernel-side staging buffer.  See the user-side virtual layout table below for the per-PD shape.
- **Kernel idle PD:** a 4 KB kernel-only page directory allocated by `high_entry` after the kernel-PT-alloc loop runs.  Built by copy-imaging the boot PD's kernel half (PDEs `FIRST_KERNEL_PDE..1023`) into a frame_alloc'd frame and leaving PDEs 0..`FIRST_KERNEL_PDE - 1` zero.  Triple-roled: (1) canonical kernel-half PDE source for `address_space_create`, (2) CR3 between programs (e.g. `shell_reload` runs on it), (3) CR3-swap target in `sys_exit` / kill-path teardown (which cannot run on the dying user PD it is about to `frame_free`).  Lives wherever the bitmap allocator returned a frame, so it isn't pinned in the kernel-side reserved cluster — `kernel_idle_pd_phys` (entry.asm BSS) holds its phys.  Once the idle PD takes over, the boot PD's 4 KB frame is freed back to the bitmap pool: that 4 KB cluster slot becomes a regular conventional frame the allocator can hand out for user pages.
- Kernel sector count and reserved-region base are both derived at build time: `make_os.sh` measures `kernel.bin`, passes the sector count to `boot.asm` as `-DKERNEL_SECTORS=N`, computes `KERNEL_RESERVED_BASE = page_align(0x20000 + sizeof(kernel.bin))`, then re-assembles `kernel.asm` and `boot.asm` with `-DKERNEL_RESERVED_BASE=N`.  A size-invariant check between the two `kernel.asm` passes confirms the change cannot shift the binary.  A separate VGA-hole assert verifies that `KERNEL_RESERVED_BASE + reserved-region-size < 0xA0000` so the kernel-side fixed-phys regions never cross the VGA aperture (which is what lets the OS boot under QEMU `-m 1`).  The boot-time `kernel_bytes` word at MBR offset 508 holds `(BOOT_SECTORS + KERNEL_SECTORS) * 512` so `add_file.py`'s host-side `compute_directory_sector` arithmetic still works.

### Static memory map

Kernel-side fixed-physical regions, all reached through the kernel direct map at virt `DIRECT_MAP_BASE + phys` (= `0xFF800000 + phys` at the current base; or via the kmap window for frames above the direct-map ceiling).  The "in kernel.bin?" column flags whether the bytes occupy the on-disk image (`yes`) or live as bare frames reserved by `frame_reserve_range` (`no`).  Addresses from `kernel_stack` onward are derived from `KERNEL_RESERVED_BASE = page_align(0x20000 + sizeof(kernel.bin))`; example values shown are for the current build (~29 KB kernel).  Two narrow `frame_reserve_range` calls at boot pin only the regions the kernel still owns: the vDSO target frame at `0x10000` (one 4 KB page) and `0x20000..(FRAME_BITMAP_PHYS + frame_bitmap_bytes)` (kernel image + KERNEL_RESERVED_BASE region; the bitmap end is runtime, sized by `frame_init` from E820).  Everything else in conventional low memory — IVT/BDA at `0..0x4FF`, `0x600..0x7BFF` gap, MBR landing zone at `0x7C00..0x7DFF`, dead post-MBR boot code at `0x7E00..0xDFFF`, the unused page-0xE region, and the boot stack at `0x9F000` — stays in the bitmap allocator's free pool.  The build script asserts that `KERNEL_RESERVED_BASE + 0x23000 < 0xA0000` (worst-case stack + boot PD + first kernel PT + 128 KB bitmap at the FRAME_PHYSICAL_LIMIT cap) so the kernel-side regions never cross the VGA aperture under any RAM size.  Update this table when adding a new fixed-phys region so newcomers can find every slot in one place.

| Phys range | Kernel-virt | Size | Symbol / purpose | In kernel.bin? |
|---|---|---|---|---|
| `0x00010000..0x00010FFF` | n/a | 4 KB | vDSO (shared user-virt frame; per-program PDs alias it user-side) | no |
| `0x00020000..0x00020001` | `0xFF820000..0xFF820001` | 2 B | `jmp short high_entry` trampoline (offset 0 of kernel.bin) | yes |
| `0x00020002` | `0xFF820002` | 1 B | `boot_disk` (BIOS drive number, written by boot.asm post-load) | yes |
| `0x00020003..0x00020004` | `0xFF820003..0xFF820004` | 2 B | `directory_sector` (LBA of first directory sector) | yes |
| `0x00020008..` | `0xFF820008..` | ~29 KB | `kernel.bin` `high_entry` and resident kernel code | yes |
| `KERNEL_RESERVED_BASE` (~`0x28000..0x28FFF`) | `0xFF828000..` | 4 KB | `kernel_stack` (`KERNEL_RESERVED_BASE = page_align(0x20000 + kernel_size)`; poison-filled with `0xDEADBEEF` at boot for high-water tracking) | no |
| ~`0x29000..0x29FFF` | `0xFF829000..` | 4 KB | boot PD (`BOOT_PD_PHYS`); freed back to the bitmap pool by `high_entry` after `kernel_idle_pd` takes over the CR3-target role.  The slot is then just a regular conventional frame — the bitmap allocator can hand it out for user pages. | no |
| ~`0x2A000..0x2AFFF` | `0xFF82A000..` | 4 KB | first kernel PT (`FIRST_KERNEL_PT_PHYS`) | no |
| ~`0x2B000..` | `0xFF82B000..` | runtime, ≤ 128 KB | `frame_bitmap` (size set by `frame_init` from the highest type=1 E820 base, clamped to FRAME_PHYSICAL_LIMIT ≈ 4 GB — `-m 1` pays ~20 bytes, `-m 1024` pays 32 KB, `-m 4096` pays 128 KB; `frame_init` fills the storage before any allocator call, so the bytes don't ride on disk inside `kernel.bin`) | no |
| `FRAME_BITMAP_PHYS + frame_bitmap_bytes` | `0xFF800000 +` same | -- | end of the kernel reserve sweep — runtime ceiling, equals `0x2B000 + 20 B` on `-m 1`, `0x2B000 + 32 KB` on `-m 1024`, and `0x2B000 + 128 KB` on `-m 4096`; everything past this in conventional RAM is owned by the bitmap allocator (subject to E820's reserved regions, including the VGA aperture at `0xA0000..0xFFFFF`) | -- |
| dynamic | dynamic | 4 KB | FS scratch frame — allocated by `vfs_init` on every boot (FS is always used); sliced into two named pointers (`sector_buffer` at offset 0, `ext2_sd_buffer` at offset 512 when ext2 is detected). 1.5 KB used inside the 4 KB frame on ext2; 512 B used on bbfs.  bbfs systems leave `ext2_sd_buffer = 0` (no caller reaches the ext2_search_blk paths that read it). | no |
| dynamic | dynamic | 4 KB | NIC scratch frame — allocated by `network_initialize` only when an NE2000 NIC is detected; sliced into four named pointers (`net_receive_buffer` at offset 0, `net_transmit_buffer` at 1536, `arp_table` at 3072, `udp_buffer` at 3168), 3.4 KB used inside the 4 KB frame.  Sessions without a NIC leave the four pointers at 0 and never spend the frame.  The ARP-table slice is zero-filled at init (lookup/add keys on `[entry] == 0` for empty slots); the other slices are fully overwritten on each use | no |
| dynamic | dynamic | 4 KB | `kernel_idle_pd` — a kernel-only PD allocated by `high_entry` post-PT-alloc.  Kernel-half (PDEs FIRST_KERNEL_PDE..1023) copy-imaged from the boot PD; user-half (PDEs 0..FIRST_KERNEL_PDE-1) zero.  Used as the canonical kernel-half PDE source for `address_space_create`, as CR3 between programs, and as the CR3-swap target during `address_space_destroy`.  `kernel_idle_pd_phys` (entry.asm BSS) holds its phys.  Replaces the boot PD's permanent-frame role; the boot PD's frame is freed back to the bitmap pool once the idle PD takes over | no |
| dynamic | dynamic | 4 KB | kmap window PT (`kmap_pt_phys`) — allocated by `kmap_init` after the idle PD takes over.  Installed at `kernel_idle_pd[1023]` so every per-program PD inherits the window through `address_space_create`'s kernel-half copy-image.  Holds the PTEs for the `KMAP_SLOT_COUNT = 4` slots at virt `0xFFC00000..0xFFC03FFF`; `kmap_map`/`kmap_unmap` write and clear them on demand to alias frames above `FRAME_DIRECT_MAP_LIMIT` | no |

User-side virtual layout (per per-program PD; same shape for every program PD that `address_space_create` builds):

| User-virt range | Size | Purpose |
|---|---|---|
| `0x00000000..0x00000FFF` | 4 KB | NULL guard — not mapped (PTE[0] absent so `*(int *)0` raises #PF) |
| `0x00001000..0x00001FFF` | 4 KB | shell↔program handoff frame at `USER_DATA_BASE` (ARGV at +0x4DE, EXEC_ARG at +0x4FC, BUFFER at +0x500) |
| `0x00010000..0x00010FFF` | 4 KB | vDSO (`FUNCTION_PRINT_STRING`, `FUNCTION_DIE`, …) |
| `0x08048000..` | program-sized | program text + BSS (Linux ELF-shaped load address) |
| `0xFF7E0000..0xFF7EFFFF` | 64 KB | unmapped (stack guard region) |
| `0xFF7F0000..0xFF7FFFFF` | 64 KB | user stack (16 pages, top at `USER_STACK_TOP`) |
| `0xFF800000` | -- | `USER_STACK_TOP` (one past end of stack; equals user/kernel boundary = `KERNEL_VIRT_BASE`) |
| `0xFF800000..0xFFBFFFFF` | 4 MB | kernel direct map (PDE 1022 = FIRST_KERNEL_PDE, copy-imaged from `kernel_idle_pd`) |
| `0xFFC00000..0xFFFFFFFF` | 4 MB | kmap window (PDE 1023, copy-imaged from `kernel_idle_pd`); only the first `KMAP_SLOT_COUNT = 4` PTEs are ever used at runtime |

### Filesystem

Trivial read-only filesystem on the floppy disk:

- **Sector 0**: MBR (stage 1)
- **Sectors 1 to DIRECTORY_SECTOR-1**: Stage 2
- **Sectors DIRECTORY_SECTOR to DIRECTORY_SECTOR+2**: File table / root directory (`DIRECTORY_SECTORS` = 3 sectors, 48 entries x 32 bytes)
- **Sectors DIRECTORY_SECTOR+2 onward**: File data

Directory entry format (32 bytes): 27 bytes filename (null-terminated, max 26 chars), 1 byte flags (`FLAG_EXECUTE = 0x01`, `FLAG_DIRECTORY = 0x02`), 2 bytes start sector, 2 bytes file size. Files span consecutive sectors starting from the start sector.

Subdirectories: one level under root only. A subdirectory occupies `DIRECTORY_SECTORS` (= 3) consecutive sectors and holds 48 entries, matching the root layout. File paths in syscalls and programs may contain a single `/` to reference a file inside a subdirectory (e.g., `dir/file`). Executables live in `bin/`; the shell automatically retries `bin/<name>` when an external command is not found in the root directory. Static reference sources (the `.asm` files used by the self-hosted assembler tests) live in `src/`. The assembler resolves `%include` paths relative to the directory of its source argument, so `asm src/cat.asm out` correctly finds `src/constants.asm`.

Use `./add_file.py <file>` to add files to the image. Use `./add_file.py -d <dir> <file>` to add a file inside a subdirectory, and `./add_file.py --mkdir <dirname>` to create a subdirectory.

### Networking

NE2000 ISA NIC driver at I/O base `0x300`. Requires QEMU `-netdev user,id=net0 -device ne2k_isa,netdev=net0,irq=3,iobase=0x300`. Polled mode (no interrupts). Networking buffers (`net_receive_buffer` / `net_transmit_buffer`, 1.5 KB used inside a 4 KB frame each) are allocated dynamically by `network_initialize` from the bitmap allocator on a successful NIC probe; sessions booted without a NIC (or without the QEMU `-device ne2k_isa` flag) never spend the two frames.  asm callers load the kernel-virt pointers indirectly: `mov edi, [net_receive_buffer]`.

### Serial Console

All output is mirrored to COM1.  `put_character` in `drivers/console.asm` includes an ANSI escape sequence parser and automatic `\n` to `\r\n` conversion — strings only need `\n`.  Raw bytes go to serial via `serial_character` (in `drivers/serial.asm`); ANSI sequences (e.g., `ESC[nA` cursor up, `ESC[nC` cursor forward, `ESC[nD` cursor back, `ESC[r;cH` cursor position, `ESC[0m` reset colors, `ESC[38;5;Nm` foreground, `ESC[48;5;Nm` background) are translated to native VGA driver calls (`vga_set_cursor`, `vga_teletype`, `vga_set_palette_color`, etc. in `drivers/vga.asm`) for the screen — no INT 10h post-protected-mode-flip.  `put_string` lives in `drivers/console.asm`.  The MBR does no string output; on boot it's BIOS text mode until the post-MBR path initialises the console driver.  Input from both PS/2 (`drivers/ps2.asm`, IRQ 1) and COM1 (`drivers/serial.asm`, polled in `fd_read_console`) feeds the same fd-0 console.  Serial terminals send `0x7F` (DEL) for backspace, which is handled alongside `0x08`.

### Syscall Interface (INT 30h)

Programs loaded from the filesystem can use INT 30h for OS services:

| AH    | Name         | Description                                          |
|-------|--------------|------------------------------------------------------|
| 00h   | fs_chmod     | Set file flags, SI = filename, AL = flags, CF on err  |
| 01h   | fs_mkdir     | Create subdirectory, SI = name, AX = start sector, CF on err |
| 02h   | fs_rename    | Rename or move file, SI = old name, DI = new name, CF on err |
| 03h   | fs_rmdir     | Remove an empty directory, SI = name, CF on err        |
| 04h   | fs_unlink    | Delete a file, SI = filename, CF on err               |
| 10h   | io_close     | Close fd, BX = fd, CF on error                        |
| 11h   | io_fstat     | Get file status, BX = fd; AL = mode, CX:DX = size     |
| 12h   | io_ioctl     | Device control, BX = fd, AL = cmd, args in other regs per (fd_type, cmd); CF on err |
| 13h   | io_open      | Open file, SI = filename, AL = flags, DL = mode; AX = fd, CF on err |
| 14h   | io_read      | Read from fd, BX = fd, DI = buf, CX = count; AX = bytes, CF on err |
| 15h   | io_write     | Write to fd, BX = fd, SI = buf, CX = count; AX = bytes, CF on err |
| 20h   | net_mac      | Read cached MAC, DI = 6-byte buffer, CF if no NIC      |
| 21h   | net_open     | Open socket, AL = type (SOCK_RAW=0, SOCK_DGRAM=1), DL = protocol (IPPROTO_UDP=17, IPPROTO_ICMP=1; 0 for raw); AX = fd, CF if no NIC or table full |
| 22h   | net_recvfrom | Recv datagram via fd (UDP or ICMP): BX=fd, DI=buf, CX=len, DX=port (UDP) or ignored (ICMP); AX=bytes (0=none), CF err |
| 23h   | net_sendto   | Send datagram via fd: BX=fd, SI=buf, CX=len, DI=IP; UDP also uses DX=src port, BP=dst port (ignored for ICMP); AX=bytes, CF err |
| 30h   | rtc_datetime | Get wall-clock time, DX:AX = unsigned seconds since 1970-01-01 UTC |
| 31h   | rtc_millis   | Get milliseconds since boot, DX:AX = ms                 |
| 32h   | rtc_sleep    | Busy-wait for CX milliseconds                           |
| 33h   | rtc_uptime   | Get uptime in seconds, AX = elapsed seconds             |
| F0h   | sys_exec     | Execute program, SI = filename, CF on error            |
| F1h   | sys_exit     | Reload and return to shell                             |
| F2h   | sys_reboot   | Reboot                                                |
| F3h   | sys_shutdown  | Shutdown                                              |

When removing a syscall, collapse the remaining numbers in its group in
the same commit (e.g. removing `SYS_NET_ARP` (20h) shifts every later
`SYS_NET_*` down by one). The group-high-nibble (2h = net, 3h = rtc, …)
is the only stable contract with userspace; within a group, expect
numbers to compact.  Programs reference `SYS_<NAME>` symbolically so
renumbering is source-compatible — just rebuild.

## File Structure

- `add_file.py` — Host-side script to add files to the drive image filesystem
- `cc.py` — Host-side C subset compiler (translates `src/c/*.c` to NASM-compatible assembly)
- `make_os.sh` — Build script (assembles kernel, compiles C programs via `cc.py`, creates floppy image)
- `src/include/constants.asm` — Shared constants (`BUFFER`, `DIRECTORY_SECTOR`, `SECTOR_BUFFER`, `EXEC_ARG`, `NE2K_BASE`, `PROGRAM_BASE`, `SYS_*` syscall numbers, etc.)
- `src/include/dns_query.asm`, `encode_domain.asm`, `parse_ip.asm` — Shared DNS/IP helpers; see source headers for calling conventions.
- `src/arch/x86/boot/boot.asm` — Pre-paging boot binary (`org 0x7C00`): MBR + post-MBR real-mode bootstrap (second INT 13h read of kernel.bin directly into phys `0x20000`, E820 probe, PIC remap, A20, GDT load, CR0.PE flip) + 32-bit `early_pe_entry` (build boot PD + first kernel PT, enable paging, far-jump to `high_entry` at virt `0xC0020000`).  No real-mode-to-PE relocation copy — `KERNEL_LOAD_PHYS = 0x20000` is the kernel's final home.  Post-MBR region pads to the next 512-byte boundary; `BOOT_SECTORS` is derived (`(boot_end - post_mbr_continue) / 512`) so the count auto-grows when the boot code crosses a sector.  `make_os.sh` only has to measure `kernel.bin`'s sector count for the `-DKERNEL_SECTORS=N` second-pass nasm invocation.
- `src/arch/x86/kernel.asm` — Post-paging high-half kernel (`org 0xC0020000` = `DIRECT_MAP_BASE + KERNEL_LOAD_PHYS`): `high_entry` (segment / GDT / IDT / stack setup, identity-drop, bitmap init, kernel-PT allocation, kernel_idle_pd build, boot-PD freeback) followed by `%include`s of every kernel subsystem (drivers, fs, helpers, net stack, syscall dispatcher, IDT, post-flip entry, frame allocator, address-space helpers) and the kernel GDT + vDSO blob (`incbin "vdso.bin"`).
- `src/arch/x86/idt.asm` — 32-bit IDT with CPU exception stubs and INT 30h gate; `idt_init` (called from `high_entry`) patches the high-half handler offsets at boot since the IDT_ENTRY macro can only emit the low 16 bits in `nasm -f bin` mode (section-relative labels reject `& 0FFFFh` / `>> 16` arithmetic).  Any post-flip exception lands in `exc_common` and prints `EXCnn EIP=h CR2=h ERR=h` on COM1
- `src/arch/x86/entry.asm` — `protected_mode_entry` (TSS patch, PIT + IRQ handler install, driver / VFS / NIC inits, banner) flowing into `shell_reload` (loads `bin/shell` and jumps), `program_enter` (fd reset, BSS zero via the trailer-magic protocol, ESP snapshot, `iretd` to ring 3 at `PROGRAM_BASE`), and the IRQ 0 / IRQ 6 handlers.  Segment / GDT / IDT / ESP setup happens in `kernel.asm`'s `high_entry` before falling into `protected_mode_entry`; the ring-0 stack lives at virt `0xC0000000 + KERNEL_RESERVED_BASE` (~`0xC0028000` for the current kernel size — see Kernel stack note above), not in entry.asm.
- `src/memory_management/frame.asm` — Bitmap physical-frame allocator: `frame_alloc` / `frame_free` / `frame_init` (two-pass E820 walker — first pass finds the highest type=1 frame base, between passes sizes the bitmap, second pass marks free regions) / `frame_reserve_range`.  Bitmap is sized at boot from E820, clamped to FRAME_PHYSICAL_LIMIT (~4 GB, the 32-bit phys ceiling), and lives in the post-kernel cluster at FRAME_BITMAP_PHYS.  Frames above FRAME_DIRECT_MAP_LIMIT (the kernel direct-map ceiling) reach the kernel via the kmap window — see `kmap.asm`.
- `src/memory_management/access.asm` — `access_ok` user-buffer pointer-validation helper (rejects ranges that span the user/kernel boundary or wrap), invoked by syscall handlers before touching userspace memory.
- `src/memory_management/address_space.asm` — Per-program PD lifecycle: `address_space_create` (allocate PD, copy-image kernel half from `kernel_idle_pd`, build user PTs for handoff frame + vDSO + program text/BSS + stack), `address_space_destroy` (free user frames, skipping shared-AVL PTEs, then free PTs and PD), and the page-mapping primitives (`address_space_map_page`, etc.) those two drive.  All PD/PT reads and writes go through `kmap_map`/`kmap_unmap` so a high-physical PD or PT frame stays addressable.
- `src/memory_management/kmap.asm` — kernel temporary-mapping window at PDE 1023 (virt `0xFFC00000..0xFFFFFFFF`).  `kmap_init` (called from `high_entry` after the idle PD takes over) allocates one frame as the window PT and installs it at `kernel_idle_pd[1023]`; per-program PDs inherit it via the kernel-half copy-image in `address_space_create`.  `kmap_map(eax = phys) → eax = kernel_virt` fast-paths to the direct-map alias when phys is below FRAME_DIRECT_MAP_LIMIT and falls back to a slot in the window for higher frames.  `kmap_unmap` releases the slot.  `KMAP_SLOT_COUNT = 4` covers the deepest concurrent nesting in the tree (PD + PT walks in `address_space_destroy`); slot exhaustion panics.
- `src/arch/x86/syscall.asm` — INT 30h dispatcher: per-handler bodies inlined directly (fs_*, io_*, rtc_*, sys_*) and tail-jump shims into `src/syscall/syscalls.c` for the four net_* handlers.  The `.iret_cf` path sign-extends AX into EAX before iret; `.iret_cf_eax` is the explicit-32-bit variant used by `io_read` / `io_write`, whose byte counts can exceed 32 767.
- `src/syscall/syscalls.c` — C bodies for the four non-trivial network handlers: `sys_net_mac`, `sys_net_open`, `sys_net_recvfrom`, `sys_net_sendto`.  Reached via `call sys_net_X; jmp .iret_cf` shims in `syscall.asm`.
- `src/arch/x86/system.c` — `reboot` (8042 reset), `shutdown` (APM / QEMU / Bochs shutdown ports).
- `src/drivers/console.c` — Unified output: `put_character` (ANSI parser + screen + serial mirror with auto `\n` → `\r\n`) and `put_string`.  Delegates raw bytes to `serial_character` (`drivers/serial.c`) and ANSI cursor / palette commands to the VGA helpers in `drivers/vga.c`.
- `src/drivers/serial.c` — COM1 driver: `serial_character` (output) and `serial_check` / `serial_read` (input, polled by `fd_read_console`).
- `src/drivers/ata.c`, `src/drivers/fdc.c` — Hardware disk drivers (ATA PIO and floppy DMA); called via `fs/block.asm`'s `read_sector` / `write_sector` dispatch (AX = 0-based sector number).
- `src/drivers/ne2k.c` — NE2000 ISA NIC driver (polled-mode Ethernet); I/O base `0x300`, IRQ 3.
- `src/drivers/ps2.c` — PS/2 keyboard driver: `ps2_init`, `ps2_check`, `ps2_read`.
- `src/drivers/rtc.c` — RTC / PIT timer: tick counter, `rtc_sleep_ms` busy-wait, CMOS date read.
- `src/drivers/vga.c` — VGA driver: text and mode-13h helpers (`vga_set_mode`, `vga_clear_screen`, `vga_fill_block`, `vga_set_palette_color`, …) plus `fd_ioctl_vga` (the `/dev/vga` ioctl dispatcher for `VGA_IOCTL_MODE` / `VGA_IOCTL_FILL_BLOCK` / `VGA_IOCTL_SET_PALETTE`).
- `src/fs/fd.c` — File descriptor table and dispatch: `fd_open` (synthesizes `/dev/vga` into `FD_TYPE_VGA` without touching the filesystem), `fd_read`, `fd_write`, `fd_close`, `fd_fstat`, `fd_ioctl`.  Per-fd-type handlers live under `src/fs/fd/` (`console.c`, `fs.c`, `net.c`).
- `src/fs/block.asm` — Block I/O dispatcher: `read_sector`, `write_sector` (dispatches to fdc/ata based on `boot_disk`).
- `src/fs/bbfs.asm` — BBoeOS filesystem (VFS backend): `bbfs_chmod`, `bbfs_create`, `bbfs_find`, `bbfs_init`, `bbfs_load`, `bbfs_mkdir`, `bbfs_rename`, `bbfs_update_size`, plus internal helpers (`find_file`, `scan_directory_entries`, etc.).
- `src/fs/ext2.asm` — ext2 filesystem (second VFS backend, auto-detected by `vfs_init`).
- `src/fs/vfs.c` — VFS layer: runtime function-pointer table (`vfs_find_fn`, etc.), `vfs_found_*` state struct, thin wrapper functions (`vfs_find`, `vfs_create`, `vfs_rmdir`, …).  Detects bbfs vs ext2 at boot and points the function pointers at the corresponding backend.
- `src/net/net.asm` — Four-line orchestrator that `%include`s the protocol modules: `net/arp.asm`, `net/udp.asm`, plus `build/kernel-c/net/icmp.kasm` and `build/kernel-c/net/ip.kasm` (cc.py-compiled from `src/net/icmp.c` and `src/net/ip.c`).  The NE2000 hardware driver itself lives in `drivers/ne2k.c`.
- `src/c/` user-facing programs written in the C subset: `arp`, `asm`, `cat`, `chmod`, `cp`, `date`, `dns`, `draw`, `echo`, `edit`, `ls`, `mkdir`, `mv`, `ping`, `rm`, `rmdir`, `shell`, `uptime`.
- `tests/programs/` smoke tests written in the same C subset, kept separate from user programs because they exist solely to exercise specific kernel paths from `tests/test_programs.py` that need a real boot to verify: `bigbss` (256 KB BSS allocation across per-program PD pages — also kmap window end-to-end smoke), `bits` (bitwise operators `|` `^` `~` `<<` `>>` `&` and compound-assignment forms), `booltest` (booleanized comparison BinOps used as values), `cftest` / `fctest` (call-flow / function-call edge cases), `gptest` (user-fault `#GP` kill path in `idt.asm`'s `exc_common` — executes `cli` at CPL=3, expects shell respawn), `loop` (basic while-loop control flow with per-character `printf`), `loop_array` (`sizeof(arr)/sizeof(elt)` folding + indexed string-array reads via `write`), `nullderef` (`#PF` on virt 0 with the unmapped PTE[0] guard), `okptest` (user-buffer pointer validation via `access_ok`), `stackbomb` (16-page user-stack overflow into the unmapped guard region), `stacktop` (asserts `USER_STACK_TOP = KERNEL_VIRT_BASE` to catch constant drift).  Pure cc.py codegen tests (file-scope `asm()` escape, `#include` directive, `asm_register` global aliasing, file-scope BSS globals, brace-initialized global arrays) live in `tests/test_kernel_cc.py` as compile-and-inspect-asm pytest cases — no QEMU boot needed.  Built and packaged into the drive image's `bin/` only when `make_os.sh` is run with `--with-test-programs`; the default build keeps them out of the image so a normal boot ships only the user programs.  `tests/test_programs.py` passes the flag automatically.
- `src/c/edit.c` — Full-screen text editor with gap buffer, Ctrl+S save, Ctrl+Q quit.  All editor state is file-scope so cc.py parks it in BSS rather than auto-pinning to registers that `buffer_character_at` clobbers (it uses EDX/ECX as scratch).  The 448 KB gap buffer (`edit_buffer[EDIT_BUFFER_SIZE]`, sized at `0x70000` to fit under the `-m 1` user-pool ceiling) and 2.5 KB kill buffer (`edit_kill_buffer[EDIT_KILL_BUFFER_SIZE]`) are BSS arrays — the per-program PD that `address_space_create` builds gets enough zero-filled user pages to back them via the trailer-magic protocol.  Any source file in the tree fits with room to spare (the largest, `src/c/asm.c`, is ~131 KB).  A single `read(fd, edit_buffer, EDIT_BUFFER_SIZE)` fills the buffer in one call: `SYS_IO_READ` returns the full 32-bit byte count via `EAX` (the dispatcher routes io_read through `.iret_cf_eax`, skipping the AX sign-extend that the rest of the syscall surface uses), so no chunking is needed up to the 448 KB buffer cap.
- `src/c/asm.c` — Self-hosted x86 assembler (two-pass; byte-identical to NASM for everything in `static/`). Phase 1 port: the driver and handlers still live inside a single file-scope `asm("...")` block that wraps `archive/asm.asm`'s original NASM source; follow-up PRs extract pieces into pure C one family at a time. Supported directives and mnemonics are documented in the inline-asm body.

## Key Conventions

- Add new commands and functions in **sorted order** (alphabetical).
- Preserve existing comments when editing code.
- Shell command dispatch is a chain of `else if (streq(buf, "name"))` checks in `src/c/shell.c`. Adding a built-in requires a new branch (and a matching entry in the `help` string).
- The shell splits input at the first space: the command name is null-terminated in `BUFFER`, and `[EXEC_ARG]` points to the argument string (or 0 if none; use `set_exec_arg()`). Unknown commands are tried as external programs via `SYS_SYS_EXEC`; `SYS_SYS_EXIT` reloads the shell.
- Programs are loaded at `PROGRAM_BASE` (`0x08048000`). The shell is the first program loaded at boot. Programs call kernel-provided helpers via the vDSO at user-virt `0x10000` (e.g. `FUNCTION_PRINT_STRING`, `FUNCTION_PRINT_CHARACTER`, `FUNCTION_WRITE_STDOUT`, `FUNCTION_DIE` — see `src/include/constants.asm` for the full table).  Only program-specific logic files (e.g. `dns_query.asm`, `parse_ip.asm`) are still `%include`d.
- Stage 1 functions must fit within the 512-byte MBR.
- When adding the `DIRECTORY_SECTOR` constant, the post-MBR sector count adjusts automatically.
- **Naming conventions**: Constants and string labels use `UPPER_CASE`. Functions and variables use `lower_case`. Local labels use `.dot_prefix`.
- All output goes through `put_character` (in `drivers/console.c`) which handles ANSI escape sequences for both screen and serial. The shell's line editor uses ANSI sequences (e.g., `ESC[nD` for cursor back, `ESC[nA` for cursor up) via `FUNCTION_PRINT_CHARACTER` for all output.

## Bootloader (real mode) constraints

The MBR + `boot/vga_font.asm` (~700 bytes total) are the only real-mode
code in the tree.  Everything past the `jmp dword 0x08:protected_mode_entry`
is flat 32-bit protected mode.  Real-mode constraints that still apply
inside the bootloader:

- Only BX, BP, SI, DI are valid base/index registers in memory operands (not AX, CX, DX, SP).
- BIOS interrupts: INT 10h (video), INT 13h (disk).  Available only before the CR0.PE flip.
- Programs must `cld` before using string instructions (`lodsb`, `rep movsw`, etc.).

## Python conventions

- Every function uses mandatory keyword arguments (keyword-only via `*`) unless the positional args are self-evident (single obvious arg → positional-only via `/`). Arguments sorted alphabetically at definition and call sites.
- Functions sorted alphabetically within their scope (module, class).
- No abbreviations in function or variable names. Examples: `expression` (not `expr`), `generate` (not `gen`), `statement` (not `stmt`), `function` (not `func`), `directory` (not `dir`), `command` (not `cmd`), `message` (not `msg`), `process` (not `proc`), `reference` (not `ref`), `buffer` (not `buf`), `offset` (not `off`), `declaration` (not `decl`), `parameter` (not `param`), `allocate` (not `alloc`), `file_descriptor` (not `fd`), `serial` (not `ser`), `sector` (not `sec`).

## Releases

Update `CHANGELOG.md` with new entries as features land.  Group entries by date under the Unreleased section.  After a batch of significant improvements, bump the version in `src/arch/x86/entry.asm` (the `welcome_msg` string emitted by `protected_mode_entry`) and move the Unreleased entries under a new version header with updated comparison links.

## Testing

Manual testing in QEMU is still the primary workflow — use `-serial stdio` to exercise the serial console and `-machine acpi=off` to test the shutdown failure path.

Automated self-hosting test: `tests/test_asm.py` boots the OS in QEMU and has the self-hosted assembler reassemble each program in `static/`, then diffs the result byte-for-byte against NASM's output. It drives QEMU via a serial fifo and waits for the `$ ` shell prompt (no fixed sleeps), so each program finishes in a second or two.

- `tests/test_asm.py` — run the full suite
- `tests/test_asm.py <name>` — run a single program; on single-program runs the nasm reference, assembled output, and drive image are copied to a persistent temp directory whose path is printed at the end

Filesystem regression tests: `tests/test_bboefs.py` boots the OS, runs shell command sequences, and inspects the resulting drive image to verify fs_copy / fs_mkdir / fs_find / fs_create handle large files (>64 KB), sectors past 255, and entries that live in the second directory sector. `tests/test_bboefs.py <name>` runs a single test.

Program runtime tests: `tests/test_programs.py` boots the OS in QEMU per test, runs a representative shell command for each entry, and checks output against a regex.  `--filesystem bbfs` (default) covers user / kernel / cc.py paths; `--filesystem ext2` adds an `e2fsck -f -n` integrity check after each test, runs the ext2-specific stress tests (doubly-indirect blocks, multi-sector directory walks, rename-across-parents, etc.), and finishes with a 2 KB-block-size matrix re-run of the FS-touching tests.  `--slow` opts in to the large-file and doubly-indirect ext2 tests.
