        org 0600h

%include "constants.asm"

main:
        mov ah, SYS_RTC_UPTIME
        int 30h                 ; AX = elapsed seconds

        xor dx, dx
        mov cx, 3600
        div cx                  ; AX = hours, DX = remaining seconds
        push dx
        call FUNCTION_PRINT_DECIMAL
        mov al, ':'
        call FUNCTION_PRINT_CHARACTER

        pop ax                  ; Remaining seconds
        xor ah, ah
        mov cl, 60
        div cl                  ; AL = minutes, AH = seconds
        push ax
        call FUNCTION_PRINT_DECIMAL
        mov al, ':'
        call FUNCTION_PRINT_CHARACTER

        pop ax
        mov al, ah              ; Seconds
        call FUNCTION_PRINT_DECIMAL
        mov al, `\n`
        call FUNCTION_PRINT_CHARACTER

        jmp FUNCTION_EXIT
