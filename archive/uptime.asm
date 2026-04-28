        [bits 32]
        org 0600h

%include "constants.asm"

main:
        mov ah, SYS_RTC_UPTIME
        int 30h                 ; EAX = elapsed seconds

        xor edx, edx
        mov ecx, 3600
        div ecx                 ; EAX = hours, EDX = remaining seconds
        push edx
        call FUNCTION_PRINT_DECIMAL
        mov al, ':'
        call FUNCTION_PRINT_CHARACTER

        pop eax                 ; Remaining seconds (0-3599)
        xor edx, edx
        mov ecx, 60
        div ecx                 ; EAX = minutes, EDX = seconds
        push edx
        call FUNCTION_PRINT_DECIMAL
        mov al, ':'
        call FUNCTION_PRINT_CHARACTER

        pop eax                 ; Seconds within minute
        call FUNCTION_PRINT_DECIMAL
        mov al, `\n`
        call FUNCTION_PRINT_CHARACTER

        jmp FUNCTION_EXIT
