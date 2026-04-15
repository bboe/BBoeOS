        org 0600h

%include "constants.asm"

main:
        mov ah, SYS_RTC_DATETIME
        int 30h                 ; DX:AX = unsigned epoch seconds
        call FUNCTION_PRINT_DATETIME
        mov al, `\n`
        call FUNCTION_PRINT_CHARACTER

        jmp FUNCTION_EXIT
