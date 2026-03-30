print_byte_dec:
        ;; Print AL as 1-3 digit decimal (no leading zeros)
        push ax
        push bx
        push cx

        xor ah, ah
        xor bx, bx             ; Digit count
        mov cl, 10
        .div_loop:
        div cl                 ; AL = quotient, AH = remainder
        push ax
        inc bx
        test al, al
        jz .print_digits
        xor ah, ah
        jmp .div_loop
        .print_digits:
        pop ax
        mov al, ah
        add al, '0'
        mov ah, SYS_IO_PUTC
        int 30h
        dec bx
        jnz .print_digits

        pop cx
        pop bx
        pop ax
        ret
