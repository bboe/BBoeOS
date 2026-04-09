dir_load_entry:
        ;; Load directory entry by index into DISK_BUFFER
        ;; Input: BX = entry index (0 to DIR_MAX_ENTRIES-1)
        ;; Output: BX = pointer to entry in DISK_BUFFER
        ;; Use dir_write_back to write the sector after modifications
        push ax
        push cx
        ;; Compute sector: DIR_SECTOR + (index / entries_per_sector)
        mov ax, bx
        mov cl, 4               ; 16 entries per sector = 2^4
        shr ax, cl
        add ax, DIR_SECTOR
        mov [dir_loaded_sec], ax
        ;; Compute offset: (index % 16) * DIR_ENTRY_SIZE
        mov ax, bx
        and ax, 0Fh             ; index % 16
        mov cl, 5               ; DIR_ENTRY_SIZE = 32 = 2^5
        shl ax, cl              ; * 32
        mov bx, ax
        add bx, DISK_BUFFER
        ;; Read the sector
        mov ax, [dir_loaded_sec]
        call read_sector
        pop cx
        pop ax
        ret

dir_write_back:
        ;; Write the directory sector last loaded by dir_load_entry
        ;; Sets CF on error
        push ax
        mov ax, [dir_loaded_sec]
        call write_sector
        pop ax
        ret

        dir_loaded_sec  dw 0
        dir_search_start dw 0

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
        ;; No slash — search root directory
        mov dx, si
        mov word [dir_search_start], DIR_SECTOR
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
        ;; Read the subdirectory's first data sector (16-bit)
        mov ax, [bx+DIR_OFF_SECTOR]
        mov [dir_search_start], ax
        ;; Search subdir entries for the filename after '/'
        inc di                  ; skip past '/'
        mov dx, di              ; DX = filename within subdir
        xor bx, bx             ; BX = entry index within subdir
        jmp .ff_load_sector

        .ff_search_root:
        xor bx, bx
        mov ax, DIR_SECTOR

        .ff_load_sector:
        mov [dir_loaded_sec], ax
        call read_sector
        jc .ff_done
        mov di, DISK_BUFFER
        mov cx, DIR_MAX_ENTRIES / DIR_SECTORS

        .ff_search:
        cmp byte [di], 0       ; Empty slot — skip (holes are allowed)
        je .ff_skip_entry

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
        .ff_skip_entry:
        add di, DIR_ENTRY_SIZE
        inc bx
        loop .ff_search

        .ff_try_next_sector:
        ;; Try next sector relative to dir_search_start
        mov ax, [dir_loaded_sec]
        sub ax, [dir_search_start]
        inc ax
        cmp ax, DIR_SECTORS
        jae .ff_not_found
        ;; Advance to next sector
        mov ax, [dir_loaded_sec]
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
        pop bx                  ; BX = entry pointer in DISK_BUFFER
        clc
        jmp .ff_done

        .ff_do_root_search:
        ;; Helper: search root directory for name in DX
        ;; Returns BX = entry index, CF on not found
        xor bx, bx
        mov al, DIR_SECTOR
        .fdr_load:
        xor ah, ah
        call read_sector
        jc .fdr_done
        mov di, DISK_BUFFER
        mov cx, DIR_MAX_ENTRIES / DIR_SECTORS
        .fdr_search:
        cmp byte [di], 0       ; Empty slot — skip (holes are allowed)
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
        ;; Input: AX = logical sector (1-based, 16-bit)
        ;; Output: CH = cylinder low 8 bits, CL bits 0-5 = sector (1-based)
        ;;         CL bits 6-7 = cylinder bits 8-9, DH = head
        ;; Clobbers: AX
        push bx
        dec ax                  ; 0-based LBA
        xor dx, dx
        mov bl, [sectors_per_track]
        xor bh, bh
        div bx                  ; AX = track (LBA/spt), DX = sector_in_track
        mov cl, dl
        inc cl                  ; CL = 1-based sector (low 6 bits)
        xor dx, dx
        mov bl, [heads_per_cyl]
        div bx                  ; AX = cylinder, DX = head
        mov ch, al              ; CH = cylinder low 8 bits
        shl ah, 6               ; encode cyl bits 8-9 into CL bits 6-7
        or cl, ah
        mov dh, dl              ; DH = head
        pop bx
        ret

load_file:
        ;; Load file sectors into memory
        ;; Input: BX = pointer to directory entry in DISK_BUFFER
        ;;        DI = destination address
        ;; Output: Carry set on disk error
        ;; Clobbers: SI, CX, DI
        mov cx, [bx+DIR_OFF_SIZE]   ; File size in bytes (low 16)
        mov bx, [bx+DIR_OFF_SECTOR] ; Start sector (16-bit)
        .lf_sector:
        mov ax, bx
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
        inc bx                  ; Next sector
        jmp .lf_sector
        .lf_loaded:
        clc
        .lf_done:
        ret

read_sector:
        ;; Read one sector into DISK_BUFFER
        ;; Input: AX = logical sector number (1-based, 16-bit)
        ;; Sets carry flag on error
        push bx
        push cx
        push dx
        call lba_to_chs         ; CH/CL/DH set; AX clobbered
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
        ;;          DX = next free data sector (16-bit)
        ;; Clobbers: AX, CX, SI
        push di
        mov bx, 0FFFFh                ; BX = free entry index (none)
        mov dx, DIR_SECTOR + DIR_SECTORS ; DX = next data sector (16-bit)
        xor di, di                    ; DI = current entry index
        mov al, DIR_SECTOR

        .sd_next_sector:
        xor ah, ah
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
        ;; end_sec = entry.start + ceil(entry.size / 512)
        ;; Compute (size_high:size_low + 511) >> 9 in BX:AX, then add start.
        push ax
        push bx
        push cx
        mov ax, [si+DIR_OFF_SIZE]
        add ax, 511
        mov bx, [si+DIR_OFF_SIZE+2]
        adc bx, 0
        mov cl, 9
        .sd_sh_loop:
        shr bx, 1
        rcr ax, 1
        dec cl
        jnz .sd_sh_loop
        ;; AX = sectors_used. Add start sector.
        mov bx, [si+DIR_OFF_SECTOR]
        add bx, ax                     ; BX = end sector
        cmp bx, dx
        jbe .sd_no_update
        mov dx, bx
        .sd_no_update:
        pop cx
        pop bx
        pop ax

        ;; If this is a directory, also scan its sectors for files
        test byte [si+DIR_OFF_FLAGS], FLAG_DIR
        jz .sd_skip
        push ax
        push bx
        push cx
        push si
        push di
        ;; AX = current subdir sector (16-bit), DI = remaining sectors to scan
        mov ax, [si+DIR_OFF_SECTOR]
        mov di, DIR_SECTORS
        .sd_subloop:
        push ax
        call read_sector
        pop ax
        jc .sd_subdir_err
        mov si, DISK_BUFFER
        mov cx, DIR_MAX_ENTRIES / DIR_SECTORS
        .sd_sub_entry:
        cmp byte [si], 0
        je .sd_sub_skip
        push ax
        push bx
        ;; Compute end_sec for the sub entry just like above
        mov ax, [si+DIR_OFF_SIZE]
        add ax, 511
        mov bx, [si+DIR_OFF_SIZE+2]
        adc bx, 0
        push cx
        mov cl, 9
        .sd_sub_sh_loop:
        shr bx, 1
        rcr ax, 1
        dec cl
        jnz .sd_sub_sh_loop
        pop cx
        mov bx, [si+DIR_OFF_SECTOR]
        add bx, ax                     ; BX = end sector (16-bit)
        cmp bx, dx
        jbe .sd_sub_no_update
        mov dx, bx
        .sd_sub_no_update:
        pop bx
        pop ax
        .sd_sub_skip:
        add si, DIR_ENTRY_SIZE
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
        ;; Re-read the root sector we were scanning (subdirectory read clobbered it)
        push dx
        push di
        pop ax                 ; AX = DI (current root entry index)
        shr al, 4
        add al, DIR_SECTOR
        xor ah, ah
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
        ;; Input: AX = logical sector number (1-based, 16-bit)
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
