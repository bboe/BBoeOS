fd_read_console:
        ;; Read from keyboard/serial into [DI], up to CX bytes.
        ;; Returns after the first key event (like Linux terminal input):
        ;;   - normal key: 1 byte (ASCII)
        ;;   - serial input: passed through as-is (1 byte at a time)
        ;;   - keyboard arrow key: 3 bytes (ESC [ A/B/C/D)
        push bx
        push cx
        push dx
        push di
        mov bx, cx             ; BX = bytes available in buffer
        test bx, bx
        jz .rcon_zero
        ;; Drain serial pushback buffer first
        cmp byte [serial_pushback_count], 0
        jne .rcon_pushback
        ;; Poll hardware.  sti so PIT IRQ 0 can advance system_ticks while
        ;; we're idle — INT 30h entered with IF=0 and nothing else re-enables
        ;; it before we spin here.
        .rcon_poll:
        sti
        push dx
        mov dx, 3FDh
        in al, dx
        pop dx
        test al, 01h
        jnz .rcon_serial
        call ps2_check
        jz .rcon_poll
        ;; Keyboard key ready
        call ps2_read           ; AL = ASCII, AH = scan code
        test al, al
        jz .rcon_extended       ; AL=0 means extended key
        ;; Normal ASCII key — store 1 byte
        stosb
        mov ax, 1
        jmp .rcon_ret
        .rcon_extended:
        ;; Map keyboard scan code to ESC sequence
        cmp bx, 3
        jb .rcon_poll           ; not enough buffer room, skip
        mov al, 1Bh
        stosb
        mov al, '['
        stosb
        cmp ah, 48h
        je .rcon_key_up
        cmp ah, 50h
        je .rcon_key_down
        cmp ah, 4Dh
        je .rcon_key_right
        cmp ah, 4Bh
        je .rcon_key_left
        ;; Unknown extended key — undo the ESC [ and retry
        sub di, 2
        jmp .rcon_poll
        .rcon_key_up:
        mov al, 'A'
        jmp .rcon_key_emit
        .rcon_key_down:
        mov al, 'B'
        jmp .rcon_key_emit
        .rcon_key_right:
        mov al, 'C'
        jmp .rcon_key_emit
        .rcon_key_left:
        mov al, 'D'
        .rcon_key_emit:
        stosb
        mov ax, 3
        jmp .rcon_ret
        .rcon_serial:
        ;; Serial byte ready — read and return it as-is
        push dx
        mov dx, 3F8h
        in al, dx
        pop dx
        stosb
        mov ax, 1
        jmp .rcon_ret
        .rcon_pushback:
        ;; Return one byte from the serial pushback buffer
        mov al, [serial_pushback_buffer]
        mov ah, [serial_pushback_buffer+1]
        mov [serial_pushback_buffer], ah
        dec byte [serial_pushback_count]
        stosb
        mov ax, 1
        .rcon_ret:
        pop di
        pop dx
        pop cx
        pop bx
        clc
        ret
        .rcon_zero:
        xor ax, ax
        pop di
        pop dx
        pop cx
        pop bx
        clc
        ret

fd_write_console:
        ;; Write CX bytes from user buffer to console via put_character
        push bx
        push cx
        push dx
        push si
        mov si, [fd_write_buffer]
        mov bx, cx             ; BX = count
        xor dx, dx             ; DX = bytes written
        test bx, bx
        jz .wcon_done
        .wcon_loop:
        lodsb
        call put_character
        inc dx
        cmp dx, bx
        jb .wcon_loop
        .wcon_done:
        mov ax, dx
        pop si
        pop dx
        pop cx
        pop bx
        clc
        ret

        serial_pushback_buffer    db 0, 0
        serial_pushback_count     db 0
