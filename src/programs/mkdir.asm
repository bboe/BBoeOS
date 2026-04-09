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

        cmp al, ERR_EXISTS
        je .exists
        cmp al, ERR_DIR_FULL
        je .dir_full
        mov si, MSG_ERROR
        jmp .print
        .exists:
        mov si, MSG_EXISTS
        jmp .print
        .dir_full:
        mov si, MSG_DIR_FULL
        .print:
        mov ah, SYS_IO_PUTS
        int 30h

        .done:
        mov ah, SYS_EXIT
        int 30h

        .usage:
        mov si, MSG_USAGE
        mov ah, SYS_IO_PUTS
        int 30h
        mov ah, SYS_EXIT
        int 30h

MSG_DIR_FULL  db `Directory full\n\0`
MSG_ERROR     db `Error\n\0`
MSG_EXISTS    db `Already exists\n\0`
MSG_USAGE     db `Usage: mkdir <name>\n\0`
