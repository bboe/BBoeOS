# Changelog

All notable changes to BBoeOS are documented in this file. Dates reflect
when changes landed, grouped under the version that was (or will be) current
at the time.

## [Unreleased](https://github.com/bboe/BBoeOS/compare/5156ae9...main)

### [2026-04-17](https://github.com/bboe/BBoeOS/compare/9dfd6d8...main)

- Convert `edit` from assembly to C; retire `src/asm/edit.asm`
- Add `EDIT_BUFFER_BASE`, `EDIT_BUFFER_SIZE`, `EDIT_KILL_BUFFER`, `EDIT_KILL_BUFFER_SIZE` constants (fixed addresses replace the former float-on-`program_end` gap buffer)
- cc.py fix: `peephole_double_jump` now keeps `.L1:` when other jumps still target it; deleting it stranded the top-of-loop `jCC` that guards `while (cond)` with `break`
- cc.py fix: `generate_if` restores AX tracking to the post-condition state (not the pre-if state) on the exit-body fall-through path, so a condition that clobbers AX (e.g. `fstat(fd) & FLAG_DIRECTORY`) no longer leaves stale `ax_local` tracking pointing at the pre-if variable
- cc.py fix: `builtin_fstat` clears AX tracking after the syscall (the syscall overwrites AX but tracking still pointed at the argument local)
- cc.py codegen: constant-base `Index` / `IndexAssign` fold into `[CONST ± disp + bx]` addressing, so `buf[gap_start - 1]` compiles to `mov bx, [_l_gap_start] / mov al, [EDIT_BUFFER_BASE-1+bx]` instead of the old `mov bx, CONST / push / load index / pop / add / load` sequence. Shrinks edit by 173 bytes, shell by 47, ls/echo/netrecv by 2–6 each
- cc.py codegen: auto-pin by usage count (body locals before parameters, tiebroken by declaration order). Combined with the next entry, pins the most-used locals onto the cheapest-to-save registers
- cc.py codegen: pin aggressively and wrap each call with `push`/`pop` for any caller pin the callee clobbers. Pinning is gated by a cost model that only keeps a pin when the local's reference count strictly exceeds the matched register's clobber count. Shrinks edit by 196 bytes, shell by 21, arp/cat/cp/draw/ls by 2–14 each
- cc.py codegen: register calling convention for user functions whose every call site passes only simple (Int/String/Var) arguments. Pinned params arrive in their assigned registers instead of being pushed and reloaded, with topological ordering (and an AX spill for cycles) to resolve source/target conflicts. Shrinks shell by 35, ping/edit/dns by 11–14 each
- cc.py codegen: pack the auto-pin pool further. Main gets BP as a fifth slot (zero call-clobber cost since every callee preserves it, gated against BP's 2-byte-per-subscript penalty in real mode). Single-assignment "expression-temporary" vars whose only uses are left-of-cmp against a constant are dropped from the pool — their value already lives in AX through `emit_comparison`'s fast path, so pinning only adds a redundant `mov pin, ax`. Leaf-only `Var ± Int/Var` BinOps qualify as "simple args" so user functions like `buffer_character_at` keep the register calling convention even when a caller passes `offset + i`
- cc.py codegen: hoist memory-resident locals into AX at the top of `if (var op K) … else if …` dispatch chains so subsequent comparisons collapse from `cmp word [mem], imm` to `cmp ax, imm`; fold all-constant BinOp trees into a single assembler-time expression (so `O_WRONLY + O_CREAT + O_TRUNC` is one `mov al, <expr>` instead of a runtime push/pop chain); and swap ≥3-pin push/pop dances for `pusha`/`popa` around statement-level calls whose return value is discarded
- cc.py peephole: four new patterns — extend store/reload elimination past AX-preserving instructions (`cmp`/`test`/`Jcc`, non-AX push/pop), fold `mov ax, <reg> / cmp ax, X / jCC` into `cmp <reg>, X / jCC`, fuse `xor reg, reg / push reg` into a single `push 0`, and use `add si, [mem]` in place of the push-ax/compute/pop-ax indexing dance
- Self-hosted assembler: add `pusha` and `popa` mnemonics so cc.py-emitted programs can be re-assembled by the in-OS assembler
- Together these shrink edit by 130 bytes (the gap vs `archive/edit.asm` drops from +400 to +270), shell by 57, draw by 24, ping by 18, dns by 16, cp by 10, netrecv by 5, arp by 4, and a handful of 2-byte wins across cat/echo/loop/ls

### [2026-04-16](https://github.com/bboe/BBoeOS/compare/5156ae9...main)

- Convert the shell, `dns`, and `ping` from assembly to C; archive each `.asm` as a same-layout reference
- Add protocol argument to `net_open` (Linux-style `(type, protocol)` API)
- Add ICMP sockets via `(SOCK_DGRAM, IPPROTO_ICMP)`; build ICMP echo requests in userspace
- Remove `SYS_NET_ARP` and `SYS_NET_PING` syscalls (both protocols now live in userspace); collapse NET syscall numbers
- Convert the `dns_query` helper to socket-based UDP syscalls
- Add cc.py builtins: `checksum`, `ticks`, `exec`, `reboot`, `shutdown`, `set_exec_arg`
- Extend cc.py language: user-defined function calls with return values, indexed assignment (`name[expr] = expr`), `\x` hex escapes in character literals, `>>` right-shift, `continue`, `const` (accepted and discarded)
- cc.py codegen: pin non-main parameters and body locals to registers, skip push/pop bx around simple subscript indices, emit inc/dec for ±1 and fuse via peephole, use memory operands directly in add/sub
- cc.py peephole: rewrite `x / 2^N` as `x >> N`, shortcut `local >> 8` to a direct high-byte load
- cc.py fixes: several codegen correctness issues exposed by dns.c, strip `+N` offset when extracting `_l_` label from stores, line-aware diagnostics on compile errors
- Sort cc.py module-level constants alphabetically
- Extend the self-hosted assembler with `lodsw` / `adc` / `not` and shorter encodings
- Add regression test that `archive/*.asm` still assembles and matches the size table in `archive/README.md`
- Drop stale UDP syscall rows from the CLAUDE.md syscall table

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
