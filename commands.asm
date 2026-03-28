handle_clear:
        call clear_screen
        xor si, si
        ret

handle_date:
        call print_date
        mov si, newline
        ret

handle_graphics:
        call graphics
        xor si, si
        ret

handle_help:
        call print_help
        xor si, si
        ret

handle_reboot:
        call reboot
        xor si, si
        ret

handle_shutdown:
        call shutdown
        mov si, shutdown_fail
        ret

handle_time:
        call print_time
        mov si, newline
        ret

handle_uptime:
        call print_uptime
        mov si, newline
        ret

print_help:
        push bx
        mov si, help_prefix
        call print_string
        mov bx, command_table
        .loop:
        mov si, [bx]
        test si, si
        jz .end
        call print_string
        mov al, ' '
        call print_char
        add bx, 4
        jmp .loop
        .end:
        mov si, newline
        call print_string
        pop bx
        ret

print_dec_byte:
        ;; Print AL as 2 decimal digits
        push ax
        push cx
        xor ah, ah
        mov cl, 10
        div cl                  ; AL = tens, AH = ones
        add al, '0'
        call print_char
        mov al, ah
        add al, '0'
        call print_char
        pop cx
        pop ax
        ret

print_uptime:
        push eax
        push ecx
        push edx

        xor ah, ah
        int 1Ah                 ; CX:DX = current ticks since midnight

        movzx eax, cx           ; Build 32-bit current ticks in EAX
        shl eax, 16
        or ax, dx

        movzx ecx, word [boot_ticks_high]
        shl ecx, 16
        or cx, [boot_ticks_low]

        sub eax, ecx            ; EAX = elapsed ticks

        xor edx, edx
        mov ecx, 18
        div ecx                 ; EAX = elapsed seconds

        xor edx, edx
        mov ecx, 3600
        div ecx                 ; EAX = hours, EDX = remaining seconds

        push dx                 ; Save remaining seconds
        call print_dec_byte     ; Print hours (in AL)
        mov al, ':'
        call print_char

        pop ax                  ; Remaining seconds
        xor ah, ah
        mov cl, 60
        div cl                  ; AL = minutes, AH = seconds

        push ax                 ; Save seconds
        call print_dec_byte     ; Print minutes (in AL)
        mov al, ':'
        call print_char

        pop ax
        mov al, ah              ; Seconds
        call print_dec_byte

        pop edx
        pop ecx
        pop eax
        ret

process_command:
        push bx
        push dx
        cld
        inc cx
        mov dx, cx              ; Save string length in DX

        mov bx, command_table
        .loop:
        mov di, [bx]            ; Load command string pointer
        test di, di
        jz .invalid             ; End of table — no match

        mov cx, dx              ; Restore length
        mov si, buffer
        repe cmpsb
        jnz .next

        call word [bx+2]        ; Call handler
        jmp .end

        .next:
        add bx, 4
        jmp .loop

        .invalid:
        mov si, invalid_message

        .end:
        test si, si
        jz .done
        call print_string
        .done:
        pop dx
        pop bx
        ret
