        org 0600h

%include "constants.asm"

main:
        cld
        mov si, [EXEC_ARG]
        test si, si
        jz .usage

        ;; Open file for reading
        mov al, O_RDONLY
        mov ah, SYS_IO_OPEN
        int 30h
        jc .not_found

        mov bx, ax             ; BX = fd

.read_loop:
        mov di, DISK_BUFFER
        mov cx, 512
        mov ah, SYS_IO_READ
        int 30h
        jc .disk_err
        test ax, ax
        jz .done                ; EOF

        ;; Print AX bytes from DISK_BUFFER
        push bx                 ; Save fd
        mov cx, ax
        mov si, DISK_BUFFER
.print:
        lodsb
        mov ah, SYS_IO_PUTC
        int 30h
        loop .print
        pop bx                  ; Restore fd
        jmp .read_loop

.done:
        mov ah, SYS_IO_CLOSE
        int 30h
        mov ah, SYS_EXIT
        int 30h

.not_found:
        mov si, FILE_NOT_FOUND
        jmp .output

.disk_err:
        mov ah, SYS_IO_CLOSE
        int 30h
        mov si, DISK_ERROR
        jmp .output

.usage:
        mov si, USAGE

.output:
        mov ah, SYS_IO_PUTS
        int 30h
        mov ah, SYS_EXIT
        int 30h

;; Strings
USAGE      db `Usage: cat <filename>\n\0`

%include "str_disk_error.asm"
%include "str_file_not_found.asm"
