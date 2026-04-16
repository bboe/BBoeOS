        org 0600h

%include "constants.asm"

main:
        cld

        ;; Require exactly two arguments
        mov di, ARGV
        call FUNCTION_PARSE_ARGV
        cmp cx, 2
        jne .usage

        ;; Validate newname length
        mov si, [ARGV]         ; SI = oldname (for syscall later)
        mov di, [ARGV+2]       ; DI = newname (for syscall later)
        push si
        push di
        mov si, di
        xor cx, cx
        .count_new:
        lodsb
        test al, al
        jz .count_done
        inc cx
        cmp cx, DIRECTORY_NAME_LENGTH - 1
        ja .name_too_long
        jmp .count_new
        .name_too_long:
        pop di
        pop si
        jmp .toolong
        .count_done:
        pop di
        pop si

        ;; SI = oldname, DI = newname
        mov ah, SYS_FS_RENAME
        int 30h
        jnc .done

        cmp al, ERROR_EXISTS
        je .exists
        cmp al, ERROR_PROTECTED
        je .protected
        ;; ERROR_NOT_FOUND (or unknown)
        mov si, MESSAGE_NOT_FOUND
        mov cx, MESSAGE_NOT_FOUND_LENGTH
        jmp .error
        .exists:
        mov si, MESSAGE_EXISTS
        mov cx, MESSAGE_EXISTS_LENGTH
        jmp .error
        .protected:
        mov si, MESSAGE_PROTECTED
        mov cx, MESSAGE_PROTECTED_LENGTH
        .error:
        jmp FUNCTION_DIE

        .done:
        jmp FUNCTION_EXIT

        .toolong:
        mov si, MESSAGE_TOO_LONG
        mov cx, MESSAGE_TOO_LONG_LENGTH
        jmp FUNCTION_DIE

        .usage:
        mov si, MESSAGE_USAGE
        mov cx, MESSAGE_USAGE_LENGTH
        jmp FUNCTION_DIE

        MESSAGE_EXISTS    db `File already exists\n`
        MESSAGE_EXISTS_LENGTH equ $ - MESSAGE_EXISTS
        MESSAGE_NOT_FOUND db `File not found\n`
        MESSAGE_NOT_FOUND_LENGTH equ $ - MESSAGE_NOT_FOUND
        MESSAGE_PROTECTED db `File is protected\n`
        MESSAGE_PROTECTED_LENGTH equ $ - MESSAGE_PROTECTED
        MESSAGE_TOO_LONG  db `Name too long (max 26 chars)\n`
        MESSAGE_TOO_LONG_LENGTH equ $ - MESSAGE_TOO_LONG
        MESSAGE_USAGE     db `Usage: mv <oldname> <newname>\n`
        MESSAGE_USAGE_LENGTH equ $ - MESSAGE_USAGE

