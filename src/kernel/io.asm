find_file:
        ;; Search directory for a filename
        ;; Input: SI = pointer to null-terminated filename
        ;; Output: BX = pointer to matching directory entry in DISK_BUFFER
        ;;         Carry set if not found or disk error
        push cx
        push dx
        push di

        mov dx, si              ; DX = filename to find

        mov al, DIR_SECTOR
        call read_sector
        jc .ff_done             ; Carry already set from read_sector

        mov bx, DISK_BUFFER
        mov cx, DIR_MAX_ENTRIES

        .ff_search:
        cmp byte [bx], 0       ; Empty entry = end of listing
        je .ff_not_found

        mov si, dx              ; User's filename
        mov di, bx              ; Entry filename
        .ff_cmp:
        mov al, [si]
        cmp al, [di]
        jne .ff_next
        test al, al             ; Both null = match
        jz .ff_found
        inc si
        inc di
        jmp .ff_cmp

        .ff_next:
        add bx, DIR_ENTRY_SIZE
        loop .ff_search

        .ff_not_found:
        stc

        .ff_done:
        pop di
        pop dx
        pop cx
        ret

        .ff_found:
        clc
        jmp .ff_done

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

