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
        jmp .print
        .exists:
        mov si, MESSAGE_EXISTS
        jmp .print
        .dir_full:
        mov si, MESSAGE_DIRECTORY_FULL
        .print:
        mov ah, SYS_IO_PUT_STRING
        int 30h

        .done:
        mov ah, SYS_EXIT
        int 30h

        .usage:
        mov si, MESSAGE_USAGE
        mov ah, SYS_IO_PUT_STRING
        int 30h
        mov ah, SYS_EXIT
        int 30h

MESSAGE_DIRECTORY_FULL  db `Directory full\n\0`
MESSAGE_ERROR     db `Error\n\0`
MESSAGE_EXISTS    db `Already exists\n\0`
MESSAGE_USAGE     db `Usage: mkdir <name>\n\0`
