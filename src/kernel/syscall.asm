syscall_handler:
        cmp ah, SYS_FS_CHMOD   ; fs_chmod
        je .fs_chmod
        cmp ah, SYS_FS_COPY    ; fs_copy
        je .fs_copy
        cmp ah, SYS_FS_FIND    ; fs_find
        je .fs_find
        cmp ah, SYS_FS_READ    ; fs_read
        je .fs_read
        cmp ah, SYS_FS_RENAME  ; fs_rename
        je .fs_rename

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
        ;; On error: CF set, AL = ERR_PROTECTED/ERR_NOT_FOUND
        ;; Protect shell: cannot be chmod'd
        call .check_shell
        jne .fs_chmod_find
        mov al, ERR_PROTECTED
        stc
        jmp .iret_cf
        .fs_chmod_find:
        push ax                ; Save new flags value
        call find_file         ; BX = directory entry in DISK_BUFFER
        jnc .fs_chmod_do
        pop ax
        mov al, ERR_NOT_FOUND
        stc
        jmp .iret_cf
        .fs_chmod_do:
        pop ax
        mov [bx+11], al        ; Write new flags byte
        mov al, DIR_SECTOR
        call write_sector
        jmp .iret_cf

        .fs_copy:
        ;; Copy file: SI = source filename, DI = dest filename
        ;; On error: CF set, AL = ERR_EXISTS/ERR_NOT_FOUND
        ;; Check dest doesn't already exist
        push si
        push di
        mov si, di
        call find_file
        pop di
        pop si
        jc .copy_check_src
        mov al, ERR_EXISTS
        stc
        jmp .iret_cf
        .copy_check_src:
        ;; Find source entry (re-reads directory into DISK_BUFFER)
        push di
        call find_file         ; BX = source entry in DISK_BUFFER
        pop di
        jnc .copy_got_src
        mov al, ERR_NOT_FOUND
        stc
        jmp .iret_cf
        .copy_got_src:
        ;; Build stack frame before scan clobbers BX
        ;; Layout (after all pushes, BP = SP):
        ;;   [bp+0]  next_sec   [bp+2]  free_entry  [bp+4]  size
        ;;   [bp+6]  src_sec    [bp+8]  flags        [bp+10] dest name ptr
        push di                        ; [bp+10]: dest name ptr
        xor ax, ax
        mov al, [bx+11]
        push ax                        ; [bp+8]: flags
        xor ax, ax
        mov al, [bx+12]
        push ax                        ; [bp+6]: src_sec
        mov ax, [bx+14]
        push ax                        ; [bp+4]: size
        ;; Scan directory: BX = free_entry (0=none), DL = next_sec
        xor bx, bx
        mov dl, DIR_SECTOR
        inc dl                         ; DL = next_sec = DIR_SECTOR+1
        mov si, DISK_BUFFER
        mov cx, DIR_MAX_ENTRIES
        .copy_scan:
        cmp byte [si], 0
        jne .copy_scan_occupied
        test bx, bx
        jnz .copy_scan_next
        mov bx, si                     ; record free entry ptr
        jmp .copy_scan_next
        .copy_scan_occupied:
        xor ax, ax
        mov al, [si+12]                ; start sector
        push bx
        xor bx, bx
        mov bx, [si+14]                ; size in bytes
        add bx, 511
        shr bx, 9                      ; ceil(size/512) = sectors used
        add al, bl                     ; end sector = start + sectors
        pop bx
        cmp al, dl
        jbe .copy_scan_next
        mov dl, al                     ; update next_sec
        .copy_scan_next:
        add si, DIR_ENTRY_SIZE
        loop .copy_scan
        ;; Check a free directory entry was found
        test bx, bx
        jnz .copy_push_scan
        add sp, 6                      ; discard size, src_sec, flags
        pop di
        mov al, ERR_DIR_FULL
        stc
        jmp .iret_cf
        .copy_push_scan:
        push bx                        ; [bp+2]: free_entry
        push dx                        ; [bp+0]: next_sec (in DL)
        mov bp, sp
        ;; Copy sectors: BL = src_sec, CL = dest_sec, DI = remaining bytes
        xor bx, bx
        mov bl, [bp+6]                 ; BL = src_sec
        xor cx, cx
        mov cl, [bp+0]                 ; CL = next_sec (dest sector)
        mov di, [bp+4]                 ; DI = remaining bytes
        .copy_sector:
        mov al, bl
        call read_sector
        jc .copy_disk_err
        mov al, cl
        call write_sector
        jc .copy_disk_err
        inc bl
        inc cl
        sub di, 512
        jbe .copy_sectors_done
        jmp .copy_sector
        .copy_disk_err:
        add sp, 10                     ; discard frame except dest name
        pop di
        mov al, ERR_NOT_FOUND
        stc
        jmp .iret_cf
        .copy_sectors_done:
        ;; Re-read directory (overwritten during sector copies)
        mov al, DIR_SECTOR
        call read_sector
        jc .copy_dir_err
        ;; Write new directory entry: name (11 bytes, null-padded), then metadata
        mov bx, [bp+2]                 ; BX = free_entry ptr
        mov si, [bp+10]                ; SI = dest name ptr
        push cx
        mov cx, 11
        .copy_write_name:
        mov al, [si]
        test al, al
        jz .copy_name_null
        inc si
        mov [bx], al
        inc bx
        dec cx
        jnz .copy_write_name
        jmp .copy_write_meta
        .copy_name_null:
        mov byte [bx], 0
        inc bx
        dec cx
        jnz .copy_name_null
        .copy_write_meta:
        pop cx
        ;; BX = free_entry + 11 (flags byte position)
        mov al, [bp+8]
        mov [bx], al                   ; flags
        inc bx
        mov al, [bp+0]
        mov [bx], al                   ; start sector low byte
        inc bx
        mov byte [bx], 0               ; start sector high byte
        inc bx
        mov ax, [bp+4]
        mov [bx], ax                   ; file size (2 bytes)
        mov al, DIR_SECTOR
        call write_sector
        add sp, 12                     ; discard full frame (6 words)
        jmp .iret_cf
        .copy_dir_err:
        add sp, 10                     ; discard frame except dest name
        pop di
        mov al, ERR_NOT_FOUND
        stc
        jmp .iret_cf

        .fs_find:
        call find_file
        jmp .iret_cf

        .fs_read:
        call read_sector
        jmp .iret_cf

        .fs_rename:
        ;; Rename file: SI = old filename, DI = new filename (max 10 chars)
        ;; On error: CF set, AL = ERR_PROTECTED/ERR_EXISTS/ERR_NOT_FOUND
        ;; Protect shell: cannot be renamed
        call .check_shell
        jne .fs_rename_check_dup
        mov al, ERR_PROTECTED
        stc
        jmp .iret_cf
        .fs_rename_check_dup:
        ;; Check new name doesn't already exist
        push si
        mov si, di
        call find_file
        pop si
        jc .fs_rename_find_old ; New name not found: proceed
        mov al, ERR_EXISTS
        stc
        jmp .iret_cf
        .fs_rename_find_old:
        ;; find_file preserves DI, so DI still holds new name after the call
        call find_file         ; BX = directory entry in DISK_BUFFER
        jnc .fs_rename_do
        mov al, ERR_NOT_FOUND
        stc
        jmp .iret_cf
        .fs_rename_do:
        push cx
        mov cx, 11
        .rename_copy:
        mov al, [di]
        test al, al
        jz .rename_null
        inc di
        mov [bx], al
        inc bx
        dec cx
        jnz .rename_copy
        jmp .rename_done
        .rename_null:
        mov byte [bx], 0
        inc bx
        dec cx
        jnz .rename_null
        .rename_done:
        pop cx
        mov al, DIR_SECTOR
        call write_sector
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
        ;; On error: CF set, AL = ERR_NOT_FOUND or ERR_NOT_EXEC
        call find_file
        jnc .exec_check_flag
        mov al, ERR_NOT_FOUND
        stc
        jmp .iret_cf
        .exec_check_flag:
        test byte [bx+11], FLAG_EXEC  ; Check executable bit in flags byte
        jnz .exec_load
        mov al, ERR_NOT_EXEC
        stc
        jmp .iret_cf
        .exec_load:
        ;; Save SP from before INT 30h (skip iret frame: IP, CS, flags)
        mov bp, sp
        add bp, 6
        mov [shell_sp], bp
        ;; Load program into PROGRAM_BASE
        mov di, PROGRAM_BASE
        call load_file
        jnc .exec_run
        mov al, ERR_NOT_FOUND
        stc
        jmp .iret_cf
        .exec_run:
        jmp PROGRAM_BASE

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

        .check_shell:
        ;; Returns ZF set if SI points to "shell" (null-terminated)
        push si
        push di
        push cx
        cld
        mov di, SHELL_NAME
        mov cx, 6              ; 5 chars + null terminator
        repe cmpsb
        pop cx
        pop di
        pop si
        ret

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
