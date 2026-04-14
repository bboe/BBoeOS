        org 0600h

%include "constants.asm"

main:
        mov si, MESSAGE
        mov cx, MESSAGE_LENGTH
        jmp FUNCTION_DIE

;; --- string literals ---
MESSAGE        db `Hello world!\n`
MESSAGE_LENGTH equ $ - MESSAGE

