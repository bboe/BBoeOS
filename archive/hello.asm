        [bits 32]
        org 0600h

%include "constants.asm"

main:
        mov esi, MESSAGE
        mov ecx, MESSAGE_LENGTH
        jmp FUNCTION_DIE

;; --- string literals ---
MESSAGE        db `Hello world!\n`
MESSAGE_LENGTH equ $ - MESSAGE

