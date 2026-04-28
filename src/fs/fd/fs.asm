fd_read_dir:
        ;; vfs_read_dir returns the entry size in AX (DIRECTORY_ENTRY_SIZE
        ;; or 0 at EOF) plus -1/CF on error.  io_read now skips the
        ;; dispatcher's movsx, so sign-extend AX into EAX here so callers
        ;; see a clean 32-bit return.
        call vfs_read_dir
        movsx eax, ax
        ret

fd_read_file:
        ;; ESI = FD entry pointer.  ECX = byte count (full dword; capped
        ;; only by the remaining-bytes-in-file clamp below).
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
        mov [fd_rw_left], ecx
        mov dword [fd_rw_done], 0
        .rf_loop:
        cmp dword [fd_rw_left], 0
        je .rf_done
        mov esi, [fd_rw_descriptor_pointer]
        call vfs_read_sec       ; ESI = fd entry → sector_buffer filled, BX = byte offset
        jc .rf_disk_err
        ;; Chunk size = min(512 - offset, bytes_left).  Per-iteration chunk
        ;; never exceeds 512, but ``fd_rw_left`` is dword so the compare /
        ;; reload widen to ECX (no operand-size mismatch).
        movzx ebx, bx           ; zero-extend byte offset (0-511)
        mov ecx, 512
        sub ecx, ebx            ; ECX = available in sector
        cmp ecx, [fd_rw_left]
        jbe .rf_chunk_ok
        mov ecx, [fd_rw_left]   ; left < 512: clamp chunk to remaining
        .rf_chunk_ok:
        ;; Copy ECX bytes from sector_buffer+EBX to [EDI]
        push esi
        mov esi, sector_buffer
        add esi, ebx
        cld
        push ecx
        rep movsb
        pop ecx
        pop esi
        ;; Update bookkeeping (ECX still holds the just-copied chunk size)
        add [fd_rw_done], ecx
        sub [fd_rw_left], ecx
        mov esi, [fd_rw_descriptor_pointer]
        add [esi+FD_OFFSET_POSITION], ecx
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
        mov eax, -1
        stc
        ret
        .rf_done:
        mov eax, [fd_rw_done]
        pop edi
        pop edx
        pop ecx
        pop ebx
        clc
        ret

fd_write_file:
        ;; ESI = FD entry pointer.  ECX = byte count (full dword).
        mov [fd_rw_descriptor_pointer], esi
        push ebx
        push ecx
        push edx
        push edi
        mov [fd_rw_left], ecx
        mov dword [fd_rw_done], 0
        .wf_loop:
        cmp dword [fd_rw_left], 0
        je .wf_done
        mov esi, [fd_rw_descriptor_pointer]
        call vfs_prepare_write_sec  ; ESI=fd_entry → sector_buffer ready, BX=byte offset
        jc .wf_disk_err
        ;; Chunk = min(512 - offset, bytes_left)
        movzx ebx, bx           ; zero-extend byte offset (0-511)
        mov ecx, 512
        sub ecx, ebx            ; ECX = space in sector
        cmp ecx, [fd_rw_left]
        jbe .wf_chunk_ok
        mov ecx, [fd_rw_left]
        .wf_chunk_ok:
        ;; Copy ECX bytes from user buffer to sector_buffer+EBX
        push esi
        mov edi, sector_buffer
        add edi, ebx
        mov esi, [fd_write_buffer]
        add esi, [fd_rw_done]
        cld
        push ecx
        rep movsb
        pop ecx
        pop esi
        ;; Write the sector
        call vfs_commit_write_sec
        jc .wf_disk_err
        ;; Update bookkeeping
        add [fd_rw_done], ecx
        sub [fd_rw_left], ecx
        mov esi, [fd_rw_descriptor_pointer]
        add [esi+FD_OFFSET_POSITION], ecx
        jmp .wf_loop
        .wf_disk_err:
        pop edi
        pop edx
        pop ecx
        pop ebx
        mov eax, -1
        stc
        ret
        .wf_done:
        mov eax, [fd_rw_done]
        pop edi
        pop edx
        pop ecx
        pop ebx
        clc
        ret

        fd_rw_descriptor_pointer dd 0
        fd_rw_done    dd 0
        fd_rw_left    dd 0
