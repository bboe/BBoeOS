print_mac:
        ;; Print a 6-byte MAC address as XX:XX:XX:XX:XX:XX
        ;; Input: SI = pointer to 6-byte MAC address
        push ax
        push cx
        mov cx, 6
        .loop:
        lodsb
        call print_hex
        dec cx
        jz .done
        mov al, ':'
        mov ah, SYS_IO_PUT_CHARACTER
        int 30h
        jmp .loop
        .done:
        pop cx
        pop ax
        ret
