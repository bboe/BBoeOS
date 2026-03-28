        org 6000h

%include "constants.asm"

main:
        mov ah, SYS_RTC_UPTIME
        int 30h                 ; AX = elapsed seconds

        xor dx, dx
        mov cx, 3600
        div cx                  ; AX = hours, DX = remaining seconds
        push dx
        call print_dec2
        mov al, ':'
        mov ah, SYS_IO_PUTC
        int 30h

        pop ax                  ; Remaining seconds
        xor ah, ah
        mov cl, 60
        div cl                  ; AL = minutes, AH = seconds
        push ax
        call print_dec2
        mov al, ':'
        mov ah, SYS_IO_PUTC
        int 30h

        pop ax
        mov al, ah              ; Seconds
        call print_dec2
        mov si, NEWLINE
        mov ah, SYS_IO_PUTS
        int 30h

        mov ah, SYS_EXIT
        int 30h

print_dec2:
        ;; Print AL as 2 decimal digits via io_putc
        aam                     ; AH = AL/10, AL = AL%10
        xchg al, ah             ; AL = tens, AH = ones
        add al, '0'
        push ax
        mov ah, SYS_IO_PUTC
        int 30h
        pop ax
        mov al, ah
        add al, '0'
        mov ah, SYS_IO_PUTC
        int 30h
        ret

;;; Strings
NEWLINE db `\r\n\0`
