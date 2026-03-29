# BBoeOS

A minimal x86 bootloader and OS written in NASM assembly, running in 16-bit real mode.

## Build and Run

```sh
./make_os.sh                                           # assemble and create floppy image
qemu-system-i386 -drive file=floppy.img,format=raw     # run in QEMU
qemu-system-i386 -drive file=floppy.img,format=raw -serial stdio  # with serial console
qemu-system-i386 -drive file=floppy.img,format=raw -serial stdio -netdev user,id=net0 -device ne2k_isa,netdev=net0,irq=3,iobase=0x300  # with NE2000 NIC
```

Requires `nasm` (`brew install nasm`).

## Architecture

Two-stage bootloader in flat binary format (`nasm -f bin`), loaded at `org 7C00h`.

- **Stage 1 (MBR, 512 bytes)**: Boot init, loads stage 2 via INT 13h, saves boot tick count. Contains `clear_screen`, ANSI parser (`put_char`, `put_string`), `serial_char`.
- **Stage 2**: Installs syscall interface (INT 30h), loads shell from filesystem.
- **Shell** (`src/programs/shell.asm`): Loaded from filesystem at `program_base` (`0x6000`). Provides CLI loop, command dispatch, and built-in commands using INT 30h syscalls.
- **Input buffer** at linear address `0x500`, max 256 characters.
- **Disk buffer** at `0x9000` for filesystem reads.
- **Stack** at `0050h:7700h` (linear `0x7C00`, grows downward).
- Stage 2 sector count is derived from `dir_sector` via `%assign stage2_sectors (dir_sector - 2)`.

### Filesystem

Trivial read-only filesystem on the floppy disk:

- **Sector 1**: MBR (stage 1)
- **Sectors 2 to dir_sector-1**: Stage 2
- **Sector dir_sector (10)**: File table / directory (32 entries x 16 bytes)
- **Sectors dir_sector+1 onward**: File data

Directory entry format (16 bytes): 12 bytes filename (null-terminated), 2 bytes start sector, 2 bytes file size. Files span consecutive sectors starting from the start sector.

Use `./add_file.sh floppy.img <file>` to add files to the image after building.

### Networking

NE2000 ISA NIC driver at I/O base `0x300`. Requires QEMU `-netdev user,id=net0 -device ne2k_isa,netdev=net0,irq=3,iobase=0x300`. Polled mode (no interrupts). Networking buffers: `NET_TX_BUF` at `0x9200` (1536 bytes), `NET_RX_BUF` at `0x9800` (1536 bytes).

### Serial Console

All output is mirrored to COM1. `put_char` (in stage 1 MBR) includes an ANSI escape sequence parser and automatic `\n` to `\r\n` conversion — strings only need `\n`. Raw bytes always go to serial, while ANSI sequences (e.g., `ESC[nD` for cursor back) are translated to INT 10h calls for the screen. `serial_char` writes to COM1 only (used internally by `put_char` and `scr_clear`). Input is polled from both keyboard (INT 16h) and COM1 simultaneously. Serial terminals send `0x7F` (DEL) for backspace, which is handled alongside `0x08`.

### Syscall Interface (INT 30h)

Programs loaded from the filesystem can use INT 30h for OS services:

| AH    | Name         | Description                                          |
|-------|--------------|------------------------------------------------------|
| 00h   | fs_find      | Find file, SI = filename, BX = entry ptr, CF on err  |
| 01h   | fs_read      | Read sector AL into disk_buffer, CF on error          |
| 10h   | io_getc      | Read one char, AL = char, AH = scan code              |
| 12h   | io_putc      | Print char in AL (screen + serial, ANSI-aware)        |
| 13h   | io_puts      | Print string at SI (screen + serial, ANSI-aware)      |
| 20h   | net_init     | Probe NE2000 NIC, DI = 6-byte MAC buffer, CF on err    |
| 30h   | rtc_datetime | Get date+time in BCD: CH=century, CL=year, DH=month, DL=day, BH=hours, BL=minutes, AL=seconds |
| 31h   | rtc_uptime   | Get uptime in seconds, AX = elapsed seconds             |
| 40h   | scr_clear    | Clear screen                                          |
| F0h   | sys_exec     | Execute program, SI = filename, CF on error            |
| F1h   | sys_exit     | Reload and return to shell                             |
| F2h   | sys_reboot   | Reboot                                                |
| F3h   | sys_shutdown  | Shutdown                                              |

## File Structure

- `add_file.sh` — Host-side script to add files to the floppy image filesystem
- `make_os.sh` — Build script (assembles kernel, auto-discovers and builds all programs, creates floppy image)
- `src/include/constants.asm` — Shared constants (`BUFFER`, `DIR_SECTOR`, `DISK_BUFFER`, `EXEC_ARG`, `NE2K_BASE`, `PROGRAM_BASE`, `SYS_*` syscall numbers, etc.)
- `src/include/print_bcd.asm` — Shared: `print_bcd` (prints AL as two BCD digits)
- `src/include/print_dec.asm` — Shared: `print_dec` (prints AL as two zero-padded decimal digits)
- `src/include/print_hex.asm` — Shared: `print_hex` (prints AL as two uppercase hex digits)
- `src/include/print_mac.asm` — Shared: `print_mac` (prints SI as colon-separated hex MAC address)
- `src/include/str_*.asm` — Shared strings: `DISK_ERROR`, `FILE_NOT_FOUND`
- `src/kernel/ansi.asm` — ANSI escape sequence parser (`put_char`, `put_string`), `serial_char` — included in stage 1 MBR
- `src/kernel/bboeos.asm` — Stage 1 boot code (includes `ansi.asm`), shell loader, `%include` directives, variables, strings
- `src/kernel/io.asm` — `find_file`, `read_sector`
- `src/kernel/net.asm` — NE2000 NIC driver: `ne2k_probe` — included in stage 2
- `src/kernel/syscall.asm` — INT 30h syscall handler, `install_syscalls`
- `src/kernel/system.asm` — `reboot`, `shutdown`
- `src/programs/cat.asm` — Cat program: displays file contents with `\n` to `\r\n` conversion
- `src/programs/date.asm` — Date program: displays YYYY-MM-DD HH:MM:SS
- `src/programs/draw.asm` — Draw program: 16-color graphics mode with cursor and background controls
- `src/programs/ls.asm` — Ls program: lists files in the directory
- `src/programs/netinit.asm` — Netinit program: probes NE2000 NIC and displays MAC address
- `src/programs/shell.asm` — Shell program: CLI loop, command dispatch, built-in commands, external program exec, line editor with full editing (insert, delete, cursor movement, kill/yank)
- `src/programs/uptime.asm` — Uptime program: displays HH:MM:SS since boot

## Key Conventions

- Add new commands and functions in **sorted order** (alphabetical).
- Preserve existing comments when editing code.
- Shell command dispatch uses a table of `dw string_ptr, handler_ptr` pairs terminated by `dw 0`. Adding a command requires: a `cmd_*` handler, a table entry, and the command string.
- The shell splits input at the first space: the command name is null-terminated in `BUFFER`, and `[EXEC_ARG]` points to the argument string (or 0 if none). Unknown commands are tried as external programs via `SYS_EXEC`; `SYS_EXIT` reloads the shell.
- Programs are loaded at `PROGRAM_BASE` (`0x6000`). The shell is the first program loaded at boot. Programs `%include` only the granular shared files they need (e.g., `print_bcd.asm`, `str_newline.asm`) at the end of the source.
- Stage 1 functions must fit within the 512-byte MBR.
- When adding the `DIR_SECTOR` constant, stage 2 sector count adjusts automatically.
- **Naming conventions**: Constants and string labels use `UPPER_CASE`. Functions and variables use `lower_case`. Local labels use `.dot_prefix`.
- All output goes through `put_char` (in MBR) which handles ANSI escape sequences for both screen and serial. The shell's line editor uses ANSI sequences (e.g., `ESC[nD` for cursor back) via `SYS_IO_PUTC` for all output.

## 16-bit Real Mode Constraints

- Only BX, BP, SI, DI are valid base/index registers in memory operands (not AX, CX, DX, SP).
- BIOS interrupts: INT 10h (video), INT 13h (disk), INT 16h (keyboard), INT 1Ah (RTC/timer).
- INT 10h AH=03h clobbers CX (returns cursor scanline shape) — save any value in CX before calling.
- `mul` clobbers DX (result in DX:AX) — save DX if needed.
- 32-bit registers (EAX, ECX, EDX) are usable with operand-size prefix (386+).
- Use unsigned conditional jumps (`jb`/`jbe`/`ja`/`jae`) for byte counts, file sizes, and buffer lengths — not signed (`jl`/`jle`/`jg`/`jge`). Signed jumps misinterpret values > 32767.
- Programs must `cld` before using string instructions (`lodsb`, `rep movsw`, etc.) — the direction flag may be in an unknown state at program entry.
- Teletype backspace (`\b` via INT 10h AH=0Eh) does not wrap across screen lines. The ANSI parser's `ESC[nD` handler uses INT 10h AH=02h/03h with linear position math for proper wrapping.

## Testing

No automated tests. Test manually in QEMU after each change. Use `-serial stdio` to test serial console. Use `-machine acpi=off` to test shutdown failure path.
