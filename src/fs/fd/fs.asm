fd_read_dir:
        jmp vfs_read_dir

fd_read_file:
        ;; SI = FD entry pointer
        mov [fd_rw_descriptor_pointer], si
        push bx
        push cx
        push dx
        push di
        ;; Clamp CX to remaining file bytes
        mov ax, [si+FD_OFFSET_SIZE]
        sub ax, [si+FD_OFFSET_POSITION]
        mov dx, [si+FD_OFFSET_SIZE+2]
        sbb dx, [si+FD_OFFSET_POSITION+2]
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
        mov si, [fd_rw_descriptor_pointer]
        call vfs_read_sec       ; SI = fd entry → SECTOR_BUFFER filled, BX = byte offset
        jc .rf_disk_err
        ;; Chunk size = min(512 - offset, bytes_left)
        mov cx, 512
        sub cx, bx              ; CX = available in sector
        cmp cx, [fd_rw_left]
        jbe .rf_chunk_ok
        mov cx, [fd_rw_left]
        .rf_chunk_ok:
        ;; Copy CX bytes from SECTOR_BUFFER+BX to [DI]
        push si
        mov si, SECTOR_BUFFER
        add si, bx
        cld
        push cx                 ; save chunk size
        rep movsb               ; copies CX bytes, DI advances
        pop cx                  ; CX = chunk size
        pop si
        ;; Update bookkeeping
        add [fd_rw_done], cx
        sub [fd_rw_left], cx
        mov si, [fd_rw_descriptor_pointer]
        add [si+FD_OFFSET_POSITION], cx
        adc word [si+FD_OFFSET_POSITION+2], 0
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

fd_write_file:
        ;; SI = FD entry pointer
        mov [fd_rw_descriptor_pointer], si
        push bx
        push cx
        push dx
        push di
        mov [fd_rw_left], cx
        mov word [fd_rw_done], 0
        .wf_loop:
        cmp word [fd_rw_left], 0
        je .wf_done
        mov si, [fd_rw_descriptor_pointer]
        call vfs_prepare_write_sec  ; SI=fd_entry → SECTOR_BUFFER ready, BX=byte offset
        jc .wf_disk_err
        ;; Chunk = min(512 - offset, bytes_left)
        mov cx, 512
        sub cx, bx              ; CX = space in sector
        cmp cx, [fd_rw_left]
        jbe .wf_chunk_ok
        mov cx, [fd_rw_left]
        .wf_chunk_ok:
        ;; Copy CX bytes from user buffer to SECTOR_BUFFER+BX
        push si
        mov di, SECTOR_BUFFER
        add di, bx
        mov si, [fd_write_buffer]
        add si, [fd_rw_done]    ; advance past already-written bytes
        cld
        push cx
        rep movsb
        pop cx
        pop si
        ;; Write the sector
        call vfs_commit_write_sec
        jc .wf_disk_err
        ;; Update bookkeeping
        add [fd_rw_done], cx
        sub [fd_rw_left], cx
        mov si, [fd_rw_descriptor_pointer]
        add [si+FD_OFFSET_POSITION], cx
        adc word [si+FD_OFFSET_POSITION+2], 0
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

        fd_rw_descriptor_pointer dw 0
        fd_rw_done    dw 0
        fd_rw_left    dw 0
