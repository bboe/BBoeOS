syscall_handler:
        cmp ah, SYS_FS_CHMOD   ; fs_chmod
        je .fs_chmod
        cmp ah, SYS_FS_MKDIR   ; fs_mkdir
        je .fs_mkdir
        cmp ah, SYS_FS_RENAME  ; fs_rename
        je .fs_rename

        cmp ah, SYS_IO_CLOSE   ; io_close
        je .io_close
        cmp ah, SYS_IO_FSTAT   ; io_fstat
        je .io_fstat
        cmp ah, SYS_IO_OPEN    ; io_open
        je .io_open
        cmp ah, SYS_IO_READ    ; io_read
        je .io_read
        cmp ah, SYS_IO_WRITE   ; io_write
        je .io_write

        cmp ah, SYS_NET_ARP    ; net_arp
        je .net_arp
        cmp ah, SYS_NET_INIT   ; net_init
        je .net_init
        cmp ah, SYS_NET_PING   ; net_ping
        je .net_ping
        cmp ah, SYS_NET_RECEIVE   ; net_receive
        je .net_receive
        cmp ah, SYS_NET_SEND   ; net_send
        je .net_send
        cmp ah, SYS_NET_UDP_RECEIVE ; net_udp_receive
        je .net_udp_receive
        cmp ah, SYS_NET_UDP_SEND ; net_udp_send
        je .net_udp_send

        cmp ah, SYS_RTC_DATETIME ; rtc_datetime
        je .rtc_datetime
        cmp ah, SYS_RTC_UPTIME ; rtc_uptime
        je .rtc_uptime

        cmp ah, SYS_VIDEO_MODE    ; video_mode
        je .video_mode

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
        ;; On error: CF set, AL = ERROR_PROTECTED/ERROR_NOT_FOUND
        ;; Protect shell: cannot be chmod'd
        call .check_shell
        jne .fs_chmod_find
        mov al, ERROR_PROTECTED
        stc
        jmp .iret_cf
        .fs_chmod_find:
        push ax                ; Save new flags value
        call find_file         ; BX = entry index
        jnc .fs_chmod_do
        pop ax
        mov al, ERROR_NOT_FOUND
        stc
        jmp .iret_cf
        .fs_chmod_do:
        pop ax                 ; AL = new flags value
        mov [bx+DIRECTORY_OFFSET_FLAGS], al
        call directory_write_back
        jmp .iret_cf


        .fs_mkdir:
        ;; Create subdirectory: SI = name (no slashes)
        ;; On success: CF clear, AX = allocated sector (16-bit)
        ;; On error: CF set, AL = ERROR_EXISTS/ERROR_DIRECTORY_FULL
        ;; Check name doesn't already exist
        call find_file
        jc .mkdir_scan
        mov al, ERROR_EXISTS
        stc
        jmp .iret_cf
        .mkdir_scan:
        mov di, si             ; DI = dirname for later
        call scan_directory_entries  ; BX = free entry index, DX = next data sector (16)
        cmp bx, 0FFFFh
        jne .mkdir_write_entry
        mov al, ERROR_DIRECTORY_FULL
        stc
        jmp .iret_cf
        .mkdir_write_entry:
        ;; Load the root sector containing the free entry
        push dx
        call directory_load_entry    ; BX = entry ptr (uses root index from scan)
        pop dx
        ;; Write directory name, null-padded
        push dx
        push bx
        mov si, di
        call write_directory_name
        pop bx                 ; BX = entry base
        pop dx                 ; DX = next free sector (16-bit)
        mov byte [bx+DIRECTORY_OFFSET_FLAGS], FLAG_DIRECTORY
        mov [bx+DIRECTORY_OFFSET_SECTOR], dx
        mov word [bx+DIRECTORY_OFFSET_SIZE], DIRECTORY_SECTORS * 512
        mov word [bx+DIRECTORY_OFFSET_SIZE+2], 0
        ;; Write root directory sector back
        call directory_write_back
        jc .iret_cf
        ;; Zero-fill SECTOR_BUFFER once and write it to each subdir sector
        push dx
        push di
        mov di, SECTOR_BUFFER
        mov cx, 256
        xor ax, ax
        cld
        rep stosw              ; fill 512 bytes with zeros
        pop di
        pop dx
        push dx
        mov cx, DIRECTORY_SECTORS
        mov ax, dx             ; AX = first subdir sector
        .mkdir_zero_loop:
        push ax
        push cx
        call write_sector
        pop cx
        pop ax
        jc .mkdir_zero_err
        inc ax
        loop .mkdir_zero_loop
        pop dx
        ;; Return allocated sector in AX (16-bit)
        mov ax, dx
        clc
        jmp .iret_cf
        .mkdir_zero_err:
        pop dx
        jmp .iret_cf

        .fs_rename:
        ;; Rename file: SI = old name, DI = new name (max 26 chars)
        ;; Both names may contain one '/' but must refer to the same directory.
        ;; On error: CF set, AL = ERROR_PROTECTED/ERROR_EXISTS/ERROR_NOT_FOUND
        ;; Protect shell: cannot be renamed
        call .check_shell
        jne .fs_rename_check_prefix
        mov al, ERROR_PROTECTED
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
        jmp .fs_rename_cross
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
        mov al, ERROR_EXISTS
        stc
        jmp .iret_cf
        .fs_rename_find_old:
        ;; find_file preserves SI/DI, so DI still holds new name after the call
        call find_file         ; BX = entry pointer in SECTOR_BUFFER
        jc .fs_rename_not_found
        jmp .fs_rename_do
        .fs_rename_not_found:
        mov al, ERROR_NOT_FOUND
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
        call write_directory_name
        pop si
        pop cx
        call directory_write_back
        jmp .iret_cf

        .fs_rename_cross:
        ;; Cross-directory rename: src in one directory, dest in another.
        ;; Stack at entry (from .fs_rename_check_prefix): [SI old], [DI new], [CX]
        pop cx
        pop di
        pop si
        ;; Check that the new name doesn't already exist
        push si
        mov si, di
        call find_file
        pop si
        jc .frc_find_old
        mov al, ERROR_EXISTS
        stc
        jmp .iret_cf
        .frc_find_old:
        call find_file         ; BX = src entry pointer in SECTOR_BUFFER
        jnc .frc_got_src
        mov al, ERROR_NOT_FOUND
        stc
        jmp .iret_cf
        .frc_got_src:
        ;; Build frame. Layout (BP = SP after pushes):
        ;;   [bp+12] basename ptr (within new path)
        ;;   [bp+10] src_sec      [bp+8]  size_lo  [bp+6]  size_hi
        ;;   [bp+4]  flags        [bp+2]  src_dir_sec  [bp+0] src_entry_off
        push di                ; [bp+12]
        mov ax, [bx+DIRECTORY_OFFSET_SECTOR]
        push ax                ; [bp+10]
        mov ax, [bx+DIRECTORY_OFFSET_SIZE]
        push ax                ; [bp+8]
        mov ax, [bx+DIRECTORY_OFFSET_SIZE+2]
        push ax                ; [bp+6]
        xor ax, ax
        mov al, [bx+DIRECTORY_OFFSET_FLAGS]
        push ax                ; [bp+4]
        mov ax, [directory_loaded_sector]
        push ax                ; [bp+2]
        mov ax, bx
        sub ax, SECTOR_BUFFER
        push ax                ; [bp+0]
        mov bp, sp
        ;; Resolve destination directory by scanning new path for '/'
        mov di, [bp+12]
        .frc_scan:
        mov al, [di]
        test al, al
        jz .frc_dst_root
        cmp al, '/'
        je .frc_dst_subdir
        inc di
        jmp .frc_scan
        .frc_dst_root:
        mov ax, DIRECTORY_SECTOR
        jmp .frc_alloc
        .frc_dst_subdir:
        mov byte [di], 0
        push di
        mov si, [bp+12]
        call find_file         ; BX = subdir entry in SECTOR_BUFFER
        pop di
        mov byte [di], '/'
        jc .frc_bad_dir
        test byte [bx+DIRECTORY_OFFSET_FLAGS], FLAG_DIRECTORY
        jz .frc_bad_dir
        mov ax, [bx+DIRECTORY_OFFSET_SECTOR]
        inc di                 ; basename = char after '/'
        mov [bp+12], di
        jmp .frc_alloc
        .frc_bad_dir:
        add sp, 14
        mov al, ERROR_NOT_FOUND
        stc
        jmp .iret_cf
        .frc_alloc:
        call subdir_find_free ; BX = entry ptr; directory_loaded_sector set
        jnc .frc_write
        add sp, 14
        stc
        jmp .iret_cf
        .frc_write:
        push bx
        mov si, [bp+12]
        call write_directory_name
        pop bx
        mov al, [bp+4]
        mov [bx+DIRECTORY_OFFSET_FLAGS], al
        mov ax, [bp+10]
        mov [bx+DIRECTORY_OFFSET_SECTOR], ax
        mov ax, [bp+8]
        mov [bx+DIRECTORY_OFFSET_SIZE], ax
        mov ax, [bp+6]
        mov [bx+DIRECTORY_OFFSET_SIZE+2], ax
        call directory_write_back
        jc .frc_disk_err
        ;; Re-read src directory sector and zero the source entry
        mov ax, [bp+2]
        mov [directory_loaded_sector], ax
        call read_sector
        jc .frc_disk_err
        mov bx, SECTOR_BUFFER
        add bx, [bp+0]
        push di
        push cx
        mov di, bx
        mov cx, DIRECTORY_ENTRY_SIZE / 2
        xor ax, ax
        cld
        rep stosw
        pop cx
        pop di
        call directory_write_back
        jc .frc_disk_err
        add sp, 14
        clc
        jmp .iret_cf
        .frc_disk_err:
        add sp, 14
        mov al, ERROR_NOT_FOUND
        stc
        jmp .iret_cf

        .io_close:
        ;; Close fd: BX = fd
        ;; CF on error
        call fd_close
        jmp .iret_cf

        .io_fstat:
        ;; Get file status: BX = fd
        ;; Returns AL = mode (permission flags), CX:DX = size (32-bit)
        ;; CF on error
        call fd_fstat
        jmp .iret_cf

        .io_open:
        ;; Open file/device: SI = filename, AL = flags
        ;; Returns AX = fd, CF on error
        call fd_open
        jmp .iret_cf

        .io_read:
        ;; Read from fd: BX = fd, DI = buffer, CX = count
        ;; Returns AX = bytes read (0 = EOF), CF on error
        call fd_read
        jmp .iret_cf

        .io_write:
        ;; Write to fd: BX = fd, SI = buffer, CX = count
        ;; Returns AX = bytes written, or -1 on error
        call fd_write
        jmp .iret_cf

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
        mov si, mac_address
        mov cx, 3              ; 6 bytes = 3 words
        rep movsw
        pop cx
        pop si
        clc
        jmp .iret_cf

        .net_ping:
        call icmp_ping
        jmp .iret_cf

        .net_receive:
        call ne2k_receive
        jmp .iret_cf

        .net_send:
        call ne2k_send
        jmp .iret_cf

        .net_udp_receive:
        call udp_receive
        jmp .iret_cf

        .net_udp_send:
        call udp_send
        jmp .iret_cf

        .rtc_datetime:
        ;; Returns DX:AX = unsigned seconds since 1970-01-01 00:00:00 UTC.
        ;; Gregorian leap rule. Valid through 2106 (32-bit seconds).
        push bx
        push cx
        push si
        push di
        push ebx
        push ecx
        push esi

        mov ah, 04h
        int 1Ah                 ; CH=century BCD, CL=year BCD, DH=month BCD, DL=day BCD
        mov al, ch
        call .bcd_to_bin
        movzx si, al            ; SI = century
        imul si, si, 100        ; SI = century * 100
        mov al, cl
        call .bcd_to_bin
        movzx bx, al
        add si, bx              ; SI = full year
        mov [epoch_year], si
        mov al, dh
        call .bcd_to_bin
        mov [epoch_month], al
        mov al, dl
        call .bcd_to_bin
        mov [epoch_day], al

        mov ah, 02h
        int 1Ah                 ; CH=hours BCD, CL=minutes BCD, DH=seconds BCD
        mov al, ch
        call .bcd_to_bin
        mov [epoch_hours], al
        mov al, cl
        call .bcd_to_bin
        mov [epoch_minutes], al
        mov al, dh
        call .bcd_to_bin
        mov [epoch_seconds], al

        ;; Days from 1970-01-01 to the first of epoch_year.
        xor esi, esi            ; ESI = day accumulator
        mov cx, 1970
        .rtc_year_loop:
        cmp cx, [epoch_year]
        jae .rtc_year_done
        mov ax, cx
        call .is_leap_year
        jz .rtc_year_leap
        add esi, 365
        jmp .rtc_year_next
        .rtc_year_leap:
        add esi, 366
        .rtc_year_next:
        inc cx
        jmp .rtc_year_loop
        .rtc_year_done:

        ;; Add cumulative days before the first of epoch_month.
        movzx bx, byte [epoch_month]
        dec bx
        shl bx, 1
        movzx eax, word [.month_days + bx]
        add esi, eax

        ;; If current year is leap and month > 2, add one extra day for Feb 29.
        cmp byte [epoch_month], 2
        jbe .rtc_skip_leap_adj
        mov ax, [epoch_year]
        call .is_leap_year
        jnz .rtc_skip_leap_adj
        inc esi
        .rtc_skip_leap_adj:

        ;; Add day-of-month minus 1.
        movzx eax, byte [epoch_day]
        dec eax
        add esi, eax

        ;; seconds = days*86400 + h*3600 + m*60 + s
        mov eax, esi
        mov ecx, 86400
        mul ecx                 ; EDX:EAX = EAX * ECX (EDX discarded; fits in 32 bits through 2106)
        movzx ebx, byte [epoch_hours]
        imul ebx, ebx, 3600
        add eax, ebx
        movzx ebx, byte [epoch_minutes]
        imul ebx, ebx, 60
        add eax, ebx
        movzx ebx, byte [epoch_seconds]
        add eax, ebx

        ;; Return DX:AX = EAX
        pop esi
        pop ecx
        pop ebx
        mov edx, eax
        shr edx, 16             ; DX = high 16
        pop di
        pop si
        pop cx
        pop bx
        iret

        .bcd_to_bin:
        ;; AL (BCD) -> AL (binary). Clobbers AX.
        push cx
        mov cl, al
        shr al, 4
        mov ch, 10
        mul ch                  ; AX = high_nibble * 10
        and cl, 0Fh
        add al, cl
        pop cx
        ret

        .is_leap_year:
        ;; AX = year. Returns ZF=1 if leap, ZF=0 otherwise.
        ;; Preserves CX, EAX beyond low word. Clobbers AX, DX.
        push cx
        push ax
        xor dx, dx
        mov cx, 4
        div cx                  ; DX = year % 4
        test dx, dx
        jnz .leap_no_pop
        pop ax
        push ax
        xor dx, dx
        mov cx, 100
        div cx                  ; DX = year % 100
        test dx, dx
        jnz .leap_yes_pop       ; div 4, not div 100 -> leap
        pop ax
        push ax
        xor dx, dx
        mov cx, 400
        div cx                  ; DX = year % 400
        test dx, dx
        jnz .leap_no_pop        ; div 100, not div 400 -> not leap
        .leap_yes_pop:
        pop ax
        pop cx
        xor ax, ax              ; ZF=1
        ret
        .leap_no_pop:
        pop ax
        pop cx
        or ax, 1                ; ZF=0
        ret

        .month_days:
        dw 0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334

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

        .video_mode:
        ;; Set video mode: AL = mode; clears serial and screen
        push ax
        mov al, `\r`
        call serial_character
        mov al, 0Ch             ; Form feed
        call serial_character
        pop ax
        xor ah, ah              ; INT 10h AH=00h set mode (AL), clears screen
        int 10h
        iret

        .sys_exec:
        ;; Execute program: SI = filename
        ;; Saves shell stack, loads program at PROGRAM_BASE, jumps to it
        ;; On error: CF set, AL = ERROR_NOT_FOUND or ERROR_NOT_EXECUTE
        call find_file
        jc .exec_not_found
        jmp .exec_check_flag
        .exec_not_found:
        mov al, ERROR_NOT_FOUND
        stc
        jmp .iret_cf
        .exec_check_flag:
        test byte [bx+DIRECTORY_OFFSET_FLAGS], FLAG_EXECUTE
        jnz .exec_load
        mov al, ERROR_NOT_EXECUTE
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
        mov al, ERROR_NOT_FOUND
        stc
        jmp .iret_cf
        .exec_run:
        call fd_init
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

        subdir_find_free:
        ;; Scan a subdirectory's DIRECTORY_SECTORS data sectors for the first
        ;; empty entry.
        ;; Input: AX = subdirectory's first data sector (16-bit)
        ;; Output: CF clear, BX = entry pointer in SECTOR_BUFFER on success.
        ;;         directory_loaded_sector set to the sector containing the entry.
        ;;         CF set on failure with AL = ERROR_NOT_FOUND (read error)
        ;;         or ERROR_DIRECTORY_FULL (no empty entry).
        ;; Clobbers: AX, BX, CX, DX
        mov dx, DIRECTORY_SECTORS
        .sff_loop:
        push ax
        push dx
        mov [directory_loaded_sector], ax
        call read_sector
        pop dx
        pop ax
        jnc .sff_scan_init
        mov al, ERROR_NOT_FOUND
        stc
        ret
        .sff_scan_init:
        mov bx, SECTOR_BUFFER
        mov cx, DIRECTORY_MAX_ENTRIES / DIRECTORY_SECTORS
        .sff_scan:
        cmp byte [bx], 0
        je .sff_found
        add bx, DIRECTORY_ENTRY_SIZE
        loop .sff_scan
        inc ax
        dec dx
        jnz .sff_loop
        mov al, ERROR_DIRECTORY_FULL
        stc
        ret
        .sff_found:
        clc
        ret

        write_directory_name:
        ;; Copy null-terminated name from SI into entry at BX, padding with
        ;; zeros up to DIRECTORY_NAME_LENGTH - 1 bytes total. SI is advanced past the
        ;; null terminator and BX is advanced DIRECTORY_NAME_LENGTH - 1 bytes.
        ;; Clobbers: AX, BX (advanced), CX, SI (advanced)
        mov cx, DIRECTORY_NAME_LENGTH - 1
        .copy:
        mov al, [si]
        test al, al
        jz .pad
        inc si
        mov [bx], al
        inc bx
        dec cx
        jnz .copy
        ret
        .pad:
        mov byte [bx], 0
        inc bx
        dec cx
        jnz .pad
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
