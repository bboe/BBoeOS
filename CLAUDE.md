# BBoeOS

A minimal x86 bootloader and OS written in NASM assembly, running in 16-bit real mode.

## Build and Run

```sh
./make_os.sh                                           # assemble and create floppy image
qemu-system-i386 -drive file=floppy.img,format=raw     # run in QEMU
qemu-system-i386 -drive file=floppy.img,format=raw -serial stdio  # with serial console
```

Requires `nasm` (`brew install nasm`).

## Architecture

Two-stage bootloader in flat binary format (`nasm -f bin`), loaded at `org 7C00h`.

- **Stage 1 (MBR, 512 bytes)**: Boot init, loads stage 2 via INT 13h, displays date/time, saves boot tick count. Contains shared functions: `clear_screen`, `print_string`, `print_char`, `print_bcd`, `print_date`, `print_time`, `serial_char`.
- **Stage 2**: CLI loop, command dispatch, line editor, graphics mode, filesystem, syscall interface (INT 30h).
- **Input buffer** at linear address `0x500`, max 256 characters.
- **Disk buffer** at `0x9000` for filesystem reads.
- **Stack** at `0050h:7700h` (linear `0x7C00`, grows downward).
- Stage 2 sector count is derived from `dir_sector` via `%assign stage2_sectors (dir_sector - 2)`.

### Filesystem

Trivial read-only filesystem on the floppy disk:

- **Sector 1**: MBR (stage 1)
- **Sectors 2 to dir_sector-1**: Stage 2
- **Sector dir_sector (6)**: File table / directory (32 entries x 16 bytes)
- **Sectors dir_sector+1 onward**: File data

Directory entry format (16 bytes): 12 bytes filename (null-terminated), 2 bytes start sector, 2 bytes file size. Files are limited to one sector (512 bytes).

Use `./add_file.sh floppy.img <file>` to add files to the image after building.

### Serial Console

All output is mirrored to COM1 (`print_char` writes to both screen and serial). `serial_char` writes to COM1 only (used for input echo and cursor movement in `readline.asm`). Input is polled from both keyboard (INT 16h) and COM1 simultaneously. Serial terminals send `0x7F` (DEL) for backspace, which is handled alongside `0x08`.

### Syscall Interface (INT 30h)

Programs loaded from the filesystem can use INT 30h for OS services:

| AH    | Name         | Description                                          |
|-------|--------------|------------------------------------------------------|
| 00h   | fs_find      | Find file, SI = filename, BX = entry ptr, CF on err  |
| 01h   | fs_read      | Read sector AL into disk_buffer, CF on error          |
| 10h   | io_getc      | Read one char, AL = char, AH = scan code              |
| 11h   | io_gets      | Read line into buffer, CX = length                    |
| 12h   | io_putc      | Print char in AL (screen + serial)                    |
| 13h   | io_puts      | Print string at SI (screen + serial)                  |
| 20h   | scr_clear    | Clear screen                                          |
| F0h   | sys_exit     | Return to shell                                       |
| F1h   | sys_reboot   | Reboot                                                |
| F2h   | sys_shutdown  | Shutdown                                              |

## File Structure

- `src/kernel/bboeos.asm` — Stage 1 boot code, CLI loop, `%include` directives, variables, command table, strings
- `src/kernel/readline.asm` — `cursor_back_n`, `read_line` with full line editing (insert, delete, cursor movement, kill/yank)
- `src/kernel/commands.asm` — Command handlers (`handle_*`), `cat_file`, `process_command`, `print_help`, `print_uptime`, `print_dec_byte`
- `src/kernel/io.asm` — `find_file`, `read_sector`, `visual_bell`
- `src/kernel/syscall.asm` — INT 30h syscall handler, `install_syscalls`
- `src/kernel/system.asm` — `graphics` mode, `reboot`, `shutdown`
- `add_file.sh` — Host-side script to add files to the floppy image filesystem
- `make_os.sh` — Build script (assembles kernel and creates floppy image)

## Key Conventions

- Add new commands and functions in **sorted order** (alphabetical).
- Preserve existing comments when editing code.
- Command dispatch uses a table of `dw string_ptr, handler_ptr` pairs terminated by `dw 0`. Adding a command requires: a `handle_*` function, a table entry, and the command string.
- Commands with arguments (like `cat`) use prefix matching in `process_command` before the table dispatch.
- Stage 1 functions must fit within the 512-byte MBR.
- When adding the `dir_sector` constant, stage 2 sector count adjusts automatically.
- Screen-only operations (cursor repositioning, insert/delete redraws) use direct `int 10h` calls with parallel `serial_char` calls for serial console sync. Do not route screen redraw loops through `print_char` as that would produce duplicate serial output.

## 16-bit Real Mode Constraints

- Only BX, BP, SI, DI are valid base/index registers in memory operands (not AX, CX, DX, SP).
- BIOS interrupts: INT 10h (video), INT 13h (disk), INT 16h (keyboard), INT 1Ah (RTC/timer).
- INT 10h AH=03h clobbers CX (returns cursor scanline shape) — save any value in CX before calling.
- `mul` clobbers DX (result in DX:AX) — save DX if needed.
- 32-bit registers (EAX, ECX, EDX) are usable with operand-size prefix (386+).
- Teletype backspace (`\b` via INT 10h AH=0Eh) does not wrap across screen lines. Use `cursor_back_n` (INT 10h AH=02h/03h) for proper screen cursor positioning.

## Testing

No automated tests. Test manually in QEMU after each change. Use `-serial stdio` to test serial console. Use `-machine acpi=off` to test shutdown failure path.
