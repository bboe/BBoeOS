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

        ;; Print the entry name (find length, then write)
        mov di, entry_buf
        xor al, al
        mov cx, DIRECTORY_NAME_LENGTH
        repne scasb
        sub di, entry_buf
        dec di                 ; DI = string length
        mov cx, di
        mov si, entry_buf
        call FUNCTION_WRITE_STDOUT

        ;; Check flags for suffix
        test byte [entry_buf+DIRECTORY_OFFSET_FLAGS], FLAG_DIRECTORY
        jz .check_exec
        mov al, '/'
        call FUNCTION_PRINT_CHARACTER
        jmp .newline
        .check_exec:
        test byte [entry_buf+DIRECTORY_OFFSET_FLAGS], FLAG_EXECUTE
        jz .newline
        mov al, '*'
        call FUNCTION_PRINT_CHARACTER

.newline:
        mov al, 10
        call FUNCTION_PRINT_CHARACTER
        jmp .read_loop

.done:
        mov bx, bp
        mov ah, SYS_IO_CLOSE
        int 30h
        jmp FUNCTION_EXIT

.not_found:
        mov si, MESSAGE_NOT_FOUND
        mov cx, MESSAGE_NOT_FOUND_LENGTH
        jmp FUNCTION_DIE

;; Strings
DOT           db '.',0
MESSAGE_NOT_FOUND db `Not found\n`
MESSAGE_NOT_FOUND_LENGTH equ $ - MESSAGE_NOT_FOUND


;; Buffer for one directory entry (32 bytes)
entry_buf:
