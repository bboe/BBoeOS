        [bits 32]
        org 0600h

%include "constants.asm"

main:
        cld

        ;; Linux-style argv: reserve a stack buffer for the pointer
        ;; slots (matches cc.py's main prologue shape) and require
        ;; argc == 3 (basename, mode, filename).
        ;; Linux SysV i386 startup: argc at [esp], argv ptrs at [esp+4..].
        ;; Pop argc into ECX and leave argv[0] at [esp+0] to match the
        ;; legacy parse_argv layout this program is written against.
        pop ecx                                 ; ecx = argc
        mov edi, esp                            ; edi = argv base
        cmp ecx, 3
        jne .usage

        ;; Parse mode argument (argv[1] = first user arg).  ESP points at
        ;; the argv pointer array; argv[1] is at [esp+4].
        mov esi, [esp+4]
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
        cmp byte [esi], 0     ; Mode arg must be exactly 2 chars
        jne .usage

        ;; ESI = filename (argv[2]).
        mov esi, [esp+8]
        mov al, dl             ; AL = new flags value
        mov ah, SYS_FS_CHMOD
        int 30h
        jnc .done

        cmp al, ERROR_PROTECTED
        je .protected
        ;; ERROR_NOT_FOUND (or unknown)
        mov esi, MESSAGE_NOT_FOUND
        mov ecx, MESSAGE_NOT_FOUND_LENGTH
        jmp .error
        .protected:
        mov esi, MESSAGE_PROTECTED
        mov ecx, MESSAGE_PROTECTED_LENGTH
        .error:
        jmp FUNCTION_DIE

        .done:
        jmp FUNCTION_EXIT

        .usage:
        mov esi, MESSAGE_USAGE
        mov ecx, MESSAGE_USAGE_LENGTH
        jmp FUNCTION_DIE

        MESSAGE_NOT_FOUND db `File not found\n`
        MESSAGE_NOT_FOUND_LENGTH equ $ - MESSAGE_NOT_FOUND
        MESSAGE_PROTECTED db `File is protected\n`
        MESSAGE_PROTECTED_LENGTH equ $ - MESSAGE_PROTECTED
        MESSAGE_USAGE     db `Usage: chmod [+x|-x] <file>\n`
        MESSAGE_USAGE_LENGTH equ $ - MESSAGE_USAGE

