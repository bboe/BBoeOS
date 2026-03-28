cat_file:
        ;; SI = pointer to filename argument (null-terminated)
        push bx
        push cx

        call find_file
        jc .cat_not_found

        ;; BX = directory entry: [BX+12] = start sector, [BX+14] = file size
        mov cx, [bx+14]        ; CX = file size in bytes
        test cx, cx
        jz .cat_empty
        mov al, [bx+12]        ; Start sector number
        call read_sector
        jc .cat_disk_err

        mov si, disk_buffer
        .cat_print:
        lodsb
        call print_char
        loop .cat_print

        .cat_empty:
        mov si, newline
        jmp .cat_done

        .cat_not_found:
        mov si, file_not_found
        jmp .cat_done

        .cat_disk_err:
        mov si, disk_error

        .cat_done:
        pop cx
        pop bx
        ret

handle_cat:
        mov si, cat_usage
        ret

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

handle_ls:
        push bx
        push cx

        mov al, dir_sector
        call read_sector
        jc .ls_err

        mov bx, disk_buffer
        mov cx, dir_max_entries

        .ls_loop:
        cmp byte [bx], 0       ; Empty entry = end of directory
        je .ls_done
        mov si, bx
        call print_string
        push si
        mov si, newline
        call print_string
        pop si
        add bx, dir_entry_size
        loop .ls_loop

        .ls_done:
        pop cx
        pop bx
        xor si, si
        ret

        .ls_err:
        pop cx
        pop bx
        mov si, disk_error
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

        ;; Check for 'cat ' prefix
        cmp dx, 5               ; Need at least "cat X" (4 + 1 char)
        jl .table_dispatch
        mov si, buffer
        mov di, cat_prefix
        mov cx, 4
        repe cmpsb
        jne .table_dispatch

        ;; SI = buffer + 4 = start of filename argument
        call cat_file
        jmp .end

        .table_dispatch:
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
