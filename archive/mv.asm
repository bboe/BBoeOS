        [bits 32]
        org 0600h

%include "constants.asm"

main:
        cld

        ;; Linux-style argv: reserve a stack buffer for the pointer
        ;; slots (matches cc.py's main prologue shape) and require
        ;; argc == 3 (basename, oldname, newname).
        sub esp, ARGV_RESERVE_BYTES
        mov edi, esp
        mov ecx, ARGV_RESERVE_BYTES / 4
        call FUNCTION_PARSE_ARGV
        cmp ecx, 3
        jne .usage

        ;; Validate newname length.
        mov esi, [esp+4]        ; ESI = oldname (argv[1]; for syscall later)
        mov edi, [esp+8]        ; EDI = newname (argv[2]; for syscall later)
        push esi
        push edi
        mov esi, edi
        xor ecx, ecx
        .count_new:
        lodsb
        test al, al
        jz .count_done
        inc ecx
        cmp ecx, DIRECTORY_NAME_LENGTH - 1
        ja .name_too_long
        jmp .count_new
        .name_too_long:
        pop edi
        pop esi
        jmp .toolong
        .count_done:
        pop edi
        pop esi

        ;; ESI = oldname, EDI = newname
        mov ah, SYS_FS_RENAME
        int 30h
        jnc .done

        cmp al, ERROR_EXISTS
        je .exists
        cmp al, ERROR_PROTECTED
        je .protected
        ;; ERROR_NOT_FOUND (or unknown)
        mov esi, MESSAGE_NOT_FOUND
        mov ecx, MESSAGE_NOT_FOUND_LENGTH
        jmp .error
        .exists:
        mov esi, MESSAGE_EXISTS
        mov ecx, MESSAGE_EXISTS_LENGTH
        jmp .error
        .protected:
        mov esi, MESSAGE_PROTECTED
        mov ecx, MESSAGE_PROTECTED_LENGTH
        .error:
        jmp FUNCTION_DIE

        .done:
        jmp FUNCTION_EXIT

        .toolong:
        mov esi, MESSAGE_TOO_LONG
        mov ecx, MESSAGE_TOO_LONG_LENGTH
        jmp FUNCTION_DIE

        .usage:
        mov esi, MESSAGE_USAGE
        mov ecx, MESSAGE_USAGE_LENGTH
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

