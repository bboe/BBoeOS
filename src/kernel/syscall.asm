syscall_handler:
        cmp ah, 00h             ; fs_find
        je .fs_find
        cmp ah, 01h             ; fs_read
        je .fs_read

        cmp ah, 10h             ; io_getc
        je .io_getc
        cmp ah, 11h             ; io_gets
        je .io_gets
        cmp ah, 12h             ; io_putc
        je .io_putc
        cmp ah, 13h             ; io_puts
        je .io_puts

        cmp ah, 20h             ; rtc_datetime
        je .rtc_datetime
        cmp ah, 21h             ; rtc_uptime
        je .rtc_uptime

        cmp ah, 30h             ; scr_clear
        je .scr_clear
        cmp ah, 31h             ; scr_graphics
        je .scr_graphics

        cmp ah, 0F0h            ; sys_exit
        je .sys_exit
        cmp ah, 0F1h            ; sys_reboot
        je .sys_reboot
        cmp ah, 0F2h            ; sys_shutdown
        je .sys_shutdown
        iret

        .fs_find:
        call find_file
        iret

        .fs_read:
        call read_sector
        iret

        .io_getc:
        mov ah, 00h
        int 16h
        iret

        .io_gets:
        call read_line
        iret

        .io_putc:
        call print_char
        iret

        .io_puts:
        call print_string
        iret

        .rtc_datetime:
        ;; Returns date+time in BCD:
        ;;   CH=century, CL=year, DH=month, DL=day
        ;;   BH=hours, BL=minutes, AL=seconds
        mov ah, 04h
        int 1Ah                 ; CH=century, CL=year, DH=month, DL=day
        push cx
        push dx
        mov ah, 02h
        int 1Ah                 ; CH=hours, CL=minutes, DH=seconds
        mov bh, ch              ; BH = hours
        mov bl, cl              ; BL = minutes
        mov al, dh              ; AL = seconds
        pop dx
        pop cx
        iret

        .rtc_uptime:
        ;; Return elapsed seconds in AX
        push ecx
        push edx
        xor ah, ah
        int 1Ah                 ; CX:DX = current ticks since midnight
        movzx eax, cx
        shl eax, 16
        or ax, dx
        movzx ecx, word [boot_ticks_high]
        shl ecx, 16
        or cx, [boot_ticks_low]
        sub eax, ecx            ; EAX = elapsed ticks
        xor edx, edx
        mov ecx, 18
        div ecx                 ; EAX = elapsed seconds
        pop edx
        pop ecx
        iret

        .scr_clear:
        call clear_screen
        iret

        .scr_graphics:
        call graphics
        iret

        .sys_exit:
        ;; Restore stack and jump back to shell
        xor ax, ax
        mov ds, ax
        mov es, ax
        mov sp, [shell_sp]
        jmp program_base

        .sys_reboot:
        call reboot
        iret

        .sys_shutdown:
        call shutdown
        iret

install_syscalls:
        ;; Install INT 30h handler
        push ax
        push bx
        push es
        xor ax, ax
        mov es, ax
        mov word [es:30h*4], syscall_handler
        mov word [es:30h*4+2], cs
        pop es
        pop bx
        pop ax
        ret
