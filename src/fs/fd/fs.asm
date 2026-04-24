fd_read_dir:
        jmp vfs_read_dir

fd_read_file:
        ;; ESI = FD entry pointer
        mov [fd_rw_descriptor_pointer], esi
        push ebx
        push ecx
        push edx
        push edi
        ;; Clamp ECX to remaining file bytes (32-bit: size - position)
        mov eax, [esi+FD_OFFSET_SIZE]
        sub eax, [esi+FD_OFFSET_POSITION]
        js .rf_eof
        jz .rf_eof
        cmp ecx, eax
        jbe .rf_start
        mov ecx, eax            ; clamp to remaining
        .rf_start:
        mov [fd_rw_left], cx
        mov word [fd_rw_done], 0
        .rf_loop:
        cmp word [fd_rw_left], 0
        je .rf_done
        mov esi, [fd_rw_descriptor_pointer]
        call vfs_read_sec       ; ESI = fd entry → SECTOR_BUFFER filled, BX = byte offset
        jc .rf_disk_err
        ;; Chunk size = min(512 - offset, bytes_left)
        movzx ebx, bx           ; zero-extend byte offset (0-511)
        mov ecx, 512
        sub ecx, ebx            ; ECX = available in sector
        cmp cx, [fd_rw_left]
        jbe .rf_chunk_ok
        mov cx, [fd_rw_left]
        .rf_chunk_ok:
        ;; Copy ECX bytes from SECTOR_BUFFER+EBX to [EDI]
        push esi
        mov esi, SECTOR_BUFFER
        add esi, ebx
        cld
        push ecx
        rep movsb
        pop ecx
        pop esi
        ;; Update bookkeeping
        add word [fd_rw_done], cx
        sub word [fd_rw_left], cx
        mov esi, [fd_rw_descriptor_pointer]
        movzx ecx, cx
        add dword [esi+FD_OFFSET_POSITION], ecx
        jmp .rf_loop
        .rf_eof:
        pop edi
        pop edx
        pop ecx
        pop ebx
        xor eax, eax
        clc
        ret
        .rf_disk_err:
        pop edi
        pop edx
        pop ecx
        pop ebx
        mov ax, -1
        stc
        ret
        .rf_done:
        movzx eax, word [fd_rw_done]
        pop edi
        pop edx
        pop ecx
        pop ebx
        clc
        ret

fd_write_file:
        ;; ESI = FD entry pointer
        mov [fd_rw_descriptor_pointer], esi
        push ebx
        push ecx
        push edx
        push edi
        mov [fd_rw_left], cx
        mov word [fd_rw_done], 0
        .wf_loop:
        cmp word [fd_rw_left], 0
        je .wf_done
        mov esi, [fd_rw_descriptor_pointer]
        call vfs_prepare_write_sec  ; ESI=fd_entry → SECTOR_BUFFER ready, BX=byte offset
        jc .wf_disk_err
        ;; Chunk = min(512 - offset, bytes_left)
        movzx ebx, bx           ; zero-extend byte offset (0-511)
        mov ecx, 512
        sub ecx, ebx            ; ECX = space in sector
        cmp cx, [fd_rw_left]
        jbe .wf_chunk_ok
        mov cx, [fd_rw_left]
        .wf_chunk_ok:
        ;; Copy ECX bytes from user buffer to SECTOR_BUFFER+EBX
        push esi
        mov edi, SECTOR_BUFFER
        add edi, ebx
        mov esi, [fd_write_buffer]
        movzx ebx, word [fd_rw_done]
        add esi, ebx
        cld
        push ecx
        rep movsb
        pop ecx
        pop esi
        ;; Write the sector
        call vfs_commit_write_sec
        jc .wf_disk_err
        ;; Update bookkeeping
        add word [fd_rw_done], cx
        sub word [fd_rw_left], cx
        mov esi, [fd_rw_descriptor_pointer]
        movzx ecx, cx
        add dword [esi+FD_OFFSET_POSITION], ecx
        jmp .wf_loop
        .wf_disk_err:
        pop edi
        pop edx
        pop ecx
        pop ebx
        mov ax, -1
        stc
        ret
        .wf_done:
        movzx eax, word [fd_rw_done]
        pop edi
        pop edx
        pop ecx
        pop ebx
        clc
        ret

        fd_rw_descriptor_pointer dd 0
        fd_rw_done    dw 0
        fd_rw_left    dw 0
