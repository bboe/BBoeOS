        org 0600h

%include "constants.asm"

main:
        mov si, GREETING
        mov ah, SYS_IO_PUTS
        int 30h
        mov ah, SYS_EXIT
        int 30h

;; Strings
GREETING db `Hello, world!\n\0`
