;;; fd.asm -- File descriptor table management
;;;
;;; fd_alloc:  Find the first free FD slot (AX = fd number, CF if full)
;;; fd_close:  SYS_CLOSE -- BX=fd
;;; fd_init:   Zero the FD table, pre-open fds 0/1/2 as console
;;; fd_lookup: Validate fd in BX, return SI = entry pointer (CF if invalid)
;;; fd_open:   SYS_OPEN  -- SI=filename, AL=flags; returns AX=fd
;;; fd_read:   SYS_READ  -- BX=fd, DI=buffer, CX=count; returns AX=bytes

fd_alloc:
        ;; Find first free FD slot
        ;; Returns: AX = fd number, SI = entry pointer; CF set if table full
        push bx
        push cx
        mov si, fd_table
        xor ax, ax
        mov cx, FD_MAX
        .scan:
        cmp byte [si+FD_OFF_TYPE], FD_TYPE_FREE
        je .found
        add si, FD_ENTRY_SIZE
        inc ax
        dec cx
        jnz .scan
        pop cx
        pop bx
        stc
        ret
        .found:
        pop cx
        pop bx
        clc
        ret

;;; -----------------------------------------------------------------------
;;; fd_close: Close a file descriptor
;;; Input:  BX = fd number
;;; Output: CF set on error
;;; -----------------------------------------------------------------------
fd_close:
        call fd_lookup
        jc .close_err
        ;; SI = entry pointer
        ;; TODO: if writable file, flush + update dir size (Phase 3)
        ;; Zero the entry to free it
        push ax
        push cx
        push di
        mov di, si
        xor ax, ax
        mov cx, FD_ENTRY_SIZE / 2
        cld
        rep stosw
        pop di
        pop cx
        pop ax
        clc
        ret
        .close_err:
        stc
        ret

fd_init:
        ;; Zero the entire FD table
        push ax
        push cx
        push di
        mov di, fd_table
        xor ax, ax
        mov cx, FD_MAX * FD_ENTRY_SIZE / 2
        cld
        rep stosw
        ;; Pre-open fd 0 (stdin), fd 1 (stdout), fd 2 (stderr) as console
        mov si, fd_table
        mov byte [si+FD_OFF_TYPE], FD_TYPE_CONSOLE
        mov byte [si+FD_OFF_FLAGS], O_RDONLY
        add si, FD_ENTRY_SIZE
        mov byte [si+FD_OFF_TYPE], FD_TYPE_CONSOLE
        mov byte [si+FD_OFF_FLAGS], O_WRONLY
        add si, FD_ENTRY_SIZE
        mov byte [si+FD_OFF_TYPE], FD_TYPE_CONSOLE
        mov byte [si+FD_OFF_FLAGS], O_WRONLY
        pop di
        pop cx
        pop ax
        ret

fd_lookup:
        ;; Validate fd in BX, return SI = entry pointer
        ;; CF set if invalid (out of range or slot is free)
        cmp bx, FD_MAX
        jae .invalid
        push ax
        mov ax, bx
        shl ax, 4              ; ax = bx * FD_ENTRY_SIZE (16)
        mov si, fd_table
        add si, ax
        cmp byte [si+FD_OFF_TYPE], FD_TYPE_FREE
        je .invalid_pop
        pop ax
        clc
        ret
        .invalid_pop:
        pop ax
        .invalid:
        stc
        ret

;;; -----------------------------------------------------------------------
;;; fd_open: Open a file and return a file descriptor
;;; Input:  SI = filename, AL = flags (O_RDONLY, O_WRONLY, O_CREAT, O_TRUNC)
;;; Output: AX = fd number (CF clear), or -1 on error (CF set)
;;; -----------------------------------------------------------------------
fd_open:
        push cx
        push dx
        push di
        ;; Save flags before find_file clobbers AX
        mov dl, al
        ;; Look up the file
        call find_file
        jc .open_not_found
        ;; Check if it is a directory
        test byte [bx+DIR_OFF_FLAGS], FLAG_DIR
        jnz .open_err
        ;; File found — allocate an FD
        call fd_alloc
        jc .open_err            ; table full
        ;; SI = entry pointer from fd_alloc, BX = dir entry in DISK_BUFFER
        ;; Populate the FD entry
        mov byte [si+FD_OFF_TYPE], FD_TYPE_FILE
        mov [si+FD_OFF_FLAGS], dl
        ;; start_sec
        mov cx, [bx+DIR_OFF_SECTOR]
        mov [si+FD_OFF_START], cx
        ;; size (32-bit)
        mov cx, [bx+DIR_OFF_SIZE]
        mov [si+FD_OFF_SIZE], cx
        mov cx, [bx+DIR_OFF_SIZE+2]
        mov [si+FD_OFF_SIZE+2], cx
        ;; pos = 0
        mov word [si+FD_OFF_POS], 0
        mov word [si+FD_OFF_POS+2], 0
        ;; dir_sec and dir_off (for writeback on close)
        mov cx, [dir_loaded_sec]
        mov [si+FD_OFF_DIR_SEC], cx
        ;; dir_off = BX - DISK_BUFFER
        mov cx, bx
        sub cx, DISK_BUFFER
        mov [si+FD_OFF_DIR_OFF], cx
        ;; If O_TRUNC, reset size to 0
        test dl, O_TRUNC
        jz .open_done
        mov word [si+FD_OFF_SIZE], 0
        mov word [si+FD_OFF_SIZE+2], 0
        .open_done:
        ;; AX = fd number (already set by fd_alloc)
        pop di
        pop dx
        pop cx
        clc
        ret

        .open_not_found:
        ;; If O_CREAT is set, create the file
        test dl, O_CREAT
        jz .open_err
        ;; TODO: implement create path in a later phase
        .open_err:
        pop di
        pop dx
        pop cx
        mov ax, -1
        stc
        ret

;;; -----------------------------------------------------------------------
;;; fd_read: Read bytes from a file descriptor
;;; Input:  BX = fd, DI = user buffer, CX = byte count
;;; Output: AX = bytes actually read (0 = EOF), or -1 on error (CF set)
;;;
;;; For console FDs, polls keyboard/serial for each byte.
;;; For file FDs, reads sectors internally and copies bytes to user buffer.
;;; -----------------------------------------------------------------------
fd_read:
        call fd_lookup
        jc .read_err
        ;; SI = entry pointer
        cmp byte [si+FD_OFF_TYPE], FD_TYPE_CONSOLE
        je .read_console
        cmp byte [si+FD_OFF_TYPE], FD_TYPE_FILE
        je .read_file
        .read_err:
        mov ax, -1
        stc
        ret

        .read_console:
        ;; Read CX bytes from keyboard/serial into [DI]
        push bx
        push cx
        push di
        mov bx, cx             ; BX = bytes requested
        xor dx, dx             ; DX = bytes read so far
        test bx, bx
        jz .rcon_done
        .rcon_loop:
        call getc_internal
        stosb                   ; [DI++] = AL
        inc dx
        cmp dx, bx
        jb .rcon_loop
        .rcon_done:
        mov ax, dx
        pop di
        pop cx
        pop bx
        clc
        ret

        .read_file:
        ;; SI = FD entry pointer
        ;; Save caller's registers and set up local state:
        ;;   [fd_read_fdp]  = FD entry pointer
        ;;   [fd_read_left] = bytes left to transfer (16-bit, after clamping)
        ;;   [fd_read_done] = bytes transferred so far
        mov [fd_read_fdp], si
        push bx
        push cx
        push dx
        push di
        ;; Clamp CX to remaining file bytes
        mov ax, [si+FD_OFF_SIZE]
        sub ax, [si+FD_OFF_POS]
        mov dx, [si+FD_OFF_SIZE+2]
        sbb dx, [si+FD_OFF_POS+2]
        ;; DX:AX = remaining
        js .rf_eof
        or dx, dx
        jnz .rf_start           ; remaining > 64K, CX is fine as-is
        test ax, ax
        jz .rf_eof
        ;; AX = remaining (fits 16-bit), clamp CX
        cmp cx, ax
        jbe .rf_start
        mov cx, ax
        .rf_start:
        mov [fd_read_left], cx
        mov word [fd_read_done], 0
        .rf_loop:
        cmp word [fd_read_left], 0
        je .rf_done
        ;; Compute sector = start_sec + (pos >> 9)
        mov si, [fd_read_fdp]
        mov ax, [si+FD_OFF_POS+2]
        mov dx, [si+FD_OFF_POS]
        ;; AX:DX is high:low of pos; we need pos >> 9
        ;; Result = (AX << 7) | (DX >> 9)
        shl ax, 7              ; AX = high_word << 7
        shr dx, 9              ; DX = low_word >> 9
        or ax, dx               ; AX = sector offset
        add ax, [si+FD_OFF_START]
        ;; Read this sector into DISK_BUFFER
        call read_sector
        jc .rf_disk_err
        ;; Byte offset within sector
        mov si, [fd_read_fdp]
        mov bx, [si+FD_OFF_POS]
        and bx, 01FFh           ; BX = offset within sector
        ;; Chunk size = min(512 - offset, bytes_left)
        mov cx, 512
        sub cx, bx              ; CX = available in sector
        cmp cx, [fd_read_left]
        jbe .rf_chunk_ok
        mov cx, [fd_read_left]
        .rf_chunk_ok:
        ;; Copy CX bytes from DISK_BUFFER+BX to [DI]
        push si
        mov si, DISK_BUFFER
        add si, bx
        cld
        push cx                 ; save chunk size
        rep movsb               ; copies CX bytes, DI advances
        pop cx                  ; CX = chunk size
        pop si
        ;; Update bookkeeping
        add [fd_read_done], cx
        sub [fd_read_left], cx
        ;; Update fd_pos += chunk (32-bit)
        mov si, [fd_read_fdp]
        add [si+FD_OFF_POS], cx
        adc word [si+FD_OFF_POS+2], 0
        jmp .rf_loop

        .rf_eof:
        pop di
        pop dx
        pop cx
        pop bx
        xor ax, ax
        clc
        ret

        .rf_disk_err:
        pop di
        pop dx
        pop cx
        pop bx
        mov ax, -1
        stc
        ret

        .rf_done:
        mov ax, [fd_read_done]
        pop di
        pop dx
        pop cx
        pop bx
        clc
        ret

        ;; Local variables for fd_read
        fd_read_done dw 0
        fd_read_fdp  dw 0
        fd_read_left dw 0
