        org 0600h

%include "constants.asm"

        ;; Memory layout. The assembler's scratch buffers live after the
        ;; binary at program_end. The symbol table and jump table live in
        ;; a dedicated ES segment (0x2000, linear 0x20000) so they don't
        ;; compete with segment-0 memory.
        %assign JUMP_MAX      4096      ; max jcc/jmp instructions per source
        %assign JUMP_TABLE    0F000h    ; jump_table offset within ES segment (4096 bytes)
        %assign LINE_MAX      255
        %assign SYMBOL_ENTRY     36        ; bytes per symbol entry (32 name + 2 val + 1 type + 1 scope)
        %assign SYMBOL_MAX       1706      ; 1706 * 36 = 61416 bytes (0x0000-0xEFF8)
        %assign SYMBOL_NAME_LENGTH  32        ; 31 chars + null
        %assign SYMBOL_SEGMENT   2000h     ; ES segment for symbol table (linear 0x20000)
        %define LINE_BUFFER      program_end
        %define OUTPUT_BUFFER       LINE_BUFFER + 256
        %define SOURCE_BUFFER       OUTPUT_BUFFER + 512
        %define INCLUDE_SAVE      SOURCE_BUFFER + 512   ; include stack (6 bytes: source_fd, source_buffer_position, source_buffer_valid)
        %define INCLUDE_SOURCE_SAVE  INCLUDE_SAVE + 64   ; saved source buffer (512 bytes per level)

;;; -----------------------------------------------------------------------
;;; Main entry point
;;; -----------------------------------------------------------------------
main:
        cld
        ;; Set ES to symbol table segment
        mov ax, SYMBOL_SEGMENT
        mov es, ax
        ;; Parse arguments: "source output"
        mov si, [EXEC_ARG]
        test si, si
        jz .usage
        mov [source_name], si
        ;; Find space separator, null-terminate source name
        .find_space:
        mov al, [si]
        test al, al
        jz .usage              ; no second arg
        cmp al, ' '
        je .found_space
        inc si
        jmp .find_space
        .found_space:
        mov byte [si], 0       ; null-terminate source name
        inc si
        call skip_ws
        cmp byte [si], 0
        je .usage              ; empty second arg
        mov [output_name], si

        ;; Compute source_prefix = directory portion of source_name (incl. trailing '/')
        ;; Walk source_name and remember position just past the last '/'
        mov si, [source_name]
        mov di, source_prefix     ; di tracks length of valid prefix
        .pfx_scan:
        mov al, [si]
        test al, al
        jz .pfx_scan_done
        inc si
        cmp al, '/'
        jne .pfx_scan
        ;; Found a '/'; copy source_name[0..si) to source_prefix
        mov bx, [source_name]
        mov di, source_prefix
        .pfx_copy:
        mov al, [bx]
        mov [di], al
        inc bx
        inc di
        cmp bx, si
        jb .pfx_copy
        jmp .pfx_scan
        .pfx_scan_done:
        mov byte [di], 0       ; null-terminate prefix

        ;; -- Pass 1: collect labels and converge jump sizes --
        ;; Iterative pass 1: jumps start near (pessimistic) and are
        ;; shrunk to short where they fit. Matches NASM's optimizer:
        ;; shrinking only makes targets closer, so convergence is
        ;; monotonic with no oscillation.
        mov byte [pass], 1
        mov word [symbol_count], 0
        mov word [org_value], 0
        ;; Fill jump_table with 1 (all jumps start near, in ES segment)
        push di
        push cx
        mov di, JUMP_TABLE
        mov cx, JUMP_MAX
        mov al, 1
        cld
        rep stosb
        pop cx
        pop di
        mov word [iteration_count], 0
        .pass1_loop:
        mov byte [changed_flag], 0
        mov word [current_address], 0
        mov word [global_scope], 0FFFFh
        mov word [jump_index], 0
        call do_pass
        test byte [error_flag], 0FFh
        jnz .error_pass1_io
        inc word [iteration_count]
        ;; Safety bound to catch oscillation.
        cmp word [iteration_count], 100
        jae .error_pass1_iter
        ;; Always run at least 2 iterations: iter 1 builds the symbol
        ;; table; iter 2 is the first one that can verify forward refs.
        cmp word [iteration_count], 2
        jb .pass1_loop
        ;; Loop while any jump changed size this iteration.
        test byte [changed_flag], 0FFh
        jnz .pass1_loop

        ;; -- Open output file for writing --
        mov si, [output_name]
        mov al, O_WRONLY + O_CREAT + O_TRUNC
        mov dl, FLAG_EXECUTE
        mov ah, SYS_IO_OPEN
        call syscall
        jc .error_create
        mov [output_fd], ax

        ;; -- Pass 2: emit bytes --
        mov byte [pass], 2
        mov ax, [org_value]
        mov [current_address], ax
        mov word [global_scope], 0FFFFh
        mov word [jump_index], 0
        mov word [output_position], 0
        mov word [output_total], 0
        call do_pass

        ;; Flush remaining output
        call flush_output

        ;; Close output — kernel writes back directory size from fd_pos
        mov bx, [output_fd]
        mov ah, SYS_IO_CLOSE
        call syscall
        jc .error_write_dir

        ;; Print success message
        mov si, MESSAGE_OK
        mov cx, MESSAGE_OK_LENGTH
        jmp call_die

        .error_create:
        mov si, MESSAGE_ERROR_CREATE
        mov cx, MESSAGE_ERROR_CREATE_LENGTH
        jmp call_die
        .error_find_out:
        mov si, MESSAGE_ERROR_FIND_OUT
        mov cx, MESSAGE_ERROR_FIND_OUT_LENGTH
        jmp call_die
        .error_pass1:
        mov si, MESSAGE_ERROR_PASS1
        mov cx, MESSAGE_ERROR_PASS1_LENGTH
        jmp call_die
        .error_pass1_io:
        mov si, MESSAGE_ERROR_PASS1_IO
        mov cx, MESSAGE_ERROR_PASS1_IO_LENGTH
        jmp call_die
        .error_pass1_iter:
        mov si, MESSAGE_ERROR_PASS1_ITER
        mov cx, MESSAGE_ERROR_PASS1_ITER_LENGTH
        jmp call_die
        .error_write_dir:
        mov si, MESSAGE_ERROR_WRITE_DIR
        mov cx, MESSAGE_ERROR_WRITE_DIR_LENGTH
        jmp call_die
        .usage:
        mov si, MESSAGE_USAGE
        mov cx, MESSAGE_USAGE_LENGTH
        jmp call_die

;;; -----------------------------------------------------------------------
;;; abort_unknown: print the offending line and exit
;;; Used by handle_unknown_word when it detects a line that has no
;;; recognized mnemonic or directive after stripping a bare label.
;;; -----------------------------------------------------------------------
abort_unknown:
        push si                ; save SI for second print
        mov si, MESSAGE_ERROR_UNKNOWN
        mov cx, MESSAGE_ERROR_UNKNOWN_LENGTH
        call call_write_stdout
        mov di, LINE_BUFFER
        call call_print_string
        mov al, 0Ah
        call call_print_character
        mov si, MESSAGE_ERROR_AT
        mov cx, MESSAGE_ERROR_AT_LENGTH
        call call_write_stdout
        pop di
        call call_print_string
        mov al, 0Ah
        call call_print_character
        jmp call_exit

;;; -----------------------------------------------------------------------
;;; do_pass: run one pass over the source file
;;; -----------------------------------------------------------------------
do_pass:
        push ax
        push bx
        push cx
        push dx
        push si
        push di

        ;; Reset origin for pass
        mov ax, [org_value]
        mov [current_address], ax

        ;; Open source file
        mov si, [source_name]
        mov al, O_RDONLY
        mov ah, SYS_IO_OPEN
        call syscall
        jc .pass_err
        mov [source_fd], ax
        mov word [source_buffer_position], 0
        mov word [source_buffer_valid], 0
        mov byte [include_depth], 0
        mov word [global_scope], 0FFFFh

        .line_loop:
        call read_line
        jc .eof
        call parse_line
        jmp .line_loop

        .eof:
        ;; Check if we're in an include -- if so, pop and continue
        cmp byte [include_depth], 0
        je .pass_done
        call include_pop
        jmp .line_loop

        .pass_done:
        ;; Close source fd
        mov bx, [source_fd]
        mov ah, SYS_IO_CLOSE
        call syscall
        pop di
        pop si
        pop dx
        pop cx
        pop bx
        pop ax
        ret

        .pass_err:
        mov byte [error_flag], 1
        jmp .pass_done

;;; -----------------------------------------------------------------------
;;; emit_byte_al: emit byte in AL
;;; -----------------------------------------------------------------------
emit_byte_al:
        cmp byte [pass], 2
        jne .count_only
        push bx
        mov bx, [output_position]
        mov [OUTPUT_BUFFER + bx], al
        inc bx
        mov [output_position], bx
        cmp bx, 512
        jb .no_flush
        call flush_output
        .no_flush:
        pop bx
        .count_only:
        inc word [current_address]
        inc word [output_total]
        ret

;;; -----------------------------------------------------------------------
;;; emit_word_ax: emit 16-bit word in AX (little-endian)
;;; -----------------------------------------------------------------------
emit_word_ax:
        push ax
        call emit_byte_al
        pop ax
        xchg al, ah
        jmp emit_byte_al

;;; -----------------------------------------------------------------------
;;; encode_rel8_jump: AL = opcode, SI points to operand (label name).
;;; Iterative pass 1 chooses short (rel8) or near (rel16) form per jump,
;;; growing instructions that don't fit; pass 2 trusts the choice.
;;; jump_table[idx] = 0 means short, 1 means near.
;;; -----------------------------------------------------------------------
encode_rel8_jump:
        push ax                        ; save opcode
        call skip_ws

        ;; Acquire this jump's index in the per-pass jump table.
        mov bx, [jump_index]
        inc word [jump_index]

        ;; If already marked near, try to shrink (pass 1 only).
        cmp byte [es:JUMP_TABLE + bx], 0
        jne .try_shrink

        ;; Currently short. In pass 2 we trust it and emit; in pass 1 we
        ;; check whether the target (if known) still fits in rel8.
        cmp byte [pass], 1
        jne .emit_short

        push bx                        ; save index across peek
        call peek_label_target         ; CF clear -> AX = target addr
        pop bx
        jc .emit_short                 ; unknown target -> stay short for now
        ;; Compute displacement vs (current_address + 2)
        mov dx, [current_address]
        add dx, 2
        sub ax, dx                     ; AX = signed displacement
        ;; In rel8 range iff AX + 128 in [0, 255]
        add ax, 128
        cmp ax, 256
        jb .emit_short
        ;; Out of range -- promote to near
        mov byte [es:JUMP_TABLE + bx], 1
        mov byte [changed_flag], 1
        jmp .long_form

        .try_shrink:
        ;; Currently near. On pass 2, just emit near.
        cmp byte [pass], 1
        jne .long_form

        push bx
        call peek_label_target
        pop bx
        jc .long_form                  ; unknown target -> stay near

        ;; Compute displacement using near instruction size.
        ;; Near jcc = 4 bytes (0F 8x rel16), near jmp = 3 bytes (E9 rel16).
        mov dx, [current_address]
        add dx, 4                      ; assume jcc
        push bp
        mov bp, sp
        cmp byte [bp+2], 0EBh          ; saved opcode: [bp]=old_bp, [bp+2]=opcode
        pop bp
        jne .shrink_check
        dec dx                         ; jmp: 3 bytes, not 4
        .shrink_check:
        sub ax, dx
        add ax, 128
        cmp ax, 256
        jae .long_form                 ; doesn't fit, stay near
        ;; Fits in rel8 -- shrink to short
        mov byte [es:JUMP_TABLE + bx], 0
        mov byte [changed_flag], 1
        jmp .emit_short

        .long_form:
        pop ax                         ; restore opcode
        cmp al, 0EBh
        je .long_jmp
        ;; Near jcc: 0F (7X + 10h) rel16
        add al, 10h
        push ax
        mov al, 0Fh
        call emit_byte_al
        pop ax
        call emit_byte_al
        jmp .long_emit_disp
        .long_jmp:
        mov al, 0E9h
        call emit_byte_al
        .long_emit_disp:
        call resolve_label             ; advances SI; AX = target (or placeholder)
        mov bx, [current_address]
        add bx, 2
        sub ax, bx
        jmp emit_word_ax

        .emit_short:
        pop ax                         ; restore opcode
        call emit_byte_al
        call resolve_label             ; advances SI; AX = target (or placeholder)
        mov bx, [current_address]
        inc bx
        sub ax, bx
        jmp emit_byte_al

;;; -----------------------------------------------------------------------
;;; flush_output: write OUTPUT_BUFFER to disk
;;; -----------------------------------------------------------------------
flush_output:
        push ax
        push cx
        push si
        push di
        ;; Don't flush if nothing to write
        cmp word [output_position], 0
        je .fl_done
        ;; Write output_position bytes from OUTPUT_BUFFER via fd
        mov bx, [output_fd]
        mov si, OUTPUT_BUFFER
        mov cx, [output_position]
        mov ah, SYS_IO_WRITE
        call syscall
        mov word [output_position], 0
        .fl_done:
        pop di
        pop si
        pop cx
        pop ax
        ret

;;; -----------------------------------------------------------------------
;;; handle_aam
;;; -----------------------------------------------------------------------
handle_aam:
        mov al, 0D4h
        call emit_byte_al
        mov al, 0Ah
        jmp emit_byte_al

;;; -----------------------------------------------------------------------
;;; handle_add: add r, imm
;;; -----------------------------------------------------------------------
handle_add:
        call skip_ws
        call parse_register    ; AL = reg, AH = size
        push ax                ; save dst reg+size
        call skip_comma
        ;; Use parse_operand so we get reg / mem_direct / imm uniformly
        call parse_operand
        mov [op2_type], ah
        mov [op2_register], al
        mov [op2_value], dx
        cmp byte [op2_type], 0
        je .add_rr
        cmp byte [op2_type], 2
        je .add_rm_direct
        ;; immediate
        mov cx, dx
        pop bx                 ; BL = dst reg, BH = dst size
        cmp bh, 8
        je .add_r8
        jmp .add_r16_imm
        .add_rr:
        ;; reg-reg: opcode 00 (8-bit) / 01 (16-bit), modrm reg=src, rm=dst
        pop bx                 ; BL = dst reg, BH = dst size
        cmp bh, 8
        je .add_rr8
        mov al, 01h
        jmp .add_rr_emit
        .add_rr8:
        mov al, 00h
        .add_rr_emit:
        call emit_byte_al
        mov al, [op2_register]      ; src reg goes in reg field
        call make_modrm_reg_reg ; AL=src(reg), BL=dst(rm)
        jmp emit_byte_al
        .add_rm_direct:
        ;; add r16, [disp16]: 03 modrm disp16 (or 02 for r8)
        pop bx                 ; BL = dst reg, BH = dst size
        cmp bh, 8
        je .add_rm8
        mov al, 03h
        jmp .add_rm_emit
        .add_rm8:
        mov al, 02h
        .add_rm_emit:
        call emit_byte_al
        mov al, bl
        shl al, 3
        or al, 06h             ; modrm: mod=00, reg=dst, rm=110 (disp16)
        call emit_byte_al
        mov ax, [op2_value]
        jmp emit_word_ax
        .add_r16_imm:
        ;; add r16, imm: prefer the 83 sign-extended-imm8 form (works for
        ;; any reg) when the immediate fits, else fall back to 05 imm16
        ;; for AX or 81 modrm imm16 for other registers.
        mov ax, cx
        add ax, 80h
        cmp ax, 0FFh
        ja .add_r16_ax_check
        mov al, 83h
        call emit_byte_al
        mov al, bl
        or al, 0C0h
        call emit_byte_al
        mov al, cl
        jmp emit_byte_al
        .add_r16_ax_check:
        test bl, bl
        jnz .add_r16_full
        ;; AX short form: 05 imm16
        mov al, 05h
        call emit_byte_al
        mov ax, cx
        jmp emit_word_ax
        .add_r16_full:
        mov al, 81h
        call emit_byte_al
        mov al, bl
        or al, 0C0h
        call emit_byte_al
        mov ax, cx
        jmp emit_word_ax
        .add_r8:
        ;; add r8, imm8. Short form for AL: 04 imm8
        test bl, bl
        jnz .add_r8_general
        mov al, 04h
        call emit_byte_al
        mov al, cl
        jmp emit_byte_al
        .add_r8_general:
        mov al, 80h
        call emit_byte_al
        mov al, bl
        or al, 0C0h
        call emit_byte_al
        mov al, cl
        jmp emit_byte_al

;;; -----------------------------------------------------------------------
;;; handle_and: and r, imm
;;; -----------------------------------------------------------------------
handle_and:
        call skip_ws
        call parse_register    ; AL = reg, AH = size
        push ax
        call skip_comma
        call resolve_value     ; AX = immediate
        mov cx, ax
        pop bx                 ; BL = reg, BH = size
        cmp bh, 8
        je .and_r8
        ;; and r16, imm16: 81 modrm(/4) imm16
        mov al, 81h
        call emit_byte_al
        mov al, bl
        or al, 0E0h            ; modrm = C0 | (4<<3) | rm = E0 | rm
        call emit_byte_al
        mov ax, cx
        jmp emit_word_ax
        .and_r8:
        ;; and r8, imm8. Short form for AL: 24 imm8
        test bl, bl
        jnz .and_r8_general
        mov al, 24h
        call emit_byte_al
        mov al, cl
        jmp emit_byte_al
        .and_r8_general:
        mov al, 80h
        call emit_byte_al
        mov al, bl
        or al, 0E0h
        call emit_byte_al
        mov al, cl
        jmp emit_byte_al

;;; -----------------------------------------------------------------------
;;; handle_call: call near label
;;; -----------------------------------------------------------------------
handle_call:
        call skip_ws
        cmp byte [si], '['
        je .call_indirect
        ;; Emit E8 rel16
        mov al, 0E8h
        call emit_byte_al
        call resolve_label     ; AX = target address
        ;; rel16 = target - (current_address + 2) (2 bytes for the rel16 itself)
        mov bx, [current_address]
        add bx, 2
        sub ax, bx
        jmp emit_word_ax
        .call_indirect:
        ;; call [reg+disp8]: FF /2 modrm disp8. Only the disp8 form is
        ;; needed (asm.asm's sole indirect call is `call [bx+2]`); any
        ;; other addressing form aborts.
        call parse_operand     ; AH=type, AL=reg, DX=disp
        cmp ah, 3
        jne abort_unknown
        test dx, dx
        jz abort_unknown
        mov bx, dx
        add bx, 80h
        cmp bx, 0FFh
        ja abort_unknown
        push dx
        mov bl, al
        mov al, 0FFh
        call emit_byte_al
        mov al, bl
        call reg_to_rm
        or al, 50h             ; mod=01 | reg field=010 (/2)
        call emit_byte_al
        pop ax
        call emit_byte_al      ; disp8
        ret

;;; -----------------------------------------------------------------------
;;; handle_clc
;;; -----------------------------------------------------------------------
handle_clc:
        mov al, 0F8h
        jmp emit_byte_al

;;; -----------------------------------------------------------------------
;;; handle_cld
;;; -----------------------------------------------------------------------
handle_cld:
        mov al, 0FCh
        jmp emit_byte_al

;;; -----------------------------------------------------------------------
;;; handle_cmp
;;; -----------------------------------------------------------------------
handle_cmp:
        call skip_ws
        call parse_operand     ; AH=type, AL=reg, DX=val
        mov [op1_type], ah
        mov [op1_register], al
        mov [op1_value], dx
        mov al, [op1_size]
        mov [cmp_op1_size], al
        call skip_comma
        ;; If first operand is a register, try parse_register for reg-reg form
        cmp byte [op1_type], 0
        jne .cmp_imm_only
        push si
        call parse_register
        jc .cmp_not_rr
        add sp, 2
        ;; cmp reg, reg: opcode 38 (8) / 39 (16), modrm reg=src, rm=dst
        mov bl, [op1_register]
        push ax                ; save src reg+size
        cmp byte [cmp_op1_size], 8
        je .cmp_rr8
        mov al, 39h
        jmp .cmp_rr_emit
        .cmp_rr8:
        mov al, 38h
        .cmp_rr_emit:
        call emit_byte_al
        pop ax                 ; AL = src reg
        call make_modrm_reg_reg ; AL=src(reg), BL=dst(rm)
        jmp emit_byte_al
        .cmp_not_rr:
        pop si
        ;; Op1 is a register but op2 isn't a register. Try [mem] form.
        cmp byte [si], '['
        jne .cmp_imm_only
        call parse_operand     ; AH=type, AL=reg, DX=disp; clobbers op1_size
        mov [op2_type], ah
        mov [op2_register], al
        mov [op2_value], dx
        cmp ah, 2
        je .cmp_rm_direct
        cmp ah, 3
        jne .cmp_imm_only
        ;; cmp r, [reg+disp]: 3A (8-bit) or 3B (16-bit), modrm reg=op1, rm=addr
        cmp byte [cmp_op1_size], 8
        je .cmp_rmbx8
        mov al, 3Bh
        jmp .cmp_rmbx_emit
        .cmp_rmbx8:
        mov al, 3Ah
        .cmp_rmbx_emit:
        call emit_byte_al
        mov al, [op2_register]
        call reg_to_rm         ; AL = rm bits
        mov bl, al
        mov al, [op1_register]
        shl al, 3
        or al, bl              ; modrm with mod=00 base
        mov dx, [op2_value]
        test dx, dx
        jz .cmp_rmbx_no_disp
        mov bx, dx
        add bx, 80h
        cmp bx, 0FFh
        ja .cmp_rmbx_disp16
        or al, 40h             ; mod=01
        call emit_byte_al
        mov al, dl
        call emit_byte_al      ; disp8
        ret
        .cmp_rmbx_disp16:
        or al, 80h             ; mod=10
        call emit_byte_al
        mov ax, [op2_value]
        call emit_word_ax      ; disp16
        ret
        .cmp_rmbx_no_disp:
        call emit_byte_al      ; mod=00
        ret
        .cmp_rm_direct:
        ;; cmp r16, [disp16]: 3B modrm disp16 (or 3A for r8)
        push dx                ; save disp16
        cmp byte [cmp_op1_size], 8
        je .cmp_rm8
        mov al, 3Bh
        jmp .cmp_rm_emit
        .cmp_rm8:
        mov al, 3Ah
        .cmp_rm_emit:
        call emit_byte_al
        mov al, [op1_register]
        shl al, 3
        or al, 06h             ; modrm: mod=00, reg=op1, rm=110 (disp16)
        call emit_byte_al
        pop ax                 ; AX = disp16
        jmp emit_word_ax
        .cmp_imm_only:
        call resolve_value
        mov cx, ax             ; CX = immediate
        ;; Check operand type
        cmp byte [op1_type], 2
        je .cmp_mem_direct
        cmp byte [op1_type], 3
        je .cmp_mem
        cmp byte [op1_type], 0
        jne .cmp_done
        ;; cmp reg, imm
        mov bl, [op1_register]
        cmp byte [op1_size], 8
        je .cmp_r8
        ;; cmp r16, imm: use 83h if fits in signed byte
        mov ax, cx
        add ax, 80h
        cmp ax, 0FFh
        ja .cmp_r16_full
        mov al, 83h
        call emit_byte_al
        mov al, bl
        or al, 0F8h
        call emit_byte_al
        mov al, cl
        jmp emit_byte_al
        .cmp_r16_full:
        ;; cmp AX, imm16 has a short form: 3D imm16
        test bl, bl
        jnz .cmp_r16_general
        mov al, 3Dh
        call emit_byte_al
        mov ax, cx
        jmp emit_word_ax
        .cmp_r16_general:
        mov al, 81h
        call emit_byte_al
        mov al, bl
        or al, 0F8h
        call emit_byte_al
        mov ax, cx
        jmp emit_word_ax
        .cmp_r8:
        ;; Short form for AL: 3C imm8
        test bl, bl
        jnz .cmp_r8_general
        mov al, 3Ch
        call emit_byte_al
        mov al, cl
        jmp emit_byte_al
        .cmp_r8_general:
        mov al, 80h
        call emit_byte_al
        mov al, bl
        or al, 0F8h
        call emit_byte_al
        mov al, cl
        jmp emit_byte_al
        .cmp_mem:
        ;; cmp byte [reg+disp], imm8 or cmp word [reg+disp], imm16
        cmp byte [op1_size], 8
        je .cmp_mem8
        mov al, 81h
        jmp .cmp_mem_modrm
        .cmp_mem8:
        mov al, 80h
        .cmp_mem_modrm:
        call emit_byte_al
        ;; Build modrm: /7 with memory addressing
        mov al, [op1_register]
        call reg_to_rm
        or al, 38h             ; /7 = 38h in reg field
        ;; Choose displacement size: 0 -> mod=00, signed byte -> mod=01,
        ;; otherwise mod=10 disp16.
        mov dx, [op1_value]
        test dx, dx
        jz .cmp_mem_no_disp
        mov bx, dx
        add bx, 80h
        cmp bx, 0FFh
        ja .cmp_mem_disp16
        or al, 40h             ; mod=01
        call emit_byte_al
        mov al, dl
        call emit_byte_al      ; disp8
        jmp .cmp_mem_imm
        .cmp_mem_disp16:
        or al, 80h             ; mod=10
        call emit_byte_al
        mov ax, [op1_value]
        call emit_word_ax      ; disp16
        jmp .cmp_mem_imm
        .cmp_mem_no_disp:
        call emit_byte_al      ; mod=00
        .cmp_mem_imm:
        cmp byte [op1_size], 8
        je .cmp_mem_imm8
        mov ax, cx
        jmp emit_word_ax
        .cmp_mem_imm8:
        mov al, cl
        jmp emit_byte_al
        .cmp_mem_direct:
        ;; cmp byte [disp16], imm8 or cmp word [disp16], imm
        cmp byte [op1_size], 8
        je .cmp_md8
        ;; Prefer the 83 sign-extended-imm8 form when imm fits in [-128, 127]
        mov ax, cx
        add ax, 80h
        cmp ax, 0FFh
        ja .cmp_md_full
        mov al, 83h
        call emit_byte_al
        mov al, 3Eh            ; modrm: mod=00, /7, rm=110 (disp16)
        call emit_byte_al
        mov ax, [op1_value]
        call emit_word_ax
        mov al, cl
        jmp emit_byte_al
        .cmp_md_full:
        mov al, 81h
        call emit_byte_al
        mov al, 3Eh            ; modrm: mod=00, /7, rm=110 (disp16)
        call emit_byte_al
        mov ax, [op1_value]
        call emit_word_ax
        mov ax, cx
        jmp emit_word_ax
        .cmp_md8:
        mov al, 80h
        call emit_byte_al
        mov al, 3Eh
        call emit_byte_al
        mov ax, [op1_value]
        call emit_word_ax
        mov al, cl
        call emit_byte_al
        .cmp_done:
        ret

;;; -----------------------------------------------------------------------
;;; handle_dec: dec r8, dec r16, dec byte [mem]
;;; -----------------------------------------------------------------------
handle_dec:
        call skip_ws
        call parse_operand     ; AH=type, AL=reg, DX=val
        cmp ah, 0
        jne .dec_mem
        ;; dec register
        cmp byte [op1_size], 8
        je .dec_r8
        ;; dec r16: 48+reg
        add al, 48h
        jmp emit_byte_al
        .dec_r8:
        ;; dec r8: FE /1 modrm(mod=11, /1, rm=reg)
        push ax
        mov al, 0FEh
        call emit_byte_al
        pop ax
        or al, 0C8h            ; modrm = C0 | 08 | reg
        jmp emit_byte_al
        .dec_mem:
        ;; dec byte [...] -> FE /1; dec word [...] -> FF /1
        mov al, 0FEh
        cmp byte [op1_size], 8
        je .dec_mem_op
        mov al, 0FFh
        .dec_mem_op:
        call emit_byte_al
        cmp ah, 2              ; OP_MEM_DIRECT
        je .dec_mem_direct
        ;; [reg] or [reg+disp]
        push dx
        mov al, [op1_register]
        call reg_to_rm
        or al, 08h             ; /1
        cmp dx, 0
        jne .dec_mem_reg_disp
        call emit_byte_al
        pop dx
        ret
        .dec_mem_reg_disp:
        or al, 40h
        call emit_byte_al
        pop dx
        mov al, dl
        jmp emit_byte_al
        .dec_mem_direct:
        mov al, 0Eh            ; modrm: mod=00, /1, rm=110
        call emit_byte_al
        mov ax, dx
        jmp emit_word_ax

;;; -----------------------------------------------------------------------
;;; handle_div: div r8 or div r16
;;; -----------------------------------------------------------------------
handle_div:
        call skip_ws
        call parse_register    ; AL = reg, AH = size
        push ax
        cmp ah, 8
        je .div8
        mov al, 0F7h
        jmp .div_emit
        .div8:
        mov al, 0F6h
        .div_emit:
        call emit_byte_al
        pop ax
        or al, 0F0h            ; modrm = C0 | (6<<3) | rm = F0 | rm
        jmp emit_byte_al

;;; -----------------------------------------------------------------------
;;; handle_inc
;;; -----------------------------------------------------------------------
handle_inc:
        call skip_ws
        call parse_operand     ; AH=type, AL=reg, DX=val
        cmp ah, 0
        jne .inc_mem
        ;; inc register
        cmp byte [op1_size], 8
        je .inc_r8
        ;; inc r16: 40+reg
        add al, 40h
        jmp emit_byte_al
        .inc_r8:
        ;; inc r8: FE /0 modrm(mod=11, /0, rm=reg)
        push ax
        mov al, 0FEh
        call emit_byte_al
        pop ax
        or al, 0C0h
        jmp emit_byte_al
        .inc_mem:
        ;; inc byte [...] -> FE /0; inc word [...] -> FF /0
        mov al, 0FEh
        cmp byte [op1_size], 8
        je .inc_mem_op
        mov al, 0FFh
        .inc_mem_op:
        call emit_byte_al
        cmp ah, 2              ; OP_MEM_DIRECT
        je .inc_mem_direct
        ;; [reg] or [reg+disp]
        push dx
        mov al, [op1_register]
        call reg_to_rm
        cmp dx, 0
        jne .inc_mem_reg_disp
        call emit_byte_al
        pop dx
        ret
        .inc_mem_reg_disp:
        or al, 40h
        call emit_byte_al
        pop dx
        mov al, dl
        jmp emit_byte_al
        .inc_mem_direct:
        mov al, 06h            ; modrm: mod=00, /0, rm=110
        call emit_byte_al
        mov ax, dx             ; DX = disp16 from parse_operand
        jmp emit_word_ax

;;; -----------------------------------------------------------------------
;;; handle_int
;;; -----------------------------------------------------------------------
handle_int:
        call skip_ws
        call resolve_value
        push ax
        mov al, 0CDh
        call emit_byte_al
        pop ax
        jmp emit_byte_al

;;; -----------------------------------------------------------------------
;;; handle_ja
;;; -----------------------------------------------------------------------
handle_ja:
        mov al, 77h
        jmp encode_rel8_jump

;;; -----------------------------------------------------------------------
;;; handle_jb / handle_jc
;;; -----------------------------------------------------------------------
handle_jb:
handle_jc:
        mov al, 72h
        jmp encode_rel8_jump

;;; -----------------------------------------------------------------------
;;; handle_jbe
;;; -----------------------------------------------------------------------
handle_jbe:
        mov al, 76h
        jmp encode_rel8_jump

;;; -----------------------------------------------------------------------
;;; handle_jg
;;; -----------------------------------------------------------------------
handle_jg:
        mov al, 7Fh
        jmp encode_rel8_jump

;;; -----------------------------------------------------------------------
;;; handle_jge
;;; -----------------------------------------------------------------------
handle_jge:
        mov al, 7Dh
        jmp encode_rel8_jump

;;; -----------------------------------------------------------------------
;;; handle_jl
;;; -----------------------------------------------------------------------
handle_jl:
        mov al, 7Ch
        jmp encode_rel8_jump

;;; -----------------------------------------------------------------------
;;; handle_jle
;;; -----------------------------------------------------------------------
handle_jle:
        mov al, 7Eh
        jmp encode_rel8_jump

;;; -----------------------------------------------------------------------
;;; handle_jnc
;;; -----------------------------------------------------------------------
handle_jnc:
        mov al, 73h
        jmp encode_rel8_jump

;;; -----------------------------------------------------------------------
;;; handle_jne
;;; -----------------------------------------------------------------------
handle_jne:
        mov al, 75h
        jmp encode_rel8_jump

;;; -----------------------------------------------------------------------
;;; handle_jns
;;; -----------------------------------------------------------------------
handle_jns:
        mov al, 79h
        jmp encode_rel8_jump

;;; -----------------------------------------------------------------------
;;; handle_jmp
;;; -----------------------------------------------------------------------
handle_jmp:
        ;; Skip optional 'short' keyword
        push si
        call skip_ws
        mov di, STR_SHORT
        call match_word
        jc .no_short
        ;; 'short' matched, SI advanced past it
        jmp .do_jmp
        .no_short:
        pop si
        push si
        call skip_ws
        .do_jmp:
        pop ax                 ; discard saved SI
        mov al, 0EBh
        jmp encode_rel8_jump

;;; -----------------------------------------------------------------------
;;; handle_jz
;;; -----------------------------------------------------------------------
handle_jz:
        mov al, 74h
        jmp encode_rel8_jump

;;; -----------------------------------------------------------------------
;;; handle_lodsb
;;; -----------------------------------------------------------------------
handle_lodsb:
        mov al, 0ACh
        jmp emit_byte_al

;;; -----------------------------------------------------------------------
;;; handle_loop
;;; -----------------------------------------------------------------------
handle_loop:
        mov al, 0E2h
        jmp encode_rel8_jump

;;; -----------------------------------------------------------------------
;;; handle_mov
;;; -----------------------------------------------------------------------
handle_mov:
        call skip_ws
        ;; Check for 'mov es, <r16>' — segment register move
        cmp byte [si], 'e'
        jne .mov_normal
        cmp byte [si+1], 's'
        jne .mov_normal
        ;; Peek ahead: next non-space after 'es' must be ','
        push si
        add si, 2
        call skip_ws
        cmp byte [si], ','
        jne .not_segment
        inc si
        call skip_ws
        ;; Parse source register
        call parse_operand
        pop bx                 ; discard saved SI
        ;; Emit 8E /r: 8E ModR/M where reg=0 (ES), r/m=source reg
        push ax
        mov al, 8Eh
        call emit_byte_al
        pop ax
        ;; AL = source register number, ModR/M = 11 000 rrr
        or al, 0C0h            ; mod=11, reg=000 (ES)
        jmp emit_byte_al
        .not_segment:
        pop si                 ; restore SI to before 'es'
        .mov_normal:
        ;; Parse destination
        call parse_operand     ; Returns: type in AH, value in DX, reg in AL
        mov [op1_type], ah
        mov [op1_register], al
        mov [op1_value], dx

        call skip_comma

        ;; Parse source
        call parse_operand
        mov [op2_type], ah
        mov [op2_register], al
        mov [op2_value], dx

        ;; Dispatch based on operand types
        ;; OP_REG=0, OP_IMM=1, OP_MEM_DIRECT=2, OP_MEM_BX_DISP=3
        mov al, [op1_type]
        cmp al, 2              ; dst is [disp16]
        je .mov_direct_dst
        cmp al, 3              ; dst is [reg] or [reg+disp]
        je .mov_mem_dst
        cmp al, 0              ; dst is register
        jne .mov_done

        mov al, [op2_type]
        cmp al, 0              ; src is register: mov r, r
        je .mov_rr
        cmp al, 1              ; src is immediate: mov r, imm
        je .mov_ri
        cmp al, 2              ; src is [disp16]: mov r, [disp]
        je .mov_rm_direct
        cmp al, 3              ; src is [bx+disp]: mov r, [bx+disp]
        je .mov_rm_bx_disp
        jmp abort_unknown

        .mov_rr:
        ;; mov r, r -- use opcode 88 (8-bit) or 89 (16-bit)
        ;; NASM encodes as: opcode modrm where reg=src, rm=dst
        mov al, [op1_register]      ; dst reg
        mov bl, al
        mov al, [op2_register]      ; src reg
        ;; Check size: use op1's size (both should match)
        cmp byte [op1_size], 8
        je .mov_rr8
        ;; 16-bit: opcode 89
        push ax
        push bx
        mov al, 89h
        call emit_byte_al
        pop bx
        pop ax
        call make_modrm_reg_reg ; AL=src(reg field), BL=dst(rm field)
        call emit_byte_al
        jmp .mov_done
        .mov_rr8:
        ;; 8-bit: opcode 88
        push ax
        push bx
        mov al, 88h
        call emit_byte_al
        pop bx
        pop ax
        call make_modrm_reg_reg
        call emit_byte_al
        jmp .mov_done

        .mov_ri:
        ;; mov r, imm -- short form: B0+r (8-bit) or B8+r (16-bit)
        mov al, [op1_register]
        cmp byte [op1_size], 8
        je .mov_ri8
        ;; 16-bit: B8+reg, imm16
        add al, 0B8h
        call emit_byte_al
        mov ax, [op2_value]
        call emit_word_ax
        jmp .mov_done
        .mov_ri8:
        ;; 8-bit: B0+reg, imm8
        add al, 0B0h
        call emit_byte_al
        mov al, [op2_value]
        call emit_byte_al
        jmp .mov_done

        .mov_rm_direct:
        ;; mov r, [disp16]: short form A0/A1 for AL/AX, else 8B/8A
        cmp byte [op1_size], 8
        je .mov_rm_d8
        ;; 16-bit: short form A1 for AX
        cmp byte [op1_register], 0
        jne .mov_rm_d16_general
        mov al, 0A1h
        call emit_byte_al
        mov ax, [op2_value]
        call emit_word_ax
        jmp .mov_done
        .mov_rm_d16_general:
        mov al, 8Bh
        jmp .mov_rm_d_emit
        .mov_rm_d8:
        ;; 8-bit: short form A0 for AL
        cmp byte [op1_register], 0
        jne .mov_rm_d8_general
        mov al, 0A0h
        call emit_byte_al
        mov ax, [op2_value]
        call emit_word_ax
        jmp .mov_done
        .mov_rm_d8_general:
        mov al, 8Ah
        .mov_rm_d_emit:
        call emit_byte_al
        ;; modrm = (0 << 6) | (reg << 3) | 6 = (reg << 3) | 6
        mov al, [op1_register]
        shl al, 3
        or al, 06h
        call emit_byte_al
        mov ax, [op2_value]
        call emit_word_ax
        jmp .mov_done

        .mov_rm_bx_disp:
        ;; mov r, [bx+disp8]: 8B (16-bit) or 8A (8-bit)
        ;; modrm: mod=01, reg=dst, rm=111 (bx)
        cmp byte [op1_size], 8
        je .mov_rm_bx8
        mov al, 8Bh
        jmp .mov_rm_bx_emit
        .mov_rm_bx8:
        mov al, 8Ah
        .mov_rm_bx_emit:
        call emit_byte_al
        ;; Get rm field from addressing register
        mov al, [op2_register]
        call reg_to_rm
        mov bl, al
        mov al, [op1_register]
        shl al, 3
        or al, bl
        ;; mod=00 if no displacement, mod=01 if disp8
        cmp word [op2_value], 0
        jne .mov_rm_with_disp
        call emit_byte_al
        jmp .mov_done
        .mov_rm_with_disp:
        ;; Choose disp8 (mod=01) when value fits in signed byte, else disp16
        ;; (mod=10).
        mov dx, [op2_value]
        mov bx, dx
        add bx, 80h
        cmp bx, 0FFh
        ja .mov_rm_disp16
        or al, 40h             ; mod=01
        call emit_byte_al
        mov al, dl
        call emit_byte_al      ; disp8
        jmp .mov_done
        .mov_rm_disp16:
        or al, 80h             ; mod=10
        call emit_byte_al
        mov ax, [op2_value]
        call emit_word_ax      ; disp16
        jmp .mov_done

        .mov_mem_dst:
        ;; mov [reg(+disp)?], imm or mov [reg(+disp)?], reg
        cmp byte [op2_type], 0
        je .mov_mem_dst_reg
        cmp byte [op2_type], 1
        jne .mov_done
        ;; mov [reg(+disp)?], imm: C6 /0 modrm [disp] imm8 (byte) or
        ;; C7 /0 modrm [disp] imm16 (word).  Displacement size mirrors the
        ;; reg-source path: 0 -> mod=00, signed byte -> mod=01, else mod=10.
        cmp byte [op1_size], 8
        je .mov_mdi8
        mov al, 0C7h
        call emit_byte_al
        mov al, [op1_register]
        call reg_to_rm         ; AL = rm field, mod=00
        mov dx, [op1_value]
        test dx, dx
        jz .mov_mdi16_emit_modrm
        mov bx, dx
        add bx, 80h
        cmp bx, 0FFh
        ja .mov_mdi16_disp16
        or al, 40h             ; mod=01
        call emit_byte_al
        mov al, dl
        call emit_byte_al      ; disp8
        jmp .mov_mdi16_imm
        .mov_mdi16_disp16:
        or al, 80h             ; mod=10
        call emit_byte_al
        mov ax, [op1_value]
        call emit_word_ax      ; disp16
        jmp .mov_mdi16_imm
        .mov_mdi16_emit_modrm:
        call emit_byte_al
        .mov_mdi16_imm:
        mov ax, [op2_value]
        call emit_word_ax
        jmp .mov_done
        .mov_mdi8:
        mov al, 0C6h
        call emit_byte_al
        mov al, [op1_register]
        call reg_to_rm         ; AL = rm field, mod=00
        mov dx, [op1_value]
        test dx, dx
        jz .mov_mdi8_emit_modrm
        mov bx, dx
        add bx, 80h
        cmp bx, 0FFh
        ja .mov_mdi8_disp16
        or al, 40h             ; mod=01
        call emit_byte_al
        mov al, dl
        call emit_byte_al      ; disp8
        jmp .mov_mdi8_imm
        .mov_mdi8_disp16:
        or al, 80h             ; mod=10
        call emit_byte_al
        mov ax, [op1_value]
        call emit_word_ax      ; disp16
        jmp .mov_mdi8_imm
        .mov_mdi8_emit_modrm:
        call emit_byte_al
        .mov_mdi8_imm:
        mov al, [op2_value]
        call emit_byte_al
        jmp .mov_done
        .mov_mem_dst_reg:
        ;; mov [reg(+disp)?], reg: 88 (8-bit) / 89 (16-bit), modrm reg=src, rm=dst
        ;; (op1_size holds the second-parsed operand's size by convention)
        cmp byte [op1_size], 8
        je .mov_mem_dst_reg8
        mov al, 89h
        jmp .mov_mem_dst_reg_emit
        .mov_mem_dst_reg8:
        mov al, 88h
        .mov_mem_dst_reg_emit:
        call emit_byte_al
        mov al, [op1_register]
        call reg_to_rm         ; AL = rm field
        mov bl, al
        mov al, [op2_register]
        shl al, 3
        or al, bl              ; modrm with mod=00 base
        ;; Choose displacement size: 0 -> mod=00, signed byte -> mod=01,
        ;; otherwise mod=10 disp16.
        mov dx, [op1_value]
        test dx, dx
        jz .mov_mds_emit_modrm
        mov bx, dx
        add bx, 80h
        cmp bx, 0FFh
        ja .mov_mds_disp16
        or al, 40h             ; mod=01
        call emit_byte_al
        mov al, dl
        call emit_byte_al      ; disp8
        jmp .mov_done
        .mov_mds_disp16:
        or al, 80h             ; mod=10
        call emit_byte_al
        mov ax, [op1_value]
        call emit_word_ax      ; disp16
        jmp .mov_done
        .mov_mds_emit_modrm:
        call emit_byte_al
        jmp .mov_done

        .mov_direct_dst:
        ;; mov [disp16], imm or mov [disp16], reg
        cmp byte [op2_type], 0
        je .mov_dd_reg
        cmp byte [op2_type], 1
        jne .mov_done
        ;; mov [disp16], imm: C6 06 disp16 imm8 (byte) or C7 06 disp16 imm16
        cmp byte [op1_size], 8
        je .mov_dd8
        mov al, 0C7h
        call emit_byte_al
        mov al, 06h
        call emit_byte_al
        mov ax, [op1_value]
        call emit_word_ax
        mov ax, [op2_value]
        call emit_word_ax
        jmp .mov_done
        .mov_dd8:
        mov al, 0C6h
        call emit_byte_al
        mov al, 06h
        call emit_byte_al
        mov ax, [op1_value]
        call emit_word_ax
        mov al, [op2_value]
        call emit_byte_al
        jmp .mov_done
        .mov_dd_reg:
        ;; mov [disp16], reg: short form A2/A3 for AL/AX, else 88/89 modrm
        cmp byte [op1_size], 8
        je .mov_dd_reg8
        ;; 16-bit
        cmp byte [op2_register], 0
        jne .mov_dd_reg16_general
        mov al, 0A3h
        call emit_byte_al
        mov ax, [op1_value]
        call emit_word_ax
        jmp .mov_done
        .mov_dd_reg16_general:
        mov al, 89h
        call emit_byte_al
        mov al, [op2_register]
        shl al, 3
        or al, 06h             ; modrm: mod=00, reg=src, rm=110 (disp16)
        call emit_byte_al
        mov ax, [op1_value]
        call emit_word_ax
        jmp .mov_done
        .mov_dd_reg8:
        cmp byte [op2_register], 0
        jne .mov_dd_reg8_general
        mov al, 0A2h
        call emit_byte_al
        mov ax, [op1_value]
        call emit_word_ax
        jmp .mov_done
        .mov_dd_reg8_general:
        mov al, 88h
        call emit_byte_al
        mov al, [op2_register]
        shl al, 3
        or al, 06h
        call emit_byte_al
        mov ax, [op1_value]
        call emit_word_ax
        jmp .mov_done

        .mov_done:
        ret

;;; -----------------------------------------------------------------------
;;; handle_movsb: movsb (no operands)
;;; -----------------------------------------------------------------------
handle_movsb:
        mov al, 0A4h
        jmp emit_byte_al

;;; -----------------------------------------------------------------------
;;; handle_movsw: movsw (no operands)
;;; -----------------------------------------------------------------------
handle_movsw:
        mov al, 0A5h
        jmp emit_byte_al

;;; -----------------------------------------------------------------------
;;; handle_movzx: movzx r16, byte [reg+disp]
;;; -----------------------------------------------------------------------
handle_movzx:
        call skip_ws
        call parse_register    ; AL = dst reg, AH = size (16)
        mov [op1_register], al
        call skip_comma
        call parse_operand     ; AH = type, AL = reg, DX = disp
        mov [op2_type], ah
        mov [op2_register], al
        mov [op2_value], dx
        ;; 0F B6 prefix
        mov al, 0Fh
        call emit_byte_al
        mov al, 0B6h
        call emit_byte_al
        cmp byte [op2_type], 0
        je .movzx_rr
        ;; mem (reg + disp form): modrm reg=dst, rm=reg_to_rm(mem reg)
        mov al, [op2_register]
        call reg_to_rm
        mov bl, al
        mov al, [op1_register]
        shl al, 3
        or al, bl
        cmp word [op2_value], 0
        jne .movzx_disp
        jmp emit_byte_al
        .movzx_disp:
        or al, 40h             ; mod = 01 (disp8)
        call emit_byte_al
        mov al, [op2_value]
        jmp emit_byte_al
        .movzx_rr:
        ;; reg-reg: modrm = 11 dst src
        mov al, [op1_register]
        shl al, 3
        or al, [op2_register]
        or al, 0C0h
        jmp emit_byte_al

;;; -----------------------------------------------------------------------
;;; handle_mul: mul r16
;;; -----------------------------------------------------------------------
handle_mul:
        call skip_ws
        call parse_register    ; AL = reg, AH = size
        push ax
        cmp ah, 8
        je .mul8
        mov al, 0F7h
        jmp .mul_emit
        .mul8:
        mov al, 0F6h
        .mul_emit:
        call emit_byte_al
        pop ax
        or al, 0E0h            ; modrm = C0 | (4<<3) | rm = E0 | rm
        jmp emit_byte_al

;;; -----------------------------------------------------------------------
;;; handle_neg: neg r16 / neg r8 (F7 /3 modrm or F6 /3 modrm)
;;; -----------------------------------------------------------------------
handle_neg:
        call skip_ws
        call parse_register    ; AL = reg, AH = size
        push ax
        cmp ah, 8
        je .neg8
        mov al, 0F7h
        jmp .neg_emit
        .neg8:
        mov al, 0F6h
        .neg_emit:
        call emit_byte_al
        pop ax
        or al, 0D8h            ; modrm = C0 | (3<<3) | rm = D8 | rm
        jmp emit_byte_al

;;; -----------------------------------------------------------------------
;;; handle_or
;;; -----------------------------------------------------------------------
handle_or:
        call skip_ws
        call parse_register    ; AL = reg, AH = size
        push ax                ; save dst reg+size
        call skip_comma
        call parse_operand
        mov [op2_type], ah
        mov [op2_register], al
        mov [op2_value], dx
        cmp byte [op2_type], 0
        je .or_rr
        cmp byte [op2_type], 2
        je .or_rm_direct
        ;; immediate
        mov cx, dx
        pop bx                 ; BL = dst reg, BH = dst size
        cmp bh, 8
        jne .or_unsupported    ; r16, imm not used yet
        ;; or r8, imm8. Short form for AL: 0C imm8
        test bl, bl
        jnz .or_r8_general
        mov al, 0Ch
        call emit_byte_al
        mov al, cl
        jmp emit_byte_al
        .or_r8_general:
        mov al, 80h
        call emit_byte_al
        mov al, bl
        or al, 0C8h            ; modrm = C0 | (1<<3) | rm = C8 | rm
        call emit_byte_al
        mov al, cl
        jmp emit_byte_al
        .or_rr:
        ;; reg-reg: opcode 08 (8-bit) / 09 (16-bit), modrm reg=src, rm=dst
        pop bx                 ; BL = dst reg, BH = dst size
        cmp bh, 8
        je .or_rr8
        mov al, 09h
        jmp .or_rr_emit
        .or_rr8:
        mov al, 08h
        .or_rr_emit:
        call emit_byte_al
        mov al, [op2_register]      ; src reg goes in reg field
        call make_modrm_reg_reg
        jmp emit_byte_al
        .or_rm_direct:
        ;; or r8, [disp16]: 0A modrm disp16 (or 0B for r16)
        pop bx                 ; BL = dst reg, BH = dst size
        cmp bh, 8
        je .or_rm8
        mov al, 0Bh
        jmp .or_rm_emit
        .or_rm8:
        mov al, 0Ah
        .or_rm_emit:
        call emit_byte_al
        mov al, bl
        shl al, 3
        or al, 06h             ; modrm: mod=00, reg=dst, rm=110 (disp16)
        call emit_byte_al
        mov ax, [op2_value]
        jmp emit_word_ax
        .or_unsupported:
        jmp abort_unknown

;;; -----------------------------------------------------------------------
;;; handle_pop: pop r16 / pop ds / pop es
;;; -----------------------------------------------------------------------
handle_pop:
        call skip_ws
        cmp byte [si], 'd'
        jne .pop_not_ds
        cmp byte [si+1], 's'
        jne .pop_reg
        add si, 2
        mov al, 1Fh            ; pop ds
        jmp emit_byte_al
        .pop_not_ds:
        cmp byte [si], 'e'
        jne .pop_reg
        cmp byte [si+1], 's'
        jne .pop_reg
        add si, 2
        mov al, 07h            ; pop es
        jmp emit_byte_al
        .pop_reg:
        call parse_register    ; AL = reg
        add al, 58h            ; 58+reg
        jmp emit_byte_al

;;; -----------------------------------------------------------------------
;;; handle_push: push r16 / push ds / push es
;;; -----------------------------------------------------------------------
handle_push:
        call skip_ws
        cmp byte [si], 'd'
        jne .push_not_ds
        cmp byte [si+1], 's'
        jne .push_operand
        add si, 2
        mov al, 1Eh            ; push ds
        jmp emit_byte_al
        .push_not_ds:
        cmp byte [si], 'e'
        jne .push_operand
        cmp byte [si+1], 's'
        jne .push_operand
        add si, 2
        mov al, 06h            ; push es
        jmp emit_byte_al
        .push_operand:
        ;; Try register first, fall through to immediate.
        push si
        call parse_register
        jc .push_imm16
        add sp, 2              ; discard saved SI
        add al, 50h            ; 50+reg: push r16
        jmp emit_byte_al
        .push_imm16:
        pop si
        call resolve_value     ; AX = imm16
        push ax
        mov al, 68h            ; push imm16 opcode
        call emit_byte_al
        pop ax
        jmp emit_word_ax

;;; -----------------------------------------------------------------------
;;; handle_rep: rep prefix — emits 0xF3 then parses the next mnemonic
;;; -----------------------------------------------------------------------
handle_rep:
        mov al, 0F3h
        call emit_byte_al
        call skip_ws
        call parse_mnemonic
        ret

;;; -----------------------------------------------------------------------
;;; handle_repne: repne prefix — emits 0xF2 then parses the next mnemonic
;;; -----------------------------------------------------------------------
handle_repne:
        mov al, 0F2h
        call emit_byte_al
        call skip_ws
        call parse_mnemonic
        ret

;;; -----------------------------------------------------------------------
;;; handle_ret
;;; -----------------------------------------------------------------------
handle_ret:
        mov al, 0C3h
        jmp emit_byte_al

;;; -----------------------------------------------------------------------
;;; handle_sbb: only `sbb word [disp16], imm8` is supported
;;; -----------------------------------------------------------------------
handle_sbb:
        call skip_ws
        push si
        mov di, STR_WORD
        call match_word
        jc .sbb_bad
        add sp, 2               ; commit consumed 'word'
        call skip_ws
        cmp byte [si], '['
        jne .sbb_bad2
        inc si
        call resolve_value      ; AX = displacement
        mov dx, ax
        cmp byte [si], ']'
        jne .sbb_bad2
        inc si
        call skip_comma
        call resolve_value      ; AX = imm
        mov cl, al
        mov al, 83h             ; 83 /3 ib (sign-extended imm8)
        call emit_byte_al
        mov al, 1Eh             ; modrm: mod=00, /3, rm=110 (disp16)
        call emit_byte_al
        mov ax, dx
        call emit_word_ax       ; disp16
        mov al, cl
        call emit_byte_al       ; imm8
        ret
        .sbb_bad:
        pop si
        .sbb_bad2:
        jmp abort_unknown

;;; -----------------------------------------------------------------------
;;; handle_scasb
;;; -----------------------------------------------------------------------
handle_scasb:
        mov al, 0AEh
        jmp emit_byte_al

;;; -----------------------------------------------------------------------
;;; handle_shl: shl r8, imm8 / shl r16, imm8
;;; -----------------------------------------------------------------------
handle_shl:
        call skip_ws
        call parse_register    ; AL = reg, AH = size
        push ax
        call skip_comma
        call resolve_value     ; AX = shift count
        mov cl, al
        pop bx                 ; BL = reg, BH = size
        ;; shl r8, imm8: C0 /4 imm8. shl r16, imm8: C1 /4 imm8
        cmp bh, 8
        je .shl8
        mov al, 0C1h
        jmp .shl_emit
        .shl8:
        mov al, 0C0h
        .shl_emit:
        call emit_byte_al
        ;; modrm = C0 | (4<<3) | rm = E0 | rm
        mov al, bl
        or al, 0E0h
        call emit_byte_al
        mov al, cl
        jmp emit_byte_al

;;; -----------------------------------------------------------------------
;;; handle_shr: shr r8, imm8
;;; -----------------------------------------------------------------------
handle_shr:
        call skip_ws
        call parse_register    ; AL = reg, AH = size
        push ax
        call skip_comma
        call resolve_value     ; AX = shift count
        mov cl, al
        pop bx                 ; BL = reg, BH = size
        ;; shr r8, imm8: C0 /5 imm8. shr r16, imm8: C1 /5 imm8
        cmp bh, 8
        je .shr8
        mov al, 0C1h
        jmp .shr_emit
        .shr8:
        mov al, 0C0h
        .shr_emit:
        call emit_byte_al
        ;; modrm = C0 | (5<<3) | rm = E8 | rm
        mov al, bl
        or al, 0E8h
        call emit_byte_al
        mov al, cl
        jmp emit_byte_al

;;; -----------------------------------------------------------------------
;;; handle_stc
;;; -----------------------------------------------------------------------
handle_stc:
        mov al, 0F9h
        jmp emit_byte_al

;;; -----------------------------------------------------------------------
;;; handle_stosb
;;; -----------------------------------------------------------------------
handle_stosb:
        mov al, 0AAh
        jmp emit_byte_al

;;; -----------------------------------------------------------------------
;;; handle_stosw
;;; -----------------------------------------------------------------------
handle_stosw:
        mov al, 0ABh
        jmp emit_byte_al

;;; -----------------------------------------------------------------------
;;; handle_sub
;;; -----------------------------------------------------------------------
handle_sub:
        call skip_ws
        ;; Detect `sub word [disp16], imm` form (only 16-bit mem-imm form
        ;; needed by self-host).
        push si
        mov di, STR_WORD
        call match_word
        jc .sub_no_mem
        add sp, 2               ; commit consumed 'word'
        call skip_ws
        cmp byte [si], '['
        jne abort_unknown
        inc si
        call resolve_value      ; AX = displacement
        mov dx, ax
        cmp byte [si], ']'
        jne abort_unknown
        inc si
        call skip_comma
        call resolve_value      ; AX = imm16
        push ax
        mov al, 81h             ; 81 /5 iw (sub r/m16, imm16)
        call emit_byte_al
        mov al, 2Eh             ; modrm: mod=00, /5, rm=110 (disp16)
        call emit_byte_al
        mov ax, dx
        call emit_word_ax       ; disp16
        pop ax
        call emit_word_ax       ; imm16
        ret
        .sub_no_mem:
        pop si
        call parse_register    ; AL = reg, AH = size
        jc abort_unknown
        push ax                ; save dst reg+size
        call skip_comma
        ;; Use parse_operand so we get reg / mem_direct / imm uniformly.
        call parse_operand
        mov [op2_type], ah
        mov [op2_register], al
        mov [op2_value], dx
        cmp byte [op2_type], 0
        je .sub_rr
        cmp byte [op2_type], 2
        je .sub_rm_direct
        ;; immediate
        mov cx, dx
        pop bx                 ; BL = dst reg, BH = dst size
        cmp bh, 8
        je .sub_r8
        ;; sub r16, imm: use 83h short form if imm fits in signed byte
        mov ax, cx
        add ax, 80h
        cmp ax, 0FFh
        ja .sub_r16_full
        mov al, 83h
        call emit_byte_al
        mov al, bl
        or al, 0E8h
        call emit_byte_al
        mov al, cl
        jmp emit_byte_al
        .sub_rr:
        ;; reg-reg: opcode 28 (8-bit) / 29 (16-bit), modrm reg=src, rm=dst
        pop bx                 ; BL = dst reg, BH = dst size
        cmp bh, 8
        je .sub_rr8
        mov al, 29h
        jmp .sub_rr_emit
        .sub_rr8:
        mov al, 28h
        .sub_rr_emit:
        call emit_byte_al
        mov al, [op2_register]      ; src reg goes in reg field
        call make_modrm_reg_reg
        jmp emit_byte_al
        .sub_rm_direct:
        ;; sub r16, [disp16]: 2B modrm disp16 (or 2A for r8)
        pop bx                 ; BL = dst reg, BH = dst size
        cmp bh, 8
        je .sub_rm8
        mov al, 2Bh
        jmp .sub_rm_emit
        .sub_rm8:
        mov al, 2Ah
        .sub_rm_emit:
        call emit_byte_al
        mov al, bl
        shl al, 3
        or al, 06h             ; modrm: mod=00, reg=dst, rm=110 (disp16)
        call emit_byte_al
        mov ax, [op2_value]
        jmp emit_word_ax
        .sub_r16_full:
        mov al, 81h
        call emit_byte_al
        mov al, bl
        or al, 0E8h
        call emit_byte_al
        mov ax, cx
        jmp emit_word_ax
        .sub_r8:
        ;; sub r8, imm8. Short form for AL: 2C imm8
        test bl, bl
        jnz .sub_r8_general
        mov al, 2Ch
        call emit_byte_al
        mov al, cl
        jmp emit_byte_al
        .sub_r8_general:
        mov al, 80h
        call emit_byte_al
        mov al, bl
        or al, 0E8h
        call emit_byte_al
        mov al, cl
        jmp emit_byte_al

;;; -----------------------------------------------------------------------
;;; handle_test: test r/mem, r/imm
;;; -----------------------------------------------------------------------
handle_test:
        call skip_ws
        call parse_operand     ; AH=type, AL=reg, DX=val
        mov [op1_type], ah
        mov [op1_register], al
        mov [op1_value], dx
        call skip_comma
        cmp byte [op1_type], 0
        jne .test_mem
        ;; First operand is a register — check if second is register or immediate
        call skip_ws
        call parse_register    ; CF set if not a register (= immediate)
        jc .test_r_imm
        ;; test r, r
        mov bl, [op1_register]      ; BL = dst reg
        push ax
        cmp byte [op1_size], 8
        je .test_rr8
        mov al, 85h            ; test r16, r16
        jmp .test_rr_emit
        .test_rr8:
        mov al, 84h            ; test r8, r8
        .test_rr_emit:
        call emit_byte_al
        pop ax
        call make_modrm_reg_reg
        jmp emit_byte_al
        .test_r_imm:
        ;; test r, imm
        call resolve_value     ; AX = immediate
        push ax
        cmp byte [op1_size], 8
        jne .test_r16_imm
        ;; test r8, imm8: short form for AL (A8), general form (F6 /0)
        cmp byte [op1_register], 0  ; AL?
        jne .test_r8_general
        mov al, 0A8h
        call emit_byte_al
        pop ax
        jmp emit_byte_al
        .test_r8_general:
        mov al, 0F6h
        call emit_byte_al
        mov al, [op1_register]
        or al, 0C0h            ; modrm: mod=11, /0, rm=reg
        call emit_byte_al
        pop ax
        jmp emit_byte_al
        .test_r16_imm:
        ;; test r16, imm16: short form for AX (A9), general form (F7 /0)
        cmp byte [op1_register], 0  ; AX?
        jne .test_r16_general
        mov al, 0A9h
        call emit_byte_al
        pop ax
        jmp emit_word_ax
        .test_r16_general:
        mov al, 0F7h
        call emit_byte_al
        mov al, [op1_register]
        or al, 0C0h
        call emit_byte_al
        pop ax
        jmp emit_word_ax
        .test_mem:
        ;; test byte [reg+disp], imm8: F6 modrm [disp] imm8
        ;; test byte [disp16],     imm8: F6 06 disp16 imm8
        call resolve_value     ; AX = immediate
        push ax
        mov al, 0F6h
        call emit_byte_al
        cmp byte [op1_type], 2  ; OP_MEM_DIRECT
        je .test_mem_direct
        ;; modrm: /0 with memory addressing
        mov al, [op1_register]
        call reg_to_rm         ; AL = rm
        cmp word [op1_value], 0
        jne .test_mem_disp
        call emit_byte_al      ; mod=00, /0, rm
        jmp .test_mem_imm
        .test_mem_disp:
        or al, 40h             ; mod=01
        call emit_byte_al
        mov ax, [op1_value]
        call emit_byte_al      ; disp8
        jmp .test_mem_imm
        .test_mem_direct:
        mov al, 06h            ; mod=00, /0, rm=110 (disp16)
        call emit_byte_al
        mov ax, [op1_value]
        call emit_word_ax
        .test_mem_imm:
        pop ax
        call emit_byte_al      ; imm8
        ret

;;; -----------------------------------------------------------------------
;;; handle_xchg: xchg r, r
;;; -----------------------------------------------------------------------
handle_xchg:
        call skip_ws
        call parse_register
        mov bl, al              ; BL = first operand (rm field)
        mov bh, ah
        call skip_comma
        call parse_register     ; AL = second operand (reg field)
        ;; Short form: xchg ax, r16 → 90h + reg (16-bit only)
        cmp bh, 8
        je .xchg_long
        cmp bl, 0
        je .xchg_short          ; first=AX: emit 90h + second
        cmp al, 0
        jne .xchg_long
        mov al, bl              ; second=AX: emit 90h + first
        .xchg_short:
        add al, 90h
        jmp emit_byte_al
        .xchg_long:
        push ax
        cmp bh, 8
        je .xchg8
        mov al, 87h
        jmp .xchg_emit
        .xchg8:
        mov al, 86h
        .xchg_emit:
        call emit_byte_al
        pop ax
        ;; Swap: NASM puts first operand in reg, second in rm
        xchg al, bl
        call make_modrm_reg_reg ; AL=first(reg), BL=second(rm)
        jmp emit_byte_al

;;; -----------------------------------------------------------------------
;;; handle_xor: xor r, r
;;; -----------------------------------------------------------------------
handle_xor:
        call skip_ws
        call parse_register    ; AH = size, AL = reg num (dst)
        push ax
        call skip_comma
        call parse_register    ; AH = size, AL = reg num (src)
        pop bx                 ; BL = dst reg
        ;; xor r8,r8: opcode 30, xor r16,r16: opcode 31
        push ax
        cmp ah, 8
        je .xor8
        mov al, 31h
        jmp .xor_emit
        .xor8:
        mov al, 30h
        .xor_emit:
        call emit_byte_al
        pop ax
        ;; modrm: mod=11, reg=src(AL), rm=dst(BL)
        call make_modrm_reg_reg
        jmp emit_byte_al

;;; -----------------------------------------------------------------------
;;; handle_unknown_word: treat unrecognized first word as bare label
;;; (e.g., "USAGE db `...`" — NASM allows labels without colons)
;;; SI points to the unrecognized word
;;; -----------------------------------------------------------------------
handle_unknown_word:
        mov di, si
        .skip_word:
        mov al, [si]
        cmp al, ' '
        je .got_bare_label
        cmp al, 9
        je .got_bare_label
        test al, al
        jz .huw_done
        inc si
        jmp .skip_word
        .got_bare_label:
        mov byte [si], 0       ; null-terminate label name
        ;; Define label (pass 1 only)
        cmp byte [pass], 1
        jne .bare_pass2
        push si
        mov si, di
        mov ax, [current_address]
        cmp byte [di], '.'
        je .bare_local
        mov bx, 0FFFFh
        call symbol_set
        mov ax, [last_symbol_index]
        mov [global_scope], ax
        jmp .bare_added
        .bare_local:
        mov bx, [global_scope]
        call symbol_set
        .bare_added:
        pop si
        jmp .bare_continue
        .bare_pass2:
        cmp byte [di], '.'
        je .bare_continue
        push si
        mov si, di
        mov bx, 0FFFFh
        call symbol_lookup
        jc .bare_no_scope
        mov ax, [last_symbol_index]
        mov [global_scope], ax
        .bare_no_scope:
        pop si
        .bare_continue:
        inc si                 ; skip past the null
        call skip_ws
        cmp byte [si], 0
        je .huw_done
        call parse_directive
        .huw_done:
        ret

;;; -----------------------------------------------------------------------
;;; hex_digit: convert ASCII hex digit in CL to value
;;; Returns value in CL, CF set if not a hex digit
;;; -----------------------------------------------------------------------
hex_digit:
        cmp cl, '0'
        jb .not_hex
        cmp cl, '9'
        jbe .digit
        cmp cl, 'A'
        jb .try_lower
        cmp cl, 'F'
        jbe .upper
        .try_lower:
        cmp cl, 'a'
        jb .not_hex
        cmp cl, 'f'
        ja .not_hex
        sub cl, 'a' - 10
        clc
        ret
        .upper:
        sub cl, 'A' - 10
        clc
        ret
        .digit:
        sub cl, '0'
        clc
        ret
        .not_hex:
        stc
        ret

;;; -----------------------------------------------------------------------
;;; include_pop: restore parent file state
;;; -----------------------------------------------------------------------
include_pop:
        push ax
        push bx
        push cx
        push si
        push di
        ;; Close the include file's fd
        mov bx, [source_fd]
        mov ah, SYS_IO_CLOSE
        call syscall
        ;; Restore from INCLUDE_SAVE (parent file state)
        mov bx, INCLUDE_SAVE
        mov ax, [bx+0]                 ; source_fd
        mov [source_fd], ax
        mov ax, [bx+2]                 ; source_buffer_position
        mov [source_buffer_position], ax
        mov ax, [bx+4]                 ; source_buffer_valid
        mov [source_buffer_valid], ax
        ;; Restore SOURCE_BUFFER
        push es
        push ds
        pop es                  ; ES=0 for rep movsw
        mov si, INCLUDE_SOURCE_SAVE
        mov di, SOURCE_BUFFER
        mov cx, 256
        cld
        rep movsw
        pop es
        dec byte [include_depth]
        pop di
        pop si
        pop cx
        pop bx
        pop ax
        ret

;;; -----------------------------------------------------------------------
;;; include_push: save current file state, switch to included file
;;; SI = pointer to include filename (null-terminated)
;;; -----------------------------------------------------------------------
include_push:
        push ax
        push bx
        ;; Save current file state to INCLUDE_SAVE (6 bytes)
        mov bx, INCLUDE_SAVE
        mov ax, [source_fd]
        mov [bx+0], ax
        mov ax, [source_buffer_position]
        mov [bx+2], ax
        mov ax, [source_buffer_valid]
        mov [bx+4], ax
        ;; Save SOURCE_BUFFER content
        push si
        push di
        push cx
        push es
        push ds
        pop es                  ; ES=0 for rep movsw
        mov si, SOURCE_BUFFER
        mov di, INCLUDE_SOURCE_SAVE
        mov cx, 256
        cld
        rep movsw
        pop es
        pop cx
        pop di
        pop si
        ;; Construct include_path = source_prefix + (include name at SI)
        mov bx, source_prefix
        mov di, include_path
        .ip_pfx:
        mov al, [bx]
        test al, al
        jz .ip_name
        mov [di], al
        inc bx
        inc di
        jmp .ip_pfx
        .ip_name:
        mov al, [si]
        mov [di], al
        inc di
        test al, al
        jz .ip_done
        inc si
        jmp .ip_name
        .ip_done:
        ;; Open included file
        mov si, include_path
        mov al, O_RDONLY
        mov ah, SYS_IO_OPEN
        call syscall
        jc .inc_err
        mov [source_fd], ax
        mov word [source_buffer_position], 0
        mov word [source_buffer_valid], 0
        inc byte [include_depth]
        pop bx
        pop ax
        ret
        .inc_err:
        mov byte [error_flag], 1
        pop bx
        pop ax
        ret

;;; -----------------------------------------------------------------------
;;; load_src_sector: read next chunk of source file into SOURCE_BUFFER via fd
;;; Returns CF if no more data (EOF)
;;; -----------------------------------------------------------------------
load_src_sector:
        push bx
        push cx
        push di
        mov bx, [source_fd]
        mov di, SOURCE_BUFFER
        mov cx, 512
        mov ah, SYS_IO_READ
        call syscall
        cmp ax, -1
        je .no_more
        test ax, ax
        jz .no_more
        mov [source_buffer_valid], ax
        mov word [source_buffer_position], 0
        clc
        pop di
        pop cx
        pop bx
        ret
        .no_more:
        stc
        pop di
        pop cx
        pop bx
        ret

;;; -----------------------------------------------------------------------
;;; make_modrm_reg_reg: AL=reg field, BL=rm field -> AL = modrm byte
;;; modrm = (3 << 6) | (reg << 3) | rm = C0 | (reg<<3) | rm
;;; -----------------------------------------------------------------------
make_modrm_reg_reg:
        shl al, 3
        or al, bl
        or al, 0C0h
        ret

;;; -----------------------------------------------------------------------
;;; reg_to_rm: convert register number to 16-bit addressing ModRM rm field
;;; Input: AL = register number (3=bx, 5=bp, 6=si, 7=di)
;;; Output: AL = rm field value
;;; -----------------------------------------------------------------------
reg_to_rm:
        cmp al, 3
        je .rm_bx
        cmp al, 6
        je .rm_si
        cmp al, 7
        je .rm_di
        mov al, 6              ; bp -> rm=6
        ret
        .rm_bx:
        mov al, 7
        ret
        .rm_si:
        mov al, 4
        ret
        .rm_di:
        mov al, 5
        ret

;;; -----------------------------------------------------------------------
;;; match_word: check if SI starts with word at DI (case-insensitive)
;;; If match: SI advanced past word, CF clear
;;; If no match: SI unchanged, CF set
;;; -----------------------------------------------------------------------
match_word:
        push ax
        push bx
        mov bx, si
        .mw_loop:
        mov al, [di]
        test al, al
        jz .mw_check_end
        ;; Compare case-insensitively
        mov ah, [si]
        ;; Lowercase both
        cmp al, 'A'
        jb .mw_no_low1
        cmp al, 'Z'
        ja .mw_no_low1
        or al, 20h
        .mw_no_low1:
        cmp ah, 'A'
        jb .mw_no_low2
        cmp ah, 'Z'
        ja .mw_no_low2
        or ah, 20h
        .mw_no_low2:
        cmp al, ah
        jne .mw_fail
        inc si
        inc di
        jmp .mw_loop
        .mw_check_end:
        ;; Word matched. Check next char in SI is not alphanumeric
        mov al, [si]
        cmp al, 'a'
        jb .mw_ok1
        cmp al, 'z'
        jbe .mw_fail
        .mw_ok1:
        cmp al, 'A'
        jb .mw_ok2
        cmp al, 'Z'
        jbe .mw_fail
        .mw_ok2:
        cmp al, '0'
        jb .mw_ok3
        cmp al, '9'
        jbe .mw_fail
        .mw_ok3:
        cmp al, '_'
        je .mw_fail
        ;; Match!
        clc
        pop bx
        pop ax
        ret
        .mw_fail:
        mov si, bx
        stc
        pop bx
        pop ax
        ret

;;; -----------------------------------------------------------------------
;;; parse_db: parse db operands (strings, bytes)
;;; SI points to first operand
;;; -----------------------------------------------------------------------
parse_db:
        .next_item:
        call skip_ws
        cmp byte [si], 0
        je .db_done
        cmp byte [si], ';'
        je .db_done

        ;; Check for backtick string
        cmp byte [si], '`'
        je .db_string

        ;; Check for single-quoted string. A bare `'c'` is a one-byte char
        ;; literal that resolve_value handles, but `'foo'` is a multi-byte
        ;; string and must be expanded byte-by-byte here. Detect strings of
        ;; length >= 2 by peeking past the first character.
        cmp byte [si], 27h
        jne .db_value
        cmp byte [si+2], 27h
        je .db_value           ; single-char literal -- let resolve_value handle
        jmp .db_squote

        .db_value:
        ;; Numeric/constant/char-literal value
        call resolve_value     ; AX = value
        call emit_byte_al
        call skip_ws
        cmp byte [si], ','
        jne .db_done
        inc si
        jmp .next_item

        .db_squote:
        inc si                 ; skip opening quote
        .sq_char:
        mov al, [si]
        cmp al, 27h
        je .sq_end
        test al, al
        jz .db_done            ; unterminated string
        call emit_byte_al
        inc si
        jmp .sq_char
        .sq_end:
        inc si                 ; skip closing quote
        call skip_ws
        cmp byte [si], ','
        jne .db_done
        inc si
        jmp .next_item

        .db_string:
        inc si                 ; skip opening backtick
        .str_char:
        mov al, [si]
        cmp al, '`'
        je .str_end
        cmp al, 0
        je .db_done
        cmp al, '\'
        je .str_escape
        call emit_byte_al
        inc si
        jmp .str_char
        .str_escape:
        inc si
        mov al, [si]
        cmp al, 'n'
        je .esc_n
        cmp al, '0'
        je .esc_0
        cmp al, 't'
        je .esc_t
        cmp al, '\'
        je .esc_bs
        cmp al, 'r'
        je .esc_r
        ;; Unknown escape, emit backslash and char
        push ax
        mov al, '\'
        call emit_byte_al
        pop ax
        call emit_byte_al
        inc si
        jmp .str_char
        .esc_n:
        mov al, 0Ah
        jmp .esc_emit
        .esc_0:
        mov al, 0
        jmp .esc_emit
        .esc_t:
        mov al, 09h
        jmp .esc_emit
        .esc_r:
        mov al, 0Dh
        jmp .esc_emit
        .esc_bs:
        mov al, '\'
        .esc_emit:
        call emit_byte_al
        inc si
        jmp .str_char
        .str_end:
        inc si                 ; skip closing backtick
        call skip_ws
        cmp byte [si], ','
        jne .db_done
        inc si
        jmp .next_item
        .db_done:
        ret

;;; -----------------------------------------------------------------------
;;; parse_directive: handle %assign, %include, org, db
;;; SI points to '%' or directive name
;;; -----------------------------------------------------------------------
parse_directive:
        push ax
        push bx
        push cx
        push dx

        cmp byte [si], '%'
        jne .not_percent
        inc si

        ;; Check %assign or %define — both bind NAME to an expression
        ;; evaluated immediately. We don't do macro text substitution, so
        ;; %define behaves identically to %assign here.
        mov di, STR_ASSIGN
        call match_word
        jnc .do_assign
        mov di, STR_DEFINE
        call match_word
        jc .try_include
        .do_assign:
        call skip_ws
        ;; Parse name
        mov di, si             ; DI = start of name
        .skip_name:
        cmp byte [si], ' '
        je .got_name
        cmp byte [si], 9
        je .got_name
        cmp byte [si], 0
        je .pd_done            ; %assign NAME with no value (silent skip)
        inc si
        jmp .skip_name
        .got_name:
        mov byte [si], 0       ; null-terminate name
        inc si
        call skip_ws
        ;; Parse value
        push di
        call resolve_value     ; AX = value
        pop di
        ;; Add to symbol table (pass 1)
        cmp byte [pass], 1
        jne .pd_done
        push si
        mov si, di
        mov bx, 0FFFFh         ; constants are global scope
        call symbol_add_constant
        pop si
        jmp .pd_done

        .try_include:
        ;; Check %include
        mov di, STR_INCLUDE
        call match_word
        jc .pd_done            ; % followed by neither %assign nor %include
        call skip_ws
        ;; Parse filename: expect "filename"
        cmp byte [si], '"'
        je .inc_quote
        jmp .pd_done           ; %include without opening quote
        .inc_quote:
        inc si                 ; skip opening quote
        mov di, si
        .find_close_quote:
        cmp byte [si], '"'
        je .got_inc_name
        cmp byte [si], 0
        je .pd_done            ; %include with unterminated string
        inc si
        jmp .find_close_quote
        .got_inc_name:
        mov byte [si], 0       ; null-terminate filename
        mov si, di             ; SI = filename
        call include_push
        jmp .pd_done

        .not_percent:
        ;; Check 'org'
        mov di, STR_ORG
        call match_word
        jc .try_times
        call skip_ws
        call resolve_value     ; AX = value
        mov [org_value], ax
        mov [current_address], ax
        jmp .pd_done

        .try_times:
        ;; Check 'times N <directive>' — repeats the inner directive N times.
        ;; Currently supports `times N db <values>` (the only form we use).
        mov di, STR_TIMES
        call match_word
        jc .try_db
        call skip_ws
        call resolve_value     ; CX = repeat count (via AX)
        mov cx, ax
        call skip_ws
        ;; Expect 'db' next
        mov di, STR_DB
        call match_word
        jc .pd_done            ; only `times N db ...` form supported
        call skip_ws
        ;; Save the data position so we can re-parse it for each iteration
        mov dx, si
        .times_loop:
        test cx, cx
        jz .pd_done
        mov si, dx
        push cx
        call parse_db
        pop cx
        dec cx
        jmp .times_loop

        .try_db:
        ;; Check 'db'
        mov di, STR_DB
        call match_word
        jc .try_dw
        call skip_ws
        call parse_db
        jmp .pd_done

        .try_dw:
        ;; Check 'dw' — emit each value as a 16-bit little-endian word
        mov di, STR_DW
        call match_word
        jc .try_dd
        call skip_ws
        .dw_next:
        call resolve_value     ; AX = value
        call emit_word_ax
        call skip_ws
        cmp byte [si], ','
        jne .pd_done
        inc si
        call skip_ws
        jmp .dw_next

        .try_dd:
        ;; Check 'dd' — emit each value as a 32-bit little-endian dword
        ;; Only literal numbers are supported (no expressions); the high
        ;; 16 bits are emitted as zero, which is enough for our use case.
        mov di, STR_DD
        call match_word
        jc .try_mnemonic
        call skip_ws
        .dd_next:
        call resolve_value     ; AX = value (low 16)
        call emit_word_ax
        xor ax, ax             ; high 16 = 0
        call emit_word_ax
        call skip_ws
        cmp byte [si], ','
        jne .pd_done
        inc si
        call skip_ws
        jmp .dd_next

        .try_mnemonic:
        ;; Not a directive — try instruction mnemonic
        call parse_mnemonic
        jmp .pd_done

        .pd_done:
        pop dx
        pop cx
        pop bx
        pop ax
        ret

;;; -----------------------------------------------------------------------
;;; parse_line: parse one source line from LINE_BUFFER
;;; -----------------------------------------------------------------------
parse_line:
        push ax
        push bx
        push cx
        push dx
        push si
        push di

        mov si, LINE_BUFFER
        call skip_ws

        ;; Empty line or comment?
        cmp byte [si], 0
        je .done
        cmp byte [si], ';'
        je .done

        ;; Check for % directive (skip label scan for % lines)
        cmp byte [si], '%'
        je .check_directive

        ;; Check for label: scan for ':'
        ;; First check if this looks like a label (identifier followed by ':')
        push si
        .scan_colon:
        mov al, [si]
        test al, al
        jz .no_label
        cmp al, ':'
        je .found_label
        cmp al, ' '
        je .check_equ
        cmp al, 9
        je .check_equ
        inc si
        jmp .scan_colon

        .check_equ:
        ;; SI points to space after identifier; stack has name start
        mov [equ_space], si    ; save space position
        call skip_ws
        mov di, STR_EQU
        call match_word
        jc .not_equ
        ;; "NAME equ VALUE" — null-terminate name at the space
        pop di                 ; DI = start of name
        mov bx, [equ_space]
        mov byte [bx], 0
        call skip_ws
        call resolve_value     ; AX = value
        ;; Add as constant (pass 1 only)
        cmp byte [pass], 1
        jne .equ_done
        push si
        mov si, di
        mov bx, 0FFFFh         ; global scope
        call symbol_add_constant
        pop si
        .equ_done:
        ;; Restore null-terminated byte
        mov bx, [equ_space]
        mov byte [bx], ' '
        jmp .done

        .not_equ:
        mov si, [equ_space]    ; restore SI to space position
        jmp .no_label

        .found_label:
        ;; SI points to ':', stack has start of label name
        mov byte [si], 0       ; null-terminate label name
        pop di                 ; DI = start of label name
        push si                ; save position after ':'
        ;; Add label to symbol table (pass 1 only)
        cmp byte [pass], 1
        jne .skip_add_label
        ;; Determine scope
        cmp byte [di], '.'
        je .local_label
        ;; Global label
        push di
        mov si, di
        mov ax, [current_address]
        mov bx, 0FFFFh         ; scope = global
        call symbol_set
        ;; Update global_scope to this symbol's index
        mov ax, [last_symbol_index]
        mov [global_scope], ax
        pop di
        jmp .skip_add_label
        .local_label:
        mov si, di
        mov ax, [current_address]
        mov bx, [global_scope]
        call symbol_set
        .skip_add_label:
        ;; If pass 2, update global_scope for global labels
        cmp byte [pass], 2
        jne .after_label
        cmp byte [di], '.'
        je .after_label
        ;; Find this global label in symbol table to get its index
        mov si, di
        mov bx, 0FFFFh
        call symbol_lookup
        jc .after_label
        mov ax, [last_symbol_index]
        mov [global_scope], ax
        .after_label:
        pop si                 ; SI = position where ':' was (now null)
        mov byte [si], ':'     ; restore the ':'
        inc si                 ; SI = past ':'
        call skip_ws
        cmp byte [si], 0
        je .done
        cmp byte [si], ';'
        je .done
        ;; Fall through to parse instruction/directive after label
        jmp .check_directive

        .no_label:
        pop si                 ; restore original SI

        .check_directive:
        call parse_directive
        jmp .done

        .done:
        pop di
        pop si
        pop dx
        pop cx
        pop bx
        pop ax
        ret

;;; -----------------------------------------------------------------------
;;; parse_mnemonic: identify and encode instruction
;;; SI points to mnemonic
;;; -----------------------------------------------------------------------
parse_mnemonic:
        push ax
        push bx
        push cx
        push dx

        ;; Try each mnemonic in the table
        mov bx, mnemonic_table
        .try_next:
        mov di, [bx]
        test di, di
        jz .unknown
        call match_word        ; on match: SI advanced past mnemonic, CF clear
        jc .next_entry
        ;; Found match -- SI points past mnemonic
        call [bx+2]
        jmp .pm_done
        .next_entry:
        add bx, 4
        jmp .try_next
        .unknown:
        ;; Unknown mnemonic -- might be a bare label (e.g., "USAGE db ...")
        call handle_unknown_word
        .pm_done:
        pop dx
        pop cx
        pop bx
        pop ax
        ret

;;; -----------------------------------------------------------------------
;;; parse_number: parse decimal or hex number at SI
;;; Returns value in AX, advances SI
;;; -----------------------------------------------------------------------
parse_number:
        push bx
        push cx
        push dx
        ;; Check for 0x prefix
        cmp byte [si], '0'
        jne .not_0x
        cmp byte [si+1], 'x'
        je .hex_prefix
        cmp byte [si+1], 'X'
        je .hex_prefix
        .not_0x:
        ;; Scan ahead to check for 'h' suffix (handles 4FEh, 0F1h, 30h, etc.)
        push si
        .scan_h:
        mov al, [si]
        cmp al, '0'
        jb .scan_h_done
        cmp al, '9'
        jbe .scan_h_next
        cmp al, 'a'
        jb .check_upper_hex
        cmp al, 'f'
        jbe .scan_h_next
        cmp al, 'h'
        je .is_hex_suffix
        jmp .scan_h_done
        .check_upper_hex:
        cmp al, 'A'
        jb .scan_h_done
        cmp al, 'F'
        jbe .scan_h_next
        cmp al, 'h'
        je .is_hex_suffix
        cmp al, 'H'
        je .is_hex_suffix
        jmp .scan_h_done
        .scan_h_next:
        inc si
        jmp .scan_h
        .scan_h_done:
        pop si
        jmp .try_decimal
        .is_hex_suffix:
        pop si
        jmp .hex_suffix

        .hex_prefix:
        add si, 2              ; skip 0x
        xor ax, ax
        .hex_p_loop:
        mov cl, [si]
        call hex_digit         ; CL = digit value, CF if not hex
        jc .hex_p_done
        shl ax, 4
        or al, cl
        inc si
        jmp .hex_p_loop
        .hex_p_done:
        pop dx
        pop cx
        pop bx
        ret

        .hex_suffix:
        ;; Parse hex digits until 'h'
        xor ax, ax
        .hex_s_loop:
        mov cl, [si]
        cmp cl, 'h'
        je .hex_s_end
        cmp cl, 'H'
        je .hex_s_end
        call hex_digit
        jc .hex_s_end
        shl ax, 4
        or al, cl
        inc si
        jmp .hex_s_loop
        .hex_s_end:
        cmp byte [si], 'h'
        jne .pn_ret
        inc si                 ; skip 'h'
        jmp .pn_ret
        .try_decimal:
        xor ax, ax
        .dec_loop:
        mov cl, [si]
        cmp cl, '0'
        jb .pn_ret
        cmp cl, '9'
        ja .pn_ret
        ;; ax = ax * 10 + digit
        push dx
        mov bx, 10
        mul bx
        pop dx
        sub cl, '0'
        xor ch, ch
        add ax, cx
        inc si
        jmp .dec_loop
        .pn_ret:
        pop dx
        pop cx
        pop bx
        ret

;;; -----------------------------------------------------------------------
;;; parse_operand: parse one operand
;;; Returns: AH = type (0=reg, 1=imm, 2=mem_direct, 3=mem_bx_disp)
;;;          AL = register number (if reg)
;;;          DX = value (if imm or displacement)
;;; Also sets [op1_size] or uses it
;;; -----------------------------------------------------------------------
parse_operand:
        call skip_ws
        ;; Check for 'byte' size prefix
        push si
        mov di, STR_BYTE
        call match_word
        jc .no_byte_prefix
        add sp, 2
        mov byte [op1_size], 8
        call skip_ws
        cmp byte [si], '['
        je .mem_operand
        ;; 'byte' before a register (e.g., "mov byte bl, [var]") — just a hint
        jmp .try_register
        .no_byte_prefix:
        pop si
        ;; Check for 'word' size prefix (no-op since 16 is the default)
        push si
        mov di, STR_WORD
        call match_word
        jc .no_word_prefix
        add sp, 2
        mov byte [op1_size], 16
        call skip_ws
        cmp byte [si], '['
        je .mem_operand
        jmp .try_register
        .no_word_prefix:
        pop si

        cmp byte [si], '['
        je .mem_operand

        .try_register:
        ;; Try register first
        push si
        call parse_register
        jnc .is_reg
        pop si

        ;; Must be immediate (number or symbol)
        call resolve_value
        mov dx, ax
        mov ah, 1              ; OP_IMM
        xor al, al
        ret

        .is_reg:
        add sp, 2              ; discard saved SI
        mov [op1_size], ah     ; save register size
        mov ah, 0              ; OP_REG
        ret

        .mem_operand:
        inc si                 ; skip '['
        call skip_ws
        ;; Check for segment override prefix 'es:'
        cmp byte [si], 'e'
        jne .memory_no_segment
        cmp byte [si+1], 's'
        jne .memory_no_segment
        cmp byte [si+2], ':'
        jne .memory_no_segment
        push ax
        mov al, 26h            ; ES segment override prefix
        call emit_byte_al
        pop ax
        add si, 3              ; skip 'es:'
        call skip_ws
        .memory_no_segment:
        ;; Case 1: bracket starts with a register name -> [reg(+disp)?]
        push si
        call parse_register
        jnc .mem_with_reg
        pop si
        ;; Case 2: detect `[<disp> + <reg>]` (e.g. `[BUF_BASE + bx]`) by
        ;; scanning the bracket and checking whether it ends with a
        ;; register preceded by `+`. If so, temporarily null out the `+`
        ;; so resolve_value computes only the displacement, then patch
        ;; the value back.
        push si                        ; save bracket start
        mov di, si
        .mem_find_close:
        mov al, [di]
        cmp al, ']'
        je .mem_close_found
        test al, al
        jz .mem_close_found
        inc di
        jmp .mem_find_close
        .mem_close_found:
        ;; DI = ']' (or NUL). Walk back over trailing whitespace.
        .mem_back_ws:
        cmp di, si
        jbe .mem_no_disp_reg
        mov al, [di-1]
        cmp al, ' '
        jne .mem_check_reg
        dec di
        jmp .mem_back_ws
        .mem_check_reg:
        ;; A register name is exactly 2 chars. Need at least 2 between
        ;; SI and DI.
        mov bx, di
        sub bx, si
        cmp bx, 2
        jb .mem_no_disp_reg
        mov bx, di
        sub bx, 2                      ; BX -> candidate register start
        ;; Try to parse a register at BX.
        push si
        mov si, bx
        call parse_register
        pop si
        jc .mem_no_disp_reg
        ;; AL=reg, AH=size; SI is unchanged here (we restored it).
        ;; Walk back from BX over whitespace looking for '+'.
        push ax                        ; save reg
        .mem_back_plus_ws:
        cmp bx, si
        jbe .mem_pop_no_dr
        mov dl, [bx-1]
        cmp dl, ' '
        jne .mem_check_plus
        dec bx
        jmp .mem_back_plus_ws
        .mem_check_plus:
        cmp byte [bx-1], '+'
        jne .mem_pop_no_dr
        ;; BX-1 -> '+'. We have `[<disp> + <reg>]`.
        ;; Null-terminate at '+' so resolve_value reads just the disp.
        dec bx                         ; BX -> '+'
        mov byte [bx], 0
        push bx                        ; save '+' position for restore
        push di                        ; save ']' position
        call resolve_value             ; parses [<disp>]
        mov dx, ax                     ; DX = displacement
        pop di
        pop bx
        mov byte [bx], '+'             ; restore
        mov si, di
        ;; Skip past ']' if present
        cmp byte [si], ']'
        jne .mem_dr_set
        inc si
        .mem_dr_set:
        pop ax                         ; AL = reg from earlier parse
        add sp, 2                      ; discard saved bracket-start SI
        mov ah, 3                      ; OP_MEM_BX_DISP
        ret
        .mem_pop_no_dr:
        pop ax                         ; discard saved reg
        .mem_no_disp_reg:
        pop si                         ; restore SI to bracket start
        ;; Plain direct memory: [number_or_symbol[+expr]]
        call resolve_value
        mov dx, ax
        .find_close:
        cmp byte [si], ']'
        je .found_close
        cmp byte [si], 0
        je .found_close
        inc si
        jmp .find_close
        .found_close:
        cmp byte [si], ']'
        jne .mem_done
        inc si
        .mem_done:
        mov ah, 2              ; OP_MEM_DIRECT
        xor al, al
        ret

        .mem_with_reg:
        add sp, 2              ; discard saved SI
        ;; AL = register, check for +disp or -disp
        call skip_ws
        cmp byte [si], '+'
        je .mem_with_reg_pos
        cmp byte [si], '-'
        je .mem_with_reg_neg
        jmp .mem_reg_only
        .mem_with_reg_pos:
        inc si                 ; skip '+'
        call skip_ws
        push ax
        call resolve_value     ; AX = displacement
        mov dx, ax
        pop ax
        jmp .mem_with_reg_close
        .mem_with_reg_neg:
        inc si                 ; skip '-'
        call skip_ws
        push ax
        call resolve_value     ; AX = displacement (positive)
        neg ax
        mov dx, ax
        pop ax
        .mem_with_reg_close:
        ;; Find closing ']'
        call skip_ws
        cmp byte [si], ']'
        jne .mem_bx_done
        inc si
        .mem_bx_done:
        mov ah, 3              ; OP_MEM_BX_DISP
        ret

        .mem_reg_only:
        ;; [reg] without displacement -- treat as [reg+0]
        cmp byte [si], ']'
        jne .mem_bx_done2
        inc si
        .mem_bx_done2:
        xor dx, dx
        mov ah, 3              ; OP_MEM_BX_DISP with disp=0
        ret

;;; -----------------------------------------------------------------------
;;; parse_register: parse register name at SI
;;; Returns: AL = reg number (0-7), AH = size (8 or 16), CF clear
;;;          CF set if not a register, SI unchanged
;;; -----------------------------------------------------------------------
parse_register:
        push bx
        push cx
        push di
        mov bx, register_table
        .try_reg:
        mov di, bx
        cmp byte [di], 0
        je .not_reg
        ;; Compare 2 chars
        mov al, [si]
        ;; Convert to lowercase
        cmp al, 'A'
        jb .no_lower1
        cmp al, 'Z'
        ja .no_lower1
        or al, 20h
        .no_lower1:
        cmp al, [di]
        jne .next_reg
        mov al, [si+1]
        cmp al, 'A'
        jb .no_lower2
        cmp al, 'Z'
        ja .no_lower2
        or al, 20h
        .no_lower2:
        cmp al, [di+1]
        jne .next_reg
        ;; Check that the next char in input is not alphanumeric/underscore
        mov al, [si+2]
        cmp al, 'a'
        jb .check_upper
        cmp al, 'z'
        jbe .next_reg
        jmp .reg_match
        .check_upper:
        cmp al, 'A'
        jb .check_digit
        cmp al, 'Z'
        jbe .next_reg
        .check_digit:
        cmp al, '0'
        jb .reg_match
        cmp al, '9'
        jbe .next_reg
        cmp al, '_'
        je .next_reg
        .reg_match:
        ;; Match! Read reg number and size
        mov al, [bx+2]        ; reg number
        mov ah, [bx+3]        ; size (8 or 16)
        add si, 2              ; advance past register name
        clc
        pop di
        pop cx
        pop bx
        ret
        .next_reg:
        add bx, 4
        jmp .try_reg
        .not_reg:
        stc
        pop di
        pop cx
        pop bx
        ret

;;; -----------------------------------------------------------------------
;;; peek_label_target: SI -> label name. Looks up the label without
;;; advancing SI. Returns AX = address with CF clear if found, CF set
;;; if not found. Only the bare label name is recognised (no arithmetic).
;;; -----------------------------------------------------------------------
peek_label_target:
        push si
        push bx
        push cx
        push dx
        push di
        ;; Find end of label name (letters, digits, '_', '.')
        mov di, si
        .pl_find_end:
        mov al, [di]
        cmp al, '.'
        je .pl_is_id
        cmp al, '_'
        je .pl_is_id
        cmp al, 'a'
        jb .pl_check_upper
        cmp al, 'z'
        jbe .pl_is_id
        jmp .pl_end
        .pl_check_upper:
        cmp al, 'A'
        jb .pl_check_dig
        cmp al, 'Z'
        jbe .pl_is_id
        .pl_check_dig:
        cmp al, '0'
        jb .pl_end
        cmp al, '9'
        ja .pl_end
        .pl_is_id:
        inc di
        jmp .pl_find_end
        .pl_end:
        ;; Save delim, null-terminate label
        mov cl, [di]
        mov byte [di], 0
        push di
        ;; Determine scope from leading char
        cmp byte [si], '.'
        jne .pl_global
        mov bx, [global_scope]
        jmp .pl_lookup
        .pl_global:
        mov bx, 0FFFFh
        .pl_lookup:
        mov word [last_symbol_index], 0FFFFh
        call symbol_lookup                ; sets last_symbol_index only when found
        pop di
        mov [di], cl                   ; restore delim
        cmp word [last_symbol_index], 0FFFFh
        je .pl_not_found
        clc
        pop di
        pop dx
        pop cx
        pop bx
        pop si
        ret
        .pl_not_found:
        stc
        pop di
        pop dx
        pop cx
        pop bx
        pop si
        ret

;;; -----------------------------------------------------------------------
;;; print_hex_word: print AX as 4-digit hex
;;; -----------------------------------------------------------------------
print_hex_word:
        push ax
        xchg al, ah
        call .print_byte
        pop ax
        call .print_byte
        ret
        .print_byte:
        push ax
        shr al, 4
        call .nibble
        pop ax
        and al, 0Fh
        call .nibble
        ret
        .nibble:
        add al, '0'
        cmp al, '9'
        jbe .nib_ok
        add al, 7
        .nib_ok:
        call call_print_character
        ret

;;; -----------------------------------------------------------------------
;;; read_line: read one line from source into LINE_BUFFER
;;; Returns CF set on EOF
;;; -----------------------------------------------------------------------
read_line:
        push bx
        push cx
        push dx
        mov di, LINE_BUFFER
        xor cx, cx             ; CX = chars in line

        .next_byte:
        ;; Check if source buffer needs refill
        mov ax, [source_buffer_position]
        cmp ax, [source_buffer_valid]
        jb .have_byte

        ;; Need more data -- is file exhausted?
        call load_src_sector
        jc .check_eof

        .have_byte:
        mov bx, [source_buffer_position]
        mov al, [SOURCE_BUFFER + bx]
        inc word [source_buffer_position]

        cmp al, 0Ah            ; newline
        je .got_line
        cmp al, 0Dh            ; carriage return -- skip
        je .next_byte
        cmp cx, LINE_MAX
        jae .next_byte         ; line too long, discard
        mov [di], al
        inc di
        inc cx
        jmp .next_byte

        .got_line:
        mov byte [di], 0
        clc
        pop dx
        pop cx
        pop bx
        ret

        .check_eof:
        ;; If we read some chars, return them
        test cx, cx
        jnz .got_line
        ;; True EOF
        stc
        pop dx
        pop cx
        pop bx
        ret

;;; -----------------------------------------------------------------------
;;; resolve_label: like resolve_value but for jump targets
;;; On pass 1, returns current_address (placeholder)
;;; -----------------------------------------------------------------------
resolve_label:
        cmp byte [pass], 1
        je .pass1
        call resolve_value
        ret
        .pass1:
        ;; Skip past label name, return current_address as placeholder
        .skip_label:
        mov al, [si]
        cmp al, 'a'
        jae .skip_more
        cmp al, 'A'
        jb .check_d
        cmp al, 'Z'
        jbe .skip_more
        .check_d:
        cmp al, '0'
        jb .skip_done
        cmp al, '9'
        ja .check_s
        .skip_more:
        inc si
        jmp .skip_label
        .check_s:
        cmp al, '_'
        je .skip_more
        cmp al, '.'
        je .skip_more
        .skip_done:
        mov ax, [current_address]
        ret

;;; -----------------------------------------------------------------------
;;; resolve_value: parse number or look up symbol at SI
;;; Returns value in AX, advances SI past the token
;;; -----------------------------------------------------------------------
resolve_value:
        push bx
        push cx
        push di
        call skip_ws
        ;; Parenthesised sub-expression
        cmp byte [si], '('
        je .paren
        ;; Check for character literal: 'c' or `\n`
        cmp byte [si], 27h    ; single quote
        je .char_literal
        cmp byte [si], '`'
        je .backtick_literal
        ;; Check for $ (current address)
        cmp byte [si], '$'
        jne .not_dollar
        inc si
        mov ax, [current_address]
        jmp .check_expr
        .not_dollar:
        ;; Check if starts with digit -- it's a number
        mov al, [si]
        cmp al, '0'
        jb .try_symbol
        cmp al, '9'
        ja .try_symbol
        call parse_number
        jmp .check_expr

        .paren:
        inc si                 ; skip '('
        call resolve_value     ; recursive: parse inner expression
        call skip_ws
        cmp byte [si], ')'
        jne .check_expr
        inc si                 ; skip ')'
        jmp .check_expr

        .char_literal:
        inc si                 ; skip opening quote
        xor ah, ah
        mov al, [si]           ; AL = character value
        inc si
        cmp byte [si], 27h    ; closing quote
        jne .check_expr
        inc si                 ; skip closing quote
        jmp .check_expr

        .backtick_literal:
        inc si                 ; skip opening backtick
        xor ah, ah
        mov al, [si]
        cmp al, '\'
        jne .bt_plain
        ;; Escape sequence
        inc si
        mov al, [si]
        cmp al, 'n'
        je .bt_n
        cmp al, '0'
        je .bt_0
        cmp al, 't'
        je .bt_t
        cmp al, 'r'
        je .bt_r
        jmp .bt_skip_close     ; unknown escape, use char as-is
        .bt_n:
        mov al, 0Ah
        jmp .bt_skip_close
        .bt_0:
        xor al, al
        jmp .bt_skip_close
        .bt_t:
        mov al, 09h
        jmp .bt_skip_close
        .bt_r:
        mov al, 0Dh
        .bt_skip_close:
        .bt_plain:
        inc si
        cmp byte [si], '`'
        jne .check_expr
        inc si                 ; skip closing backtick
        jmp .check_expr

        .try_symbol:
        ;; Read identifier and look up in symbol table
        mov di, si
        ;; Find end of identifier (letters, digits, '_', '.')
        .find_end:
        mov al, [si]
        cmp al, '.'
        je .is_ident
        cmp al, '_'
        je .is_ident
        cmp al, 'a'
        jae .is_ident
        cmp al, 'A'
        jb .check_digit
        cmp al, 'Z'
        jbe .is_ident
        .check_digit:
        cmp al, '0'
        jb .end_ident
        cmp al, '9'
        ja .end_ident
        .is_ident:
        inc si
        jmp .find_end
        .end_ident:
        ;; DI = start, SI = end
        mov cl, [si]           ; save delimiter
        mov byte [si], 0       ; null-terminate
        push cx
        push si
        mov si, di
        ;; Determine scope for lookup
        cmp byte [si], '.'
        jne .global_lookup
        mov bx, [global_scope]
        jmp .do_lookup
        .global_lookup:
        mov bx, 0FFFFh
        .do_lookup:
        call symbol_lookup        ; AX = value
        pop si
        pop cx
        mov [si], cl           ; restore delimiter
        .check_expr:
        ;; Check for +/-/* arithmetic after the parsed value
        call skip_ws
        cmp byte [si], '+'
        je .expr_add
        cmp byte [si], '-'
        je .expr_sub
        cmp byte [si], '/'
        je .expr_div
        .expr_done:
        pop di
        pop cx
        pop bx
        ret
        .expr_add:
        inc si
        push ax
        call skip_ws
        call resolve_value     ; recursive: get RHS
        mov cx, ax
        pop ax
        add ax, cx
        jmp .expr_done
        .expr_sub:
        inc si
        push ax
        call skip_ws
        call resolve_value
        mov cx, ax
        pop ax
        sub ax, cx
        jmp .expr_done
        .expr_div:
        inc si
        push ax
        call skip_ws
        call resolve_value
        mov cx, ax
        pop ax
        xor dx, dx
        div cx
        jmp .expr_done

;;; -----------------------------------------------------------------------
;;; skip_comma: skip whitespace, comma, whitespace
;;; -----------------------------------------------------------------------
skip_comma:
        call skip_ws
        cmp byte [si], ','
        jne .done
        inc si
        call skip_ws
        .done:
        ret

;;; -----------------------------------------------------------------------
;;; skip_ws: skip spaces and tabs at SI
;;; -----------------------------------------------------------------------
skip_ws:
        .loop:
        cmp byte [si], ' '
        je .skip
        cmp byte [si], 9
        je .skip
        ret
        .skip:
        inc si
        jmp .loop

;;; -----------------------------------------------------------------------
;;; symbol_add: add label to symbol table
;;; SI = name, AX = value, BX = scope (0xFFFF = global)
;;; -----------------------------------------------------------------------
symbol_add:
        ;; SI = name, AX = value, BX = scope
        ;; Refuse to overflow the symbol table -- doing so silently corrupts
        ;; LINE_BUFFER (which sits immediately after) and produces wrong output.
        cmp word [symbol_count], SYMBOL_MAX
        jae .symbol_overflow
        push cx
        push di
        push si
        ;; Compute entry offset: symbol_count * SYMBOL_ENTRY (in ES segment)
        push ax                ; save value
        push bx                ; save scope
        mov ax, [symbol_count]
        call symbol_entry_address    ; DI = entry offset in ES
        ;; Copy name (up to SYMBOL_NAME_LENGTH-1 chars + null)
        mov cx, SYMBOL_NAME_LENGTH - 1
        .copy_sym_name:
        mov al, [si]
        test al, al
        jz .pad_name
        mov [es:di], al
        inc si
        inc di
        dec cx
        jnz .copy_sym_name
        .pad_name:
        mov byte [es:di], 0
        inc di
        dec cx
        jns .pad_name
        ;; Re-derive entry base for metadata
        mov ax, [symbol_count]
        call symbol_entry_address    ; DI = entry offset in ES
        ;; Write metadata at offset SYMBOL_NAME_LENGTH
        pop bx                 ; restore scope
        pop ax                 ; restore value
        mov [es:di+SYMBOL_NAME_LENGTH], ax     ; value
        mov byte [es:di+SYMBOL_NAME_LENGTH+2], 0 ; type = label
        mov [es:di+SYMBOL_NAME_LENGTH+3], bl   ; scope
        inc word [symbol_count]
        pop si
        pop di
        pop cx
        ret
        .symbol_overflow:
        mov si, MESSAGE_SYMBOL_OVERFLOW
        mov cx, MESSAGE_SYMBOL_OVERFLOW_LENGTH
        jmp call_die

;;; -----------------------------------------------------------------------
;;; symbol_add_constant: add constant (%assign) to symbol table
;;; SI = name, AX = value
;;; -----------------------------------------------------------------------
symbol_add_constant:
        push bx
        mov bx, 0FFFFh
        call symbol_set
        ;; Fix type to 1 (constant)
        push ax
        mov ax, [last_symbol_index]
        call symbol_entry_address    ; DI = entry address
        mov byte [es:di+SYMBOL_NAME_LENGTH+2], 1
        pop ax
        pop bx
        ret

;;; -----------------------------------------------------------------------
;;; symbol_entry_address: compute symbol table entry offset within ES segment
;;; AX = index, returns DI = index * SYMBOL_ENTRY (ES-relative)
;;; Clobbers AX, DX
;;; -----------------------------------------------------------------------
symbol_entry_address:
        push bx
        mov bx, SYMBOL_ENTRY
        mul bx                 ; AX = index * SYMBOL_ENTRY (DX clobbered)
        mov di, ax
        pop bx
        ret

;;; -----------------------------------------------------------------------
;;; symbol_lookup: find symbol by name
;;; SI = name (null-terminated), BX = scope (0xFFFF for global search)
;;; Returns: AX = value, CF clear if found; CF set if not found
;;; -----------------------------------------------------------------------
symbol_lookup:
        push cx
        push dx
        push di
        mov cx, [symbol_count]
        test cx, cx
        jz .symbol_not_found
        xor di, di             ; DI = offset 0 within ES segment
        xor dx, dx             ; DX = index
        .symbol_search:
        ;; Filter by scope: BL = wanted scope; entry's scope at di+SYMBOL_NAME_LENGTH+3.
        ;; Wanted 0xFF means global; only match entries whose scope is 0xFF.
        ;; Wanted other means local; only match entries whose scope == BL.
        push ax
        mov al, [es:di+SYMBOL_NAME_LENGTH+3]
        cmp al, bl
        pop ax
        jne .symbol_next
        ;; Compare names
        push si
        push di
        push cx
        .cmp_name:
        mov al, [si]
        cmp al, [es:di]
        jne .symbol_no_match
        test al, al
        jz .symbol_name_match
        inc si
        inc di
        jmp .cmp_name
        .symbol_name_match:
        pop cx
        pop di
        pop si
        ;; Name matches -- found
        .symbol_found:
        mov ax, [es:di+SYMBOL_NAME_LENGTH]
        mov [last_symbol_index], dx
        clc
        pop di
        pop dx
        pop cx
        ret
        .symbol_no_match:
        pop cx
        pop di
        pop si
        .symbol_next:
        add di, SYMBOL_ENTRY
        inc dx
        loop .symbol_search
        .symbol_not_found:
        ;; Return 0 on pass 1 (symbol not yet defined)
        xor ax, ax
        cmp byte [pass], 1
        je .symbol_pass1_ok
        stc
        pop di
        pop dx
        pop cx
        ret
        .symbol_pass1_ok:
        clc
        pop di
        pop dx
        pop cx
        ret

;;; -----------------------------------------------------------------------
;;; symbol_set: update existing entry's value, or add a new entry.
;;; SI = name (null-terminated), AX = value, BX = scope.
;;; On return: last_symbol_index = the entry's index.
;;; Used by pass 1, which iterates -- the first iteration adds entries
;;; and subsequent iterations update them as current_address shifts.
;;; -----------------------------------------------------------------------
symbol_set:
        mov [symbol_set_value], ax
        mov [symbol_set_scope], bx
        push di
        push cx
        push dx
        mov word [last_symbol_index], 0FFFFh
        call symbol_lookup
        cmp word [last_symbol_index], 0FFFFh
        je .ss_add
        ;; Found -- update value in place
        mov ax, [last_symbol_index]
        call symbol_entry_address    ; DI = entry offset in ES
        mov ax, [symbol_set_value]
        mov [es:di+SYMBOL_NAME_LENGTH], ax
        pop dx
        pop cx
        pop di
        ret
        .ss_add:
        pop dx
        pop cx
        pop di
        mov ax, [symbol_set_value]
        mov bx, [symbol_set_scope]
        call symbol_add
        ;; Record the new entry's index in last_symbol_index for callers.
        push ax
        mov ax, [symbol_count]
        dec ax
        mov [last_symbol_index], ax
        pop ax
        ret

;;; -----------------------------------------------------------------------
;;; Mnemonic table: pairs of (name_ptr, handler_ptr), terminated by 0
;;; -----------------------------------------------------------------------
mnemonic_table:
        dw STR_AAM, handle_aam
        dw STR_ADD, handle_add
        dw STR_AND, handle_and
        dw STR_CALL, handle_call
        dw STR_CLC, handle_clc
        dw STR_CLD, handle_cld
        dw STR_CMP, handle_cmp
        dw STR_DEC, handle_dec
        dw STR_DIV, handle_div
        dw STR_INC, handle_inc
        dw STR_INT, handle_int
        dw STR_JA,  handle_ja
        dw STR_JAE, handle_jnc
        dw STR_JB,  handle_jb
        dw STR_JBE, handle_jbe
        dw STR_JC,  handle_jc
        dw STR_JE,  handle_jz
        dw STR_JG,  handle_jg
        dw STR_JGE, handle_jge
        dw STR_JL,  handle_jl
        dw STR_JLE, handle_jle
        dw STR_JMP, handle_jmp
        dw STR_JNC, handle_jnc
        dw STR_JNE, handle_jne
        dw STR_JNS, handle_jns
        dw STR_JNZ, handle_jne
        dw STR_JZ,  handle_jz
        dw STR_LODSB, handle_lodsb
        dw STR_LOOP, handle_loop
        dw STR_MOV, handle_mov
        dw STR_MOVSB, handle_movsb
        dw STR_MOVSW, handle_movsw
        dw STR_MOVZX, handle_movzx
        dw STR_MUL, handle_mul
        dw STR_NEG, handle_neg
        dw STR_OR,  handle_or
        dw STR_POP, handle_pop
        dw STR_PUSH, handle_push
        dw STR_REP, handle_rep
        dw STR_REPNE, handle_repne
        dw STR_RET, handle_ret
        dw STR_SBB, handle_sbb
        dw STR_SCASB, handle_scasb
        dw STR_SHL, handle_shl
        dw STR_SHR, handle_shr
        dw STR_STC, handle_stc
        dw STR_STOSB, handle_stosb
        dw STR_STOSW, handle_stosw
        dw STR_SUB, handle_sub
        dw STR_TEST, handle_test
        dw STR_XCHG, handle_xchg
        dw STR_XOR, handle_xor
        dw 0

;;; Mnemonic strings
STR_AAM     db 'aam',0
STR_ADD     db 'add',0
STR_AND     db 'and',0
STR_ASSIGN  db 'assign',0
STR_BYTE    db 'byte',0
STR_CALL    db 'call',0
STR_CLC     db 'clc',0
STR_CLD     db 'cld',0
STR_CMP     db 'cmp',0
STR_DEC     db 'dec',0
STR_DIV     db 'div',0
STR_DB      db 'db',0
STR_EQU     db 'equ',0
STR_DD      db 'dd',0
STR_DEFINE  db 'define',0
STR_DW      db 'dw',0
STR_INC     db 'inc',0
STR_INCLUDE db 'include',0
STR_INT     db 'int',0
STR_JA      db 'ja',0
STR_JAE     db 'jae',0
STR_JB      db 'jb',0
STR_JBE     db 'jbe',0
STR_JC      db 'jc',0
STR_JE      db 'je',0
STR_JG      db 'jg',0
STR_JGE     db 'jge',0
STR_JL      db 'jl',0
STR_JLE     db 'jle',0
STR_JMP     db 'jmp',0
STR_JNC     db 'jnc',0
STR_JNE     db 'jne',0
STR_JNS     db 'jns',0
STR_JNZ     db 'jnz',0
STR_JZ      db 'jz',0
STR_LODSB   db 'lodsb',0
STR_LOOP    db 'loop',0
STR_MOV     db 'mov',0
STR_MOVSB   db 'movsb',0
STR_MOVSW   db 'movsw',0
STR_MOVZX   db 'movzx',0
STR_MUL     db 'mul',0
STR_NEG     db 'neg',0
STR_OR      db 'or',0
STR_ORG     db 'org',0
STR_SHORT   db 'short',0
STR_POP     db 'pop',0
STR_PUSH    db 'push',0
STR_REP     db 'rep',0
STR_REPNE   db 'repne',0
STR_RET     db 'ret',0
STR_SBB     db 'sbb',0
STR_SCASB   db 'scasb',0
STR_SHL     db 'shl',0
STR_SHR     db 'shr',0
STR_STC     db 'stc',0
STR_STOSB   db 'stosb',0
STR_STOSW   db 'stosw',0
STR_SUB     db 'sub',0
STR_TEST    db 'test',0
STR_TIMES   db 'times',0
STR_WORD    db 'word',0
STR_XCHG    db 'xchg',0
STR_XOR     db 'xor',0

;;; Register table: 2-char name, reg number, size (8 or 16)
register_table:
        db 'al', 0, 8
        db 'cl', 1, 8
        db 'dl', 2, 8
        db 'bl', 3, 8
        db 'ah', 4, 8
        db 'ch', 5, 8
        db 'dh', 6, 8
        db 'bh', 7, 8
        db 'ax', 0, 16
        db 'cx', 1, 16
        db 'dx', 2, 16
        db 'bx', 3, 16
        db 'sp', 4, 16
        db 'bp', 5, 16
        db 'si', 6, 16
        db 'di', 7, 16
        db 0                   ; terminator

;;; -----------------------------------------------------------------------
;;; ES-safe kernel jump table wrappers
;;; -----------------------------------------------------------------------
call_die:
        push ds
        pop es
        jmp FUNCTION_DIE

call_exit:
        push ds
        pop es
        jmp FUNCTION_EXIT

call_print_character:
        push es
        push ds
        pop es
        call FUNCTION_PRINT_CHARACTER
        pop es
        ret

call_print_string:
        push es
        push ds
        pop es
        call FUNCTION_PRINT_STRING
        pop es
        ret

call_write_stdout:
        push es
        push ds
        pop es
        call FUNCTION_WRITE_STDOUT
        pop es
        ret

;;; -----------------------------------------------------------------------
;;; ES-safe syscall wrapper: save ES (symbol table segment), set ES=0
;;; for kernel calls, then restore ES before returning.
;;; -----------------------------------------------------------------------
syscall:
        push es
        push ds
        pop es                  ; ES=0 (kernel expects ES=0)
        int 30h
        pop es
        ret

;;; -----------------------------------------------------------------------
;;; Strings
;;; -----------------------------------------------------------------------
MESSAGE_ERROR_AT        db `  at: `
MESSAGE_ERROR_AT_LENGTH equ $ - MESSAGE_ERROR_AT
MESSAGE_ERROR_CREATE    db `Error: cannot create output\n`
MESSAGE_ERROR_CREATE_LENGTH equ $ - MESSAGE_ERROR_CREATE
MESSAGE_ERROR_FIND_OUT  db `Error: cannot find output file\n`
MESSAGE_ERROR_FIND_OUT_LENGTH equ $ - MESSAGE_ERROR_FIND_OUT
MESSAGE_ERROR_PASS1     db `Error: pass 1 failed\n`
MESSAGE_ERROR_PASS1_LENGTH equ $ - MESSAGE_ERROR_PASS1
MESSAGE_ERROR_PASS1_IO  db `Error: pass 1 io\n`
MESSAGE_ERROR_PASS1_IO_LENGTH equ $ - MESSAGE_ERROR_PASS1_IO
MESSAGE_ERROR_PASS1_ITER db `Error: pass 1 iter\n`
MESSAGE_ERROR_PASS1_ITER_LENGTH equ $ - MESSAGE_ERROR_PASS1_ITER
MESSAGE_ERROR_UNKNOWN   db `Error: unknown mnemonic or directive at line:\n  `
MESSAGE_ERROR_UNKNOWN_LENGTH equ $ - MESSAGE_ERROR_UNKNOWN
MESSAGE_ERROR_WRITE_DIR db `Error: directory write failed\n`
MESSAGE_ERROR_WRITE_DIR_LENGTH equ $ - MESSAGE_ERROR_WRITE_DIR
MESSAGE_OK      db `OK\n`
MESSAGE_OK_LENGTH equ $ - MESSAGE_OK
MESSAGE_SYMBOL_OVERFLOW db `Error: symbol table overflow (raise SYMBOL_MAX)\n`
MESSAGE_SYMBOL_OVERFLOW_LENGTH equ $ - MESSAGE_SYMBOL_OVERFLOW
MESSAGE_USAGE   db `Usage: asm <source> <output>\n`
MESSAGE_USAGE_LENGTH equ $ - MESSAGE_USAGE

;;; -----------------------------------------------------------------------
;;; Variables
;;; -----------------------------------------------------------------------
changed_flag  db 0
cmp_op1_size  db 0
current_address      dw 0
equ_space     dw 0
error_flag    db 0
global_scope  dw 0FFFFh
include_depth     db 0
include_path  times 32 db 0
iteration_count    dw 0
jump_index    dw 0
last_symbol_index  dw 0
op1_register  db 0
op1_size      db 0
op1_type      db 0
op1_value     dw 0
op2_register  db 0
op2_type      db 0
op2_value     dw 0
org_value     dw 0
output_fd        dw 0
output_name      dw 0
output_position  dw 0
output_total     dw 0
pass          db 0
source_buffer_position  dw 0
source_buffer_valid     dw 0
source_fd     dw 0
source_name   dw 0
source_prefix times 32 db 0
symbol_count  dw 0
symbol_set_scope  dw 0
symbol_set_value  dw 0

;;; -----------------------------------------------------------------------
;;; program_end: marks the end of the loaded image. Floating buffers
;;; (LINE_BUFFER, OUTPUT_BUFFER, SOURCE_BUFFER, INCLUDE_SAVE, INCLUDE_SOURCE_SAVE) are %defined
;;; relative to this label so they always sit immediately after the
;;; program in memory.
;;; -----------------------------------------------------------------------
program_end:
