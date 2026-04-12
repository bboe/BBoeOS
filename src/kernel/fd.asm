;;; fd.asm -- File descriptor table management
;;;
;;; fd_alloc:  Find the first free FD slot (AX = fd number, CF if full)
;;; fd_init:   Zero the FD table, pre-open fds 0/1/2 as console
;;; fd_lookup: Validate fd in BX, return SI = entry pointer (CF if invalid)

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
