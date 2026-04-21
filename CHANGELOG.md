# Changelog

All notable changes to BBoeOS are documented in this file. Dates reflect
when changes landed, grouped under the version that was (or will be) current
at the time.

## [Unreleased](https://github.com/bboe/BBoeOS/compare/5156ae9...main)

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
- Host-side C compiler (`cc.py`): feature and codegen work in support of the above — file-scope globals, inline `asm(...)` escape, `#include` directive, `regparm(1)` / `carry_return` / `always_inline` / `asm_register` attributes, `uint8_t` type with byte-codegen for byte-typed globals and body locals, `far_read8/16` / `far_write8/16` builtins, new user-callable builtins (`checksum`, `ticks`, `exec`, `reboot`, `shutdown`, `set_exec_arg`), and many peephole / calling-convention improvements

## [0.5.0](https://github.com/bboe/BBoeOS/compare/a0a0980...5156ae9) (2026-04-16)

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

## [0.4.0](https://github.com/bboe/BBoeOS/compare/6ca690e...a0a0980) (2026-03-28)

### [2026-03-28](https://github.com/bboe/BBoeOS/compare/6ca690e...a0a0980)

- General cleanup across the project

## [0.3.0](https://github.com/bboe/BBoeOS/compare/f2af0a6...6ca690e) (2026-03-27)

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

## [0.2.0](https://github.com/bboe/BBoeOS/compare/4ec1217...f2af0a6) (2018-08-12)

### [2018-08-12](https://github.com/bboe/BBoeOS/compare/4ec1217...f2af0a6)

- Two-stage bootloader: load second stage from disk
- Proper backspace handling at the command prompt
- Fix bug where short `g` matched `graphics`

## [0.1.0](https://github.com/bboe/BBoeOS/compare/1e2a995...4ec1217) (2018-07-27)

### [2018-07-29](https://github.com/bboe/BBoeOS/compare/95a9a1a...4ec1217)

- Move input string buffer to beginning of usable address space

### [2018-07-27 – 2018-07-28](https://github.com/bboe/BBoeOS/compare/1e2a995...95a9a1a)

- Add `help`, `clear`, `color`, and `time` commands
- Color output mode with multiple color commands
- Extract code into functions and protect most registers
- Update version string to 0.1.0

## [0.0.3dev](https://github.com/bboe/BBoeOS/compare/21f5d53...1e2a995) (2018-07-26)

### [2018-07-26](https://github.com/bboe/BBoeOS/compare/21f5d53...1e2a995)

- Add simple user-input loop
- Auto-advance cursor row
- Advance cursor on carriage return
- Clear screen on escape
- Echo typed commands
- Detect whether something was entered

## [0.0.2dev](https://github.com/bboe/BBoeOS/compare/99f9894...21f5d53) (2018-07-26)

### [2018-07-26](https://github.com/bboe/BBoeOS/compare/99f9894...21f5d53)

- Add one more line of output
- Improve formatting and assembly readability
- Save bytes through origin specification and row-increment optimization

## [0.0.1dev](https://github.com/bboe/BBoeOS/commit/8180e0f) (2012-08-22)

### [2012-08-22](https://github.com/bboe/BBoeOS/commit/8180e0f)

- Initial BBoeOS code: minimal bootloader with welcome message
