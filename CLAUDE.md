# BBoeOS

A minimal x86 operating system with a two-stage bootloader, shell, filesystem, networking stack, self-hosted assembler, and C compiler — all running in 16-bit real mode on a floppy disk.

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

Two-stage bootloader in flat binary format (`nasm -f bin`), loaded at `org 7C00h`.

- **Stage 1 (MBR, 512 bytes)**: Minimal boot loader — sets up DS/ES/SS:SP, resets disk, reads stage 2 via BIOS INT 13h, jumps to `boot_shell`.  On error, prints `!` via INT 10h AH=0Eh and halts.  No string output, no kernel init.
- **Stage 2**: `boot_shell` runs kernel init (`pic_remap`, `rtc_tick_init`, `install_syscalls`, `network_initialize`), prints the welcome banner via `drivers/ansi.asm`, initialises PS/2 / FDC / file descriptors / VFS, installs the 32-bit IDT, then far-jumps through `enter_protected_mode` into `protected_mode_entry`.  The post-flip path currently halts; widening the driver inits and shell load into 32-bit code is the next tranche of work.
- **Shell** (`src/c/shell.c`): Loaded from filesystem at `program_base` (`0x0600`) in the pre-pmode real-mode boot path.  Currently unreachable on the `protectedmode` branch — the pmode flip happens before `shell_reload` — and will come back once the post-flip kernel widens enough to run a 32-bit shell.  Provides CLI loop, command dispatch, and built-in commands using INT 30h syscalls.
- **Input buffer** at linear address `0x500`, max 256 characters.
- **Disk buffer** at `0xE000` for filesystem reads.
- **Stack** in its own segment at `9000h:0FFF0h` (linear `0x9FFF0`, grows downward).
- **Resident kernel** (stage 1 MBR + stage 2) lives in segment 0 from `0x7C00` up through (roughly) `0xE000`, where the disk and NIC buffers begin. Programs loaded at `PROGRAM_BASE` (`0x0600`) may allocate working buffers in segment 0, but everything between `0x7C00` and `0xEE00` is off-limits — overwriting it corrupts the live kernel and the next `int 30h` jumps into trashed code.
- Stage 2 sector count is derived from `DIRECTORY_SECTOR` via `%assign stage2_sectors (DIRECTORY_SECTOR - 1)`.

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

All output is mirrored to COM1. `put_character` in `drivers/ansi.asm` includes an ANSI escape sequence parser and automatic `\n` to `\r\n` conversion — strings only need `\n`. Raw bytes always go to serial, while ANSI sequences (e.g., `ESC[nA` cursor up, `ESC[nC` cursor forward, `ESC[nD` cursor back, `ESC[r;cH` cursor position, `ESC[0m` reset colors, `ESC[38;5;Nm` foreground, `ESC[48;5;Nm` background) are translated to INT 10h calls for the screen. `put_string` and `serial_character` live in the same file.  Stage 1 does no string output; on boot it's BIOS text mode until stage 2 initialises the driver.  `serial_character` writes to COM1 only (used internally by `put_character` and `video_mode`). Input is polled from both keyboard (INT 16h) and COM1 simultaneously. Serial terminals send `0x7F` (DEL) for backspace, which is handled alongside `0x08`.

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
- `src/arch/x86/boot/bboeos.asm` — Top-level flat-binary entry; `%include`s `stage1.asm`, `stage2.asm`, then `arch/x86/kernel.asm` to aggregate every kernel subsystem after the boot handoff
- `src/arch/x86/boot/stage1.asm` — MBR (512 bytes): set DS/ES/SS:SP, reset disk, load stage 2 via BIOS INT 13h, jump to `boot_shell`.  On error prints `!` via INT 10h AH=0Eh and halts.  No string output, no kernel init
- `src/arch/x86/boot/stage2.asm` — Post-MBR boot handoff: jump table, `boot_shell` (kernel init → welcome banner → driver inits → VFS init → `idt_install` + `jmp enter_protected_mode`), `shell_reload` (currently unreachable: pre-pmode shell loader left in place for the eventual widened shell path), `bss_setup`.  Does NOT `%include` kernel subsystems — that's `kernel.asm`'s job
- `src/arch/x86/kernel.asm` — Kernel subsystem aggregator: `%include`s every `drivers/`, `fs/`, `lib/`, `net/` file plus the arch-specific `pic.asm`, `syscall.asm`, `system.asm`, `init.asm`, and the pmode flip trio (`boot/stage1_5.asm`, `idt.asm`, `entry.asm`).  Pulled in once by `bboeos.asm`, immediately after `stage2.asm`, so kernel code sits contiguously after the boot handoff
- `src/arch/x86/init.asm` — `kernel_init`: PIC remap, PIT + IRQ 0 init, INT 30h gate install, NIC probe.  Called once from `boot_shell` before the pmode flip; some steps (IDT-dependent IRQ handlers, 32-bit INT 30h gate) will move post-flip as subsystems widen
- `src/arch/x86/idt.asm` — 32-bit IDT with CPU exception stubs and INT 30h gate.  `idt_install` runs in `boot_shell` right before `enter_protected_mode`, so any post-flip exception lands in `exc_common` and prints `EXCnn` on COM1
- `src/arch/x86/entry.asm` — `protected_mode_entry`: 32-bit post-flip landing pad.  Currently a `cli/hlt` loop; replaced by actual 32-bit kernel work (driver re-init, shell load) as subsystems widen
- `src/arch/x86/pic.asm` — `pic_remap`: ICW1-ICW4 sequence that moves master IRQs to 0x20-0x27 and slave IRQs to 0x28-0x2F (prerequisite for the pmode flip)
- `src/arch/x86/boot/stage1_5.asm` — 16→32-bit protected-mode entry, GDT (the "stage 1.5" of the boot flow).  `enter_protected_mode` fires at the tail of `boot_shell` after real-mode init completes
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
- `src/lib/lib.asm` — 2-line orchestrator; includes `lib/print.asm` and `lib/proc.asm`
- `src/lib/print.asm` — output utilities: `shared_print_*`, `shared_printf`, `shared_write_stdout`
- `src/lib/proc.asm` — program utilities: `shared_die`, `shared_exit`, `shared_get_character`, `shared_parse_argv`
- `src/net/net.asm` — 4-line orchestrator; includes `net/arp.asm`, `net/icmp.asm`, `net/ip.asm`, `net/udp.asm`.  The NE2000 hardware driver itself lives in `drivers/ne2k.asm`
- `src/syscall/` — syscall handler implementations: `fs.asm`, `io.asm`, `net.asm`, `rtc.asm`, `sys.asm`.  Dispatched from `arch/x86/syscall.asm` (the INT 30h entry)
- `src/c/` programs written in the C subset: `arp`, `asm`, `asmesc`, `bits`, `booltest`, `cat`, `chmod`, `cp`, `date`, `dns`, `draw`, `echo`, `edit`, `gdemo`, `gtable`, `hello`, `inctest`, `loop`, `loop_array`, `ls`, `mkdir`, `mv`, `netinit`, `netrecv`, `netsend`, `ping`, `rm`, `rmdir`, `shell`, `uptime`. `asmesc` smoke-tests the `asm(...)` inline-asm escape (both file-scope and statement forms); `bits` is a smoke test for cc.py's bitwise operators (`|`, `^`, `~`, `<<`, `>>`, `&`) and their compound-assignment forms; `booltest` is a smoke test for cc.py's booleanized comparison BinOps used as expression values (`int x = (a == b);` etc.); `gdemo` and `gtable` are smoke tests for cc.py's file-scope globals; `inctest` is a smoke test for cc.py's `#include` directive (pairs with `src/c/inctest.h`).
- `src/c/edit.c` — Full-screen text editor with gap buffer, Ctrl+S save, Ctrl+Q quit. Gap buffer at `EDIT_BUFFER_BASE` (`0x2000`) up to the 2.5 KB kill buffer at `EDIT_KILL_BUFFER` (`0x7200`); sizes are defined in `constants.asm`. Still cannot open `asm.asm` (118 KB) — lifting that requires moving the gap buffer out of segment 0; see "Known limitations" in README.md.
- `src/c/asm.c` — Self-hosted x86 assembler (two-pass; byte-identical to NASM for everything in `static/`). Phase 1 port: the driver and handlers still live inside a single file-scope `asm("...")` block that wraps `archive/asm.asm`'s original NASM source; follow-up PRs extract pieces into pure C one family at a time. Supported directives and mnemonics are documented in the inline-asm body.

## Key Conventions

- Add new commands and functions in **sorted order** (alphabetical).
- Preserve existing comments when editing code.
- Shell command dispatch is a chain of `else if (streq(buf, "name"))` checks in `src/c/shell.c`. Adding a built-in requires a new branch (and a matching entry in the `help` string).
- The shell splits input at the first space: the command name is null-terminated in `BUFFER`, and `[EXEC_ARG]` points to the argument string (or 0 if none; use `set_exec_arg()`). Unknown commands are tried as external programs via `SYS_EXEC`; `SYS_EXIT` reloads the shell.
- Programs are loaded at `PROGRAM_BASE` (`0x0600`). The shell is the first program loaded at boot. Programs call kernel-provided functions at fixed addresses (e.g., `FUNCTION_PRINT_BCD`, `FUNCTION_WRITE_STDOUT`) instead of `%include`ing shared helpers. Only program-specific logic files (e.g., `dns_query.asm`, `parse_ip.asm`) are still `%include`d.
- Stage 1 functions must fit within the 512-byte MBR.
- When adding the `DIRECTORY_SECTOR` constant, stage 2 sector count adjusts automatically.
- **Naming conventions**: Constants and string labels use `UPPER_CASE`. Functions and variables use `lower_case`. Local labels use `.dot_prefix`.
- All output goes through `put_character` (in MBR) which handles ANSI escape sequences for both screen and serial. The shell's line editor uses ANSI sequences (e.g., `ESC[nD` for cursor back, `ESC[nA` for cursor up) via `FUNCTION_PRINT_CHARACTER` for all output.

## 16-bit Real Mode Constraints

- Only BX, BP, SI, DI are valid base/index registers in memory operands (not AX, CX, DX, SP).
- BIOS interrupts: INT 10h (video), INT 13h (disk), INT 16h (keyboard), INT 1Ah (RTC/timer).
- INT 10h AH=03h clobbers CX (returns cursor scanline shape) — save any value in CX before calling.
- `mul` clobbers DX (result in DX:AX) — save DX if needed.
- 32-bit registers (EAX, ECX, EDX) are usable with operand-size prefix (386+).
- Use unsigned conditional jumps (`jb`/`jbe`/`ja`/`jae`) for byte counts, file sizes, and buffer lengths — not signed (`jl`/`jle`/`jg`/`jge`). Signed jumps misinterpret values > 32767.
- Programs must `cld` before using string instructions (`lodsb`, `rep movsw`, etc.) — the direction flag may be in an unknown state at program entry.
- Teletype backspace (`\b` via INT 10h AH=0Eh) does not wrap across screen lines. The ANSI parser's `ESC[nD` handler uses INT 10h AH=02h/03h with linear position math for proper wrapping.

## Python conventions

- Every function uses mandatory keyword arguments (keyword-only via `*`) unless the positional args are self-evident (single obvious arg → positional-only via `/`). Arguments sorted alphabetically at definition and call sites.
- Functions sorted alphabetically within their scope (module, class).
- No abbreviations in function or variable names. Examples: `expression` (not `expr`), `generate` (not `gen`), `statement` (not `stmt`), `function` (not `func`), `directory` (not `dir`), `command` (not `cmd`), `message` (not `msg`), `process` (not `proc`), `reference` (not `ref`), `buffer` (not `buf`), `offset` (not `off`), `declaration` (not `decl`), `parameter` (not `param`), `allocate` (not `alloc`), `file_descriptor` (not `fd`), `serial` (not `ser`), `sector` (not `sec`).

## Releases

Update `CHANGELOG.md` with new entries as features land. Group entries by date under the Unreleased section. After a batch of significant improvements, bump the version in `src/arch/x86/boot/stage1.asm` (the `WELCOME` string) and move the Unreleased entries under a new version header with updated comparison links.

## Testing

Manual testing in QEMU is still the primary workflow — use `-serial stdio` to exercise the serial console and `-machine acpi=off` to test the shutdown failure path.

Automated self-hosting test: `tests/test_asm.py` boots the OS in QEMU and has the self-hosted assembler reassemble each program in `static/`, then diffs the result byte-for-byte against NASM's output. It drives QEMU via a serial fifo and waits for the `$ ` shell prompt (no fixed sleeps), so each program finishes in a second or two.

- `tests/test_asm.py` — run the full suite
- `tests/test_asm.py <name>` — run a single program; on single-program runs the nasm reference, assembled output, and drive image are copied to a persistent temp directory whose path is printed at the end

Filesystem regression tests: `tests/test_bboefs.py` boots the OS, runs shell command sequences, and inspects the resulting drive image to verify fs_copy / fs_mkdir / fs_find / fs_create handle large files (>64 KB), sectors past 255, and entries that live in the second directory sector. `tests/test_bboefs.py <name>` runs a single test.
