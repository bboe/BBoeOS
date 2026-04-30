# BBoeOS

A minimal x86 operating system with a single-file bootloader-plus-kernel, shell, filesystem, networking stack, self-hosted assembler, and C compiler.  Boots in 16-bit real mode, flips into flat 32-bit protected mode, runs the kernel at ring 0 and userland programs at ring 3 (privileged instructions trap to `exc_common` with `EXC0D`).

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

- **`boot.bin`** (`org 0x7C00`, `src/arch/x86/boot/boot.asm`): MBR + post-MBR real-mode bootstrap + early-PE bootstrap.  Loaded by BIOS at `0x7C00`.  The MBR does DS/ES/SS:SP setup, disk reset, and an INT 13h read that pulls the post-MBR portion of `boot.bin` into `0x7E00`.  The post-MBR real-mode code issues a second INT 13h read to load `kernel.bin` into physical `0x10000`, walks the BIOS memory map via INT 15h `AX=E820` (entries stashed at `0x500` for the bitmap allocator), copies the BIOS ROM 8x16 font, remaps the PIC, enables A20, loads the 32-bit GDT, flips CR0.PE, and far-jumps into `early_pe_entry`.  `early_pe_entry` (32-bit, low physical) `rep movsd`-copies `kernel.bin` from `0x10000` to physical `0x100000`, builds an initial PD at phys `0x1000` with the first kernel PT at phys `0x2000` identity-mapped at PDE[0] and direct-mapped at PDE[768] (= virt `0xC0000000`), enables paging (CR0.PG | CR0.WP), and far-jumps to `high_entry` at virt `0xC0100000`.  No IDT in `boot.asm` — an exception during early-PE triple-faults; the bootstrap is short and tested.  On disk error the MBR prints `!` via INT 10h AH=0Eh and halts; an INT 13h failure on the kernel.bin read prints `K`.
- **`kernel.bin`** (`org 0xC0100000`, `src/arch/x86/kernel.asm`): post-paging high-half kernel.  The very first byte is `high_entry`, which lgdts the kernel GDT, lidts the kernel IDT (`idt_init` patches the high-half handler offsets at boot — see `src/arch/x86/idt.asm` for why the IDT_ENTRY macro can't fold them at assemble time), drops the boot identity mapping at PDE[0], initializes the bitmap frame allocator from E820, allocates only the kernel direct-map PTs needed to cover installed RAM (PDEs 769..N-1, where N = (frame_max_phys >> 22) + 769, capped at 831), and falls through into `protected_mode_entry`.

- **Post-flip entry** (`protected_mode_entry` in `src/arch/x86/entry.asm`): TSS base patch + `SS0`/`ESP0`/IOPB-offset init + `ltr`, PIT @ 100 Hz, 32-bit IRQ 0 / IRQ 6 handlers via `idt_set_gate32`, driver inits (`ata_init`, `fd_init`, `fdc_init`, `ps2_init`, `vfs_init`, `network_initialize`), unmask IRQ 0/6, `sti`, welcome banner, then falls into `shell_reload`.  Segment reload, ESP, GDT, and IDT are already in place from `high_entry`.  Any post-flip CPU exception lands in `idt.asm`'s `exc_common` and prints `EXCnn EIP=h CR2=h ERR=h` on COM1.
- **Ring-3 userland**: GDT has user code (0x18, DPL=3) and user data (0x20, DPL=3) descriptors plus a TSS at 0x28 whose `SS0:ESP0` points at the kernel stack.  The INT 30h gate is DPL=3 so ring-3 programs can call it; CPU exceptions and IRQs stay DPL=0 (hardware bypasses the gate-DPL check) so user code can't synthesise fake fault frames.  `program_enter` reloads DS/ES/FS/GS to `USER_DATA_SELECTOR` (0x23) and `iretd`s into ring 3 at `PROGRAM_BASE` with `ESP=USER_STACK_TOP` (0x8FFF0) and `EFLAGS=0x202` (IF=1, IOPL=0).  Privileged instructions (`cli`/`sti`/`in`/`out`/CR writes) `#GP` from userland.
- **Shell respawn** (`shell_reload` → `program_enter`): `vfs_find` + `vfs_load` for `bin/shell`, then `program_enter` resets the fd table, zeroes the program's BSS region per the trailer-magic protocol (`dw bss_size; dw 0xB055`), snapshots the ring-0 ESP into `[shell_esp]`, and `iretd`s the program at ring 3.  `sys_exit` from any program restores `[shell_esp]` (the CPU has already auto-switched to TSS.ESP0 on the ring-3 → 0 transition) and re-enters `shell_reload`.
- **Shell** (`src/c/shell.c`): Loaded from filesystem at `PROGRAM_BASE` (`0x0600`).  Provides CLI loop, command dispatch, and built-in commands using INT 30h syscalls.
- **Input buffer** at linear address `0x500`, max 256 characters.
- **Disk buffer** at phys `0xF000` (kernel-virt alias `0xC000F000`), pinned low so `bbfs.asm` / `ext2.asm` can keep their 16-bit `[bx+offset]`-style accesses to sector_buffer entries.
- **FD table** at phys `0xE000` (kernel-virt alias `0xC000E000`), same reasoning as sector_buffer.
- **Boot-time stash** at phys `0x4D0` (`boot_disk`, byte) and `0x4D2` (`directory_sector`, word), set by `boot.asm` before paging and read by the kernel through the direct map.  The kernel reads them via the direct map at `0xC00004D0` / `0xC00004D2`; user programs never see these phys addresses (per-program PDs only map user pages).
- **Kernel stack** at phys `KERNEL_RESERVED_BASE..KERNEL_RESERVED_BASE+0x4000` (16 KB; currently ~`0x10A000..0x10E000`, shifts with `kernel.bin` size).  `KERNEL_RESERVED_BASE = page_align(0x100000 + sizeof(kernel.bin))` is computed by `make_os.sh` and passed via `-DKERNEL_RESERVED_BASE=N` to the second `kernel.asm` pass and to `boot.asm`.  Lives outside `kernel.bin` to avoid 16 KB of zero padding on disk; reachable immediately after paging because PDE[768]'s direct map covers phys `0..0x3FFFFF`; reserved via `frame_reserve_range` at boot.  `kernel_stack` / `kernel_stack_top` are `equ`s in `kernel.asm`.
- **Resident kernel** (`kernel.bin`) is loaded at physical `0x100000` and runs at virtual `0xC0100000`.  The 256 MB direct map at `0xC0000000..0xCFFFFFFF` mirrors physical `0..256 MB` so the kernel can reach any frame the bitmap allocator returns.
- **Per-program address spaces:** each program runs in its own page directory built by `address_space_create` from `program_enter`.  The PD's kernel half (PDEs 768..1023) is copy-imaged from `kernel_pd_template` so the kernel direct map is reachable from every address space.  The user half (PDEs 0..767) is populated only with the program's own pages plus a shared vDSO PTE marked with the `ADDRESS_SPACE_PTE_SHARED` AVL bit (so `address_space_destroy` skips `frame_free` on it).  See the user-side virtual layout table below for the per-PD shape.
- Kernel sector count and reserved-region base are both derived at build time: `make_os.sh` measures `kernel.bin`, passes the sector count to `boot.asm` as `-DKERNEL_SECTORS=N`, computes `KERNEL_RESERVED_BASE = page_align(0x100000 + sizeof(kernel.bin))`, then re-assembles `kernel.asm` and `boot.asm` with `-DKERNEL_RESERVED_BASE=N`.  A size-invariant check between the two `kernel.asm` passes confirms the change cannot shift the binary.  The boot-time `kernel_bytes` word at MBR offset 508 holds `(BOOT_SECTORS + KERNEL_SECTORS) * 512` so `add_file.py`'s host-side `compute_directory_sector` arithmetic still works.

### Static memory map

Kernel-side fixed-physical regions, all reached through the kernel direct map at virt `0xC0000000 + phys`.  The "in kernel.bin?" column flags whether the bytes occupy the on-disk image (`yes`) or live as bare frames pre-reserved by `LOW_RESERVE_BYTES` (`no`).  Addresses from `kernel_stack` onward are derived from `KERNEL_RESERVED_BASE = page_align(0x100000 + sizeof(kernel.bin))`; example values shown are for the current build (~38 KB kernel).  Update this table when adding a new fixed-phys region so newcomers can find every slot in one place.

| Phys range | Kernel-virt | Size | Symbol / purpose | In kernel.bin? |
|---|---|---|---|---|
| `0x000004D0` | `0xC00004D0` | 1 B | `boot_disk` (BIOS drive number, set in real mode) | no |
| `0x000004D2` | `0xC00004D2` | 2 B | `directory_sector` (LBA of first directory sector) | no |
| `0x000004DE..0x000004FB` | `0xC00004DE..0xC00004FB` | 32 B | `ARGV` (shell-to-program argv staging) | no |
| `0x000004FC` | `0xC00004FC` | 4 B | `EXEC_ARG` (per-program arg pointer) | no |
| `0x00000500..0x000005FF` | `0xC0000500..0xC00005FF` | 256 B | `BUFFER` / E820 table (set by stage-2, read by bitmap allocator) | no |
| `0x0000E000..0x0000E1FF` | `0xC000E000..0xC000E1FF` | 512 B | FD table (kept low so 16-bit `[bx+offset]` accesses still work) | no |
| `0x0000F000..0x0000F1FF` | `0xC000F000..0xC000F1FF` | 512 B | `sector_buffer` (disk read buffer, used by `bbfs.asm` / `ext2.asm`) | no |
| `0x0000F200..0x0000F5FF` | `0xC000F200..0xC000F5FF` | 1024 B | `ext2_sd_buffer` (sliding 2-sector window for `ext2_search_blk`) | no |
| `0x00010000..0x00010FFF` | n/a | 4 KB | vDSO (shared user-virt frame; per-program PDs alias it user-side) | no |
| `0x00100000..` | `0xC0100000..` | ~38 KB | `kernel.bin` (resident kernel image) | yes |
| `KERNEL_RESERVED_BASE` (~`0x10A000..0x10DFFF`) | `0xC010A000..` | 16 KB | `kernel_stack` (`KERNEL_RESERVED_BASE = page_align(0x100000 + kernel_size)`) | no |
| ~`0x10E000..0x10E5FF` | `0xC010E000..` | 1.5 KB | `net_receive_buffer` (NE2000 RX scratch) | no |
| ~`0x10E600..0x10EBFF` | `0xC010E600..` | 1.5 KB | `net_transmit_buffer` (NE2000 TX scratch) | no |
| ~`0x10F000..0x12EFFF` | `0xC010F000..` | 128 KB | `program_scratch` (vfs_load staging buffer; page-aligned above TX buf) | no |
| ~`0x12F000..0x12FFFF` | `0xC012F000..` | 4 KB | boot PD (`BOOT_PD_PHYS`; promoted to `kernel_pd_template`) | no |
| ~`0x130000..0x130FFF` | `0xC0130000..` | 4 KB | first kernel PT (`FIRST_KERNEL_PT_PHYS`) | no |
| ~`0x131000+` | `0xC0131000+` | -- | `LOW_RESERVE_BYTES` ceiling — all frames below this are pre-reserved at boot; everything past is owned by the bitmap allocator | -- |

User-side virtual layout (per per-program PD; same shape for every program PD that `address_space_create` builds):

| User-virt range | Size | Purpose |
|---|---|---|
| `0x00000000..0x00000FFF` | 4 KB | NULL guard — not mapped (PTE[0] absent so `*(int *)0` raises #PF) |
| `0x00001000..0x00001FFF` | 4 KB | shell↔program handoff frame at `USER_DATA_BASE` (ARGV at +0x4DE, EXEC_ARG at +0x4FC, BUFFER at +0x500) |
| `0x00010000..0x00010FFF` | 4 KB | vDSO (`FUNCTION_PRINT_STRING`, `FUNCTION_DIE`, …) |
| `0x08048000..` | program-sized | program text + BSS (Linux ELF-shaped load address) |
| `0x3FFE0000..0x3FFEFFFF` | 64 KB | unmapped (stack guard region) |
| `0x3FFF0000..0x3FFFFFFF` | 64 KB | user stack (16 pages, top at `USER_STACK_TOP`) |
| `0x40000000` | -- | `USER_STACK_TOP` (one past end of stack) |
| `0xC0000000..` | 1 GB | kernel half (PDEs 768..1023, copy-imaged from `kernel_pd_template`) |

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

NE2000 ISA NIC driver at I/O base `0x300`. Requires QEMU `-netdev user,id=net0 -device ne2k_isa,netdev=net0,irq=3,iobase=0x300`. Polled mode (no interrupts). Networking buffers (`net_receive_buffer` / `net_transmit_buffer`, 1536 bytes each) live at fixed phys `0x184000`+ (right after the kernel stack), reached through the kernel direct map at virt `0xC0184000`+; the `equ` aliases are declared in `kernel.asm` so the buffers don't burn 3 KB of zero padding inside `kernel.bin`.

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
| 31h   | rtc_sleep    | Busy-wait for CX milliseconds                           |
| 32h   | rtc_uptime   | Get uptime in seconds, AX = elapsed seconds             |
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
- `src/arch/x86/boot/boot.asm` — Pre-paging boot binary (`org 0x7C00`): MBR + post-MBR real-mode bootstrap (second INT 13h read of kernel.bin into phys `0x10000`, E820 probe, PIC remap, A20, GDT load, CR0.PE flip) + 32-bit `early_pe_entry` (relocate kernel from phys `0x10000` to `0x100000`, build boot PD + first kernel PT, enable paging, far-jump to `high_entry` at virt `0xC0100000`).  Post-MBR region pads to the next 512-byte boundary; `BOOT_SECTORS` is derived (`(boot_end - post_mbr_continue) / 512`) so the count auto-grows when the boot code crosses a sector.  `make_os.sh` only has to measure `kernel.bin`'s sector count for the `-DKERNEL_SECTORS=N` second-pass nasm invocation.
- `src/arch/x86/kernel.asm` — Post-paging high-half kernel (`org 0xC0100000`): `high_entry` (segment / GDT / IDT / stack setup, identity-drop, bitmap init, kernel-PT allocation) followed by `%include`s of every kernel subsystem (drivers, fs, helpers, net stack, syscall dispatcher, system reboot/shutdown, IDT, post-flip entry, frame allocator) and the kernel GDT + vDSO blob (`incbin "vdso.bin"`).
- `src/arch/x86/idt.asm` — 32-bit IDT with CPU exception stubs and INT 30h gate; `idt_init` (called from `high_entry`) patches the high-half handler offsets at boot since the IDT_ENTRY macro can only emit the low 16 bits in `nasm -f bin` mode (section-relative labels reject `& 0FFFFh` / `>> 16` arithmetic).  Any post-flip exception lands in `exc_common` and prints `EXCnn EIP=h CR2=h ERR=h` on COM1
- `src/arch/x86/entry.asm` — `protected_mode_entry` (TSS patch, PIT + IRQ handler install, driver / VFS / NIC inits, banner) flowing into `shell_reload` (loads `bin/shell` and jumps), `program_enter` (fd reset, BSS zero, ESP snapshot, jump to `PROGRAM_BASE`), and the IRQ 0 / IRQ 6 handlers.  Segment / GDT / IDT / ESP setup happens in `kernel.asm`'s `high_entry` before falling into `protected_mode_entry`; the ring-0 stack itself lives at virt `0xC0180000..0xC0184000` (see Kernel stack note above), not in entry.asm.
- `src/memory_management/frame.asm` — Bitmap physical-frame allocator: `frame_alloc` / `frame_free` / `frame_init` (E820 walker) / `frame_reserve_range`.  256 MB ceiling, 8 KB bitmap in kernel BSS
- `src/arch/x86/syscall.asm` — INT 30h dispatch table and helpers; includes `syscall/fs.asm`, `syscall/io.asm`, `syscall/net.asm`, `syscall/rtc.asm`, `syscall/sys.asm`, `syscall/video.asm`
- `src/arch/x86/system.asm` — `reboot`, `shutdown` (PC-specific: 8042 reset, QEMU/Bochs shutdown ports)
- `src/drivers/ansi.asm` — ANSI escape sequence parser (`put_character`, `put_string`), `serial_character`; delegates to `drivers/vga.asm` for screen writes
- `src/drivers/ata.asm`, `src/drivers/fdc.asm` — Hardware disk drivers (ATA PIO and floppy DMA); called via `fs/block.asm`'s `read_sector`/`write_sector` dispatch (AX = 0-based sector number)
- `src/drivers/ne2k.asm` — NE2000 ISA NIC driver (polled-mode Ethernet); I/O base `0x300`, IRQ 3
- `src/drivers/ps2.asm` — PS/2 keyboard driver: `ps2_init`, `ps2_check`, `ps2_read`
- `src/drivers/rtc.asm` — RTC/timer: `rtc_tick_read`, `rtc_sleep_ms`, CMOS date read
- `src/drivers/vga.asm` — VGA driver: text and mode-13h helpers (`vga_set_mode`, `vga_clear_screen`, `vga_fill_block`, `vga_set_palette_color`, …) plus `fd_ioctl_vga` (the `/dev/vga` ioctl dispatcher for `VGA_IOCTL_MODE` / `VGA_IOCTL_FILL_BLOCK` / `VGA_IOCTL_SET_PALETTE`)
- `src/fs/fd.asm` — File descriptor table management: `fd_open` (synthesizes `/dev/vga` into `FD_TYPE_VGA` without touching the filesystem), `fd_read`, `fd_write`, `fd_close`, `fd_fstat`, `fd_ioctl`; includes `fs/fd/console.asm`, `fs/fd/fs.asm`, `fs/fd/net.asm`
- `src/fs/block.asm` — Block I/O dispatcher: `read_sector`, `write_sector` (dispatches to fdc/ata based on `boot_disk`)
- `src/fs/bbfs.asm` — BBoeOS filesystem implementation (VFS backend): `bbfs_chmod`, `bbfs_create`, `bbfs_find`, `bbfs_init`, `bbfs_load`, `bbfs_mkdir`, `bbfs_rename`, `bbfs_update_size`, plus internal helpers (`find_file`, `scan_directory_entries`, etc.)
- `src/fs/ext2.asm` — ext2 filesystem implementation (second VFS backend, auto-detected by `vfs_init`)
- `src/fs/vfs.asm` — VFS layer: runtime function-pointer table (`vfs_find_fn`, etc.), `vfs_found_*` state struct, thin wrapper functions (`vfs_find`, `vfs_create`, `vfs_rmdir`, …); `%include`s `fs/bbfs.asm` and `fs/ext2.asm`
- `src/lib/print.asm` — output utilities: `shared_print_*`, `shared_printf`, `shared_write_stdout`
- `src/lib/proc.asm` — program utilities: `shared_die`, `shared_exit`, `shared_get_character`, `shared_parse_argv`
- `src/net/net.asm` — 4-line orchestrator; includes `net/arp.asm`, `net/icmp.asm`, `net/ip.asm`, `net/udp.asm`.  The NE2000 hardware driver itself lives in `drivers/ne2k.asm`
- `src/syscall/` — syscall handler implementations: `fs.asm`, `io.asm`, `net.asm`, `rtc.asm`, `sys.asm`.  Dispatched from `arch/x86/syscall.asm` (the INT 30h entry)
- `src/c/` programs written in the C subset: `arp`, `asm`, `asmesc`, `bits`, `booltest`, `cat`, `chmod`, `cp`, `date`, `dns`, `draw`, `echo`, `edit`, `gdemo`, `gptest`, `gtable`, `hello`, `inctest`, `loop`, `loop_array`, `ls`, `mkdir`, `mv`, `netinit`, `netrecv`, `netsend`, `ping`, `rm`, `rmdir`, `shell`, `uptime`. `asmesc` smoke-tests the `asm(...)` inline-asm escape (both file-scope and statement forms); `bits` is a smoke test for cc.py's bitwise operators (`|`, `^`, `~`, `<<`, `>>`, `&`) and their compound-assignment forms; `booltest` is a smoke test for cc.py's booleanized comparison BinOps used as expression values (`int x = (a == b);` etc.); `gdemo` and `gtable` are smoke tests for cc.py's file-scope globals; `gptest` is a smoke test for the user-fault kill path in `idt.asm`'s `exc_common` (executes `cli` at CPL=3 to raise #GP, expects shell respawn); `inctest` is a smoke test for cc.py's `#include` directive (pairs with `src/c/inctest.h`).
- `src/c/edit.c` — Full-screen text editor with gap buffer, Ctrl+S save, Ctrl+Q quit.  All editor state is file-scope so cc.py parks it in BSS rather than auto-pinning to registers that `buffer_character_at` clobbers (it uses EDX/ECX as scratch).  The 1 MB gap buffer (`edit_buffer[EDIT_BUFFER_SIZE]`) and 2.5 KB kill buffer (`edit_kill_buffer[EDIT_KILL_BUFFER_SIZE]`) are BSS arrays — the per-program PD that `address_space_create` builds gets enough zero-filled user pages to back them via the trailer-magic protocol, so the gap buffer can hold any source file in the tree.  Disk reads chunk at 32767 bytes per `read()` because `SYS_IO_READ` returns `AX` (sign-extended), so a single read returning ≥ 32768 looks like a negative error.
- `src/c/asm.c` — Self-hosted x86 assembler (two-pass; byte-identical to NASM for everything in `static/`). Phase 1 port: the driver and handlers still live inside a single file-scope `asm("...")` block that wraps `archive/asm.asm`'s original NASM source; follow-up PRs extract pieces into pure C one family at a time. Supported directives and mnemonics are documented in the inline-asm body.

## Key Conventions

- Add new commands and functions in **sorted order** (alphabetical).
- Preserve existing comments when editing code.
- Shell command dispatch is a chain of `else if (streq(buf, "name"))` checks in `src/c/shell.c`. Adding a built-in requires a new branch (and a matching entry in the `help` string).
- The shell splits input at the first space: the command name is null-terminated in `BUFFER`, and `[EXEC_ARG]` points to the argument string (or 0 if none; use `set_exec_arg()`). Unknown commands are tried as external programs via `SYS_SYS_EXEC`; `SYS_SYS_EXIT` reloads the shell.
- Programs are loaded at `PROGRAM_BASE` (`0x0600`). The shell is the first program loaded at boot. Programs call kernel-provided functions at fixed addresses (e.g., `FUNCTION_PRINT_BCD`, `FUNCTION_WRITE_STDOUT`) instead of `%include`ing shared helpers. Only program-specific logic files (e.g., `dns_query.asm`, `parse_ip.asm`) are still `%include`d.
- Stage 1 functions must fit within the 512-byte MBR.
- When adding the `DIRECTORY_SECTOR` constant, the post-MBR sector count adjusts automatically.
- **Naming conventions**: Constants and string labels use `UPPER_CASE`. Functions and variables use `lower_case`. Local labels use `.dot_prefix`.
- All output goes through `put_character` (in MBR) which handles ANSI escape sequences for both screen and serial. The shell's line editor uses ANSI sequences (e.g., `ESC[nD` for cursor back, `ESC[nA` for cursor up) via `FUNCTION_PRINT_CHARACTER` for all output.

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
