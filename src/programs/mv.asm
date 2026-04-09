        org 0600h

%include "constants.asm"

main:
        cld

        ;; Require argument of the form "<oldname> <newname>"
        mov bx, [EXEC_ARG]
        test bx, bx
        jz .usage

        mov si, bx             ; SI = oldname (will be SI for syscall)

        ;; Find the space separating oldname and newname
        mov di, bx
        .find_space:
        mov al, [di]
        test al, al
        jz .usage
        cmp al, ' '
        je .found_space
        inc di
        jmp .find_space

        .found_space:
        mov byte [di], 0       ; Null-terminate oldname in EXEC_ARG buffer
        inc di                 ; DI = newname

        ;; Validate newname is non-empty and <= 10 chars
        test byte [di], 0FFh
        jz .usage
        push si
        push di
        mov si, di
        xor cx, cx
        .count_new:
        lodsb
        test al, al
        jz .count_done
        inc cx
        cmp cx, DIR_NAME_LEN - 1
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

        cmp al, ERR_EXISTS
        je .exists
        cmp al, ERR_PROTECTED
        je .protected
        ;; ERR_NOT_FOUND (or unknown)
        mov si, MSG_NOT_FOUND
        jmp .error
        .exists:
        mov si, MSG_EXISTS
        jmp .error
        .protected:
        mov si, MSG_PROTECTED
        .error:
        mov ah, SYS_IO_PUTS
        int 30h

        .done:
        mov ah, SYS_EXIT
        int 30h

        .toolong:
        mov si, MSG_TOO_LONG
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

        MSG_EXISTS    db `File already exists\n\0`
        MSG_NOT_FOUND db `File not found\n\0`
        MSG_PROTECTED db `File is protected\n\0`
        MSG_TOO_LONG  db `Name too long (max 26 chars)\n\0`
        MSG_USAGE     db `Usage: mv <oldname> <newname>\n\0`
