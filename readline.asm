cursor_back_n:
        ;; Move cursor back by BX positions
        test bx, bx
        jz .done
        push ax
        push bx
        push cx
        push dx
        push bx                 ; Save count on stack
        mov ah, 03h
        xor bx, bx
        int 10h                 ; DH=row, DL=col (CX clobbered with cursor shape)
        pop cx                  ; Restore count
        movzx ax, dh
        push dx
        mov bx, 80
        mul bx                  ; AX = row * 80
        pop dx
        movzx bx, dl
        add ax, bx              ; AX = linear position
        sub ax, cx              ; AX = new linear position
        xor dx, dx
        mov bx, 80
        div bx                  ; AX = new row, DX = new col
        mov dh, al
        mov ah, 02h
        xor bx, bx
        int 10h                 ; Set cursor position
        pop dx
        pop cx
        pop bx
        pop ax
        .done:
        ret

read_line:
        push ax
        push bx
        push dx
        mov cx, buffer          ; Cursor position
        mov dx, buffer          ; End of buffer

        .read_char:
        mov ah, 00h             ; int 16h 'keyboard read' function
        int 16h                 ; Call 'keyboard read' function

        cmp al, 0               ; Extended key
        je .extended_key
        cmp al, 0E0h            ; Extended key (alternate)
        je .extended_key
        cmp al, 01h             ; Ctrl+A — beginning of line
        je .ctrl_a
        cmp al, 02h             ; Ctrl+B — cursor left
        je .cursor_left
        cmp al, 03h             ; Ctrl+C — cancel line
        je .ctrl_c
        cmp al, 04h             ; Ctrl+D — shutdown
        je .ctrl_d
        cmp al, 05h             ; Ctrl+E — end of line
        je .ctrl_e
        cmp al, 06h             ; Ctrl+F — cursor right
        je .cursor_right
        cmp al, `\b`            ; Backspace
        je .backspace
        cmp al, 0Bh             ; Ctrl+K — kill to end of line
        je .ctrl_k
        cmp al, 0Ch             ; Ctrl+L — clear screen
        je .ctrl_l
        cmp al, `\r`            ; Enter
        je .end
        cmp al, 19h             ; Ctrl+Y — yank from kill buffer
        je .ctrl_y
        cmp al, 20h             ; Ignore other control characters
        jl .read_char

        call serial_char        ; Echo to COM1
        call .insert_char       ; Insert character at cursor
        jnc .read_char
        call visual_bell
        jmp .read_char

        .extended_key:
        cmp ah, 4Bh             ; Left arrow
        je .cursor_left
        cmp ah, 4Dh             ; Right arrow
        je .cursor_right
        cmp ah, 53h             ; Delete
        je .delete
        jmp .read_char          ; Ignore other extended keys

        .cursor_left:
        cmp cx, buffer
        je .read_char
        dec cx
        mov bx, 1
        call cursor_back_n
        jmp .read_char

        .cursor_right:
        cmp cx, dx
        je .read_char
        mov ah, 0Eh
        xor bx, bx
        mov bx, cx
        mov al, [bx]            ; Print char under cursor to advance
        int 10h
        inc cx
        jmp .read_char

        .backspace:
        cmp cx, buffer
        je .read_char
        push ax
        mov al, `\b`
        call serial_char
        pop ax
        dec cx
        mov bx, 1
        call cursor_back_n
        call .delete_at_cursor
        jmp .read_char

        .delete:
        cmp cx, dx
        je .read_char
        call .delete_at_cursor
        jmp .read_char

        .ctrl_a:
        cmp cx, buffer
        je .read_char
        mov bx, cx
        sub bx, buffer
        call cursor_back_n
        mov cx, buffer
        jmp .read_char

        .ctrl_c:
        mov al, `\r`
        call print_char
        mov al, `\n`
        call print_char
        mov cx, buffer
        mov dx, buffer
        jmp .return

        .ctrl_d:
        call shutdown
        jmp .read_char          ; If shutdown fails, continue

        .ctrl_e:
        cmp cx, dx
        je .read_char
        mov ah, 0Eh
        .ce_loop:
        mov bx, cx
        mov al, [bx]
        xor bx, bx
        int 10h
        inc cx
        cmp cx, dx
        jne .ce_loop
        jmp .read_char

        .ctrl_k:
        cmp cx, dx
        je .read_char
        push si
        push di
        ;; Copy killed text to kill buffer
        mov si, cx
        mov di, kill_buffer
        mov bx, dx
        sub bx, cx
        cmp bx, max_input
        jle .ck_save
        mov bx, max_input
        .ck_save:
        mov [kill_length], bx
        .ck_copy:
        mov al, [si]
        mov [di], al
        inc si
        inc di
        dec bx
        jnz .ck_copy
        ;; Erase killed text from screen
        mov ah, 0Eh
        xor bx, bx
        mov si, dx
        sub si, cx              ; Count of chars to erase
        push si                 ; Save count
        .ck_erase:
        mov al, ' '
        int 10h
        dec si
        jnz .ck_erase
        pop bx                  ; Restore count
        call cursor_back_n
        mov dx, cx              ; Truncate buffer at cursor
        pop di
        pop si
        jmp .read_char

        .ctrl_y:
        push si
        mov si, kill_buffer
        mov bx, [kill_length]
        test bx, bx
        jz .cy_done
        .cy_loop:
        mov al, [si]
        push bx
        call .insert_char
        pop bx
        jc .cy_full             ; Stop yanking if buffer full
        inc si
        dec bx
        jnz .cy_loop
        jmp .cy_done
        .cy_full:
        call visual_bell
        .cy_done:
        pop si
        jmp .read_char

        .ctrl_l:
        call clear_screen
        mov cx, buffer          ; Reset to start of buffer
        mov dx, buffer
        jmp .return

        .end:
        mov al, `\r`
        call print_char
        mov al, `\n`
        call print_char
        .return:
        mov bx, dx              ; Add null terminating character to buffer
        mov byte [bx], 00h
        mov cx, dx
        sub cx, buffer         ; Store how many characters were read in cx
        pop dx
        pop bx
        pop ax
        ret

        ;; Insert char in AL at cursor, shift buffer right, redraw
        .insert_char:
        push bx
        mov bx, dx
        sub bx, buffer
        cmp bx, max_input
        pop bx
        jl .ic_ok
        stc                     ; Set carry flag to signal buffer full
        ret
        .ic_ok:
        push si
        push ax
        mov si, dx
        .ic_shift:
        cmp si, cx
        jle .ic_insert
        dec si
        mov al, [si]
        mov [si+1], al
        jmp .ic_shift
        .ic_insert:
        pop ax
        mov bx, cx
        mov [bx], al
        inc dx
        ;; Print from cursor to end
        mov ah, 0Eh
        xor bx, bx
        mov si, cx
        .ic_print:
        cmp si, dx
        jge .ic_repos
        mov al, [si]
        int 10h
        inc si
        jmp .ic_print
        .ic_repos:
        inc cx
        mov bx, dx
        sub bx, cx
        call cursor_back_n
        clc                     ; Clear carry flag to signal success
        pop si
        ret

        ;; Delete char at cursor, shift buffer left, redraw
        .delete_at_cursor:
        push si
        mov si, cx
        inc si
        .dac_shift:
        cmp si, dx
        jge .dac_redraw
        mov al, [si]
        dec si
        mov [si], al
        inc si
        inc si
        jmp .dac_shift
        .dac_redraw:
        dec dx
        ;; Print from cursor to end, space to erase, backspace to cursor
        mov ah, 0Eh
        xor bx, bx
        mov si, cx
        .dac_print:
        cmp si, dx
        jge .dac_erase
        mov al, [si]
        int 10h
        inc si
        jmp .dac_print
        .dac_erase:
        mov al, ' '
        int 10h                 ; Erase trailing character
        mov bx, dx
        sub bx, cx
        inc bx
        call cursor_back_n
        pop si
        ret
