        org 0600h

%include "constants.asm"

main:
        mov si, _str_0
        mov ah, SYS_IO_PUT_STRING
        int 30h
        mov ah, SYS_EXIT
        int 30h

;; --- string literals ---
_str_0: db `Hello world!\n\0`
