;;; fs/bbfs.asm -- BBoeOS custom filesystem (flat directory + contiguous files)
;;;
;;; VFS interface (called through vfs.asm function pointers):
;;; bbfs_chmod:             SI=path, AL=mode → CF on error (AL=error code)
;;; bbfs_commit_write_sec:  → CF on disk error
;;; bbfs_create:            SI=path, DL=mode → vfs_found_*, CF on error
;;; bbfs_delete:            SI=path → CF on error (AL=error code)
;;; bbfs_find:              SI=path → vfs_found_*, CF if not found
;;; bbfs_init:              → (no-op: no persistent state to initialise)
;;; bbfs_load:              DI=dest → CF (loads file using vfs_found_inode + vfs_found_size)
;;; bbfs_mkdir:             SI=name → AX=allocated sector, CF on error
;;; bbfs_prepare_write_sec: SI=fd_entry → sector_buffer ready, BX=offset; CF on err
;;; bbfs_read_dir:          SI=fd_entry, DI=buf → AX=entry size or 0 at EOF; CF on error
;;; bbfs_read_sec:          SI=fd_entry → sector_buffer filled, BX=byte offset; CF on err
;;; bbfs_rename:            SI=old, DI=new → CF on error (AL=error code)
;;; bbfs_rmdir:             SI=name → CF on error (AL=error code)
;;; bbfs_update_size:       SI=fd_entry → CF on disk error

bbfs_chmod:
        ;; Change a file's flags byte
        ;; Input:  SI = path, AL = new flags
        ;; Output: CF clear on success; CF set, AL = error code on failure
        push ebx
        push esi
        push eax                     ; save new flags
        call find_file              ; EBX = dir entry in sector_buffer
        jnc .do_chmod
        pop eax
        pop esi
        pop ebx
        mov al, ERROR_NOT_FOUND
        stc
        ret
        .do_chmod:
        pop eax                      ; restore new flags
        mov [ebx+DIRECTORY_OFFSET_FLAGS], al
        call directory_write_back
        pop esi
        pop ebx
        ret

bbfs_commit_write_sec:
        ;; Write sector_buffer to the sector cached by bbfs_prepare_write_sec.
        ;; Output: CF on disk error
        push eax
        mov ax, [bbfs_pws_sector]
        call write_sector
        pop eax
        ret

bbfs_create:
        ;; Create a new empty file entry and populate vfs_found_*
        ;; Input:  SI = null-terminated path (may contain one '/'),
        ;;         DL = mode flags (FLAG_EXECUTE bit honoured; others zero)
        ;; Output: CF clear, vfs_found_* set; CF set on error
        push ebx
        push ecx
        push edx
        push edi
        push esi
        mov [bbfs_create_name], esi
        mov [bbfs_create_mode], dl    ; stash before scan_directory_entries clobbers DX
        call scan_directory_entries   ; BX = free root entry index, DX = next data sector
        mov [bbfs_create_sector], dx
        ;; scan_directory_entries clobbers SI; restore path pointer from saved variable
        mov edi, [bbfs_create_name]
        .bc_slash:
        mov al, [edi]
        test al, al
        jz .bc_root
        cmp al, '/'
        je .bc_subdir
        inc edi
        jmp .bc_slash
        .bc_root:
        cmp bx, 0FFFFh
        je .bc_full
        call directory_load_entry     ; EBX = entry ptr in sector_buffer
        mov esi, [bbfs_create_name]
        jmp .bc_write
        .bc_subdir:
        ;; Null-terminate dir component, find subdir, then find a free slot in it
        mov byte [edi], 0
        push edi
        mov esi, [bbfs_create_name]
        call find_file                ; EBX = subdir entry in sector_buffer
        pop edi
        mov byte [edi], '/'
        jc .bc_err
        test byte [ebx+DIRECTORY_OFFSET_FLAGS], FLAG_DIRECTORY
        jz .bc_err
        mov ax, [ebx+DIRECTORY_OFFSET_SECTOR]
        call subdir_find_free         ; EBX = free entry ptr, directory_loaded_sector set
        jc .bc_full
        inc edi                       ; ESI = basename (past '/')
        mov esi, edi
        .bc_write:
        push ebx
        call write_directory_name
        pop ebx
        mov al, [bbfs_create_mode]
        and al, FLAG_EXECUTE          ; only FLAG_EXECUTE is meaningful for files
        mov [ebx+DIRECTORY_OFFSET_FLAGS], al
        mov ax, [bbfs_create_sector]
        mov [ebx+DIRECTORY_OFFSET_SECTOR], ax
        mov word [ebx+DIRECTORY_OFFSET_SIZE], 0
        mov word [ebx+DIRECTORY_OFFSET_SIZE+2], 0
        call directory_write_back
        jc .bc_err
        ;; Populate vfs_found_*
        mov ax, [bbfs_create_sector]
        mov [vfs_found_inode], ax
        mov word [vfs_found_size], 0
        mov word [vfs_found_size+2], 0
        mov al, [bbfs_create_mode]
        and al, FLAG_EXECUTE
        mov [vfs_found_mode], al
        mov byte [vfs_found_type], FD_TYPE_FILE
        mov ax, [directory_loaded_sector]
        mov [vfs_found_dir_sec], ax
        mov eax, ebx
        sub eax, [sector_buffer]
        mov [vfs_found_dir_off], ax
        pop esi
        pop edi
        pop edx
        pop ecx
        pop ebx
        clc
        ret
        .bc_full:
        pop esi
        pop edi
        pop edx
        pop ecx
        pop ebx
        mov al, ERROR_DIRECTORY_FULL
        stc
        ret
        .bc_err:
        pop esi
        pop edi
        pop edx
        pop ecx
        pop ebx
        stc
        ret

bbfs_delete:
        ;; Delete a file by zeroing its directory entry.
        ;; Input:  SI = path
        ;; Output: CF clear on success; CF set, AL = error code on failure
        push ebx
        push ecx
        push edi
        call find_file          ; EBX = entry ptr in sector_buffer, CF on miss
        jnc .do_delete
        pop edi
        pop ecx
        pop ebx
        mov al, ERROR_NOT_FOUND
        stc
        ret
        .do_delete:
        push esi
        mov edi, ebx
        mov ecx, DIRECTORY_ENTRY_SIZE / 2
        xor eax, eax
        cld
        rep stosw
        pop esi
        call directory_write_back
        pop edi
        pop ecx
        pop ebx
        ret

bbfs_find:
        ;; Find a file (or "." root) and populate vfs_found_*
        ;; Input:  SI = null-terminated path (may contain one '/')
        ;; Output: CF clear, vfs_found_* set; CF set if not found
        push ebx
        ;; Handle "." — synthesise root directory entry
        cmp byte [esi], '.'
        jne .normal_find
        cmp byte [esi+1], 0
        jne .normal_find
        mov ax, [directory_sector]
        mov [vfs_found_inode], ax
        mov word [vfs_found_size], DIRECTORY_SECTORS * 512
        mov word [vfs_found_size+2], 0
        mov byte [vfs_found_mode], FLAG_DIRECTORY
        mov byte [vfs_found_type], FD_TYPE_DIRECTORY
        mov word [vfs_found_dir_sec], 0
        mov word [vfs_found_dir_off], 0
        pop ebx
        clc
        ret
        .normal_find:
        call find_file          ; EBX = dir entry pointer in sector_buffer, CF on miss
        jnc .found
        pop ebx
        stc
        ret
        .found:
        mov ax, [ebx+DIRECTORY_OFFSET_SECTOR]
        mov [vfs_found_inode], ax
        mov ax, [ebx+DIRECTORY_OFFSET_SIZE]
        mov [vfs_found_size], ax
        mov ax, [ebx+DIRECTORY_OFFSET_SIZE+2]
        mov [vfs_found_size+2], ax
        mov al, [ebx+DIRECTORY_OFFSET_FLAGS]
        mov [vfs_found_mode], al
        mov byte [vfs_found_type], FD_TYPE_FILE
        test al, FLAG_DIRECTORY
        jz .set_dir_info
        mov byte [vfs_found_type], FD_TYPE_DIRECTORY
        .set_dir_info:
        mov ax, [directory_loaded_sector]
        mov [vfs_found_dir_sec], ax
        mov eax, ebx
        sub eax, [sector_buffer]
        mov [vfs_found_dir_off], ax
        pop ebx
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
        push ebx
        push ecx
        push esi
        mov bx, [vfs_found_inode]   ; start sector
        movzx ecx, word [vfs_found_size] ; file size (low 16 bits; executables fit in 64 KB)
        .load_sector:
        mov ax, bx
        call read_sector
        jc .load_done
        push ecx
        cmp ecx, 512
        jbe .partial
        mov ecx, 256                ; full sector = 256 words
        jmp .copy
        .partial:
        inc ecx
        shr ecx, 1
        .copy:
        cld
        mov esi, [sector_buffer]
        rep movsw
        pop ecx
        sub ecx, 512
        jbe .loaded
        inc bx
        jmp .load_sector
        .loaded:
        clc
        .load_done:
        pop esi
        pop ecx
        pop ebx
        ret

bbfs_mkdir:
        ;; Create a subdirectory entry and zero its data sectors
        ;; Input:  SI = name (no slashes, max 24 chars)
        ;; Output: AX = allocated sector (16-bit), CF clear on success
        ;;         CF set, AL = error code on failure
        push ebx
        push ecx
        push edx
        push edi
        push esi
        ;; Reject if the name already exists
        call find_file
        jc .bbmkdir_scan
        pop esi
        pop edi
        pop edx
        pop ecx
        pop ebx
        mov al, ERROR_EXISTS
        stc
        ret
        .bbmkdir_scan:
        mov edi, esi                    ; EDI = dirname (for write_directory_name later)
        call scan_directory_entries     ; BX = free root entry index, DX = next data sector
        cmp bx, 0FFFFh
        jne .bbmkdir_write
        pop esi
        pop edi
        pop edx
        pop ecx
        pop ebx
        mov al, ERROR_DIRECTORY_FULL
        stc
        ret
        .bbmkdir_write:
        push edx                         ; save next data sector
        call directory_load_entry       ; EBX = entry ptr in sector_buffer
        pop edx
        push edx
        push ebx
        mov esi, edi
        call write_directory_name
        pop ebx
        pop edx
        mov byte [ebx+DIRECTORY_OFFSET_FLAGS], FLAG_DIRECTORY
        mov [ebx+DIRECTORY_OFFSET_SECTOR], dx
        mov word [ebx+DIRECTORY_OFFSET_SIZE], DIRECTORY_SECTORS * 512
        mov word [ebx+DIRECTORY_OFFSET_SIZE+2], 0
        call directory_write_back
        jc .bbmkdir_disk_err
        ;; Zero-fill sector_buffer and write to each subdir sector
        push edx
        push edi
        mov edi, [sector_buffer]
        mov ecx, 256
        xor eax, eax
        cld
        rep stosw
        pop edi
        pop edx
        push edx
        mov ecx, DIRECTORY_SECTORS
        mov ax, dx
        .bbmkdir_zero_loop:
        push eax
        push ecx
        call write_sector
        pop ecx
        pop eax
        jc .bbmkdir_zero_err
        inc ax
        loop .bbmkdir_zero_loop
        pop edx
        mov ax, dx                      ; return allocated sector in AX
        pop esi
        pop edi
        pop edx
        pop ecx
        pop ebx
        clc
        ret
        .bbmkdir_zero_err:
        pop edx
        .bbmkdir_disk_err:
        pop esi
        pop edi
        pop edx
        pop ecx
        pop ebx
        stc
        ret

bbfs_prepare_write_sec:
        ;; Prepare for a write: translate fd position to sector, optionally read.
        ;; Input:  SI = fd_entry pointer
        ;; Output: sector_buffer ready for modification (read if partial sector),
        ;;         BX = byte offset within sector; CF on disk error
        push ecx                        ; inline fd_pos_to_sector
        mov ax, [esi+FD_OFFSET_POSITION+2]
        mov bx, [esi+FD_OFFSET_POSITION]
        shl ax, 7
        mov cx, bx
        shr cx, 9
        or ax, cx
        add ax, [esi+FD_OFFSET_START]    ; AX = absolute sector
        and bx, 01FFh                   ; BX = byte offset within sector
        pop ecx
        mov [bbfs_pws_sector], ax
        test bx, bx
        jz .no_read             ; offset 0: new/full-sector write, skip read
        call read_sector
        ret
        .no_read:
        clc
        ret

bbfs_read_dir:
        ;; Read the next non-empty bbfs directory entry into [DI]
        ;; SI = FD entry pointer, DI = output buffer (DIRECTORY_ENTRY_SIZE bytes)
        ;; Returns AX = DIRECTORY_ENTRY_SIZE if found, 0 at EOF, CF on error
        push ebx
        push ecx
        push edx
        push edi
        .brd_next:
        mov ax, [esi+FD_OFFSET_POSITION]
        cmp ax, DIRECTORY_SECTORS * 512
        jae .brd_eof
        mov ax, [esi+FD_OFFSET_POSITION+2]  ; inline fd_pos_to_sector
        movzx ebx, word [esi+FD_OFFSET_POSITION]
        shl ax, 7
        mov cx, bx
        shr cx, 9
        or ax, cx
        add ax, [esi+FD_OFFSET_START]    ; AX = absolute sector
        and ebx, 01FFh                  ; EBX = byte offset within sector
        call read_sector
        jc .brd_disk_err
        mov eax, [sector_buffer]
        cmp byte [eax+ebx], 0
        jne .brd_found
        add word [esi+FD_OFFSET_POSITION], DIRECTORY_ENTRY_SIZE
        jmp .brd_next
        .brd_found:
        push esi
        mov esi, ebx
        add esi, [sector_buffer]
        mov ecx, DIRECTORY_ENTRY_SIZE
        cld
        rep movsb
        pop esi
        add word [esi+FD_OFFSET_POSITION], DIRECTORY_ENTRY_SIZE
        mov ax, DIRECTORY_ENTRY_SIZE
        pop edi
        pop edx
        pop ecx
        pop ebx
        clc
        ret
        .brd_eof:
        pop edi
        pop edx
        pop ecx
        pop ebx
        xor ax, ax
        clc
        ret
        .brd_disk_err:
        pop edi
        pop edx
        pop ecx
        pop ebx
        mov ax, -1
        stc
        ret

bbfs_read_sec:
        ;; Fill sector_buffer with the 512-byte sector at the current read position.
        ;; Input:  SI = FD entry pointer (FD_OFFSET_START = file start sector)
        ;; Output: sector_buffer filled, BX = byte offset within sector; CF on error
        push eax
        push ecx
        mov ax, [esi+FD_OFFSET_POSITION+2]
        mov bx, [esi+FD_OFFSET_POSITION]
        shl ax, 7
        mov cx, bx
        shr cx, 9
        or ax, cx
        add ax, [esi+FD_OFFSET_START]    ; AX = absolute sector
        and bx, 01FFh                   ; BX = byte offset within sector
        push ebx
        call read_sector
        pop ebx
        pop ecx
        pop eax
        ret

bbfs_rename:
        ;; Rename (or cross-directory move) a file
        ;; Input:  SI = old path, DI = new path
        ;; Output: CF clear on success; CF set, AL = error code on failure
        ;; Verify both names share the same directory prefix (or both are root)
        push esi
        push edi
        push ecx
        ;; CX = byte offset of '/' in SI, or 0FFFFh if none
        mov cx, 0FFFFh
        push esi
        .rename_pfx_scan_si:
        cmp byte [esi], 0
        je .rename_pfx_si_done
        cmp byte [esi], '/'
        jne .rename_pfx_si_next
        mov cx, si
        pop eax
        sub cx, ax
        push eax
        jmp .rename_pfx_si_done
        .rename_pfx_si_next:
        inc esi
        jmp .rename_pfx_scan_si
        .rename_pfx_si_done:
        pop esi
        push ecx
        ;; CX = byte offset of '/' in DI, or 0FFFFh if none
        mov cx, 0FFFFh
        push edi
        .rename_pfx_scan_di:
        cmp byte [edi], 0
        je .rename_pfx_di_done
        cmp byte [edi], '/'
        jne .rename_pfx_di_next
        mov cx, di
        pop eax
        sub cx, ax
        push eax
        jmp .rename_pfx_di_done
        .rename_pfx_di_next:
        inc edi
        jmp .rename_pfx_scan_di
        .rename_pfx_di_done:
        pop edi
        pop eax                          ; AX = SI slash offset
        cmp ax, cx
        jne .rename_pfx_bad             ; different slash positions → cross-dir
        cmp ax, 0FFFFh
        je .rename_pfx_ok               ; both root
        ;; Both have slash at offset AX; compare that many bytes
        push esi
        push edi
        movzx ecx, ax
        .rename_pfx_cmp:
        mov al, [esi]
        cmp al, [edi]
        jne .rename_pfx_cmp_bad
        inc esi
        inc edi
        loop .rename_pfx_cmp
        pop edi
        pop esi
        jmp .rename_pfx_ok
        .rename_pfx_cmp_bad:
        pop edi
        pop esi
        .rename_pfx_bad:
        jmp .rename_cross
        .rename_pfx_ok:
        pop ecx
        pop edi
        pop esi
        ;; Check new name doesn't already exist
        push esi
        mov esi, edi
        call find_file
        pop esi
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
        push esi
        mov esi, edi
        .rename_basename:
        lodsb
        test al, al
        jz .rename_basename_done
        cmp al, '/'
        jne .rename_basename
        mov edi, esi                      ; one past '/'
        .rename_basename_done:
        pop esi
        push ecx
        push esi
        mov esi, edi
        call write_directory_name
        pop esi
        pop ecx
        call directory_write_back
        ret                             ; CF from write_back

        .rename_cross:
        ;; Cross-directory rename — stack still has [SI old], [DI new], [CX]
        pop ecx
        pop edi
        pop esi
        ;; Check new name doesn't already exist
        push esi
        mov esi, edi
        call find_file
        pop esi
        jc .frc_find_old
        mov al, ERROR_EXISTS
        stc
        ret
        .frc_find_old:
        call find_file                  ; EBX = src entry in sector_buffer
        jnc .frc_got_src
        mov al, ERROR_NOT_FOUND
        stc
        ret
        .frc_got_src:
        ;; Build frame.  Layout (EBP = ESP after 7 dwords pushed):
        ;;   [ebp+24] basename ptr  [ebp+20] src_sec  [ebp+16] size_lo
        ;;   [ebp+12] size_hi       [ebp+8]  flags    [ebp+4]  src_dir_sec
        ;;   [ebp+0]  src_entry_off
        push edi                         ; [ebp+24]
        mov ax, [ebx+DIRECTORY_OFFSET_SECTOR]
        push eax                         ; [ebp+20]
        mov ax, [ebx+DIRECTORY_OFFSET_SIZE]
        push eax                         ; [ebp+16]
        mov ax, [ebx+DIRECTORY_OFFSET_SIZE+2]
        push eax                         ; [ebp+12]
        xor eax, eax
        mov al, [ebx+DIRECTORY_OFFSET_FLAGS]
        push eax                         ; [ebp+8]
        mov ax, [directory_loaded_sector]
        push eax                         ; [ebp+4]
        mov eax, ebx
        sub eax, [sector_buffer]
        push eax                         ; [ebp+0]
        mov ebp, esp
        ;; Locate the destination directory
        mov edi, [ebp+24]
        .frc_scan:
        mov al, [edi]
        test al, al
        jz .frc_dst_root
        cmp al, '/'
        je .frc_dst_subdir
        inc edi
        jmp .frc_scan
        .frc_dst_root:
        mov ax, [directory_sector]
        jmp .frc_alloc
        .frc_dst_subdir:
        mov byte [edi], 0
        push edi
        mov esi, [ebp+24]
        call find_file                  ; EBX = subdir entry
        pop edi
        mov byte [edi], '/'
        jc .frc_bad_dir
        test byte [ebx+DIRECTORY_OFFSET_FLAGS], FLAG_DIRECTORY
        jz .frc_bad_dir
        mov ax, [ebx+DIRECTORY_OFFSET_SECTOR]
        inc edi                         ; basename = char after '/'
        mov [ebp+24], edi
        jmp .frc_alloc
        .frc_bad_dir:
        add esp, 28
        mov al, ERROR_NOT_FOUND
        stc
        ret
        .frc_alloc:
        call subdir_find_free           ; EBX = entry ptr; directory_loaded_sector set
        jnc .frc_write
        add esp, 28
        stc
        ret
        .frc_write:
        push ebx
        mov esi, [ebp+24]
        call write_directory_name
        pop ebx
        mov al, [ebp+8]
        mov [ebx+DIRECTORY_OFFSET_FLAGS], al
        mov ax, [ebp+20]
        mov [ebx+DIRECTORY_OFFSET_SECTOR], ax
        mov ax, [ebp+16]
        mov [ebx+DIRECTORY_OFFSET_SIZE], ax
        mov ax, [ebp+12]
        mov [ebx+DIRECTORY_OFFSET_SIZE+2], ax
        call directory_write_back
        jc .frc_disk_err
        ;; Re-read the source directory sector and zero the original entry
        mov ax, [ebp+4]
        mov [directory_loaded_sector], ax
        call read_sector
        jc .frc_disk_err
        mov ebx, [sector_buffer]
        add ebx, [ebp+0]
        push edi
        push ecx
        mov edi, ebx
        mov ecx, DIRECTORY_ENTRY_SIZE / 2
        xor eax, eax
        cld
        rep stosw
        pop ecx
        pop edi
        call directory_write_back
        jc .frc_disk_err
        add esp, 28
        clc
        ret
        .frc_disk_err:
        add esp, 28
        mov al, ERROR_NOT_FOUND
        stc
        ret

bbfs_rmdir:
        ;; Remove an empty subdirectory.
        ;; Input:  SI = name (root-level only; slashes not allowed)
        ;; Output: CF clear on success; CF set, AL = error code on failure
        push ebx
        push ecx
        push edx
        push esi
        push edi
        call find_file                  ; EBX = entry ptr in sector_buffer, CF on miss
        jnc .bbrd_found
        pop edi
        pop esi
        pop edx
        pop ecx
        pop ebx
        mov al, ERROR_NOT_FOUND
        stc
        ret
        .bbrd_found:
        ;; Must be a directory
        test byte [ebx+DIRECTORY_OFFSET_FLAGS], FLAG_DIRECTORY
        jnz .bbrd_is_dir
        pop edi
        pop esi
        pop edx
        pop ecx
        pop ebx
        mov al, ERROR_NOT_FOUND
        stc
        ret
        .bbrd_is_dir:
        ;; Save location of parent dir entry and start sector of subdir data
        mov ax, [ebx+DIRECTORY_OFFSET_SECTOR]
        mov [bbfs_rd_subdir_sec], ax
        mov ax, [directory_loaded_sector]
        mov [bbfs_rd_parent_sec], ax
        mov eax, ebx
        sub eax, [sector_buffer]
        mov [bbfs_rd_entry_off], ax
        ;; Scan subdir data sectors for any occupied entry
        mov ax, [bbfs_rd_subdir_sec]
        mov ecx, DIRECTORY_SECTORS
        .bbrd_check_sec:
        call read_sector
        jc .bbrd_disk_err
        mov esi, [sector_buffer]
        mov edi, DIRECTORY_MAX_ENTRIES / DIRECTORY_SECTORS
        .bbrd_check_entry:
        cmp byte [esi], 0
        jne .bbrd_not_empty
        add esi, DIRECTORY_ENTRY_SIZE
        dec edi
        jnz .bbrd_check_entry
        inc ax
        dec ecx
        jnz .bbrd_check_sec
        ;; Empty: reload parent sector, zero the directory entry, write back
        mov ax, [bbfs_rd_parent_sec]
        mov [directory_loaded_sector], ax
        call read_sector
        jc .bbrd_disk_err
        mov ebx, [sector_buffer]
        movzx eax, word [bbfs_rd_entry_off]
        add ebx, eax
        mov edi, ebx
        mov ecx, DIRECTORY_ENTRY_SIZE / 2
        xor eax, eax
        cld
        rep stosw
        call directory_write_back
        pop edi
        pop esi
        pop edx
        pop ecx
        pop ebx
        ret
        .bbrd_not_empty:
        pop edi
        pop esi
        pop edx
        pop ecx
        pop ebx
        mov al, ERROR_NOT_EMPTY
        stc
        ret
        .bbrd_disk_err:
        pop edi
        pop esi
        pop edx
        pop ecx
        pop ebx
        stc
        ret

bbfs_update_size:
        ;; Write fd position back to the directory entry as the file size
        ;; Input:  SI = fd_table entry pointer
        ;; Output: CF set on disk error
        push eax
        push ebx
        push ecx
        push edx
        mov ax, [esi+FD_OFFSET_DIRECTORY_SECTOR]
        mov [directory_loaded_sector], ax
        call read_sector
        jc .us_err
        mov ebx, [sector_buffer]
        movzx eax, word [esi+FD_OFFSET_DIRECTORY_OFFSET]
        add ebx, eax
        mov ax, [esi+FD_OFFSET_POSITION]
        mov [ebx+DIRECTORY_OFFSET_SIZE], ax
        mov ax, [esi+FD_OFFSET_POSITION+2]
        mov [ebx+DIRECTORY_OFFSET_SIZE+2], ax
        call directory_write_back
        pop edx
        pop ecx
        pop ebx
        pop eax
        ret
        .us_err:
        pop edx
        pop ecx
        pop ebx
        pop eax
        stc
        ret

;;; -----------------------------------------------------------------------
;;; Internal helpers (not called directly by the rest of the kernel)
;;; -----------------------------------------------------------------------

directory_load_entry:
        ;; Load a root directory entry by index and return a pointer to it
        ;; Input:  BX = entry index (0 to DIRECTORY_MAX_ENTRIES-1)
        ;; Output: EBX = pointer to entry in sector_buffer
        ;; Side effect: directory_loaded_sector set; sector_buffer updated
        push eax
        push ecx
        movzx ebx, bx
        mov eax, ebx
        mov cl, 4                       ; 16 entries per sector = 2^4
        shr eax, cl
        add ax, [directory_sector]
        mov [directory_loaded_sector], ax
        mov eax, ebx
        and eax, 0Fh                    ; index % 16
        mov cl, 5                       ; DIRECTORY_ENTRY_SIZE = 32 = 2^5
        shl eax, cl
        mov ebx, eax
        add ebx, [sector_buffer]
        mov ax, [directory_loaded_sector]
        call read_sector
        pop ecx
        pop eax
        ret

directory_write_back:
        ;; Write the sector last loaded by directory_load_entry or find_file
        ;; Output: CF set on disk error
        push eax
        mov ax, [directory_loaded_sector]
        call write_sector
        pop eax
        ret

find_file:
        ;; Search directory for a filename, with optional subdirectory path support
        ;; Input:  SI = null-terminated filename (may contain one '/')
        ;; Output: EBX = pointer to entry in sector_buffer; CF set if not found
        ;; Side effect: directory_loaded_sector set to the sector of the found entry
        push eax
        push ecx
        push edx
        push esi
        push edi
        ;; Scan for '/' to detect a subdirectory path
        mov edi, esi
        .ff_scan_slash:
        mov al, [edi]
        test al, al
        jz .ff_no_slash
        cmp al, '/'
        je .ff_has_slash
        inc edi
        jmp .ff_scan_slash
        .ff_no_slash:
        mov edx, esi
        mov ax, [directory_sector]
        mov [directory_search_start], ax
        jmp .ff_search_root
        .ff_has_slash:
        ;; Split path at '/': DI points to '/'
        mov byte [edi], 0
        push edi
        mov edx, esi
        push edx
        call .ff_do_root_search
        pop edx
        pop edi
        mov byte [edi], '/'
        jc .ff_done
        test byte [ebx+DIRECTORY_OFFSET_FLAGS], FLAG_DIRECTORY
        jz .ff_not_found
        mov ax, [ebx+DIRECTORY_OFFSET_SECTOR]
        mov [directory_search_start], ax
        inc edi
        mov edx, edi
        xor bx, bx
        jmp .ff_load_sector
        .ff_search_root:
        xor bx, bx
        mov ax, [directory_sector]
        .ff_load_sector:
        mov [directory_loaded_sector], ax
        call read_sector
        jc .ff_done
        mov edi, [sector_buffer]
        mov ecx, DIRECTORY_MAX_ENTRIES / DIRECTORY_SECTORS
        .ff_search:
        cmp byte [edi], 0
        je .ff_skip_entry
        mov esi, edx
        push edi
        .ff_cmp:
        mov al, [esi]
        cmp al, [edi]
        jne .ff_no_match
        test al, al
        jz .ff_found
        inc esi
        inc edi
        jmp .ff_cmp
        .ff_no_match:
        pop edi
        .ff_skip_entry:
        add edi, DIRECTORY_ENTRY_SIZE
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
        pop edi
        pop esi
        pop edx
        pop ecx
        pop eax
        ret
        .ff_found:
        pop ebx
        clc
        jmp .ff_done
        .ff_do_root_search:
        ;; Helper: search root directory for name in DX → EBX = entry ptr, CF on miss
        xor bx, bx
        mov al, byte [directory_sector]
        .fdr_load:
        xor ah, ah
        call read_sector
        jc .fdr_done
        mov edi, [sector_buffer]
        mov ecx, DIRECTORY_MAX_ENTRIES / DIRECTORY_SECTORS
        .fdr_search:
        cmp byte [edi], 0
        je .fdr_skip
        mov esi, edx
        push edi
        .fdr_cmp:
        mov al, [esi]
        cmp al, [edi]
        jne .fdr_no_match
        test al, al
        jz .fdr_found
        inc esi
        inc edi
        jmp .fdr_cmp
        .fdr_no_match:
        pop edi
        .fdr_skip:
        add edi, DIRECTORY_ENTRY_SIZE
        inc bx
        loop .fdr_search
        .fdr_try_next:
        add bx, cx
        mov al, bl
        shr al, 4
        add al, byte [directory_sector]
        mov ah, byte [directory_sector]
        add ah, DIRECTORY_SECTORS
        cmp al, ah
        jb .fdr_load
        stc
        .fdr_done:
        ret
        .fdr_found:
        pop ebx
        clc
        ret

scan_directory_entries:
        ;; Scan all directory sectors for the first free root entry and next data sector
        ;; Output: BX = first free root entry index (0xFFFF if full)
        ;;         DX = next free data sector (16-bit)
        ;; Clobbers: AX, CX, SI
        push edi
        mov bx, 0FFFFh
        mov dx, [directory_sector]
        add dx, DIRECTORY_SECTORS
        xor edi, edi
        mov al, byte [directory_sector]
        .sd_next_sector:
        xor ah, ah
        call read_sector
        jc .sd_done
        mov esi, [sector_buffer]
        mov cx, DIRECTORY_MAX_ENTRIES / DIRECTORY_SECTORS
        .sd_entry:
        cmp byte [esi], 0
        jne .sd_occupied
        cmp bx, 0FFFFh
        jne .sd_skip
        mov bx, di
        jmp .sd_skip
        .sd_occupied:
        push eax
        push ebx
        push ecx
        mov ax, [esi+DIRECTORY_OFFSET_SIZE]
        add ax, 511
        mov bx, [esi+DIRECTORY_OFFSET_SIZE+2]
        adc bx, 0
        mov cl, 9
        .sd_sh_loop:
        shr bx, 1
        rcr ax, 1
        dec cl
        jnz .sd_sh_loop
        mov bx, [esi+DIRECTORY_OFFSET_SECTOR]
        add bx, ax
        cmp bx, dx
        jbe .sd_no_update
        mov dx, bx
        .sd_no_update:
        pop ecx
        pop ebx
        pop eax
        test byte [esi+DIRECTORY_OFFSET_FLAGS], FLAG_DIRECTORY
        jz .sd_skip
        push eax
        push ebx
        push ecx
        push esi
        push edi
        mov ax, [esi+DIRECTORY_OFFSET_SECTOR]
        mov di, DIRECTORY_SECTORS
        .sd_subloop:
        push eax
        call read_sector
        pop eax
        jc .sd_subdir_err
        mov esi, [sector_buffer]
        mov ecx, DIRECTORY_MAX_ENTRIES / DIRECTORY_SECTORS
        .sd_sub_entry:
        cmp byte [esi], 0
        je .sd_sub_skip
        push eax
        push ebx
        mov ax, [esi+DIRECTORY_OFFSET_SIZE]
        add ax, 511
        mov bx, [esi+DIRECTORY_OFFSET_SIZE+2]
        adc bx, 0
        push ecx
        mov cl, 9
        .sd_sub_sh_loop:
        shr bx, 1
        rcr ax, 1
        dec cl
        jnz .sd_sub_sh_loop
        pop ecx
        mov bx, [esi+DIRECTORY_OFFSET_SECTOR]
        add bx, ax
        cmp bx, dx
        jbe .sd_sub_no_update
        mov dx, bx
        .sd_sub_no_update:
        pop ebx
        pop eax
        .sd_sub_skip:
        add esi, DIRECTORY_ENTRY_SIZE
        loop .sd_sub_entry
        inc ax
        dec edi
        jnz .sd_subloop
        jmp .sd_subdir_done
        .sd_subdir_err:
        .sd_subdir_done:
        pop edi
        pop esi
        pop ecx
        pop ebx
        pop eax
        push edx
        push edi
        pop eax
        shr al, 4
        add al, byte [directory_sector]
        xor ah, ah
        call read_sector
        pop edx
        push eax
        push ecx
        mov ax, di
        and al, 0Fh
        mov cl, 5
        shl ax, cl
        mov esi, [sector_buffer]
        movzx eax, ax
        add esi, eax
        pop ecx
        pop eax
        .sd_skip:
        add esi, DIRECTORY_ENTRY_SIZE
        inc edi
        dec cx
        jnz .sd_entry
        push edx
        push edi
        pop eax
        shr al, 4
        add al, byte [directory_sector]
        pop edx
        mov ah, byte [directory_sector]
        add ah, DIRECTORY_SECTORS
        cmp al, ah
        jb .sd_next_sector
        .sd_done:
        pop edi
        ret

subdir_find_free:
        ;; Find the first empty slot in a subdirectory
        ;; Input:  AX = subdirectory's first data sector
        ;; Output: CF clear, EBX = entry pointer in sector_buffer;
        ;;         directory_loaded_sector set to the containing sector
        ;;         CF set: AL = ERROR_NOT_FOUND or ERROR_DIRECTORY_FULL
        ;; Clobbers: AX, EBX, CX, DX
        mov dx, DIRECTORY_SECTORS
        .sff_loop:
        push eax
        push edx
        mov [directory_loaded_sector], ax
        call read_sector
        pop edx
        pop eax
        jnc .sff_scan_init
        mov al, ERROR_NOT_FOUND
        stc
        ret
        .sff_scan_init:
        mov ebx, [sector_buffer]
        mov ecx, DIRECTORY_MAX_ENTRIES / DIRECTORY_SECTORS
        .sff_scan:
        cmp byte [ebx], 0
        je .sff_found
        add ebx, DIRECTORY_ENTRY_SIZE
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
        ;; Copy null-terminated name from SI into the entry starting at EBX,
        ;; padding with zeros to DIRECTORY_NAME_LENGTH-1 bytes total.
        ;; Clobbers: AX, EBX (advanced), CX, SI (advanced)
        mov cx, DIRECTORY_NAME_LENGTH - 1
        .copy:
        mov al, [esi]
        test al, al
        jz .pad
        inc esi
        mov [ebx], al
        inc ebx
        dec cx
        jnz .copy
        ret
        .pad:
        mov byte [ebx], 0
        inc ebx
        dec cx
        jnz .pad
        ret

        bbfs_create_mode   db 0   ; bbfs_create: FLAG_EXECUTE bit (others ignored)
        bbfs_create_name   dd 0   ; 32-bit kernel-virt or user-virt path pointer
        bbfs_create_sector dw 0
        bbfs_pws_sector    dw 0
        bbfs_rd_entry_off  dw 0   ; bbfs_rmdir: byte offset of entry within parent sector
        bbfs_rd_parent_sec dw 0   ; bbfs_rmdir: disk sector containing the directory entry
        bbfs_rd_subdir_sec dw 0   ; bbfs_rmdir: first sector of the subdirectory's data
        directory_loaded_sector  dw 0
        directory_search_start   dw 0
