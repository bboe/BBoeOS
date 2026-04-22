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

