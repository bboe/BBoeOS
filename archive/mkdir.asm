        org 0600h

%include "constants.asm"

main:
        cld
        mov si, [EXEC_ARG]
        test si, si
        jz .usage

        mov ah, SYS_FS_MKDIR
        int 30h
        jnc .done

        cmp al, ERROR_EXISTS
        je .exists
        cmp al, ERROR_DIRECTORY_FULL
        je .dir_full
        mov si, MESSAGE_ERROR
        mov cx, MESSAGE_ERROR_LENGTH
        jmp .print
        .exists:
        mov si, MESSAGE_EXISTS
        mov cx, MESSAGE_EXISTS_LENGTH
        jmp .print
        .dir_full:
        mov si, MESSAGE_DIRECTORY_FULL
        mov cx, MESSAGE_DIRECTORY_FULL_LENGTH
        .print:
        call write_stdout

        .done:
        mov ah, SYS_EXIT
        int 30h

        .usage:
        mov si, MESSAGE_USAGE
        mov cx, MESSAGE_USAGE_LENGTH
        call write_stdout
        mov ah, SYS_EXIT
        int 30h

MESSAGE_DIRECTORY_FULL         db `Directory full\n`
MESSAGE_DIRECTORY_FULL_LENGTH  equ $ - MESSAGE_DIRECTORY_FULL
MESSAGE_ERROR         db `Error\n`
MESSAGE_ERROR_LENGTH  equ $ - MESSAGE_ERROR
MESSAGE_EXISTS        db `Already exists\n`
MESSAGE_EXISTS_LENGTH equ $ - MESSAGE_EXISTS
MESSAGE_USAGE         db `Usage: mkdir <name>\n`
MESSAGE_USAGE_LENGTH  equ $ - MESSAGE_USAGE

%include "write_stdout.asm"
