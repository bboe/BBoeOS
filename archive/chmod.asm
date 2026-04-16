        org 0600h

%include "constants.asm"

main:
        cld

        ;; Require exactly two arguments: mode (+x/-x) and filename
        mov di, ARGV
        call FUNCTION_PARSE_ARGV
        cmp cx, 2
        jne .usage

        ;; Parse mode argument (argv[0])
        mov si, [ARGV]
        lodsb
        cmp al, '+'
        je .set_exec
        cmp al, '-'
        je .clear_exec
        jmp .usage

        .set_exec:
        mov dl, FLAG_EXECUTE
        jmp .check_x
        .clear_exec:
        xor dl, dl

        .check_x:
        lodsb
        cmp al, 'x'
        jne .usage
        cmp byte [si], 0      ; Mode arg must be exactly 2 chars
        jne .usage

        ;; SI = filename (argv[1])
        mov si, [ARGV+2]
        mov al, dl             ; AL = new flags value
        mov ah, SYS_FS_CHMOD
        int 30h
        jnc .done

        cmp al, ERROR_PROTECTED
        je .protected
        ;; ERROR_NOT_FOUND (or unknown)
        mov si, MESSAGE_NOT_FOUND
        mov cx, MESSAGE_NOT_FOUND_LENGTH
        jmp .error
        .protected:
        mov si, MESSAGE_PROTECTED
        mov cx, MESSAGE_PROTECTED_LENGTH
        .error:
        jmp FUNCTION_DIE

        .done:
        jmp FUNCTION_EXIT

        .usage:
        mov si, MESSAGE_USAGE
        mov cx, MESSAGE_USAGE_LENGTH
        jmp FUNCTION_DIE

        MESSAGE_NOT_FOUND db `File not found\n`
        MESSAGE_NOT_FOUND_LENGTH equ $ - MESSAGE_NOT_FOUND
        MESSAGE_PROTECTED db `File is protected\n`
        MESSAGE_PROTECTED_LENGTH equ $ - MESSAGE_PROTECTED
        MESSAGE_USAGE     db `Usage: chmod [+x|-x] <file>\n`
        MESSAGE_USAGE_LENGTH equ $ - MESSAGE_USAGE

