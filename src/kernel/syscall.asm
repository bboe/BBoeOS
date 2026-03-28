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

        cmp ah, 20h             ; scr_clear
        je .scr_clear

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

        .scr_clear:
        call clear_screen
        iret

        .sys_exit:
        ;; Restore stack and jump back to CLI loop
        mov sp, [cli_sp]
        jmp cli

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
