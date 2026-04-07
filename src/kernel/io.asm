dir_load_entry:
        ;; Load directory entry by index into DISK_BUFFER
        ;; Input: BX = entry index (0 to DIR_MAX_ENTRIES-1)
        ;; Output: BX = pointer to entry in DISK_BUFFER
        ;; Use dir_write_back to write the sector after modifications
        push ax
        push cx
        ;; Compute sector: DIR_SECTOR + (index / entries_per_sector)
        mov al, bl
        mov cl, 4               ; 16 entries per sector = 2^4
        shr al, cl
        add al, DIR_SECTOR
        mov [dir_loaded_sec], al
        ;; Compute offset: (index % 16) * DIR_ENTRY_SIZE
        mov al, bl
        and al, 0Fh             ; index % 16
        xor ah, ah
        mov cl, 5               ; DIR_ENTRY_SIZE = 32 = 2^5
        shl ax, cl              ; * 32
        mov bx, ax
        add bx, DISK_BUFFER
        ;; Read the sector
        mov al, [dir_loaded_sec]
        call read_sector
        pop cx
        pop ax
        ret

dir_write_back:
        ;; Write the directory sector last loaded by dir_load_entry
        ;; Sets CF on error
        push ax
        mov al, [dir_loaded_sec]
        call write_sector
        pop ax
        ret

        dir_loaded_sec db 0

find_file:
        ;; Search directory for a filename, with optional path support
        ;; Input: SI = pointer to null-terminated filename (may contain one '/')
        ;; Output: BX = pointer to entry in DISK_BUFFER
        ;;         dir_loaded_sec set to the sector containing the found entry
        ;;         CF set if not found or disk error
        push ax
        push cx
        push dx
        push si
        push di

        ;; Check for '/' in the filename to detect subdirectory path
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
        ;; No slash — search root directory as before
        mov dx, si
        jmp .ff_search_root

        .ff_has_slash:
        ;; Split path at '/': DI points to '/'
        mov byte [di], 0       ; null-terminate directory name
        push di                 ; save '/' position for restore
        ;; Search root for the directory name
        mov dx, si              ; DX = directory name
        push dx
        call .ff_do_root_search
        pop dx
        pop di                  ; restore '/' position
        mov byte [di], '/'     ; restore the slash
        jc .ff_done             ; directory not found
        ;; Verify it's a directory (FLAG_DIR set)
        ;; BX = pointer to entry in DISK_BUFFER (from root search)
        test byte [bx+DIR_OFF_FLAGS], FLAG_DIR
        jz .ff_not_found        ; not a directory
        ;; Read the subdirectory's data sector
        mov al, [bx+DIR_OFF_SECTOR]
        mov [dir_loaded_sec], al
        call read_sector
        jc .ff_done
        ;; Search the subdirectory sector for the filename after '/'
        inc di                  ; skip past '/'
        mov dx, di              ; DX = filename within subdir
        mov di, DISK_BUFFER
        mov cx, DIR_MAX_ENTRIES / DIR_SECTORS ; 16 entries per sector
        xor bx, bx             ; BX = entry index within subdir
        jmp .ff_search

        .ff_search_root:
        xor bx, bx
        mov al, DIR_SECTOR

        .ff_load_sector:
        mov [dir_loaded_sec], al
        call read_sector
        jc .ff_done
        mov di, DISK_BUFFER
        mov cx, DIR_MAX_ENTRIES / DIR_SECTORS

        .ff_search:
        cmp byte [di], 0       ; Empty entry = end of listing in this sector
        je .ff_try_next_sector

        mov si, dx              ; User's filename
        push di                 ; Save entry pointer
        .ff_cmp:
        mov al, [si]
        cmp al, [di]
        jne .ff_no_match
        test al, al             ; Both null = match
        jz .ff_found
        inc si
        inc di
        jmp .ff_cmp

        .ff_no_match:
        pop di
        add di, DIR_ENTRY_SIZE
        inc bx
        loop .ff_search

        .ff_try_next_sector:
        ;; For subdirectory searches (single sector), we're done
        ;; For root searches, try next sector
        mov al, [dir_loaded_sec]
        sub al, DIR_SECTOR
        inc al
        cmp al, DIR_SECTORS
        jae .ff_not_found
        ;; Advance BX to start of next sector
        add bx, cx
        mov al, bl
        shr al, 4
        add al, DIR_SECTOR
        cmp al, DIR_SECTOR + DIR_SECTORS
        jb .ff_load_sector

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
        pop bx                  ; BX = entry pointer in DISK_BUFFER
        clc
        jmp .ff_done

        .ff_do_root_search:
        ;; Helper: search root directory for name in DX
        ;; Returns BX = entry index, CF on not found
        xor bx, bx
        mov al, DIR_SECTOR
        .fdr_load:
        call read_sector
        jc .fdr_done
        mov di, DISK_BUFFER
        mov cx, DIR_MAX_ENTRIES / DIR_SECTORS
        .fdr_search:
        cmp byte [di], 0
        je .fdr_try_next
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
        add di, DIR_ENTRY_SIZE
        inc bx
        loop .fdr_search
        .fdr_try_next:
        add bx, cx
        mov al, bl
        shr al, 4
        add al, DIR_SECTOR
        cmp al, DIR_SECTOR + DIR_SECTORS
        jb .fdr_load
        stc
        .fdr_done:
        ret
        .fdr_found:
        pop bx                  ; BX = entry pointer
        clc
        ret

lba_to_chs:
        ;; Convert 1-based logical sector to CHS using detected geometry
        ;; Input: AL = logical sector (1-based)
        ;; Output: CH = cylinder, CL = sector (1-based), DH = head
        ;; Clobbers: AX
        push bx
        dec al                  ; 0-based LBA
        xor ah, ah
        mov bl, [sectors_per_track]
        div bl                  ; AL = track, AH = sector_in_track
        mov cl, ah
        inc cl                  ; CL = 1-based sector
        xor ah, ah
        mov bl, [heads_per_cyl]
        div bl                  ; AL = cylinder, AH = head
        mov ch, al
        mov dh, ah
        pop bx
        ret

load_file:
        ;; Load file sectors into memory
        ;; Input: BX = pointer to directory entry in DISK_BUFFER
        ;;        DI = destination address
        ;; Output: Carry set on disk error
        ;; Clobbers: SI, CX, DI
        mov cx, [bx+DIR_OFF_SIZE]   ; File size in bytes
        mov bl, [bx+DIR_OFF_SECTOR] ; Start sector
        .lf_sector:
        mov al, bl
        call read_sector
        jc .lf_done
        push cx
        cmp cx, 512
        jbe .lf_partial
        mov cx, 256             ; Full sector = 256 words
        jmp .lf_copy
        .lf_partial:
        inc cx                  ; Round up to whole words
        shr cx, 1
        .lf_copy:
        cld
        mov si, DISK_BUFFER
        rep movsw
        pop cx
        sub cx, 512
        jbe .lf_loaded
        inc bl                  ; Next sector
        jmp .lf_sector
        .lf_loaded:
        clc
        .lf_done:
        ret

read_sector:
        ;; Read one sector into DISK_BUFFER
        ;; Input: AL = logical sector number (1-based)
        ;; Sets carry flag on error
        push bx
        push cx
        push dx
        call lba_to_chs
        mov dl, [boot_disk]
        mov bx, DISK_BUFFER
        mov ax, 0201h
        int 13h
        pop dx
        pop cx
        pop bx
        ret

scan_dir_entries:
        ;; Scan all directory sectors (including subdirectories) for next data sector
        ;; Also finds first free entry in root directory
        ;; Returns: BX = first free root entry index (0xFFFF if full)
        ;;          DL = next free data sector
        ;; Clobbers: AX, CX, SI
        push di
        mov bx, 0FFFFh                ; BX = free entry index (none)
        mov dl, DIR_SECTOR + DIR_SECTORS ; DL = next data sector
        xor di, di                    ; DI = current entry index
        mov al, DIR_SECTOR

        .sd_next_sector:
        call read_sector
        jc .sd_done
        mov si, DISK_BUFFER
        mov cx, DIR_MAX_ENTRIES / DIR_SECTORS

        .sd_entry:
        cmp byte [si], 0
        jne .sd_occupied
        cmp bx, 0FFFFh
        jne .sd_skip
        mov bx, di                    ; first free entry index
        jmp .sd_skip

        .sd_occupied:
        push bx
        xor ax, ax
        mov al, [si+DIR_OFF_SECTOR]
        xor bx, bx
        mov bx, [si+DIR_OFF_SIZE]
        add bx, 511
        shr bx, 9
        add al, bl
        pop bx
        cmp al, dl
        jbe .sd_check_subdir
        mov dl, al

        .sd_check_subdir:
        ;; If this is a directory, also scan its sector for files
        test byte [si+DIR_OFF_FLAGS], FLAG_DIR
        jz .sd_skip
        push ax
        push bx
        push cx
        push si
        push di
        push dx
        ;; Read the subdirectory sector
        mov al, [si+DIR_OFF_SECTOR]
        call read_sector
        pop dx
        jc .sd_subdir_done
        ;; Scan entries in subdirectory for max data sector
        mov si, DISK_BUFFER
        mov cx, DIR_MAX_ENTRIES / DIR_SECTORS
        .sd_sub_entry:
        cmp byte [si], 0
        je .sd_sub_skip
        push bx
        xor ax, ax
        mov al, [si+DIR_OFF_SECTOR]
        xor bx, bx
        mov bx, [si+DIR_OFF_SIZE]
        add bx, 511
        shr bx, 9
        add al, bl
        pop bx
        cmp al, dl
        jbe .sd_sub_skip
        mov dl, al
        .sd_sub_skip:
        add si, DIR_ENTRY_SIZE
        loop .sd_sub_entry
        .sd_subdir_done:
        pop di
        pop si
        pop cx
        pop bx
        pop ax
        ;; Re-read the root sector we were scanning (subdirectory read clobbered it)
        push dx
        push di
        pop ax                 ; AX = DI (current root entry index)
        shr al, 4
        add al, DIR_SECTOR
        call read_sector
        pop dx
        ;; Recompute SI to point to the current entry in the re-read sector
        push ax
        push cx
        mov ax, di
        and al, 0Fh
        mov cl, 5
        shl ax, cl
        mov si, DISK_BUFFER
        add si, ax
        pop cx
        pop ax

        .sd_skip:
        add si, DIR_ENTRY_SIZE
        inc di
        dec cx
        jnz .sd_entry

        ;; Try next root sector
        push dx
        push di
        pop ax
        shr al, 4
        add al, DIR_SECTOR
        pop dx
        cmp al, DIR_SECTOR + DIR_SECTORS
        jb .sd_next_sector

        .sd_done:
        pop di
        ret

write_sector:
        ;; Write DISK_BUFFER to one sector on disk
        ;; Input: AL = logical sector number (1-based)
        ;; Sets carry flag on error
        push bx
        push cx
        push dx
        call lba_to_chs
        mov dl, [boot_disk]
        mov bx, DISK_BUFFER
        mov ax, 0301h
        int 13h
        pop dx
        pop cx
        pop bx
        ret
