parse_ip:
        ;; Parse dotted-decimal IP string into 4-byte buffer
        ;; Input: SI = null-terminated string, DI = 4-byte output buffer
        ;; Output: CF set on parse error
        push ax
        push bx
        push cx

        mov cx, 4              ; 4 octets
        .octet_loop:
        xor bx, bx             ; BX = 16-bit octet accumulator
        mov byte [.has_digit], 0
        .digit_loop:
        lodsb
        sub al, '0'
        jb .sep_check          ; Below '0': check if valid separator
        cmp al, 9
        ja .parse_error        ; Above '9': not a digit or valid separator
        mov byte [.has_digit], 1
        ;; BX = BX * 10 + digit
        xor ah, ah             ; Zero-extend digit into AX
        push ax                ; Save digit
        mov ax, bx             ; AX = accumulator
        mov bx, 10
        mul bx                 ; DX:AX = AX * 10
        pop bx                 ; BX = digit
        add ax, bx             ; AX = acc * 10 + digit
        cmp ax, 255
        ja .parse_error        ; Octet value > 255
        mov bx, ax             ; BX = updated accumulator
        jmp .digit_loop

        .sep_check:
        cmp byte [.has_digit], 0
        je .parse_error        ; At least one digit required per octet
        ;; Recover original character and validate separator
        add al, '0'
        cmp cx, 1
        je .last_octet         ; Last octet must end with null
        cmp al, '.'            ; Other octets must end with '.'
        jne .parse_error
        jmp .octet_done
        .last_octet:
        test al, al            ; Must be null terminator
        jnz .parse_error
        .octet_done:
        mov [di], bl           ; Store octet (BL = value, guaranteed ≤ 255)
        inc di
        loop .octet_loop

        clc
        jmp .parse_done
        .parse_error:
        stc
        .parse_done:
        pop cx
        pop bx
        pop ax
        ret

        .has_digit db 0
