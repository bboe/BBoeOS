        org 6000h

%include "constants.asm"

main:
        mov ah, SYS_RTC_DATETIME
        int 30h
        ;; CH=century, CL=year, DH=month, DL=day
        ;; BH=hours, BL=minutes, AL=seconds
        push ax                 ; Save seconds
        push bx                 ; Save hours/minutes

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
        mov al, ' '
        mov ah, SYS_IO_PUTC
        int 30h

        pop bx                  ; Restore hours/minutes
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
        mov ah, SYS_IO_PUTS
        int 30h

        mov ah, SYS_EXIT
        int 30h

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

;;; Strings
NEWLINE db `\r\n\0`
