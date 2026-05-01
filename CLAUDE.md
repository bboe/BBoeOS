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

Two flat binaries (`nasm -f bin`) concatenated on disk: `boot.bin` (`src/arch/x86/boot/boot.asm`, MBR + post-MBR real-mode bootstrap + 32-bit `early_pe_entry` that builds the boot PD and enables paging) and `kernel.bin` (`src/arch/x86/kernel.asm`, post-paging high-half kernel `org`'d at `0xFF820000`).  After `early_pe_entry` far-jumps to `high_entry`, the kernel sets up its IDT/GDT/stack, brings up the bitmap allocator + kmap window + kernel idle PD, then falls into `protected_mode_entry` (`src/arch/x86/entry.asm`) which does driver inits and falls into `shell_reload`.  Programs run ring-3 in per-program PDs at `PROGRAM_BASE` (`0x08048000`) with `ESP = USER_STACK_TOP = KERNEL_VIRT_BASE = 0xFF800000`; the kernel direct map at PDE 1022 covers phys `0..0x3FFFFF` and the kmap window at PDE 1023 reaches anything above that.

See [`docs/architecture.md`](docs/architecture.md) for the full deep-dive on each phase: boot path, post-flip bring-up, ring-3 userland, kernel-side runtime data, paging / address-space lifecycle, and build-time derivation.

### Static memory map

The kernel-side fixed-physical region table and the per-program user-virt layout live in [`docs/memory_map.md`](docs/memory_map.md).  Update that table when adding a new fixed-phys region so newcomers can find every slot in one place.

The build script asserts that `KERNEL_RESERVED_BASE + 0x23000 < 0xA0000` (worst-case stack + boot PD + first kernel PT + 128 KB bitmap at the FRAME_PHYSICAL_LIMIT cap) so the kernel-side regions never cross the VGA aperture under any RAM size.

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

Programs use INT 30h for OS services.  Numbers and argument-register conventions live in [`docs/syscalls.md`](docs/syscalls.md); symbolic names (`SYS_*`) are in `src/include/constants.asm`.

When removing a syscall, collapse the remaining numbers in its group in
the same commit (e.g. removing `SYS_NET_ARP` (20h) shifts every later
`SYS_NET_*` down by one). The group-high-nibble (2h = net, 3h = rtc, …)
is the only stable contract with userspace; within a group, expect
numbers to compact.  Programs reference `SYS_<NAME>` symbolically so
renumbering is source-compatible — just rebuild.

## File Structure

See [`docs/file_structure.md`](docs/file_structure.md) for the file-by-file breakdown — host-side build tooling, shared includes, boot/kernel core, memory management, syscalls, drivers, FS / VFS, networking, userland programs, and smoke tests.

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

Update `docs/CHANGELOG.md` with new entries as features land (the file at the repo root is a stub that points at it).  Group entries by date under the Unreleased section.  After a batch of significant improvements, bump the version in `src/arch/x86/entry.asm` (the `welcome_msg` string emitted by `protected_mode_entry`) and move the Unreleased entries under a new version header with updated comparison links.

## Testing

Manual testing in QEMU is still the primary workflow — use `-serial stdio` to exercise the serial console and `-machine acpi=off` to test the shutdown failure path.

Automated self-hosting test: `tests/test_asm.py` boots the OS in QEMU and has the self-hosted assembler reassemble each program in `static/`, then diffs the result byte-for-byte against NASM's output. It drives QEMU via a serial fifo and waits for the `$ ` shell prompt (no fixed sleeps), so each program finishes in a second or two.

- `tests/test_asm.py` — run the full suite
- `tests/test_asm.py <name>` — run a single program; on single-program runs the nasm reference, assembled output, and drive image are copied to a persistent temp directory whose path is printed at the end

Filesystem regression tests: `tests/test_bboefs.py` boots the OS, runs shell command sequences, and inspects the resulting drive image to verify fs_copy / fs_mkdir / fs_find / fs_create handle large files (>64 KB), sectors past 255, and entries that live in the second directory sector. `tests/test_bboefs.py <name>` runs a single test.

Program runtime tests: `tests/test_programs.py` boots the OS in QEMU per test, runs a representative shell command for each entry, and checks output against a regex.  `--filesystem bbfs` (default) covers user / kernel / cc.py paths; `--filesystem ext2` adds an `e2fsck -f -n` integrity check after each test, runs the ext2-specific stress tests (doubly-indirect blocks, multi-sector directory walks, rename-across-parents, etc.), and finishes with a 2 KB-block-size matrix re-run of the FS-touching tests.  `--slow` opts in to the large-file and doubly-indirect ext2 tests.
