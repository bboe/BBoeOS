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
        mov cx, DIRECTORY_ENTRY_SIZE
        mov ah, SYS_IO_READ
        int 30h
        test ax, ax
        jz .done                ; EOF

        ;; Print the entry name (null-terminated at offset 0)
        mov si, entry_buf
        mov ah, SYS_IO_PUT_STRING
        int 30h

        ;; Check flags for suffix
        test byte [entry_buf+DIRECTORY_OFFSET_FLAGS], FLAG_DIRECTORY
        jz .check_exec
        mov al, '/'
        mov ah, SYS_IO_PUT_CHARACTER
        int 30h
        jmp .newline
        .check_exec:
        test byte [entry_buf+DIRECTORY_OFFSET_FLAGS], FLAG_EXECUTE
        jz .newline
        mov al, '*'
        mov ah, SYS_IO_PUT_CHARACTER
        int 30h

.newline:
        mov al, 10
        mov ah, SYS_IO_PUT_CHARACTER
        int 30h
        jmp .read_loop

.done:
        mov bx, bp
        mov ah, SYS_IO_CLOSE
        int 30h
        mov ah, SYS_EXIT
        int 30h

.not_found:
        mov si, MESSAGE_NOT_FOUND
        mov ah, SYS_IO_PUT_STRING
        int 30h
        mov ah, SYS_EXIT
        int 30h

;; Strings
DOT           db '.',0
MESSAGE_NOT_FOUND db `Not found\n\0`

;; Buffer for one directory entry (32 bytes)
entry_buf:
