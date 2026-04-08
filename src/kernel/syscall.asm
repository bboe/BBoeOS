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
        ;; Both names may contain one '/' for subdirectories.
        ;; On error: CF set, AL = ERR_EXISTS/ERR_NOT_FOUND/ERR_DIR_FULL
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
        call find_file         ; BX = src entry pointer
        pop di
        jc .copy_src_err
        jmp .copy_got_src
        .copy_src_err:
        mov al, ERR_NOT_FOUND
        stc
        jmp .iret_cf
        .copy_got_src:
        ;; Build stack frame. Layout (BP = SP after all pushes):
        ;;   [bp+10] basename_ptr  [bp+8]  flags        [bp+6] src_sec
        ;;   [bp+4]  size          [bp+2]  dest_entry_off
        ;;   [bp+0]  packed: low=dest_entry_sec, high=next_data_sec
        push di                        ; [bp+10]: basename (full dest for now)
        xor ax, ax
        mov al, [bx+DIR_OFF_FLAGS]
        push ax                        ; [bp+8]: flags
        xor ax, ax
        mov al, [bx+DIR_OFF_SECTOR]
        push ax                        ; [bp+6]: src_sec
        mov ax, [bx+DIR_OFF_SIZE]
        push ax                        ; [bp+4]: size
        ;; Scan directory globally for next data sector + free root entry
        call scan_dir_entries  ; BX = free root idx, DL = next data sector
        ;; Locate destination entry: examine dest path for '/'
        push bx                ; save root_idx temporarily
        push dx                ; save next_data_sec temporarily
        mov bp, sp             ; [bp+0] = next_dat, [bp+2] = root_idx, [bp+4] = size, ...
        mov di, [bp+10]        ; DI = full dest path
        .copy_find_slash:
        mov al, [di]
        test al, al
        jz .copy_dest_root
        cmp al, '/'
        je .copy_dest_subdir
        inc di
        jmp .copy_find_slash
        .copy_dest_root:
        ;; Use root free idx
        mov bx, [bp+2]
        cmp bx, 0FFFFh
        jne .copy_root_resolve
        ;; No free root entry
        add sp, 12
        mov al, ERR_DIR_FULL
        stc
        jmp .iret_cf
        .copy_root_resolve:
        ;; Compute dest_entry_off + dest_entry_sec from root_idx in BX
        push cx
        mov ax, bx
        and al, 0Fh
        xor ah, ah
        mov cl, 5
        shl ax, cl             ; AX = (idx & 15) * 32 = offset within sector
        mov cx, ax             ; CX = entry offset
        mov ax, bx
        shr al, 4
        add al, DIR_SECTOR     ; AL = dest_entry_sec
        mov ah, [bp+0]         ; AH = next_data_sec
        ;; Replace stack slots: [bp+0] = packed, [bp+2] = entry_off
        mov [bp+0], ax
        mov [bp+2], cx
        pop cx
        jmp .copy_data
        .copy_dest_subdir:
        ;; DI points to '/'. Split path.
        mov byte [di], 0       ; null-terminate dirname
        push di                ; save '/' position
        mov si, [bp+10]        ; SI = dirname (start of path)
        call find_file         ; BX = subdir entry ptr in DISK_BUFFER
        pop di                 ; restore '/' position
        mov byte [di], '/'
        jc .copy_subdir_bad
        test byte [bx+DIR_OFF_FLAGS], FLAG_DIR
        jz .copy_subdir_bad
        ;; Scan subdirectory for a free entry
        push cx
        mov al, [bx+DIR_OFF_SECTOR]
        call .subdir_find_free
        pop cx
        jc .copy_subdir_err
        ;; BX = entry pointer in DISK_BUFFER, dir_loaded_sec = current sector
        mov al, [dir_loaded_sec]
        ;; Compute dest_entry_off = BX - DISK_BUFFER
        sub bx, DISK_BUFFER
        ;; Build packed sector word: low = current subdir sector, high = next_dat
        mov ah, [bp+0]         ; high byte = next_dat
        mov [bp+0], ax         ; replace next_dat slot with packed sectors
        mov [bp+2], bx         ; replace root_idx slot with entry_offset
        ;; Update basename pointer to skip past '/'
        inc di                 ; DI = basename
        mov [bp+10], di
        jmp .copy_data
        .copy_subdir_bad:
        mov al, ERR_NOT_FOUND
        .copy_subdir_err:
        ;; AL = error code already set
        add sp, 12
        stc
        jmp .iret_cf
        .copy_data:
        ;; Stack frame finalized. Copy file data sectors.
        ;; src_sec = [bp+6], next_dat = high byte of [bp+0], size = [bp+4]
        xor bx, bx
        mov bl, [bp+6]                 ; BL = src_sec
        xor cx, cx
        mov cl, [bp+0+1]               ; CL = next_data_sec (high byte of word)
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
        add sp, 12
        mov al, ERR_NOT_FOUND
        stc
        jmp .iret_cf
        .copy_sectors_done:
        ;; Re-read the destination entry's sector
        mov al, [bp+0]                 ; AL = dest_entry_sec (low byte)
        mov [dir_loaded_sec], al
        call read_sector
        jc .copy_disk_err
        ;; Compute entry pointer
        mov bx, DISK_BUFFER
        add bx, [bp+2]                 ; BX = DISK_BUFFER + dest_entry_off
        mov si, [bp+10]                ; SI = basename ptr
        ;; Copy name (null-padded to DIR_NAME_LEN)
        push cx
        push bx                        ; save entry base
        call .write_dir_name
        pop bx                         ; BX = entry base
        pop cx
        ;; Write metadata at fixed offsets
        mov al, [bp+8]
        mov [bx+DIR_OFF_FLAGS], al     ; flags
        mov al, [bp+0+1]               ; AL = next_data_sec
        mov [bx+DIR_OFF_SECTOR], al    ; start sector
        mov byte [bx+DIR_OFF_SECTOR+1], 0
        mov ax, [bp+4]
        mov [bx+DIR_OFF_SIZE], ax      ; file size
        call dir_write_back
        add sp, 12                     ; discard full frame (6 words)
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
        call find_file         ; BX = dir entry pointer
        pop si
        pop dx
        jc .create_subdir_err
        test byte [bx+DIR_OFF_FLAGS], FLAG_DIR
        jz .create_subdir_err
        ;; Scan subdirectory for a free entry
        mov al, [bx+DIR_OFF_SECTOR]
        call .subdir_find_free
        jc .create_subdir_pop
        ;; BX = free entry ptr in subdir DISK_BUFFER (current sector)
        pop di                 ; restore '/' position
        mov byte [di], '/'
        inc di                 ; DI = filename after '/'
        pop dx                 ; DL = next data sector
        jmp .create_do_write

        .create_subdir_err:
        mov al, ERR_NOT_FOUND
        .create_subdir_pop:
        ;; AL = error code already set
        pop di
        mov byte [di], '/'
        pop dx
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
        call find_file         ; BX = pointer to entry in DISK_BUFFER
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
        mov word [bx+DIR_OFF_SIZE], DIR_SECTORS * 512
        ;; Write root directory sector back
        call dir_write_back
        jc .iret_cf
        ;; Zero-fill DISK_BUFFER once and write it to each subdir sector
        push dx
        push di
        mov di, DISK_BUFFER
        mov cx, 256
        xor ax, ax
        cld
        rep stosw              ; fill 512 bytes with zeros
        pop di
        pop dx
        push dx
        mov ah, DIR_SECTORS
        .mkdir_zero_loop:
        push ax
        mov al, dl
        call write_sector
        pop ax
        jc .mkdir_zero_err
        inc dl
        dec ah
        jnz .mkdir_zero_loop
        pop dx
        ;; Return allocated sector in AL
        mov al, dl
        clc
        jmp .iret_cf
        .mkdir_zero_err:
        pop dx
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
        ;; Rename file: SI = old name, DI = new name (max 26 chars)
        ;; Both names may contain one '/' but must refer to the same directory.
        ;; On error: CF set, AL = ERR_PROTECTED/ERR_EXISTS/ERR_NOT_FOUND
        ;; Protect shell: cannot be renamed
        call .check_shell
        jne .fs_rename_check_prefix
        mov al, ERR_PROTECTED
        stc
        jmp .iret_cf
        .fs_rename_check_prefix:
        ;; Verify both names have the same directory prefix (or both are root)
        push si
        push di
        push cx
        ;; CX = SI slash offset, or 0FFFFh if no slash
        mov cx, 0FFFFh
        push si
        .rename_pfx_scan_si:
        cmp byte [si], 0
        je .rename_pfx_si_done
        cmp byte [si], '/'
        jne .rename_pfx_si_next
        mov cx, si
        pop ax
        sub cx, ax
        push ax
        jmp .rename_pfx_si_done
        .rename_pfx_si_next:
        inc si
        jmp .rename_pfx_scan_si
        .rename_pfx_si_done:
        pop si
        push cx
        ;; CX = DI slash offset, or 0FFFFh if no slash
        mov cx, 0FFFFh
        push di
        .rename_pfx_scan_di:
        cmp byte [di], 0
        je .rename_pfx_di_done
        cmp byte [di], '/'
        jne .rename_pfx_di_next
        mov cx, di
        pop ax
        sub cx, ax
        push ax
        jmp .rename_pfx_di_done
        .rename_pfx_di_next:
        inc di
        jmp .rename_pfx_scan_di
        .rename_pfx_di_done:
        pop di
        pop ax                 ; AX = SI slash offset
        cmp ax, cx
        jne .rename_pfx_bad    ; different slash positions
        cmp ax, 0FFFFh
        je .rename_pfx_ok      ; both root
        ;; Both have slash at offset AX. Compare CX = AX bytes.
        push si
        push di
        mov cx, ax
        .rename_pfx_cmp:
        mov al, [si]
        cmp al, [di]
        jne .rename_pfx_cmp_bad
        inc si
        inc di
        loop .rename_pfx_cmp
        pop di
        pop si
        jmp .rename_pfx_ok
        .rename_pfx_cmp_bad:
        pop di
        pop si
        .rename_pfx_bad:
        pop cx
        pop di
        pop si
        mov al, ERR_NOT_FOUND
        stc
        jmp .iret_cf
        .rename_pfx_ok:
        pop cx
        pop di
        pop si
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
        ;; find_file preserves SI/DI, so DI still holds new name after the call
        call find_file         ; BX = entry pointer in DISK_BUFFER
        jc .fs_rename_not_found
        jmp .fs_rename_do
        .fs_rename_not_found:
        mov al, ERR_NOT_FOUND
        stc
        jmp .iret_cf
        .fs_rename_do:
        ;; Advance DI past '/' if the new name has one (use basename only)
        push si
        mov si, di
        .rename_basename:
        lodsb
        test al, al
        jz .rename_basename_done
        cmp al, '/'
        jne .rename_basename
        mov di, si             ; SI is one past the '/'
        .rename_basename_done:
        pop si
        ;; Copy basename into the entry
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
        ;; Returns ZF set if SI points to the shell path (null-terminated)
        push si
        push di
        push cx
        cld
        mov di, SHELL_NAME
        mov cx, 10             ; "bin/shell" + null terminator
        repe cmpsb
        pop cx
        pop di
        pop si
        ret

        .subdir_find_free:
        ;; Scan a subdirectory's DIR_SECTORS data sectors for the first
        ;; empty entry.
        ;; Input: AL = subdirectory's first data sector
        ;; Output: CF clear, BX = entry pointer in DISK_BUFFER on success.
        ;;         dir_loaded_sec set to the sector containing the entry.
        ;;         CF set on failure with AL = ERR_NOT_FOUND (read error)
        ;;         or ERR_DIR_FULL (no empty entry).
        ;; Clobbers: AX, BX, CX
        mov ah, DIR_SECTORS
        .sff_loop:
        push ax
        mov [dir_loaded_sec], al
        call read_sector
        pop ax
        jnc .sff_scan_init
        mov al, ERR_NOT_FOUND
        stc
        ret
        .sff_scan_init:
        mov bx, DISK_BUFFER
        mov cx, DIR_MAX_ENTRIES / DIR_SECTORS
        .sff_scan:
        cmp byte [bx], 0
        je .sff_found
        add bx, DIR_ENTRY_SIZE
        loop .sff_scan
        inc al
        dec ah
        jnz .sff_loop
        mov al, ERR_DIR_FULL
        stc
        ret
        .sff_found:
        clc
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
