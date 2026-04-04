        org 6000h

%include "constants.asm"

        ;; Memory layout
        %assign SYM_TABLE     0600h
        %assign SYM_ENTRY     24        ; bytes per symbol entry (20 name + 2 val + 1 type + 1 scope)
        %assign SYM_MAX       256       ; 256 * 24 = 6144 bytes (0x0600-0x1DFF)
        %assign SYM_NAME_LEN  20        ; 19 chars + null
        %assign LINE_BUF      1E00h
        %assign LINE_MAX      255
        %assign OUT_BUF       1F00h
        %assign SRC_BUF       2100h
        %assign INC_SAVE      2300h     ; include stack save area (10 bytes per level)
        %assign INC_SRC_SAVE  2340h     ; saved source buffer (512 bytes per level)

;;; -----------------------------------------------------------------------
;;; Main entry point
;;; -----------------------------------------------------------------------
main:
        cld
        ;; Parse arguments: "source output"
        mov si, [EXEC_ARG]
        test si, si
        jz .usage
        mov [src_name], si
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
        mov [out_name], si

        ;; -- Pass 1: collect labels, compute sizes --
        mov byte [pass], 1
        mov word [sym_count], 0
        mov word [org_value], 0
        mov word [cur_addr], 0
        mov word [global_scope], 0FFFFh
        call do_pass

        test byte [err_flag], 0FFh
        jnz .err_pass1

        ;; -- Create output file --
        mov si, [out_name]
        mov ah, SYS_FS_CREATE
        int 30h
        jc .err_create
        mov [out_start_sec], al

        ;; -- Pass 2: emit bytes --
        mov byte [pass], 2
        mov ax, [org_value]
        mov [cur_addr], ax
        mov word [global_scope], 0FFFFh
        mov al, [out_start_sec]
        mov [out_sector], al
        mov word [out_pos], 0
        mov word [out_total], 0
        call do_pass

        ;; Flush remaining output
        call flush_output

        ;; Update directory entry with file size and mark executable
        mov si, [out_name]
        mov ah, SYS_FS_FIND
        int 30h
        jc .err_find_out
        mov ax, [out_total]
        mov [bx+DIR_OFF_SIZE], ax
        mov byte [bx+DIR_OFF_FLAGS], FLAG_EXEC
        xor al, al                     ; AL=0 = write back directory
        mov ah, SYS_FS_WRITE
        int 30h
        jc .err_write_dir

        ;; Print success message
        mov si, MSG_OK
        mov ah, SYS_IO_PUTS
        int 30h
        mov ah, SYS_EXIT
        int 30h

        .usage:
        mov si, MSG_USAGE
        mov ah, SYS_IO_PUTS
        int 30h
        mov ah, SYS_EXIT
        int 30h
        .err_create:
        mov si, MSG_E_CREATE
        jmp .die
        .err_find_out:
        mov si, MSG_E_FIND_OUT
        jmp .die
        .err_pass1:
        mov si, MSG_E_PASS1
        jmp .die
        .err_write_dir:
        mov si, MSG_E_WRITE_DIR
        jmp .die
        .error:
        mov si, MSG_ERROR
        .die:
        mov ah, SYS_IO_PUTS
        int 30h
        mov ah, SYS_EXIT
        int 30h

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
        mov [cur_addr], ax

        ;; Open source file
        mov si, [src_name]
        mov ah, SYS_FS_FIND
        int 30h
        jc .pass_err
        mov ax, [bx+DIR_OFF_SIZE]
        mov [file_size], ax
        mov al, [bx+DIR_OFF_SECTOR]
        mov [file_start], al
        mov [file_cur_sec], al
        mov word [src_buf_pos], 0
        mov word [src_buf_valid], 0
        mov byte [inc_depth], 0
        mov word [global_scope], 0FFFFh

        .line_loop:
        call read_line
        jc .eof
        call parse_line
        jmp .line_loop

        .eof:
        ;; Check if we're in an include -- if so, pop and continue
        cmp byte [inc_depth], 0
        je .pass_done
        call include_pop
        jmp .line_loop

        .pass_done:
        pop di
        pop si
        pop dx
        pop cx
        pop bx
        pop ax
        ret

        .pass_err:
        mov byte [err_flag], 1
        jmp .pass_done

;;; -----------------------------------------------------------------------
;;; emit_byte_al: emit byte in AL
;;; -----------------------------------------------------------------------
emit_byte_al:
        cmp byte [pass], 2
        jne .count_only
        push bx
        mov bx, [out_pos]
        mov [OUT_BUF + bx], al
        inc bx
        mov [out_pos], bx
        cmp bx, 512
        jb .no_flush
        call flush_output
        .no_flush:
        pop bx
        .count_only:
        inc word [cur_addr]
        inc word [out_total]
        ret

;;; -----------------------------------------------------------------------
;;; emit_word_ax: emit 16-bit word in AX (little-endian)
;;; -----------------------------------------------------------------------
emit_word_ax:
        push ax
        call emit_byte_al
        pop ax
        xchg al, ah
        call emit_byte_al
        ret

;;; -----------------------------------------------------------------------
;;; encode_rel8_jump: AL = opcode, SI points to label name
;;; -----------------------------------------------------------------------
encode_rel8_jump:
        push ax
        call emit_byte_al      ; emit opcode
        call skip_ws
        ;; Resolve target address
        call resolve_label     ; AX = target address
        ;; Compute rel8 = target - (cur_addr + 1)
        mov bx, [cur_addr]
        inc bx                 ; cur_addr will be past the rel8 byte
        sub ax, bx             ; AX = relative offset
        call emit_byte_al
        pop ax
        ret

;;; -----------------------------------------------------------------------
;;; flush_output: write OUT_BUF to disk
;;; -----------------------------------------------------------------------
flush_output:
        push ax
        push cx
        push si
        push di
        ;; Don't flush if nothing to write
        cmp word [out_pos], 0
        je .fl_done
        ;; Zero-pad remainder
        mov di, OUT_BUF
        add di, [out_pos]
        mov cx, 512
        sub cx, [out_pos]
        jz .fl_write
        xor al, al
        rep stosb
        .fl_write:
        ;; Copy OUT_BUF to DISK_BUFFER
        mov si, OUT_BUF
        mov di, DISK_BUFFER
        mov cx, 256
        cld
        rep movsw
        ;; Write sector
        mov al, [out_sector]
        mov ah, SYS_FS_WRITE
        int 30h
        inc byte [out_sector]
        mov word [out_pos], 0
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
        call emit_byte_al
        ret

;;; -----------------------------------------------------------------------
;;; handle_add: add r, imm
;;; -----------------------------------------------------------------------
handle_add:
        call skip_ws
        call parse_register    ; AL = reg, AH = size
        push ax
        call skip_comma
        call resolve_value     ; AX = immediate
        mov cx, ax
        pop bx                 ; BL = reg, BH = size
        cmp bh, 8
        je .add_r8
        ;; add r16, imm: short forms
        test bl, bl
        jnz .add_r16_general
        ;; AX short form: 05 imm16
        mov al, 05h
        call emit_byte_al
        mov ax, cx
        call emit_word_ax
        ret
        .add_r16_general:
        ;; Use 83h if imm fits in signed byte
        cmp cx, 127
        ja .add_r16_full
        mov al, 83h
        call emit_byte_al
        mov al, bl
        or al, 0C0h
        call emit_byte_al
        mov al, cl
        call emit_byte_al
        ret
        .add_r16_full:
        mov al, 81h
        call emit_byte_al
        mov al, bl
        or al, 0C0h
        call emit_byte_al
        mov ax, cx
        call emit_word_ax
        ret
        .add_r8:
        ;; add r8, imm8. Short form for AL: 04 imm8
        test bl, bl
        jnz .add_r8_general
        mov al, 04h
        call emit_byte_al
        mov al, cl
        call emit_byte_al
        ret
        .add_r8_general:
        mov al, 80h
        call emit_byte_al
        mov al, bl
        or al, 0C0h
        call emit_byte_al
        mov al, cl
        call emit_byte_al
        ret

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
        call emit_word_ax
        ret
        .and_r8:
        ;; and r8, imm8. Short form for AL: 24 imm8
        test bl, bl
        jnz .and_r8_general
        mov al, 24h
        call emit_byte_al
        mov al, cl
        call emit_byte_al
        ret
        .and_r8_general:
        mov al, 80h
        call emit_byte_al
        mov al, bl
        or al, 0E0h
        call emit_byte_al
        mov al, cl
        call emit_byte_al
        ret

;;; -----------------------------------------------------------------------
;;; handle_call: call near label
;;; -----------------------------------------------------------------------
handle_call:
        call skip_ws
        ;; Emit E8 rel16
        mov al, 0E8h
        call emit_byte_al
        call resolve_label     ; AX = target address
        ;; rel16 = target - (cur_addr + 2) (2 bytes for the rel16 itself)
        mov bx, [cur_addr]
        add bx, 2
        sub ax, bx
        call emit_word_ax
        ret

;;; -----------------------------------------------------------------------
;;; handle_cld
;;; -----------------------------------------------------------------------
handle_cld:
        mov al, 0FCh
        call emit_byte_al
        ret

;;; -----------------------------------------------------------------------
;;; handle_cmp
;;; -----------------------------------------------------------------------
handle_cmp:
        call skip_ws
        call parse_operand     ; AH=type, AL=reg, DX=val
        mov [op1_type], ah
        mov [op1_reg], al
        mov [op1_val], dx
        call skip_comma
        call resolve_value
        mov cx, ax             ; CX = immediate
        ;; Check operand type
        cmp byte [op1_type], 3
        je .cmp_mem
        cmp byte [op1_type], 0
        jne .cmp_done
        ;; cmp reg, imm
        mov bl, [op1_reg]
        cmp byte [op1_size], 8
        je .cmp_r8
        ;; cmp r16, imm: use 83h if fits in byte
        cmp cx, 127
        ja .cmp_r16_full
        mov al, 83h
        call emit_byte_al
        mov al, bl
        or al, 0F8h
        call emit_byte_al
        mov al, cl
        call emit_byte_al
        ret
        .cmp_r16_full:
        mov al, 81h
        call emit_byte_al
        mov al, bl
        or al, 0F8h
        call emit_byte_al
        mov ax, cx
        call emit_word_ax
        ret
        .cmp_r8:
        ;; Short form for AL: 3C imm8
        test bl, bl
        jnz .cmp_r8_general
        mov al, 3Ch
        call emit_byte_al
        mov al, cl
        call emit_byte_al
        ret
        .cmp_r8_general:
        mov al, 80h
        call emit_byte_al
        mov al, bl
        or al, 0F8h
        call emit_byte_al
        mov al, cl
        call emit_byte_al
        ret
        .cmp_mem:
        ;; cmp byte [reg], imm8 or cmp byte [reg+disp], imm8
        mov al, 80h
        call emit_byte_al
        ;; Build modrm: /7 with memory addressing
        mov al, [op1_reg]
        call reg_to_rm
        or al, 38h             ; /7 = 38h in reg field
        cmp word [op1_val], 0
        jne .cmp_mem_disp
        call emit_byte_al      ; mod=00
        jmp .cmp_mem_imm
        .cmp_mem_disp:
        or al, 40h             ; mod=01
        call emit_byte_al
        mov ax, [op1_val]
        call emit_byte_al      ; disp8
        .cmp_mem_imm:
        mov al, cl
        call emit_byte_al
        .cmp_done:
        ret

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
        call emit_byte_al
        ret

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
        call emit_byte_al
        ret
        .inc_r8:
        ;; inc r8: FE /0 modrm(mod=11, /0, rm=reg)
        push ax
        mov al, 0FEh
        call emit_byte_al
        pop ax
        or al, 0C0h
        call emit_byte_al
        ret
        .inc_mem:
        ;; inc byte [disp16]: FE 06 disp16
        ;; inc byte [reg]: FE /0 modrm
        mov al, 0FEh
        call emit_byte_al
        cmp ah, 2              ; OP_MEM_DIRECT
        je .inc_mem_direct
        ;; [reg] or [reg+disp]
        push dx
        mov al, [op1_reg]
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
        call emit_byte_al
        ret
        .inc_mem_direct:
        mov al, 06h            ; modrm: mod=00, /0, rm=110
        call emit_byte_al
        mov ax, dx             ; DX = disp16 from parse_operand
        call emit_word_ax
        ret

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
        call emit_byte_al
        ret

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
;;; handle_je (alias for jz)
;;; -----------------------------------------------------------------------
handle_je:
        mov al, 74h
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
        call emit_byte_al
        ret

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
        ;; Parse destination
        call parse_operand     ; Returns: type in AH, value in DX, reg in AL
        mov [op1_type], ah
        mov [op1_reg], al
        mov [op1_val], dx

        call skip_comma

        ;; Parse source
        call parse_operand
        mov [op2_type], ah
        mov [op2_reg], al
        mov [op2_val], dx

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
        jmp .mov_done

        .mov_rr:
        ;; mov r, r -- use opcode 88 (8-bit) or 89 (16-bit)
        ;; NASM encodes as: opcode modrm where reg=src, rm=dst
        mov al, [op1_reg]      ; dst reg
        mov bl, al
        mov al, [op2_reg]      ; src reg
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
        mov al, [op1_reg]
        cmp byte [op1_size], 8
        je .mov_ri8
        ;; 16-bit: B8+reg, imm16
        add al, 0B8h
        call emit_byte_al
        mov ax, [op2_val]
        call emit_word_ax
        jmp .mov_done
        .mov_ri8:
        ;; 8-bit: B0+reg, imm8
        add al, 0B0h
        call emit_byte_al
        mov al, [op2_val]
        call emit_byte_al
        jmp .mov_done

        .mov_rm_direct:
        ;; mov r, [disp16]: short form A0/A1 for AL/AX, else 8B/8A
        cmp byte [op1_size], 8
        je .mov_rm_d8
        ;; 16-bit: short form A1 for AX
        cmp byte [op1_reg], 0
        jne .mov_rm_d16_general
        mov al, 0A1h
        call emit_byte_al
        mov ax, [op2_val]
        call emit_word_ax
        jmp .mov_done
        .mov_rm_d16_general:
        mov al, 8Bh
        jmp .mov_rm_d_emit
        .mov_rm_d8:
        ;; 8-bit: short form A0 for AL
        cmp byte [op1_reg], 0
        jne .mov_rm_d8_general
        mov al, 0A0h
        call emit_byte_al
        mov ax, [op2_val]
        call emit_word_ax
        jmp .mov_done
        .mov_rm_d8_general:
        mov al, 8Ah
        .mov_rm_d_emit:
        call emit_byte_al
        ;; modrm = (0 << 6) | (reg << 3) | 6 = (reg << 3) | 6
        mov al, [op1_reg]
        shl al, 3
        or al, 06h
        call emit_byte_al
        mov ax, [op2_val]
        call emit_word_ax
        jmp .mov_done

        .mov_rm_bx_disp:
        ;; mov r, [reg+disp8]: 8B (16-bit) or 8A (8-bit)
        cmp byte [op1_size], 8
        je .mov_rm_bx8
        mov al, 8Bh
        jmp .mov_rm_bx_emit
        .mov_rm_bx8:
        mov al, 8Ah
        .mov_rm_bx_emit:
        call emit_byte_al
        ;; Get rm field from addressing register
        mov al, [op2_reg]
        call reg_to_rm
        mov bl, al
        mov al, [op1_reg]
        shl al, 3
        or al, bl
        ;; mod=00 if no displacement, mod=01 if disp8
        cmp word [op2_val], 0
        jne .mov_rm_with_disp
        call emit_byte_al
        jmp .mov_done
        .mov_rm_with_disp:
        or al, 40h             ; mod=01
        call emit_byte_al
        mov ax, [op2_val]
        call emit_byte_al      ; disp8
        jmp .mov_done

        .mov_mem_dst:
        ;; mov [reg], imm: C6 /0 modrm imm8 (byte) or C7 /0 modrm imm16
        cmp byte [op2_type], 1
        jne .mov_done
        mov al, [op1_reg]
        call reg_to_rm
        cmp byte [op1_size], 8
        je .mov_mem_dst8
        push ax
        mov al, 0C7h
        call emit_byte_al
        pop ax
        call emit_byte_al
        mov ax, [op2_val]
        call emit_word_ax
        jmp .mov_done
        .mov_mem_dst8:
        push ax
        mov al, 0C6h
        call emit_byte_al
        pop ax
        call emit_byte_al
        mov al, [op2_val]
        call emit_byte_al
        jmp .mov_done

        .mov_direct_dst:
        ;; mov [disp16], imm: C6 06 disp16 imm8 (byte) or C7 06 disp16 imm16
        cmp byte [op2_type], 1
        jne .mov_done
        cmp byte [op1_size], 8
        je .mov_dd8
        mov al, 0C7h
        call emit_byte_al
        mov al, 06h
        call emit_byte_al
        mov ax, [op1_val]
        call emit_word_ax
        mov ax, [op2_val]
        call emit_word_ax
        jmp .mov_done
        .mov_dd8:
        mov al, 0C6h
        call emit_byte_al
        mov al, 06h
        call emit_byte_al
        mov ax, [op1_val]
        call emit_word_ax
        mov al, [op2_val]
        call emit_byte_al
        jmp .mov_done

        .mov_done:
        ret

;;; -----------------------------------------------------------------------
;;; handle_pop: pop r16
;;; -----------------------------------------------------------------------
handle_pop:
        call skip_ws
        call parse_register    ; AL = reg
        add al, 58h            ; 58+reg
        call emit_byte_al
        ret

;;; -----------------------------------------------------------------------
;;; handle_push: push r16
;;; -----------------------------------------------------------------------
handle_push:
        call skip_ws
        call parse_register    ; AL = reg
        add al, 50h            ; 50+reg
        call emit_byte_al
        ret

;;; -----------------------------------------------------------------------
;;; handle_ret
;;; -----------------------------------------------------------------------
handle_ret:
        mov al, 0C3h
        call emit_byte_al
        ret

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
        call emit_byte_al
        ret

;;; -----------------------------------------------------------------------
;;; handle_sub
;;; -----------------------------------------------------------------------
handle_sub:
        call skip_ws
        call parse_register    ; AL = reg, AH = size
        push ax
        call skip_comma
        call resolve_value
        mov cx, ax
        pop bx                 ; BL = reg, BH = size
        cmp bh, 8
        je .sub_r8
        ;; sub r16, imm: use 83h short form if imm fits in byte
        cmp cx, 127
        ja .sub_r16_full
        mov al, 83h
        call emit_byte_al
        mov al, bl
        or al, 0E8h
        call emit_byte_al
        mov al, cl
        call emit_byte_al
        ret
        .sub_r16_full:
        mov al, 81h
        call emit_byte_al
        mov al, bl
        or al, 0E8h
        call emit_byte_al
        mov ax, cx
        call emit_word_ax
        ret
        .sub_r8:
        ;; sub r8, imm8. Short form for AL: 2C imm8
        test bl, bl
        jnz .sub_r8_general
        mov al, 2Ch
        call emit_byte_al
        mov al, cl
        call emit_byte_al
        ret
        .sub_r8_general:
        mov al, 80h
        call emit_byte_al
        mov al, bl
        or al, 0E8h
        call emit_byte_al
        mov al, cl
        call emit_byte_al
        ret

;;; -----------------------------------------------------------------------
;;; handle_test
;;; -----------------------------------------------------------------------
handle_test:
        call skip_ws
        call parse_operand     ; AH=type, AL=reg, DX=val
        mov [op1_type], ah
        mov [op1_reg], al
        mov [op1_val], dx
        call skip_comma
        cmp byte [op1_type], 0
        jne .test_mem
        ;; test r, r: second operand is register
        call parse_register
        mov bl, [op1_reg]      ; BL = dst reg
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
        call emit_byte_al
        ret
        .test_mem:
        ;; test byte [reg+disp], imm8: F6 modrm [disp] imm8
        call resolve_value     ; AX = immediate
        push ax
        mov al, 0F6h
        call emit_byte_al
        ;; modrm: /0 with memory addressing
        mov al, [op1_reg]
        call reg_to_rm         ; AL = rm
        cmp word [op1_val], 0
        jne .test_mem_disp
        call emit_byte_al      ; mod=00, /0, rm
        jmp .test_mem_imm
        .test_mem_disp:
        or al, 40h             ; mod=01
        call emit_byte_al
        mov ax, [op1_val]
        call emit_byte_al      ; disp8
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
        call emit_byte_al
        ret

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
        call emit_byte_al
        ret

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
        mov ax, [cur_addr]
        cmp byte [di], '.'
        je .bare_local
        mov bx, 0FFFFh
        call sym_add
        mov ax, [sym_count]
        dec ax
        mov [global_scope], ax
        jmp .bare_added
        .bare_local:
        mov bx, [global_scope]
        call sym_add
        .bare_added:
        pop si
        jmp .bare_continue
        .bare_pass2:
        cmp byte [di], '.'
        je .bare_continue
        push si
        mov si, di
        mov bx, 0FFFFh
        call sym_lookup
        jc .bare_no_scope
        mov ax, [last_sym_idx]
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
        ;; Restore from INC_SAVE
        mov bx, INC_SAVE
        mov al, [bx]
        mov [file_start], al
        mov al, [bx+1]
        mov [file_cur_sec], al
        mov ax, [bx+2]
        mov [file_size], ax
        mov ax, [bx+4]
        mov [src_buf_pos], ax
        mov ax, [bx+6]
        mov [src_buf_valid], ax
        ;; Restore SRC_BUF
        mov si, INC_SRC_SAVE
        mov di, SRC_BUF
        mov cx, 256
        cld
        rep movsw
        dec byte [inc_depth]
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
        ;; Save current file state to INC_SAVE
        mov bx, INC_SAVE
        mov al, [file_start]
        mov [bx], al
        mov al, [file_cur_sec]
        mov [bx+1], al
        mov ax, [file_size]
        mov [bx+2], ax
        mov ax, [src_buf_pos]
        mov [bx+4], ax
        mov ax, [src_buf_valid]
        mov [bx+6], ax
        ;; Save SRC_BUF content
        push si
        push di
        push cx
        mov si, SRC_BUF
        mov di, INC_SRC_SAVE
        mov cx, 256
        cld
        rep movsw
        pop cx
        pop di
        pop si
        ;; Open included file
        mov ah, SYS_FS_FIND
        int 30h
        jc .inc_err
        mov ax, [bx+DIR_OFF_SIZE]
        mov [file_size], ax
        mov al, [bx+DIR_OFF_SECTOR]
        mov [file_start], al
        mov [file_cur_sec], al
        mov word [src_buf_pos], 0
        mov word [src_buf_valid], 0
        inc byte [inc_depth]
        pop bx
        pop ax
        ret
        .inc_err:
        mov byte [err_flag], 1
        pop bx
        pop ax
        ret

;;; -----------------------------------------------------------------------
;;; load_src_sector: read next sector of source file into SRC_BUF
;;; Returns CF if no more data
;;; -----------------------------------------------------------------------
load_src_sector:
        push bx
        push cx
        push si
        push di
        ;; Compute bytes already consumed
        ;; sectors_read = file_cur_sec - file_start
        mov al, [file_cur_sec]
        sub al, [file_start]
        xor ah, ah
        ;; bytes_consumed = sectors_read * 512
        mov cl, 9
        shl ax, cl
        ;; remaining = file_size - bytes_consumed
        mov bx, [file_size]
        sub bx, ax
        jbe .no_more
        ;; valid = min(remaining, 512)
        cmp bx, 512
        jbe .set_valid
        mov bx, 512
        .set_valid:
        ;; Read sector
        mov al, [file_cur_sec]
        mov ah, SYS_FS_READ
        int 30h
        jc .no_more
        ;; Copy DISK_BUFFER to SRC_BUF
        push bx
        mov si, DISK_BUFFER
        mov di, SRC_BUF
        mov cx, 256
        cld
        rep movsw
        pop bx
        mov [src_buf_valid], bx
        mov word [src_buf_pos], 0
        inc byte [file_cur_sec]
        clc
        pop di
        pop si
        pop cx
        pop bx
        ret
        .no_more:
        stc
        pop di
        pop si
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

        ;; Numeric/constant value
        call resolve_value     ; AX = value
        call emit_byte_al
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

        ;; Check %assign
        mov di, STR_ASSIGN
        call match_word
        jc .try_include
        call skip_ws
        ;; Parse name
        mov di, si             ; DI = start of name
        .skip_name:
        cmp byte [si], ' '
        je .got_name
        cmp byte [si], 9
        je .got_name
        cmp byte [si], 0
        je .pd_done
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
        call sym_add_const
        pop si
        jmp .pd_done

        .try_include:
        ;; Check %include
        mov di, STR_INCLUDE
        call match_word
        jc .pd_done
        call skip_ws
        ;; Parse filename: expect "filename" or `filename`
        cmp byte [si], '"'
        je .inc_quote
        jmp .pd_done
        .inc_quote:
        inc si                 ; skip opening quote
        mov di, si
        .find_close_quote:
        cmp byte [si], '"'
        je .got_inc_name
        cmp byte [si], 0
        je .pd_done
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
        jc .try_db
        call skip_ws
        call resolve_value     ; AX = value
        mov [org_value], ax
        mov [cur_addr], ax
        jmp .pd_done

        .try_db:
        ;; Check 'db'
        mov di, STR_DB
        call match_word
        jc .try_mnemonic
        call skip_ws
        call parse_db
        jmp .pd_done

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
;;; parse_line: parse one source line from LINE_BUF
;;; -----------------------------------------------------------------------
parse_line:
        push ax
        push bx
        push cx
        push dx
        push si
        push di

        mov si, LINE_BUF
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
        je .no_label
        cmp al, 9
        je .no_label
        inc si
        jmp .scan_colon

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
        mov ax, [cur_addr]
        mov bx, 0FFFFh         ; scope = global
        call sym_add
        ;; Update global_scope to this symbol's index
        mov ax, [sym_count]
        dec ax
        mov [global_scope], ax
        pop di
        jmp .skip_add_label
        .local_label:
        mov si, di
        mov ax, [cur_addr]
        mov bx, [global_scope]
        call sym_add
        .skip_add_label:
        ;; If pass 2, update global_scope for global labels
        cmp byte [pass], 2
        jne .after_label
        cmp byte [di], '.'
        je .after_label
        ;; Find this global label in symbol table to get its index
        mov si, di
        mov bx, 0FFFFh
        call sym_lookup
        jc .after_label
        mov ax, [last_sym_idx]
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
        ;; 'byte' before a register (e.g., "mov byte bl, [var]") -- just a hint
        jmp .try_register
        .no_byte_prefix:
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
        ;; Check if starts with a register name (bx, si, di, bp)
        push si
        call parse_register
        jnc .mem_with_reg
        pop si
        ;; Direct memory: [number_or_symbol]
        call resolve_value
        mov dx, ax
        ;; Find closing ']'
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
        ;; AL = register, check for +disp
        call skip_ws
        cmp byte [si], '+'
        jne .mem_reg_only
        inc si                 ; skip '+'
        call skip_ws
        push ax
        call resolve_value     ; AX = displacement
        mov dx, ax
        pop ax
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
        mov bx, reg_table
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
        mov ah, SYS_IO_PUTC
        int 30h
        ret

;;; -----------------------------------------------------------------------
;;; read_line: read one line from source into LINE_BUF
;;; Returns CF set on EOF
;;; -----------------------------------------------------------------------
read_line:
        push bx
        push cx
        push dx
        mov di, LINE_BUF
        xor cx, cx             ; CX = chars in line

        .next_byte:
        ;; Check if source buffer needs refill
        mov ax, [src_buf_pos]
        cmp ax, [src_buf_valid]
        jb .have_byte

        ;; Need more data -- is file exhausted?
        call load_src_sector
        jc .check_eof

        .have_byte:
        mov bx, [src_buf_pos]
        mov al, [SRC_BUF + bx]
        inc word [src_buf_pos]

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
;;; On pass 1, returns cur_addr (placeholder)
;;; -----------------------------------------------------------------------
resolve_label:
        cmp byte [pass], 1
        je .pass1
        call resolve_value
        ret
        .pass1:
        ;; Skip past label name, return cur_addr as placeholder
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
        mov ax, [cur_addr]
        ret

;;; -----------------------------------------------------------------------
;;; resolve_value: parse number or look up symbol at SI
;;; Returns value in AX, advances SI past the token
;;; -----------------------------------------------------------------------
resolve_value:
        push bx
        push cx
        call skip_ws
        ;; Check for character literal: 'c' or `\n`
        cmp byte [si], 27h    ; single quote
        je .char_literal
        cmp byte [si], '`'
        je .backtick_literal
        ;; Check if starts with digit -- it's a number
        mov al, [si]
        cmp al, '0'
        jb .try_symbol
        cmp al, '9'
        ja .try_symbol
        call parse_number
        pop cx
        pop bx
        ret

        .char_literal:
        inc si                 ; skip opening quote
        xor ah, ah
        mov al, [si]           ; AL = character value
        inc si
        cmp byte [si], 27h    ; closing quote
        jne .char_done
        inc si                 ; skip closing quote
        .char_done:
        pop cx
        pop bx
        ret

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
        jne .bt_done
        inc si                 ; skip closing backtick
        .bt_done:
        pop cx
        pop bx
        ret

        .try_symbol:
        ;; Read identifier and look up in symbol table
        push di
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
        call sym_lookup        ; AX = value
        pop si
        pop cx
        mov [si], cl           ; restore delimiter
        ;; Check for +/- arithmetic after symbol
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
;;; sym_add: add label to symbol table
;;; SI = name, AX = value, BX = scope (0xFFFF = global)
;;; -----------------------------------------------------------------------
sym_add:
        ;; SI = name, AX = value, BX = scope
        push cx
        push di
        push si
        ;; Compute entry address: SYM_TABLE + sym_count * SYM_ENTRY
        push ax                ; save value
        push bx                ; save scope
        mov ax, [sym_count]
        call sym_entry_addr    ; DI = entry address
        ;; Copy name (up to SYM_NAME_LEN-1 chars + null)
        mov cx, SYM_NAME_LEN - 1
        .copy_sym_name:
        mov al, [si]
        test al, al
        jz .pad_name
        mov [di], al
        inc si
        inc di
        dec cx
        jnz .copy_sym_name
        .pad_name:
        mov byte [di], 0
        inc di
        dec cx
        jns .pad_name
        ;; Re-derive entry base for metadata
        mov ax, [sym_count]
        call sym_entry_addr    ; DI = entry address
        ;; Write metadata at offset SYM_NAME_LEN
        pop bx                 ; restore scope
        pop ax                 ; restore value
        mov [di+SYM_NAME_LEN], ax     ; value
        mov byte [di+SYM_NAME_LEN+2], 0 ; type = label
        mov [di+SYM_NAME_LEN+3], bl   ; scope
        inc word [sym_count]
        pop si
        pop di
        pop cx
        ret

;;; -----------------------------------------------------------------------
;;; sym_add_const: add constant (%assign) to symbol table
;;; SI = name, AX = value
;;; -----------------------------------------------------------------------
sym_add_const:
        push bx
        mov bx, 0FFFFh
        call sym_add
        ;; Fix type to 1 (constant)
        push ax
        mov ax, [sym_count]
        dec ax
        call sym_entry_addr    ; DI = entry address
        mov byte [di+SYM_NAME_LEN+2], 1
        pop ax
        pop bx
        ret

;;; -----------------------------------------------------------------------
;;; sym_entry_addr: compute symbol table entry address
;;; AX = index, returns DI = SYM_TABLE + index * SYM_ENTRY
;;; Clobbers AX, DX
;;; -----------------------------------------------------------------------
sym_entry_addr:
        push bx
        mov bx, SYM_ENTRY
        mul bx                 ; AX = index * SYM_ENTRY (DX clobbered)
        mov di, ax
        add di, SYM_TABLE
        pop bx
        ret

;;; -----------------------------------------------------------------------
;;; sym_lookup: find symbol by name
;;; SI = name (null-terminated), BX = scope (0xFFFF for global search)
;;; Returns: AX = value, CF clear if found; CF set if not found
;;; -----------------------------------------------------------------------
sym_lookup:
        push cx
        push dx
        push di
        mov cx, [sym_count]
        test cx, cx
        jz .sym_not_found
        mov di, SYM_TABLE
        xor dx, dx             ; DX = index
        .sym_search:
        ;; Compare names
        push si
        push di
        push cx
        .cmp_name:
        mov al, [si]
        cmp al, [di]
        jne .sym_no_match
        test al, al
        jz .sym_name_match
        inc si
        inc di
        jmp .cmp_name
        .sym_name_match:
        pop cx
        pop di
        pop si
        ;; Name matches -- found
        .sym_found:
        mov ax, [di+SYM_NAME_LEN]
        mov [last_sym_idx], dx
        clc
        pop di
        pop dx
        pop cx
        ret
        .sym_no_match:
        pop cx
        pop di
        pop si
        .sym_next:
        add di, SYM_ENTRY
        inc dx
        loop .sym_search
        .sym_not_found:
        ;; Return 0 on pass 1 (symbol not yet defined)
        xor ax, ax
        cmp byte [pass], 1
        je .sym_pass1_ok
        stc
        pop di
        pop dx
        pop cx
        ret
        .sym_pass1_ok:
        clc
        pop di
        pop dx
        pop cx
        ret


;;; -----------------------------------------------------------------------
;;; Mnemonic table: pairs of (name_ptr, handler_ptr), terminated by 0
;;; -----------------------------------------------------------------------
mnemonic_table:
        dw STR_AAM, handle_aam
        dw STR_ADD, handle_add
        dw STR_AND, handle_and
        dw STR_CALL, handle_call
        dw STR_CLD, handle_cld
        dw STR_CMP, handle_cmp
        dw STR_DIV, handle_div
        dw STR_INC, handle_inc
        dw STR_INT, handle_int
        dw STR_JA,  handle_ja
        dw STR_JB,  handle_jb
        dw STR_JBE, handle_jbe
        dw STR_JC,  handle_jc
        dw STR_JE,  handle_je
        dw STR_JMP, handle_jmp
        dw STR_JNC, handle_jnc
        dw STR_JNE, handle_jne
        dw STR_JNZ, handle_jne
        dw STR_JZ,  handle_jz
        dw STR_LODSB, handle_lodsb
        dw STR_LOOP, handle_loop
        dw STR_MOV, handle_mov
        dw STR_POP, handle_pop
        dw STR_PUSH, handle_push
        dw STR_RET, handle_ret
        dw STR_SHR, handle_shr
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
STR_CLD     db 'cld',0
STR_CMP     db 'cmp',0
STR_DB      db 'db',0
STR_DIV     db 'div',0
STR_INC     db 'inc',0
STR_INCLUDE db 'include',0
STR_INT     db 'int',0
STR_JA      db 'ja',0
STR_JB      db 'jb',0
STR_JBE     db 'jbe',0
STR_JC      db 'jc',0
STR_JE      db 'je',0
STR_JMP     db 'jmp',0
STR_JNC     db 'jnc',0
STR_JNE     db 'jne',0
STR_JNZ     db 'jnz',0
STR_JZ      db 'jz',0
STR_LODSB   db 'lodsb',0
STR_LOOP    db 'loop',0
STR_MOV     db 'mov',0
STR_ORG     db 'org',0
STR_POP     db 'pop',0
STR_PUSH    db 'push',0
STR_RET     db 'ret',0
STR_SHORT   db 'short',0
STR_SHR     db 'shr',0
STR_SUB     db 'sub',0
STR_TEST    db 'test',0
STR_XCHG    db 'xchg',0
STR_XOR     db 'xor',0

;;; Register table: 2-char name, reg number, size (8 or 16)
reg_table:
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
;;; Strings
;;; -----------------------------------------------------------------------
MSG_E_CREATE    db `Error: cannot create output\n\0`
MSG_E_FIND_OUT  db `Error: cannot find output file\n\0`
MSG_E_PASS1     db `Error: pass 1 failed\n\0`
MSG_E_WRITE_DIR db `Error: directory write failed\n\0`
MSG_ERROR       db `Error\n\0`
MSG_OK      db `OK\n\0`
MSG_USAGE   db `Usage: asm <source> <output>\n\0`

;;; -----------------------------------------------------------------------
;;; Variables
;;; -----------------------------------------------------------------------
cur_addr      dw 0
err_flag      db 0
file_cur_sec  db 0
file_size     dw 0
file_start    db 0
global_scope  dw 0FFFFh
inc_depth     db 0
last_sym_idx  dw 0
op1_reg       db 0
op1_size      db 0
op1_type      db 0
op1_val       dw 0
op2_reg       db 0
op2_type      db 0
op2_val       dw 0
org_value     dw 0
out_name      dw 0
out_pos       dw 0
out_sector    db 0
out_start_sec db 0
out_total     dw 0
pass          db 0
src_buf_pos   dw 0
src_buf_valid dw 0
src_name      dw 0
sym_count     dw 0
