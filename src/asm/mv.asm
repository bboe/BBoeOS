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
        call write_stdout
        mov ah, SYS_EXIT
        int 30h

        .done:
        mov ah, SYS_EXIT
        int 30h

        .toolong:
        mov si, MESSAGE_TOO_LONG
        mov cx, MESSAGE_TOO_LONG_LENGTH
        call write_stdout
        mov ah, SYS_EXIT
        int 30h

        .usage:
        mov si, MESSAGE_USAGE
        mov cx, MESSAGE_USAGE_LENGTH
        call write_stdout
        mov ah, SYS_EXIT
        int 30h

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

%include "write_stdout.asm"
