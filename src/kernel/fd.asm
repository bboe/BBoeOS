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
        cmp byte [si+FD_OFFSET_TYPE], FD_TYPE_FREE
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
;;; For writable file FDs, calls vfs_update_size to write the final
;;; position back to the directory entry as the file size.
;;; -----------------------------------------------------------------------
fd_close:
        call fd_lookup
        jc .close_err
        cmp byte [si+FD_OFFSET_TYPE], FD_TYPE_FILE
        jne .close_free
        test byte [si+FD_OFFSET_FLAGS], O_WRONLY
        jz .close_free
        call vfs_update_size    ; SI = fd_table entry → updates dir entry size
        .close_free:
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
        mov al, [si+FD_OFFSET_MODE]
        mov dx, [si+FD_OFFSET_SIZE]
        mov cx, [si+FD_OFFSET_SIZE+2]
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
        mov byte [si+FD_OFFSET_TYPE], FD_TYPE_CONSOLE
        mov byte [si+FD_OFFSET_FLAGS], O_RDONLY
        add si, FD_ENTRY_SIZE
        mov byte [si+FD_OFFSET_TYPE], FD_TYPE_CONSOLE
        mov byte [si+FD_OFFSET_FLAGS], O_WRONLY
        add si, FD_ENTRY_SIZE
        mov byte [si+FD_OFFSET_TYPE], FD_TYPE_CONSOLE
        mov byte [si+FD_OFFSET_FLAGS], O_WRONLY
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
        cmp byte [si+FD_OFFSET_TYPE], FD_TYPE_FREE
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
        mov [fd_open_flags], al
        mov [fd_open_name], si
        ;; Look up the file (vfs_find handles "." → root directory)
        call vfs_find           ; populates vfs_found_*
        jc .open_not_found
        jmp .open_populate

        .open_not_found:
        ;; If O_CREAT is set, create the file
        test byte [fd_open_flags], O_CREAT
        jz .open_err
        mov si, [fd_open_name]
        call vfs_create         ; SI=path → vfs_found_*, CF on error
        jc .open_err
        jmp .open_populate

        .open_populate:
        ;; vfs_found_* is now fully populated
        call fd_alloc
        jc .open_err
        mov [fd_open_fd], ax
        ;; Type, flags, mode, inode, size, position from vfs_found_*
        mov cl, [vfs_found_type]
        mov [si+FD_OFFSET_TYPE], cl
        mov cl, [fd_open_flags]
        mov [si+FD_OFFSET_FLAGS], cl
        mov cl, [vfs_found_mode]
        mov [si+FD_OFFSET_MODE], cl
        mov cx, [vfs_found_inode]
        mov [si+FD_OFFSET_START], cx
        mov cx, [vfs_found_size]
        mov [si+FD_OFFSET_SIZE], cx
        mov cx, [vfs_found_size+2]
        mov [si+FD_OFFSET_SIZE+2], cx
        mov word [si+FD_OFFSET_POSITION], 0
        mov word [si+FD_OFFSET_POSITION+2], 0
        mov cx, [vfs_found_dir_sec]
        mov [si+FD_OFFSET_DIRECTORY_SECTOR], cx
        mov cx, [vfs_found_dir_off]
        mov [si+FD_OFFSET_DIRECTORY_OFFSET], cx
        ;; O_TRUNC: reset size to 0
        test byte [fd_open_flags], O_TRUNC
        jz .open_done
        mov word [si+FD_OFFSET_SIZE], 0
        mov word [si+FD_OFFSET_SIZE+2], 0
        .open_done:
        mov ax, [fd_open_fd]
        pop di
        pop dx
        pop cx
        clc
        ret

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
        mov ax, [si+FD_OFFSET_POSITION+2]
        mov bx, [si+FD_OFFSET_POSITION]
        ;; AX:BX >> 9 = sector offset
        shl ax, 7
        push cx
        mov cx, bx
        shr cx, 9
        or ax, cx
        pop cx
        add ax, [si+FD_OFFSET_START]
        ;; BX = pos & 0x1FF
        and bx, 01FFh
        ret

;;; -----------------------------------------------------------------------
;;; fd_read / fd_write: Table-driven dispatch via fd_ops.
;;;
;;; fd_ops is a flat table of (read_fn, write_fn) word pairs indexed by
;;; FD_TYPE_*.  A zero entry means the operation is unsupported for that
;;; type.  Adding a new fd type requires only a new row in fd_ops — the
;;; dispatch functions need no changes.
;;; -----------------------------------------------------------------------
fd_read:
        call fd_lookup
        jc .err
        xor bh, bh
        mov bl, [si+FD_OFFSET_TYPE]
        shl bx, 2               ; * 4: each ops entry is two words
        mov ax, [fd_ops+bx]     ; read_fn
        test ax, ax
        jz .err
        jmp ax
        .err:
        mov ax, -1
        stc
        ret

fd_write:
        mov [fd_write_buffer], si
        call fd_lookup
        jc .err
        xor bh, bh
        mov bl, [si+FD_OFFSET_TYPE]
        shl bx, 2               ; * 4: each ops entry is two words
        mov ax, [fd_ops+bx+2]   ; write_fn
        test ax, ax
        jz .err
        jmp ax
        .err:
        mov ax, -1
        stc
        ret

        ;; Operations table: (read_fn, write_fn) indexed by FD_TYPE_*
        ;; A zero entry means unsupported for that type.
fd_ops:
        dw 0,               0                ; FD_TYPE_FREE (0)
        dw fd_read_file,    fd_write_file     ; FD_TYPE_FILE (1)
        dw fd_read_console, fd_write_console  ; FD_TYPE_CONSOLE (2)
        dw fd_read_dir,     0                 ; FD_TYPE_DIRECTORY (3)
        dw fd_read_net,     fd_write_net      ; FD_TYPE_NET (4)
        dw 0,               0                 ; FD_TYPE_UDP (5)
        dw 0,               0                 ; FD_TYPE_ICMP (6)

        ;; Variables
        fd_open_fd    dw 0
        fd_open_flags db 0
        fd_open_mode  db 0
        fd_open_name  dw 0
        fd_table times FD_MAX * FD_ENTRY_SIZE db 0
        fd_write_buffer dw 0

%include "fd/console.asm"
%include "fd/fs.asm"
%include "fd/net.asm"
