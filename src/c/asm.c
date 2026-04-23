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
uint8_t changed_flag;
int current_address;
/* Default operand size set by the ``[bits N]`` directive.  Starts at
   16 (real-mode default); a ``[bits 32]`` line flips it to 32 and
   pmode encodings that match the default size emit without the 0x66
   prefix, while 16-bit encodings acquire the prefix.  Reset to 16 at
   the start of every pass so the directive's effect on each pass
   matches the source order. */
uint8_t default_bits;
int equ_space;
uint8_t error_flag;
/* abort_unknown stores the offending mnemonic's SI into
   ``error_word`` before jumping to the pure-C reporter. */
char *error_word;
int global_scope = 0xFFFF;
uint8_t include_depth;
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
uint8_t iteration_count;
int jump_index;
int last_symbol_index;
/* Pointer to the 256-byte line-accumulation buffer at
   ``_program_end`` (main() initializes it).  read_line fills it
   null-terminated; abort_unknown_impl prints it. */
char *line_buffer;
/* Macro table — ``%macro NAME N`` through ``%endmacro`` stores the
   body lines in ``macro_body_buffer`` (each null-terminated, packed
   one after another) and the metadata in parallel arrays indexed
   by macro slot.  Lookup is a linear scan over ``macro_names``;
   invocation substitutes ``%1``..``%9`` with the call-site
   arguments into ``line_buffer`` and re-runs ``parse_line`` on each
   expanded body line.  Reset at the start of every pass so %macro
   blocks re-populate the table as they're re-parsed.

   ``macro_args_text`` / ``macro_arg_starts`` are per-invocation
   scratch filled by expand_macro just before it walks the body;
   valid only for the duration of that expansion (macros are not
   currently re-entrant). */
int macro_arg_counts[MACRO_MAX];
int macro_arg_starts[9];
char macro_args_text[256];
char macro_body_buffer[MACRO_BODY_BUFFER_SIZE];
int macro_body_lengths[MACRO_MAX];
int macro_body_starts[MACRO_MAX];
int macro_body_used;
int macro_count;
char macro_names[MACRO_MAX * MACRO_NAME_LEN];
/* Kept ``int`` — hot-path readers (``int size1 = op1_size;``) bind
   the value into an ``int`` local, which under byte-slot codegen
   pays a ``xor ah, ah`` per load with no matching store-side win. */
int op1_size;
int op1_value;
int op2_value;
int org_value;
/* Addressing size of the most recently parsed memory operand: 16 or
   32, matching the base register used inside the brackets (or
   ``default_bits`` for a plain ``[disp]``).  Stashed by
   ``parse_operand`` so callers can emit the 0x67 address-size
   prefix and pick the correct ModR/M encoding.  Only touched on
   memory operands; register/immediate parses leave the value from
   a previous memory parse intact so a two-memory-operand call
   order (e.g. ``mov [esp], eax``) still sees the address size of
   the bracketed operand. */
int parse_operand_address_size;
/* ``parse_operand`` returns the packed ``(type << 8) | reg`` in AX
   and stashes the displacement / immediate here for the caller to
   read after the call — cc.py's return ABI is AX-only. */
int parse_operand_value;
/* Pointer to the 512-byte output-byte buffer at ``_program_end + 256``
   (= OUTPUT_BUFFER in the asm_layout.h #define).  main() initializes
   it; ``emit_byte`` / ``flush_output`` index into it directly. */
uint8_t *output_buffer;
int output_fd;
char *output_name;
int output_position;
int output_total;
uint8_t pass;
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
   ``source_cursor += 1`` folds to ``inc si``, and
   ``source_cursor[0]`` compiles to ``mov al, [si]``. */
__attribute__((asm_register("si")))
char *source_cursor;
int source_fd;
char *source_name;
char source_prefix[32];
int symbol_count;

/* Forward declarations for functions defined later in the file that
   pure-C bodies need to call.  cc.py resolves these via its two-pass
   ``user_functions`` registry regardless of source order, but clang
   enforces ISO C99 declare-before-use; the prototypes placate the
   syntax check without affecting codegen. */
void define_macro();
__attribute__((regparm(1)))
void emit_address_disp(int disp);
__attribute__((regparm(1)))
void emit_address_size_prefix(int size);
__attribute__((regparm(1)))
void emit_alu_reg_imm(int op_rr, int reg, int size, int imm);
__attribute__((regparm(1)))
int emit_alu_mem_imm(int rfield);
__attribute__((regparm(1)))
void emit_byte(int value);
__attribute__((regparm(1)))
void emit_dword(int value);
__attribute__((regparm(1)))
void emit_indexed_mem(int reg_field, int rm_reg_id, int disp);
__attribute__((regparm(1)))
void emit_modrm_direct(int reg, int disp);
__attribute__((regparm(1)))
void emit_modrm_disp(int modrm, int disp);
__attribute__((regparm(1)))
void emit_operand_size_prefix(int size);
__attribute__((regparm(1)))
void emit_sized(int base, int size);
__attribute__((regparm(1)))
void emit_sized_mem(int base, int size);
__attribute__((regparm(1)))
void emit_word(int value);
__attribute__((regparm(1)))
void expand_macro(int idx);
int find_macro();
void flush_output();
__attribute__((regparm(1)))
void inc_dec_handler(int rfield);
void include_pop();
__attribute__((regparm(1)))
__attribute__((carry_return))
int is_ident_char(int c);
__attribute__((regparm(1)))
int make_modrm_reg_reg_impl(int register_id, int rm);
__attribute__((regparm(1)))
int match_seg_ds_es(int ds_opcode, int es_opcode);
__attribute__((regparm(1)))
__attribute__((carry_return))
int match_word(char *keyword);
__attribute__((regparm(1)))
void mem_op_reg_emit(int opcode);
__attribute__((regparm(1)))
int open_file_ro(char *path);
void parse_directive();
void parse_line();
void parse_mnemonic();
int parse_creg();
int parse_operand();
int parse_register();
__attribute__((carry_return))
int peek_label_target();
int read_line();
int read_source_sector();
__attribute__((regparm(1)))
int reg_to_rm(int register_id);
int resolve_label();
int resolve_value();
void restore_es();
void run_pass1();
void run_pass2();
void scan_ident_dot();
__attribute__((regparm(1)))
void shift_handler(int modrm_base);
void skip_comma();
void skip_ws();
__attribute__((regparm(1)))
void symbol_add_constant(int value);
__attribute__((regparm(1)))
int symbol_entry_address(int index);
__attribute__((regparm(1)))
int symbol_lookup(int scope);
__attribute__((regparm(1)))
void symbol_set(int value, int scope);
__attribute__((regparm(1)))
void symbol_set_global(int value);
__attribute__((regparm(1)))
void symbol_set_local(int value);
__attribute__((regparm(1)))
void unary_f6f7(int modrm_base);

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

/* Shared body for ``adc`` (/2, modrm base 0xD0) and ``sbb`` (/3,
   modrm base 0xD8) — the r, imm form only.  r8 uses 80 /r ib, r16
   uses 83 /r ib (sign-extended).  adc carries the checksum-fold
   idiom (``adc bx, 0``); sbb carries the byte-borrow propagate
   cc.py's byte-compound-``-``-assign split emits (``sub al, [mem] /
   sbb ah, 0``).  No r,r / [mem] / mem-dst forms — the self-host
   never needs them. */
__attribute__((regparm(1)))
void adc_sbb_handler(int modrm_base) {
    skip_ws();
    int packed_register = parse_register();
    skip_comma();
    int imm = resolve_value();
    if ((packed_register >> 8) == 8) {
        emit_byte(0x80);
    } else {
        emit_byte(0x83);
    }
    emit_byte(modrm_base | (packed_register & 0xFF));
    emit_byte(imm);
}

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
        i += 1;
    }
    int j = 0;
    while (j < end) {
        source_prefix[j] = source_name[j];
        j += 1;
    }
    source_prefix[end] = '\0';
}

/* Define the label at ``source_cursor`` as pointing at ``current_address``.
   Shared by ``parse_line``'s ``LABEL:`` branch and ``handle_unknown_word``'s
   bare-label fallback.  Pass 1 registers the symbol (global or local by
   the ``.`` prefix, captured by the caller); pass 2 re-resolves globals
   so ``global_scope`` tracks the enclosing label for subsequent locals.
   Callers arrange ``source_cursor`` = name (SI-pinned ABI shared with
   every ``symbol_*`` entry point). */
__attribute__((regparm(1)))
void define_label_here(int is_local) {
    if (pass == 1) {
        if (is_local) {
            symbol_set_local(current_address);
        } else {
            symbol_set_global(current_address);
            global_scope = last_symbol_index;
        }
    } else if (is_local == 0) {
        symbol_lookup(0xFFFF);
        if (last_symbol_index != 0xFFFF) {
            global_scope = last_symbol_index;
        }
    }
}

/* Parse a ``%macro NAME N`` header and slurp body lines into
   ``macro_body_buffer`` up to ``%endmacro``.  source_cursor is
   already past the ``%macro`` token when this is called.  Each body
   line is stored null-terminated so expand_macro can walk them
   cheaply.  Overflow of either ``macro_count`` or
   ``macro_body_used`` silently drops the macro (we still consume
   through %endmacro to keep the parser aligned). */
void define_macro() {
    skip_ws();
    /* Save the name's starting address, then advance ``source_cursor``
       one char at a time (``inc si``) through the identifier.  Copying
       the name to ``macro_names`` afterwards uses ``name_start`` as a
       plain char pointer that cc.py places in a non-SI register, so
       the byte-index loop doesn't have to juggle the SI-pinned
       source_cursor against ``macro_names`` indexing. */
    char *name_start = source_cursor;
    int name_len = 0;
    while (is_ident_char(source_cursor[0])) {
        source_cursor += 1;
        name_len += 1;
    }
    int slot = macro_count;
    int has_slot = 0;
    if (slot < MACRO_MAX) {
        has_slot = 1;
        int j = 0;
        while (j < name_len && j < MACRO_NAME_LEN - 1) {
            macro_names[slot * MACRO_NAME_LEN + j] = name_start[j];
            j += 1;
        }
        macro_names[slot * MACRO_NAME_LEN + j] = '\0';
    }
    skip_ws();
    int argcount = resolve_value();
    int body_start = macro_body_used;
    if (has_slot) {
        macro_arg_counts[slot] = argcount;
        macro_body_starts[slot] = body_start;
    }
    while (1) {
        if (read_line() != 0) {
            break;
        }
        char *cur = line_buffer;
        while (cur[0] == ' ' || cur[0] == '\t') {
            cur += 1;
        }
        if (cur[0] == '%') {
            /* match_word returns via CF (``carry_return`` attribute);
               assigning its result to an int doesn't work, so test the
               match directly in the condition.  source_cursor is
               clobbered either way — the outer do_pass loop's next
               read_line + parse_line will reset it. */
            source_cursor = cur + 1;
            if (match_word(STR_ENDMACRO)) {
                break;
            }
        }
        int len = 0;
        while (line_buffer[len] != '\0') {
            len += 1;
        }
        if (macro_body_used + len + 1 < MACRO_BODY_BUFFER_SIZE) {
            int k = 0;
            while (k <= len) {
                macro_body_buffer[macro_body_used + k] = line_buffer[k];
                k += 1;
            }
            macro_body_used = macro_body_used + len + 1;
        }
    }
    if (has_slot) {
        macro_body_lengths[slot] = macro_body_used - body_start;
        macro_count = slot + 1;
    }
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

/* Run one full pass over the source file: open it, reset the
   per-pass buffer cursors, and loop through read_line / parse_line
   until every line (including those from %included files, via
   include_pop on inner EOF) has been processed.  On open failure
   the pass exits immediately with error_flag raised; the enclosing
   run_pass1 / run_pass2 callers check error_flag and invoke
   die_error_pass1_io() as appropriate.  Callable from inline asm
   (``call do_pass``) since run_pass1 / run_pass2 reach it that way
   — cc.py emits the bare label.  The function has no locals, so
   no DX-pinned spill cc.py would otherwise wrap the inline-asm
   blocks with. */
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

/* The ALU binop family (``add`` /0, ``or`` /1, ``and`` /4, ``sub`` /5,
   ``xor`` /6) all share one encoding shape: the /r field drives every
   opcode in the instruction, so one body parametrized on ``rfield``
   replaces five near-identical handlers.

   Opcode derivations from ``rfield``:
     - r/r byte:       rfield * 8           (00 / 08 / 20 / 28 / 30)
     - r/mem byte:     rfield * 8 | 2       (02 / 0A / 22 / 2A / 32)
     - AL imm8 short:  rfield * 8 | 4       (04 / 0C / 24 / 2C / 34)
     - AX imm16 short: rfield * 8 | 5       (05 / 0D / 25 / 2D / 35)
     - [mem], r16:     rfield * 8 | 1       (01 / 09 / 21 / 29 / 31)
     - 80 /r modrm:    0xC0 | (rfield * 8)  (C0 / C8 / E0 / E8 / F0)

   The five retired handlers differed only in which optional forms
   they accepted (add/sub covered the most; xor/and the fewest).  The
   shared helper is a strict superset: any operand shape the retired
   handlers accepted is preserved byte-identically, and a handful of
   shapes that were silently mis-encoded (e.g. ``or ax, imm16`` went
   through ``81 /1 iw`` instead of the ``0D iw`` short form) now match
   NASM.  asm.asm doesn't exercise those previously-broken shapes, so
   test_asm.py parity holds.  ``handle_add`` / ``handle_and`` /
   ``handle_or`` / ``handle_sub`` / ``handle_xor`` peel off the
   ``<op> byte|word [disp16], imm`` shape via ``emit_alu_mem_imm``
   before calling ``emit_alu_binop`` (the shared path expects the
   second operand to be a register).  Byte width always emits
   ``80 /r ib``; word width picks ``83 /r ib`` (sign-extended short
   form) when the immediate fits signed 8-bit and ``81 /r iw``
   otherwise. */
/* Emit ``disp`` at the current addressing width: disp16 under
   bits=16, disp32 under bits=32.  Used by the accumulator-direct
   ``moffs`` short forms (A0 / A1 / A2 / A3) whose address field
   follows the bare opcode with no ModR/M byte. */
__attribute__((regparm(1)))
void emit_address_disp(int disp) {
    if (default_bits == 32) {
        emit_dword(disp);
    } else {
        emit_word(disp);
    }
}

/* Emit the 0x67 address-size prefix iff ``size`` is a 16/32 size
   that disagrees with ``default_bits``.  ``size`` is typically the
   value parse_operand stashed in ``parse_operand_address_size``;
   the emit site calls this right before the opcode so the prefix
   lands ahead of any 0x66 operand-size prefix and the opcode
   itself. */
__attribute__((regparm(1)))
void emit_address_size_prefix(int size) {
    if (size != 16 && size != 32) {
        return;
    }
    if (size != default_bits) {
        emit_byte(0x67);
    }
}

__attribute__((regparm(1)))
void emit_alu_binop(int rfield) {
    skip_ws();
    int op_rr = rfield << 3;
    if (source_cursor[0] == '[') {
        int packed_mem = parse_operand();
        int mem_type = (packed_mem >> 8) & 0xFF;
        int mem_reg = packed_mem & 0xFF;
        int mem_val = parse_operand_value;
        skip_comma();
        int packed_register = parse_register();
        int reg_id = packed_register & 0xFF;
        int size = (packed_register >> 8) & 0xFF;
        emit_sized_mem(op_rr, size);
        if (mem_type == 3) {
            emit_indexed_mem(reg_id, mem_reg, mem_val);
        } else {
            emit_modrm_direct(reg_id, mem_val);
        }
        return;
    }
    int packed_register = parse_register();
    int register1_id = packed_register & 0xFF;
    int size1 = (packed_register >> 8) & 0xFF;
    skip_comma();
    int packed_operand = parse_operand();
    int type2 = (packed_operand >> 8) & 0xFF;
    int register2_id = packed_operand & 0xFF;
    int value2 = parse_operand_value;
    if (type2 == 0) {
        emit_sized(op_rr, size1);
        emit_byte(make_modrm_reg_reg_impl(register2_id, register1_id));
    } else if (type2 == 2) {
        emit_sized_mem(op_rr | 2, size1);
        emit_modrm_direct(register1_id, value2);
    } else if (type2 == 3) {
        emit_sized_mem(op_rr | 2, size1);
        emit_indexed_mem(register1_id, register2_id, value2);
    } else {
        emit_alu_reg_imm(op_rr, register1_id, size1, value2);
    }
}

/* ``<op> reg, imm`` shared by ``emit_alu_binop`` and ``handle_cmp``.
   ``op_rr`` is ``rfield << 3`` (add=0x00, or=0x08, and=0x20, sub=0x28,
   xor=0x30, cmp=0x38); ``modrm_base = 0xC0 | op_rr`` covers the
   register-mode ModR/M constants.  Four encodings picked by size /
   range / AL-or-AX:
     - r8:           80 /r ib, or AL short 04+op_rr / 0C / 24 / 2C / 34 / 3C
     - r16, imm8:    83 /r ib (sign-extended)
     - AX, imm16:    05+op_rr / 0D / 25 / 2D / 35 / 3D short form
     - r16, imm16:   81 /r iw */
__attribute__((regparm(1)))
void emit_alu_reg_imm(int op_rr, int reg, int size, int imm) {
    int modrm_base = 0xC0 | op_rr;
    if (size == 8) {
        if (reg == 0) {
            emit_byte(op_rr | 4);
        } else {
            emit_byte(0x80);
            emit_byte(modrm_base | reg);
        }
        emit_byte(imm & 0xFF);
        return;
    }
    /* size == 16 or 32 — the operand-size prefix selects the width
       against ``default_bits`` (no prefix in matching mode, 0x66 in
       the other).  The imm16 tail grows to imm32 (emit_dword).  The
       sign-extended ``83 /r ib`` shape is identical between 16 and
       32 bits — only the prefix distinguishes them. */
    emit_operand_size_prefix(size);
    if (imm >= -128 && imm <= 127) {
        emit_byte(0x83);
        emit_byte(modrm_base | reg);
        emit_byte(imm & 0xFF);
        return;
    }
    if (reg == 0) {
        emit_byte(op_rr | 5);
    } else {
        emit_byte(0x81);
        emit_byte(modrm_base | reg);
    }
    if (size == 32) {
        emit_dword(imm);
    } else {
        emit_word(imm);
    }
}

/* ``<op> <width> [disp16], imm`` for the 80 / 81 / 83 /r families —
   shared by handle_add, handle_and, handle_or, handle_sub, handle_xor.
   ``rfield`` is the /r field (add=0, or=1, and=4, sub=5, xor=6) and
   the direct-disp16 ModR/M byte works out to ``0x06 | (rfield << 3)``.
   Three encodings picked by width and immediate range:
     - ``byte [mem], imm8``:            80 /r ib    (5 bytes)
     - ``word [mem], imm8`` (sign-ext): 83 /r ib    (5 bytes)
     - ``word [mem], imm16``:           81 /r iw    (6 bytes)
   ``word`` picks the 83 form in the signed-8-bit imm range and the
   81 form otherwise, matching NASM.

   Returns 1 when a ``byte|word [mem], imm`` shape was consumed and
   emitted, 0 when the cursor didn't start with either keyword — the
   caller then falls back to the register-taking ``emit_alu_binop``
   path.  Expects the caller to have skipped leading whitespace. */
__attribute__((regparm(1)))
int emit_alu_mem_imm(int rfield) {
    int size;
    if (match_word(STR_BYTE)) {
        size = 8;
    } else if (match_word(STR_WORD)) {
        size = 16;
    } else {
        return 0;
    }
    skip_ws();
    if (source_cursor[0] != '[') {
        abort_unknown();
    }
    source_cursor += 1;
    int disp = resolve_value();
    if (source_cursor[0] != ']') {
        abort_unknown();
    }
    source_cursor += 1;
    skip_comma();
    int imm = resolve_value();
    int modrm = 0x06 | (rfield << 3);
    if (size == 8) {
        emit_byte(0x80);
        emit_byte(modrm);
        emit_word(disp);
        emit_byte(imm & 0xFF);
    } else if (imm >= -128 && imm <= 127) {
        emit_byte(0x83);
        emit_byte(modrm);
        emit_word(disp);
        emit_byte(imm & 0xFF);
    } else {
        emit_byte(0x81);
        emit_byte(modrm);
        emit_word(disp);
        emit_word(imm);
    }
    return 1;
}

/* Emit one byte into the output stream.  Pass 1 only bumps
   current_address / output_total; pass 2 also stores the byte into
   OUTPUT_BUFFER at output_position, flushing to output_fd when the
   buffer fills.  Callers load ``value`` into AX via the ``regparm(1)``
   calling convention; the fastcall prologue spills AX into ``value``'s
   local slot so the body reads it through the normal local path.

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
void emit_byte(int value) {
    if (pass == 2) {
        asm("push si");
        output_buffer[output_position] = value;
        output_position += 1;
        if (output_position >= 512) {
            flush_output();
        }
        asm("pop si");
    }
    current_address += 1;
    output_total += 1;
}

/* Emit a ``[reg+disp]`` ModR/M (and any needed SIB + disp) for the
   current ``parse_operand_address_size``.  Under 16-bit addressing
   the rm field is picked by ``reg_to_rm`` and the disp passes
   through ``emit_modrm_disp``.  Under 32-bit addressing rm = the
   register number directly, with two special cases: ESP (reg 4)
   always needs a SIB byte (0x24: scale=0, no index, base=ESP), and
   EBP (reg 5) at mod=00 means disp32 with no base, so ``[ebp]``
   must be encoded as ``[ebp+0]`` with mod=01 disp8=0.  Every disp
   that doesn't fit in a signed byte widens to disp32 (vs disp16
   in 16-bit addressing). */
__attribute__((regparm(1)))
void emit_indexed_mem(int reg_field, int rm_reg_id, int disp) {
    if (parse_operand_address_size != 32) {
        int modrm = (reg_field << 3) | reg_to_rm(rm_reg_id);
        emit_modrm_disp(modrm, disp);
        return;
    }
    rm_reg_id &= 0xFF;
    int reg_bits = (reg_field & 0x7) << 3;
    if (rm_reg_id == 4) {
        /* ESP: emit the ModR/M + SIB pair as a single word (SIB=0x24
           goes in the high byte since emit_word is little-endian). */
        if (disp == 0) {
            emit_word(0x2400 | reg_bits | 0x04);
        } else if (disp >= -128 && disp <= 127) {
            emit_word(0x2400 | reg_bits | 0x44);
            emit_byte(disp & 0xFF);
        } else {
            emit_word(0x2400 | reg_bits | 0x84);
            emit_dword(disp);
        }
        return;
    }
    if (rm_reg_id == 5 && disp == 0) {
        /* [ebp] → [ebp+0] with mod=01 disp8=0; the disp byte is 0
           so it lands in the high byte of the word for free. */
        emit_word(reg_bits | 0x45);
        return;
    }
    if (disp == 0) {
        emit_byte(reg_bits | rm_reg_id);
    } else if (disp >= -128 && disp <= 127) {
        emit_word(((disp & 0xFF) << 8) | reg_bits | 0x40 | rm_reg_id);
    } else {
        emit_byte(reg_bits | 0x80 | rm_reg_id);
        emit_dword(disp);
    }
}

/* Emit the direct-memory ModR/M form plus its displacement at the
   current addressing width.  Under 16-bit addressing the rm field
   is 110 and the disp is 16-bit; under 32-bit addressing the rm
   field is 101 and the disp is 32-bit.  Used by lgdt / lidt,
   handle_mov's direct-memory branches, and any future caller
   encoding a plain ``[disp]`` memory operand. */
__attribute__((regparm(1)))
void emit_modrm_direct(int reg, int disp) {
    if (default_bits == 32) {
        emit_byte((reg << 3) | 0x05);
        emit_dword(disp);
    } else {
        emit_byte((reg << 3) | 0x06);
        emit_word(disp);
    }
}

/* Emit the ModR/M byte plus an optional disp8 / disp16 based on the
   displacement magnitude.  ``modrm`` is the mod=00 base (rm / reg
   fields already set); the helper ORs in 0x40 for disp8 and 0x80
   for disp16.  Used by every ``[reg+disp]`` memory-operand emit. */
__attribute__((regparm(1)))
void emit_modrm_disp(int modrm, int disp) {
    if (disp == 0) {
        emit_byte(modrm);
    } else if (disp >= -128 && disp <= 127) {
        emit_byte(modrm | 0x40);
        emit_byte(disp);
    } else {
        emit_byte(modrm | 0x80);
        emit_word(disp);
    }
}

/* Narrower sibling of ``emit_modrm_disp`` for the handlers that only
   accept disp8 (``inc_dec_handler``, ``handle_movzx``, ``handle_test``'s
   memory-dest branch).  ``disp == 0`` emits a bare ModR/M; a non-zero
   ``disp`` emits ``modrm | 0x40`` followed by the low byte.  Unlike
   ``emit_modrm_disp``, no disp16 fallback — the asm sources these
   handlers see never exceed ±128. */
__attribute__((regparm(1)))
void emit_modrm_disp8(int rm, int disp) {
    if (disp == 0) {
        emit_byte(rm);
    } else {
        emit_byte(rm | 0x40);
        emit_byte(disp & 0xFF);
    }
}

/* Emit the 0x66 operand-size prefix iff ``size`` is a 16/32 size
   that disagrees with the current ``default_bits``.  Under the
   default bits=16 mode a 32-bit operand acquires the prefix; under
   bits=32 a 16-bit operand does.  Used by ``emit_sized`` and every
   hand-written pmode-encoding site that used to emit 0x66 directly. */
__attribute__((regparm(1)))
void emit_operand_size_prefix(int size) {
    if (size != 16 && size != 32) {
        return;
    }
    if (size != default_bits) {
        emit_byte(0x66);
    }
}

/* Emit ``base`` for an 8-bit operand size, ``base + 1`` otherwise.
   Collapses the ``if (size == 8) emit_byte(X); else emit_byte(X+1);``
   split that every ALU / mov / cmp / test handler carries.
   Sizes that differ from ``default_bits`` get the 0x66 operand-size
   prefix in front, so the same 16/32 opcode body assembles both
   widths depending on the current [bits N] mode. */
__attribute__((regparm(1)))
void emit_sized(int base, int size) {
    if (size == 8) {
        emit_byte(base);
        return;
    }
    emit_operand_size_prefix(size);
    emit_byte(base + 1);
}

/* Same as ``emit_sized`` but also emits the 0x67 address-size prefix
   ahead of the opcode when the current ``parse_operand_address_size``
   disagrees with ``default_bits``.  Ordering follows NASM: operand-
   size prefix first (0x66), address-size prefix second (0x67), then
   the opcode.  Every memory-operand emit site uses this instead of
   ``emit_sized`` so instructions like ``mov eax, [esp]`` assemble
   identically to NASM across both bits modes. */
__attribute__((regparm(1)))
void emit_sized_mem(int base, int size) {
    emit_operand_size_prefix(size);
    emit_address_size_prefix(parse_operand_address_size);
    if (size == 8) {
        emit_byte(base);
    } else {
        emit_byte(base + 1);
    }
}

/* Emit a little-endian dword — the 32-bit companion to ``emit_word``.
   Used by the pmode-specific paths that widen imm16 / disp16 to
   imm32 / disp32 behind a 0x66 operand-size prefix. */
__attribute__((regparm(1)))
void emit_dword(int value) {
    emit_byte(value);
    emit_byte(value >> 8);
    /* cc.py's 16-bit codegen can't reach bits above 15 from a
       single ``int`` — write the upper half as zeros, which matches
       every 32-bit address the self-host needs to emit (all labels
       live below 64 KB). */
    emit_word(0);
}

/* Emit a size-tagged immediate: byte for ``size == 8``, little-endian
   word otherwise.  Used for the ``[mem], imm`` tail shared by two of
   ``handle_mov``'s branches (the other imm tails already fit
   ``emit_byte`` / ``emit_word`` directly). */
__attribute__((regparm(1)))
void emit_sized_imm(int value, int size) {
    if (size == 8) {
        emit_byte(value & 0xFF);
    } else if (size == 32) {
        emit_dword(value);
    } else {
        emit_word(value);
    }
}

/* ``emit_byte(value); emit_byte(value >> 8);`` — the low/high byte pair used
   for every ``disp16`` / ``imm16`` in the instruction handlers.
   ``regparm(1)`` puts ``value`` in AX so the call site compiles to
   ``mov ax, value ; call emit_word``.  ``emit_byte`` masks to a byte on
   store, so no ``& 0xFF`` guard is needed before passing the raw
   value. */
__attribute__((regparm(1)))
void emit_word(int value) {
    emit_byte(value);
    emit_byte(value >> 8);
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
    skip_ws();
    int bx = jump_index;
    jump_index += 1;
    int current_size = far_read8(JUMP_TABLE + bx);
    int use_short = 1;
    if (current_size == 0) {
        /* Currently short; check whether the target is still in
           range for pass 1 -- if the distance grew out of
           rel8, flip the bit to long and set changed_flag so
           the pass-1 convergence loop runs another iteration. */
        if (pass == 1) {
            if (peek_label_target()) {
                int rel = peek_label_value - (current_address + 2);
                if (rel < -128 || rel > 127) {
                    far_write8(JUMP_TABLE + bx, 1);
                    changed_flag = 1;
                    use_short = 0;
                }
            }
        }
    } else {
        /* Currently long; attempt to shrink in pass 1 if the
           target has moved into rel8 range.  Forward jumps need
           an extra +4/-1 correction because the 4-byte near form
           straddles the comparison point (and the ``jmp rel8``
           0xEB opcode shrinks to 2 bytes rather than 3). */
        use_short = 0;
        if (pass == 1) {
            if (peek_label_target()) {
                int target = peek_label_value;
                int base = current_address;
                if (target >= base) {
                    base += 4;
                    if (opcode == 0xEB) {
                        base -= 1;
                    }
                } else {
                    base += 2;
                }
                int rel = target - base;
                if (rel >= -128 && rel <= 127) {
                    far_write8(JUMP_TABLE + bx, 0);
                    changed_flag = 1;
                    use_short = 1;
                }
            }
        }
    }
    if (use_short) {
        emit_byte(opcode);
        int disp = resolve_label() - (current_address + 1);
        emit_byte(disp);
    } else {
        if (opcode == 0xEB) {
            emit_byte(0xE9);
        } else {
            emit_byte(0x0F);
            emit_byte(opcode + 0x10);
        }
        int disp = resolve_label() - (current_address + 2);
        emit_word(disp);
    }
}

/* Expand macro *idx*.  source_cursor points past the macro name, at
   the (possibly empty) comma-separated argument list.  Arguments
   are copied to ``macro_args_text`` with each arg null-terminated;
   ``macro_arg_starts[i]`` records where the i-th arg begins.  The
   body is then walked one line at a time: ``%1``..``%9`` runs are
   replaced with the corresponding argument text (higher indices
   are silently dropped), the expanded line is written into
   ``line_buffer``, and ``parse_line`` is re-invoked to process it
   as if it had been the current source line. */
__attribute__((regparm(1)))
void expand_macro(int idx) {
    /* Snapshot source_cursor into a non-SI-pinned local BEFORE any
       indexed global access — cc.py uses SI as scratch for computing
       ``macro_*[idx]`` addresses, which would clobber the SI-pinned
       source_cursor and leave cc.py's live-range tracking stale. */
    char *cursor = source_cursor;
    int argcount = macro_arg_counts[idx];
    if (argcount > 9) {
        argcount = 9;
    }
    int pos = 0;
    int i = 0;
    while (i < argcount) {
        while (cursor[0] == ' ' || cursor[0] == '\t') {
            cursor += 1;
        }
        macro_arg_starts[i] = pos;
        while (cursor[0] != ',' && cursor[0] != '\0' && cursor[0] != ';') {
            macro_args_text[pos] = cursor[0];
            pos += 1;
            cursor += 1;
        }
        while (pos > macro_arg_starts[i] && (macro_args_text[pos - 1] == ' ' || macro_args_text[pos - 1] == '\t')) {
            pos -= 1;
        }
        macro_args_text[pos] = '\0';
        pos += 1;
        if (cursor[0] == ',') {
            cursor += 1;
        }
        i += 1;
    }
    source_cursor = cursor;
    int body_offset = macro_body_starts[idx];
    int body_end = body_offset + macro_body_lengths[idx];
    while (body_offset < body_end) {
        int dst = 0;
        while (macro_body_buffer[body_offset] != '\0') {
            char ch = macro_body_buffer[body_offset];
            char next = macro_body_buffer[body_offset + 1];
            if (ch == '%' && next >= '1' && next <= '9') {
                int n = next - '1';
                if (n < argcount) {
                    int k = macro_arg_starts[n];
                    while (macro_args_text[k] != '\0') {
                        line_buffer[dst] = macro_args_text[k];
                        dst += 1;
                        k += 1;
                    }
                }
                body_offset += 2;
            } else {
                line_buffer[dst] = ch;
                dst += 1;
                body_offset += 1;
            }
        }
        line_buffer[dst] = '\0';
        body_offset += 1;
        parse_line();
    }
}

/* Linear scan over the macro table matching the identifier at
   source_cursor.  Returns the macro index (and advances
   source_cursor past the name) on hit; returns -1 with the cursor
   unchanged on miss.  Uses ``cursor`` as a local copy of
   source_cursor so byte-comparison loops don't have to juggle SI
   between the source cursor and macro_names indexing. */
int find_macro() {
    char *cursor = source_cursor;
    int len = 0;
    while (is_ident_char(cursor[len])) {
        len += 1;
    }
    if (len == 0) {
        return -1;
    }
    int i = 0;
    while (i < macro_count) {
        int base = i * MACRO_NAME_LEN;
        int j = 0;
        int match = 1;
        while (j < len) {
            if (macro_names[base + j] != cursor[j]) {
                match = 0;
            }
            j += 1;
        }
        if (match != 0 && macro_names[base + len] == '\0') {
            source_cursor = cursor + len;
            return i;
        }
        i += 1;
    }
    return -1;
}

/* Write the accumulated OUTPUT_BUFFER (output_position bytes) to
   output_fd via SYS_IO_WRITE, then reset the position.  No-op when
   nothing is queued.  Uses the ES-safe ``syscall`` wrapper so
   ES=SYMBOL_SEGMENT survives the ``int 30h``. */
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

/* Single-byte / two-byte emitters for zero-operand mnemonics.  Each
   handler is dispatched through ``mnemonic_table`` (inline-asm tail
   of this file): ``parse_mnemonic`` does an indirect ``call`` on the
   label.  ``emit_byte`` uses the ``regparm(1)`` fastcall convention
   so each call site compiles to ``mov ax, OPCODE ; call emit_byte``. */
void handle_aam() {
    emit_word(0x0AD4);
}

void handle_adc() {
    adc_sbb_handler(0xD0);
}

void handle_add() {
    skip_ws();
    if (emit_alu_mem_imm(0)) {
        return;
    }
    emit_alu_binop(0);
}

void handle_and() {
    skip_ws();
    if (emit_alu_mem_imm(4)) {
        return;
    }
    emit_alu_binop(4);
}

/* ``call <label>`` (E8 rel16) and ``call [reg+disp8]`` (FF /2) —
   the only two call forms the self-host needs.  The indirect form
   requires a non-zero disp that fits in a signed byte; anything
   else jumps to abort_unknown. */
void handle_call() {
    skip_ws();
    if (source_cursor[0] == '[') {
        int packed_operand = parse_operand();
        int type = (packed_operand >> 8) & 0xFF;
        int register_id = packed_operand & 0xFF;
        int value = parse_operand_value;
        if (type != 3 || value == 0 || value < -128 || value > 127) {
            abort_unknown();
        }
        emit_address_size_prefix(parse_operand_address_size);
        emit_byte(0xFF);
        emit_indexed_mem(2, register_id, value);
    } else {
        emit_byte(0xE8);
        int target = resolve_label();
        int delta = target - current_address - 2;
        emit_word(delta);
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
    int packed_operand = parse_operand();
    int type1 = (packed_operand >> 8) & 0xFF;
    int register1_id = packed_operand & 0xFF;
    int value1 = parse_operand_value;
    int size1 = op1_size;
    skip_comma();
    if (type1 == 0) {
        int packed_register2 = parse_register();
        if (packed_register2 >= 0) {
            emit_sized(0x38, size1);
            emit_byte(make_modrm_reg_reg_impl(packed_register2 & 0xFF, register1_id));
            return;
        }
        if (source_cursor[0] == '[') {
            int packed_operand2 = parse_operand();
            int type2 = (packed_operand2 >> 8) & 0xFF;
            int register2_id = packed_operand2 & 0xFF;
            int value2 = parse_operand_value;
            if (type2 == 2) {
                emit_sized_mem(0x3A, size1);
                emit_modrm_direct(register1_id, value2);
                return;
            }
            if (type2 == 3) {
                emit_sized_mem(0x3A, size1);
                emit_indexed_mem(register1_id, register2_id, value2);
                return;
            }
        }
        int imm = resolve_value();
        emit_alu_reg_imm(0x38, register1_id, size1, imm);
        return;
    }
    if (type1 != 2 && type1 != 3) {
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
    emit_operand_size_prefix(size1);
    emit_address_size_prefix(parse_operand_address_size);
    emit_byte(opcode);
    if (type1 == 2) {
        emit_modrm_direct(7, value1);
    } else {
        emit_indexed_mem(7, register1_id, value1);
    }
    if (is_imm8) {
        emit_byte(imm & 0xFF);
    } else {
        emit_sized_imm(imm, size1);
    }
}

void handle_dec() {
    inc_dec_handler(1);
}

void handle_div() {
    unary_f6f7(0xF0);
}

void handle_in() {
    /* ``in al, dx`` → EC  (byte port read).
       ``in ax, dx`` → ED  (word port read).
       Operands are fixed (DX is the port, AL/AX is the destination);
       parser validates the form and the data-register size picks the
       opcode width. */
    skip_ws();
    int data_reg = parse_register();
    if (data_reg < 0 || (data_reg & 0xFF) != 0) {
        die("Error: in expects al or ax as data\n");
    }
    skip_comma();
    int port_reg = parse_register();
    if (port_reg < 0 || (port_reg & 0xFF) != 2 || (port_reg >> 8) != 16) {
        die("Error: in expects dx as port\n");
    }
    if ((data_reg >> 8) == 8) {
        emit_byte(0xEC);
    } else {
        emit_byte(0xED);
    }
}

void handle_inc() {
    inc_dec_handler(0);
}

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
    /* ``jmp dword <selector>:<label>`` — far jmp with a 32-bit
       offset.  Under bits=16 the 0x66 operand-size prefix flips the
       EA opcode's offset from word to dword; under bits=32 the dword
       offset is already the default and the prefix is omitted.  The
       ptr16:32 immediate tail is fixed either way.  The label
       resolves via ``resolve_label`` so pass 1 places a same-size
       placeholder (instruction width is constant within a mode). */
    if (match_word(STR_DWORD)) {
        skip_ws();
        int selector = resolve_value();
        skip_ws();
        if (source_cursor[0] == ':') {
            source_cursor += 1;
            skip_ws();
        }
        int offset = resolve_label();
        emit_operand_size_prefix(32);
        emit_byte(0xEA);
        emit_dword(offset);
        emit_word(selector);
        return;
    }
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

void handle_lea() {
    skip_ws();
    int packed_operand1 = parse_operand();
    int register1_id = packed_operand1 & 0xFF;
    skip_comma();
    int packed_operand2 = parse_operand();
    int register2_id = packed_operand2 & 0xFF;
    int value2 = parse_operand_value;
    emit_byte(0x8D);
    emit_indexed_mem(register1_id, register2_id, value2);
}

/* ``lgdt [mem]`` / ``lidt [mem]`` — load the GDT / IDT descriptor
   register from a 6-byte memory operand.  Both are pmode bootstrap
   essentials.  Encoded as ``0F 01 /r`` with reg field 2 for lgdt
   and 3 for lidt; the shared ``emit_modrm_direct`` helper packs
   the reg field into mod=00 rm=110 (direct disp16) for the only
   memory shape we need.  The self-host never sees the ``[reg+disp]``
   forms of these instructions. */
void handle_lgdt() {
    skip_ws();
    parse_operand();
    emit_address_size_prefix(parse_operand_address_size);
    emit_word(0x010F);
    emit_modrm_direct(2, parse_operand_value);
}

void handle_lidt() {
    skip_ws();
    parse_operand();
    emit_address_size_prefix(parse_operand_address_size);
    emit_word(0x010F);
    emit_modrm_direct(3, parse_operand_value);
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
    /* ``mov crN, r32`` — the protected-mode entry-and-exit pair.
       Emits 0F 22 /r with the control register in the reg field
       and the 32-bit GPR in the rm field.  Must come before the
       ``es`` segment-register probe below so the cr-prefixed
       destination is matched first; ``es`` can never start with
       'c' so there's no overlap the other direction. */
    int creg_dst = parse_creg();
    if (creg_dst >= 0) {
        skip_comma();
        int packed_register = parse_register();
        emit_word(0x220F);
        emit_byte(0xC0 | (creg_dst << 3) | (packed_register & 0xFF));
        return;
    }
    if (source_cursor[0] == 'e' && source_cursor[1] == 's') {
        char *saved = source_cursor;
        source_cursor += 2;
        skip_ws();
        if (source_cursor[0] == ',') {
            source_cursor += 1;
            skip_ws();
            int packed_operand = parse_operand();
            emit_byte(0x8E);
            emit_byte(0xC0 | (packed_operand & 0xFF));
            return;
        }
        source_cursor = saved;
    }
    int packed_operand1 = parse_operand();
    int type1 = (packed_operand1 >> 8) & 0xFF;
    int register1_id = packed_operand1 & 0xFF;
    int value1 = parse_operand_value;
    int op1_parsed_size = op1_size;
    skip_comma();
    /* ``mov r32, crN`` — companion of the cr-as-destination path.
       Emits 0F 20 /r.  Only legal when op1 is a 32-bit GPR; for
       any other op1 shape fall through to the general parse path
       below so the source operand can still be an identifier
       that happens to start with 'c' or 'C'.  The cr probe has to
       happen before ``parse_operand`` touches the second operand
       (cr0 is not in ``register_table`` and would be consumed as
       a symbol). */
    if (type1 == 0 && op1_parsed_size == 32) {
        int creg_src = parse_creg();
        if (creg_src >= 0) {
            emit_word(0x200F);
            emit_byte(0xC0 | (creg_src << 3) | register1_id);
            return;
        }
    }
    int packed_operand2 = parse_operand();
    int type2 = (packed_operand2 >> 8) & 0xFF;
    int register2_id = packed_operand2 & 0xFF;
    int value2 = parse_operand_value;
    /* Legacy sizing rule: ``mov [mem], reg`` doesn't set op1_size
       during op1 parse (direct-memory operands carry no size), so
       the width is read from op1_size after op2's register parse
       has set it.  Register-first shapes captured the correct
       size in op1_parsed_size above — use that so a subsequent
       op2 parse (e.g. ``mov ax, [disp]``) can't clobber the
       width when both operands disagree. */
    int size1 = op1_size;
    if (type1 == 0) {
        size1 = op1_parsed_size;
    }
    if (type1 == 0) {
        if (type2 == 0) {
            emit_sized(0x88, size1);
            emit_byte(make_modrm_reg_reg_impl(register2_id, register1_id));
            return;
        }
        if (type2 == 1) {
            if (size1 == 8) {
                emit_byte(0xB0 | register1_id);
                emit_byte(value2 & 0xFF);
            } else {
                emit_operand_size_prefix(size1);
                emit_byte(0xB8 | register1_id);
                if (size1 == 32) {
                    emit_dword(value2);
                } else {
                    emit_word(value2);
                }
            }
            return;
        }
        if (type2 == 2) {
            if (size1 == 8 && register1_id == 0) {
                emit_byte(0xA0);
                emit_address_disp(value2);
            } else if (size1 != 8 && register1_id == 0) {
                emit_operand_size_prefix(size1);
                emit_byte(0xA1);
                emit_address_disp(value2);
            } else {
                emit_sized_mem(0x8A, size1);
                emit_modrm_direct(register1_id, value2);
            }
            return;
        }
        if (type2 == 3) {
            emit_sized_mem(0x8A, size1);
            emit_indexed_mem(register1_id, register2_id, value2);
            return;
        }
        abort_unknown();
    }
    if (type1 == 2) {
        if (type2 == 0) {
            if (size1 == 8 && register2_id == 0) {
                emit_byte(0xA2);
                emit_address_disp(value1);
            } else if (size1 != 8 && register2_id == 0) {
                emit_operand_size_prefix(size1);
                emit_byte(0xA3);
                emit_address_disp(value1);
            } else {
                emit_sized_mem(0x88, size1);
                emit_modrm_direct(register2_id, value1);
            }
            return;
        }
        if (type2 == 1) {
            emit_sized_mem(0xC6, size1);
            emit_modrm_direct(0, value1);
            emit_sized_imm(value2, size1);
            return;
        }
        return;
    }
    if (type1 == 3) {
        if (type2 == 0) {
            emit_sized_mem(0x88, size1);
            emit_indexed_mem(register2_id, register1_id, value1);
            return;
        }
        if (type2 == 1) {
            emit_sized_mem(0xC6, size1);
            emit_indexed_mem(0, register1_id, value1);
            emit_sized_imm(value2, size1);
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
    int packed_register = parse_register();
    int register1_id = packed_register & 0xFF;
    skip_comma();
    int packed_operand = parse_operand();
    int type2 = (packed_operand >> 8) & 0xFF;
    int register2_id = packed_operand & 0xFF;
    int value2 = parse_operand_value;
    if (type2 != 0) {
        emit_address_size_prefix(parse_operand_address_size);
    }
    emit_word(0xB60F);
    if (type2 == 0) {
        emit_byte(0xC0 | (register1_id << 3) | register2_id);
    } else {
        emit_indexed_mem(register1_id, register2_id, value2);
    }
}

void handle_mul() {
    unary_f6f7(0xE0);
}

void handle_neg() {
    unary_f6f7(0xD8);
}

void handle_not() {
    unary_f6f7(0xD0);
}

void handle_or() {
    skip_ws();
    if (emit_alu_mem_imm(1)) {
        return;
    }
    emit_alu_binop(1);
}

void handle_out() {
    /* ``out dx, al`` → EE  (byte port write).
       ``out dx, ax`` → EF  (word port write).
       Operands are fixed (DX is the port, AL/AX is the data source);
       parser validates the form and the data-register size picks the
       opcode width. */
    skip_ws();
    int port_reg = parse_register();
    if (port_reg < 0 || (port_reg & 0xFF) != 2 || (port_reg >> 8) != 16) {
        die("Error: out expects dx as port\n");
    }
    skip_comma();
    int data_reg = parse_register();
    if (data_reg < 0 || (data_reg & 0xFF) != 0) {
        die("Error: out expects al or ax as data\n");
    }
    if ((data_reg >> 8) == 8) {
        emit_byte(0xEE);
    } else {
        emit_byte(0xEF);
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
    if (match_seg_ds_es(0x1F, 0x07)) {
        return;
    }
    int packed_register = parse_register();
    int size = (packed_register >> 8) & 0xFF;
    emit_operand_size_prefix(size);
    emit_byte(0x58 | (packed_register & 0xFF));
}

void handle_popa() {
    emit_byte(0x61);
}

void handle_push() {
    skip_ws();
    if (match_seg_ds_es(0x1E, 0x06)) {
        return;
    }
    int packed_register = parse_register();
    if (packed_register >= 0) {
        int size = (packed_register >> 8) & 0xFF;
        emit_operand_size_prefix(size);
        emit_byte(0x50 | (packed_register & 0xFF));
        return;
    }
    /* ``push [word|dword] imm`` or ``push [word|dword] [mem]`` —
       the size token forces the push width.  Without a token the
       width defaults to the current bits mode so ``push 0`` under
       bits=32 pushes a dword.  Memory operands use the ``FF /6``
       encoding; immediates use the 0x6A short / 0x68 long form. */
    int size = default_bits;
    if (match_word(STR_WORD)) {
        size = 16;
        skip_ws();
    } else if (match_word(STR_DWORD)) {
        size = 32;
        skip_ws();
    }
    if (source_cursor[0] == '[') {
        int packed_operand = parse_operand();
        int type = (packed_operand >> 8) & 0xFF;
        int register_id = packed_operand & 0xFF;
        int value = parse_operand_value;
        emit_operand_size_prefix(size);
        emit_address_size_prefix(parse_operand_address_size);
        emit_byte(0xFF);
        if (type == 2) {
            emit_modrm_direct(6, value);
        } else {
            emit_indexed_mem(6, register_id, value);
        }
        return;
    }
    int value = resolve_value();
    emit_operand_size_prefix(size);
    if (value >= -128 && value <= 127) {
        emit_byte(0x6A);
        emit_byte(value & 0xFF);
    } else if (size == 32) {
        emit_byte(0x68);
        emit_dword(value);
    } else {
        emit_byte(0x68);
        emit_word(value);
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

void handle_sbb() {
    adc_sbb_handler(0xD8);
}

void handle_scasb() {
    emit_byte(0xAE);
}

void handle_shl() {
    shift_handler(0xE0);
}

void handle_shr() {
    shift_handler(0xE8);
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

/* ``sub`` supports the shared ALU binop grammar plus one bespoke
   form that only sub uses — ``sub word [disp16], imm16`` (the
   dedicated 81 /5 iw path, the TCP-checksum update idiom in
   asm.asm).  The wrapper peels off that path before delegating. */
void handle_sub() {
    skip_ws();
    if (emit_alu_mem_imm(5)) {
        return;
    }
    emit_alu_binop(5);
}

/* ``test r, r`` / ``test r, imm`` / ``test byte [mem], imm8`` —
   the three forms self-host needs.  parse_operand seeds op1; the
   second operand branches on parse_register success (register →
   84/85 r-r) vs failure (immediate → A8/A9 short for AL/AX, else
   F6/F7 modrm).  Memory destination uses F6 /0 with the op1 info
   already parsed (disp8, disp16, or bare [reg]). */
void handle_test() {
    skip_ws();
    int packed_operand = parse_operand();
    int type1 = (packed_operand >> 8) & 0xFF;
    int register1_id = packed_operand & 0xFF;
    int value1 = parse_operand_value;
    int size1 = op1_size;
    skip_comma();
    if (type1 == 0) {
        skip_ws();
        int packed_register2 = parse_register();
        if (packed_register2 >= 0) {
            emit_sized(0x84, size1);
            emit_byte(make_modrm_reg_reg_impl(packed_register2 & 0xFF, register1_id));
        } else {
            int imm = resolve_value();
            if (size1 == 8) {
                if (register1_id == 0) {
                    emit_byte(0xA8);
                } else {
                    emit_byte(0xF6);
                    emit_byte(0xC0 | register1_id);
                }
                emit_byte(imm & 0xFF);
            } else {
                if (register1_id == 0) {
                    emit_byte(0xA9);
                } else {
                    emit_byte(0xF7);
                    emit_byte(0xC0 | register1_id);
                }
                emit_word(imm);
            }
        }
    } else {
        int imm = resolve_value();
        emit_address_size_prefix(parse_operand_address_size);
        emit_byte(0xF6);
        if (type1 == 2) {
            emit_modrm_direct(0, value1);
        } else {
            emit_indexed_mem(0, register1_id, value1);
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
        source_cursor += 1;
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
    define_label_here(is_local);
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
    int packed_register1 = parse_register();
    int register1_id = packed_register1 & 0xFF;
    int size1 = (packed_register1 >> 8) & 0xFF;
    skip_comma();
    int packed_register2 = parse_register();
    int register2_id = packed_register2 & 0xFF;
    if (size1 != 8 && register1_id == 0) {
        emit_byte(0x90 | register2_id);
    } else if (size1 != 8 && register2_id == 0) {
        emit_byte(0x90 | register1_id);
    } else if (size1 == 8) {
        emit_byte(0x86);
        emit_byte(make_modrm_reg_reg_impl(register1_id, register2_id));
    } else {
        emit_byte(0x87);
        emit_byte(make_modrm_reg_reg_impl(register1_id, register2_id));
    }
}

void handle_xor() {
    skip_ws();
    if (emit_alu_mem_imm(6)) {
        return;
    }
    emit_alu_binop(6);
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

/* ``inc`` / ``dec`` with r8 / r16 / memory destination.  r16 uses
   the 40+reg / 48+reg one-byte forms; r8 and memory use FE/FF with
   a /0 (inc) or /1 (dec) reg field.  Memory dispatch mirrors the
   three parse_operand op2 types: 0=reg (handled above), 2=direct
   disp16, 3=reg+disp8 (or bare reg when disp == 0).  ``rfield`` is
   the /r constant (0 inc, 1 dec); the helper shifts it into position
   and ORs it into every register / modrm byte that carries the
   inc-vs-dec distinction. */
__attribute__((regparm(1)))
void inc_dec_handler(int rfield) {
    skip_ws();
    int packed_operand = parse_operand();
    int type = (packed_operand >> 8) & 0xFF;
    int register_id = packed_operand & 0xFF;
    int value = parse_operand_value;
    int size = op1_size;
    int reg_shift = rfield << 3;
    if (type == 0) {
        if (size == 8) {
            emit_byte(0xFE);
            emit_byte(0xC0 | reg_shift | register_id);
        } else {
            emit_byte(0x40 | reg_shift | register_id);
        }
    } else {
        emit_sized_mem(0xFE, size);
        if (type == 2) {
            emit_modrm_direct(rfield, value);
        } else {
            emit_indexed_mem(rfield, register_id, value);
        }
    }
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
    include_depth -= 1;
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
        i += 1;
        j += 1;
    }
    int k = 0;
    while (include_push_arg[k] != '\0') {
        include_path[j] = include_push_arg[k];
        j += 1;
        k += 1;
    }
    include_path[j] = '\0';
    source_fd = open_file_ro(include_path);
    if (source_fd == -1) {
        error_flag = 1;
        return;
    }
    source_buffer_position = 0;
    source_buffer_valid = 0;
    include_depth += 1;
}

/* Classify an ASCII byte as an identifier character — ``[a-zA-Z0-9_]``.
   The ``.`` prefix that marks local labels is NOT an ident char for
   our purposes; label-scan loops add it via an explicit ``|| c == '.'``
   next to the call.  ``regparm(1)`` + ``carry_return`` so cc.py lowers
   ``if (is_ident_char(c))`` to ``mov ax, c ; call is_ident_char ; jc/jnc``. */
__attribute__((regparm(1)))
__attribute__((carry_return))
int is_ident_char(int c) {
    return (c >= 'a' && c <= 'z')
            || (c >= 'A' && c <= 'Z')
            || (c >= '0' && c <= '9')
            || c == '_';
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

/* Resolve the identifier at ``source_cursor`` — scan an identifier-
   with-dot span, null-terminate it in place, pick the scope by the
   leading ``.`` prefix, call ``symbol_lookup``, then restore the
   delimiter byte.  ``advance`` picks whether ``source_cursor`` ends
   up past the identifier (1, used by ``resolve_value``'s symbol
   branch) or rewound to the name start (0, used by
   ``peek_label_target``).  ``last_symbol_index`` carries the
   hit/miss signal (0xFFFF on miss) so both callers can branch on it
   without a separate return code. */
__attribute__((regparm(1)))
int lookup_ident_here(int advance) {
    char *name_start = source_cursor;
    scan_ident_dot();
    char *end_pos = source_cursor;
    char delim = end_pos[0];
    end_pos[0] = '\0';
    int scope = 0xFFFF;
    if (name_start[0] == '.') {
        scope = global_scope;
    }
    source_cursor = name_start;
    int value = symbol_lookup(scope);
    end_pos[0] = delim;
    if (advance) {
        source_cursor = end_pos;
    }
    return value;
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
    default_bits = 16;
    parse_operand_address_size = 16;
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

/* Build a register/register ModR/M byte.  ``regparm(1)`` — reg in
   AX, rm on stack; returns ``0xC0 | (reg << 3) | rm`` in AX.
   Previous legacy ``make_modrm_reg_reg`` thunk (AL/BL in, modrm out)
   retired with its ~7 inline-asm callers. */
__attribute__((regparm(1)))
int make_modrm_reg_reg_impl(int register_id, int rm) {
    register_id &= 0xFF;
    rm &= 0xFF;
    return 0xC0 | (register_id << 3) | rm;
}

/* Peek the 2-char segment register (``ds`` or ``es``) at
   ``source_cursor`` and emit the corresponding push / pop opcode
   (caller supplies the pair: push ds = 0x1E, push es = 0x06,
   pop ds = 0x1F, pop es = 0x07).  On match ``source_cursor``
   advances past the token and the helper returns 1; on miss it
   leaves the cursor alone and returns 0. */
__attribute__((regparm(1)))
int match_seg_ds_es(int ds_opcode, int es_opcode) {
    if (source_cursor[0] == 'd' && source_cursor[1] == 's') {
        source_cursor += 2;
        emit_byte(ds_opcode);
        return 1;
    }
    if (source_cursor[0] == 'e' && source_cursor[1] == 's') {
        source_cursor += 2;
        emit_byte(es_opcode);
        return 1;
    }
    return 0;
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
            s += 32;
        }
        if (s != keyword[0]) {
            source_cursor = saved;
            return 0;
        }
        source_cursor += 1;
        keyword += 1;
    }
    if (is_ident_char(source_cursor[0])) {
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
    source_cursor += 1;
    int disp = resolve_value();
    if (source_cursor[0] != ']') {
        abort_unknown();
    }
    source_cursor += 1;
    skip_comma();
    int packed_register = parse_register();
    if (packed_register < 0) {
        abort_unknown();
    }
    emit_byte(opcode);
    emit_modrm_direct(packed_register & 0xFF, disp);
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

/* Shared body for ``dw`` / ``dd`` directives — ``dw`` is
   ``parse_d_values(0)`` and ``dd`` is ``parse_d_values(1)`` (the
   extra zero word past the 16-bit value).  Comma-separated
   operand list; each operand evaluates via resolve_value. */
__attribute__((regparm(1)))
void parse_d_values(int extra_word) {
    skip_ws();
    while (1) {
        int value = resolve_value();
        emit_word(value);
        if (extra_word != 0) {
            emit_word(0);
        }
        skip_ws();
        if (source_cursor[0] != ',') {
            return;
        }
        source_cursor += 1;
        skip_ws();
    }
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
            source_cursor += 1;
            while (1) {
                char c = source_cursor[0];
                if (c == '`') {
                    source_cursor += 1;
                    break;
                }
                if (c == '\0') {
                    return;
                }
                if (c == '\\') {
                    source_cursor += 1;
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
                        source_cursor += 1;
                        int hi = hex_digit(source_cursor[0]);
                        source_cursor += 1;
                        int lo = hex_digit(source_cursor[0]);
                        emit_byte((hi << 4) | lo);
                    } else {
                        emit_byte('\\');
                        emit_byte(esc);
                    }
                    source_cursor += 1;
                } else {
                    emit_byte(c);
                    source_cursor += 1;
                }
            }
        } else if (source_cursor[0] == '\'' && source_cursor[2] != '\'') {
            source_cursor += 1;
            while (1) {
                char c = source_cursor[0];
                if (c == '\'') {
                    source_cursor += 1;
                    break;
                }
                if (c == '\0') {
                    return;
                }
                emit_byte(c);
                source_cursor += 1;
            }
        } else {
            int value = resolve_value();
            emit_byte(value);
        }
        skip_ws();
        if (source_cursor[0] != ',') {
            return;
        }
        source_cursor += 1;
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
        source_cursor += 1;
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
                source_cursor += 1;
            }
            if (source_cursor[0] == '\0') {
                return;
            }
            source_cursor[0] = '\0';
            source_cursor += 1;
            skip_ws();
            int value = resolve_value();
            if (pass == 1) {
                source_cursor = name;
                symbol_add_constant(value);
            }
            return;
        }
        if (match_word(STR_MACRO)) {
            define_macro();
            return;
        }
        if (match_word(STR_INCLUDE) == 0) {
            return;
        }
        skip_ws();
        if (source_cursor[0] != '"') {
            return;
        }
        source_cursor += 1;
        char *fname = source_cursor;
        while (source_cursor[0] != '"') {
            if (source_cursor[0] == '\0') {
                return;
            }
            source_cursor += 1;
        }
        source_cursor[0] = '\0';
        source_cursor = fname;
        include_push();
        return;
    }
    if (match_word(STR_ALIGN)) {
        skip_ws();
        int n = resolve_value();
        /* Power-of-two alignment only — every NASM source we assemble
           uses 2/4/8/16.  ``mask = n - 1`` picks the low bits that
           must be zero; pad one NOP (0x90) at a time until they are.
           NASM's flat-binary output uses 0x90 as the default fill,
           so matching byte-for-byte requires it. */
        int mask = n - 1;
        while ((current_address & mask) != 0) {
            emit_byte(0x90);
        }
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
            count -= 1;
        }
        return;
    }
    if (match_word(STR_DB)) {
        skip_ws();
        parse_db();
        return;
    }
    if (match_word(STR_DW)) {
        parse_d_values(0);
        return;
    }
    if (match_word(STR_DD)) {
        parse_d_values(1);
        return;
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
    /* ``[bits N]`` — NASM's bracketed directive switching the default
       operand-size mode.  Must fire before label scanning because ``[``
       never appears in a label.  No other bracketed directive is
       supported; the handler silently skips through ``]`` so trailing
       junk can't reach parse_mnemonic. */
    if (source_cursor[0] == '[') {
        source_cursor += 1;
        skip_ws();
        if (match_word(STR_BITS)) {
            skip_ws();
            int value = resolve_value();
            default_bits = value;
        }
        while (source_cursor[0] != ']' && source_cursor[0] != '\0') {
            source_cursor += 1;
        }
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
            define_label_here(is_local);
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
                symbol_add_constant(value);
            }
            space_pos[0] = ' ';
            return;
        }
        source_cursor += 1;
    }
}

/* Instruction dispatcher: linear scan over ``mnemonic_table``
   trying each keyword against the source cursor via ``match_word``.
   On match, invoke the matching handler.  Walking past the
   2-byte zero terminator (``mnemonic_keyword_at`` returns NULL)
   falls through to ``handle_unknown_word`` so bare labels
   (``USAGE db ...`` without a colon) still reach their
   symbol-table branch. */
void parse_mnemonic() {
    int macro_index = find_macro();
    if (macro_index >= 0) {
        expand_macro(macro_index);
        return;
    }
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
        index += 1;
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
            source_cursor += 2;
            while (1) {
                d = hex_digit(source_cursor[0]);
                if (d < 0) {
                    return value;
                }
                value = (value << 4) | d;
                source_cursor += 1;
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
            source_cursor += 1;
        } else if (c >= 'A' && c <= 'F') {
            source_cursor += 1;
        } else if (c >= 'a' && c <= 'f') {
            source_cursor += 1;
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
                source_cursor += 1;
                return value;
            }
            d = hex_digit(c);
            if (d < 0) {
                c = source_cursor[0];
                if (c == 'h' || c == 'H') {
                    source_cursor += 1;
                }
                return value;
            }
            value = (value << 4) | d;
            source_cursor += 1;
        }
    }
    /* Decimal */
    while (1) {
        c = source_cursor[0];
        if (c < '0' || c > '9') {
            return value;
        }
        value = value * 10 + (c - '0');
        source_cursor += 1;
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
   afterwards).  Returns the packed ``(type << 8) | reg`` in AX
   — type is 0=reg, 1=imm, 2=mem_direct, 3=mem_reg_disp, and reg
   carries the register number for the reg / mem_reg_disp forms.
   The displacement / immediate lands in ``parse_operand_value``
   (cc.py's return ABI is AX-only).  Updates ``op1_size`` on
   register / byte/word-prefix paths. */
int parse_operand() {
    skip_ws();
    /* ``byte`` / ``word`` / ``dword`` size prefix — match_word already
       rewinds ``source_cursor`` on miss, so no manual backtrack
       needed.  The ``dword`` form appears in pmode sources with
       ``cmp dword [reg], imm`` / ``inc dword [reg]`` shapes. */
    if (match_word(STR_BYTE)) {
        op1_size = 8;
        skip_ws();
    } else if (match_word(STR_WORD)) {
        op1_size = 16;
        skip_ws();
    } else if (match_word(STR_DWORD)) {
        op1_size = 32;
        skip_ws();
    }
    if (source_cursor[0] != '[') {
        /* Register or immediate. */
        int packed_register = parse_register();
        if (packed_register >= 0) {
            op1_size = (packed_register >> 8) & 0xFF;
            return packed_register & 0xFF;           /* type=0 (reg), reg in low byte */
        }
        int imm = resolve_value();
        parse_operand_value = imm;
        return 1 << 8;                  /* type=1 (imm) */
    }
    /* Memory operand starting at ``[``.  Default addressing size
       matches the current bits mode; gets bumped to 32 below if the
       base register is an e-prefixed reg. */
    parse_operand_address_size = default_bits;
    source_cursor += 1;
    skip_ws();
    /* ``[es:...]`` segment override: emit 0x26 prefix, skip past. */
    if (source_cursor[0] == 'e' && source_cursor[1] == 's' && source_cursor[2] == ':') {
        emit_byte(0x26);
        source_cursor += 3;
        skip_ws();
    }
    /* Try ``[reg...]`` form (register first inside brackets). */
    int packed_register = parse_register();
    if (packed_register >= 0) {
        int register_id = packed_register & 0xFF;
        int reg_size = (packed_register >> 8) & 0xFF;
        if (reg_size == 16 || reg_size == 32) {
            parse_operand_address_size = reg_size;
        }
        skip_ws();
        int disp = 0;
        /* ``[reg + expr]`` and ``[reg - expr]`` — leave the sign for
           resolve_value so left-to-right semantics apply (``[bp-4+1]``
           evaluates to ``bp-3``, not ``bp-5``).  ``+`` is consumed so
           resolve_value sees the bare expression. */
        if (source_cursor[0] == '+') {
            source_cursor += 1;
            skip_ws();
            disp = resolve_value();
        } else if (source_cursor[0] == '-') {
            disp = resolve_value();
        }
        skip_ws();
        if (source_cursor[0] == ']') {
            source_cursor += 1;
        }
        parse_operand_value = disp;
        return (3 << 8) | register_id;          /* type=3 (reg+disp) */
    }
    /* Not ``[reg...]``: could be ``[disp]`` or ``[disp+reg]``.
       Scan forward to ``]`` (or NUL), then scan backwards over
       trailing whitespace to find the end of the bracket contents. */
    char *bracket_start = source_cursor;
    char *close = source_cursor;
    while (close[0] != ']' && close[0] != '\0') {
        close += 1;
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
        int packed_register2 = parse_register();
        source_cursor = saved;
        if (packed_register2 >= 0) {
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
                        source_cursor += 1;
                    }
                    int reg_size2 = (packed_register2 >> 8) & 0xFF;
                    if (reg_size2 == 16 || reg_size2 == 32) {
                        parse_operand_address_size = reg_size2;
                    }
                    parse_operand_value = disp;
                    return (3 << 8) | (packed_register2 & 0xFF);
                }
            }
        }
    }
    /* Plain ``[disp16]``. */
    int disp = resolve_value();
    while (source_cursor[0] != ']' && source_cursor[0] != '\0') {
        source_cursor += 1;
    }
    if (source_cursor[0] == ']') {
        source_cursor += 1;
    }
    parse_operand_value = disp;
    return 2 << 8;                      /* type=2 (direct mem) */
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
    char *saved = source_cursor;
    int size_bump = 0;
    char first = source_cursor[0];
    if (first == 'e' || first == 'E') {
        /* ``e``-prefix opens the 32-bit register file (eax..edi).
           Tentatively advance past the ``e`` and scan the 2-char
           table with size_bump = 16; on miss (not a real 32-bit
           reg, e.g. a user identifier starting with ``e``) we
           rewind the cursor so callers see no change. */
        source_cursor += 1;
        size_bump = 16;
    }
    char *entry = register_table;
    while (entry[0] != '\0') {
        char a = source_cursor[0];
        if (a >= 'A' && a <= 'Z') {
            a += 32;
        }
        if (a != entry[0]) {
            entry += 4;
            continue;
        }
        a = source_cursor[1];
        if (a >= 'A' && a <= 'Z') {
            a += 32;
        }
        if (a != entry[1]) {
            entry += 4;
            continue;
        }
        if (is_ident_char(source_cursor[2])) {
            entry += 4;
            continue;
        }
        int size = entry[3];
        /* ``e`` only applies to 16-bit table entries — ``eal``
           isn't a real mnemonic, so skip 8-bit matches in that
           case and keep scanning. */
        if (size_bump != 0 && size != 16) {
            entry += 4;
            continue;
        }
        source_cursor += 2;
        return ((size + size_bump) << 8) | entry[2];
    }
    source_cursor = saved;
    return -1;
}

/* Parse a control register name (``cr0``..``cr7``) at
   ``source_cursor``.  Returns the register number on match with
   ``source_cursor`` advanced past the token; returns -1 on miss
   with the cursor untouched.  Control registers live outside the
   general register file — they only appear in ``mov crN, r32`` /
   ``mov r32, crN`` in this assembler, so no size / encoding is
   packed into the return value. */
int parse_creg() {
    char a = source_cursor[0];
    char b = source_cursor[1];
    char c = source_cursor[2];
    if (a != 'c' && a != 'C') {
        return -1;
    }
    if (b != 'r' && b != 'R') {
        return -1;
    }
    if (c < '0' || c > '7') {
        return -1;
    }
    if (is_ident_char(source_cursor[3])) {
        return -1;
    }
    source_cursor += 3;
    return c - '0';
}

/* Shared RHS step for ``resolve_value``'s operator chain — advance past
   the operator byte, skip whitespace, and recurse.  Factored so the
   7 operator arms each collapse to ``value = value OP parse_rhs();``. */
int parse_rhs() {
    source_cursor += 1;
    skip_ws();
    return resolve_value();
}

/* Lookup a label's address without advancing SI.  Used by
   encode_rel8_jump to decide between short and near forms based on
   the known target distance.  ``carry_return`` signals miss via CF;
   on hit the resolved value lands in ``peek_label_value`` (AX-side
   of the retired dual AX + CF return).  Delegates identifier scan /
   null-term / symbol lookup / delim restore to ``lookup_ident_here``
   with ``advance = 0`` so source_cursor stays on the name.
   ``last_symbol_index`` is reset by ``symbol_lookup`` (so the explicit
   pre-clear retired with the refactor). */
__attribute__((carry_return))
int peek_label_target() {
    peek_label_value = lookup_ident_here(0);
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
        source_buffer_position += 1;
        if (c == '\n') {
            break;
        }
        if (c == '\r') {
            continue;
        }
        if (length < 255) {
            line_buffer[length] = c;
            length += 1;
        }
    }
    line_buffer[length] = '\0';
    return 0;
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
int reg_to_rm(int register_id) {
    register_id &= 0xFF;
    if (register_id == 3) {
        return 7;
    }
    if (register_id == 6) {
        return 4;
    }
    if (register_id == 7) {
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
    scan_ident_dot();
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
   can bind the AX return value (``int value = resolve_value();``); the
   inline-asm body ends with AX already set, and cc.py emits
   ``ret`` directly after (naked_asm elide), so the missing C-level
   ``return`` that clang's -Wreturn-type warns about is harmless —
   same pattern as ``load_src_sector``. */
int resolve_value() {
    skip_ws();
    int value;
    /* Leading unary ``-`` / ``+`` on the first term — matches NASM's
       displacement-expression semantics where ``[bp-4+1]`` evaluates
       left-to-right as ``((-4) + 1) = -3``.  Negation applies only
       to the first term; the operator chain that follows picks up
       from there. */
    int negate = 0;
    if (source_cursor[0] == '-') {
        source_cursor += 1;
        skip_ws();
        negate = 1;
    } else if (source_cursor[0] == '+') {
        source_cursor += 1;
        skip_ws();
    }
    char first = source_cursor[0];
    if (first == '(') {
        source_cursor += 1;
        value = resolve_value();
        skip_ws();
        if (source_cursor[0] == ')') {
            source_cursor += 1;
        }
    } else if (first == '\'') {
        source_cursor += 1;
        value = source_cursor[0];
        source_cursor += 1;
        if (source_cursor[0] == '\'') {
            source_cursor += 1;
        }
    } else if (first == '`') {
        source_cursor += 1;
        char c = source_cursor[0];
        if (c == '\\') {
            source_cursor += 1;
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
        source_cursor += 1;
        if (source_cursor[0] == '`') {
            source_cursor += 1;
        }
    } else if (first == '$') {
        source_cursor += 1;
        value = current_address;
    } else if (first >= '0' && first <= '9') {
        value = parse_number();
    } else {
        /* Symbol path: shared with peek_label_target via
           ``lookup_ident_here(1)`` — advance=1 moves source_cursor past
           the identifier so the operator-chain tail starts on the next
           non-ident byte. */
        value = lookup_ident_here(1);
    }
    if (negate) {
        value = 0 - value;
    }
    /* Operator chain (flat precedence, right-associative via recursion
       in ``parse_rhs``).  NASM's constant-expression lowering
       parenthesises every subtree, so flat precedence still produces
       the intended grouping. */
    skip_ws();
    char op = source_cursor[0];
    if (op == '+') {
        value += parse_rhs();
    } else if (op == '-') {
        value -= parse_rhs();
    } else if (op == '*') {
        value *= parse_rhs();
    } else if (op == '/') {
        value /= parse_rhs();
    } else if (op == '&') {
        value &= parse_rhs();
    } else if (op == '|') {
        value |= parse_rhs();
    } else if (op == '^') {
        value ^= parse_rhs();
    }
    return value;
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
        default_bits = 16;
        global_scope = 0xFFFF;
        jump_index = 0;
        macro_count = 0;
        macro_body_used = 0;
        do_pass();
        if (error_flag != 0) {
            die_error_pass1_io();
        }
        iteration_count += 1;
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
    default_bits = 16;
    global_scope = 0xFFFF;
    jump_index = 0;
    macro_count = 0;
    macro_body_used = 0;
    output_position = 0;
    output_total = 0;
    do_pass();
}

/* Scan source_cursor forward through an identifier-with-dot span — the
   character class every label / symbol parser uses (``[a-zA-Z0-9_.]``).
   Advances source_cursor past the last matching byte.  Three callers:
   peek_label_target, resolve_label, resolve_value's symbol path. */
void scan_ident_dot() {
    while (is_ident_char(source_cursor[0]) || source_cursor[0] == '.') {
        source_cursor += 1;
    }
}

/* ``shl`` / ``shr`` with r8/r16 destination and either a constant 1
   (short D0/D1 form) or imm8 shift count (C0/C1 imm8 form).  The two
   handlers share one body; ``modrm_base`` carries the /r field (0xE0
   for shl, 0xE8 for shr). */
__attribute__((regparm(1)))
void shift_handler(int modrm_base) {
    skip_ws();
    int packed_register = parse_register();
    skip_comma();
    int count = resolve_value();
    int register_id = packed_register & 0xFF;
    int size = (packed_register >> 8) & 0xFF;
    if (count == 1) {
        emit_sized(0xD0, size);
        emit_byte(modrm_base | register_id);
    } else {
        emit_sized(0xC0, size);
        emit_byte(modrm_base | register_id);
        emit_byte(count);
    }
}

/* Skip whitespace, a single ``,``, then whitespace — the inter-operand
   separator every multi-operand handler uses.  No-op if no comma is
   present (the first call to skip_ws still advances past leading
   whitespace). */
void skip_comma() {
    skip_ws();
    if (source_cursor[0] == ',') {
        source_cursor += 1;
        skip_ws();
    }
}

/* Advance source_cursor past any run of ' ' / '\t' at the current
   cursor position.  Called hundreds of times from the instruction
   handlers; ``source_cursor`` aliases SI through
   ``__attribute__((asm_register("si")))`` so the loop compiles to
   ``cmp byte [si], 32 ; je .skip ; cmp byte [si], 9 ; je .skip ;
   jmp .end ; .skip: inc si ; jmp .loop ; .end:`` — byte-identical
   to the retired inline-asm body except for cc.py's bp frame. */
void skip_ws() {
    while (source_cursor[0] == ' ' || source_cursor[0] == '\t') {
        source_cursor += 1;
    }
}

/* Append a label to the symbol table.  Callers pass SI = name,
   AX = value, BX = scope (0xFFFF = global, else the index of the
   enclosing global that owns this local label).  The name copy
   pads to SYMBOL_NAME_LENGTH with zeros; metadata lands at offset
   SYMBOL_NAME_LENGTH (value, type=0, scope byte).  Overflow jumps
   to die_symbol_overflow — silently corrupting past the table
   would clobber LINE_BUFFER which lives immediately after. */
__attribute__((regparm(1)))
void symbol_add(int value, int scope) {
    if (symbol_count >= SYMBOL_MAX) {
        die_symbol_overflow();
    }
    int entry = symbol_entry_address(symbol_count);
    /* Copy up to SYMBOL_NAME_LENGTH - 1 chars from source_cursor
       into the entry's name field, then zero-fill the remainder
       through offset SYMBOL_NAME_LENGTH - 1.  source_cursor is
       SI-pinned; advance it in-place and restore at the end so the
       inner read compiles to ``mov al, [si]`` (the variable-offset
       ``source_cursor[n]`` subscript would force a destructive
       ``add si, <reg>`` — same hazard symbol_lookup's inner loop
       dodges). */
    char *saved = source_cursor;
    int n = 0;
    while (n < SYMBOL_NAME_LENGTH - 1) {
        int src = source_cursor[0];
        if (src == 0) {
            break;
        }
        far_write8(entry + n, src);
        source_cursor += 1;
        n += 1;
    }
    while (n < SYMBOL_NAME_LENGTH) {
        far_write8(entry + n, 0);
        n += 1;
    }
    source_cursor = saved;
    far_write16(entry + SYMBOL_NAME_LENGTH, value);
    far_write8(entry + SYMBOL_NAME_LENGTH + 2, 0);
    far_write8(entry + SYMBOL_NAME_LENGTH + 3, scope & 0xFF);
    symbol_count += 1;
}

/* ``%assign`` entries: a value-only binding (scope=0xFFFF, type=1
   so pass-1 code that tells labels from %assigns can skip the
   relocation step).  Delegates the add / update logic to
   symbol_set, then rewrites the type byte.  Takes value via
   regparm(1); source_cursor supplies the name via its SI pin. */
__attribute__((regparm(1)))
void symbol_add_constant(int value) {
    symbol_set(value, 0xFFFF);
    int offset = symbol_entry_address(last_symbol_index) + SYMBOL_NAME_LENGTH + 2;
    far_write8(offset, 1);
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

/* Linear scan of the symbol table at ES:0..EFF8.  Each entry is
   SYMBOL_ENTRY (36) bytes: 32-char null-padded name, 2-byte value,
   1-byte type, 1-byte scope.  Caller passes scope via regparm(1) AX
   (low byte is compared against the entry's scope byte; 0xFFFF
   selects globals since the low byte stored is 0xFF).  Name pointer
   is ``source_cursor`` (SI-pinned).  On hit: returns AX = value,
   sets ``last_symbol_index`` to the entry index.  On miss: returns
   AX = 0, sets ``last_symbol_index`` = 0xFFFF.  No CF return — all
   remaining callers test ``last_symbol_index`` for hit/miss.
   Accesses the symbol segment via ``far_read8`` / ``far_read16``
   builtins so the body stays pure C (the [es:...] prefix is emitted
   by cc.py when the builtin expands). */
__attribute__((regparm(1)))
int symbol_lookup(int scope) {
    int count = symbol_count;
    int entry = 0;
    int index = 0;
    char *saved = source_cursor;
    last_symbol_index = 0xFFFF;
    while (index < count) {
        int entry_scope = far_read8(entry + SYMBOL_NAME_LENGTH + 3);
        if (entry_scope == (scope & 0xFF)) {
            /* Walk source_cursor (= SI) and an entry cursor in
               parallel.  Reading ``source_cursor[0]`` lowers to a
               clean ``mov al, [si]``; the variable-offset subscript
               ``source_cursor[n]`` would force cc.py to ``add si,
               <reg>`` and wreck the SI alias.  Restore source_cursor
               from ``saved`` before returning or continuing. */
            int name_offset = 0;
            int mismatch = 0;
            while (1) {
                int src = source_cursor[0];
                int ent = far_read8(entry + name_offset);
                if (src != ent) {
                    mismatch = 1;
                    break;
                }
                if (src == 0) {
                    break;
                }
                source_cursor += 1;
                name_offset += 1;
            }
            source_cursor = saved;
            if (mismatch == 0) {
                last_symbol_index = index;
                return far_read16(entry + SYMBOL_NAME_LENGTH);
            }
        }
        entry += SYMBOL_ENTRY;
        index += 1;
    }
    return 0;
}

/* Update or add.  SI = name via source_cursor pin, value via
   regparm(1) AX, scope on the stack.  Runs a symbol_lookup first;
   if the name exists in the table, overwrites the value word in
   place; otherwise appends via symbol_add and caches the new
   entry's index in last_symbol_index. */
__attribute__((regparm(1)))
void symbol_set(int value, int scope) {
    symbol_lookup(scope);
    if (last_symbol_index == 0xFFFF) {
        symbol_add(value, scope);
        last_symbol_index = symbol_count - 1;
    } else {
        far_write16(symbol_entry_address(last_symbol_index) + SYMBOL_NAME_LENGTH, value);
    }
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
    asm("push word 0xFFFF\n"
        "call symbol_set\n"
        "add sp, 2");
}

__attribute__((regparm(1)))
__attribute__((always_inline))
void symbol_set_local(int value) {
    asm("push word [_g_global_scope]\n"
        "call symbol_set\n"
        "add sp, 2");
}

/* Single-operand F6/F7-family handlers (``mul`` / ``neg`` / ``not``
   / ``div`` on a r8 or r16).  Emits F6 (byte) or F7 (word) followed
   by a register-mode ModR/M byte whose /r field is baked into
   ``modrm_base`` by the caller (0xE0 mul, 0xD8 neg, 0xD0 not, 0xF0
   div).  ``regparm(1)`` puts ``modrm_base`` in AX. */
__attribute__((regparm(1)))
void unary_f6f7(int modrm_base) {
    skip_ws();
    int packed_register = parse_register();
    int opcode = 0xF7;
    if ((packed_register >> 8) == 8) {
        opcode = 0xF6;
    }
    emit_byte(opcode);
    emit_byte(modrm_base | (packed_register & 0xFF));
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
    "        dw STR_IN,  handle_in\n"
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
    "        dw STR_LEA, handle_lea\n"
    "        dw STR_LGDT, handle_lgdt\n"
    "        dw STR_LIDT, handle_lidt\n"
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
    "        dw STR_OUT, handle_out\n"
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
    "STR_ALIGN   db 'align',0\n"
    "STR_AND     db 'and',0\n"
    "STR_ASSIGN  db 'assign',0\n"
    "STR_BITS    db 'bits',0\n"
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
    "STR_ENDMACRO db 'endmacro',0\n"
    "STR_IN      db 'in',0\n"
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
    "STR_DWORD   db 'dword',0\n"
    "STR_LEA     db 'lea',0\n"
    "STR_LGDT    db 'lgdt',0\n"
    "STR_LIDT    db 'lidt',0\n"
    "STR_LODSB   db 'lodsb',0\n"
    "STR_LODSW   db 'lodsw',0\n"
    "STR_LOOP    db 'loop',0\n"
    "STR_MACRO   db 'macro',0\n"
    "STR_MOV     db 'mov',0\n"
    "STR_MOVSB   db 'movsb',0\n"
    "STR_MOVSW   db 'movsw',0\n"
    "STR_MOVZX   db 'movzx',0\n"
    "STR_MUL     db 'mul',0\n"
    "STR_NEG     db 'neg',0\n"
    "STR_NOT     db 'not',0\n"
    "STR_OR      db 'or',0\n"
    "STR_ORG     db 'org',0\n"
    "STR_OUT     db 'out',0\n"
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
