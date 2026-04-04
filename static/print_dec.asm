print_dec:
        ;; Print AL as 2 zero-padded decimal digits via io_putc
        aam                     ; AH = AL/10, AL = AL%10
        xchg al, ah             ; AL = tens, AH = ones
        add al, '0'
        push ax
        mov ah, SYS_IO_PUTC
        int 30h
        pop ax
        mov al, ah
        add al, '0'
        mov ah, SYS_IO_PUTC
        int 30h
        ret
