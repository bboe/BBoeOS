        [bits 32]
        org 0600h

%include "constants.asm"

main:
        cld

        ;; Linux-style argv: reserve stack slots; require argc == 2.
        ;; Linux SysV i386 startup: argc at [esp], argv ptrs at [esp+4..].
        ;; Pop argc into ECX and leave argv[0] at [esp+0] to match the
        ;; legacy parse_argv layout this program is written against.
        pop ecx                                 ; ecx = argc
        mov edi, esp                            ; edi = argv base
        cmp ecx, 2
        jne .usage

        mov esi, [esp+4]
        mov ah, SYS_FS_MKDIR
        int 30h
        jnc .done

        cmp al, ERROR_EXISTS
        je .exists
        cmp al, ERROR_DIRECTORY_FULL
        je .dir_full
        mov esi, MESSAGE_ERROR
        mov ecx, MESSAGE_ERROR_LENGTH
        jmp .print
        .exists:
        mov esi, MESSAGE_EXISTS
        mov ecx, MESSAGE_EXISTS_LENGTH
        jmp .print
        .dir_full:
        mov esi, MESSAGE_DIRECTORY_FULL
        mov ecx, MESSAGE_DIRECTORY_FULL_LENGTH
        .print:
        jmp FUNCTION_DIE

        .done:
        jmp FUNCTION_EXIT

        .usage:
        mov esi, MESSAGE_USAGE
        mov ecx, MESSAGE_USAGE_LENGTH
        jmp FUNCTION_DIE

MESSAGE_DIRECTORY_FULL         db `Directory full\n`
MESSAGE_DIRECTORY_FULL_LENGTH  equ $ - MESSAGE_DIRECTORY_FULL
MESSAGE_ERROR         db `Error\n`
MESSAGE_ERROR_LENGTH  equ $ - MESSAGE_ERROR
MESSAGE_EXISTS        db `Already exists\n`
MESSAGE_EXISTS_LENGTH equ $ - MESSAGE_EXISTS
MESSAGE_USAGE         db `Usage: mkdir <name>\n`
MESSAGE_USAGE_LENGTH  equ $ - MESSAGE_USAGE

