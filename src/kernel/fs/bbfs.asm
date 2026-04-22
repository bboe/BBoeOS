;;; fs/bbfs.asm -- BBoeOS custom filesystem (flat directory + contiguous files)
;;;
;;; VFS interface (called through vfs.asm function pointers):
;;; bbfs_chmod:       SI=path, AL=mode → CF on error (AL=error code)
;;; bbfs_create:      SI=path → vfs_found_*, CF on error
;;; bbfs_find:        SI=path → vfs_found_*, CF if not found
;;; bbfs_init:        → (no-op: no persistent state to initialise)
;;; bbfs_load:        DI=dest → CF (loads file using vfs_found_inode + vfs_found_size)
;;; bbfs_mkdir:       SI=name → AX=allocated sector, CF on error
;;; bbfs_read_sec:    SI=fd_entry → SECTOR_BUFFER filled, BX=byte offset; CF on err
;;; bbfs_rename:      SI=old, DI=new → CF on error (AL=error code)
;;; bbfs_update_size: SI=fd_entry → CF on disk error

bbfs_chmod:
        ;; Change a file's flags byte
        ;; Input:  SI = path, AL = new flags
        ;; Output: CF clear on success; CF set, AL = error code on failure
        push bx
        push si
        push ax                     ; save new flags
        call find_file              ; BX = dir entry in SECTOR_BUFFER
        jnc .do_chmod
        pop ax
        pop si
        pop bx
        mov al, ERROR_NOT_FOUND
        stc
        ret
        .do_chmod:
        pop ax                      ; restore new flags
        mov [bx+DIRECTORY_OFFSET_FLAGS], al
        call directory_write_back
        pop si
        pop bx
        ret

bbfs_create:
        ;; Create a new empty file entry and populate vfs_found_*
        ;; Input:  SI = null-terminated path (may contain one '/')
        ;; Output: CF clear, vfs_found_* set; CF set on error
        push bx
        push cx
        push dx
        push di
        push si
        mov [bbfs_create_name], si
        call scan_directory_entries   ; BX = free root entry index, DX = next data sector
        mov [bbfs_create_sector], dx
        ;; scan_directory_entries clobbers SI; restore path pointer from saved variable
        mov di, [bbfs_create_name]
        .bc_slash:
        mov al, [di]
        test al, al
        jz .bc_root
        cmp al, '/'
        je .bc_subdir
        inc di
        jmp .bc_slash
        .bc_root:
        cmp bx, 0FFFFh
        je .bc_full
        call directory_load_entry     ; BX = entry ptr in SECTOR_BUFFER
        mov si, [bbfs_create_name]
        jmp .bc_write
        .bc_subdir:
        ;; Null-terminate dir component, find subdir, then find a free slot in it
        mov byte [di], 0
        push di
        mov si, [bbfs_create_name]
        call find_file                ; BX = subdir entry in SECTOR_BUFFER
        pop di
        mov byte [di], '/'
        jc .bc_err
        test byte [bx+DIRECTORY_OFFSET_FLAGS], FLAG_DIRECTORY
        jz .bc_err
        mov ax, [bx+DIRECTORY_OFFSET_SECTOR]
        call subdir_find_free         ; BX = free entry ptr, directory_loaded_sector set
        jc .bc_full
        inc di                        ; SI = basename (past '/')
        mov si, di
        .bc_write:
        push bx
        call write_directory_name
        pop bx
        mov byte [bx+DIRECTORY_OFFSET_FLAGS], 0
        mov ax, [bbfs_create_sector]
        mov [bx+DIRECTORY_OFFSET_SECTOR], ax
        mov word [bx+DIRECTORY_OFFSET_SIZE], 0
        mov word [bx+DIRECTORY_OFFSET_SIZE+2], 0
        call directory_write_back
        jc .bc_err
        ;; Populate vfs_found_*
        mov ax, [bbfs_create_sector]
        mov [vfs_found_inode], ax
        mov word [vfs_found_size], 0
        mov word [vfs_found_size+2], 0
        mov byte [vfs_found_mode], 0
        mov byte [vfs_found_type], FD_TYPE_FILE
        mov ax, [directory_loaded_sector]
        mov [vfs_found_dir_sec], ax
        mov ax, bx
        sub ax, SECTOR_BUFFER
        mov [vfs_found_dir_off], ax
        pop si
        pop di
        pop dx
        pop cx
        pop bx
        clc
        ret
        .bc_full:
        pop si
        pop di
        pop dx
        pop cx
        pop bx
        mov al, ERROR_DIRECTORY_FULL
        stc
        ret
        .bc_err:
        pop si
        pop di
        pop dx
        pop cx
        pop bx
        stc
        ret

        bbfs_create_name   dw 0
        bbfs_create_sector dw 0

bbfs_find:
        ;; Find a file (or "." root) and populate vfs_found_*
        ;; Input:  SI = null-terminated path (may contain one '/')
        ;; Output: CF clear, vfs_found_* set; CF set if not found
        push bx
        ;; Handle "." — synthesise root directory entry
        cmp byte [si], '.'
        jne .normal_find
        cmp byte [si+1], 0
        jne .normal_find
        mov word [vfs_found_inode], DIRECTORY_SECTOR
        mov word [vfs_found_size], DIRECTORY_SECTORS * 512
        mov word [vfs_found_size+2], 0
        mov byte [vfs_found_mode], FLAG_DIRECTORY
        mov byte [vfs_found_type], FD_TYPE_DIRECTORY
        mov word [vfs_found_dir_sec], 0
        mov word [vfs_found_dir_off], 0
        pop bx
        clc
        ret
        .normal_find:
        call find_file          ; BX = dir entry pointer in SECTOR_BUFFER, CF on miss
        jnc .found
        pop bx
        stc
        ret
        .found:
        mov ax, [bx+DIRECTORY_OFFSET_SECTOR]
        mov [vfs_found_inode], ax
        mov ax, [bx+DIRECTORY_OFFSET_SIZE]
        mov [vfs_found_size], ax
        mov ax, [bx+DIRECTORY_OFFSET_SIZE+2]
        mov [vfs_found_size+2], ax
        mov al, [bx+DIRECTORY_OFFSET_FLAGS]
        mov [vfs_found_mode], al
        mov byte [vfs_found_type], FD_TYPE_FILE
        test al, FLAG_DIRECTORY
        jz .set_dir_info
        mov byte [vfs_found_type], FD_TYPE_DIRECTORY
        .set_dir_info:
        mov ax, [directory_loaded_sector]
        mov [vfs_found_dir_sec], ax
        mov ax, bx
        sub ax, SECTOR_BUFFER
        mov [vfs_found_dir_off], ax
        pop bx
        clc
        ret

bbfs_init:
        ;; Nothing to initialise for the flat BBoeOS filesystem
        ret

bbfs_load:
        ;; Load file into memory using vfs_found_inode (start sector) and vfs_found_size
        ;; Input:  DI = destination address
        ;; Output: CF set on disk error
        ;; Clobbers: AX, BX, CX, SI
        push bx
        push cx
        push si
        mov bx, [vfs_found_inode]   ; start sector
        mov cx, [vfs_found_size]    ; file size (low 16 bits; executables fit in 64 KB)
        .load_sector:
        mov ax, bx
        call read_sector
        jc .load_done
        push cx
        cmp cx, 512
        jbe .partial
        mov cx, 256                 ; full sector = 256 words
        jmp .copy
        .partial:
        inc cx
        shr cx, 1
        .copy:
        cld
        mov si, SECTOR_BUFFER
        rep movsw
        pop cx
        sub cx, 512
        jbe .loaded
        inc bx
        jmp .load_sector
        .loaded:
        clc
        .load_done:
        pop si
        pop cx
        pop bx
        ret

bbfs_mkdir:
        ;; Create a subdirectory entry and zero its data sectors
        ;; Input:  SI = name (no slashes, max 24 chars)
        ;; Output: AX = allocated sector (16-bit), CF clear on success
        ;;         CF set, AL = error code on failure
        push bx
        push cx
        push dx
        push di
        push si
        ;; Reject if the name already exists
        call find_file
        jc .bbmkdir_scan
        pop si
        pop di
        pop dx
        pop cx
        pop bx
        mov al, ERROR_EXISTS
        stc
        ret
        .bbmkdir_scan:
        mov di, si                      ; DI = dirname (for write_directory_name later)
        call scan_directory_entries     ; BX = free root entry index, DX = next data sector
        cmp bx, 0FFFFh
        jne .bbmkdir_write
        pop si
        pop di
        pop dx
        pop cx
        pop bx
        mov al, ERROR_DIRECTORY_FULL
        stc
        ret
        .bbmkdir_write:
        push dx                         ; save next data sector
        call directory_load_entry       ; BX = entry ptr in SECTOR_BUFFER
        pop dx
        push dx
        push bx
        mov si, di
        call write_directory_name
        pop bx
        pop dx
        mov byte [bx+DIRECTORY_OFFSET_FLAGS], FLAG_DIRECTORY
        mov [bx+DIRECTORY_OFFSET_SECTOR], dx
        mov word [bx+DIRECTORY_OFFSET_SIZE], DIRECTORY_SECTORS * 512
        mov word [bx+DIRECTORY_OFFSET_SIZE+2], 0
        call directory_write_back
        jc .bbmkdir_disk_err
        ;; Zero-fill SECTOR_BUFFER and write to each subdir sector
        push dx
        push di
        mov di, SECTOR_BUFFER
        mov cx, 256
        xor ax, ax
        cld
        rep stosw
        pop di
        pop dx
        push dx
        mov cx, DIRECTORY_SECTORS
        mov ax, dx
        .bbmkdir_zero_loop:
        push ax
        push cx
        call write_sector
        pop cx
        pop ax
        jc .bbmkdir_zero_err
        inc ax
        loop .bbmkdir_zero_loop
        pop dx
        mov ax, dx                      ; return allocated sector in AX
        pop si
        pop di
        pop dx
        pop cx
        pop bx
        clc
        ret
        .bbmkdir_zero_err:
        pop dx
        .bbmkdir_disk_err:
        pop si
        pop di
        pop dx
        pop cx
        pop bx
        stc
        ret

bbfs_rename:
        ;; Rename (or cross-directory move) a file
        ;; Input:  SI = old path, DI = new path
        ;; Output: CF clear on success; CF set, AL = error code on failure
        ;; Verify both names share the same directory prefix (or both are root)
        push si
        push di
        push cx
        ;; CX = byte offset of '/' in SI, or 0FFFFh if none
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
        ;; CX = byte offset of '/' in DI, or 0FFFFh if none
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
        pop ax                          ; AX = SI slash offset
        cmp ax, cx
        jne .rename_pfx_bad             ; different slash positions → cross-dir
        cmp ax, 0FFFFh
        je .rename_pfx_ok               ; both root
        ;; Both have slash at offset AX; compare that many bytes
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
        jmp .rename_cross
        .rename_pfx_ok:
        pop cx
        pop di
        pop si
        ;; Check new name doesn't already exist
        push si
        mov si, di
        call find_file
        pop si
        jc .rename_find_old             ; new name not found → good, proceed
        mov al, ERROR_EXISTS
        stc
        ret
        .rename_find_old:
        call find_file                  ; BX = entry of old name
        jc .rename_not_found
        jmp .rename_do
        .rename_not_found:
        mov al, ERROR_NOT_FOUND
        stc
        ret
        .rename_do:
        ;; Advance DI past '/' to the basename of the new name
        push si
        mov si, di
        .rename_basename:
        lodsb
        test al, al
        jz .rename_basename_done
        cmp al, '/'
        jne .rename_basename
        mov di, si                      ; one past '/'
        .rename_basename_done:
        pop si
        push cx
        push si
        mov si, di
        call write_directory_name
        pop si
        pop cx
        call directory_write_back
        ret                             ; CF from write_back

        .rename_cross:
        ;; Cross-directory rename — stack still has [SI old], [DI new], [CX]
        pop cx
        pop di
        pop si
        ;; Check new name doesn't already exist
        push si
        mov si, di
        call find_file
        pop si
        jc .frc_find_old
        mov al, ERROR_EXISTS
        stc
        ret
        .frc_find_old:
        call find_file                  ; BX = src entry in SECTOR_BUFFER
        jnc .frc_got_src
        mov al, ERROR_NOT_FOUND
        stc
        ret
        .frc_got_src:
        ;; Build frame.  Layout (BP = SP after 7 words pushed):
        ;;   [bp+12] basename ptr  [bp+10] src_sec  [bp+8] size_lo
        ;;   [bp+6]  size_hi       [bp+4]  flags     [bp+2] src_dir_sec
        ;;   [bp+0]  src_entry_off
        push di                         ; [bp+12]
        mov ax, [bx+DIRECTORY_OFFSET_SECTOR]
        push ax                         ; [bp+10]
        mov ax, [bx+DIRECTORY_OFFSET_SIZE]
        push ax                         ; [bp+8]
        mov ax, [bx+DIRECTORY_OFFSET_SIZE+2]
        push ax                         ; [bp+6]
        xor ax, ax
        mov al, [bx+DIRECTORY_OFFSET_FLAGS]
        push ax                         ; [bp+4]
        mov ax, [directory_loaded_sector]
        push ax                         ; [bp+2]
        mov ax, bx
        sub ax, SECTOR_BUFFER
        push ax                         ; [bp+0]
        mov bp, sp
        ;; Locate the destination directory
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
        call find_file                  ; BX = subdir entry
        pop di
        mov byte [di], '/'
        jc .frc_bad_dir
        test byte [bx+DIRECTORY_OFFSET_FLAGS], FLAG_DIRECTORY
        jz .frc_bad_dir
        mov ax, [bx+DIRECTORY_OFFSET_SECTOR]
        inc di                          ; basename = char after '/'
        mov [bp+12], di
        jmp .frc_alloc
        .frc_bad_dir:
        add sp, 14
        mov al, ERROR_NOT_FOUND
        stc
        ret
        .frc_alloc:
        call subdir_find_free           ; BX = entry ptr; directory_loaded_sector set
        jnc .frc_write
        add sp, 14
        stc
        ret
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
        ;; Re-read the source directory sector and zero the original entry
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
        ret
        .frc_disk_err:
        add sp, 14
        mov al, ERROR_NOT_FOUND
        stc
        ret

bbfs_update_size:
        ;; Write fd position back to the directory entry as the file size
        ;; Input:  SI = fd_table entry pointer
        ;; Output: CF set on disk error
        push ax
        push bx
        push cx
        push dx
        mov ax, [si+FD_OFFSET_DIRECTORY_SECTOR]
        mov [directory_loaded_sector], ax
        call read_sector
        jc .us_err
        mov bx, SECTOR_BUFFER
        add bx, [si+FD_OFFSET_DIRECTORY_OFFSET]
        mov ax, [si+FD_OFFSET_POSITION]
        mov [bx+DIRECTORY_OFFSET_SIZE], ax
        mov ax, [si+FD_OFFSET_POSITION+2]
        mov [bx+DIRECTORY_OFFSET_SIZE+2], ax
        call directory_write_back
        pop dx
        pop cx
        pop bx
        pop ax
        ret
        .us_err:
        pop dx
        pop cx
        pop bx
        pop ax
        stc
        ret

;;; -----------------------------------------------------------------------
;;; Internal helpers (not called directly by the rest of the kernel)
;;; -----------------------------------------------------------------------

directory_load_entry:
        ;; Load a root directory entry by index and return a pointer to it
        ;; Input:  BX = entry index (0 to DIRECTORY_MAX_ENTRIES-1)
        ;; Output: BX = pointer to entry in SECTOR_BUFFER
        ;; Side effect: directory_loaded_sector set; SECTOR_BUFFER updated
        push ax
        push cx
        mov ax, bx
        mov cl, 4                       ; 16 entries per sector = 2^4
        shr ax, cl
        add ax, DIRECTORY_SECTOR
        mov [directory_loaded_sector], ax
        mov ax, bx
        and ax, 0Fh                     ; index % 16
        mov cl, 5                       ; DIRECTORY_ENTRY_SIZE = 32 = 2^5
        shl ax, cl
        mov bx, ax
        add bx, SECTOR_BUFFER
        mov ax, [directory_loaded_sector]
        call read_sector
        pop cx
        pop ax
        ret

directory_write_back:
        ;; Write the sector last loaded by directory_load_entry or find_file
        ;; Output: CF set on disk error
        push ax
        mov ax, [directory_loaded_sector]
        call write_sector
        pop ax
        ret

        directory_loaded_sector  dw 0
        directory_search_start   dw 0

find_file:
        ;; Search directory for a filename, with optional subdirectory path support
        ;; Input:  SI = null-terminated filename (may contain one '/')
        ;; Output: BX = pointer to entry in SECTOR_BUFFER; CF set if not found
        ;; Side effect: directory_loaded_sector set to the sector of the found entry
        push ax
        push cx
        push dx
        push si
        push di
        ;; Scan for '/' to detect a subdirectory path
        mov di, si
        .ff_scan_slash:
        mov al, [di]
        test al, al
        jz .ff_no_slash
        cmp al, '/'
        je .ff_has_slash
        inc di
        jmp .ff_scan_slash
        .ff_no_slash:
        mov dx, si
        mov word [directory_search_start], DIRECTORY_SECTOR
        jmp .ff_search_root
        .ff_has_slash:
        ;; Split path at '/': DI points to '/'
        mov byte [di], 0
        push di
        mov dx, si
        push dx
        call .ff_do_root_search
        pop dx
        pop di
        mov byte [di], '/'
        jc .ff_done
        test byte [bx+DIRECTORY_OFFSET_FLAGS], FLAG_DIRECTORY
        jz .ff_not_found
        mov ax, [bx+DIRECTORY_OFFSET_SECTOR]
        mov [directory_search_start], ax
        inc di
        mov dx, di
        xor bx, bx
        jmp .ff_load_sector
        .ff_search_root:
        xor bx, bx
        mov ax, DIRECTORY_SECTOR
        .ff_load_sector:
        mov [directory_loaded_sector], ax
        call read_sector
        jc .ff_done
        mov di, SECTOR_BUFFER
        mov cx, DIRECTORY_MAX_ENTRIES / DIRECTORY_SECTORS
        .ff_search:
        cmp byte [di], 0
        je .ff_skip_entry
        mov si, dx
        push di
        .ff_cmp:
        mov al, [si]
        cmp al, [di]
        jne .ff_no_match
        test al, al
        jz .ff_found
        inc si
        inc di
        jmp .ff_cmp
        .ff_no_match:
        pop di
        .ff_skip_entry:
        add di, DIRECTORY_ENTRY_SIZE
        inc bx
        loop .ff_search
        .ff_try_next_sector:
        mov ax, [directory_loaded_sector]
        sub ax, [directory_search_start]
        inc ax
        cmp ax, DIRECTORY_SECTORS
        jae .ff_not_found
        mov ax, [directory_loaded_sector]
        inc ax
        jmp .ff_load_sector
        .ff_not_found:
        stc
        .ff_done:
        pop di
        pop si
        pop dx
        pop cx
        pop ax
        ret
        .ff_found:
        pop bx
        clc
        jmp .ff_done
        .ff_do_root_search:
        ;; Helper: search root directory for name in DX → BX = entry ptr, CF on miss
        xor bx, bx
        mov al, DIRECTORY_SECTOR
        .fdr_load:
        xor ah, ah
        call read_sector
        jc .fdr_done
        mov di, SECTOR_BUFFER
        mov cx, DIRECTORY_MAX_ENTRIES / DIRECTORY_SECTORS
        .fdr_search:
        cmp byte [di], 0
        je .fdr_skip
        mov si, dx
        push di
        .fdr_cmp:
        mov al, [si]
        cmp al, [di]
        jne .fdr_no_match
        test al, al
        jz .fdr_found
        inc si
        inc di
        jmp .fdr_cmp
        .fdr_no_match:
        pop di
        .fdr_skip:
        add di, DIRECTORY_ENTRY_SIZE
        inc bx
        loop .fdr_search
        .fdr_try_next:
        add bx, cx
        mov al, bl
        shr al, 4
        add al, DIRECTORY_SECTOR
        cmp al, DIRECTORY_SECTOR + DIRECTORY_SECTORS
        jb .fdr_load
        stc
        .fdr_done:
        ret
        .fdr_found:
        pop bx
        clc
        ret

load_file:
        ;; Load file sectors into memory (used internally)
        ;; Input:  BX = pointer to directory entry in SECTOR_BUFFER
        ;;         DI = destination address
        ;; Output: CF set on disk error
        ;; Clobbers: AX, BX, CX, SI, DI
        mov cx, [bx+DIRECTORY_OFFSET_SIZE]
        mov bx, [bx+DIRECTORY_OFFSET_SECTOR]
        .lf_sector:
        mov ax, bx
        call read_sector
        jc .lf_done
        push cx
        cmp cx, 512
        jbe .lf_partial
        mov cx, 256
        jmp .lf_copy
        .lf_partial:
        inc cx
        shr cx, 1
        .lf_copy:
        cld
        mov si, SECTOR_BUFFER
        rep movsw
        pop cx
        sub cx, 512
        jbe .lf_loaded
        inc bx
        jmp .lf_sector
        .lf_loaded:
        clc
        .lf_done:
        ret

scan_directory_entries:
        ;; Scan all directory sectors for the first free root entry and next data sector
        ;; Output: BX = first free root entry index (0xFFFF if full)
        ;;         DX = next free data sector (16-bit)
        ;; Clobbers: AX, CX, SI
        push di
        mov bx, 0FFFFh
        mov dx, DIRECTORY_SECTOR + DIRECTORY_SECTORS
        xor di, di
        mov al, DIRECTORY_SECTOR
        .sd_next_sector:
        xor ah, ah
        call read_sector
        jc .sd_done
        mov si, SECTOR_BUFFER
        mov cx, DIRECTORY_MAX_ENTRIES / DIRECTORY_SECTORS
        .sd_entry:
        cmp byte [si], 0
        jne .sd_occupied
        cmp bx, 0FFFFh
        jne .sd_skip
        mov bx, di
        jmp .sd_skip
        .sd_occupied:
        push ax
        push bx
        push cx
        mov ax, [si+DIRECTORY_OFFSET_SIZE]
        add ax, 511
        mov bx, [si+DIRECTORY_OFFSET_SIZE+2]
        adc bx, 0
        mov cl, 9
        .sd_sh_loop:
        shr bx, 1
        rcr ax, 1
        dec cl
        jnz .sd_sh_loop
        mov bx, [si+DIRECTORY_OFFSET_SECTOR]
        add bx, ax
        cmp bx, dx
        jbe .sd_no_update
        mov dx, bx
        .sd_no_update:
        pop cx
        pop bx
        pop ax
        test byte [si+DIRECTORY_OFFSET_FLAGS], FLAG_DIRECTORY
        jz .sd_skip
        push ax
        push bx
        push cx
        push si
        push di
        mov ax, [si+DIRECTORY_OFFSET_SECTOR]
        mov di, DIRECTORY_SECTORS
        .sd_subloop:
        push ax
        call read_sector
        pop ax
        jc .sd_subdir_err
        mov si, SECTOR_BUFFER
        mov cx, DIRECTORY_MAX_ENTRIES / DIRECTORY_SECTORS
        .sd_sub_entry:
        cmp byte [si], 0
        je .sd_sub_skip
        push ax
        push bx
        mov ax, [si+DIRECTORY_OFFSET_SIZE]
        add ax, 511
        mov bx, [si+DIRECTORY_OFFSET_SIZE+2]
        adc bx, 0
        push cx
        mov cl, 9
        .sd_sub_sh_loop:
        shr bx, 1
        rcr ax, 1
        dec cl
        jnz .sd_sub_sh_loop
        pop cx
        mov bx, [si+DIRECTORY_OFFSET_SECTOR]
        add bx, ax
        cmp bx, dx
        jbe .sd_sub_no_update
        mov dx, bx
        .sd_sub_no_update:
        pop bx
        pop ax
        .sd_sub_skip:
        add si, DIRECTORY_ENTRY_SIZE
        loop .sd_sub_entry
        inc ax
        dec di
        jnz .sd_subloop
        jmp .sd_subdir_done
        .sd_subdir_err:
        .sd_subdir_done:
        pop di
        pop si
        pop cx
        pop bx
        pop ax
        push dx
        push di
        pop ax
        shr al, 4
        add al, DIRECTORY_SECTOR
        xor ah, ah
        call read_sector
        pop dx
        push ax
        push cx
        mov ax, di
        and al, 0Fh
        mov cl, 5
        shl ax, cl
        mov si, SECTOR_BUFFER
        add si, ax
        pop cx
        pop ax
        .sd_skip:
        add si, DIRECTORY_ENTRY_SIZE
        inc di
        dec cx
        jnz .sd_entry
        push dx
        push di
        pop ax
        shr al, 4
        add al, DIRECTORY_SECTOR
        pop dx
        cmp al, DIRECTORY_SECTOR + DIRECTORY_SECTORS
        jb .sd_next_sector
        .sd_done:
        pop di
        ret

subdir_find_free:
        ;; Find the first empty slot in a subdirectory
        ;; Input:  AX = subdirectory's first data sector
        ;; Output: CF clear, BX = entry pointer in SECTOR_BUFFER;
        ;;         directory_loaded_sector set to the containing sector
        ;;         CF set: AL = ERROR_NOT_FOUND or ERROR_DIRECTORY_FULL
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
        ;; Copy null-terminated name from SI into the entry starting at BX,
        ;; padding with zeros to DIRECTORY_NAME_LENGTH-1 bytes total.
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

bbfs_read_sec:
        ;; Fill SECTOR_BUFFER with the 512-byte sector at the current read position.
        ;; Input:  SI = FD entry pointer (FD_OFFSET_START = file start sector)
        ;; Output: SECTOR_BUFFER filled, BX = byte offset within sector; CF on error
        push ax
        push cx
        mov ax, [si+FD_OFFSET_POSITION+2]
        mov bx, [si+FD_OFFSET_POSITION]
        shl ax, 7
        mov cx, bx
        shr cx, 9
        or ax, cx
        add ax, [si+FD_OFFSET_START]    ; AX = absolute sector
        and bx, 01FFh                   ; BX = byte offset within sector
        push bx
        call read_sector
        pop bx
        pop cx
        pop ax
        ret
