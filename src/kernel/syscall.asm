syscall_handler:
        cmp ah, SYS_FS_FIND    ; fs_find
        je .fs_find
        cmp ah, SYS_FS_READ    ; fs_read
        je .fs_read

        cmp ah, SYS_IO_GETC    ; io_getc
        je .io_getc
        cmp ah, SYS_IO_PUTC    ; io_putc
        je .io_putc
        cmp ah, SYS_IO_PUTS    ; io_puts
        je .io_puts

        cmp ah, SYS_RTC_DATETIME ; rtc_datetime
        je .rtc_datetime
        cmp ah, SYS_RTC_UPTIME ; rtc_uptime
        je .rtc_uptime

        cmp ah, SYS_SCR_CLEAR  ; scr_clear
        je .scr_clear

        cmp ah, SYS_EXEC       ; sys_exec
        je .sys_exec
        cmp ah, SYS_EXIT       ; sys_exit
        je .sys_exit
        cmp ah, SYS_REBOOT     ; sys_reboot
        je .sys_reboot
        cmp ah, SYS_SHUTDOWN   ; sys_shutdown
        je .sys_shutdown
        iret

        .fs_find:
        call find_file
        jmp .iret_cf

        .fs_read:
        call read_sector
        jmp .iret_cf

        .io_getc:
        ;; Poll both keyboard and serial, return char in AL, scan code in AH
        .getc_poll:
        push dx
        mov dx, 3FDh
        in al, dx
        pop dx
        test al, 01h            ; Serial data ready?
        jnz .getc_serial
        mov ah, 01h
        int 16h
        jz .getc_poll           ; Neither ready, keep polling
        mov ah, 00h
        int 16h                 ; Consume the key
        iret
        .getc_serial:
        push dx
        mov dx, 3F8h
        in al, dx               ; Read the byte
        pop dx
        xor ah, ah              ; No scan code from serial
        iret

        .io_putc:
        call put_char
        iret

        .io_puts:
        call put_string
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
        ;; Clear serial terminal
        mov al, `\r`
        call serial_char
        mov al, 0Ch             ; Form feed
        call serial_char
        ;; Clear screen
        call clear_screen
        iret

        .sys_exec:
        ;; Execute program: SI = filename
        ;; Saves shell stack, loads program at PROGRAM_BASE, jumps to it
        ;; If file not found, returns with carry set
        call find_file
        jc .exec_fail
        ;; Save SP from before INT 30h (skip iret frame: IP, CS, flags)
        mov bp, sp
        add bp, 6
        mov [shell_sp], bp
        ;; Load program sectors into PROGRAM_BASE
        mov cx, [bx+14]        ; File size in bytes
        mov bl, [bx+12]        ; Start sector
        mov di, PROGRAM_BASE
        .exec_load:
        mov al, bl
        call read_sector
        jc .exec_fail
        push cx
        cmp cx, 512
        jle .exec_partial
        mov cx, 256             ; Full sector = 256 words
        jmp .exec_copy
        .exec_partial:
        inc cx                  ; Round up to whole words
        shr cx, 1
        .exec_copy:
        cld
        mov si, DISK_BUFFER
        rep movsw
        pop cx
        sub cx, 512
        jle .exec_run
        inc bl
        jmp .exec_load
        .exec_run:
        jmp PROGRAM_BASE
        .exec_fail:
        stc
        jmp .iret_cf

        .sys_exit:
        ;; Restore stack and reload shell
        xor ax, ax
        mov ds, ax
        mov es, ax
        mov sp, [shell_sp]
        jmp boot_shell

        .sys_reboot:
        call reboot
        iret

        .sys_shutdown:
        call shutdown
        iret

        .iret_cf:
        ;; Return via iret, propagating current CF to caller's saved flags
        ;; Stack: [IP] [CS] [FLAGS]
        push bp
        mov bp, sp
        jnc .iret_clc
        or word [bp+6], 0001h   ; Set CF in saved FLAGS
        pop bp
        iret
        .iret_clc:
        and word [bp+6], 0FFFEh ; Clear CF in saved FLAGS
        pop bp
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
