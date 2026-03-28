        org 6000h

%include "constants.asm"

main:
        cld
        mov si, PROMPT
        mov ah, SYS_IO_PUTS
        int 30h

        mov ah, SYS_IO_GETS
        int 30h
        test cx, cx
        jz main

        ;; Split command at first space
        mov si, BUFFER
        mov word [EXEC_ARG], 0
.find_space:
        lodsb
        cmp al, ' '
        je .found_space
        test al, al
        jnz .find_space
        jmp .split_done
.found_space:
        mov byte [si-1], 0     ; Null-terminate command name
        mov [EXEC_ARG], si     ; Point to argument
.split_done:
        ;; DX = command name length including null terminator
        mov dx, si
        sub dx, BUFFER

.dispatch:
        mov bx, cmd_table
.loop:
        mov di, [bx]
        test di, di
        jz .not_found

        mov cx, dx
        mov si, BUFFER
        repe cmpsb
        jne .next

        call word [bx+2]
        jmp .output

.next:
        add bx, 4
        jmp .loop

.not_found:
        ;; Try to execute as external program
        mov si, BUFFER
        mov ah, SYS_EXEC
        int 30h                 ; Does not return on success
        mov si, INVALID_CMD

.output:
        test si, si
        jz main
        mov ah, SYS_IO_PUTS
        int 30h
        jmp main

;;; Command handlers
;;; Return: SI = string to print, or SI = 0 for no output

cmd_help:
        push bx
        mov si, HELP_PREFIX
        mov ah, SYS_IO_PUTS
        int 30h
        mov bx, cmd_table
.help_loop:
        mov si, [bx]
        test si, si
        jz .help_end
        mov ah, SYS_IO_PUTS
        int 30h
        mov al, ' '
        mov ah, SYS_IO_PUTC
        int 30h
        add bx, 4
        jmp .help_loop
.help_end:
        pop bx
        mov si, NEWLINE
        ret

cmd_ls:
        push bx
        mov al, DIR_SECTOR
        mov ah, SYS_FS_READ
        int 30h
        jc .ls_err
        mov bx, DISK_BUFFER
.ls_loop:
        cmp byte [bx], 0
        je .ls_done
        mov si, bx
        mov ah, SYS_IO_PUTS
        int 30h
        mov si, NEWLINE
        mov ah, SYS_IO_PUTS
        int 30h
        add bx, DIR_ENTRY_SIZE
        jmp .ls_loop
.ls_done:
        pop bx
        xor si, si
        ret
.ls_err:
        pop bx
        mov si, DISK_ERROR
        ret

cmd_reboot:
        mov ah, SYS_REBOOT
        jmp syscall_null

cmd_shutdown:
        mov ah, SYS_SHUTDOWN
        int 30h
        mov si, SHUTDOWN_FAIL
        ret

;;; Utility functions

syscall_null:
        int 30h
        xor si, si
        ret

;;; Command table
cmd_table:
        dw .help,     cmd_help
        dw .ls,       cmd_ls
        dw .reboot,   cmd_reboot
        dw .shutdown, cmd_shutdown
        dw 0
        .help     db `help\0`
        .ls       db `ls\0`
        .reboot   db `reboot\0`
        .shutdown db `shutdown\0`

;;; Strings
HELP_PREFIX   db `Commands: \0`
INVALID_CMD   db `unknown command\r\n\0`
PROMPT        db `$ \0`
SHUTDOWN_FAIL db `APM shutdown failed\r\n\0`

%include "str_disk_error.asm"
%include "str_newline.asm"
