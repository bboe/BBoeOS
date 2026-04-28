        [bits 32]
        org 0600h

%include "constants.asm"

main:
        cld

        ;; Parse arguments (0 or 1)
        mov edi, ARGV
        call FUNCTION_PARSE_ARGV
        cmp ecx, 1
        ja .not_found

        ;; Open directory: use argument or "." for root
        je .have_arg
        .open_root:
        mov esi, DOT
        jmp .open_dir
        .have_arg:
        mov esi, [ARGV]
        .open_dir:
        mov al, O_RDONLY
        mov ah, SYS_IO_OPEN
        int 30h
        jc .not_found

        mov ebp, eax           ; EBP = dir fd

        ;; Read entries one at a time
.read_loop:
        mov ebx, ebp
        mov edi, entry_buf
        mov ecx, DIRECTORY_ENTRY_SIZE
        mov ah, SYS_IO_READ
        int 30h
        test eax, eax
        jz .done                ; EOF

        ;; Print the entry name (find length, then write)
        mov edi, entry_buf
        xor al, al
        mov ecx, DIRECTORY_NAME_LENGTH
        repne scasb
        sub edi, entry_buf
        dec edi                ; EDI = string length
        mov ecx, edi
        mov esi, entry_buf
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
        mov ebx, ebp
        mov ah, SYS_IO_CLOSE
        int 30h
        jmp FUNCTION_EXIT

.not_found:
        mov esi, MESSAGE_NOT_FOUND
        mov ecx, MESSAGE_NOT_FOUND_LENGTH
        jmp FUNCTION_DIE

;; Strings
DOT           db '.',0
MESSAGE_NOT_FOUND db `Not found\n`
MESSAGE_NOT_FOUND_LENGTH equ $ - MESSAGE_NOT_FOUND


;; Buffer for one directory entry (32 bytes)
entry_buf:
