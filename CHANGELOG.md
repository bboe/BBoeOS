# Changelog

All notable changes to BBoeOS are documented in this file. Dates reflect
when changes landed, grouped under the version that was (or will be) current
at the time.

## [Unreleased](https://github.com/bboe/BBoeOS/compare/5156ae9...main)

### [2026-04-18](https://github.com/bboe/BBoeOS/compare/f208689...main)

- asm.c: extract the six driver error reporters (`die_ok`, `die_usage`, `die_error_create`, `die_error_pass1_io`, `die_error_pass1_iter`, `die_error_write_dir`) into pure-C helpers that pop `ds` into `es` (asm_main keeps ES pointed at the symbol-table segment) and then call cc.py's `die()` builtin.  The inline asm drops eight `.error_*:` / `.usage:` labels and their `mov si / mov cx / jmp call_die` chains in favor of direct `jCC die_*` edges; two dead paths (`.error_find_out`, `.error_pass1` — the old asm.asm source had them but nothing jumps there) come out along with their `MESSAGE_ERROR_FIND_OUT` / `MESSAGE_ERROR_PASS1` strings.  Six more `MESSAGE_*` / `MESSAGE_*_LENGTH` pairs (OK, USAGE, ERROR_CREATE, ERROR_PASS1_IO, ERROR_PASS1_ITER, ERROR_WRITE_DIR) shift out of the inline asm tail and into cc.py's `_str_*` section via the C string literals.  Binary shrinks 28 bytes (8306 → 8278): dropping the two dead `.error_find_out` / `.error_pass1` paths (~53 bytes of unused string plus their label blocks) outweighs the bp-frame overhead cc.py adds to each of the six new `die_*` helpers.  Self-host still byte-identical (asm.asm re-assembles to 8253 bytes)
- asm.c: extract the ``source_prefix`` directory-portion walk out of asm_main's inline asm and into a pure-C helper ``compute_source_prefix``.  Reads ``source_name`` (now declared ``char *`` instead of ``int`` — same ``dw 0`` storage, but cc.py can now index it as a byte array) and populates the ``source_prefix[32]`` global with everything up to and including the last ``/`` (empty when there is no slash).  Inline asm shrinks from ~20 lines of nested scan-and-copy to a single ``call compute_source_prefix``; cc.py emits the helper with the usual bp frame and stack-cleanup ret, costing 40 extra bytes over the old fold-everything-inline form.  Byte-identical self-host output preserved (asm.asm still re-assembles to 8253 bytes)
- asm.c: lift the 33 mutable globals (`changed_flag`, `cmp_imm_byte`, `cmp_op1_size`, `current_address`, `equ_space`, `error_flag`, `global_scope`, `include_depth`, `include_path[32]`, `iteration_count`, `jump_index`, `last_symbol_index`, `op1_register`, `op1_size`, `op1_type`, `op1_value`, `op2_register`, `op2_type`, `op2_value`, `org_value`, `output_fd`, `output_name`, `output_position`, `output_total`, `pass`, `source_buffer_position`, `source_buffer_valid`, `source_fd`, `source_name`, `source_prefix[32]`, `symbol_count`, `symbol_set_scope`, `symbol_set_value`) out of the inline asm block and declare them as cc.py file-scope globals at the top of `src/c/asm.c`. `global_scope` keeps its `0xFFFF` initializer; the two 32-byte buffers declare as `char[32]` and emit `times 32 db 0`. The inline asm gains a block of `<name> equ _g_<name>` aliases (forward refs to the cc.py-emitted tail labels resolve cleanly) so every one of the 377 existing handler references works unchanged. `%define LINE_BUFFER` shifts from `program_end` to the new `_program_end` cc.py tail sentinel, and the old NASM `program_end:` label and `db 0` / `dw 0` / `times 32 db 0` declarations come out. Scalars widen from `db` (1 byte) to `dw` (cc.py's global layout) for the 11 former byte-sized fields, so the binary grows 11 bytes (8255 → 8266); every existing byte-granular access targets the low byte of the widened cell, verified safe by grep. All 27 asm self-host tests pass, `test_asm.py asm` still clocks 6.20 s (no TCG regression after PR #75's inline-asm-before-globals reorder), and clang still accepts the new source
- cc.py: emit a `_program_end:` sentinel label at the very end of every output, after globals / strings / arrays. Zero bytes, no-op for programs that don't reference it; byte-identical output verified for all 26 other C programs. The assembler port uses the sentinel as its scratch-buffer anchor (previously `program_end:` was the inline asm's own tail label, which now lands before the cc.py data sections)
- cc.py: file-scope `asm("...")` blocks now emit before globals / strings / array data in `generate()`, instead of after.  When a file-scope asm block contains code (for example, `src/c/asm.c`'s wrapped assembler), interleaving its mutable globals with the same 4K page as hot code triggers QEMU's TCG per-page invalidation on every store — a first attempt at migrating the assembler's 33 plain globals to cc.py-declared globals produced byte-identical output but a 2× runtime slowdown on the self-hosting pass loop.  Moving the inline-asm section ahead of the data sections places cc.py's global storage at the binary's tail, clear of any code page, so the future globals migration can land without a perf hit.  Safe no-op for programs that don't use file-scope asm; `asmesc`'s layout shifts (globals now follow the `asmesc_table` block) but the program still prints `value = 7`.  All 27 self-host byte-identity tests pass

### [2026-04-17](https://github.com/bboe/BBoeOS/compare/9dfd6d8...main)

- cc.py: adjacent string literals concatenate, matching standard C. `"foo" "bar"` folds to `"foobar"` at parse time — `parse_primary` (regular expressions) and `parse_top_level_declaration` (file-scope `asm(...)` argument) both loop on consecutive `STRING` tokens after the first. The headline user is `src/c/asm.c`, which is now regenerated as one `"line\n"` literal per NASM source line concatenated into a single `asm(...)` argument. clang accepts the new form, so `tests/test_cc.py`'s `CC_CHECK_SKIP` gate is dropped and `asm.c` rejoins the syntax-check suite (27 pass, up from 26). The generated binary is byte-identical to the previous phase 1 port (8255 bytes)
- Port the self-hosted assembler from NASM to C — phase 1. `src/c/asm.c` holds the entire contents of `src/asm/asm.asm` inside a single file-scope `asm("...")` block, with the original `main:` renamed to `asm_main:` and cc.py's own `main` bridged by a `jmp asm_main` trampoline. All 27 programs in `tests/test_asm.py` still self-host byte-identically; the generated binary grows by 2 bytes (NASM collapses the trampoline to a 2-byte short jump). `src/asm/asm.asm` moves to `archive/asm.asm`, the empty `src/asm/` directory is removed, `static/asm.asm` repoints at the archived file, and `make_os.sh` drops the now-empty `src/asm/*.asm` loop. Follow-up PRs will extract the driver (main / do_pass / read_line / parse_line / parse_directive / parse_mnemonic), symbol-table operations, emit functions, data tables, and each instruction-handler family into pure C one at a time
- cc.py: `asm("...")` inline-asm escape. Callable as a statement (emits the string verbatim at the current instruction location, after saving any pinned registers in `BUILTIN_CLOBBERS["asm"]`) and at file scope — a top-level `asm("...");` is collected into a dedicated `;; --- inline asm ---` tail section so user labels, `db`/`dw` tables, and whole NASM directives can be planted alongside cc.py's own string / array data. Content is decoded via a new `_decode_string_escapes()` helper (handles `\n` / `\t` / `\r` / `\b` / `\0` / `\\` / `\"` / `\xNN`; unknown escapes pass through for NASM to see). New `InlineAsm` AST node; `parse_top_level_declaration` peeks for `asm (` before the usual `type IDENT` path, and `_register_globals` / `generate` know to skip it. New `src/c/asmesc.c` smoke test plants a byte table at file scope and reads from it via a statement-level `asm(...)` that uses addressing modes cc.py can't otherwise express
- cc.py: `#include "path"` directive. Matches NASM's `%include` semantics — double-quoted form only, path resolved relative to the including file's directory, recursively expanded (included files can themselves `#include`), cycles rejected with a clear error. `#define` entries collected from an included file merge into the outer define pool so a later outer `#define` can still shadow an earlier inner one. Existing `preprocess()` gained `include_base` / `include_stack` keyword arguments and a new leading branch for `#include` handling (the `#define` path is unchanged). New `src/c/inctest.c` + `src/c/inctest.h` smoke test pulls a `#define` and a helper function out of a sibling header. No `#ifndef`/`#endif` yet — include each header only once per translation unit
- cc.py: bitwise operators `|`, `^`, `~`, `<<` (joining the existing `&` and `>>`) and the compound-assignment forms `&=`, `|=`, `^=`, `<<=`, `>>=`. Lexer adds `SHL`/`PIPE`/`CARET`/`TILDE` plus the five compound tokens; parser grows `parse_bitwise_or` / `parse_bitwise_xor` at the standard C precedence (`|` → `^` → `&` between `&&` and the comparison level), and `parse_shift` accepts `<<` alongside `>>`. Unary `~x` desugars to `x ^ 0xFFFF`, which further folds to `not ax` (2 bytes vs. 3 for `xor ax, 0xFFFF`) when the operand is a runtime value. Immediate fast paths (`or ax, imm`, `xor ax, imm`, `shl ax, imm`) mirror the existing `add`/`sub`/`and` shapes, and the load-modify-store peepholes fuse `or`/`xor` into their memory/register destinations the same way they already fuse `add`/`sub`/`and`. NASM constant-expression lowering handles `&`, `|`, `^`, so any all-constant bitwise tree (including named-constant operands) collapses to a single `mov ax, <expr>` at assembly time. `<<` / `>>` stay out of NASM-expression folding — the compile-time case is already folded at AST time and emitting them at NASM level would require register-count shift support (`shl/shr r16, cl`) downstream. New `src/c/bits.c` smoke test
- asm.asm: `resolve_value` now parses `&`, `|`, `^` alongside `+`/`-`/`*`/`/`, so cc.py's all-constant bitwise NASM expressions (e.g., `mov ax, (FLAG_EXECUTE|FLAG_DIRECTORY)`) assemble byte-identically under the self-hosted assembler. `handle_or` and `handle_xor` grew `r16, imm` encodings (prefers `83 /1`/`83 /6` sign-extended-imm8 when the value fits in -128..127, else `81 /1`/`81 /6` imm16; `or ax, imm16` / `xor ax, imm16` still short-circuit through the AX short form where NASM does). Together these make `bits.asm` self-host cleanly — no `SELF_HOST_SKIP` gate needed
- cc.py fix: `peephole_memory_arithmetic` / `peephole_register_arithmetic` fusions no longer strand `ax_local` tracking. `emit_store_local` now inspects the three lines it just emitted via `_peephole_will_strand_ax` and, when the shape matches a pattern the peephole will collapse to an in-place op, drops the tracking that would have let a downstream read of the store's destination skip its reload and pick up stale AX. `peephole()` also runs `peephole_memory_arithmetic` before `peephole_store_reload` so the emitted reload survives the fold. `gdemo.c`'s `bump()` helper was switched to return `counter` as a regression test; edit.c ticks up 10 bytes (2247 → 2257) because five `cursor_line`/`cursor_column` mutation sites now correctly reload the pinned value before the subsequent scroll check (the pre-fix build was comparing whatever AX happened to carry from the preceding statement)
- cc.py: file-scope (global) variables. Declare a scalar (`int counter;`), a sized byte buffer (`char name[64];`), a sized word buffer (`int table[16];`), or an initialized array (`int fib[] = {1, 1, 2, 3, 5};`) at top level. Storage emits as `_g_<name>` at program tail; locals and parameters shadow globals (error on overlap). Global `char` arrays use byte-granular element access, `int` arrays use word-granular. `sizeof(global_array)` folds to a compile-time constant. New `gdemo.c` / `gtable.c` smoke tests exercise scalar mutation from a helper, sized char/int buffers, initialized tables, and sizeof
- asm.asm: `resolve_value` now parses `*` alongside `+`/`-`/`/`, so `times (N)*2 db 0` (cc.py's byte-count expression for uninitialized int globals) assembles byte-identically under the self-hosted assembler
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
