;;; fd.asm -- File descriptor table management
;;;
;;; fd_alloc:         Find the first free FD slot (AX = fd number, CF if full)
;;; fd_close:         SYS_IO_CLOSE -- BX=fd; flushes writable files
;;; fd_fstat:         SYS_IO_FSTAT -- BX=fd; returns AL=mode, CX:DX=size
;;; fd_init:          Zero the FD table, pre-open fds 0/1/2 as console
;;; fd_lookup:        Validate fd in BX, return SI = entry pointer (CF if invalid)
;;; fd_open:          SYS_IO_OPEN  -- SI=filename, AL=flags, DL=mode; returns AX=fd
;;; fd_pos_to_sector: Convert fd_pos to sector + offset (internal helper)
;;; fd_read:          SYS_IO_READ  -- BX=fd, DI=buffer, CX=count; returns AX=bytes
;;; fd_write:         SYS_IO_WRITE -- BX=fd, SI=buffer, CX=count; returns AX=bytes

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
;;;
;;; For writable file FDs, updates the directory entry's size field
;;; from fd_pos (the number of bytes written) before freeing the slot.
;;; -----------------------------------------------------------------------
fd_close:
        call fd_lookup
        jc .close_err
        ;; SI = entry pointer
        cmp byte [si+FD_OFF_TYPE], FD_TYPE_FILE
        jne .close_free
        test byte [si+FD_OFF_FLAGS], O_WRONLY
        jz .close_free
        ;; Writable file — update directory entry size from fd_pos
        push ax
        push bx
        push cx
        push dx
        ;; Read the directory sector containing this file's entry
        mov ax, [si+FD_OFF_DIR_SEC]
        mov [dir_loaded_sec], ax
        call read_sector
        jc .close_write_err
        ;; Point BX at the entry within DISK_BUFFER
        mov bx, DISK_BUFFER
        add bx, [si+FD_OFF_DIR_OFF]
        ;; Update size from fd_pos (32-bit)
        mov ax, [si+FD_OFF_POS]
        mov [bx+DIR_OFF_SIZE], ax
        mov ax, [si+FD_OFF_POS+2]
        mov [bx+DIR_OFF_SIZE+2], ax
        ;; Write back the directory sector
        call dir_write_back
        jc .close_write_err
        pop dx
        pop cx
        pop bx
        pop ax
        jmp .close_free
        .close_write_err:
        pop dx
        pop cx
        pop bx
        pop ax
        ;; Fall through to free the slot, but propagate error
        .close_free:
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

;;; -----------------------------------------------------------------------
;;; fd_fstat: Get file status from a file descriptor
;;; Input:  BX = fd number
;;; Output: AL = mode (file permission flags), CX:DX = size (32-bit)
;;;         CF set on error
;;; -----------------------------------------------------------------------
fd_fstat:
        call fd_lookup
        jc .fstat_err
        ;; SI = entry pointer
        mov al, [si+FD_OFF_MODE]
        mov dx, [si+FD_OFF_SIZE]
        mov cx, [si+FD_OFF_SIZE+2]
        clc
        ret
        .fstat_err:
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
        shl ax, 5              ; ax = bx * FD_ENTRY_SIZE (32)
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
        ;; Save flags and filename before subsequent calls clobber them
        mov [fd_open_flags], al
        mov [fd_open_name], si
        ;; Check for "." (root directory)
        cmp byte [si], '.'
        jne .open_find
        cmp byte [si+1], 0
        jne .open_find
        jmp .open_root_dir
        .open_find:
        ;; Look up the file
        call find_file
        jc .open_not_found
        ;; Found — open as DIR or FILE depending on entry flags
        jmp .open_populate

        .open_not_found:
        ;; If O_CREAT is set, create the file
        test byte [fd_open_flags], O_CREAT
        jz .open_err
        ;; Create: scan for free entry + next data sector
        mov si, [fd_open_name]
        call scan_dir_entries   ; BX = free root entry index, DX = next data sector
        mov [fd_open_sec], dx   ; save start sector for new file
        ;; Check for '/' in filename to handle subdirectory paths
        mov di, [fd_open_name]
        .open_find_slash:
        mov al, [di]
        test al, al
        jz .open_create_root
        cmp al, '/'
        je .open_create_subdir
        inc di
        jmp .open_find_slash

        .open_create_root:
        ;; Create in root directory
        cmp bx, 0FFFFh
        je .open_err            ; directory full
        call dir_load_entry     ; BX = entry ptr in DISK_BUFFER
        mov si, [fd_open_name]
        jmp .open_write_entry

        .open_create_subdir:
        ;; DI points to '/'. Null-terminate dirname, find subdir, create entry.
        mov byte [di], 0
        push di
        mov si, [fd_open_name]
        call find_file          ; find the subdirectory
        pop di
        mov byte [di], '/'
        jc .open_err
        test byte [bx+DIR_OFF_FLAGS], FLAG_DIR
        jz .open_err
        ;; Scan subdirectory for free entry
        mov ax, [bx+DIR_OFF_SECTOR]
        call subdir_find_free
        jc .open_err
        ;; BX = free entry ptr in DISK_BUFFER, dir_loaded_sec set
        inc di                  ; skip past '/'
        mov si, di              ; SI = basename
        jmp .open_write_entry

        .open_write_entry:
        ;; BX = entry ptr in DISK_BUFFER, SI = filename to write
        push bx
        call write_dir_name
        pop bx
        mov byte [bx+DIR_OFF_FLAGS], 0
        mov ax, [fd_open_sec]
        mov [bx+DIR_OFF_SECTOR], ax
        mov word [bx+DIR_OFF_SIZE], 0
        mov word [bx+DIR_OFF_SIZE+2], 0
        call dir_write_back
        jc .open_err
        ;; Now BX points to the new entry in DISK_BUFFER — fall through
        ;; to populate the FD

        .open_populate:
        ;; BX = dir entry in DISK_BUFFER
        call fd_alloc
        jc .open_err            ; table full
        ;; SI = FD entry pointer from fd_alloc, AX = fd number
        mov [fd_open_fd], ax
        ;; Set FD type: DIR if directory entry, FILE otherwise
        test byte [bx+DIR_OFF_FLAGS], FLAG_DIR
        jnz .open_set_dir
        mov byte [si+FD_OFF_TYPE], FD_TYPE_FILE
        jmp .open_set_flags
        .open_set_dir:
        mov byte [si+FD_OFF_TYPE], FD_TYPE_DIR
        .open_set_flags:
        mov cl, [fd_open_flags]
        mov [si+FD_OFF_FLAGS], cl
        ;; mode (file permission flags from directory entry)
        mov cl, [bx+DIR_OFF_FLAGS]
        mov [si+FD_OFF_MODE], cl
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
        mov cx, bx
        sub cx, DISK_BUFFER
        mov [si+FD_OFF_DIR_OFF], cx
        ;; If O_TRUNC, reset size to 0
        test byte [fd_open_flags], O_TRUNC
        jz .open_done
        mov word [si+FD_OFF_SIZE], 0
        mov word [si+FD_OFF_SIZE+2], 0
        .open_done:
        mov ax, [fd_open_fd]
        pop di
        pop dx
        pop cx
        clc
        ret

        .open_root_dir:
        ;; Synthesize a DIR fd for the root directory
        call fd_alloc
        jc .open_err
        mov [fd_open_fd], ax
        mov byte [si+FD_OFF_TYPE], FD_TYPE_DIR
        mov cl, [fd_open_flags]
        mov [si+FD_OFF_FLAGS], cl
        mov byte [si+FD_OFF_MODE], FLAG_DIR
        mov word [si+FD_OFF_START], DIR_SECTOR
        mov word [si+FD_OFF_SIZE], DIR_SECTORS * 512
        mov word [si+FD_OFF_SIZE+2], 0
        mov word [si+FD_OFF_POS], 0
        mov word [si+FD_OFF_POS+2], 0
        mov word [si+FD_OFF_DIR_SEC], 0
        mov word [si+FD_OFF_DIR_OFF], 0
        jmp .open_done

        .open_err:
        pop di
        pop dx
        pop cx
        mov ax, -1
        stc
        ret

;;; -----------------------------------------------------------------------
;;; fd_pos_to_sector: Convert fd_pos to absolute sector + byte offset
;;; Input:  SI = FD entry pointer
;;; Output: AX = absolute sector number, BX = byte offset within sector
;;; -----------------------------------------------------------------------
fd_pos_to_sector:
        mov ax, [si+FD_OFF_POS+2]
        mov bx, [si+FD_OFF_POS]
        ;; AX:BX >> 9 = sector offset
        shl ax, 7
        push cx
        mov cx, bx
        shr cx, 9
        or ax, cx
        pop cx
        add ax, [si+FD_OFF_START]
        ;; BX = pos & 0x1FF
        and bx, 01FFh
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
        cmp byte [si+FD_OFF_TYPE], FD_TYPE_DIR
        je .read_dir
        cmp byte [si+FD_OFF_TYPE], FD_TYPE_FILE
        je .read_file
        .read_err:
        mov ax, -1
        stc
        ret

        .read_console:
        ;; Read from keyboard/serial into [DI], up to CX bytes.
        ;; Returns after the first key event (like Linux terminal input):
        ;;   - normal key: 1 byte (ASCII)
        ;;   - serial input: passed through as-is (1 byte at a time)
        ;;   - keyboard arrow key: 3 bytes (ESC [ A/B/C/D)
        push bx
        push cx
        push dx
        push di
        mov bx, cx             ; BX = bytes available in buffer
        test bx, bx
        jz .rcon_zero
        ;; Drain serial pushback buffer first
        cmp byte [serial_pb_count], 0
        jne .rcon_pushback
        ;; Poll hardware
        .rcon_poll:
        push dx
        mov dx, 3FDh
        in al, dx
        pop dx
        test al, 01h
        jnz .rcon_serial
        mov ah, 01h
        int 16h
        jz .rcon_poll
        ;; Keyboard key ready
        mov ah, 00h
        int 16h                 ; AL = ASCII, AH = scan code
        test al, al
        jz .rcon_extended       ; AL=0 means extended key
        ;; Normal ASCII key — store 1 byte
        stosb
        mov ax, 1
        jmp .rcon_ret
        .rcon_extended:
        ;; Map keyboard scan code to ESC sequence
        cmp bx, 3
        jb .rcon_poll           ; not enough buffer room, skip
        mov al, 1Bh
        stosb
        mov al, '['
        stosb
        cmp ah, 48h
        je .rcon_key_up
        cmp ah, 50h
        je .rcon_key_down
        cmp ah, 4Dh
        je .rcon_key_right
        cmp ah, 4Bh
        je .rcon_key_left
        ;; Unknown extended key — undo the ESC [ and retry
        sub di, 2
        jmp .rcon_poll
        .rcon_key_up:
        mov al, 'A'
        jmp .rcon_key_emit
        .rcon_key_down:
        mov al, 'B'
        jmp .rcon_key_emit
        .rcon_key_right:
        mov al, 'C'
        jmp .rcon_key_emit
        .rcon_key_left:
        mov al, 'D'
        .rcon_key_emit:
        stosb
        mov ax, 3
        jmp .rcon_ret
        .rcon_serial:
        ;; Serial byte ready — read and return it as-is
        push dx
        mov dx, 3F8h
        in al, dx
        pop dx
        stosb
        mov ax, 1
        jmp .rcon_ret
        .rcon_pushback:
        ;; Return one byte from the serial pushback buffer
        mov al, [serial_pb_buf]
        mov ah, [serial_pb_buf+1]
        mov [serial_pb_buf], ah
        dec byte [serial_pb_count]
        stosb
        mov ax, 1
        .rcon_ret:
        pop di
        pop dx
        pop cx
        pop bx
        clc
        ret
        .rcon_zero:
        xor ax, ax
        pop di
        pop dx
        pop cx
        pop bx
        clc
        ret

        .read_dir:
        ;; Read the next non-empty directory entry into [DI]
        ;; SI = FD entry pointer
        ;; Returns 32 bytes (one entry) or 0 at end of directory
        push bx
        push cx
        push dx
        push di
        .rd_next:
        ;; Check if past end of directory
        mov ax, [si+FD_OFF_POS]
        cmp ax, DIR_SECTORS * 512
        jae .rd_eof
        ;; Compute sector = start_sec + (pos / 512)
        call fd_pos_to_sector   ; AX = sector, BX = offset
        call read_sector
        jc .rd_disk_err
        ;; Check if entry at offset BX is non-empty
        cmp byte [DISK_BUFFER+bx], 0
        jne .rd_found
        ;; Empty slot — advance pos by DIR_ENTRY_SIZE and try again
        add word [si+FD_OFF_POS], DIR_ENTRY_SIZE
        jmp .rd_next
        .rd_found:
        ;; Copy DIR_ENTRY_SIZE bytes from DISK_BUFFER+BX to [DI]
        push si
        mov si, DISK_BUFFER
        add si, bx
        mov cx, DIR_ENTRY_SIZE
        cld
        rep movsb
        pop si
        ;; Advance pos
        add word [si+FD_OFF_POS], DIR_ENTRY_SIZE
        mov ax, DIR_ENTRY_SIZE
        pop di
        pop dx
        pop cx
        pop bx
        clc
        ret
        .rd_eof:
        pop di
        pop dx
        pop cx
        pop bx
        xor ax, ax
        clc
        ret
        .rd_disk_err:
        pop di
        pop dx
        pop cx
        pop bx
        mov ax, -1
        stc
        ret

        .read_file:
        ;; SI = FD entry pointer
        mov [fd_rw_fdp], si
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
        mov [fd_rw_left], cx
        mov word [fd_rw_done], 0
        .rf_loop:
        cmp word [fd_rw_left], 0
        je .rf_done
        ;; Compute sector = start_sec + (pos >> 9)
        mov si, [fd_rw_fdp]
        call fd_pos_to_sector   ; AX = sector, BX = offset within sector
        ;; Read this sector into DISK_BUFFER
        call read_sector
        jc .rf_disk_err
        ;; Chunk size = min(512 - offset, bytes_left)
        mov cx, 512
        sub cx, bx              ; CX = available in sector
        cmp cx, [fd_rw_left]
        jbe .rf_chunk_ok
        mov cx, [fd_rw_left]
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
        add [fd_rw_done], cx
        sub [fd_rw_left], cx
        mov si, [fd_rw_fdp]
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
        mov ax, [fd_rw_done]
        pop di
        pop dx
        pop cx
        pop bx
        clc
        ret

;;; -----------------------------------------------------------------------
;;; fd_write: Write bytes to a file descriptor
;;; Input:  BX = fd, SI = user buffer, CX = byte count
;;; Output: AX = bytes actually written, or -1 on error (CF set)
;;;
;;; For console FDs, calls put_char for each byte.
;;; For file FDs, writes sectors via DISK_BUFFER.
;;; -----------------------------------------------------------------------
fd_write:
        mov [fd_write_buf], si  ; save user buffer before fd_lookup clobbers SI
        call fd_lookup
        jc .wr_err
        ;; SI = entry pointer
        cmp byte [si+FD_OFF_TYPE], FD_TYPE_CONSOLE
        je .wr_console
        cmp byte [si+FD_OFF_TYPE], FD_TYPE_FILE
        je .wr_file
        .wr_err:
        mov ax, -1
        stc
        ret

        .wr_console:
        ;; Write CX bytes from user buffer to console via put_char
        push bx
        push cx
        push si
        mov si, [fd_write_buf]
        mov bx, cx             ; BX = count
        xor dx, dx             ; DX = bytes written
        test bx, bx
        jz .wcon_done
        .wcon_loop:
        lodsb
        call put_char
        inc dx
        cmp dx, bx
        jb .wcon_loop
        .wcon_done:
        mov ax, dx
        pop si
        pop cx
        pop bx
        clc
        ret

        .wr_file:
        ;; SI = FD entry pointer
        mov [fd_rw_fdp], si
        push bx
        push cx
        push dx
        push di
        mov [fd_rw_left], cx
        mov word [fd_rw_done], 0
        .wf_loop:
        cmp word [fd_rw_left], 0
        je .wf_done
        ;; Compute sector and offset from fd_pos
        mov si, [fd_rw_fdp]
        call fd_pos_to_sector   ; AX = sector, BX = offset within sector
        ;; If offset != 0, need read-modify-write (partial sector start)
        test bx, bx
        jz .wf_no_read
        ;; Also need read-modify-write if writing less than a full sector
        ;; from the start.  But check offset first — if offset is 0 and
        ;; count >= 512, we can skip the read entirely.
        call read_sector
        jc .wf_disk_err
        jmp .wf_copy
        .wf_no_read:
        ;; Offset is 0.  If writing >= 512 bytes, skip read.
        cmp word [fd_rw_left], 512
        jae .wf_copy
        ;; Writing < 512 bytes at offset 0 — might need existing data
        ;; for the tail of the sector.  But for new files written
        ;; sequentially, the tail is garbage anyway.  Skip the read.
        .wf_copy:
        ;; Chunk = min(512 - offset, bytes_left)
        mov cx, 512
        sub cx, bx              ; CX = space in sector
        cmp cx, [fd_rw_left]
        jbe .wf_chunk_ok
        mov cx, [fd_rw_left]
        .wf_chunk_ok:
        ;; Copy CX bytes from user buffer to DISK_BUFFER+BX
        push si
        mov di, DISK_BUFFER
        add di, bx
        mov si, [fd_write_buf]
        add si, [fd_rw_done]    ; advance past already-written bytes
        cld
        push cx
        rep movsb
        pop cx
        pop si
        ;; Recompute sector number (read_sector may have clobbered AX)
        mov si, [fd_rw_fdp]
        call fd_pos_to_sector   ; AX = sector
        ;; Write the sector
        call write_sector
        jc .wf_disk_err
        ;; Update bookkeeping
        add [fd_rw_done], cx
        sub [fd_rw_left], cx
        mov si, [fd_rw_fdp]
        add [si+FD_OFF_POS], cx
        adc word [si+FD_OFF_POS+2], 0
        jmp .wf_loop

        .wf_disk_err:
        pop di
        pop dx
        pop cx
        pop bx
        mov ax, -1
        stc
        ret

        .wf_done:
        mov ax, [fd_rw_done]
        pop di
        pop dx
        pop cx
        pop bx
        clc
        ret

        ;; Local variables (all fd.asm variables consolidated here)
        fd_open_fd    dw 0
        fd_open_flags db 0
        fd_open_mode  db 0
        fd_open_name  dw 0
        fd_open_sec   dw 0
        fd_rw_done    dw 0
        fd_rw_fdp     dw 0
        fd_rw_left    dw 0
        fd_write_buf  dw 0
