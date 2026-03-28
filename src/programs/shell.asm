        org 6000h

%include "constants.asm"

main:
        cld
        mov si, s_prompt
        mov ah, 13h             ; io_puts
        int 30h

        mov ah, 11h             ; io_gets
        int 30h
        test cx, cx
        jz main

        inc cx                  ; Include null terminator
        mov dx, cx              ; Save length in DX

        ;; Check for "cat " prefix
        cmp dx, 5               ; Need at least "cat X"
        jl .dispatch
        mov si, buffer
        mov di, s_cat_pfx
        mov cx, 4
        repe cmpsb
        jne .dispatch

        ;; SI = buffer + 4 = start of filename argument
        call cmd_cat
        jmp .output

.dispatch:
        mov bx, cmd_table
.loop:
        mov di, [bx]
        test di, di
        jz .invalid

        mov cx, dx
        mov si, buffer
        repe cmpsb
        jne .next

        call word [bx+2]
        jmp .output

.next:
        add bx, 4
        jmp .loop

.invalid:
        mov si, s_invalid

.output:
        test si, si
        jz main
        mov ah, 13h             ; io_puts
        int 30h
        jmp main

;;; Command handlers
;;; Return: SI = string to print, or SI = 0 for no output

cmd_cat:
        push bx
        push cx
        mov ah, 00h             ; fs_find
        int 30h
        jc .not_found

        mov cx, [bx+14]        ; File size
        test cx, cx
        jz .empty
        mov al, [bx+12]        ; Start sector
        mov ah, 01h             ; fs_read
        int 30h
        jc .disk_err

        mov si, disk_buffer
.print:
        lodsb
        cmp al, 0Ah             ; Convert \n to \r\n
        jne .putc
        push ax
        mov al, 0Dh
        mov ah, 12h             ; io_putc
        int 30h
        pop ax
.putc:
        mov ah, 12h             ; io_putc
        int 30h
        loop .print

.empty:
        mov si, s_nl
        jmp .done

.not_found:
        mov si, s_no_file
        jmp .done

.disk_err:
        mov si, s_disk_err

.done:
        pop cx
        pop bx
        ret

cmd_cat_usage:
        mov si, s_cat_usage
        ret

cmd_clear:
        mov ah, 30h             ; scr_clear
        jmp syscall_null

cmd_graphics:
        mov ah, 31h             ; scr_graphics
        jmp syscall_null

cmd_reboot:
        mov ah, 0F1h            ; sys_reboot

syscall_null:
        int 30h
        xor si, si
        ret

cmd_date:
        mov ah, 20h             ; rtc_datetime
        int 30h
        ;; CH=century, CL=year, DH=month, DL=day
        mov al, ch
        call print_bcd
        mov al, cl
        call print_bcd
        mov al, '-'
        mov ah, 12h
        int 30h
        mov al, dh
        call print_bcd
        mov al, '-'
        mov ah, 12h
        int 30h
        mov al, dl
        call print_bcd
        mov si, s_nl
        ret

cmd_time:
        mov ah, 20h             ; rtc_datetime
        int 30h
        ;; BH=hours, BL=minutes, AL=seconds
        push ax                 ; Save seconds
        mov al, bh
        call print_bcd
        mov al, ':'
        mov ah, 12h
        int 30h
        mov al, bl
        call print_bcd
        mov al, ':'
        mov ah, 12h
        int 30h
        pop ax                  ; Restore seconds
        call print_bcd
        mov si, s_nl
        ret

cmd_help:
        push bx
        mov si, s_help_pfx
        mov ah, 13h             ; io_puts
        int 30h
        mov bx, cmd_table
.help_loop:
        mov si, [bx]
        test si, si
        jz .help_end
        mov ah, 13h             ; io_puts
        int 30h
        mov al, ' '
        mov ah, 12h             ; io_putc
        int 30h
        add bx, 4
        jmp .help_loop
.help_end:
        pop bx
        mov si, s_nl
        ret

cmd_ls:
        push bx
        mov al, dir_sector
        mov ah, 01h             ; fs_read
        int 30h
        jc .ls_err
        mov bx, disk_buffer
.ls_loop:
        cmp byte [bx], 0
        je .ls_done
        mov si, bx
        mov ah, 13h             ; io_puts
        int 30h
        mov si, s_nl
        mov ah, 13h             ; io_puts
        int 30h
        add bx, dir_entry_size
        jmp .ls_loop
.ls_done:
        pop bx
        xor si, si
        ret
.ls_err:
        pop bx
        mov si, s_disk_err
        ret

cmd_shutdown:
        mov ah, 0F2h            ; sys_shutdown
        int 30h
        mov si, s_sd_fail
        ret

cmd_uptime:
        mov ah, 22h             ; rtc_uptime
        int 30h                 ; AX = elapsed seconds

        xor dx, dx
        mov cx, 3600
        div cx                  ; AX = hours, DX = remaining seconds
        push dx
        call print_dec2
        mov al, ':'
        mov ah, 12h             ; io_putc
        int 30h

        pop ax                  ; Remaining seconds
        xor ah, ah
        mov cl, 60
        div cl                  ; AL = minutes, AH = seconds
        push ax
        call print_dec2
        mov al, ':'
        mov ah, 12h             ; io_putc
        int 30h

        pop ax
        mov al, ah              ; Seconds
        call print_dec2
        mov si, s_nl
        ret

print_bcd:
        ;; Print AL as two BCD digits via io_putc
        push cx
        mov cl, al
        shr al, 4               ; High nibble
        add al, '0'
        mov ah, 12h             ; io_putc
        int 30h
        mov al, cl
        and al, 0Fh             ; Low nibble
        add al, '0'
        mov ah, 12h             ; io_putc
        int 30h
        pop cx
        ret

print_dec2:
        ;; Print AL as 2 decimal digits via io_putc
        aam                     ; AH = AL/10, AL = AL%10
        xchg al, ah             ; AL = tens, AH = ones
        add al, '0'
        push ax
        mov ah, 12h             ; io_putc
        int 30h
        pop ax
        mov al, ah
        add al, '0'
        mov ah, 12h             ; io_putc
        int 30h
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
        dw .uptime,   cmd_uptime
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
        .uptime   db `uptime\0`

;;; Strings
s_cat_pfx   db `cat \0`
s_cat_usage db `Usage: cat <filename>\r\n\0`
s_disk_err  db `Disk read error\r\n\0`
s_help_pfx  db `Commands: \0`
s_invalid   db `unknown command\r\n\0`
s_nl        db `\r\n\0`
s_no_file   db `File not found\r\n\0`
s_prompt    db `$ \0`
s_sd_fail   db `APM shutdown failed\r\n\0`
