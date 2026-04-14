        org 0600h

%include "constants.asm"

main:
        mov si, MESSAGE
        mov cx, MESSAGE_LENGTH
        call write_stdout
        mov ah, SYS_EXIT
        int 30h

;; --- string literals ---
MESSAGE        db `Hello world!\n`
MESSAGE_LENGTH equ $ - MESSAGE

%include "write_stdout.asm"
