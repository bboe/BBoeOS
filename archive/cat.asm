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

        ;; Write AX bytes from DISK_BUFFER
        push bx                 ; Save fd
        mov cx, ax
        mov si, DISK_BUFFER
        call write_stdout
        pop bx                  ; Restore fd
        jmp .read_loop

.done:
        mov ah, SYS_IO_CLOSE
        int 30h
        mov ah, SYS_EXIT
        int 30h

.not_found:
        mov si, FILE_NOT_FOUND
        mov cx, FILE_NOT_FOUND_LENGTH
        jmp .output

.disk_err:
        mov ah, SYS_IO_CLOSE
        int 30h
        mov si, DISK_ERROR
        mov cx, DISK_ERROR_LENGTH
        jmp .output

.usage:
        mov si, USAGE
        mov cx, USAGE_LENGTH

.output:
        call write_stdout
        mov ah, SYS_EXIT
        int 30h

;; Strings
USAGE        db `Usage: cat <filename>\n`
USAGE_LENGTH equ $ - USAGE

%include "str_disk_error.asm"
%include "str_file_not_found.asm"
%include "write_stdout.asm"
