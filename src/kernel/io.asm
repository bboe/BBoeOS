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
        ;; Search directory for a filename (across DIR_SECTORS sectors)
        ;; Input: SI = pointer to null-terminated filename
        ;; Output: BX = entry index (0 to DIR_MAX_ENTRIES-1), CF clear
        ;;         CF set if not found or disk error
        push ax
        push cx
        push dx
        push di

        mov dx, si              ; DX = filename to find
        xor bx, bx              ; BX = current entry index
        mov al, DIR_SECTOR

        .ff_load_sector:
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
        ;; Advance BX to start of next sector's entries
        ;; (skip remaining entries in this sector)
        add bx, cx              ; skip unscanned entries
        mov al, bl
        shr al, 4               ; sector offset = index / 16
        add al, DIR_SECTOR
        cmp al, DIR_SECTOR + DIR_SECTORS
        jb .ff_load_sector

        .ff_not_found:
        stc

        .ff_done:
        pop di
        pop dx
        pop cx
        pop ax
        ret

        .ff_found:
        pop di                  ; discard saved entry pointer
        clc
        jmp .ff_done

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
        ;; Input: AL = sector number (1-based CHS, cylinder 0, head 0)
        ;; Sets carry flag on error
        push bx
        push cx
        push dx
        mov cl, al              ; CL = sector number
        xor ch, ch              ; CH = cylinder 0
        xor dh, dh              ; DH = head 0
        mov dl, [boot_disk]     ; DL = drive number
        mov bx, DISK_BUFFER     ; ES:BX = buffer
        mov ax, 0201h           ; AH=02 (read), AL=01 (1 sector)
        int 13h
        pop dx
        pop cx
        pop bx
        ret

scan_dir_entries:
        ;; Scan all directory sectors for free entry and next data sector
        ;; Returns: BX = first free entry index (0xFFFF if full)
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
        jbe .sd_skip
        mov dl, al

        .sd_skip:
        add si, DIR_ENTRY_SIZE
        inc di
        loop .sd_entry

        ;; Try next sector
        push dx
        push di
        pop ax                 ; AX = DI (entry index)
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
        ;; Input: AL = sector number (1-based CHS, cylinder 0, head 0)
        ;; Sets carry flag on error
        push bx
        push cx
        push dx
        mov cl, al              ; CL = sector number
        xor ch, ch              ; CH = cylinder 0
        xor dh, dh              ; DH = head 0
        mov dl, [boot_disk]     ; DL = drive number
        mov bx, DISK_BUFFER     ; ES:BX = buffer
        mov ax, 0301h           ; AH=03 (write), AL=01 (1 sector)
        int 13h
        pop dx
        pop cx
        pop bx
        ret
