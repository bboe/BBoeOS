        org 0600h

%include "constants.asm"

main:
        cld

        ;; Open directory: use argument or "." for root
        mov si, [EXEC_ARG]
        test si, si
        jz .open_root
        cmp byte [si], 0
        jne .open_dir
        .open_root:
        mov si, DOT
        .open_dir:
        mov al, O_RDONLY
        mov ah, SYS_IO_OPEN
        int 30h
        jc .not_found

        mov bp, ax             ; BP = dir fd

        ;; Read entries one at a time
.read_loop:
        mov bx, bp
        mov di, entry_buf
        mov cx, DIR_ENTRY_SIZE
        mov ah, SYS_IO_READ
        int 30h
        test ax, ax
        jz .done                ; EOF

        ;; Print the entry name (null-terminated at offset 0)
        mov si, entry_buf
        mov ah, SYS_IO_PUTS
        int 30h

        ;; Check flags for suffix
        test byte [entry_buf+DIR_OFF_FLAGS], FLAG_DIR
        jz .check_exec
        mov al, '/'
        mov ah, SYS_IO_PUTC
        int 30h
        jmp .newline
        .check_exec:
        test byte [entry_buf+DIR_OFF_FLAGS], FLAG_EXEC
        jz .newline
        mov al, '*'
        mov ah, SYS_IO_PUTC
        int 30h

.newline:
        mov al, 10
        mov ah, SYS_IO_PUTC
        int 30h
        jmp .read_loop

.done:
        mov bx, bp
        mov ah, SYS_IO_CLOSE
        int 30h
        mov ah, SYS_EXIT
        int 30h

.not_found:
        mov si, MSG_NOT_FOUND
        mov ah, SYS_IO_PUTS
        int 30h
        mov ah, SYS_EXIT
        int 30h

;; Strings
DOT           db '.',0
MSG_NOT_FOUND db `Not found\n\0`

;; Buffer for one directory entry (32 bytes)
entry_buf:
