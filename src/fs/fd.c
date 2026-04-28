asm("
;;; fd.c -- File descriptor table management
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
        push ebx
        push ecx
        mov esi, fd_table
        xor eax, eax
        mov ecx, FD_MAX
        .scan:
        cmp byte [esi+FD_OFFSET_TYPE], FD_TYPE_FREE
        je .found
        add esi, FD_ENTRY_SIZE
        inc eax
        dec ecx
        jnz .scan
        pop ecx
        pop ebx
        stc
        ret
        .found:
        pop ecx
        pop ebx
        clc
        ret

;;; -----------------------------------------------------------------------
;;; fd_close: Close a file descriptor
;;; Input:  BX = fd number
;;; Output: CF set on error
;;;
;;; For writable file FDs, calls vfs_update_size to write the final
;;; position back to the directory entry as the file size.
;;; -----------------------------------------------------------------------
fd_close:
        call fd_lookup
        jc .close_err
        cmp byte [esi+FD_OFFSET_TYPE], FD_TYPE_FILE
        jne .close_free
        test byte [esi+FD_OFFSET_FLAGS], O_WRONLY
        jz .close_free
        call vfs_update_size    ; ESI = fd_table entry -> updates dir entry size
        .close_free:
        push eax
        push ecx
        push edi
        mov edi, esi
        xor eax, eax
        mov ecx, FD_ENTRY_SIZE / 2
        cld
        rep stosw
        pop edi
        pop ecx
        pop eax
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
        mov al, [esi+FD_OFFSET_MODE]
        mov dx, [esi+FD_OFFSET_SIZE]
        mov cx, [esi+FD_OFFSET_SIZE+2]
        clc
        ret
        .fstat_err:
        stc
        ret

fd_init:
        ;; Zero the entire FD table
        push eax
        push ecx
        push edi
        mov edi, fd_table
        xor eax, eax
        mov ecx, FD_MAX * FD_ENTRY_SIZE / 2
        cld
        rep stosw
        ;; Pre-open fd 0 (stdin), fd 1 (stdout), fd 2 (stderr) as console
        mov esi, fd_table
        mov byte [esi+FD_OFFSET_TYPE], FD_TYPE_CONSOLE
        mov byte [esi+FD_OFFSET_FLAGS], O_RDONLY
        add esi, FD_ENTRY_SIZE
        mov byte [esi+FD_OFFSET_TYPE], FD_TYPE_CONSOLE
        mov byte [esi+FD_OFFSET_FLAGS], O_WRONLY
        add esi, FD_ENTRY_SIZE
        mov byte [esi+FD_OFFSET_TYPE], FD_TYPE_CONSOLE
        mov byte [esi+FD_OFFSET_FLAGS], O_WRONLY
        pop edi
        pop ecx
        pop eax
        ret

fd_lookup:
        ;; Validate fd in BX, return SI = entry pointer
        ;; CF set if invalid (out of range or slot is free)
        cmp bx, FD_MAX
        jae .invalid
        push eax
        movzx eax, bx
        shl eax, 5              ; eax = fd_number * FD_ENTRY_SIZE (32)
        mov esi, fd_table
        add esi, eax
        cmp byte [esi+FD_OFFSET_TYPE], FD_TYPE_FREE
        je .invalid_pop
        pop eax
        clc
        ret
        .invalid_pop:
        pop eax
        .invalid:
        stc
        ret

;;; -----------------------------------------------------------------------
;;; fd_open: Open a file and return a file descriptor
;;; Input:  SI = filename, AL = flags (O_RDONLY, O_WRONLY, O_CREAT, O_TRUNC)
;;; Output: AX = fd number (CF clear), or -1 on error (CF set)
;;; -----------------------------------------------------------------------
fd_open:
        push ecx
        push edx
        push edi
        mov [fd_open_flags], al
        mov [fd_open_name], esi
        ;; Check synthetic device paths first (no filesystem lookup).
        mov edi, DEV_VGA_PATH
        mov ecx, 9                      ; \"/dev/vga\" + null
        cld
        repe cmpsb
        jne .open_not_device
        call fd_alloc
        jc .open_err
        mov byte [esi+FD_OFFSET_TYPE], FD_TYPE_VGA
        mov cl, [fd_open_flags]
        mov [esi+FD_OFFSET_FLAGS], cl
        mov [fd_open_fd], ax
        jmp .open_done
        .open_not_device:
        mov esi, [fd_open_name]
        ;; Look up the file (vfs_find handles \".\" -> root directory)
        call vfs_find           ; populates vfs_found_*
        jc .open_not_found
        jmp .open_populate

        .open_not_found:
        ;; If O_CREAT is set, create the file
        test byte [fd_open_flags], O_CREAT
        jz .open_err
        mov esi, [fd_open_name]
        call vfs_create         ; SI=path -> vfs_found_*, CF on error
        jc .open_err
        jmp .open_populate

        .open_populate:
        ;; vfs_found_* is now fully populated
        call fd_alloc
        jc .open_err
        mov [fd_open_fd], ax
        ;; Type, flags, mode, inode, size, position from vfs_found_*
        mov cl, [vfs_found_type]
        mov [esi+FD_OFFSET_TYPE], cl
        mov cl, [fd_open_flags]
        mov [esi+FD_OFFSET_FLAGS], cl
        mov cl, [vfs_found_mode]
        mov [esi+FD_OFFSET_MODE], cl
        mov cx, [vfs_found_inode]
        mov [esi+FD_OFFSET_START], cx
        mov cx, [vfs_found_size]
        mov [esi+FD_OFFSET_SIZE], cx
        mov cx, [vfs_found_size+2]
        mov [esi+FD_OFFSET_SIZE+2], cx
        mov dword [esi+FD_OFFSET_POSITION], 0
        mov cx, [vfs_found_dir_sec]
        mov [esi+FD_OFFSET_DIRECTORY_SECTOR], cx
        mov cx, [vfs_found_dir_off]
        mov [esi+FD_OFFSET_DIRECTORY_OFFSET], cx
        ;; O_TRUNC: reset size to 0
        test byte [fd_open_flags], O_TRUNC
        jz .open_done
        mov word [esi+FD_OFFSET_SIZE], 0
        mov word [esi+FD_OFFSET_SIZE+2], 0
        .open_done:
        mov ax, [fd_open_fd]
        pop edi
        pop edx
        pop ecx
        clc
        ret

        .open_err:
        pop edi
        pop edx
        pop ecx
        mov ax, -1
        stc
        ret

;;; -----------------------------------------------------------------------
;;; fd_pos_to_sector: Convert fd_pos to absolute sector + byte offset
;;; Input:  SI = FD entry pointer
;;; Output: AX = absolute sector number, BX = byte offset within sector
;;; -----------------------------------------------------------------------
fd_pos_to_sector:
        push ecx
        mov eax, [esi+FD_OFFSET_POSITION]
        mov ebx, eax
        shr eax, 9
        movzx ecx, word [esi+FD_OFFSET_START]
        add eax, ecx
        and ebx, 01FFh
        pop ecx
        ret

;;; -----------------------------------------------------------------------
;;; fd_read / fd_write: Table-driven dispatch via fd_ops.
;;;
;;; fd_ops is a flat table of (read_fn, write_fn) dword pairs indexed by
;;; FD_TYPE_*.  A zero entry means the operation is unsupported for that
;;; type.  Adding a new fd type requires only a new row in fd_ops -- the
;;; dispatch functions need no changes.
;;; -----------------------------------------------------------------------
fd_read:
        call fd_lookup
        jc .err
        movzx ebx, byte [esi+FD_OFFSET_TYPE]
        shl ebx, 3              ; * 8: each ops entry is two dwords
        mov eax, [fd_ops+ebx]  ; read_fn
        test eax, eax
        jz .err
        jmp eax
        .err:
        mov eax, -1
        stc
        ret

fd_write:
        mov [fd_write_buffer], esi
        call fd_lookup
        jc .err
        movzx ebx, byte [esi+FD_OFFSET_TYPE]
        shl ebx, 3              ; * 8: each ops entry is two dwords
        mov eax, [fd_ops+ebx+4] ; write_fn
        test eax, eax
        jz .err
        jmp eax
        .err:
        mov eax, -1
        stc
        ret

;;; -----------------------------------------------------------------------
;;; fd_ioctl: Device-control dispatch.  Looks up BX=fd, then jumps to the
;;; per-type ioctl handler in fd_ioctl_ops.  Handler receives AL=cmd plus
;;; cmd-specific args in other registers and returns CF=0/1.
;;; -----------------------------------------------------------------------
fd_ioctl:
        call fd_lookup
        jc .err
        movzx ebx, byte [esi+FD_OFFSET_TYPE]
        shl ebx, 2              ; one dword per entry
        mov ebx, [fd_ioctl_ops+ebx]
        test ebx, ebx
        jz .err
        jmp ebx
        .err:
        stc
        ret

%include \"fs/fd/console.asm\"
%include \"fs/fd/fs.asm\"
%include \"fs/fd/net.asm\"

        ;; Operations table: (read_fn, write_fn) dword pairs indexed by FD_TYPE_*
        ;; A zero entry means unsupported for that type.
fd_ops:
        dd 0,               0                 ; FD_TYPE_FREE (0)
        dd fd_read_console, fd_write_console  ; FD_TYPE_CONSOLE (1)
        dd fd_read_dir,     0                 ; FD_TYPE_DIRECTORY (2)
        dd fd_read_file,    fd_write_file     ; FD_TYPE_FILE (3)
        dd 0,               0                 ; FD_TYPE_ICMP (4)
        dd fd_read_net,     fd_write_net      ; FD_TYPE_NET (5)
        dd 0,               0                 ; FD_TYPE_UDP (6)
        dd 0,               0                 ; FD_TYPE_VGA (7)

        ;; Ioctl dispatch table indexed by FD_TYPE_*.  Zero = unsupported.
fd_ioctl_ops:
        dd 0                    ; FD_TYPE_FREE (0)
        dd 0                    ; FD_TYPE_CONSOLE (1)
        dd 0                    ; FD_TYPE_DIRECTORY (2)
        dd 0                    ; FD_TYPE_FILE (3)
        dd 0                    ; FD_TYPE_ICMP (4)
        dd 0                    ; FD_TYPE_NET (5)
        dd 0                    ; FD_TYPE_UDP (6)
        dd fd_ioctl_vga         ; FD_TYPE_VGA (7)

        DEV_VGA_PATH    db \"/dev/vga\", 0
        fd_open_fd      dw 0
        fd_open_flags   db 0
        fd_open_mode    db 0
        fd_open_name    dd 0
        ;; fd_table lives at fixed low-physical 0xE000 (kernel-virt
        ;; 0xC000E000 via the direct map; see EQU in kernel.asm).
        ;; Pinned low so bbfs.asm / ext2.asm can keep their `[si+...]`
        ;; / `[di+...]` 16-bit-register accesses to FD entries
        ;; without 32-bit-register conversion churn.  fd_init zeroes
        ;; it through the kernel-virt alias.
        fd_write_buffer dd 0
");
