syscall_handler:
        cmp ah, SYS_FS_CHMOD   ; fs_chmod
        je .fs_chmod
        cmp ah, SYS_FS_COPY    ; fs_copy
        je .fs_copy
        cmp ah, SYS_FS_CREATE  ; fs_create
        je .fs_create
        cmp ah, SYS_FS_FIND    ; fs_find
        je .fs_find
        cmp ah, SYS_FS_MKDIR   ; fs_mkdir
        je .fs_mkdir
        cmp ah, SYS_FS_READ    ; fs_read
        je .fs_read
        cmp ah, SYS_FS_RENAME  ; fs_rename
        je .fs_rename
        cmp ah, SYS_FS_WRITE   ; fs_write
        je .fs_write

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
        call find_file         ; BX = entry index
        jnc .fs_chmod_do
        pop ax
        mov al, ERR_NOT_FOUND
        stc
        jmp .iret_cf
        .fs_chmod_do:
        pop ax                 ; AL = new flags value
        mov [bx+DIR_OFF_FLAGS], al
        call dir_write_back
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
        call find_file         ; BX = entry index
        pop di
        jc .copy_src_err
        jmp .copy_got_src
        .copy_src_err:
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
        mov al, [bx+DIR_OFF_FLAGS]
        push ax                        ; [bp+8]: flags
        xor ax, ax
        mov al, [bx+DIR_OFF_SECTOR]
        push ax                        ; [bp+6]: src_sec
        mov ax, [bx+DIR_OFF_SIZE]
        push ax                        ; [bp+4]: size
        ;; Scan directory across all sectors
        call scan_dir_entries  ; BX = free entry index, DL = next data sector
        ;; Check a free directory entry was found
        cmp bx, 0FFFFh
        jne .copy_push_scan
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
        ;; Re-read directory entry (overwritten during sector copies)
        mov bx, [bp+2]                 ; BX = free entry index
        call dir_load_entry            ; BX = entry ptr in DISK_BUFFER
        mov si, [bp+10]                ; SI = dest name ptr
        ;; Copy name (null-padded to DIR_NAME_LEN)
        push cx
        push bx                        ; save entry base
        call .write_dir_name
        pop bx                         ; BX = entry base
        pop cx
        ;; Write metadata at fixed offsets
        mov al, [bp+8]
        mov [bx+DIR_OFF_FLAGS], al     ; flags
        mov al, [bp+0]
        mov [bx+DIR_OFF_SECTOR], al    ; start sector low byte
        mov byte [bx+DIR_OFF_SECTOR+1], 0
        mov ax, [bp+4]
        mov [bx+DIR_OFF_SIZE], ax      ; file size
        call dir_write_back
        add sp, 12                     ; discard full frame (6 words)
        jmp .iret_cf
        .copy_dir_err:
        add sp, 10                     ; discard frame except dest name
        pop di
        mov al, ERR_NOT_FOUND
        stc
        jmp .iret_cf

        .fs_create:
        ;; Create file: SI = filename (may contain one '/')
        ;; On success: CF clear, AL = start sector
        ;; On error: CF set, AL = ERR_EXISTS/ERR_DIR_FULL
        ;; Check name doesn't already exist
        call find_file
        jc .create_scan
        mov al, ERR_EXISTS
        stc
        jmp .iret_cf
        .create_scan:
        mov di, si             ; DI = full filename for later
        ;; Get next free data sector (global scan)
        call scan_dir_entries  ; BX = free root entry, DL = next data sector
        push dx                ; save next_sec
        ;; Check if path has '/' (subdirectory)
        push di
        .create_find_slash:
        mov al, [di]
        test al, al
        jz .create_no_slash
        cmp al, '/'
        je .create_in_subdir
        inc di
        jmp .create_find_slash
        .create_no_slash:
        pop di                 ; DI = filename (no slash)
        ;; Create in root directory
        cmp bx, 0FFFFh
        jne .create_write_entry
        pop dx
        mov al, ERR_DIR_FULL
        stc
        jmp .iret_cf
        .create_write_entry:
        ;; Load root sector with free entry
        pop dx                 ; DL = next data sector
        push dx
        call dir_load_entry    ; BX = entry ptr (uses root index from scan_dir_entries)
        pop dx
        jmp .create_do_write

        .create_in_subdir:
        ;; DI points to '/'. Stack has: [saved DI (full path)], [next_sec]
        mov byte [di], 0       ; null-terminate dir name
        pop si                 ; SI = start of path = dir name
        push di                ; save '/' position
        ;; Find the subdirectory in root
        push dx                ; save next_sec across find_file
        push si
        call find_file         ; BX = dir entry index
        pop si
        pop dx
        jc .create_subdir_err
        test byte [bx+DIR_OFF_FLAGS], FLAG_DIR
        jz .create_subdir_err
        ;; Read subdirectory sector
        mov al, [bx+DIR_OFF_SECTOR]
        mov [dir_loaded_sec], al
        call read_sector
        jc .create_subdir_err
        ;; Find free entry in subdirectory sector
        mov bx, DISK_BUFFER
        mov cx, DIR_MAX_ENTRIES / DIR_SECTORS
        .create_sub_scan:
        cmp byte [bx], 0
        je .create_sub_found
        add bx, DIR_ENTRY_SIZE
        loop .create_sub_scan
        ;; No free entry in subdirectory
        pop di                 ; restore '/'
        mov byte [di], '/'
        pop dx
        mov al, ERR_DIR_FULL
        stc
        jmp .iret_cf
        .create_sub_found:
        ;; BX = free entry ptr in subdirectory's DISK_BUFFER
        pop di                 ; restore '/' position
        mov byte [di], '/'
        inc di                 ; DI = filename after '/'
        pop dx                 ; DL = next data sector
        jmp .create_do_write

        .create_subdir_err:
        pop di
        mov byte [di], '/'
        pop dx
        mov al, ERR_NOT_FOUND
        stc
        jmp .iret_cf

        .create_do_write:
        ;; BX = entry ptr in DISK_BUFFER, DI = filename to write, DL = start sector
        push dx
        push bx
        mov si, di
        call .write_dir_name
        pop bx                 ; BX = entry base
        pop dx                 ; DL = next free sector
        mov byte [bx+DIR_OFF_FLAGS], 0
        mov [bx+DIR_OFF_SECTOR], dl
        mov byte [bx+DIR_OFF_SECTOR+1], 0
        mov word [bx+DIR_OFF_SIZE], 0
        call dir_write_back
        jc .iret_cf
        mov al, dl
        clc
        jmp .iret_cf

        .fs_find:
        call find_file         ; BX = entry index within loaded sector
        jc .iret_cf
        ;; Convert index to pointer (entry is already in DISK_BUFFER)
        and bx, 0Fh            ; index within sector (0-15)
        shl bx, 5              ; * DIR_ENTRY_SIZE (32)
        add bx, DISK_BUFFER
        jmp .iret_cf

        .fs_mkdir:
        ;; Create subdirectory: SI = name (no slashes)
        ;; On success: CF clear, AL = allocated sector
        ;; On error: CF set, AL = ERR_EXISTS/ERR_DIR_FULL
        ;; Check name doesn't already exist
        call find_file
        jc .mkdir_scan
        mov al, ERR_EXISTS
        stc
        jmp .iret_cf
        .mkdir_scan:
        mov di, si             ; DI = dirname for later
        call scan_dir_entries  ; BX = free entry index, DL = next data sector
        cmp bx, 0FFFFh
        jne .mkdir_write_entry
        mov al, ERR_DIR_FULL
        stc
        jmp .iret_cf
        .mkdir_write_entry:
        ;; Load the root sector containing the free entry
        push dx
        call dir_load_entry    ; BX = entry ptr (uses root index from scan)
        pop dx
        ;; Write directory name, null-padded
        push dx
        push bx
        mov si, di
        call .write_dir_name
        pop bx                 ; BX = entry base
        pop dx                 ; DL = next free sector
        mov byte [bx+DIR_OFF_FLAGS], FLAG_DIR
        mov [bx+DIR_OFF_SECTOR], dl
        mov byte [bx+DIR_OFF_SECTOR+1], 0
        mov word [bx+DIR_OFF_SIZE], 512
        ;; Write root directory sector back
        call dir_write_back
        jc .iret_cf
        ;; Zero-fill the subdirectory sector
        push dx
        push di
        mov di, DISK_BUFFER
        mov cx, 256
        xor ax, ax
        cld
        rep stosw              ; fill 512 bytes with zeros
        pop di
        pop dx
        ;; Write the zeroed sector to disk
        mov al, dl
        call write_sector
        jc .iret_cf
        ;; Return allocated sector in AL
        mov al, dl
        clc
        jmp .iret_cf

        .fs_read:
        call read_sector
        jmp .iret_cf

        .fs_write:
        ;; Write DISK_BUFFER to sector: AL = sector number, CF on error
        ;; Special: AL=0 writes back the directory sector loaded by dir_load_entry
        test al, al
        jnz .fs_write_sector
        call dir_write_back
        jmp .iret_cf
        .fs_write_sector:
        call write_sector
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
        call find_file         ; BX = entry index
        jc .fs_rename_not_found
        jmp .fs_rename_do
        .fs_rename_not_found:
        mov al, ERR_NOT_FOUND
        stc
        jmp .iret_cf
        .fs_rename_do:
        push cx
        push si
        mov si, di
        call .write_dir_name
        pop si
        pop cx
        call dir_write_back
        jmp .iret_cf

        .io_getc:
        ;; Poll both keyboard and serial, return char in AL, scan code in AH
        .getc_poll:
        ;; Drain pushback buffer first (used when ESC sequence detection reads ahead)
        cmp byte [serial_pb_count], 0
        je .getc_poll_hw
        mov al, [serial_pb_buf]    ; return first buffered byte
        mov ah, [serial_pb_buf+1]
        mov [serial_pb_buf], ah    ; shift second byte down
        xor ah, ah
        dec byte [serial_pb_count]
        iret
        .getc_poll_hw:
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
        cmp al, 1Bh             ; ESC? May be start of arrow key sequence
        jne .getc_serial_done
        ;; Poll for '[' with timeout
        push cx
        mov cx, 0FFFFh
        .getc_esc_bracket:
        push dx
        mov dx, 3FDh
        in al, dx
        pop dx
        test al, 01h
        jnz .getc_read_bracket
        loop .getc_esc_bracket
        pop cx
        jmp .getc_serial_esc    ; Timeout: standalone ESC
        .getc_read_bracket:
        push dx
        mov dx, 3F8h
        in al, dx               ; Read second byte
        pop dx
        cmp al, '['
        je .getc_esc_final
        ;; Not '[': push it back and return ESC
        mov [serial_pb_buf], al
        mov byte [serial_pb_count], 1
        pop cx
        jmp .getc_serial_esc
        .getc_esc_final:
        ;; Poll for final byte (A/B/C/D) with timeout
        mov cx, 0FFFFh
        .getc_esc_final_poll:
        push dx
        mov dx, 3FDh
        in al, dx
        pop dx
        test al, 01h
        jnz .getc_read_final
        loop .getc_esc_final_poll
        pop cx
        mov byte [serial_pb_buf], '['
        mov byte [serial_pb_count], 1
        jmp .getc_serial_esc    ; Timeout: return ESC, push back '['
        .getc_read_final:
        push dx
        mov dx, 3F8h
        in al, dx               ; Read third byte
        pop dx
        pop cx
        cmp al, 'A'
        je .getc_arrow_up
        cmp al, 'B'
        je .getc_arrow_down
        cmp al, 'C'
        je .getc_arrow_right
        cmp al, 'D'
        je .getc_arrow_left
        ;; Unknown third byte: push back '[' and the byte, return ESC
        mov byte [serial_pb_buf], '['
        mov [serial_pb_buf+1], al
        mov byte [serial_pb_count], 2
        jmp .getc_serial_esc
        .getc_arrow_up:
        xor al, al
        mov ah, 48h
        iret
        .getc_arrow_down:
        xor al, al
        mov ah, 50h
        iret
        .getc_arrow_right:
        xor al, al
        mov ah, 4Dh
        iret
        .getc_arrow_left:
        xor al, al
        mov ah, 4Bh
        iret
        .getc_serial_esc:
        xor ah, ah
        mov al, 1Bh
        iret
        .getc_serial_done:
        xor ah, ah
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
        jc .exec_not_found
        jmp .exec_check_flag
        .exec_not_found:
        mov al, ERR_NOT_FOUND
        stc
        jmp .iret_cf
        .exec_check_flag:
        test byte [bx+DIR_OFF_FLAGS], FLAG_EXEC
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

        .write_dir_name:
        ;; Copy null-terminated name from SI into entry at BX, padding with
        ;; zeros up to DIR_NAME_LEN - 1 bytes total. SI is advanced past the
        ;; null terminator and BX is advanced DIR_NAME_LEN - 1 bytes.
        ;; Clobbers: AX, BX (advanced), CX, SI (advanced)
        mov cx, DIR_NAME_LEN - 1
        .wdn_copy:
        mov al, [si]
        test al, al
        jz .wdn_pad
        inc si
        mov [bx], al
        inc bx
        dec cx
        jnz .wdn_copy
        ret
        .wdn_pad:
        mov byte [bx], 0
        inc bx
        dec cx
        jnz .wdn_pad
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
