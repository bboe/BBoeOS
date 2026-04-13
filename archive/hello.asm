        org 0600h

%include "constants.asm"

main:
        mov si, _str_0
        mov cx, 13
        call write_stdout
        mov ah, SYS_EXIT
        int 30h

;; --- string literals ---
_str_0: db `Hello world!\n\0`
%include "write_stdout.asm"
