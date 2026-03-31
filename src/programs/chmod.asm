        org 6000h

%include "constants.asm"

main:
        cld

        ;; Require argument of the form "+x <filename>" or "-x <filename>"
        mov bx, [EXEC_ARG]
        test bx, bx
        jz .usage

        mov si, bx
        lodsb
        cmp al, '+'
        je .set_exec
        cmp al, '-'
        je .clear_exec
        jmp .usage

        .set_exec:
        mov dl, FLAG_EXEC
        jmp .check_x
        .clear_exec:
        xor dl, dl

        .check_x:
        lodsb
        cmp al, 'x'
        jne .usage
        lodsb
        cmp al, ' '
        jne .usage
        ;; SI now points to filename

        mov al, dl             ; AL = new flags value
        mov ah, SYS_FS_CHMOD
        int 30h
        jc .not_found

        mov ah, SYS_EXIT
        int 30h

        .not_found:
        mov si, MSG_NOT_FOUND
        mov ah, SYS_IO_PUTS
        int 30h
        mov ah, SYS_EXIT
        int 30h

        .usage:
        mov si, MSG_USAGE
        mov ah, SYS_IO_PUTS
        int 30h
        mov ah, SYS_EXIT
        int 30h

        MSG_NOT_FOUND db `File not found\n\0`
        MSG_USAGE db `Usage: chmod [+x|-x] <file>\n\0`
