# BBoeOS

A minimal x86 bootloader and OS written in NASM assembly, running in 16-bit real mode.

## Build and Run

```sh
./make_os.sh                                           # assemble and create floppy image
qemu-system-i386 -drive file=floppy.img,format=raw     # run in QEMU
```

Requires `nasm` (`brew install nasm`).

## Architecture

Two-stage bootloader in flat binary format (`nasm -f bin`), loaded at `org 7C00h`.

- **Stage 1 (MBR, 512 bytes)**: Boot init, loads stage 2 (3 sectors) via INT 13h, displays date/time, saves boot tick count. Contains shared functions: `clear_screen`, `print_string`, `print_char`, `print_bcd`, `print_date`, `print_time`.
- **Stage 2**: CLI loop, command dispatch, line editor, graphics mode.
- **Input buffer** at linear address `0x500`, max 256 characters.
- **Stack** at `0050h:7700h` (linear `0x7C00`, grows downward).

## File Structure

- `bboeos.asm` — Stage 1 boot code, CLI loop, `%include` directives, variables, command table, strings
- `readline.asm` — `cursor_back_n`, `read_line` with full line editing (insert, delete, cursor movement, kill/yank)
- `commands.asm` — Command handlers (`handle_*`), `process_command`, `print_help`, `print_uptime`, `print_dec_byte`
- `io.asm` — `visual_bell` (red border flash)
- `system.asm` — `graphics` mode, `reboot`, `shutdown`

## Key Conventions

- Add new commands and functions in **sorted order** (alphabetical).
- Preserve existing comments when editing code.
- Command dispatch uses a table of `dw string_ptr, handler_ptr` pairs terminated by `dw 0`. Adding a command requires: a `handle_*` function, a table entry, and the command string.
- Stage 1 functions must fit within the 512-byte MBR. Stage 2 currently uses 3 sectors (1536 bytes max).
- When stage 2 grows past its sector allocation, update the sector count in both `mov ax, 02XXh` and `cmp al, X` in stage 1.

## 16-bit Real Mode Constraints

- Only BX, BP, SI, DI are valid base/index registers in memory operands (not AX, CX, DX, SP).
- BIOS interrupts: INT 10h (video), INT 13h (disk), INT 16h (keyboard), INT 1Ah (RTC/timer).
- INT 10h AH=03h clobbers CX (returns cursor scanline shape) — save any value in CX before calling.
- `mul` clobbers DX (result in DX:AX) — save DX if needed.
- 32-bit registers (EAX, ECX, EDX) are usable with operand-size prefix (386+).
- Teletype backspace (`\b` via INT 10h AH=0Eh) does not wrap across screen lines. Use `cursor_back_n` (INT 10h AH=02h/03h) for proper cursor positioning.

## Testing

No automated tests. Test manually in QEMU after each change. Use `-machine acpi=off` to test shutdown failure path.
