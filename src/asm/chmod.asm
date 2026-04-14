        org 0600h

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
        mov dl, FLAG_EXECUTE
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
        jnc .done

        cmp al, ERROR_PROTECTED
        je .protected
        ;; ERROR_NOT_FOUND (or unknown)
        mov si, MESSAGE_NOT_FOUND
        jmp .error
        .protected:
        mov si, MESSAGE_PROTECTED
        .error:
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

        MESSAGE_NOT_FOUND db `File not found\n\0`
        MESSAGE_PROTECTED db `File is protected\n\0`
        MESSAGE_USAGE     db `Usage: chmod [+x|-x] <file>\n\0`
