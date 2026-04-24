        ;; Full ANSI escape sequence parser (stage 2)
        ;; put_character: unified output to screen (with ANSI parsing) and serial
        ;;
        ;; Supported escapes:
        ;;   ESC[nA        cursor up
        ;;   ESC[nC        cursor forward
        ;;   ESC[nD        cursor back
        ;;   ESC[r;cH      cursor position (1-indexed)
        ;;   ESC[0m        reset foreground to 7, background to 0
        ;;   ESC[38;5;Nm   256-color foreground (stored in ansi_fg)
        ;;   ESC[48;5;Nm   256-color background (palette via vga_set_bg)
        ;;   ESC[<N>@      write char code N at cursor, no advance or scroll

put_character:
        push eax
        push ebx
        push ecx
        push edx

        ;; Convert \n to \r\n
        cmp al, 0Ah
        jne .serial
        push eax
        mov al, 0Dh
        call serial_character
        call vga_teletype       ; CR on screen
        pop eax

.serial:
        call serial_character

        ;; State dispatch
        cmp byte [ansi_state], 2
        je .state_csi
        cmp byte [ansi_state], 1
        je .state_esc

        ;; STATE_NORMAL
        cmp al, 1Bh
        je .enter_esc
        call vga_teletype       ; uses [ansi_fg] as attribute
        jmp .done

.csi_command:
        mov byte [ansi_state], 0
        mov bx, [ansi_params]
        test bx, bx
        jnz .have_p1
        mov bx, 1              ; Default parameter is 1
.have_p1:
        cmp al, '@'
        je .write_char
        cmp al, 'A'
        je .cursor_up
        cmp al, 'C'
        je .cursor_forward
        cmp al, 'D'
        je .cursor_back
        cmp al, 'H'
        je .cursor_position
        cmp al, 'm'
        je .sgr
        jmp .done

.csi_next_param:
        ;; Advance to next param slot, clamped at +4 (3rd slot)
        mov ax, [ansi_param_index]
        cmp ax, 4
        jae .done
        add ax, 2
        mov [ansi_param_index], ax
        jmp .done

.cursor_back:
        push ebx
        call vga_get_cursor          ; DH=row, DL=col
        pop ecx                       ; ECX = backward count
        movzx eax, dh
        imul eax, eax, VGA_COLS
        movzx edx, dl
        add eax, edx                  ; EAX = linear position
        movzx ecx, cx
        sub eax, ecx
        xor edx, edx
        mov ecx, VGA_COLS
        div ecx                       ; EAX = row, EDX = col
        mov dh, al
        call vga_set_cursor
        jmp .done

.cursor_forward:
        push ebx
        call vga_get_cursor
        pop ecx
        add dl, cl
        call vga_set_cursor
        jmp .done

.cursor_position:
        ;; BX = row (default 1, 1-indexed)
        dec bx
        mov dh, bl
        mov bx, [ansi_params+2]
        test bx, bx
        jnz .cp_have_col
        mov bx, 1
.cp_have_col:
        dec bx
        mov dl, bl
        call vga_set_cursor
        jmp .done

.cursor_up:
        push ebx
        call vga_get_cursor     ; DH=row, DL=col
        pop ecx
        sub dh, cl
        jnb .cursor_up_set
        xor dh, dh
.cursor_up_set:
        call vga_set_cursor
        jmp .done

.done:
        pop edx
        pop ecx
        pop ebx
        pop eax
        ret

.enter_csi:
        mov byte [ansi_state], 2
        mov word [ansi_params], 0
        mov word [ansi_params+2], 0
        mov word [ansi_params+4], 0
        mov word [ansi_param_index], 0
        jmp .done

.enter_esc:
        mov byte [ansi_state], 1
        jmp .done

.sgr:
        ;; ESC[0m          reset: fg=7, bg=0
        ;; ESC[38;5;Nm     256-color fg (N stored in ansi_fg)
        ;; ESC[48;5;Nm     256-color bg (palette via vga_set_bg)
        mov ax, [ansi_params]
        test ax, ax
        jnz .sgr_fg_check
        ;; Reset
        mov byte [ansi_fg], 7
        xor al, al              ; overscan colour = 0
        call vga_set_bg
        jmp .done
.sgr_fg_check:
        cmp ax, 38
        jne .sgr_bg_check
        cmp word [ansi_params+2], 5
        jne .done
        mov ax, [ansi_params+4]
        mov [ansi_fg], al
        jmp .done
.sgr_bg_check:
        cmp ax, 48
        jne .done
        cmp word [ansi_params+2], 5
        jne .done
        mov ax, [ansi_params+4]
        call vga_set_bg         ; AL = colour
        jmp .done

.state_csi:
        cmp al, ';'
        je .csi_next_param
        cmp al, '0'
        jb .csi_command
        cmp al, '9'
        ja .csi_command
        ;; Accumulate digit into current slot
        sub al, '0'
        movzx ecx, al
        movzx ebx, word [ansi_param_index]
        add ebx, ansi_params
        mov ax, [ebx]
        imul ax, ax, 10
        add ax, cx
        mov [ebx], ax
        jmp .done

.state_esc:
        cmp al, '['
        je .enter_csi
        ;; Not CSI: emit ESC then char
        push eax
        mov al, 1Bh
        call vga_teletype
        pop eax
        call vga_teletype
        mov byte [ansi_state], 0
        jmp .done

.write_char:
        ;; ESC[<N>@ — write char code N at cursor with ansi_fg color,
        ;; no cursor advance and no scroll.  BX holds the decoded N.
        mov al, bl
        mov bl, [ansi_fg]
        call vga_write_attribute
        jmp .done

put_string:
        ;; Print null-terminated string at ESI via put_character (ANSI-aware).
        ;; Preserves EAX.
        push eax
.loop:
        mov al, [esi]
        inc esi
        test al, al
        jz .done
        call put_character
        jmp .loop
.done:
        pop eax
        ret

        ;; Parser state
        ansi_state db 0
        ansi_fg db 7
        ansi_params dw 0, 0, 0
        ansi_param_index dw 0
