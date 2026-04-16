        org 0600h

%include "constants.asm"

main:
        cld

        ;; Require exactly one argument
        mov di, ARGV
        call FUNCTION_PARSE_ARGV
        cmp cx, 1
        jne .usage

        ;; Open file for reading
        mov si, [ARGV]
        mov al, O_RDONLY
        mov ah, SYS_IO_OPEN
        int 30h
        jc .not_found

        mov bx, ax             ; BX = fd

.read_loop:
        mov di, SECTOR_BUFFER
        mov cx, 512
        mov ah, SYS_IO_READ
        int 30h
        jc .disk_err
        test ax, ax
        jz .done                ; EOF

        ;; Write AX bytes from SECTOR_BUFFER
        push bx                 ; Save fd
        mov cx, ax
        mov si, SECTOR_BUFFER
        call FUNCTION_WRITE_STDOUT
        pop bx                  ; Restore fd
        jmp .read_loop

.done:
        mov ah, SYS_IO_CLOSE
        int 30h
        jmp FUNCTION_EXIT

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
        jmp FUNCTION_DIE

;; Strings
USAGE        db `Usage: cat <filename>\n`
USAGE_LENGTH equ $ - USAGE

DISK_ERROR db `Disk read error\n\0`
DISK_ERROR_LENGTH equ $ - DISK_ERROR - 1
FILE_NOT_FOUND db `File not found\n\0`
FILE_NOT_FOUND_LENGTH equ $ - FILE_NOT_FOUND - 1
