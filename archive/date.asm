        org 0600h

%include "constants.asm"

main:
        mov ah, SYS_RTC_DATETIME
        int 30h
        ;; CH=century, CL=year, DH=month, DL=day
        ;; BH=hours, BL=minutes, AL=seconds
        push ax                 ; Save seconds
        push bx                 ; Save hours/minutes

        mov al, ch
        call FUNCTION_PRINT_BCD
        mov al, cl
        call FUNCTION_PRINT_BCD
        mov al, '-'
        call FUNCTION_PRINT_CHARACTER
        mov al, dh
        call FUNCTION_PRINT_BCD
        mov al, '-'
        call FUNCTION_PRINT_CHARACTER
        mov al, dl
        call FUNCTION_PRINT_BCD
        mov al, ' '
        call FUNCTION_PRINT_CHARACTER

        pop bx                  ; Restore hours/minutes
        mov al, bh
        call FUNCTION_PRINT_BCD
        mov al, ':'
        call FUNCTION_PRINT_CHARACTER
        mov al, bl
        call FUNCTION_PRINT_BCD
        mov al, ':'
        call FUNCTION_PRINT_CHARACTER
        pop ax                  ; Restore seconds
        call FUNCTION_PRINT_BCD

        mov al, `\n`
        call FUNCTION_PRINT_CHARACTER

        jmp FUNCTION_EXIT
