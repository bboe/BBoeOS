print_hex:
        ;; Print AL as two uppercase hex digits
        push ax
        shr al, 4
        call .nibble
        pop ax
        push ax
        and al, 0Fh
        call .nibble
        pop ax
        ret
        .nibble:
        cmp al, 10
        jb .digit
        add al, 'A' - 10
        jmp .print
        .digit:
        add al, '0'
        .print:
        mov ah, SYS_IO_PUTC
        int 30h
        ret
