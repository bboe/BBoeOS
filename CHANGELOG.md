# Changelog

All notable changes to BBoeOS are documented in this file. Dates reflect
when changes landed, grouped under the version that was (or will be) current
at the time.

## [Unreleased](https://github.com/bboe/BBoeOS/compare/5156ae9...main)

### [2026-04-18](https://github.com/bboe/BBoeOS/compare/f208689...main)

- cc.py: ``__attribute__((regparm(1)))`` calling convention for user functions.  The caller loads arg 0 into AX before ``call fn`` (no push / stack cleanup for that arg); the callee prologue allocates a local stack slot for the parameter and spills AX into it immediately after ``push bp / mov bp, sp / sub sp, N``, so the body reads the parameter through the normal local-address path.  Params 1..N keep the standard cdecl layout shifted down by one slot (caller didn't push arg 0).  Mutually exclusive with the existing register-convention auto-pin path in the MVP — fastcall funcs aren't promoted to ``register_convention_functions`` and their param 0 isn't eligible for auto-pin.  Attribute is accepted either before the return type (silent on clang) or after the parameter list (GCC-compat warning; returncode still 0).  Unblocks porting the asm.c inline-asm bodies that depend on the AL-first-arg ABI — ``emit_byte_al`` / ``encode_rel8_jump`` / ``reg_to_rm`` / ``symbol_entry_address`` / ``make_modrm_reg_reg`` can all migrate to pure-C bodies with proper parameters once their inline-asm callers re-point at the new entry.  New ``src/c/fctest.c`` smoke test exercises constant / local / computed-expression / nested-call argument forms
- asm.c: collapse the ~20 archaeological ``<name> lives in a cc.py-emitted C function near the top of src/c/asm.c`` pointer comments left over from the incremental port.  Every function is in C now, so the scattered pointers were noise every reader had to skip past; replaced with a single header paragraph that explains what's still in the trailing asm block (syscall, data tables, keyword strings, equ aliases) and why.  204 lines of comment noise out
- asm.c: port the ``abort_unknown`` trampoline to a pure-C naked-asm function.  Two-instruction bridge from handler ``jmp abort_unknown`` sites to the printf-and-exit path in abort_unknown_impl — stashes SI into error_word, jumps to the C reporter.  cc.py's bp-frame elide keeps the cost identical to the retired asm version; the ``ret`` cc.py appends is dead code since the ``jmp`` always takes.  ``syscall`` stays inline — it's a reserved libc symbol and clang's syntax check rejects a user redefinition, and every ``call syscall`` site in the other inline-asm bodies would need a rename to move it to C
- cc.py: elide the ``push bp / mov bp, sp`` prologue and the ``pop bp`` epilogue when a function's body is a single ``asm("...")`` statement with no parameters.  cc.py's codegen never reads BP inside an inline-asm block, the body has no C locals, and no parameters means no [bp+N] access either, so the bp frame is dead weight.  Inside a function body, ``asm(...)`` parses as ``Call("asm", [String])`` (InlineAsm nodes only apply to file-scope directives), so the detection checks for that shape.  ``elide_frame`` already skips the prologue path; the epilogue gets a dedicated ``ret``-only branch ahead of the normal ``mov sp, bp / pop bp / ret``.  asm.c drops 324 bytes (8830 → 8506) and the self-host run drops from ~13s to ~9s — back to the pre-port baseline.  ASM_SELF_HOST_TIMEOUT rolls back from 32s to 16s
- asm.c: extract the parse_* family (``parse_db``, ``parse_directive``, ``parse_line``, ``parse_mnemonic``, ``parse_number``, ``parse_operand``, ``parse_register``) into cc.py-emitted C functions with inline-asm bodies.  parse_line is the top-level dispatcher called by do_pass for every LINE_BUFFER-read line; parse_directive handles ``%assign`` / ``%define`` / ``%include`` / ``org`` / ``times N db ...`` / ``db`` / ``dw`` / ``dd`` and falls through to parse_mnemonic; parse_mnemonic is the linear table scan over mnemonic_table with a handle_unknown_word fallback for bare labels.  parse_operand / parse_register / parse_number / parse_db are the operand / register / numeric / data-literal leaves.  The STR_* match keywords and the mnemonic / register tables stay in the trailing file-scope asm block (NASM data constructs cc.py doesn't emit)
- asm.c: extract ``peek_label_target`` / ``resolve_label`` / ``resolve_value`` into cc.py-emitted C functions with inline-asm bodies.  peek_label_target looks up a bare identifier without advancing SI (preserves every register via the initial push/pop chain); resolve_label dispatches on pass (pass 1 skips past the identifier and returns current_address as a placeholder, pass 2 calls resolve_value); resolve_value is the recursive expression evaluator with left-to-right ``+ - * / & | ^`` chaining, parens, ``$`` / ``'c'`` / ``` `c` ``` literals, and symbol lookup.  cc.py's bp frame makes the recursion safe — each frame pushes its own BP and the BX/CX/DI save triplet stays stack-balanced
- asm.c: extract the symbol_* family (``symbol_add``, ``symbol_add_constant``, ``symbol_lookup``, ``symbol_set``) into cc.py-emitted C functions with inline-asm bodies.  symbol_add appends a new entry (name + value + type + scope) and jumps to die_symbol_overflow on full; symbol_add_constant delegates to symbol_set then flips the type byte to 1 so pass 1 can distinguish ``%assign`` entries from labels; symbol_lookup is the linear scan with a pass-1 pass-through that returns AX = 0 / CF clear for missing forward references; symbol_set updates in place when found, else falls through to symbol_add — stashing AX / BX in symbol_set_value / symbol_set_scope globals so the intervening symbol_lookup call doesn't lose them
- tests: bump ASM_SELF_HOST_TIMEOUT from 16s to 32s.  The bp-framed resolve_value / symbol_lookup are called from the hottest expression paths in the assembler, so the per-call overhead adds up to ~3s on the self-host run.  A 32s cap stays within the same doubling cadence (powers of 2) and leaves plenty of headroom for QEMU / host noise
- asm.c: extract handle_mov into a cc.py-emitted C function with an inline-asm body.  Ten operand shapes: mov es, r16 (8E /r); reg × reg (88/89 modrm); reg × imm (B0+r / B8+r short); reg × [disp16] (A0/A1 short AL/AX or 8A/8B + modrm rm=110); reg × [reg+disp] (8A/8B + modrm with mod 0/01/10 by disp size); [disp16] × imm (C6/C7 + modrm); [disp16] × reg (A2/A3 short AL/AX or 88/89 + modrm); [reg+disp] × imm / [reg+disp] × reg share the same disp-size modrm logic.  Any other op1 × op2 combination falls through to ``.hmv_done`` as a no-op, matching the retired asm
- asm.c: extract ``encode_rel8_jump`` into a cc.py-emitted C function with an inline-asm body.  The iterative-shrinking jump-size selector called by every handle_jXX / handle_loop / handle_jmp; all callers already use ``call encode_rel8_jump`` so the label-to-function move is transparent.  Terminal ``jmp emit_byte_al`` / ``jmp emit_word_ax`` tail calls become ``call`` + ``jmp .erj_end`` so cc.py's bp frame closes cleanly.  The ``.erj_shrink_forward`` trick that peeks the saved-AX opcode via an inner ``push bp / cmp byte [bp+2] / pop bp`` still works — cc.py's outer frame lives above the saved AX, so [bp+2] relative to the inner push still references the opcode byte
- asm.c: extract ``mem_op_reg_emit`` into a cc.py-emitted C function with an inline-asm body.  Shared helper for the ``<op> [disp16], r16`` form used by handle_add / handle_and / handle_sub; all three already ``call`` into it so converting the label into a cc.py-emitted function matches the existing call convention.  Retired the tail-``jmp emit_word_ax`` in favor of ``call`` so the cc.py epilogue runs
- asm.c: extract handle_cmp into a cc.py-emitted C function with an inline-asm body.  The /r=7 companion of handle_sub / handle_xor — covers r-r (38/39 modrm), r-imm (3C/3D short AL/AX, 80/81/83 /7 general, sign-extended imm8 preferred when it fits), r-[mem] (3A/3B with disp16 or reg+disp), mem-imm (byte/word split with cmp_imm_byte tracking whether the imm is imm8 or imm16), and [disp16]-imm (80/81/83 /7 with mod=00 rm=110).  Every success path jumps to ``.hcm_done:`` so cc.py's bp frame closes cleanly
- asm.c: extract handle_add into a cc.py-emitted C function with an inline-asm body.  Four operand shapes: ``[disp16], r16`` via mem_op_reg_emit (call + ``.end:``), reg/reg (00/01 modrm), reg/[mem] (02/03 with direct disp16 or reg+disp8), reg/imm with the AX-specific 05 imm16 short form, the 04 imm8 AL short form, and 83 /0 sign-extended-imm8 / 81 /0 imm16 / 80 /0 imm8 general forms.  /r field is 0 so the register-mode modrm constant is 0xC0
- asm.c: extract handle_sub into a cc.py-emitted C function with an inline-asm body.  Four operand shapes: ``[disp16], r16`` via mem_op_reg_emit, the dedicated ``word [disp16], imm16`` path (81 /5 iw) the TCP-checksum fold uses, reg/reg (28/29 modrm), and reg/imm with /r=5 (0xE8) — 83 sign-extended imm8 when it fits, 81 iw otherwise, 2C short AL / 80 general ib for r8.  ``[disp16]`` and ``word [disp16]`` structure mismatches jump to abort_unknown; every success path jumps to ``.hsu_end:``
- asm.c: extract handle_unknown_word into a cc.py-emitted C function with an inline-asm body.  The parse_mnemonic fallback that treats an unrecognized first word as a bare label (NASM's colon-less syntax, e.g. ``USAGE db ...``).  Walks SI past the alphanumeric span, null-terminates in place, adds the symbol on pass 1 (or looks it up on pass 2) with the ``.``-prefix local-scope distinction, then reinvokes parse_directive on the remainder
- asm.c: extract handle_and / handle_or into cc.py-emitted C functions with inline-asm bodies.  Both follow the handle_xor template with distinct /r constants: and=4 (0xE0), or=1 (0xC8).  handle_and also handles the ``[disp16], reg`` memory-destination form by ``call``ing mem_op_reg_emit (retired asm ``jmp``'d there, but a tail-jmp out of a C body would strand the pushed bp).  handle_or adds the reg-[disp16] source form (0A/0B modrm, mod=00 rm=110) that handle_xor doesn't need
- asm.c: extract handle_test / handle_xchg / handle_xor into cc.py-emitted C functions with inline-asm bodies.  handle_test covers the three forms self-host needs (r-r, r-imm with the A8/A9 short AX/AL encoding, byte[mem]-imm8 with disp16 / disp8 / [reg] dispatch via reg_to_rm).  handle_xchg uses the 90h+reg short form when one operand is AX and the 86/87 modrm form otherwise (with the NASM first-in-reg / second-in-rm swap).  handle_xor mirrors the handle_or shape — 30/31 modrm for r-r, 34 short AL / 83 /6 ib / 81 /6 iw / 80 /6 ib for the immediate forms
- asm.c: extract handle_call / handle_dec / handle_div / handle_inc into cc.py-emitted C functions with inline-asm bodies.  handle_call emits the direct ``E8 rel16`` form or the indirect ``FF /2 [reg+disp8]`` form (any other addressing still ``jmp``s to abort_unknown).  handle_inc and handle_dec share layout — register form uses the ``40+reg`` / ``48+reg`` short encoding for r16 and the ``FE /0`` / ``FE /1`` modrm encoding for r8, while memory form picks FE/FF by size and dispatches on the parse_operand op2 type (direct disp16 vs reg / reg+disp); the two differ only in the /r-field constants (0x00 vs 0x08 for reg form, 0x06 vs 0x0E for the mod=00 disp16 modrm).  handle_div mirrors the handle_mul / neg / not shape with /r=6 (``0xF0`` modrm).  Each body falls through to cc.py's ``pop bp / ret`` epilogue via a terminal ``.end:`` label, replacing the retired inline-asm ``ret`` / ``jmp emit_byte_al`` tail forms
- asm.c: extract handle_movzx / handle_pop / handle_push into cc.py-emitted C functions with inline-asm bodies.  handle_movzx emits the ``0F B6`` prefix then dispatches on op2 (register, register+disp memory) for the only forms the self-host uses.  handle_pop / handle_push peek the first two bytes for the ``ds`` / ``es`` segment-register special case (1F/07 / 1E/06), then handle register (58/50 + reg) or — push only — imm16 with the sign-extended ``6A imm8`` short form when the value fits.  Each body uses a terminal ``.end:`` label so the non-abort exit paths close cc.py's bp frame cleanly
- asm.c: extract handle_sbb / handle_shl / handle_shr into cc.py-emitted C functions with inline-asm bodies.  handle_sbb implements the one ``sbb word [disp16], imm8`` form the TCP checksum fold relies on (match_word gate on ``word``, abort_unknown on shape mismatch).  handle_shl / handle_shr share layout — parse a register and shift count, pick the ``D0`` / ``D1`` short form when count == 1 or the ``C0`` / ``C1`` imm8 form otherwise, OR the /r-field constant (shl=4, shr=5) into the register-mode modrm byte.  Each success path jumps past the abort_unknown tail to a terminal ``.end:`` label so cc.py's bp frame closes cleanly
- asm.c: extract handle_adc / handle_mul / handle_neg / handle_not into cc.py-emitted C functions with inline-asm bodies.  handle_adc is the single-form ``adc r16, imm8`` (``83 /2 ib``) used by the checksum fold idiom.  handle_mul / handle_neg / handle_not share structure — parse one register operand, pick F6 (byte) or F7 (word), OR the /r-field constant (mul=4, neg=3, not=2) into a register-mode modrm byte (``C0 | (n<<3) | rm``).  Each body calls ``emit_byte_al`` for the final byte so cc.py's ``pop bp / ret`` epilogue runs (tail-jumping would bypass it and strand the pushed bp).  A stray duplicate-header comment left over from the original archive is cleaned up on the way through
- asm.c: extract handle_int, the conditional-jump family (handle_ja / jb / jbe / jg / jge / jl / jle / jnc / jne / jns / jz), handle_jmp, and handle_loop into cc.py-emitted C functions with inline-asm bodies.  Each conditional handler loads its rel8 opcode into AL and ``call``s into ``encode_rel8_jump`` (still in the file-scope asm block); handle_jmp layers the optional ``short`` keyword peel-off through match_word before reaching the shared path.  handle_int shuttles the immediate across ``emit_byte_al`` via push/pop AX.  The mnemonic table's ``STR_JC, handle_jc`` alias retires with the duplicate handle_jc label — STR_JC now points at handle_jb, matching the existing STR_JAE→handle_jnc / STR_JE→handle_jz / STR_JNZ→handle_jne aliases
- asm.c: extract handle_rep / handle_repne into cc.py-emitted C functions with inline-asm bodies.  Each emits its prefix byte (F3 or F2) then calls ``parse_mnemonic`` to dispatch the following string instruction's handler; cc.py's bp frame wraps the call path cleanly (the retired asm version's explicit ``ret`` after the recurse becomes the epilogue's implicit one)
- asm.c: extract the 14 zero-operand handlers (handle_aam / clc / cld / lodsb / lodsw / movsb / movsw / popa / pusha / ret / scasb / stc / stosb / stosw) into cc.py-emitted C functions with inline-asm bodies.  Each was a 2-3 line ``mov al, OPCODE / jmp emit_byte_al`` block in the file-scope asm; the C versions call emit_byte_al instead of tail-jumping so cc.py's ``pop bp / ret`` epilogue closes the frame without stranding the pushed bp.  mnemonic_table still references each handler by its C name.  Net effect is ~70 bytes of growth in the generated asm.asm
- asm.c: extract ``match_word`` into a cc.py-emitted C helper with an inline-asm body.  The case-insensitive SI-vs-DI word compare with word-boundary validation keeps its SI-in/out / DI-in / CF-out ABI and preserves AX / BX.  Both the success and failure paths jump to a shared ``.mw_end`` epilogue that pops BX / AX before cc.py's ``pop bp / ret`` closes the frame; POP and RET don't touch FLAGS, so the ``clc`` / ``stc`` the branch just set reaches the caller intact.  All ``call match_word`` sites in parse_operand (``byte`` / ``word`` prefixes) and parse_mnemonic (directive lookup) continue to resolve against the bare function label.  Binary grows 3 bytes (8348 → 8351) — bp frame minus the shared epilogue's savings
- asm.c: extract ``symbol_entry_address`` into a cc.py-emitted C helper with an inline-asm body.  Input AX (symbol index) times SYMBOL_ENTRY (36) lands in DI; BX is pushed / popped around the ``mul bx`` so callers preserve it through the call.  The ``SYMBOL_ENTRY`` literal substitutes in at the C level because the ``%assign`` is emitted after each C function.  All four ``call symbol_entry_address`` sites in symbol_add / symbol_add_constant / symbol_set continue to resolve against the bare function label.  Binary grows 4 bytes (8344 → 8348) for the bp frame
- asm.c: extract ``emit_byte_al`` and ``emit_word_ax`` into cc.py-emitted C helpers with inline-asm bodies.  ``emit_byte_al`` keeps its AL-in ABI and the pass-1-is-counting / pass-2-buffers branch, still saving BX only inside the pass-2 branch around the buffer-pointer math; ``OUTPUT_BUFFER`` is spelled inline as ``_program_end + 256`` because the ``%define`` lives in the file-scope asm block that cc.py emits after each C function.  ``emit_word_ax`` changes its trailing ``jmp emit_byte_al`` tail call to a regular ``call`` so cc.py's ``pop bp / ret`` epilogue can close the bp frame.  All ~500 ``call emit_byte_al`` / ``jmp emit_byte_al`` sites in the handler block continue to resolve against the bare function labels.  Binary grows 10 bytes (8334 → 8344) for the two bp frames plus the tail-call-to-call overhead in emit_word_ax
- asm.c: extract ``make_modrm_reg_reg`` and ``reg_to_rm`` into cc.py-emitted C helpers with inline-asm bodies.  ``make_modrm_reg_reg`` keeps its AL-in/BL-in → AL-out ABI (``AL = (3 << 6) | (AL << 3) | BL``); ``reg_to_rm`` keeps its AL-in → AL-out register-index → ModR/M-rm mapping, with the four branches rewritten as ``jmp .rtr_end`` to share cc.py's ``pop bp / ret`` epilogue.  Binary grows 11 bytes (8323 → 8334) for the two bp frames and the jmp-vs-ret overhead on reg_to_rm's three non-fall-through branches
- asm.c: extract ``hex_digit`` into a cc.py-emitted C helper with an inline-asm body.  ASCII ``0``-``9`` / ``A``-``F`` / ``a``-``f`` still arrive in CL and return numeric value in CL with CF set on a non-hex byte; the rewritten body replaces each branch's early ``ret`` with a ``jmp`` to a shared ``.hd_end`` tail label so cc.py's ``pop bp / ret`` epilogue closes the function (POP and RET don't touch FLAGS, so the CF the branch just set reaches the caller intact).  Callers in ``parse_number``'s ``0x`` and ``h``-suffix loops are unchanged (``call hex_digit`` still resolves against the bare function label).  Binary grows 7 bytes (8316 → 8323) — bp frame plus 1-byte jmp-vs-ret deltas for the three non-fall-through branches
- asm.c: extract ``skip_ws`` and ``skip_comma`` into cc.py-emitted C helpers with inline-asm bodies — the SI-register ABI every handler relies on is preserved through cc.py's ``push bp / mov bp, sp / pop bp / ret`` frame (BP push/pop doesn't touch SI or FLAGS).  No call-site changes; the 70+ ``call skip_ws`` / ``call skip_comma`` sites in the handler block continue to resolve against the bare function label cc.py emits.  Binary grows 8 bytes (8308 → 8316) for the two bp frames — same cost structure as ``flush_output``'s earlier extraction
- asm.c: rewrite ``read_line`` as a pure-C loop over the 512-byte SOURCE_BUFFER, refilling via ``load_src_sector()`` on cursor exhaustion.  CR bytes are silently skipped so DOS line endings fold to Unix, over-long lines truncate at 255 chars (the old LINE_MAX), and the function returns 1 on true EOF (load_src_sector signaled no more data with no partial line in flight) or 0 otherwise.  ``load_src_sector`` adopts the same int-return convention now that C owns both ends of the relationship, so ``clc`` / ``stc`` / ``sbb ax, ax`` bridges retire entirely; the CF-to-int ``read_line_is_eof`` shim also goes away — ``do_pass`` now calls ``read_line()`` and ``include_pop()`` directly as C functions.  A new ``char *source_buffer`` global (main() initializes it to ``_program_end + 768``) lets the C byte-at-position indexing compile; the inline-asm ``%assign LINE_MAX 255`` retires with read_line.  Binary grows 23 bytes (8285 → 8308) for cc.py's word-granular byte-by-byte loop vs the old ``rep``-lodsb style hand-tuned asm, against the two retired label blocks and the LINE_MAX %assign

- asm.c: extract ``load_src_sector`` into a pure-C function that preserves the CF-on-EOF return contract.  The body is one SP-balanced asm block (push bx/cx/di → ``SYS_IO_READ`` via the ES-safe syscall wrapper → check for AX = -1 or 0 → clc / stc → pop di/cx/bx → jmp to the function's end label so cc.py's standard ``pop bp / ret`` epilogue runs).  POP and RET don't touch FLAGS, so CF survives from the ``clc`` / ``stc`` through the epilogue to the caller (``read_line`` still holds the sole call site and uses ``jc .check_eof``).  Binary grows 5 bytes (8280 → 8285) for the bp frame minus the saved pops

- asm.c: extract ``do_pass`` into a pure-C function with a natural ``while (1)`` loop.  A tiny ``read_line_is_eof()`` helper bridges the inline-asm ``read_line`` routine's CF-on-EOF convention into a C int return (``sbb ax, ax`` / ``and ax, 1``), so the pass body reads as ``if (read_line_is_eof()) { if (!include_depth) break; call include_pop; continue; } call parse_line;`` — the CF-dispatch machinery stays invisible at the C level.  do_pass drops the inline-asm version's all-registers push/pop pair (``push ax bx cx dx si di`` at entry, mirrored pops at exit) because the only callers (``run_pass1`` / ``run_pass2`` in cc.py-emitted C) don't rely on register preservation across the call — cc.py's prologue / epilogue only touches BP, matching what the C callers expect.  The open / close blocks stay as short inline asm using the ES-safe ``syscall`` wrapper (CF propagation into C would require another bridge helper per site).  ``error_flag = 1`` on open failure lives in the asm via ``mov byte [_g_error_flag], 1`` since the jmp-over-the-close requires a label target anyway.  Binary grows 7 bytes (8273 → 8280) for cc.py's bp frame on do_pass plus the read_line_is_eof helper's own frame plus the shift from ``call do_pass`` to an inter-function call

- asm.c: extract ``include_push`` / ``include_pop`` into pure-C helpers and retire the INCLUDE_SAVE / INCLUDE_SOURCE_SAVE ``%define``s.  The three saved-parent-state fields (fd / position / valid) become cc.py-emitted globals (``include_save_fd`` / ``include_save_position`` / ``include_save_valid``); the 512-byte source-buffer copy stays as post-binary scratch RAM via a new ``char *include_source_save`` pointer that ``main()`` initializes to ``_program_end + 1280`` (past LINE_BUFFER / OUTPUT_BUFFER / SOURCE_BUFFER), so no zero-filled bytes land in the binary.  The ``include_push`` body runs a C-level ``source_prefix + <name>`` concatenation into the existing ``include_path`` buffer; its sole inline-asm blocks are the ``rep movsw`` that stashes SOURCE_BUFFER → ``include_source_save`` (ES=DS needed for the copy) and the ``SYS_IO_OPEN`` with a ``jc`` to an ``.ipush_err:`` label that raises ``error_flag``.  A new ``char *include_push_arg`` global bridges SI → C: the caller still passes the filename in SI, and the first instruction inside ``include_push`` stashes it with ``asm("mov [_g_include_push_arg], si")`` before cc.py's codegen is free to clobber SI.  Each ``asm()`` inside both helpers is SP-balanced — cc.py wraps every inline-asm block with ``push dx / pop dx`` to preserve the local pinned to DX, so a net-positive push would land pops on the wrong values.  The original inline-asm labels preserved AX / BX (and AX/BX/CX/SI/DI for include_pop); the pure-C versions drop that contract because the sole callers (``parse_directive``'s ``.got_inc_name`` jumps to ``.pd_done``; ``do_pass``'s ``.eof`` jumps to ``.line_loop`` → ``call read_line`` which reloads its own registers) don't rely on it.  Only ES is still guarded around the rep movsw, since callers run with ES=SYMBOL_SEGMENT.  Binary grows 57 bytes (8216 → 8273, delta -37 → +20) for the two bp frames plus C's word-granular string-copy idiom vs the old byte-inlined asm

- asm.c: extract the bulk of ``abort_unknown`` into a pure-C ``abort_unknown_impl`` (printf the bad line + the offending mnemonic, then jmp to FUNCTION_EXIT).  The inline-asm ``abort_unknown:`` shrinks to a two-instruction trampoline that stashes the caller's ``SI`` into a new ``char *error_word`` global and jumps to the C reporter — no caller changes; every ``call abort_unknown`` / ``jmp abort_unknown`` continues to work.  ``line_buffer`` (a new ``char *`` global) gets initialized at main() entry via ``asm("mov word [_g_line_buffer], _program_end")`` so the printf can read the source line.  With ``abort_unknown`` gone, the four ES-safe jump-table wrappers that only it used — ``call_exit``, ``call_print_character``, ``call_print_string``, ``call_write_stdout`` — retire with it; ``MESSAGE_ERROR_UNKNOWN`` / ``MESSAGE_ERROR_AT`` strings and their length-equ pairs come out of the inline-asm tail too.  Binary drops 14 bytes (8230 → 8216) despite the new printf machinery — the four jump-table wrappers and two 30+ byte format strings outweighed the added bp frame and printf call site
- asm.c: replace the symbol-overflow jmp-to-``call_die`` tail with a pure-C ``die_symbol_overflow`` helper that resets ES=DS and hands off to cc.py's ``die()``.  The ``call_die`` kernel-jump wrapper and the ``MESSAGE_SYMBOL_OVERFLOW`` string / length-equ retire with it — together with the just-deleted dead ``print_hex_word``, the inline asm now owns zero calls to ``call_die`` and zero to ``call_print_character`` outside ``abort_unknown``.  Binary grows 4 bytes (8226 → 8230) for the new helper's bp frame
- asm.c: delete dead ``print_hex_word`` helper (defined but never called — a debugging leftover from when the assembler was still being stood up).  Saves 37 bytes (8263 → 8226) and removes the last caller of ``call_print_character`` outside ``abort_unknown``
- asm.c: extract ``flush_output`` into a pure-C helper.  Body is a single ``asm("...")`` block that preserves AX / CX / SI / DI across the ``int 30h`` (via the ES-safe ``syscall`` wrapper) so the instruction handlers that reach it through ``emit_byte_al`` don't have to guard those registers at every call site — matches the contract of the retired inline-asm ``flush_output`` exactly.  OUTPUT_BUFFER = ``_program_end + 256`` is written as a literal arithmetic expression; NASM folds it at assembly time.  Binary grows 4 bytes (8259 → 8263) — the bp frame cc.py wraps the function with, against the saved push/pop chain the old asm version inlined
- asm.c: retire the inline-asm ``asm_main`` driver entirely.  cc.py's own ``main(int argc, char *argv[])`` now orchestrates the full boot sequence: argc check → ``die("Usage...")`` on mismatch, ``source_name = argv[0]`` / ``output_name = argv[1]``, ``compute_source_prefix()``, ``open(output_name, O_WRONLY+O_CREAT+O_TRUNC, FLAG_EXECUTE)`` → ``die("Error: cannot create output\n")`` on failure, ES ← ``0x2000`` (symbol-table segment for the handler pass), ``run_pass1() / run_pass2()``, ``asm("call flush_output")`` (the syscall wrapper inside flush_output preserves ES=SYMBOL_SEGMENT), ES ← DS, ``close()`` → ``die("Error: directory write failed\n")`` on failure, ``die("OK\n")`` on success.  ``output_name`` joins ``source_name`` as a ``char *`` so the C body can assign from argv directly.  Four die_* helpers (die_ok, die_usage, die_error_create, die_error_write_dir) go away — inlined into their single call sites now that main() does the error check itself; die_error_pass1_io / die_error_pass1_iter stay because run_pass1 still calls them from inside its convergence loop with ES=SYMBOL_SEGMENT in hand.  Inline asm drops the ``asm_main:`` label, the ``call syscall`` dance around IO_OPEN / IO_CLOSE, and the ``jmp asm_main`` trampoline; only the memory-layout %assigns/%defines, the 33 ``<name> equ _g_<name>`` aliases, and the ~4 000 lines of handler code remain.  Binary shrinks 29 bytes (8288 → 8259) — cc.py's main() is more compact than the hand-coded asm_main once both go through the same calling convention.  Self-host still byte-identical (asm.asm re-assembles to 8253 bytes), ``test_asm.py asm`` unchanged, ``test_cc.py asm`` passes (clang needed ``output_name`` to be ``char *`` for ``open()`` compatibility)
- asm.c: extract the iterative pass 1 convergence loop and the pass 2 setup into pure-C helpers ``run_pass1`` / ``run_pass2``.  ``run_pass1`` zeroes the per-pass counters, fills ES:JUMP_TABLE with all-1 via a tiny ``asm("mov di, 0F000h\nmov cx, 4096\nmov al, 1\ncld\nrep stosb")`` (NASM's ``%assign JUMP_TABLE`` lives inside the file-scope asm block which cc.py emits after every helper, so the numeric literals are duplicated at the extraction site), then spins a ``while(1)`` loop that invokes ``do_pass`` via ``asm("call do_pass")``, bails to ``die_error_pass1_io()`` on any handler error, caps at 100 iterations via ``die_error_pass1_iter()``, forces at least two iterations so forward references get verified, and exits once ``changed_flag`` stops flipping.  ``run_pass2`` resets the output counters, copies ``org_value`` into ``current_address``, then likewise kicks do_pass.  asm_main's inline asm drops ~30 lines in favor of ``call run_pass1`` / ``call run_pass2``; +10 bytes for the two bp frames and call sites.  All 27 self-host tests still pass, byte-identical output preserved
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
