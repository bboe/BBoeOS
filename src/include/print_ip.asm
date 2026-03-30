print_ip:
        ;; Print 4-byte IP address as dotted decimal
        ;; Input: SI = pointer to 4-byte IP
        push ax
        push cx

        mov cx, 4
        .ip_loop:
        lodsb
        call print_byte_dec
        dec cx
        jz .ip_done
        push cx
        mov al, '.'
        mov ah, SYS_IO_PUTC
        int 30h
        pop cx
        jmp .ip_loop
        .ip_done:

        pop cx
        pop ax
        ret
