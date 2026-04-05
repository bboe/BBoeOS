print_bcd:
        ;; Print AL as two BCD digits via io_putc
        push cx
        mov cl, al
        shr al, 4               ; High nibble
        add al, '0'
        mov ah, SYS_IO_PUTC
        int 30h
        mov al, cl
        and al, 0Fh             ; Low nibble
        add al, '0'
        mov ah, SYS_IO_PUTC
        int 30h
        pop cx
        ret
