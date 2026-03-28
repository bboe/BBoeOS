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

        inc cx                  ; Include null terminator
        mov dx, cx              ; Save length in DX

        ;; Check for "cat " prefix
        cmp dx, 5               ; Need at least "cat X"
        jl .dispatch
        mov si, BUFFER
        mov di, CAT_PREFIX
        mov cx, 4
        repe cmpsb
        jne .dispatch

        ;; SI = BUFFER + 4 = start of filename argument
        call cmd_cat
        jmp .output

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

cmd_cat:
        push bx
        push cx
        mov ah, SYS_FS_FIND
        int 30h
        jc .not_found

        mov cx, [bx+14]        ; File size
        test cx, cx
        jz .empty
        mov al, [bx+12]        ; Start sector
        mov ah, SYS_FS_READ
        int 30h
        jc .disk_err

        mov si, DISK_BUFFER
.print:
        lodsb
        cmp al, 0Ah             ; Convert \n to \r\n
        jne .putc
        push ax
        mov al, 0Dh
        mov ah, SYS_IO_PUTC
        int 30h
        pop ax
.putc:
        mov ah, SYS_IO_PUTC
        int 30h
        loop .print

.empty:
        mov si, NEWLINE
        jmp .done

.not_found:
        mov si, FILE_NOT_FOUND
        jmp .done

.disk_err:
        mov si, DISK_ERROR

.done:
        pop cx
        pop bx
        ret

cmd_cat_usage:
        mov si, CAT_USAGE
        ret

cmd_clear:
        mov ah, SYS_SCR_CLEAR
        jmp syscall_null

cmd_date:
        mov ah, SYS_RTC_DATETIME
        int 30h
        ;; CH=century, CL=year, DH=month, DL=day
        mov al, ch
        call print_bcd
        mov al, cl
        call print_bcd
        mov al, '-'
        mov ah, SYS_IO_PUTC
        int 30h
        mov al, dh
        call print_bcd
        mov al, '-'
        mov ah, SYS_IO_PUTC
        int 30h
        mov al, dl
        call print_bcd
        mov si, NEWLINE
        ret

cmd_graphics:
        mov ah, SYS_SCR_GRAPHICS
        jmp syscall_null

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

cmd_time:
        mov ah, SYS_RTC_DATETIME
        int 30h
        ;; BH=hours, BL=minutes, AL=seconds
        push ax                 ; Save seconds
        mov al, bh
        call print_bcd
        mov al, ':'
        mov ah, SYS_IO_PUTC
        int 30h
        mov al, bl
        call print_bcd
        mov al, ':'
        mov ah, SYS_IO_PUTC
        int 30h
        pop ax                  ; Restore seconds
        call print_bcd
        mov si, NEWLINE
        ret

;;; Utility functions

print_bcd:
        ;; Print AL as two BCD digits via io_putc
        push cx
        mov cl, al
        shr al, 4               ; High nibble
        add al, '0'
        mov ah, SYS_IO_PUTC
        int 30h
        mov al, cl
        and al, 0Fh             ; Low nibble
        add al, '0'
        mov ah, SYS_IO_PUTC
        int 30h
        pop cx
        ret

syscall_null:
        int 30h
        xor si, si
        ret

;;; Command table
cmd_table:
        dw .cat,      cmd_cat_usage
        dw .clear,    cmd_clear
        dw .date,     cmd_date
        dw .graphics, cmd_graphics
        dw .help,     cmd_help
        dw .ls,       cmd_ls
        dw .reboot,   cmd_reboot
        dw .shutdown, cmd_shutdown
        dw .time,     cmd_time
        dw 0
        .cat      db `cat\0`
        .clear    db `clear\0`
        .date     db `date\0`
        .graphics db `graphics\0`
        .help     db `help\0`
        .ls       db `ls\0`
        .reboot   db `reboot\0`
        .shutdown db `shutdown\0`
        .time     db `time\0`

;;; Strings
CAT_PREFIX    db `cat \0`
CAT_USAGE     db `Usage: cat <filename>\r\n\0`
DISK_ERROR    db `Disk read error\r\n\0`
FILE_NOT_FOUND db `File not found\r\n\0`
HELP_PREFIX   db `Commands: \0`
INVALID_CMD   db `unknown command\r\n\0`
NEWLINE       db `\r\n\0`
PROMPT        db `$ \0`
SHUTDOWN_FAIL db `APM shutdown failed\r\n\0`
