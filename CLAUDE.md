# BBoeOS

A minimal x86 bootloader and OS written in NASM assembly, running in 16-bit real mode.

## Build and Run

```sh
./make_os.sh                                           # assemble and create floppy image
qemu-system-i386 -drive file=drive.img,format=raw     # run in QEMU (IDE mode)
qemu-system-i386 -drive file=drive.img,if=floppy,format=raw  # run as floppy
qemu-system-i386 -drive file=drive.img,format=raw -serial stdio  # with serial console
qemu-system-i386 -drive file=drive.img,format=raw -serial stdio -netdev user,id=net0 -device ne2k_isa,netdev=net0,irq=3,iobase=0x300  # with NE2000 NIC
```

Requires `nasm` (`brew install nasm`).

## Architecture

Two-stage bootloader in flat binary format (`nasm -f bin`), loaded at `org 7C00h`.

- **Stage 1 (MBR, 512 bytes)**: Boot init, loads stage 2 via INT 13h, saves boot tick count. Contains `clear_screen`, ANSI parser (`put_char`, `put_string`), `serial_char`.
- **Stage 2**: Installs syscall interface (INT 30h), loads shell from filesystem.
- **Shell** (`src/programs/shell.asm`): Loaded from filesystem at `program_base` (`0x0600`). Provides CLI loop, command dispatch, and built-in commands using INT 30h syscalls.
- **Input buffer** at linear address `0x500`, max 256 characters.
- **Disk buffer** at `0xE000` for filesystem reads.
- **Stack** in its own segment at `9000h:0FFF0h` (linear `0x9FFF0`, grows downward).
- **Resident kernel** (stage 1 MBR + stage 2) lives in segment 0 from `0x7C00` up through (roughly) `0xE000`, where the disk and NIC buffers begin. Programs loaded at `PROGRAM_BASE` (`0x0600`) may allocate working buffers in segment 0, but everything between `0x7C00` and `0xEE00` is off-limits — overwriting it corrupts the live kernel and the next `int 30h` jumps into trashed code.
- Stage 2 sector count is derived from `dir_sector` via `%assign stage2_sectors (dir_sector - 2)`.

### Filesystem

Trivial read-only filesystem on the floppy disk:

- **Sector 1**: MBR (stage 1)
- **Sectors 2 to dir_sector-1**: Stage 2
- **Sectors dir_sector to dir_sector+1**: File table / root directory (`DIR_SECTORS` = 2 sectors, 32 entries x 32 bytes)
- **Sectors dir_sector+2 onward**: File data

Directory entry format (32 bytes): 27 bytes filename (null-terminated, max 26 chars), 1 byte flags (`FLAG_EXEC = 0x01`, `FLAG_DIR = 0x02`), 2 bytes start sector, 2 bytes file size. Files span consecutive sectors starting from the start sector.

Subdirectories: one level under root only. A subdirectory occupies `DIR_SECTORS` (= 2) consecutive sectors and holds 32 entries, matching the root layout. File paths in syscalls and programs may contain a single `/` to reference a file inside a subdirectory (e.g., `dir/file`). Executables live in `bin/`; the shell automatically retries `bin/<name>` when an external command is not found in the root directory. Static reference sources (the `.asm` files used by the self-hosted assembler tests) live in `src/`. The assembler resolves `%include` paths relative to the directory of its source argument, so `asm src/cat.asm out` correctly finds `src/constants.asm`.

Use `./add_file.py <file>` to add files to the image. Use `./add_file.py -d <dir> <file>` to add a file inside a subdirectory, and `./add_file.py --mkdir <dirname>` to create a subdirectory.

### Networking

NE2000 ISA NIC driver at I/O base `0x300`. Requires QEMU `-netdev user,id=net0 -device ne2k_isa,netdev=net0,irq=3,iobase=0x300`. Polled mode (no interrupts). Networking buffers: `NET_TX_BUF` at `0xE200` (1536 bytes), `NET_RX_BUF` at `0xE800` (1536 bytes).

### Serial Console

All output is mirrored to COM1. `put_char` (in stage 1 MBR) includes an ANSI escape sequence parser and automatic `\n` to `\r\n` conversion — strings only need `\n`. Raw bytes always go to serial, while ANSI sequences (e.g., `ESC[nA` cursor up, `ESC[nC` cursor forward, `ESC[nD` cursor back) are translated to INT 10h calls for the screen. `serial_char` writes to COM1 only (used internally by `put_char` and `scr_clear`). Input is polled from both keyboard (INT 16h) and COM1 simultaneously. Serial terminals send `0x7F` (DEL) for backspace, which is handled alongside `0x08`.

### Syscall Interface (INT 30h)

Programs loaded from the filesystem can use INT 30h for OS services:

| AH    | Name         | Description                                          |
|-------|--------------|------------------------------------------------------|
| 00h   | fs_chmod     | Set file flags, SI = filename, AL = flags, CF on err  |
| 01h   | fs_copy      | Copy file, SI = src filename, DI = dest filename, CF on err |
| 02h   | fs_create    | Create file, SI = filename, AX = start sector, CF on err |
| 03h   | fs_find      | Find file, SI = filename, BX = entry ptr in disk_buffer, CF on err |
| 04h   | fs_mkdir     | Create subdirectory, SI = name, AX = start sector, CF on err |
| 05h   | fs_read      | Read sector CX (16-bit) into disk_buffer, CF on error |
| 06h   | fs_rename    | Rename or move file, SI = old name, DI = new name, CF on err |
| 07h   | fs_write     | Write disk_buffer to sector CX (16-bit; CX=0: write back directory), CF on error |
| 10h   | io_getc      | Read one char, AL = char, AH = scan code              |
| 12h   | io_putc      | Print char in AL (screen + serial, ANSI-aware)        |
| 13h   | io_puts      | Print string at SI (screen + serial, ANSI-aware)      |
| 20h   | net_arp      | ARP resolve, SI = 4-byte IP, DI = 6-byte MAC, CF err   |
| 21h   | net_init     | Probe NE2000 NIC, DI = 6-byte MAC buffer, CF on err    |
| 22h   | net_ping     | ICMP ping, SI = 4-byte IP, AX = RTT ticks, CF timeout  |
| 23h   | net_recv     | Receive frame, DI = buf, CX = len, CF if none          |
| 24h   | net_send     | Send raw Ethernet frame, SI = frame, CX = len, CF err  |
| 25h   | net_udp_recv | UDP recv, DI = data, CX = len, BX = src port, CF none  |
| 26h   | net_udp_send | UDP send, BX = IP, DI = src port, DX = dst port, SI = data, CX = len |
| 30h   | rtc_datetime | Get date+time in BCD: CH=century, CL=year, DH=month, DL=day, BH=hours, BL=minutes, AL=seconds |
| 31h   | rtc_uptime   | Get uptime in seconds, AX = elapsed seconds             |
| 40h   | scr_clear    | Clear screen                                          |
| F0h   | sys_exec     | Execute program, SI = filename, CF on error            |
| F1h   | sys_exit     | Reload and return to shell                             |
| F2h   | sys_reboot   | Reboot                                                |
| F3h   | sys_shutdown  | Shutdown                                              |

## File Structure

- `add_file.py` — Host-side script to add files to the drive image filesystem
- `make_os.sh` — Build script (assembles kernel, auto-discovers and builds all programs, creates floppy image)
- `src/include/constants.asm` — Shared constants (`BUFFER`, `DIR_SECTOR`, `DISK_BUFFER`, `EXEC_ARG`, `NE2K_BASE`, `PROGRAM_BASE`, `SYS_*` syscall numbers, etc.)
- `src/include/dns_query.asm` — Shared: `dns_query` (sends DNS A query for domain at SI; returns DI = first answer record, AL = ANCOUNT, CF on error; caller defines `dns_base`, `dns_query_buf`, `dns_server_ip`)
- `src/include/encode_domain.asm` — Shared: `encode_domain` (encodes null-terminated domain string at SI into DNS QNAME wire format at DI, CF on error)
- `src/include/parse_ip.asm` — Shared: `parse_ip` (parses dotted-decimal string at SI into 4-byte buffer at DI, CF on error)
- `src/include/print_bcd.asm` — Shared: `print_bcd` (prints AL as two BCD digits)
- `src/include/print_byte_dec.asm` — Shared: `print_byte_dec` (prints AL as 1-3 digit decimal, no leading zeros)
- `src/include/print_dec.asm` — Shared: `print_dec` (prints AL as two zero-padded decimal digits)
- `src/include/print_hex.asm` — Shared: `print_hex` (prints AL as two uppercase hex digits)
- `src/include/print_ip.asm` — Shared: `print_ip` (prints SI as dotted-decimal IPv4 address)
- `src/include/print_mac.asm` — Shared: `print_mac` (prints SI as colon-separated hex MAC address)
- `src/include/str_*.asm` — Shared strings: `DISK_ERROR`, `FILE_NOT_FOUND`
- `src/kernel/ansi.asm` — ANSI escape sequence parser (`put_char`, `put_string`), `serial_char` — included in stage 1 MBR
- `src/kernel/bboeos.asm` — Stage 1 boot code (includes `ansi.asm`), shell loader, `%include` directives, variables, strings
- `src/kernel/io.asm` — `find_file`, `load_file`, `read_sector`, `write_sector`
- `src/kernel/net.asm` — NE2000 NIC driver: `ne2k_probe`, `ne2k_init`, `ne2k_send`, `ne2k_recv`, ARP, IP, ICMP, UDP — included in stage 2
- `src/kernel/syscall.asm` — INT 30h syscall handler, `install_syscalls`
- `src/kernel/system.asm` — `reboot`, `shutdown`
- `src/programs/cat.asm` — Cat program: displays file contents with `\n` to `\r\n` conversion
- `src/programs/chmod.asm` — Chmod program: sets or clears the executable bit with `+x`/`-x`
- `src/programs/cp.asm` — Cp program: copies a file to a new name
- `src/programs/date.asm` — Date program: displays YYYY-MM-DD HH:MM:SS
- `src/programs/dns.asm` — DNS program: resolves arbitrary domains, displays CNAME chains and all A records
- `src/programs/draw.asm` — Draw program: 16-color graphics mode with cursor and background controls
- `src/programs/edit.asm` — Edit program: full-screen text editor with gap buffer, Ctrl+S save, Ctrl+Q quit (gap buffer is segment-0 limited to 20 KB; cannot open `asm.asm` — see "Known limitations" in README.md)
- `src/programs/hello.asm` — Hello program: prints `Hello, world!` (smallest program; useful as a self-host smoke test)
- `src/programs/ls.asm` — Ls program: lists files in the root directory or a subdirectory, marks executables with `*` and directories with `/`
- `src/programs/mkdir.asm` — Mkdir program: creates a subdirectory under root
- `src/programs/mv.asm` — Mv program: renames a file (within the same directory)
- `src/programs/netinit.asm` — Netinit program: probes NE2000 NIC and displays MAC address
- `src/programs/netrecv.asm` — Netrecv program: sends ARP request and hex-dumps reply
- `src/programs/netsend.asm` — Netsend program: sends broadcast ARP request
- `src/programs/ping.asm` — Ping program: sends 4 ICMP echo requests to a user-supplied IP address or hostname (resolves via DNS)
- `src/programs/shell.asm` — Shell program: CLI loop, command dispatch, built-in commands, external program exec, line editor with full editing (insert, delete, cursor movement, kill/yank)
- `src/programs/uptime.asm` — Uptime program: displays HH:MM:SS since boot

## Key Conventions

- Add new commands and functions in **sorted order** (alphabetical).
- Preserve existing comments when editing code.
- Shell command dispatch uses a table of `dw string_ptr, handler_ptr` pairs terminated by `dw 0`. Adding a command requires: a `cmd_*` handler, a table entry, and the command string.
- The shell splits input at the first space: the command name is null-terminated in `BUFFER`, and `[EXEC_ARG]` points to the argument string (or 0 if none). Unknown commands are tried as external programs via `SYS_EXEC`; `SYS_EXIT` reloads the shell.
- Programs are loaded at `PROGRAM_BASE` (`0x0600`). The shell is the first program loaded at boot. Programs `%include` only the granular shared files they need (e.g., `print_bcd.asm`, `str_newline.asm`) at the end of the source.
- Stage 1 functions must fit within the 512-byte MBR.
- When adding the `DIR_SECTOR` constant, stage 2 sector count adjusts automatically.
- **Naming conventions**: Constants and string labels use `UPPER_CASE`. Functions and variables use `lower_case`. Local labels use `.dot_prefix`.
- All output goes through `put_char` (in MBR) which handles ANSI escape sequences for both screen and serial. The shell's line editor uses ANSI sequences (e.g., `ESC[nD` for cursor back, `ESC[nA` for cursor up) via `SYS_IO_PUTC` for all output.

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

Manual testing in QEMU is still the primary workflow — use `-serial stdio` to exercise the serial console and `-machine acpi=off` to test the shutdown failure path.

Automated self-hosting test: `./test_asm.py` boots the OS in QEMU and has the self-hosted assembler reassemble each program in `static/`, then diffs the result byte-for-byte against NASM's output. It drives QEMU via a serial fifo and waits for the `$ ` shell prompt (no fixed sleeps), so each program finishes in a second or two.

- `./test_asm.py` — run the full suite
- `./test_asm.py <name>` — run a single program; on single-program runs the nasm reference, assembled output, and drive image are copied to a persistent temp directory whose path is printed at the end

Filesystem regression tests: `./test_fs.py` boots the OS, runs shell command sequences, and inspects the resulting drive image to verify fs_copy / fs_mkdir / fs_find / fs_create handle large files (>64 KB), sectors past 255, and entries that live in the second directory sector. `./test_fs.py <name>` runs a single test.
