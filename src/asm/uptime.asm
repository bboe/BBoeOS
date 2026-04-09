        org 0600h

%include "constants.asm"

main:
        mov ah, SYS_RTC_UPTIME
        int 30h                 ; AX = elapsed seconds

        xor dx, dx
        mov cx, 3600
        div cx                  ; AX = hours, DX = remaining seconds
        push dx
        call print_dec
        mov al, ':'
        mov ah, SYS_IO_PUTC
        int 30h

        pop ax                  ; Remaining seconds
        xor ah, ah
        mov cl, 60
        div cl                  ; AL = minutes, AH = seconds
        push ax
        call print_dec
        mov al, ':'
        mov ah, SYS_IO_PUTC
        int 30h

        pop ax
        mov al, ah              ; Seconds
        call print_dec
        mov al, `\n`
        mov ah, SYS_IO_PUTC
        int 30h

        mov ah, SYS_EXIT
        int 30h

%include "print_dec.asm"
