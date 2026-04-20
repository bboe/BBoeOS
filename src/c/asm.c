/* Port of src/asm/asm.asm to C.  Phase 1: the assembler's logic still
   lives in inline assembly — one string literal per NASM source line,
   concatenated by cc.py (adjacent-literal fold) into the single
   file-scope `asm(...)` block that emits the body verbatim.  Follow-up
   PRs replace the driver, symbol table, emit functions, data tables,
   and each instruction-handler family with pure C one at a time.  The
   original NASM source is preserved under archive/asm.asm. */

#include "asm_layout.h"

/* File-scope globals that back the assembler's mutable state.  cc.py
   emits each as ``_g_<name>`` at the tail of the output; C code
   accesses them through the bare name (cc.py's symbol table resolves
   it) and the few remaining inline-asm blocks reference the
   ``_g_<name>`` form directly.  Scalars widen from ``db`` to ``dw``
   (cc.py's global layout), but every byte-width access reads/writes
   only the low byte — verified safe by grep for any word-granular
   load on the old ``db`` variables.

   ``char *`` pointer globals point at fixed post-binary scratch
   addresses (``_program_end`` + offset) initialized by main() so the
   on-disk image stays the same size as the NASM layout; ``char[N]``
   arrays emit ``times N db 0`` at the binary tail, which is fine for
   the two small fixed-size buffers. */
int changed_flag;
int current_address;
int equ_space;
int error_flag;
/* abort_unknown stores the offending mnemonic's SI into
   ``error_word`` before jumping to the pure-C reporter. */
char *error_word;
int global_scope = 0xFFFF;
int include_depth;
char include_path[32];
/* Bridge for include_push's SI input (pointer to the raw include
   filename parsed out of the source line).  cc.py has no syntax for
   mapping SI to a C parameter, so the caller's asm stashes SI here
   before the C body runs. */
char *include_push_arg;
/* Saved parent-file state for a single %include level.  These
   replace the INCLUDE_SAVE memory region that previously lived at
   ``_program_end + 1280`` — moving the 6 bytes into cc.py-emitted
   globals lets include_push / include_pop access the fields by name
   instead of [bx+0/2/4]. */
int include_save_fd;
int include_save_position;
int include_save_valid;
/* Pointer to the parent's 512-byte SOURCE_BUFFER copy, held in
   post-binary scratch RAM (main() sets it to ``_program_end + 1280``
   — past LINE_BUFFER / OUTPUT_BUFFER / SOURCE_BUFFER).  Storing the
   buffer as a C array instead would bake 512 zero bytes into the
   binary; the scratch address keeps the on-disk size the same as
   the NASM layout. */
char *include_source_save;
int iteration_count;
int jump_index;
int last_symbol_index;
/* Pointer to the 256-byte line-accumulation buffer at
   ``_program_end`` (main() initializes it).  read_line fills it
   null-terminated; abort_unknown_impl prints it. */
char *line_buffer;
int op1_register;
int op1_size;
int op1_type;
int op1_value;
int op2_register;
int op2_type;
int op2_value;
int org_value;
/* ``parse_operand_c`` stashes its DX output here so the C caller
   can read the displacement / immediate value after the call; AX
   keeps the packed ``AH = type``, ``AL = reg`` return. */
int parse_operand_value;
/* Pointer to the 512-byte output-byte buffer at ``_program_end + 256``
   (= OUTPUT_BUFFER in the asm_layout.h #define).  main() initializes
   it; ``emit_byte`` / ``flush_output`` index into it directly. */
char *output_buffer;
int output_fd;
char *output_name;
int output_position;
int output_total;
int pass;
/* ``peek_label_target`` stashes the resolved symbol value here (AX-side
   of the retired dual AX + CF return).  ``carry_return`` can only
   signal CF, so the two encode_rel8_jump call sites now read
   ``peek_label_value`` after the ``jnc`` instead of reading AX directly.
   Only valid on hit (CF clear); callers branch on CF first. */
int peek_label_value;
/* Pointer to the 512-byte source-file read buffer at
   ``_program_end + 768`` (= SOURCE_BUFFER in the old %define).
   main() initializes it; read_line / include_pop / include_push
   index into it directly. */
char *source_buffer;
int source_buffer_position;
int source_buffer_valid;
/* C-level alias for the SI register — the source cursor that every
   handler / parser function consumes.  Inline-asm bodies still use
   SI directly (no conflict since it's the same register); pure-C
   bodies read and advance ``source_cursor`` as a normal ``char *``.
   No storage is emitted for the global — reads compile as ``mov ax,
   si`` (or a no-op when the target IS SI), writes as ``mov si, ...``,
   ``source_cursor = source_cursor + 1`` folds to ``inc si``, and
   ``source_cursor[0]`` compiles to ``mov al, [si]``. */
__attribute__((asm_register("si")))
char *source_cursor;
int source_fd;
char *source_name;
char source_prefix[32];
int symbol_count;
int symbol_set_scope;
int symbol_set_value;

/* Forward declarations for functions defined later in the file that
   pure-C bodies need to call.  cc.py resolves these via its two-pass
   ``user_functions`` registry regardless of source order, but clang
   enforces ISO C99 declare-before-use; the prototypes placate the
   syntax check without affecting codegen. */
__attribute__((regparm(1)))
int make_modrm_reg_reg_impl(int reg, int rm);
__attribute__((regparm(1)))
__attribute__((carry_return))
int match_word(char *keyword);
__attribute__((regparm(1)))
void mem_op_reg_emit(int opcode);
__attribute__((regparm(1)))
void mnemonic_dispatch_at(int index);
__attribute__((regparm(1)))
char *mnemonic_keyword_at(int index);
void parse_directive();
int parse_operand_c();
void parse_mnemonic();
int parse_register();
void close_source();
__attribute__((regparm(1)))
int open_file_ro(char *path);
__attribute__((regparm(1)))
int reg_to_rm(int reg);
int resolve_label();
int resolve_value();
void skip_comma();
void skip_ws();
__attribute__((regparm(1)))
void symbol_add_constant_c(int value);
__attribute__((regparm(1)))
int symbol_lookup_c(int scope);
__attribute__((regparm(1)))
void symbol_set_global(int value);
__attribute__((regparm(1)))
void symbol_set_local(int value);

/* Two-instruction trampoline reached via ``jmp abort_unknown`` (not
   ``call``) from dozens of handler sites.  Stashes the offending
   mnemonic's SI into ``error_word`` and jumps to
   abort_unknown_impl, which prints and exits.  Naked-asm shape,
   so cc.py elides the bp frame — the terminal ``jmp`` means the
   ``ret`` that cc.py appends is dead code (1 byte), same cost as
   the retired file-scope asm version. */
void abort_unknown() {
    asm("mov [_g_error_word], si\n"
        "jmp abort_unknown_impl");
}

/* Restore ES=DS so cc.py's ``die`` / ``printf`` / ``close`` builtins
   (which jmp/int-30h into the kernel expecting ES=0) work correctly
   from code paths where ES has been pointed at SYMBOL_SEGMENT for
   the symbol table.  ``always_inline`` splices the 2-byte body at
   every call site, saving the 3-byte ``call`` + 1-byte shared ``ret``. */
__attribute__((always_inline))
void restore_es() {
    asm("push ds\npop es");
}

/* Invoked through ``abort_unknown`` above.  Prints the offending
   source line from ``line_buffer`` together with the bad token,
   then exits. */
void abort_unknown_impl() {
    restore_es();
    printf("Error: unknown mnemonic or directive at line:\n  %s\n  at: %s\n",
           line_buffer, error_word);
    /* ``exit()`` would be cleaner but clang sees the stdlib prototype
       via tests/bboeos.h and rejects the zero-argument form.  Jumping
       to FUNCTION_EXIT keeps cc.py and clang both happy. */
    asm("jmp FUNCTION_EXIT");
}

/* Populate ``source_prefix`` with the directory portion of
   ``source_name`` (everything up to and including the last ``/``).
   Empty when the source has no directory.  Bounded by the
   ``source_prefix[32]`` buffer — deeper include paths would overflow
   silently, which matches the original inline-asm behavior. */
void compute_source_prefix() {
    int end = 0;
    int i = 0;
    while (source_name[i] != '\0') {
        if (source_name[i] == '/') {
            end = i + 1;
        }
        i = i + 1;
    }
    int j = 0;
    while (j < end) {
        source_prefix[j] = source_name[j];
        j = j + 1;
    }
    source_prefix[end] = '\0';
}

/* Error reporters called while ES is still pointed at the symbol-
   table segment.  Each resets ES to DS before handing off to cc.py's
   ``die()`` builtin (which jumps to FUNCTION_DIE with the string
   preloaded).  ``die_error_pass1_io`` / ``die_error_pass1_iter`` are
   called from ``run_pass1`` when do_pass signals an I/O error or the
   jump-size convergence fails to settle; ``die_symbol_overflow`` is
   the jmp-target of the symbol_set overflow branch. */
void die_error_pass1_io() {
    restore_es();
    die("Error: pass 1 io\n");
}

void die_error_pass1_iter() {
    restore_es();
    die("Error: pass 1 iter\n");
}

void die_symbol_overflow() {
    restore_es();
    die("Error: symbol table overflow (raise SYMBOL_MAX)\n");
}

/* Emit one byte (AL) into the output stream.  Pass 1 only bumps
   ``current_address`` / ``output_total``; pass 2 also stores the
   byte into ``OUTPUT_BUFFER`` at ``output_position``, flushing to
   ``output_fd`` when the buffer fills.  The inline-asm body
   preserves the AL-in ABI.  Clobbers BX only when pass == 2 (saved
   + restored around the buffer-pointer math). */
/* Legacy AL-in thunk: inline-asm callers still do ``mov al, OPCODE ;
   call emit_byte_al``.  The thunk zero-extends AL to AX (so garbage
   AH can't leak into ``v``'s local slot during the fastcall spill)
   and hands off to the pure-C ``emit_byte`` through the standard
   C calling convention.  ``pusha`` / ``popa`` preserve every
   register the old inline-asm body guaranteed (and more) at 1 byte
   each; this is the backwards-compat bridge that lets the handler
   family migrate to pure C one family at a time without touching
   every call site in the remaining inline asm. */
void emit_byte_al() {
    asm("pusha\n"
        "mov ah, 0\n"
        "call emit_byte\n"
        "popa");
}

/* Emit one little-endian word (AX).  Saves the high byte, emits
   AL, swaps, emits the old AH.  The retired asm used a tail call
   for the second emit_byte_al; this version uses a regular call
   so cc.py's ``pop bp / ret`` epilogue can close the bp frame. */
void emit_word_ax() {
    asm("push ax\n"
        "call emit_byte_al\n"
        "pop ax\n"
        "xchg al, ah\n"
        "call emit_byte_al");
}

/* Pick between short (rel8) and near (rel16) jump encoding per
   call, using the pass-1 bitmap at ES:JUMP_TABLE indexed by
   ``jump_index``.  Takes the opcode via the ``regparm(1)`` fastcall
   convention (AX on entry); the body does its own ``push ax`` to
   save the value across ``skip_ws`` / ``peek_label_target`` calls
   and later ``pop ax`` to recover it for emission.  SI points at
   the label operand on entry (inline-asm ABI, inherited from the
   handler callers).  jump_table[idx] = 0 means short, 1 means near.
   Pass 1 iterates, flipping bits until ``changed_flag`` stops
   toggling; pass 2 trusts the choices.  Note: the
   ``.erj_shrink_forward`` branch peeks at the body's saved-AX slot
   via an inner ``push bp / cmp byte [bp+2], 0EBh / pop bp`` — the
   peek is relative to the body's ``push ax``, so cc.py's fastcall
   prologue (``push bp / mov bp, sp / sub sp, 2 / mov [bp-2], ax``)
   sits further up the stack and doesn't affect the offset. */
__attribute__((regparm(1)))
void encode_rel8_jump(int opcode) {
    asm("push ax\n"
        "call skip_ws\n"
        "mov bx, [_g_jump_index]\n"
        "inc word [_g_jump_index]\n"
        "cmp byte [es:JUMP_TABLE + bx], 0\n"
        "jne .erj_try_shrink\n"
        "cmp byte [_g_pass], 1\n"
        "jne .erj_emit_short\n"
        "push bx\n"
        "call peek_label_target\n"
        "pop bx\n"
        "jc .erj_emit_short\n"
        "mov ax, [_g_peek_label_value]\n"
        "mov dx, [_g_current_address]\n"
        "add dx, 2\n"
        "sub ax, dx\n"
        "add ax, 128\n"
        "cmp ax, 256\n"
        "jb .erj_emit_short\n"
        "mov byte [es:JUMP_TABLE + bx], 1\n"
        "mov byte [_g_changed_flag], 1\n"
        "jmp .erj_long_form\n"
        ".erj_try_shrink:\n"
        "cmp byte [_g_pass], 1\n"
        "jne .erj_long_form\n"
        "push bx\n"
        "call peek_label_target\n"
        "pop bx\n"
        "jc .erj_long_form\n"
        "mov ax, [_g_peek_label_value]\n"
        "mov dx, [_g_current_address]\n"
        "cmp ax, dx\n"
        "jae .erj_shrink_forward\n"
        "add dx, 2\n"
        "jmp .erj_shrink_check\n"
        ".erj_shrink_forward:\n"
        "add dx, 4\n"
        "push bp\n"
        "mov bp, sp\n"
        "cmp byte [bp+2], 0EBh\n"
        "pop bp\n"
        "jne .erj_shrink_check\n"
        "dec dx\n"
        ".erj_shrink_check:\n"
        "sub ax, dx\n"
        "add ax, 128\n"
        "cmp ax, 256\n"
        "jae .erj_long_form\n"
        "mov byte [es:JUMP_TABLE + bx], 0\n"
        "mov byte [_g_changed_flag], 1\n"
        "jmp .erj_emit_short\n"
        ".erj_long_form:\n"
        "pop ax\n"
        "cmp al, 0EBh\n"
        "je .erj_long_jmp\n"
        "add al, 10h\n"
        "push ax\n"
        "mov al, 0Fh\n"
        "call emit_byte_al\n"
        "pop ax\n"
        "call emit_byte_al\n"
        "jmp .erj_long_emit_disp\n"
        ".erj_long_jmp:\n"
        "mov al, 0E9h\n"
        "call emit_byte_al\n"
        ".erj_long_emit_disp:\n"
        "call resolve_label\n"
        "mov bx, [_g_current_address]\n"
        "add bx, 2\n"
        "sub ax, bx\n"
        "call emit_word_ax\n"
        "jmp .erj_end\n"
        ".erj_emit_short:\n"
        "pop ax\n"
        "call emit_byte_al\n"
        "call resolve_label\n"
        "mov bx, [_g_current_address]\n"
        "inc bx\n"
        "sub ax, bx\n"
        "call emit_byte_al\n"
        ".erj_end:");
}

/* Write the accumulated OUTPUT_BUFFER (output_position bytes) to
   output_fd via SYS_IO_WRITE, then reset the position.  No-op when
   nothing is queued.  Callable from inline asm (``call flush_output``)
   since cc.py emits the label with the C name.  Uses the ES-safe
   ``syscall`` wrapper so ES=SYMBOL_SEGMENT survives the ``int 30h``.
   The retired asm preserved AX / CX / SI / DI so transitive callers
   reaching here through ``emit_byte_al``'s ``pusha`` didn't need
   per-register guards; ``emit_byte_al`` still does the ``pusha`` /
   ``popa`` externally, so the C body can let cc.py's calling
   convention handle clobber. */
void flush_output() {
    if (output_position == 0) {
        return;
    }
    asm("mov bx, [_g_output_fd]\n"
        "mov si, OUTPUT_BUFFER\n"
        "mov cx, [_g_output_position]\n"
        "mov ah, SYS_IO_WRITE\n"
        "call syscall");
    output_position = 0;
}

/* Emit one byte into the output stream.  Pass 1 only bumps
   current_address / output_total; pass 2 also stores the byte into
   OUTPUT_BUFFER at output_position, flushing to output_fd when the
   buffer fills.  Callers load ``v`` into AX via the ``regparm(1)``
   calling convention; the fastcall prologue spills AX into ``v``'s
   local slot so the body reads it through the normal local path.
   Placed after its C-level callee (``flush_output``) rather than in
   strict alphabetical position: cc.py resolves the call either way
   via its pre-codegen ``user_functions`` registry, but clang
   enforces ISO C99 declare-before-use.

   SI is preserved across the body via an explicit ``push si`` /
   ``pop si`` pair around the ``output_buffer[...]`` subscript
   (cc.py's byte-index codegen uses SI as scratch).  Pure-C
   handlers that follow an ``emit_byte`` with a ``skip_ws`` or
   ``parse_mnemonic`` call rely on ``source_cursor`` (the SI alias)
   surviving the emit — without the guard, every subscript would
   trash the live cursor.  ``flush_output`` already preserves SI
   around its own body, so the inner ``flush_output()`` call
   inside our push/pop bracket composes cleanly. */
__attribute__((regparm(1)))
void emit_byte(int v) {
    if (pass == 2) {
        asm("push si");
        output_buffer[output_position] = v;
        output_position = output_position + 1;
        if (output_position >= 512) {
            flush_output();
        }
        asm("pop si");
    }
    current_address = current_address + 1;
    output_total = output_total + 1;
}

/* Single-byte / two-byte emitters for zero-operand mnemonics.  Each
   handler is dispatched through ``mnemonic_table`` (inline-asm tail
   of this file): ``parse_mnemonic`` does an indirect ``call`` on the
   label.  Bodies are plain C calls to ``emit_byte``, which uses the
   ``regparm(1)`` fastcall convention so the call site is ``mov ax,
   OPCODE ; call emit_byte`` — the same byte count as the retired
   ``mov al, OPCODE ; call emit_byte_al`` inline form but expressed
   in pure C. */
void handle_aam() {
    emit_byte(0xD4);
    emit_byte(0x0A);
}

/* ``adc r16, imm8`` — the only form cc.py emits (the checksum-fold
   idiom).  Encoded as 83 /2 ib: sign-extended imm8 into r16. */
void handle_adc() {
    skip_ws();
    int pr = parse_register();
    skip_comma();
    int imm = resolve_value();
    emit_byte(0x83);
    emit_byte(0xD0 | (pr & 0xFF));
    emit_byte(imm);
}

/* ``add`` has four operand shapes: ``[disp16], r16`` (via
   mem_op_reg_emit with opcode 01), reg/reg (00/01 modrm),
   reg/[mem] (02/03 modrm with direct disp16 or reg+disp8), and
   reg/imm (04/05 short AL / AX / 80 / 81 / 83 /0 by imm size and
   register).  /r field is 0 so modrm constants are 0xC0 for
   register mode.  The ``add [disp16], r16`` entry uses call +
   ``.had_end:`` so cc.py's bp frame closes after mem_op_reg_emit
   returns. */
void handle_add() {
    skip_ws();
    if (source_cursor[0] == '[') {
        mem_op_reg_emit(0x01);
        return;
    }
    int pr = parse_register();
    int reg1 = pr & 0xFF;
    int size1 = (pr >> 8) & 0xFF;
    skip_comma();
    int po = parse_operand_c();
    int t2 = (po >> 8) & 0xFF;
    int r2 = po & 0xFF;
    int v2 = parse_operand_value;
    if (t2 == 0) {
        if (size1 == 8) {
            emit_byte(0x00);
        } else {
            emit_byte(0x01);
        }
        emit_byte(make_modrm_reg_reg_impl(r2, reg1));
    } else if (t2 == 2) {
        if (size1 == 8) {
            emit_byte(0x02);
        } else {
            emit_byte(0x03);
        }
        emit_byte((reg1 << 3) | 0x06);
        emit_byte(v2 & 0xFF);
        emit_byte((v2 >> 8) & 0xFF);
    } else if (t2 == 3) {
        if (size1 == 8) {
            emit_byte(0x02);
        } else {
            emit_byte(0x03);
        }
        emit_byte(reg_to_rm(r2) | 0x40 | (reg1 << 3));
        emit_byte(v2 & 0xFF);
    } else if (size1 == 8) {
        if (reg1 == 0) {
            emit_byte(0x04);
        } else {
            emit_byte(0x80);
            emit_byte(0xC0 | reg1);
        }
        emit_byte(v2 & 0xFF);
    } else if (v2 >= -128 && v2 <= 127) {
        emit_byte(0x83);
        emit_byte(0xC0 | reg1);
        emit_byte(v2 & 0xFF);
    } else if (reg1 == 0) {
        emit_byte(0x05);
        emit_byte(v2 & 0xFF);
        emit_byte((v2 >> 8) & 0xFF);
    } else {
        emit_byte(0x81);
        emit_byte(0xC0 | reg1);
        emit_byte(v2 & 0xFF);
        emit_byte((v2 >> 8) & 0xFF);
    }
}

/* ``and r, r`` / ``and r, imm`` / ``and [disp16], r16``.  The
   memory-destination form tail-calls into mem_op_reg_emit (still
   a file-scope asm label) via ``call`` — the retired asm used
   ``jmp mem_op_reg_emit`` but that would strand cc.py's pushed
   bp here; the call lets emit_word_ax inside mem_op_reg_emit
   return into our body so the ``jmp .han_end`` path can close
   the epilogue cleanly.  Reg-reg / reg-imm dispatch mirrors
   handle_xor with the /r field constant swapped for 0xE0 (``/4``)
   and the short AL / AX form using 24/25. */
void handle_and() {
    skip_ws();
    if (source_cursor[0] == '[') {
        mem_op_reg_emit(0x21);
        return;
    }
    int pr = parse_register();
    int reg1 = pr & 0xFF;
    int size1 = (pr >> 8) & 0xFF;
    skip_comma();
    int po = parse_operand_c();
    int t2 = (po >> 8) & 0xFF;
    int r2 = po & 0xFF;
    int v2 = parse_operand_value;
    if (t2 == 0) {
        if (size1 == 8) {
            emit_byte(0x20);
        } else {
            emit_byte(0x21);
        }
        emit_byte(make_modrm_reg_reg_impl(r2, reg1));
    } else if (size1 == 8) {
        if (reg1 == 0) {
            emit_byte(0x24);
            emit_byte(v2 & 0xFF);
        } else {
            emit_byte(0x80);
            emit_byte(0xE0 | reg1);
            emit_byte(v2 & 0xFF);
        }
    } else if (v2 >= -128 && v2 <= 127) {
        emit_byte(0x83);
        emit_byte(0xE0 | reg1);
        emit_byte(v2 & 0xFF);
    } else {
        emit_byte(0x81);
        emit_byte(0xE0 | reg1);
        emit_byte(v2 & 0xFF);
        emit_byte((v2 >> 8) & 0xFF);
    }
}

/* ``call <label>`` (E8 rel16) and ``call [reg+disp8]`` (FF /2) —
   the only two call forms the self-host needs.  The indirect form
   requires a non-zero disp that fits in a signed byte; anything
   else jumps to abort_unknown. */
void handle_call() {
    skip_ws();
    if (source_cursor[0] == '[') {
        int po = parse_operand_c();
        int t = (po >> 8) & 0xFF;
        int r = po & 0xFF;
        int v = parse_operand_value;
        if (t != 3 || v == 0 || v < -128 || v > 127) {
            abort_unknown();
        }
        emit_byte(0xFF);
        emit_byte(reg_to_rm(r) | 0x50);
        emit_byte(v & 0xFF);
    } else {
        emit_byte(0xE8);
        int target = resolve_label();
        int delta = target - current_address - 2;
        emit_byte(delta & 0xFF);
        emit_byte((delta >> 8) & 0xFF);
    }
}

void handle_clc() {
    emit_byte(0xF8);
}

void handle_cld() {
    emit_byte(0xFC);
}

/* ``cmp`` covers r-r, r-imm, r-[mem], [mem]-imm, and [disp16]-imm.
   ``/r`` field is 7 (``0xF8`` register-mode modrm constant, ``0x38``
   for memory-mode reg field, ``0x3E`` for the mod=00 disp16 form).
   Short forms are 3C (AL imm8), 3D (AX imm16), 80 /7 / 81 /7 / 83 /7
   for the general reg-imm; 38 / 39 for reg-reg; 3A / 3B for reg-mem. */
void handle_cmp() {
    skip_ws();
    int po = parse_operand_c();
    int t1 = (po >> 8) & 0xFF;
    int r1 = po & 0xFF;
    int v1 = parse_operand_value;
    int size1 = op1_size;
    skip_comma();
    if (t1 == 0) {
        char *saved = source_cursor;
        int pr2 = parse_register();
        if (pr2 >= 0) {
            if (size1 == 8) {
                emit_byte(0x38);
            } else {
                emit_byte(0x39);
            }
            emit_byte(make_modrm_reg_reg_impl(pr2 & 0xFF, r1));
            return;
        }
        source_cursor = saved;
        if (source_cursor[0] == '[') {
            int po2 = parse_operand_c();
            int t2 = (po2 >> 8) & 0xFF;
            int r2 = po2 & 0xFF;
            int v2 = parse_operand_value;
            if (t2 == 2) {
                if (size1 == 8) {
                    emit_byte(0x3A);
                } else {
                    emit_byte(0x3B);
                }
                emit_byte((r1 << 3) | 0x06);
                emit_byte(v2 & 0xFF);
                emit_byte((v2 >> 8) & 0xFF);
                return;
            }
            if (t2 == 3) {
                if (size1 == 8) {
                    emit_byte(0x3A);
                } else {
                    emit_byte(0x3B);
                }
                int modrm = (r1 << 3) | reg_to_rm(r2);
                if (v2 == 0) {
                    emit_byte(modrm);
                } else if (v2 >= -128 && v2 <= 127) {
                    emit_byte(modrm | 0x40);
                    emit_byte(v2 & 0xFF);
                } else {
                    emit_byte(modrm | 0x80);
                    emit_byte(v2 & 0xFF);
                    emit_byte((v2 >> 8) & 0xFF);
                }
                return;
            }
        }
        int imm = resolve_value();
        if (size1 == 8) {
            if (r1 == 0) {
                emit_byte(0x3C);
            } else {
                emit_byte(0x80);
                emit_byte(0xF8 | r1);
            }
            emit_byte(imm & 0xFF);
        } else if (imm >= -128 && imm <= 127) {
            emit_byte(0x83);
            emit_byte(0xF8 | r1);
            emit_byte(imm & 0xFF);
        } else if (r1 == 0) {
            emit_byte(0x3D);
            emit_byte(imm & 0xFF);
            emit_byte((imm >> 8) & 0xFF);
        } else {
            emit_byte(0x81);
            emit_byte(0xF8 | r1);
            emit_byte(imm & 0xFF);
            emit_byte((imm >> 8) & 0xFF);
        }
        return;
    }
    if (t1 != 2 && t1 != 3) {
        return;
    }
    int imm = resolve_value();
    int opcode;
    int is_imm8;
    if (size1 == 8) {
        opcode = 0x80;
        is_imm8 = 1;
    } else if (imm >= -128 && imm <= 127) {
        opcode = 0x83;
        is_imm8 = 1;
    } else {
        opcode = 0x81;
        is_imm8 = 0;
    }
    emit_byte(opcode);
    if (t1 == 2) {
        emit_byte(0x3E);
        emit_byte(v1 & 0xFF);
        emit_byte((v1 >> 8) & 0xFF);
    } else {
        int modrm = reg_to_rm(r1) | 0x38;
        if (v1 == 0) {
            emit_byte(modrm);
        } else if (v1 >= -128 && v1 <= 127) {
            emit_byte(modrm | 0x40);
            emit_byte(v1 & 0xFF);
        } else {
            emit_byte(modrm | 0x80);
            emit_byte(v1 & 0xFF);
            emit_byte((v1 >> 8) & 0xFF);
        }
    }
    if (is_imm8) {
        emit_byte(imm & 0xFF);
    } else {
        emit_byte(imm & 0xFF);
        emit_byte((imm >> 8) & 0xFF);
    }
}

/* ``inc`` / ``dec`` with r8 / r16 / memory destination.  r16 uses
   the 40+reg / 48+reg one-byte forms; r8 and memory use FE/FF with
   a /0 (inc) or /1 (dec) reg field.  Memory dispatch mirrors the
   three parse_operand op2 types: 0=reg (handled above), 2=direct
   disp16, 3=reg+disp8 (or bare reg when disp == 0).  handle_dec
   differs from handle_inc only in the /r-field constant (0x08 vs
   0x00 for reg form, 0x0E vs 0x06 for mod=00 disp16 form). */
void handle_dec() {
    skip_ws();
    int po = parse_operand_c();
    int type = (po >> 8) & 0xFF;
    int reg = po & 0xFF;
    int value = parse_operand_value;
    int size = op1_size;
    if (type == 0) {
        if (size == 8) {
            emit_byte(0xFE);
            emit_byte(0xC8 | reg);
        } else {
            emit_byte(0x48 + reg);
        }
    } else {
        if (size == 8) {
            emit_byte(0xFE);
        } else {
            emit_byte(0xFF);
        }
        if (type == 2) {
            emit_byte(0x0E);
            emit_byte(value & 0xFF);
            emit_byte((value >> 8) & 0xFF);
        } else {
            int rm = reg_to_rm(reg) | 0x08;
            if (value == 0) {
                emit_byte(rm);
            } else {
                emit_byte(rm | 0x40);
                emit_byte(value & 0xFF);
            }
        }
    }
}

/* ``div r8`` / ``div r16`` — picks F6 or F7, ORs 0xF0 (modrm with
   /6 reg field) into the register number.  Same shape as
   handle_mul / handle_neg / handle_not above. */
void handle_div() {
    skip_ws();
    int pr = parse_register();
    int opcode = 0xF7;
    if ((pr >> 8) == 8) {
        opcode = 0xF6;
    }
    emit_byte(opcode);
    emit_byte(0xF0 | (pr & 0xFF));
}

void handle_inc() {
    skip_ws();
    int po = parse_operand_c();
    int type = (po >> 8) & 0xFF;
    int reg = po & 0xFF;
    int value = parse_operand_value;
    int size = op1_size;
    if (type == 0) {
        if (size == 8) {
            emit_byte(0xFE);
            emit_byte(0xC0 | reg);
        } else {
            emit_byte(0x40 + reg);
        }
    } else {
        if (size == 8) {
            emit_byte(0xFE);
        } else {
            emit_byte(0xFF);
        }
        if (type == 2) {
            emit_byte(0x06);
            emit_byte(value & 0xFF);
            emit_byte((value >> 8) & 0xFF);
        } else {
            int rm = reg_to_rm(reg);
            if (value == 0) {
                emit_byte(rm);
            } else {
                emit_byte(rm | 0x40);
                emit_byte(value & 0xFF);
            }
        }
    }
}

/* ``int <imm8>`` — emits CD imm8.  Uses push/pop AX to shuttle the
   immediate across the emit_byte_al call, since both the opcode
   byte and the immediate byte land in AL. */
/* ``int <imm8>`` — two-byte encoding (``CD imm8``).  ``resolve_value``
   returns the immediate in AX and advances source_cursor past the
   expression; emit_byte preserves SI across its subscript so the
   inner ``emit_byte(resolve_value())`` composes without spilling
   through a local (body stays frameless). */
void handle_int() {
    skip_ws();
    emit_byte(0xCD);
    emit_byte(resolve_value());
}

/* Conditional-jump family: each handler hands its rel8 opcode off
   to ``encode_rel8_jump`` via the fastcall ``regparm(1)`` convention
   so the call site compiles as ``mov ax, OP ; call encode_rel8_jump``
   in pure C.  The shared helper picks between short (rel8) and near
   (rel16) encoding per jump based on the pass-1 jump-table bitmap.
   The mnemonic table aliases STR_JAE to handle_jnc, STR_JE to
   handle_jz, STR_JNZ to handle_jne, and STR_JC to handle_jb so the
   shared-body cases reuse a single C function. */
void handle_ja() {
    encode_rel8_jump(0x77);
}

void handle_jb() {
    encode_rel8_jump(0x72);
}

void handle_jbe() {
    encode_rel8_jump(0x76);
}

void handle_jg() {
    encode_rel8_jump(0x7F);
}

void handle_jge() {
    encode_rel8_jump(0x7D);
}

void handle_jl() {
    encode_rel8_jump(0x7C);
}

void handle_jle() {
    encode_rel8_jump(0x7E);
}

/* ``jmp <label>`` accepts an optional ``short`` keyword that
   ``match_word`` peels off the front of the operand before handing
   the real target to ``encode_rel8_jump``.  The extra SI dance
   (push / pop around match_word) mirrors the retired inline-asm
   version; keeping it in asm avoids leaking a C local whose
   register pinning would make the SI shuffle more awkward. */
/* ``jmp`` peels an optional ``short`` keyword (the asm's .hj_no_short
   branch's pop/push/skip_ws restore was a no-op: match_word already
   rewinds SI on miss, and skip_ws is idempotent) then falls through
   to ``encode_rel8_jump(0xEB)``, which runs its own skip_ws before
   consuming the label.  The short-form opcode 0xEB may grow to the
   long-form 0xE9 rel16 in pass 1 if the target doesn't fit ±128. */
void handle_jmp() {
    skip_ws();
    match_word(STR_SHORT);
    encode_rel8_jump(0xEB);
}

void handle_jnc() {
    encode_rel8_jump(0x73);
}

void handle_jne() {
    encode_rel8_jump(0x75);
}

void handle_jns() {
    encode_rel8_jump(0x79);
}

void handle_jz() {
    encode_rel8_jump(0x74);
}

void handle_lodsb() {
    emit_byte(0xAC);
}

void handle_lodsw() {
    emit_byte(0xAD);
}

void handle_loop() {
    encode_rel8_jump(0xE2);
}

/* The most operand-heavy handler: mov has ten shapes.
   ``mov es, r16`` emits 8E /r (only ES is needed — the self-host's
   sole segment-register write).  Otherwise parse_operand seeds
   op1 and op2, then dispatch on op1_type × op2_type:
     - reg × reg           → 88/89 modrm
     - reg × imm           → B0+r / B8+r short
     - reg × [disp16]      → A0/A1 short for AL/AX, else 8A/8B + modrm rm=110
     - reg × [reg+disp]    → 8A/8B + modrm (mod=00/01/10 by disp size)
     - [disp16] × imm      → C6/C7 + modrm (mod=00, rm=110) + disp16 + imm
     - [disp16] × reg      → A2/A3 short for AL/AX, else 88/89 + modrm
     - [reg+disp] × imm    → C6/C7 + modrm + disp + imm (0 / disp8 / disp16)
     - [reg+disp] × reg    → 88/89 + modrm + disp
   Any other combination lands in ``.hmv_done`` as a no-op
   (following the retired asm's behavior — callers treat unparsed
   forms as abort_unknown candidates earlier).  The .not_segment
   path restores SI via the saved copy so the register-by-register
   parser sees the original ``es,`` token again; the matched-ES
   path discards the saved SI. */
void handle_mov() {
    skip_ws();
    if (source_cursor[0] == 'e' && source_cursor[1] == 's') {
        char *saved = source_cursor;
        source_cursor = source_cursor + 2;
        skip_ws();
        if (source_cursor[0] == ',') {
            source_cursor = source_cursor + 1;
            skip_ws();
            int po = parse_operand_c();
            emit_byte(0x8E);
            emit_byte(0xC0 | (po & 0xFF));
            return;
        }
        source_cursor = saved;
    }
    int po1 = parse_operand_c();
    int t1 = (po1 >> 8) & 0xFF;
    int r1 = po1 & 0xFF;
    int v1 = parse_operand_value;
    skip_comma();
    int po2 = parse_operand_c();
    int t2 = (po2 >> 8) & 0xFF;
    int r2 = po2 & 0xFF;
    int v2 = parse_operand_value;
    int size1 = op1_size;
    if (t1 == 0) {
        if (t2 == 0) {
            if (size1 == 8) {
                emit_byte(0x88);
            } else {
                emit_byte(0x89);
            }
            emit_byte(make_modrm_reg_reg_impl(r2, r1));
            return;
        }
        if (t2 == 1) {
            if (size1 == 8) {
                emit_byte(0xB0 | r1);
                emit_byte(v2 & 0xFF);
            } else {
                emit_byte(0xB8 | r1);
                emit_byte(v2 & 0xFF);
                emit_byte((v2 >> 8) & 0xFF);
            }
            return;
        }
        if (t2 == 2) {
            if (size1 == 8 && r1 == 0) {
                emit_byte(0xA0);
            } else if (size1 != 8 && r1 == 0) {
                emit_byte(0xA1);
            } else {
                if (size1 == 8) {
                    emit_byte(0x8A);
                } else {
                    emit_byte(0x8B);
                }
                emit_byte((r1 << 3) | 0x06);
            }
            emit_byte(v2 & 0xFF);
            emit_byte((v2 >> 8) & 0xFF);
            return;
        }
        if (t2 == 3) {
            if (size1 == 8) {
                emit_byte(0x8A);
            } else {
                emit_byte(0x8B);
            }
            int modrm = (r1 << 3) | reg_to_rm(r2);
            if (v2 == 0) {
                emit_byte(modrm);
            } else if (v2 >= -128 && v2 <= 127) {
                emit_byte(modrm | 0x40);
                emit_byte(v2 & 0xFF);
            } else {
                emit_byte(modrm | 0x80);
                emit_byte(v2 & 0xFF);
                emit_byte((v2 >> 8) & 0xFF);
            }
            return;
        }
        abort_unknown();
    }
    if (t1 == 2) {
        if (t2 == 0) {
            if (size1 == 8 && r2 == 0) {
                emit_byte(0xA2);
            } else if (size1 != 8 && r2 == 0) {
                emit_byte(0xA3);
            } else {
                if (size1 == 8) {
                    emit_byte(0x88);
                } else {
                    emit_byte(0x89);
                }
                emit_byte((r2 << 3) | 0x06);
            }
            emit_byte(v1 & 0xFF);
            emit_byte((v1 >> 8) & 0xFF);
            return;
        }
        if (t2 == 1) {
            if (size1 == 8) {
                emit_byte(0xC6);
            } else {
                emit_byte(0xC7);
            }
            emit_byte(0x06);
            emit_byte(v1 & 0xFF);
            emit_byte((v1 >> 8) & 0xFF);
            if (size1 == 8) {
                emit_byte(v2 & 0xFF);
            } else {
                emit_byte(v2 & 0xFF);
                emit_byte((v2 >> 8) & 0xFF);
            }
            return;
        }
        return;
    }
    if (t1 == 3) {
        if (t2 == 0) {
            if (size1 == 8) {
                emit_byte(0x88);
            } else {
                emit_byte(0x89);
            }
            int modrm = (r2 << 3) | reg_to_rm(r1);
            if (v1 == 0) {
                emit_byte(modrm);
            } else if (v1 >= -128 && v1 <= 127) {
                emit_byte(modrm | 0x40);
                emit_byte(v1 & 0xFF);
            } else {
                emit_byte(modrm | 0x80);
                emit_byte(v1 & 0xFF);
                emit_byte((v1 >> 8) & 0xFF);
            }
            return;
        }
        if (t2 == 1) {
            if (size1 == 8) {
                emit_byte(0xC6);
            } else {
                emit_byte(0xC7);
            }
            int modrm = reg_to_rm(r1);
            if (v1 == 0) {
                emit_byte(modrm);
            } else if (v1 >= -128 && v1 <= 127) {
                emit_byte(modrm | 0x40);
                emit_byte(v1 & 0xFF);
            } else {
                emit_byte(modrm | 0x80);
                emit_byte(v1 & 0xFF);
                emit_byte((v1 >> 8) & 0xFF);
            }
            if (size1 == 8) {
                emit_byte(v2 & 0xFF);
            } else {
                emit_byte(v2 & 0xFF);
                emit_byte((v2 >> 8) & 0xFF);
            }
            return;
        }
    }
}

void handle_movsb() {
    emit_byte(0xA4);
}

void handle_movsw() {
    emit_byte(0xA5);
}

/* ``movzx r16, byte [reg+disp]`` — the only form the self-host uses.
   Emits the 0F B6 prefix, then dispatches on op2 type: register
   (modrm 11 dst src) or register+disp memory (mod=00 or 01 disp8,
   rm picked by reg_to_rm).  The direct ``[disp16]`` memory form
   isn't needed and isn't emitted by cc.py, so it's omitted here
   too. */
void handle_movzx() {
    skip_ws();
    int pr = parse_register();
    int reg1 = pr & 0xFF;
    skip_comma();
    int po = parse_operand_c();
    int t2 = (po >> 8) & 0xFF;
    int r2 = po & 0xFF;
    int v2 = parse_operand_value;
    emit_byte(0x0F);
    emit_byte(0xB6);
    if (t2 == 0) {
        emit_byte(0xC0 | (reg1 << 3) | r2);
    } else {
        int modrm = (reg1 << 3) | reg_to_rm(r2);
        if (v2 != 0) {
            emit_byte(modrm | 0x40);
            emit_byte(v2 & 0xFF);
        } else {
            emit_byte(modrm);
        }
    }
}

/* Single-operand arithmetic family (``mul`` / ``neg`` / ``not`` on a
   r8 or r16).  Each handler picks opcode F6 (byte) or F7 (word) based
   on the register's width and ORs the /r field constant (4 for mul,
   3 for neg, 2 for not) into a register-mode ModR/M byte (C0 | (n<<3)
   | rm).  The C bodies stash the parsed AX (AL=reg, AH=size) on the
   stack around the opcode emit; cc.py's bp frame is outside this
   push/pop so the balance stays clean. */
void handle_mul() {
    skip_ws();
    int pr = parse_register();
    int opcode = 0xF7;
    if ((pr >> 8) == 8) {
        opcode = 0xF6;
    }
    emit_byte(opcode);
    emit_byte(0xE0 | (pr & 0xFF));
}

void handle_neg() {
    skip_ws();
    int pr = parse_register();
    int opcode = 0xF7;
    if ((pr >> 8) == 8) {
        opcode = 0xF6;
    }
    emit_byte(opcode);
    emit_byte(0xD8 | (pr & 0xFF));
}

void handle_not() {
    skip_ws();
    int pr = parse_register();
    int opcode = 0xF7;
    if ((pr >> 8) == 8) {
        opcode = 0xF6;
    }
    emit_byte(opcode);
    emit_byte(0xD0 | (pr & 0xFF));
}

/* ``or r, r`` / ``or r, imm`` / ``or r, [disp16]``.  Same /r=1 as
   the retired asm (0xC8 modrm); reg-reg uses 08/09; reg-imm uses
   0C short AL / 83 /1 ib sign-extended / 81 /1 iw / 80 /1 ib;
   reg-[disp16] uses 0A/0B with modrm mod=00 rm=110.  Structurally
   this is handle_xor with /r swapped and one extra op2_type=2
   branch for the direct-memory source form. */
void handle_or() {
    skip_ws();
    int pr = parse_register();
    int reg1 = pr & 0xFF;
    int size1 = (pr >> 8) & 0xFF;
    skip_comma();
    int po = parse_operand_c();
    int t2 = (po >> 8) & 0xFF;
    int r2 = po & 0xFF;
    int v2 = parse_operand_value;
    if (t2 == 0) {
        if (size1 == 8) {
            emit_byte(0x08);
        } else {
            emit_byte(0x09);
        }
        emit_byte(make_modrm_reg_reg_impl(r2, reg1));
    } else if (t2 == 2) {
        if (size1 == 8) {
            emit_byte(0x0A);
        } else {
            emit_byte(0x0B);
        }
        emit_byte((reg1 << 3) | 0x06);
        emit_byte(v2 & 0xFF);
        emit_byte((v2 >> 8) & 0xFF);
    } else if (size1 == 8) {
        if (reg1 == 0) {
            emit_byte(0x0C);
            emit_byte(v2 & 0xFF);
        } else {
            emit_byte(0x80);
            emit_byte(0xC8 | reg1);
            emit_byte(v2 & 0xFF);
        }
    } else if (v2 >= -128 && v2 <= 127) {
        emit_byte(0x83);
        emit_byte(0xC8 | reg1);
        emit_byte(v2 & 0xFF);
    } else {
        emit_byte(0x81);
        emit_byte(0xC8 | reg1);
        emit_byte(v2 & 0xFF);
        emit_byte((v2 >> 8) & 0xFF);
    }
}

/* ``pop`` / ``push`` accept a register (58+reg / 50+reg), segment
   registers ds/es (1F/07 / 1E/06), and (push only) an imm16 with
   short-form sign-extended-imm8 fallback.  The segment-register
   branches peek two bytes of source without calling parse_register,
   then advance SI past the match.  Keeping the asm body faithful
   to the retired version avoids subtle register-allocation
   differences cc.py might introduce if rewritten as C with
   locals. */
void handle_pop() {
    skip_ws();
    if (source_cursor[0] == 'd' && source_cursor[1] == 's') {
        source_cursor = source_cursor + 2;
        emit_byte(0x1F);
    } else if (source_cursor[0] == 'e' && source_cursor[1] == 's') {
        source_cursor = source_cursor + 2;
        emit_byte(0x07);
    } else {
        int pr = parse_register();
        emit_byte(0x58 | (pr & 0xFF));
    }
}

void handle_popa() {
    emit_byte(0x61);
}

void handle_push() {
    skip_ws();
    if (source_cursor[0] == 'd' && source_cursor[1] == 's') {
        source_cursor = source_cursor + 2;
        emit_byte(0x1E);
    } else if (source_cursor[0] == 'e' && source_cursor[1] == 's') {
        source_cursor = source_cursor + 2;
        emit_byte(0x06);
    } else {
        char *saved = source_cursor;
        int pr = parse_register();
        if (pr >= 0) {
            emit_byte(0x50 | (pr & 0xFF));
        } else {
            source_cursor = saved;
            int value = resolve_value();
            if (value >= -128 && value <= 127) {
                emit_byte(0x6A);
                emit_byte(value & 0xFF);
            } else {
                emit_byte(0x68);
                emit_byte(value & 0xFF);
                emit_byte((value >> 8) & 0xFF);
            }
        }
    }
}

void handle_pusha() {
    emit_byte(0x60);
}

/* ``rep`` / ``repne`` prefixes — emit the prefix byte then recurse
   into parse_mnemonic so the following mnemonic's handler appends
   its own opcode(s).  ``emit_byte`` preserves SI across its body so
   the subsequent ``skip_ws`` / ``parse_mnemonic`` still see a live
   ``source_cursor``.  Both handlers' bodies are three statement-
   level Calls, which qualifies them for ``frameless_calls``
   elide_frame — no bp frame, ``mov ax, OP ; call emit_byte ; call
   skip_ws ; call parse_mnemonic ; ret`` exactly. */
void handle_rep() {
    emit_byte(0xF3);
    skip_ws();
    parse_mnemonic();
}

void handle_repne() {
    emit_byte(0xF2);
    skip_ws();
    parse_mnemonic();
}

void handle_ret() {
    emit_byte(0xC3);
}

/* ``sbb word [disp16], imm8`` — the only form the self-host needs
   (TCP checksum carry fold).  Requires the ``word`` keyword; the
   match_word gate restores SI on failure so the fall-through to
   abort_unknown prints the original token.  The success path jumps
   past the abort tail so the epilogue closes cc.py's bp frame; the
   abort paths jmp out to abort_unknown (which never returns). */
void handle_sbb() {
    skip_ws();
    if (match_word(STR_WORD) == 0) {
        abort_unknown();
    }
    skip_ws();
    if (source_cursor[0] != '[') {
        abort_unknown();
    }
    source_cursor = source_cursor + 1;
    int disp = resolve_value();
    if (source_cursor[0] != ']') {
        abort_unknown();
    }
    source_cursor = source_cursor + 1;
    skip_comma();
    int imm = resolve_value();
    emit_byte(0x83);
    emit_byte(0x1E);
    emit_byte(disp & 0xFF);
    emit_byte((disp >> 8) & 0xFF);
    emit_byte(imm & 0xFF);
}

void handle_scasb() {
    emit_byte(0xAE);
}

/* ``shl`` / ``shr`` with r8/r16 destination and either a constant 1
   (short D0/D1 form) or imm8 shift count (C0/C1 imm8 form).  The two
   handlers differ only in the /r field constant: shl=4 (0xE0), shr=5
   (0xE8). */
void handle_shl() {
    skip_ws();
    int pr = parse_register();
    skip_comma();
    int count = resolve_value();
    int reg = pr & 0xFF;
    int size = (pr >> 8) & 0xFF;
    if (count == 1) {
        if (size == 8) {
            emit_byte(0xD0);
        } else {
            emit_byte(0xD1);
        }
        emit_byte(0xE0 | reg);
    } else {
        if (size == 8) {
            emit_byte(0xC0);
        } else {
            emit_byte(0xC1);
        }
        emit_byte(0xE0 | reg);
        emit_byte(count);
    }
}

void handle_shr() {
    skip_ws();
    int pr = parse_register();
    skip_comma();
    int count = resolve_value();
    int reg = pr & 0xFF;
    int size = (pr >> 8) & 0xFF;
    if (count == 1) {
        if (size == 8) {
            emit_byte(0xD0);
        } else {
            emit_byte(0xD1);
        }
        emit_byte(0xE8 | reg);
    } else {
        if (size == 8) {
            emit_byte(0xC0);
        } else {
            emit_byte(0xC1);
        }
        emit_byte(0xE8 | reg);
        emit_byte(count);
    }
}

void handle_stc() {
    emit_byte(0xF9);
}

void handle_stosb() {
    emit_byte(0xAA);
}

void handle_stosw() {
    emit_byte(0xAB);
}

/* ``sub`` has four operand shapes — ``[disp16], r16`` (via
   mem_op_reg_emit with opcode 29), ``word [disp16], imm16`` (a
   dedicated 81 /5 iw path), reg/reg (28/29 modrm), and reg/imm
   with the /r=5 constant (0xE8).  reg-reg / reg-imm dispatch
   mirrors handle_xor but without the AX / AL short forms (sub
   has 2C/2D short forms but the self-host never emits them).
   Memory-destination call into mem_op_reg_emit uses ``call`` +
   terminal ``.hsu_end:`` so cc.py's bp frame closes. */
void handle_sub() {
    skip_ws();
    if (source_cursor[0] == '[') {
        mem_op_reg_emit(0x29);
        return;
    }
    if (match_word(STR_WORD)) {
        skip_ws();
        if (source_cursor[0] != '[') {
            abort_unknown();
        }
        source_cursor = source_cursor + 1;
        int disp = resolve_value();
        if (source_cursor[0] != ']') {
            abort_unknown();
        }
        source_cursor = source_cursor + 1;
        skip_comma();
        int imm = resolve_value();
        emit_byte(0x81);
        emit_byte(0x2E);
        emit_byte(disp & 0xFF);
        emit_byte((disp >> 8) & 0xFF);
        emit_byte(imm & 0xFF);
        emit_byte((imm >> 8) & 0xFF);
        return;
    }
    int pr = parse_register();
    int reg1 = pr & 0xFF;
    int size1 = (pr >> 8) & 0xFF;
    skip_comma();
    int po = parse_operand_c();
    int t2 = (po >> 8) & 0xFF;
    int r2 = po & 0xFF;
    int v2 = parse_operand_value;
    if (t2 == 0) {
        if (size1 == 8) {
            emit_byte(0x28);
        } else {
            emit_byte(0x29);
        }
        emit_byte(make_modrm_reg_reg_impl(r2, reg1));
    } else if (t2 == 2) {
        if (size1 == 8) {
            emit_byte(0x2A);
        } else {
            emit_byte(0x2B);
        }
        emit_byte((reg1 << 3) | 0x06);
        emit_byte(v2 & 0xFF);
        emit_byte((v2 >> 8) & 0xFF);
    } else if (t2 == 3) {
        if (size1 == 8) {
            emit_byte(0x2A);
        } else {
            emit_byte(0x2B);
        }
        emit_byte(reg_to_rm(r2) | 0x40 | (reg1 << 3));
        emit_byte(v2 & 0xFF);
    } else if (size1 == 8) {
        if (reg1 == 0) {
            emit_byte(0x2C);
        } else {
            emit_byte(0x80);
            emit_byte(0xE8 | reg1);
        }
        emit_byte(v2 & 0xFF);
    } else if (v2 >= -128 && v2 <= 127) {
        emit_byte(0x83);
        emit_byte(0xE8 | reg1);
        emit_byte(v2 & 0xFF);
    } else {
        emit_byte(0x81);
        emit_byte(0xE8 | reg1);
        emit_byte(v2 & 0xFF);
        emit_byte((v2 >> 8) & 0xFF);
    }
}

/* ``test r, r`` / ``test r, imm`` / ``test byte [mem], imm8`` —
   the three forms self-host needs.  parse_operand seeds op1; the
   second operand branches on parse_register success (register →
   84/85 r-r) vs failure (immediate → A8/A9 short for AL/AX, else
   F6/F7 modrm).  Memory destination uses F6 /0 with the op1 info
   already parsed (disp8, disp16, or bare [reg]). */
void handle_test() {
    skip_ws();
    int po = parse_operand_c();
    int t1 = (po >> 8) & 0xFF;
    int r1 = po & 0xFF;
    int v1 = parse_operand_value;
    int size1 = op1_size;
    skip_comma();
    if (t1 == 0) {
        skip_ws();
        int pr2 = parse_register();
        if (pr2 >= 0) {
            if (size1 == 8) {
                emit_byte(0x84);
            } else {
                emit_byte(0x85);
            }
            emit_byte(make_modrm_reg_reg_impl(pr2 & 0xFF, r1));
        } else {
            int imm = resolve_value();
            if (size1 == 8) {
                if (r1 == 0) {
                    emit_byte(0xA8);
                } else {
                    emit_byte(0xF6);
                    emit_byte(0xC0 | r1);
                }
                emit_byte(imm & 0xFF);
            } else {
                if (r1 == 0) {
                    emit_byte(0xA9);
                } else {
                    emit_byte(0xF7);
                    emit_byte(0xC0 | r1);
                }
                emit_byte(imm & 0xFF);
                emit_byte((imm >> 8) & 0xFF);
            }
        }
    } else {
        int imm = resolve_value();
        emit_byte(0xF6);
        if (t1 == 2) {
            emit_byte(0x06);
            emit_byte(v1 & 0xFF);
            emit_byte((v1 >> 8) & 0xFF);
        } else {
            int rm = reg_to_rm(r1);
            if (v1 != 0) {
                emit_byte(0x40 | rm);
                emit_byte(v1 & 0xFF);
            } else {
                emit_byte(rm);
            }
        }
        emit_byte(imm & 0xFF);
    }
}

/* ``handle_unknown_word`` — the parse_mnemonic fallback for bare
   labels (NASM accepts labels without colons, e.g., ``USAGE db ...``).
   Walks SI past the alphanumeric span, null-terminates the word in
   place, adds the symbol on pass 1 (or validates it on pass 2) with
   the ``.``-prefix local-scope distinction, advances SI past the
   null, and reinvokes parse_directive on whatever remains.  Uses
   ``push si`` / ``pop si`` around symbol_set / symbol_lookup so SI
   survives those calls' own register use.  ``handle_unknown_word``
   is reached via ``jmp`` from parse_mnemonic, so the whole body
   (prologue, work, epilogue) runs in one call frame — cc.py's
   ``push bp / pop bp / ret`` wraps cleanly. */
void handle_unknown_word() {
    char *name_start = source_cursor;
    while (source_cursor[0] != ' ' && source_cursor[0] != '\t' && source_cursor[0] != '\0') {
        source_cursor = source_cursor + 1;
    }
    if (source_cursor[0] == '\0') {
        return;
    }
    char *end_pos = source_cursor;
    source_cursor[0] = '\0';
    int is_local = 0;
    if (name_start[0] == '.') {
        is_local = 1;
    }
    source_cursor = name_start;
    if (pass == 1) {
        if (is_local) {
            symbol_set_local(current_address);
        } else {
            symbol_set_global(current_address);
            global_scope = last_symbol_index;
        }
    } else if (is_local == 0) {
        symbol_lookup_c(0xFFFF);
        if (last_symbol_index != 0xFFFF) {
            global_scope = last_symbol_index;
        }
    }
    source_cursor = end_pos + 1;
    skip_ws();
    if (source_cursor[0] != '\0') {
        parse_directive();
    }
}

/* ``xchg r, r`` — uses the 90h+reg short form when one operand is
   AX (16-bit); otherwise emits the 86 / 87 r/m form with the NASM
   operand-order swap (first operand in reg field, second in rm). */
void handle_xchg() {
    skip_ws();
    int pr1 = parse_register();
    int reg1 = pr1 & 0xFF;
    int size1 = (pr1 >> 8) & 0xFF;
    skip_comma();
    int pr2 = parse_register();
    int reg2 = pr2 & 0xFF;
    if (size1 != 8 && reg1 == 0) {
        emit_byte(0x90 | reg2);
    } else if (size1 != 8 && reg2 == 0) {
        emit_byte(0x90 | reg1);
    } else if (size1 == 8) {
        emit_byte(0x86);
        emit_byte(make_modrm_reg_reg_impl(reg1, reg2));
    } else {
        emit_byte(0x87);
        emit_byte(make_modrm_reg_reg_impl(reg1, reg2));
    }
}

/* ``xor r, r`` / ``xor r, imm`` — same shape as handle_or / and.
   reg-reg uses 30/31 modrm; reg-imm prefers 34/35 short AX/AL
   forms, sign-extended ``83 /6 ib`` for r16 when the immediate
   fits in -128..127, else ``81 /6 iw`` (or ``80 /6 ib`` for r8). */
void handle_xor() {
    skip_ws();
    int pr = parse_register();
    int reg1 = pr & 0xFF;
    int size1 = (pr >> 8) & 0xFF;
    skip_comma();
    int po = parse_operand_c();
    int t2 = (po >> 8) & 0xFF;
    int r2 = po & 0xFF;
    int v2 = parse_operand_value;
    if (t2 == 0) {
        if (size1 == 8) {
            emit_byte(0x30);
        } else {
            emit_byte(0x31);
        }
        emit_byte(make_modrm_reg_reg_impl(r2, reg1));
    } else if (size1 == 8) {
        if (reg1 == 0) {
            emit_byte(0x34);
            emit_byte(v2 & 0xFF);
        } else {
            emit_byte(0x80);
            emit_byte(0xF0 | reg1);
            emit_byte(v2 & 0xFF);
        }
    } else if (v2 >= -128 && v2 <= 127) {
        emit_byte(0x83);
        emit_byte(0xF0 | reg1);
        emit_byte(v2 & 0xFF);
    } else {
        emit_byte(0x81);
        emit_byte(0xF0 | reg1);
        emit_byte(v2 & 0xFF);
        emit_byte((v2 >> 8) & 0xFF);
    }
}

/* Convert an ASCII hex digit to its numeric value.  Returns 0..15
   on success, ``-1`` on a non-hex byte.  ``regparm(1)`` fastcall —
   the byte arrives in AX (caller zero-extends from AL before the
   call so AH is clean).  Callers check ``ax < 0`` (``js`` on the
   sign bit) to detect the not-hex case; the ``-1`` sentinel
   replaces the CF-return ABI the retired asm used.  4 call sites
   in ``parse_db``'s ``\x..`` escape handler and ``parse_number``'s
   hex-prefix / hex-suffix loops. */
__attribute__((regparm(1)))
int hex_digit(int c) {
    if (c >= 48 && c <= 57) {
        return c - 48;      /* '0'..'9' → 0..9 */
    }
    if (c >= 65 && c <= 70) {
        return c - 55;      /* 'A'..'F' → 10..15 */
    }
    if (c >= 97 && c <= 102) {
        return c - 87;      /* 'a'..'f' → 10..15 */
    }
    return -1;
}

/* Pop the include stack: close the included file, restore the
   parent file's fd / buffer / position / valid fields, and copy the
   saved SOURCE_BUFFER contents back.  Called from do_pass (via
   ``call include_pop`` in the pass-loop inline asm) when read_line
   hits EOF while include_depth > 0.  The original inline-asm label
   also preserved AX/BX/CX/SI/DI, but the sole caller jumps to
   ``.line_loop`` → ``call read_line`` which reloads its own
   registers, so the C version only guards ES (callers rely on ES
   staying at SYMBOL_SEGMENT across the rep movsw that needs ES=DS).
   Keeping every asm() block SP-balanced is required because cc.py
   wraps each inline block with ``push dx / pop dx`` to preserve the
   local pinned to DX. */
/* Close the current ``source_fd`` via the ES-safe ``syscall`` wrapper.
   Factored so ``do_pass`` and ``include_pop`` share one inline-asm
   block instead of each open-coding the 3-instruction SYS_IO_CLOSE
   sequence.  Inlined at both call sites via always_inline. */
__attribute__((always_inline))
void close_source() {
    asm("mov bx, [_g_source_fd]\n"
        "mov ah, SYS_IO_CLOSE\n"
        "call syscall");
}

/* Open ``path`` read-only via SYS_IO_OPEN (through the ES-safe
   ``syscall`` wrapper).  Returns the fd on success, or -1 on error
   (CF set by the syscall).  Takes the path pointer via regparm(1)
   AX; the body threads it into SI for the syscall.  Inlined at both
   call sites via always_inline; the internal ``.ofr_ok`` label gets
   per-site uniquified. */
__attribute__((regparm(1)))
__attribute__((always_inline))
int open_file_ro(char *path) {
    asm("mov si, ax\n"
        "mov al, O_RDONLY\n"
        "mov ah, SYS_IO_OPEN\n"
        "call syscall\n"
        "jnc .ofr_ok\n"
        "mov ax, -1\n"
        ".ofr_ok:");
}

void include_pop() {
    close_source();
    source_fd = include_save_fd;
    source_buffer_position = include_save_position;
    source_buffer_valid = include_save_valid;
    asm("push es\n"
        "push ds\npop es\n"
        "mov si, [_g_include_source_save]\n"
        "mov di, SOURCE_BUFFER\n"
        "mov cx, 256\n"
        "cld\nrep movsw\n"
        "pop es");
    include_depth = include_depth - 1;
}

/* Push the include stack: save the current file's fd / buffer
   state, stash a copy of the 512-byte SOURCE_BUFFER, build
   ``include_path = source_prefix + <name>``, and open the result
   via the ES-safe syscall wrapper.  On success source_fd points at
   the included file and include_depth is bumped; on failure
   error_flag is raised so the enclosing pass iteration reports it.
   SI-on-entry holds the raw filename pointer taken straight from
   the source line; the first instruction stashes it into
   ``include_push_arg`` before cc.py's codegen gets a chance to
   clobber SI. */
void include_push() {
    include_push_arg = source_cursor;
    include_save_fd = source_fd;
    include_save_position = source_buffer_position;
    include_save_valid = source_buffer_valid;
    asm("push es\n"
        "push ds\npop es\n"
        "mov si, SOURCE_BUFFER\n"
        "mov di, [_g_include_source_save]\n"
        "mov cx, 256\n"
        "cld\nrep movsw\n"
        "pop es");
    int i = 0;
    int j = 0;
    while (source_prefix[i] != '\0') {
        include_path[j] = source_prefix[i];
        i = i + 1;
        j = j + 1;
    }
    int k = 0;
    while (include_push_arg[k] != '\0') {
        include_path[j] = include_push_arg[k];
        j = j + 1;
        k = k + 1;
    }
    include_path[j] = '\0';
    source_fd = open_file_ro(include_path);
    if (source_fd == -1) {
        error_flag = 1;
        return;
    }
    source_buffer_position = 0;
    source_buffer_valid = 0;
    include_depth = include_depth + 1;
}

/* Naked-asm helper that invokes ``SYS_IO_READ`` on ``source_fd``
   filling ``SOURCE_BUFFER`` with up to 512 bytes; uses the ES-safe
   ``syscall`` wrapper so ES=SYMBOL_SEGMENT survives the ``int 30h``.
   Returns AX = bytes read, or -1 on error.  Factored as its own
   function so ``load_src_sector``'s C body can receive the result
   via the standard return-in-AX convention — cc.py has no syntax
   for binding an inline ``call syscall``'s AX return to a C local. */
__attribute__((always_inline))
int read_source_sector() {
    asm("mov bx, [_g_source_fd]\n"
        "mov di, SOURCE_BUFFER\n"
        "mov cx, 512\n"
        "mov ah, SYS_IO_READ\n"
        "call syscall");
}

/* Refill SOURCE_BUFFER from the current source_fd via
   ``read_source_sector``.  Returns 0 when a new chunk lands in the
   buffer (position / valid cursors reset) or 1 on EOF (zero-byte
   read) or I/O error (-1 back from the syscall).  Sole caller is
   ``read_line``. */
int load_src_sector() {
    int bytes = read_source_sector();
    if (bytes == 0) {
        return 1;
    }
    if (bytes == -1) {
        return 1;
    }
    source_buffer_valid = bytes;
    source_buffer_position = 0;
    return 0;
}

/* Build a register/register ModR/M byte.  ``regparm(1)`` — reg in
   AX, rm on stack; returns ``0xC0 | (reg << 3) | rm`` in AX.
   Previous legacy ``make_modrm_reg_reg`` thunk (AL/BL in, modrm out)
   retired with its ~7 inline-asm callers. */
__attribute__((regparm(1)))
int make_modrm_reg_reg_impl(int reg, int rm) {
    reg = reg & 0xFF;
    rm = rm & 0xFF;
    return 0xC0 | (reg << 3) | rm;
}

/* Case-insensitive match of ``keyword`` (null-terminated, all
   lowercase in the assembler's keyword / mnemonic tables) against
   the token at ``source_cursor``, followed by an identifier-boundary
   check on the next source character (must not be alphanumeric
   or ``_``).  On match ``source_cursor`` advances past the keyword
   and the carry_return signals success (CF clear); on miss
   ``source_cursor`` rewinds to its entry value and the carry_return
   signals failure (CF set).  Since keyword strings are always
   lowercase, only the source side gets case-folded.  Used by
   parse_operand (``byte`` / ``word`` prefixes), parse_directive
   (``%assign`` / ``%include`` / ``org`` / ``times`` / ``db`` /
   ``dw`` / ``dd``), parse_mnemonic (instruction dispatch), and
   the callers that specialize on ``STR_EQU`` / ``STR_SHORT`` /
   ``STR_WORD``. */
__attribute__((regparm(1)))
__attribute__((carry_return))
int match_word(char *keyword) {
    char *saved = source_cursor;
    while (keyword[0] != '\0') {
        char s = source_cursor[0];
        if (s >= 'A' && s <= 'Z') {
            s = s + 32;
        }
        if (s != keyword[0]) {
            source_cursor = saved;
            return 0;
        }
        source_cursor = source_cursor + 1;
        keyword = keyword + 1;
    }
    char s = source_cursor[0];
    if ((s >= 'a' && s <= 'z')
            || (s >= 'A' && s <= 'Z')
            || (s >= '0' && s <= '9')
            || s == '_') {
        source_cursor = saved;
        return 0;
    }
    return 1;
}

/* Shared helper for the ``<op> [disp16], r16`` memory-destination
   form (called by handle_add / handle_and / handle_sub).  Opcode
   arrives via ``regparm(1)`` AX; source cursor is at ``[``.  Emits
   ``<opcode> <modrm(mod=00, reg=src, rm=110)> <disp16>``.  Bad
   structure (missing ``]`` or a non-register source) calls
   abort_unknown (which never returns). */
__attribute__((regparm(1)))
void mem_op_reg_emit(int opcode) {
    source_cursor = source_cursor + 1;
    int disp = resolve_value();
    if (source_cursor[0] != ']') {
        abort_unknown();
    }
    source_cursor = source_cursor + 1;
    skip_comma();
    int pr = parse_register();
    if (pr < 0) {
        abort_unknown();
    }
    emit_byte(opcode);
    emit_byte(((pr & 0xFF) << 3) | 0x06);
    emit_byte(disp & 0xFF);
    emit_byte((disp >> 8) & 0xFF);
}

/* Parse ``db`` operands (comma-separated mix of numbers, symbols,
   char/backtick strings).  ``source_cursor`` starts at the first
   operand and advances past each byte; ``emit_byte`` handles the
   pass-1 address bump and pass-2 buffer write.  Backtick strings
   support the ``\n`` / ``\0`` / ``\t`` / ``\r`` / ``\e`` / ``\\``
   / ``\x??`` escape set; any other ``\c`` emits a literal backslash
   followed by ``c`` (matching NASM's own unknown-escape behaviour).
   Single-quoted multi-byte strings (``'foo'``) are copied verbatim;
   single-char ``'c'`` literals fall through to resolve_value since
   the distinguishing byte at offset +2 tells the two apart. */
void parse_db() {
    while (1) {
        skip_ws();
        if (source_cursor[0] == '\0' || source_cursor[0] == ';') {
            return;
        }
        if (source_cursor[0] == '`') {
            source_cursor = source_cursor + 1;
            while (1) {
                char c = source_cursor[0];
                if (c == '`') {
                    source_cursor = source_cursor + 1;
                    break;
                }
                if (c == '\0') {
                    return;
                }
                if (c == '\\') {
                    source_cursor = source_cursor + 1;
                    char esc = source_cursor[0];
                    if (esc == 'n') {
                        emit_byte('\n');
                    } else if (esc == '0') {
                        emit_byte('\0');
                    } else if (esc == 't') {
                        emit_byte('\t');
                    } else if (esc == 'r') {
                        emit_byte('\r');
                    } else if (esc == 'e') {
                        emit_byte('\e');
                    } else if (esc == '\\') {
                        emit_byte('\\');
                    } else if (esc == 'x') {
                        source_cursor = source_cursor + 1;
                        int hi = hex_digit(source_cursor[0]);
                        source_cursor = source_cursor + 1;
                        int lo = hex_digit(source_cursor[0]);
                        emit_byte((hi << 4) | lo);
                    } else {
                        emit_byte('\\');
                        emit_byte(esc);
                    }
                    source_cursor = source_cursor + 1;
                } else {
                    emit_byte(c);
                    source_cursor = source_cursor + 1;
                }
            }
        } else if (source_cursor[0] == '\'' && source_cursor[2] != '\'') {
            source_cursor = source_cursor + 1;
            while (1) {
                char c = source_cursor[0];
                if (c == '\'') {
                    source_cursor = source_cursor + 1;
                    break;
                }
                if (c == '\0') {
                    return;
                }
                emit_byte(c);
                source_cursor = source_cursor + 1;
            }
        } else {
            int v = resolve_value();
            emit_byte(v);
        }
        skip_ws();
        if (source_cursor[0] != ',') {
            return;
        }
        source_cursor = source_cursor + 1;
    }
}

/* Directive dispatcher: ``%assign`` / ``%define`` (both bind NAME
   to a value — we treat them as the same since we don't do macro
   text substitution), ``%include`` (via include_push), ``org``
   (sets org_value + current_address), ``times N db ...`` (repeats
   the inner parse_db), ``db`` / ``dw`` / ``dd``, or falls through
   to parse_mnemonic for instructions.  Each arm uses match_word
   with the shared STR_* constants (still in the file-scope asm
   block's data section). */
void parse_directive() {
    if (source_cursor[0] == '%') {
        source_cursor = source_cursor + 1;
        int matched_assign = 0;
        if (match_word(STR_ASSIGN)) {
            matched_assign = 1;
        } else if (match_word(STR_DEFINE)) {
            matched_assign = 1;
        }
        if (matched_assign) {
            skip_ws();
            char *name = source_cursor;
            while (source_cursor[0] != ' ' && source_cursor[0] != '\t' && source_cursor[0] != '\0') {
                source_cursor = source_cursor + 1;
            }
            if (source_cursor[0] == '\0') {
                return;
            }
            source_cursor[0] = '\0';
            source_cursor = source_cursor + 1;
            skip_ws();
            int value = resolve_value();
            if (pass == 1) {
                source_cursor = name;
                symbol_add_constant_c(value);
            }
            return;
        }
        if (match_word(STR_INCLUDE) == 0) {
            return;
        }
        skip_ws();
        if (source_cursor[0] != '"') {
            return;
        }
        source_cursor = source_cursor + 1;
        char *fname = source_cursor;
        while (source_cursor[0] != '"') {
            if (source_cursor[0] == '\0') {
                return;
            }
            source_cursor = source_cursor + 1;
        }
        source_cursor[0] = '\0';
        source_cursor = fname;
        include_push();
        return;
    }
    if (match_word(STR_ORG)) {
        skip_ws();
        int addr = resolve_value();
        org_value = addr;
        current_address = addr;
        return;
    }
    if (match_word(STR_TIMES)) {
        skip_ws();
        int count = resolve_value();
        skip_ws();
        if (match_word(STR_DB) == 0) {
            return;
        }
        skip_ws();
        char *saved = source_cursor;
        while (count != 0) {
            source_cursor = saved;
            parse_db();
            count = count - 1;
        }
        return;
    }
    if (match_word(STR_DB)) {
        skip_ws();
        parse_db();
        return;
    }
    if (match_word(STR_DW)) {
        skip_ws();
        while (1) {
            int v = resolve_value();
            emit_byte(v & 0xFF);
            emit_byte((v >> 8) & 0xFF);
            skip_ws();
            if (source_cursor[0] != ',') {
                return;
            }
            source_cursor = source_cursor + 1;
            skip_ws();
        }
    }
    if (match_word(STR_DD)) {
        skip_ws();
        while (1) {
            int v = resolve_value();
            emit_byte(v & 0xFF);
            emit_byte((v >> 8) & 0xFF);
            emit_byte(0);
            emit_byte(0);
            skip_ws();
            if (source_cursor[0] != ',') {
                return;
            }
            source_cursor = source_cursor + 1;
            skip_ws();
        }
    }
    parse_mnemonic();
}

/* Top-level line dispatcher.  Starts at LINE_BUFFER, strips
   leading whitespace, returns if the line is empty or a ``;``
   comment.  Recognises three label shapes: ``%`` directive (no
   label scan), ``NAME equ VALUE`` (bound as an %assign),
   ``LABEL:`` (colon-terminated; optionally followed by a
   directive/mnemonic after the colon).  Updates global_scope to
   the latest global label's symbol index in both passes so local
   (``.``-prefixed) labels are scoped correctly.  Anything else
   falls through to parse_directive. */
void parse_line() {
    source_cursor = line_buffer;
    skip_ws();
    if (source_cursor[0] == '\0' || source_cursor[0] == ';') {
        return;
    }
    if (source_cursor[0] == '%') {
        parse_directive();
        return;
    }
    char *label_start = source_cursor;
    while (1) {
        char c = source_cursor[0];
        if (c == '\0') {
            source_cursor = label_start;
            parse_directive();
            return;
        }
        if (c == ':') {
            /* ``LABEL:`` — null-terminate the name, add or refresh the
               symbol table entry, restore the colon, and let
               parse_directive take a crack at any trailing content. */
            char *colon_pos = source_cursor;
            source_cursor[0] = '\0';
            int is_local = 0;
            if (label_start[0] == '.') {
                is_local = 1;
            }
            source_cursor = label_start;
            if (pass == 1) {
                if (is_local) {
                    symbol_set_local(current_address);
                } else {
                    symbol_set_global(current_address);
                    global_scope = last_symbol_index;
                }
            } else if (is_local == 0) {
                symbol_lookup_c(0xFFFF);
                if (last_symbol_index != 0xFFFF) {
                    global_scope = last_symbol_index;
                }
            }
            colon_pos[0] = ':';
            source_cursor = colon_pos + 1;
            skip_ws();
            if (source_cursor[0] == '\0' || source_cursor[0] == ';') {
                return;
            }
            parse_directive();
            return;
        }
        if (c == ' ' || c == '\t') {
            /* ``NAME equ VALUE`` — whitespace after the name is the
               entry point to the equ-or-not decision.  If it's not equ,
               fall back to parse_directive with source_cursor rewound
               to label_start (so directives like ``db .. equ`` unknown
               mnemonics still have their original token visible). */
            char *space_pos = source_cursor;
            skip_ws();
            if (match_word(STR_EQU) == 0) {
                source_cursor = label_start;
                parse_directive();
                return;
            }
            space_pos[0] = '\0';
            skip_ws();
            int value = resolve_value();
            if (pass == 1) {
                source_cursor = label_start;
                symbol_add_constant_c(value);
            }
            space_pos[0] = ' ';
            return;
        }
        source_cursor = source_cursor + 1;
    }
}

/* Fetch the keyword pointer from ``mnemonic_table[index]`` (each
   entry is 4 bytes: keyword-pointer + handler-pointer, terminated
   by a 2-byte zero).  Returns ``NULL`` when the caller has walked
   past the terminator.  Compact naked-asm body because cc.py has
   no syntax for reading a 16-bit pointer out of a packed data
   table; factoring this out keeps ``parse_mnemonic`` pure C. */
__attribute__((regparm(1)))
__attribute__((always_inline))
char *mnemonic_keyword_at(int index) {
    asm("shl ax, 2\n"
        "mov bx, mnemonic_table\n"
        "add bx, ax\n"
        "mov ax, [bx]");
}

/* Invoke the handler pointer in ``mnemonic_table[index]`` (at
   offset +2 of the 4-byte entry).  The indirect ``call [bx+2]``
   has no C analogue cc.py emits, so this tiny wrapper pairs with
   ``mnemonic_keyword_at`` to let ``parse_mnemonic`` stay pure C.
   Inlined at its single call site — no body overhead. */
__attribute__((regparm(1)))
__attribute__((always_inline))
void mnemonic_dispatch_at(int index) {
    asm("shl ax, 2\n"
        "mov bx, mnemonic_table\n"
        "add bx, ax\n"
        "call [bx+2]");
}

/* Instruction dispatcher: linear scan over ``mnemonic_table``
   trying each keyword against the source cursor via ``match_word``.
   On match, invoke the matching handler.  Walking past the
   2-byte zero terminator (``mnemonic_keyword_at`` returns NULL)
   falls through to ``handle_unknown_word`` so bare labels
   (``USAGE db ...`` without a colon) still reach their
   symbol-table branch. */
void parse_mnemonic() {
    int index = 0;
    while (1) {
        char *keyword = mnemonic_keyword_at(index);
        if (keyword == NULL) {
            handle_unknown_word();
            return;
        }
        if (match_word(keyword)) {
            mnemonic_dispatch_at(index);
            return;
        }
        index = index + 1;
    }
}

/* Parse a decimal, hex-prefix (``0x``), or hex-suffix (``h``)
   number at ``source_cursor``.  NASM lets both forms coexist in a
   single program (and even in a single expression), so the handler
   peeks ahead to decide: if any digit before the next non-hex char
   is ``h`` the whole thing is hex-suffixed, else decimal.  Result
   returns via AX; ``source_cursor`` advances past the number.
   Sole caller is ``resolve_value`` (after a digit-prefix test), so
   no character-literal path here.  All comparisons against ASCII
   classes use numeric decimals because cc.py's
   ``validate_comparison_types`` rejects ``int`` vs ``Char``
   mismatch. */
int parse_number() {
    int value = 0;
    char c;
    int d;
    /* Hex prefix: 0x.. or 0X.. */
    c = source_cursor[0];
    if (c == '0') {
        c = source_cursor[1];
        if (c == 'x' || c == 'X') {
            source_cursor = source_cursor + 2;
            while (1) {
                d = hex_digit(source_cursor[0]);
                if (d < 0) {
                    return value;
                }
                value = (value << 4) | d;
                source_cursor = source_cursor + 1;
            }
        }
    }
    /* Peek ahead: scan digits / hex chars, if the terminator is ``h`` or
       ``H`` the number is hex-suffixed; otherwise it's decimal. */
    char *saved = source_cursor;
    int is_hex = 0;
    while (1) {
        c = source_cursor[0];
        if (c >= '0' && c <= '9') {
            source_cursor = source_cursor + 1;
        } else if (c >= 'A' && c <= 'F') {
            source_cursor = source_cursor + 1;
        } else if (c >= 'a' && c <= 'f') {
            source_cursor = source_cursor + 1;
        } else if (c == 'h' || c == 'H') {
            is_hex = 1;
            break;
        } else {
            break;
        }
    }
    source_cursor = saved;
    if (is_hex) {
        while (1) {
            c = source_cursor[0];
            if (c == 'h' || c == 'H') {
                source_cursor = source_cursor + 1;
                return value;
            }
            d = hex_digit(c);
            if (d < 0) {
                c = source_cursor[0];
                if (c == 'h' || c == 'H') {
                    source_cursor = source_cursor + 1;
                }
                return value;
            }
            value = (value << 4) | d;
            source_cursor = source_cursor + 1;
        }
    }
    /* Decimal */
    while (1) {
        c = source_cursor[0];
        if (c < '0' || c > '9') {
            return value;
        }
        value = value * 10 + (c - '0');
        source_cursor = source_cursor + 1;
    }
}

/* Parse a single operand — register, immediate, direct memory
   (``[disp16]``), or indexed memory (``[reg]`` / ``[reg+disp]`` /
   ``[disp+reg]``).  Recognises ``byte`` / ``word`` size prefixes
   (emit op1_size = 8 / 16) and the ``[es:...]`` segment override
   (emits 0x26 before returning so the next emit continues the
   instruction).  The ``[disp + reg]`` dialect NASM accepts is
   detected by scanning backwards from ``]`` for a trailing
   register preceded by ``+``; it's rewritten in-place to
   ``[disp]`` for resolve_value (with the ``+`` restored
   afterwards).  Returns AH = type (0=reg, 1=imm, 2=mem_direct,
   3=mem_bx_disp); AL = register number (for reg / mem_bx_disp);
   DX = value (imm or disp).  Updates [op1_size] on register /
   byte/word-prefix paths. */
int parse_operand() {
    skip_ws();
    /* ``byte`` / ``word`` size prefix — match_word already rewinds
       ``source_cursor`` on miss, so no manual backtrack needed. */
    if (match_word(STR_BYTE)) {
        op1_size = 8;
        skip_ws();
    } else if (match_word(STR_WORD)) {
        op1_size = 16;
        skip_ws();
    }
    if (source_cursor[0] != '[') {
        /* Register or immediate. */
        int pr = parse_register();
        if (pr >= 0) {
            op1_size = (pr >> 8) & 0xFF;
            return pr & 0xFF;           /* type=0 (reg), reg in low byte */
        }
        int imm = resolve_value();
        parse_operand_value = imm;
        return 1 << 8;                  /* type=1 (imm) */
    }
    /* Memory operand starting at ``[``. */
    source_cursor = source_cursor + 1;
    skip_ws();
    /* ``[es:...]`` segment override: emit 0x26 prefix, skip past. */
    if (source_cursor[0] == 'e' && source_cursor[1] == 's' && source_cursor[2] == ':') {
        emit_byte(0x26);
        source_cursor = source_cursor + 3;
        skip_ws();
    }
    /* Try ``[reg...]`` form (register first inside brackets). */
    int pr = parse_register();
    if (pr >= 0) {
        int reg = pr & 0xFF;
        skip_ws();
        int disp = 0;
        if (source_cursor[0] == '+') {
            source_cursor = source_cursor + 1;
            skip_ws();
            disp = resolve_value();
        } else if (source_cursor[0] == '-') {
            source_cursor = source_cursor + 1;
            skip_ws();
            int v = resolve_value();
            disp = 0 - v;
        }
        skip_ws();
        if (source_cursor[0] == ']') {
            source_cursor = source_cursor + 1;
        }
        parse_operand_value = disp;
        return (3 << 8) | reg;          /* type=3 (reg+disp) */
    }
    /* Not ``[reg...]``: could be ``[disp]`` or ``[disp+reg]``.
       Scan forward to ``]`` (or NUL), then scan backwards over
       trailing whitespace to find the end of the bracket contents. */
    char *bracket_start = source_cursor;
    char *close = source_cursor;
    while (close[0] != ']' && close[0] != '\0') {
        close = close + 1;
    }
    char *end = close;
    while (end > bracket_start) {
        char *prev = end - 1;
        if (prev[0] != ' ') {
            break;
        }
        end = prev;
    }
    /* Try to parse a 2-char register at ``end-2`` — this catches the
       ``[disp + reg]`` NASM dialect.  The register must be preceded
       by ``+`` (with optional whitespace).  On match, null-terminate
       just before the ``+``, resolve the displacement, then restore. */
    if (end - bracket_start >= 2) {
        char *reg_pos = end - 2;
        char *saved = source_cursor;
        source_cursor = reg_pos;
        int pr2 = parse_register();
        source_cursor = saved;
        if (pr2 >= 0) {
            char *back = reg_pos;
            while (back > bracket_start) {
                char *pb = back - 1;
                if (pb[0] != ' ') {
                    break;
                }
                back = pb;
            }
            if (back > bracket_start) {
                char *plus = back - 1;
                if (plus[0] == '+') {
                    plus[0] = '\0';
                    int disp = resolve_value();
                    plus[0] = '+';
                    source_cursor = close;
                    if (source_cursor[0] == ']') {
                        source_cursor = source_cursor + 1;
                    }
                    parse_operand_value = disp;
                    return (3 << 8) | (pr2 & 0xFF);
                }
            }
        }
    }
    /* Plain ``[disp16]``. */
    int disp = resolve_value();
    while (source_cursor[0] != ']' && source_cursor[0] != '\0') {
        source_cursor = source_cursor + 1;
    }
    if (source_cursor[0] == ']') {
        source_cursor = source_cursor + 1;
    }
    parse_operand_value = disp;
    return 2 << 8;                      /* type=2 (direct mem) */
}

/* C-callable wrapper around the inline-asm ``parse_operand``.  The
   retired asm returned three values — AH = type, AL = reg, DX =
   immediate / displacement — across two registers.  cc.py's return
   ABI is AX-only, so the wrapper stashes DX into the
   ``parse_operand_value`` global before falling through to the
   caller with AX intact.  Pure-C callers write ``int po =
   parse_operand_c();`` and extract type / reg / value via ``(po >>
   8) & 0xFF`` / ``po & 0xFF`` / ``parse_operand_value``.  ``op1_size``
   is set by ``parse_operand`` itself as a side effect; C callers
   read it through the existing ``int op1_size`` global. */
int parse_operand_c() {
    return parse_operand();
}

/* Linear scan over ``register_table`` (4 bytes per entry: 2 chars,
   reg-num byte, size byte; zero-terminated).  Case-insensitive
   match with an identifier-boundary check on ``source_cursor[2]``.
   Returns ``(size << 8) | reg`` on match with ``source_cursor``
   advanced past the 2-char name, or ``-1`` on miss with
   ``source_cursor`` unchanged.  cc.py resolves ``register_table``
   through NAMED_CONSTANTS to the data-table label in the tail
   inline-asm block. */
int parse_register() {
    char *entry = register_table;
    while (entry[0] != '\0') {
        char a = source_cursor[0];
        if (a >= 'A' && a <= 'Z') {
            a = a + 32;
        }
        if (a != entry[0]) {
            entry = entry + 4;
            continue;
        }
        a = source_cursor[1];
        if (a >= 'A' && a <= 'Z') {
            a = a + 32;
        }
        if (a != entry[1]) {
            entry = entry + 4;
            continue;
        }
        a = source_cursor[2];
        if ((a >= 'a' && a <= 'z')
                || (a >= 'A' && a <= 'Z')
                || (a >= '0' && a <= '9')
                || a == '_') {
            entry = entry + 4;
            continue;
        }
        source_cursor = source_cursor + 2;
        return (entry[3] << 8) | entry[2];
    }
    return -1;
}

/* Lookup a label's address without advancing SI.  Used by
   encode_rel8_jump to decide between short and near forms based on
   the known target distance.  ``carry_return`` signals miss via CF;
   on hit the resolved value lands in ``peek_label_value`` (AX-side
   of the retired dual AX + CF return).  Walks ``source_cursor`` (SI
   pin) forward through the identifier, null-terminates in place,
   resets ``source_cursor`` to the name start for the ``symbol_lookup``
   SI = name ABI, then restores the saved delimiter.  is_local is
   captured *before* the scan since ``source_cursor[0]`` is cheapest
   on the SI-pinned global (no scratch-register guard needed). */
__attribute__((carry_return))
int peek_label_target() {
    int is_local = (source_cursor[0] == '.');
    char *saved = source_cursor;
    while (1) {
        char c = source_cursor[0];
        if ((c >= 'a' && c <= 'z')
                || (c >= 'A' && c <= 'Z')
                || (c >= '0' && c <= '9')
                || c == '_'
                || c == '.') {
            source_cursor = source_cursor + 1;
        } else {
            break;
        }
    }
    char *end_pos = source_cursor;
    char delim = source_cursor[0];
    source_cursor[0] = '\0';
    source_cursor = saved;
    last_symbol_index = 0xFFFF;
    if (is_local) {
        peek_label_value = symbol_lookup_c(global_scope);
    } else {
        peek_label_value = symbol_lookup_c(0xFFFF);
    }
    end_pos[0] = delim;
    if (last_symbol_index == 0xFFFF) {
        return 0;
    }
    return 1;
}

/* Read one line of source into LINE_BUFFER (null-terminated, at
   most LINE_MAX = 255 chars; over-long lines silently truncate to
   LINE_MAX before the terminating NUL).  Returns 1 on true EOF
   (load_src_sector signaled no more data and no bytes had been
   accumulated into the current line), 0 on any successful outcome
   including a partial trailing line without a final newline.  CR
   bytes are silently skipped so DOS line endings fold to Unix.
   Called by do_pass's line loop; replaces the pre-port inline-asm
   ``read_line`` + ``read_line_is_eof`` CF-to-int bridge. */
int read_line() {
    int length = 0;
    while (1) {
        if (source_buffer_position >= source_buffer_valid) {
            if (load_src_sector() != 0) {
                if (length == 0) {
                    return 1;
                }
                break;
            }
        }
        char c = source_buffer[source_buffer_position];
        source_buffer_position = source_buffer_position + 1;
        if (c == '\n') {
            break;
        }
        if (c == '\r') {
            continue;
        }
        if (length < 255) {
            line_buffer[length] = c;
            length = length + 1;
        }
    }
    line_buffer[length] = '\0';
    return 0;
}

/* Run one full pass over the source file: open it, reset the
   per-pass buffer cursors, and loop through read_line / parse_line
   until every line (including those from %included files, via
   include_pop on inner EOF) has been processed.  On open failure
   the pass exits immediately with error_flag raised; the enclosing
   run_pass1 / run_pass2 callers check error_flag and invoke
   die_error_pass1_io() as appropriate.

   Placed after its C-level callees (``include_pop`` and
   ``read_line``) rather than in strict alphabetical position:
   cc.py resolves the calls either way via its pre-codegen
   ``user_functions`` registry, but clang (run by tests/test_cc.py)
   enforces ISO C99 declare-before-use.  Written to remain callable
   from inline asm (``call do_pass``) since run_pass1 / run_pass2
   reach it that way — cc.py emits the bare label.  The function
   has no locals, so no DX-pinned spill cc.py would otherwise wrap
   the inline-asm blocks with. */
void do_pass() {
    current_address = org_value;
    source_fd = open_file_ro(source_name);
    if (source_fd == -1) {
        error_flag = 1;
        return;
    }
    source_buffer_position = 0;
    source_buffer_valid = 0;
    include_depth = 0;
    global_scope = 0xFFFF;
    while (1) {
        if (read_line() != 0) {
            if (include_depth == 0) {
                break;
            }
            include_pop();
            continue;
        }
        parse_line();
    }
    close_source();
}

/* Map a register number to its 16-bit addressing ModR/M r/m field.
   Input register index in AL (3=bx, 5=bp, 6=si, 7=di); returns the
   ModR/M encoding in AL (bx=7, bp=6, si=4, di=5).  Any input that
   isn't one of the four indexable base registers is treated as bp
   (rm=6), matching the retired asm.

   Fastcall ``regparm(1)`` so the C body reads the parameter
   naturally.  Inline-asm callers still do ``mov al, X ; call
   reg_to_rm``; AX arrives with AH carrying whatever junk the
   caller didn't zero, so the body masks to a byte before the
   switch to match the old AL-only comparison semantics. */
__attribute__((regparm(1)))
int reg_to_rm(int reg) {
    reg = reg & 0xFF;
    if (reg == 3) {
        return 7;
    }
    if (reg == 6) {
        return 4;
    }
    if (reg == 7) {
        return 5;
    }
    return 6;
}

/* Resolve a jump/label operand.  Pass 1: skip past the identifier
   and return ``current_address`` as a placeholder so the handler
   can emit a same-size displacement; pass 2: evaluate the operand
   via resolve_value (which performs the real symbol lookup plus
   any trailing ``+ offset`` math).  Both branches advance
   ``source_cursor`` past the label.  Returns int (in AX) — the
   inline-asm callers in ``encode_rel8_jump`` read AX after the
   call. */
int resolve_label() {
    if (pass != 1) {
        return resolve_value();
    }
    while (1) {
        char c = source_cursor[0];
        if ((c >= 'a' && c <= 'z')
                || (c >= 'A' && c <= 'Z')
                || (c >= '0' && c <= '9')
                || c == '_'
                || c == '.') {
            source_cursor = source_cursor + 1;
        } else {
            break;
        }
    }
    return current_address;
}

/* Expression evaluator at SI.  Recognises parenthesised
   sub-expressions, ``'c'`` / ``` `c` ``` character / backtick
   literals, ``$`` (current address), decimal / hex / binary
   numbers via parse_number, and named symbols via symbol_lookup.
   Trailing ``+`` / ``-`` / ``*`` / ``/`` / ``&`` / ``|`` / ``^``
   chain left-to-right with flat precedence — NASM's constant
   expression lowering parenthesises every subtree, so the flat
   precedence still produces the intended grouping.  Recurses into
   itself via ``call resolve_value`` from the paren / operator-RHS
   branches; cc.py's bp frame makes the recursion safe (each frame
   pushes its own BP and the BX/CX/DI save triplet stays
   stack-balanced).  Signature declares ``int`` so pure-C callers
   can bind the AX return value (``int v = resolve_value();``); the
   inline-asm body ends with AX already set, and cc.py emits
   ``ret`` directly after (naked_asm elide), so the missing C-level
   ``return`` that clang's -Wreturn-type warns about is harmless —
   same pattern as ``load_src_sector``. */
int resolve_value() {
    skip_ws();
    int value;
    char first = source_cursor[0];
    if (first == '(') {
        source_cursor = source_cursor + 1;
        value = resolve_value();
        skip_ws();
        if (source_cursor[0] == ')') {
            source_cursor = source_cursor + 1;
        }
    } else if (first == '\'') {
        source_cursor = source_cursor + 1;
        value = source_cursor[0];
        source_cursor = source_cursor + 1;
        if (source_cursor[0] == '\'') {
            source_cursor = source_cursor + 1;
        }
    } else if (first == '`') {
        source_cursor = source_cursor + 1;
        char c = source_cursor[0];
        if (c == '\\') {
            source_cursor = source_cursor + 1;
            char esc = source_cursor[0];
            if (esc == 'n') {
                value = '\n';
            } else if (esc == '0') {
                value = '\0';
            } else if (esc == 't') {
                value = '\t';
            } else if (esc == 'r') {
                value = '\r';
            } else {
                value = esc;
            }
        } else {
            value = c;
        }
        source_cursor = source_cursor + 1;
        if (source_cursor[0] == '`') {
            source_cursor = source_cursor + 1;
        }
    } else if (first == '$') {
        source_cursor = source_cursor + 1;
        value = current_address;
    } else if (first >= '0' && first <= '9') {
        value = parse_number();
    } else {
        /* Symbol path: scan identifier, null-term, symbol_lookup with
           scope = global_scope for locals (``.``-prefixed) or 0xFFFF
           for globals.  Restore the delimiter byte before returning
           so the original source line stays intact for pass 2. */
        char *name_start = source_cursor;
        while (1) {
            char c = source_cursor[0];
            if (c == '.' || c == '_' || c >= 'a'
                    || (c >= 'A' && c <= 'Z')
                    || (c >= '0' && c <= '9')) {
                source_cursor = source_cursor + 1;
            } else {
                break;
            }
        }
        char *end_pos = source_cursor;
        char delim = source_cursor[0];
        source_cursor[0] = '\0';
        int scope = 0xFFFF;
        if (name_start[0] == '.') {
            scope = global_scope;
        }
        source_cursor = name_start;
        value = symbol_lookup_c(scope);
        end_pos[0] = delim;
        source_cursor = end_pos;
    }
    /* Operator chain: right-associative via recursion (the retired
       asm dispatches to per-operator tails that each recurse into
       resolve_value for the RHS, then fall through to the shared
       ``.rv_expr_done`` epilogue).  Flat precedence — NASM's
       constant-expression lowering parenthesises every subtree
       before we see it, so the grouping still comes out right. */
    skip_ws();
    char op = source_cursor[0];
    if (op == '+') {
        source_cursor = source_cursor + 1;
        skip_ws();
        int rhs = resolve_value();
        value = value + rhs;
    } else if (op == '-') {
        source_cursor = source_cursor + 1;
        skip_ws();
        int rhs = resolve_value();
        value = value - rhs;
    } else if (op == '*') {
        source_cursor = source_cursor + 1;
        skip_ws();
        int rhs = resolve_value();
        value = value * rhs;
    } else if (op == '/') {
        source_cursor = source_cursor + 1;
        skip_ws();
        int rhs = resolve_value();
        value = value / rhs;
    } else if (op == '&') {
        source_cursor = source_cursor + 1;
        skip_ws();
        int rhs = resolve_value();
        value = value & rhs;
    } else if (op == '|') {
        source_cursor = source_cursor + 1;
        skip_ws();
        int rhs = resolve_value();
        value = value | rhs;
    } else if (op == '^') {
        source_cursor = source_cursor + 1;
        skip_ws();
        int rhs = resolve_value();
        value = value ^ rhs;
    }
    return value;
}

/* Iterative pass 1.  Starts every jcc/jmp pessimistic (near form)
   and lets the instruction handlers mark any jump they can shrink
   to rel8; loops until no jump changes size.  Convergence is
   monotonic (shrinking only makes targets closer), so a 100-
   iteration safety bound is enough to catch any infinite
   oscillation a buggy handler might introduce.  Always runs at
   least two iterations so forward references get verified against
   the symbol table built in iteration 1. */
void run_pass1() {
    pass = 1;
    symbol_count = 0;
    org_value = 0;
    /* Initialize ES:JUMP_TABLE to all-1 (near). */
    asm("mov di, JUMP_TABLE\n"
        "mov cx, JUMP_MAX\n"
        "mov al, 1\n"
        "cld\n"
        "rep stosb");
    iteration_count = 0;
    while (1) {
        changed_flag = 0;
        current_address = 0;
        global_scope = 0xFFFF;
        jump_index = 0;
        do_pass();
        if (error_flag != 0) {
            die_error_pass1_io();
        }
        iteration_count = iteration_count + 1;
        if (iteration_count >= 100) {
            die_error_pass1_iter();
        }
        if (iteration_count < 2) {
            continue;
        }
        if (changed_flag == 0) {
            break;
        }
    }
}

/* Pass 2 emits bytes to ``output_fd``.  The jump-size choices
   from pass 1 are reused (the jump_table in ES is still set), and
   ``current_address`` resets to the org origin chosen by pass 1.
   Caller has already opened output_fd. */
void run_pass2() {
    pass = 2;
    current_address = org_value;
    global_scope = 0xFFFF;
    jump_index = 0;
    output_position = 0;
    output_total = 0;
    do_pass();
}

/* Skip whitespace, a single ``,``, then whitespace — the inter-operand
   separator every multi-operand handler uses.  No-op if no comma is
   present (the first call to skip_ws still advances past leading
   whitespace). */
/* Advance source_cursor past any run of ' ' / '\t' at the current
   cursor position.  Called hundreds of times from the instruction
   handlers; ``source_cursor`` aliases SI through
   ``__attribute__((asm_register("si")))`` so the loop compiles to
   ``cmp byte [si], 32 ; je .skip ; cmp byte [si], 9 ; je .skip ;
   jmp .end ; .skip: inc si ; jmp .loop ; .end:`` — byte-identical
   to the retired inline-asm body except for cc.py's bp frame.

   Placed before ``skip_comma`` rather than in strict alphabetical
   position so clang's declare-before-use rule is satisfied; cc.py
   resolves the order-independent call either way via its
   pre-codegen ``user_functions`` registry. */
void skip_ws() {
    while (source_cursor[0] == ' ' || source_cursor[0] == '\t') {
        source_cursor = source_cursor + 1;
    }
}

void skip_comma() {
    skip_ws();
    if (source_cursor[0] == ',') {
        source_cursor = source_cursor + 1;
        skip_ws();
    }
}

/* Compute the ES-relative offset of a symbol table entry: returns
   ``index * SYMBOL_ENTRY`` (36) in AX.  Fastcall ``regparm(1)`` so
   the index arrives in AX directly.  cc.py's multiplication codegen
   uses ``mul bx`` which clobbers BX; callers that need BX across
   the call save it on the stack.  The four inline-asm call sites
   each do ``call symbol_entry_address ; mov di, ax`` now — the old
   inline body wrote DI internally, the pure-C version returns via
   AX and leaves the DI move to the caller (2 bytes per site × 4 =
   8 bytes, offset by the smaller function body). */
__attribute__((regparm(1)))
int symbol_entry_address(int index) {
    return index * SYMBOL_ENTRY;
}

/* Append a label to the symbol table.  Callers pass SI = name,
   AX = value, BX = scope (0xFFFF = global, else the index of the
   enclosing global that owns this local label).  The name copy
   pads to SYMBOL_NAME_LENGTH with zeros; metadata lands at offset
   SYMBOL_NAME_LENGTH (value, type=0, scope byte).  Overflow jumps
   to die_symbol_overflow — silently corrupting past the table
   would clobber LINE_BUFFER which lives immediately after. */
void symbol_add() {
    asm("cmp word [_g_symbol_count], SYMBOL_MAX\n"
        "jae .sa_overflow\n"
        "push cx\n"
        "push di\n"
        "push si\n"
        "push ax\n"
        "push bx\n"
        "mov ax, [_g_symbol_count]\n"
        "call symbol_entry_address\n"
        "mov di, ax\n"
        "mov cx, SYMBOL_NAME_LENGTH - 1\n"
        ".sa_copy_name:\n"
        "mov al, [si]\n"
        "test al, al\n"
        "jz .sa_pad_name\n"
        "mov [es:di], al\n"
        "inc si\n"
        "inc di\n"
        "dec cx\n"
        "jnz .sa_copy_name\n"
        ".sa_pad_name:\n"
        "mov byte [es:di], 0\n"
        "inc di\n"
        "dec cx\n"
        "jns .sa_pad_name\n"
        "mov ax, [_g_symbol_count]\n"
        "call symbol_entry_address\n"
        "mov di, ax\n"
        "pop bx\n"
        "pop ax\n"
        "mov [es:di+SYMBOL_NAME_LENGTH], ax\n"
        "mov byte [es:di+SYMBOL_NAME_LENGTH+2], 0\n"
        "mov [es:di+SYMBOL_NAME_LENGTH+3], bl\n"
        "inc word [_g_symbol_count]\n"
        "pop si\n"
        "pop di\n"
        "pop cx\n"
        "jmp .sa_end\n"
        ".sa_overflow:\n"
        "jmp die_symbol_overflow\n"
        ".sa_end:");
}

/* C-callable wrapper around ``symbol_add_constant`` for the common
   ``name = source_cursor, scope = 0xFFFF`` call shape used by
   parse_line's equ path.  Naked-asm thunk: the regparm(1) ``value``
   arrives in AX and is forwarded untouched to symbol_add_constant;
   BX gets the hard-coded 0xFFFF scope; SI must be set to the name
   pointer by the caller (parse_line writes ``source_cursor =
   label_start;`` immediately before the call). */
__attribute__((regparm(1)))
__attribute__((always_inline))
void symbol_add_constant_c(int value) {
    asm("mov bx, 0xFFFF\n"
        "call symbol_add_constant");
}

/* C-callable ``symbol_set`` wrappers that hardcode the scope.  Both
   take ``value`` via ``regparm(1)`` AX; SI = name is pre-loaded by
   the caller through ``source_cursor``.  Factored out so the
   identical "global" / "local" dispatches in ``handle_unknown_word``
   and ``parse_line`` don't need to open-code BX-setup ``call
   symbol_set`` inline. */
__attribute__((regparm(1)))
__attribute__((always_inline))
void symbol_set_global(int value) {
    asm("mov bx, 0xFFFF\n"
        "call symbol_set");
}

__attribute__((regparm(1)))
__attribute__((always_inline))
void symbol_set_local(int value) {
    asm("mov bx, [_g_global_scope]\n"
        "call symbol_set");
}

/* ``%assign`` entries: a value-only binding (scope=0xFFFF, type=1
   so pass-1 code that tells labels from %assigns can skip the
   relocation step).  Delegates the add / update logic to
   symbol_set, then rewrites the type byte. */
void symbol_add_constant() {
    asm("push bx\n"
        "mov bx, 0FFFFh\n"
        "call symbol_set\n"
        "push ax\n"
        "mov ax, [_g_last_symbol_index]\n"
        "call symbol_entry_address\n"
        "mov di, ax\n"
        "mov byte [es:di+SYMBOL_NAME_LENGTH+2], 1\n"
        "pop ax\n"
        "pop bx");
}

/* C-callable ``symbol_lookup`` wrapper.  ``scope`` arrives via
   ``regparm(1)`` AX and is threaded into BX; SI = name is pre-loaded
   by the caller (pinned ``source_cursor``).  Returns AX = value
   (0 on miss in either pass; symbol_lookup's pass-1 forward-reference
   behavior returns 0 / CF clear, pass-2 miss returns 0 / CF set — both
   paths leave AX = 0).  Callers that care about hit / miss read
   ``last_symbol_index`` (symbol_lookup sets it to 0xFFFF on miss, else
   the entry's index); pure-C ``resolve_value`` treats both pass-1 and
   pass-2 misses as value = 0, matching the retired inline-asm body. */
__attribute__((regparm(1)))
__attribute__((always_inline))
int symbol_lookup_c(int scope) {
    asm("mov bx, ax\n"
        "call symbol_lookup");
}

/* Linear scan of the symbol table.  Callers pass SI = name, BX =
   wanted scope (0xFFFF for global).  Returns AX = value, CF clear,
   ``last_symbol_index`` = entry's index on hit; CF set on miss
   (pass 1 returns 0 / CF clear so forward references don't abort
   parsing before the symbol is defined).  Body preserves the
   entry-index push/pop discipline the retired asm used so callers
   that pass SI / DI as live data don't need to guard them. */
void symbol_lookup() {
    asm("push cx\n"
        "push dx\n"
        "push di\n"
        "mov cx, [_g_symbol_count]\n"
        "test cx, cx\n"
        "jz .sl_not_found\n"
        "xor di, di\n"
        "xor dx, dx\n"
        ".sl_search:\n"
        "push ax\n"
        "mov al, [es:di+SYMBOL_NAME_LENGTH+3]\n"
        "cmp al, bl\n"
        "pop ax\n"
        "jne .sl_next\n"
        "push si\n"
        "push di\n"
        "push cx\n"
        ".sl_cmp_name:\n"
        "mov al, [si]\n"
        "cmp al, [es:di]\n"
        "jne .sl_no_match\n"
        "test al, al\n"
        "jz .sl_name_match\n"
        "inc si\n"
        "inc di\n"
        "jmp .sl_cmp_name\n"
        ".sl_name_match:\n"
        "pop cx\n"
        "pop di\n"
        "pop si\n"
        "mov ax, [es:di+SYMBOL_NAME_LENGTH]\n"
        "mov [_g_last_symbol_index], dx\n"
        "clc\n"
        "jmp .sl_end\n"
        ".sl_no_match:\n"
        "pop cx\n"
        "pop di\n"
        "pop si\n"
        ".sl_next:\n"
        "add di, SYMBOL_ENTRY\n"
        "inc dx\n"
        "loop .sl_search\n"
        ".sl_not_found:\n"
        "xor ax, ax\n"
        "cmp byte [_g_pass], 1\n"
        "je .sl_pass1_ok\n"
        "stc\n"
        "jmp .sl_end\n"
        ".sl_pass1_ok:\n"
        "clc\n"
        ".sl_end:\n"
        "pop di\n"
        "pop dx\n"
        "pop cx");
}

/* Update or add.  SI = name (null-terminated), AX = value, BX =
   scope.  Looks up the entry first; if present, updates the value
   in place; otherwise calls symbol_add.  Stashes AX / BX in
   symbol_set_value / symbol_set_scope globals so the symbol_lookup
   call in between doesn't lose them.  Sets last_symbol_index to
   the entry's (new or existing) index. */
void symbol_set() {
    asm("mov [_g_symbol_set_value], ax\n"
        "mov [_g_symbol_set_scope], bx\n"
        "push di\n"
        "push cx\n"
        "push dx\n"
        "mov word [_g_last_symbol_index], 0FFFFh\n"
        "call symbol_lookup\n"
        "cmp word [_g_last_symbol_index], 0FFFFh\n"
        "je .ss_add\n"
        "mov ax, [_g_last_symbol_index]\n"
        "call symbol_entry_address\n"
        "mov di, ax\n"
        "mov ax, [_g_symbol_set_value]\n"
        "mov [es:di+SYMBOL_NAME_LENGTH], ax\n"
        "pop dx\n"
        "pop cx\n"
        "pop di\n"
        "jmp .ss_end\n"
        ".ss_add:\n"
        "pop dx\n"
        "pop cx\n"
        "pop di\n"
        "mov ax, [_g_symbol_set_value]\n"
        "mov bx, [_g_symbol_set_scope]\n"
        "call symbol_add\n"
        "push ax\n"
        "mov ax, [_g_symbol_count]\n"
        "dec ax\n"
        "mov [_g_last_symbol_index], ax\n"
        "pop ax\n"
        ".ss_end:");
}

int main(int argc, char *argv[]) {
    /* ES starts at DS (kernel convention) so die() / open() run
       safely.  We only switch to SYMBOL_SEGMENT once we're ready to
       run the assembler passes — the handlers index the symbol table
       via ``[es:...]``, which needs ES pointed at that segment. */
    /* Publish the scratch-buffer bases as C-visible pointers so
       C code can index ``line_buffer[i]`` / ``source_buffer[i]`` and
       so abort_unknown_impl can printf the bad source line.
       ``include_source_save`` lives one SOURCE_BUFFER length past
       SOURCE_BUFFER — an %include level saves the 512-byte
       SOURCE_BUFFER into that scratch RAM instead of bloating the
       binary. */
    line_buffer = LINE_BUFFER;
    output_buffer = OUTPUT_BUFFER;
    source_buffer = SOURCE_BUFFER;
    include_source_save = SOURCE_BUFFER + 512;
    if (argc != 2) {
        die("Usage: asm <source> <output>\n");
    }
    source_name = argv[0];
    output_name = argv[1];
    compute_source_prefix();
    int fd = open(output_name, O_WRONLY + O_CREAT + O_TRUNC, FLAG_EXECUTE);
    if (fd < 0) {
        die("Error: cannot create output\n");
    }
    output_fd = fd;
    /* Switch ES to the symbol-table segment for pass 1 / pass 2; the
       handlers index ``[es:0..EFF8]`` for symbols and the jump table. */
    asm("mov ax, SYMBOL_SEGMENT\n"
        "mov es, ax\n"
        "cld");
    run_pass1();
    run_pass2();
    /* flush_output uses the ES-safe ``syscall`` wrapper internally, so
       it preserves ES=SYMBOL_SEGMENT across the write. */
    flush_output();
    /* Restore ES=DS before the cc.py close() builtin (kernel expects
       ES=DS on int 30h). */
    restore_es();
    if (close(output_fd) < 0) {
        die("Error: directory write failed\n");
    }
    die("OK\n");
}

asm(
    "\n"
    "\n"
    "        ;; Memory layout.  The assembler's scratch buffers live past\n"
    "        ;; _program_end (cc.py tail sentinel); the symbol table and\n"
    "        ;; jump table live in a dedicated ES segment (SYMBOL_SEGMENT,\n"
    "        ;; linear 0x20000) so they don't compete with segment-0\n"
    "        ;; memory.  Named constants are ``#define``d in\n"
    "        ;; src/c/asm_layout.h and bridged into NASM ``%define``s by\n"
    "        ;; cc.py at the top of the generated output.\n"
    "\n"
    ";;; -----------------------------------------------------------------------\n"
    ";;; Every function in the assembler — main, the pass driver, the\n"
    ";;; read / include / emit / resolve / symbol / parse / handler\n"
    ";;; families — lives in cc.py-emitted C with an inline-asm body\n"
    ";;; near the top of this file.  cc.py emits the bare label so\n"
    ";;; ``call X`` / ``jmp X`` from the tables below continue to\n"
    ";;; resolve.  Remaining C-level globals are accessed through their\n"
    ";;; ``_g_<name>`` names directly (see the C declarations at the top\n"
    ";;; of src/c/asm.c).  What remains in this trailing asm block: the\n"
    ";;; syscall wrapper (name collides with libc so it stays here to\n"
    ";;; keep clang happy), the mnemonic and register data tables, and\n"
    ";;; the STR_* keyword strings.\n"
    ";;; -----------------------------------------------------------------------\n"
    "\n"
    ";;; -----------------------------------------------------------------------\n"
    ";;; Mnemonic table: pairs of (name_ptr, handler_ptr), terminated by 0\n"
    ";;; -----------------------------------------------------------------------\n"
    "mnemonic_table:\n"
    "        dw STR_AAM, handle_aam\n"
    "        dw STR_ADC, handle_adc\n"
    "        dw STR_ADD, handle_add\n"
    "        dw STR_AND, handle_and\n"
    "        dw STR_CALL, handle_call\n"
    "        dw STR_CLC, handle_clc\n"
    "        dw STR_CLD, handle_cld\n"
    "        dw STR_CMP, handle_cmp\n"
    "        dw STR_DEC, handle_dec\n"
    "        dw STR_DIV, handle_div\n"
    "        dw STR_INC, handle_inc\n"
    "        dw STR_INT, handle_int\n"
    "        dw STR_JA,  handle_ja\n"
    "        dw STR_JAE, handle_jnc\n"
    "        dw STR_JB,  handle_jb\n"
    "        dw STR_JBE, handle_jbe\n"
    "        dw STR_JC,  handle_jb\n"
    "        dw STR_JE,  handle_jz\n"
    "        dw STR_JG,  handle_jg\n"
    "        dw STR_JGE, handle_jge\n"
    "        dw STR_JL,  handle_jl\n"
    "        dw STR_JLE, handle_jle\n"
    "        dw STR_JMP, handle_jmp\n"
    "        dw STR_JNC, handle_jnc\n"
    "        dw STR_JNE, handle_jne\n"
    "        dw STR_JNS, handle_jns\n"
    "        dw STR_JNZ, handle_jne\n"
    "        dw STR_JZ,  handle_jz\n"
    "        dw STR_LODSB, handle_lodsb\n"
    "        dw STR_LODSW, handle_lodsw\n"
    "        dw STR_LOOP, handle_loop\n"
    "        dw STR_MOV, handle_mov\n"
    "        dw STR_MOVSB, handle_movsb\n"
    "        dw STR_MOVSW, handle_movsw\n"
    "        dw STR_MOVZX, handle_movzx\n"
    "        dw STR_MUL, handle_mul\n"
    "        dw STR_NEG, handle_neg\n"
    "        dw STR_NOT, handle_not\n"
    "        dw STR_OR,  handle_or\n"
    "        dw STR_POP, handle_pop\n"
    "        dw STR_POPA, handle_popa\n"
    "        dw STR_PUSH, handle_push\n"
    "        dw STR_PUSHA, handle_pusha\n"
    "        dw STR_REP, handle_rep\n"
    "        dw STR_REPNE, handle_repne\n"
    "        dw STR_RET, handle_ret\n"
    "        dw STR_SBB, handle_sbb\n"
    "        dw STR_SCASB, handle_scasb\n"
    "        dw STR_SHL, handle_shl\n"
    "        dw STR_SHR, handle_shr\n"
    "        dw STR_STC, handle_stc\n"
    "        dw STR_STOSB, handle_stosb\n"
    "        dw STR_STOSW, handle_stosw\n"
    "        dw STR_SUB, handle_sub\n"
    "        dw STR_TEST, handle_test\n"
    "        dw STR_XCHG, handle_xchg\n"
    "        dw STR_XOR, handle_xor\n"
    "        dw 0\n"
    "\n"
    ";;; Mnemonic strings\n"
    "STR_AAM     db 'aam',0\n"
    "STR_ADC     db 'adc',0\n"
    "STR_ADD     db 'add',0\n"
    "STR_AND     db 'and',0\n"
    "STR_ASSIGN  db 'assign',0\n"
    "STR_BYTE    db 'byte',0\n"
    "STR_CALL    db 'call',0\n"
    "STR_CLC     db 'clc',0\n"
    "STR_CLD     db 'cld',0\n"
    "STR_CMP     db 'cmp',0\n"
    "STR_DEC     db 'dec',0\n"
    "STR_DIV     db 'div',0\n"
    "STR_DB      db 'db',0\n"
    "STR_EQU     db 'equ',0\n"
    "STR_DD      db 'dd',0\n"
    "STR_DEFINE  db 'define',0\n"
    "STR_DW      db 'dw',0\n"
    "STR_INC     db 'inc',0\n"
    "STR_INCLUDE db 'include',0\n"
    "STR_INT     db 'int',0\n"
    "STR_JA      db 'ja',0\n"
    "STR_JAE     db 'jae',0\n"
    "STR_JB      db 'jb',0\n"
    "STR_JBE     db 'jbe',0\n"
    "STR_JC      db 'jc',0\n"
    "STR_JE      db 'je',0\n"
    "STR_JG      db 'jg',0\n"
    "STR_JGE     db 'jge',0\n"
    "STR_JL      db 'jl',0\n"
    "STR_JLE     db 'jle',0\n"
    "STR_JMP     db 'jmp',0\n"
    "STR_JNC     db 'jnc',0\n"
    "STR_JNE     db 'jne',0\n"
    "STR_JNS     db 'jns',0\n"
    "STR_JNZ     db 'jnz',0\n"
    "STR_JZ      db 'jz',0\n"
    "STR_LODSB   db 'lodsb',0\n"
    "STR_LODSW   db 'lodsw',0\n"
    "STR_LOOP    db 'loop',0\n"
    "STR_MOV     db 'mov',0\n"
    "STR_MOVSB   db 'movsb',0\n"
    "STR_MOVSW   db 'movsw',0\n"
    "STR_MOVZX   db 'movzx',0\n"
    "STR_MUL     db 'mul',0\n"
    "STR_NEG     db 'neg',0\n"
    "STR_NOT     db 'not',0\n"
    "STR_OR      db 'or',0\n"
    "STR_ORG     db 'org',0\n"
    "STR_SHORT   db 'short',0\n"
    "STR_POP     db 'pop',0\n"
    "STR_POPA    db 'popa',0\n"
    "STR_PUSH    db 'push',0\n"
    "STR_PUSHA   db 'pusha',0\n"
    "STR_REP     db 'rep',0\n"
    "STR_REPNE   db 'repne',0\n"
    "STR_RET     db 'ret',0\n"
    "STR_SBB     db 'sbb',0\n"
    "STR_SCASB   db 'scasb',0\n"
    "STR_SHL     db 'shl',0\n"
    "STR_SHR     db 'shr',0\n"
    "STR_STC     db 'stc',0\n"
    "STR_STOSB   db 'stosb',0\n"
    "STR_STOSW   db 'stosw',0\n"
    "STR_SUB     db 'sub',0\n"
    "STR_TEST    db 'test',0\n"
    "STR_TIMES   db 'times',0\n"
    "STR_WORD    db 'word',0\n"
    "STR_XCHG    db 'xchg',0\n"
    "STR_XOR     db 'xor',0\n"
    "\n"
    ";;; Register table: 2-char name, reg number, size (8 or 16)\n"
    "register_table:\n"
    "        db 'al', 0, 8\n"
    "        db 'cl', 1, 8\n"
    "        db 'dl', 2, 8\n"
    "        db 'bl', 3, 8\n"
    "        db 'ah', 4, 8\n"
    "        db 'ch', 5, 8\n"
    "        db 'dh', 6, 8\n"
    "        db 'bh', 7, 8\n"
    "        db 'ax', 0, 16\n"
    "        db 'cx', 1, 16\n"
    "        db 'dx', 2, 16\n"
    "        db 'bx', 3, 16\n"
    "        db 'sp', 4, 16\n"
    "        db 'bp', 5, 16\n"
    "        db 'si', 6, 16\n"
    "        db 'di', 7, 16\n"
    "        db 0                   ; terminator\n"
    "\n"
    ";;; -----------------------------------------------------------------------\n"
    ";;; ES-safe syscall wrapper: save ES (symbol table segment), set ES=0\n"
    ";;; for kernel calls, then restore ES before returning.  Kept as a\n"
    ";;; file-scope asm label (not a C function) because ``syscall`` is\n"
    ";;; a reserved libc symbol and clang's syntax check rejects a user\n"
    ";;; definition; renaming would touch every ``call syscall`` site in\n"
    ";;; the inline-asm bodies.\n"
    ";;; -----------------------------------------------------------------------\n"
    "syscall:\n"
    "        push es\n"
    "        push ds\n"
    "        pop es                  ; ES=0 (kernel expects ES=0)\n"
    "        int 30h\n"
    "        pop es\n"
    "        ret\n"
    "\n"
    ";;; -----------------------------------------------------------------------\n"
    ";;; Mutable state lives as cc.py-emitted ``_g_<name>:`` cells after\n"
    ";;; this inline-asm block (see the C declarations at the top of\n"
    ";;; src/c/asm.c).  cc.py emits a ``_program_end:`` sentinel at the\n"
    ";;; very end of the output, which LINE_BUFFER and friends point at\n"
    ";;; so the scratch buffers still sit immediately past the loaded\n"
    ";;; image.\n"
    ";;; -----------------------------------------------------------------------\n"
);
