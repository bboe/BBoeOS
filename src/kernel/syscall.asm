syscall_handler:
        cmp ah, SYS_FS_CHMOD   ; fs_chmod
        je .fs_chmod
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

        cmp ah, SYS_NET_ARP    ; net_arp
        je .net_arp
        cmp ah, SYS_NET_INIT   ; net_init
        je .net_init
        cmp ah, SYS_NET_PING   ; net_ping
        je .net_ping
        cmp ah, SYS_NET_RECV   ; net_recv
        je .net_recv
        cmp ah, SYS_NET_SEND   ; net_send
        je .net_send
        cmp ah, SYS_NET_UDP_RECV ; net_udp_recv
        je .net_udp_recv
        cmp ah, SYS_NET_UDP_SEND ; net_udp_send
        je .net_udp_send

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

        .fs_chmod:
        ;; Update flags byte for a file: SI = filename, AL = new flags value
        push ax                ; Save new flags value
        call find_file         ; BX = directory entry in DISK_BUFFER
        jc .fs_chmod_err
        pop ax
        mov [bx+11], al        ; Write new flags byte
        mov al, DIR_SECTOR
        call write_sector
        jmp .iret_cf
        .fs_chmod_err:
        pop ax
        stc
        jmp .iret_cf

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

        .net_arp:
        call arp_resolve
        jmp .iret_cf

        .net_init:
        call ne2k_probe
        jc .iret_cf
        call ne2k_init
        ;; Copy MAC address to caller's buffer at DI
        push si
        push cx
        cld
        mov si, mac_addr
        mov cx, 3              ; 6 bytes = 3 words
        rep movsw
        pop cx
        pop si
        clc
        jmp .iret_cf

        .net_ping:
        call icmp_ping
        jmp .iret_cf

        .net_recv:
        call ne2k_recv
        jmp .iret_cf

        .net_send:
        call ne2k_send
        jmp .iret_cf

        .net_udp_recv:
        call udp_recv
        jmp .iret_cf

        .net_udp_send:
        call udp_send
        jmp .iret_cf

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
        ;; If file not found or not executable, returns with carry set
        call find_file
        jc .exec_fail
        test byte [bx+11], FLAG_EXEC  ; Check executable bit in flags byte
        jz .exec_fail
        ;; Save SP from before INT 30h (skip iret frame: IP, CS, flags)
        mov bp, sp
        add bp, 6
        mov [shell_sp], bp
        ;; Load program into PROGRAM_BASE
        mov di, PROGRAM_BASE
        call load_file
        jc .exec_fail
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
