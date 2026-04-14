write_stdout:
        ;; Write CX bytes from SI to stdout (fd 1)
        mov bx, STDOUT
        mov ah, SYS_IO_WRITE
        int 30h
        ret
