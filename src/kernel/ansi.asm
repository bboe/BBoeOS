        ;; ANSI escape sequence parser
        ;; put_char: unified output to screen (with ANSI parsing) and serial
        ;; put_string: print null-terminated string via put_char

put_char:
        push ax
        push bx
        push cx
        push dx

        ;; Always send raw byte to serial
        call serial_char

        ;; State machine dispatch
        cmp byte [ansi_state], 2
        je .state_csi
        cmp byte [ansi_state], 1
        je .state_esc

        ;; STATE_NORMAL
        cmp al, 1Bh            ; ESC?
        je .enter_esc
        ;; Regular character — teletype output
        mov ah, 0Eh
        xor bx, bx
        int 10h
        jmp .done

.enter_esc:
        mov byte [ansi_state], 1
        jmp .done

.state_esc:
        cmp al, '['
        je .enter_csi
        ;; Not a CSI sequence, output ESC and this char to screen
        push ax
        mov al, 1Bh
        mov ah, 0Eh
        xor bx, bx
        int 10h
        pop ax
        mov ah, 0Eh
        xor bx, bx
        int 10h
        mov byte [ansi_state], 0
        jmp .done

.enter_csi:
        mov byte [ansi_state], 2
        mov word [ansi_param], 0
        jmp .done

.state_csi:
        ;; Check if digit
        cmp al, '0'
        jb .csi_command
        cmp al, '9'
        ja .csi_command
        ;; Accumulate digit: param = param * 10 + (al - '0')
        sub al, '0'
        movzx cx, al
        mov ax, [ansi_param]
        mov bx, 10
        mul bx                  ; DX:AX = param * 10 (clobbers DX)
        add ax, cx
        mov [ansi_param], ax
        jmp .done

.csi_command:
        mov byte [ansi_state], 0
        mov bx, [ansi_param]
        test bx, bx
        jnz .has_param
        mov bx, 1              ; Default parameter is 1
.has_param:
        cmp al, 'C'            ; Cursor forward
        je .cursor_forward
        cmp al, 'D'            ; Cursor back
        je .cursor_back
        ;; Unknown command — ignore
        jmp .done

.cursor_back:
        ;; Move cursor back by BX positions (handles line wrapping)
        push bx                 ; Save count
        mov ah, 03h
        xor bx, bx
        int 10h                 ; DH=row, DL=col (clobbers CX)
        pop cx                  ; Restore count
        movzx ax, dh
        push dx
        mov bx, 80
        mul bx                  ; AX = row * 80 (clobbers DX)
        pop dx
        movzx bx, dl
        add ax, bx              ; AX = linear position
        sub ax, cx              ; AX = new linear position
        xor dx, dx
        mov bx, 80
        div bx                  ; AX = new row, DX = new col
        mov dh, al
        mov dl, dl              ; DL already has new col from DX
        mov ah, 02h
        xor bx, bx
        int 10h                 ; Set cursor position
        jmp .done

.cursor_forward:
        ;; Move cursor forward by BX positions
        push bx                 ; Save count
        mov ah, 03h
        xor bx, bx
        int 10h                 ; DH=row, DL=col (clobbers CX)
        pop cx                  ; Restore count
        add dl, cl              ; New column
        mov ah, 02h
        xor bx, bx
        int 10h
        jmp .done

.done:
        pop dx
        pop cx
        pop bx
        pop ax
        ret

put_string:
        push ax
.repeat:
        lodsb
        cmp al, 0
        je .end
        call put_char
        jmp .repeat
.end:
        pop ax
        ret

        ;; Parser state
        ansi_state db 0
        ansi_param dw 0

serial_char:
        ;; Write AL to COM1 (preserves all registers)
        push ax
        push dx
        push ax                 ; Save char
        mov dx, 3FDh           ; Line status register
        .wait:
        in al, dx
        test al, 20h           ; Transmit holding register empty?
        jz .wait
        pop ax                  ; Restore char
        mov dx, 3F8h           ; COM1 data register
        out dx, al
        pop dx
        pop ax
        ret
