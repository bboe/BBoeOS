# BBoeOS

A minimal x86 operating system with a single-file bootloader-plus-kernel, shell, filesystem, networking stack, self-hosted assembler, and C compiler.  Boots in 16-bit real mode, flips into flat 32-bit ring-0 protected mode, and runs the shell and user programs from there.

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

Single flat-binary kernel (`nasm -f bin`) loaded at `org 7C00h`.  The first 512 bytes are the MBR — DS/ES/SS:SP setup, disk reset, INT 13h read of the rest of the kernel into `0x7E00`, jump into the post-MBR path.  On disk error the MBR prints `!` via INT 10h AH=0Eh and halts.  After the read the post-MBR code remaps the PIC (master IRQs → 0x20-0x27), enables A20, loads the 32-bit GDT, and far-jumps through `enter_protected_mode` into `protected_mode_entry`.  All of this lives in `src/arch/x86/boot/bboeos.asm`; the previous `stage1.asm` / `stage2.asm` / `stage1_5.asm` / `kernel.asm` split has been collapsed into the one file.

- **Post-flip entry** (`protected_mode_entry` in `src/arch/x86/entry.asm`): segment reload (CS=0x08, DS/ES/SS=0x10, ESP=0x9FFF0), PIT @ 100 Hz, 32-bit IRQ 0 / IRQ 6 handlers via `idt_set_gate32`, driver inits (`ata_init`, `fd_init`, `fdc_init`, `ps2_init`, `vfs_init`, `network_initialize`), unmask IRQ 0/6, `sti`, welcome banner, then falls into `shell_reload`.  Any post-flip CPU exception lands in `idt.asm`'s `exc_common` and prints `EXCnn` on COM1.
- **Shell respawn** (`shell_reload` → `program_enter`): `vfs_find` + `vfs_load` for `bin/shell`, then `program_enter` resets the fd table, zeroes the program's BSS region per the trailer-magic protocol (`dw bss_size; dw 0xB055`), snapshots ESP into `[shell_esp]`, and `jmp PROGRAM_BASE`.  `sys_exit` from any program restores `[shell_esp]` and re-enters `shell_reload`.
- **Shell** (`src/c/shell.c`): Loaded from filesystem at `PROGRAM_BASE` (`0x0600`).  Provides CLI loop, command dispatch, and built-in commands using INT 30h syscalls.
- **Input buffer** at linear address `0x500`, max 256 characters.
- **Disk buffer** at `0xE000` for filesystem reads.
- **Stack** at linear `0x9FFF0`, grows downward.
- **Resident kernel** (the `bboeos.asm` flat binary) lives at `0x7C00` up through (roughly) `0xE000`, where the disk and NIC buffers begin.  Programs loaded at `PROGRAM_BASE` (`0x0600`) may allocate working buffers in segment 0, but everything between `0x7C00` and `0xEE00` is off-limits — overwriting it corrupts the live kernel and the next `int 30h` jumps into trashed code.  Programs that need more than ~28 KB of RAM put their buffers in extended memory above the 1 MB mark (e.g. `edit`'s 1 MB gap buffer at `0x100000`); flat 32-bit segments make any address up to ESP usable.
- Kernel sector count is derived from `DIRECTORY_SECTOR` via `%assign stage2_sectors (DIRECTORY_SECTOR - 1)` (the constant name carries over from the pre-merge stage1/stage2 split).

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

NE2000 ISA NIC driver at I/O base `0x300`. Requires QEMU `-netdev user,id=net0 -device ne2k_isa,netdev=net0,irq=3,iobase=0x300`. Polled mode (no interrupts). Networking buffers: `NET_TRANSMIT_BUFFER` at `0xE200` (1536 bytes), `NET_RECEIVE_BUFFER` at `0xE800` (1536 bytes).

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
- `src/arch/x86/boot/bboeos.asm` — Single flat-binary entry: 16-bit MBR setup, stage-2 disk read, PIC remap, A20, GDT load, far-jmp into protected mode, then `%include`s every kernel subsystem (drivers, fs, lib helpers, net stack, syscall dispatcher, system reboot/shutdown, IDT, post-flip entry).  No more `stage1.asm` / `stage2.asm` / `stage1_5.asm` / `kernel.asm` split — they were collapsed into this file
- `src/arch/x86/idt.asm` — 32-bit IDT with CPU exception stubs and INT 30h gate; `idt_install` runs in the bootstrap right before the protected mode flip so any post-flip exception lands in `exc_common` and prints `EXCnn` on COM1
- `src/arch/x86/entry.asm` — `protected_mode_entry` (segment reload, PIT + IRQ handler install, driver / VFS / NIC inits, banner) flowing into `shell_reload` (loads `bin/shell` and jumps), `program_enter` (fd reset, BSS zero, ESP snapshot, jump to `PROGRAM_BASE`), and the IRQ 0 / IRQ 6 handlers
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
- `src/c/` programs written in the C subset: `arp`, `asm`, `asmesc`, `bits`, `booltest`, `cat`, `chmod`, `cp`, `date`, `dns`, `draw`, `echo`, `edit`, `gdemo`, `gtable`, `hello`, `inctest`, `loop`, `loop_array`, `ls`, `mkdir`, `mv`, `netinit`, `netrecv`, `netsend`, `ping`, `rm`, `rmdir`, `shell`, `uptime`. `asmesc` smoke-tests the `asm(...)` inline-asm escape (both file-scope and statement forms); `bits` is a smoke test for cc.py's bitwise operators (`|`, `^`, `~`, `<<`, `>>`, `&`) and their compound-assignment forms; `booltest` is a smoke test for cc.py's booleanized comparison BinOps used as expression values (`int x = (a == b);` etc.); `gdemo` and `gtable` are smoke tests for cc.py's file-scope globals; `inctest` is a smoke test for cc.py's `#include` directive (pairs with `src/c/inctest.h`).
- `src/c/edit.c` — Full-screen text editor with gap buffer, Ctrl+S save, Ctrl+Q quit. The 1 MB gap buffer (`EDIT_BUFFER_SIZE`) and 2.5 KB kill buffer (`EDIT_KILL_BUFFER_SIZE`) sit in extended memory (`EDIT_BUFFER_BASE` = 0x100000, `EDIT_KILL_BUFFER` = 0x200000) — above the 1 MB mark to clear the VGA/BIOS regions at 0xA0000-0xFFFFF, so the gap buffer can hold any source file in the tree.  Cursor / view / dirty / kill / status state is BSS (file-scope globals) so cc.py won't pin to registers that `buffer_character_at` clobbers.  Disk reads chunk at 32767 bytes per `read()` because `SYS_IO_READ` returns `AX` (sign-extended), so a single read returning ≥ 32768 looks like a negative error.
- `src/c/asm.c` — Self-hosted x86 assembler (two-pass; byte-identical to NASM for everything in `static/`). Phase 1 port: the driver and handlers still live inside a single file-scope `asm("...")` block that wraps `archive/asm.asm`'s original NASM source; follow-up PRs extract pieces into pure C one family at a time. Supported directives and mnemonics are documented in the inline-asm body.

## Key Conventions

- Add new commands and functions in **sorted order** (alphabetical).
- Preserve existing comments when editing code.
- Shell command dispatch is a chain of `else if (streq(buf, "name"))` checks in `src/c/shell.c`. Adding a built-in requires a new branch (and a matching entry in the `help` string).
- The shell splits input at the first space: the command name is null-terminated in `BUFFER`, and `[EXEC_ARG]` points to the argument string (or 0 if none; use `set_exec_arg()`). Unknown commands are tried as external programs via `SYS_SYS_EXEC`; `SYS_SYS_EXIT` reloads the shell.
- Programs are loaded at `PROGRAM_BASE` (`0x0600`). The shell is the first program loaded at boot. Programs call kernel-provided functions at fixed addresses (e.g., `FUNCTION_PRINT_BCD`, `FUNCTION_WRITE_STDOUT`) instead of `%include`ing shared helpers. Only program-specific logic files (e.g., `dns_query.asm`, `parse_ip.asm`) are still `%include`d.
- Stage 1 functions must fit within the 512-byte MBR.
- When adding the `DIRECTORY_SECTOR` constant, stage 2 sector count adjusts automatically.
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
