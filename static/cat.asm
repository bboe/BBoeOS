        org 6000h

%include "constants.asm"

main:
        cld
        mov si, [EXEC_ARG]
        test si, si
        jz .usage

        mov ah, SYS_FS_FIND
        int 30h
        jc .not_found

        mov cx, [bx+30]
        test cx, cx
        jz .empty
        mov bl, [bx+28]

.read_sector:
        mov al, bl
        mov ah, SYS_FS_READ
        int 30h
        jc .disk_err

        mov dx, cx
        cmp cx, 512
        jbe .print_sector
        mov cx, 512
.print_sector:
        mov si, DISK_BUFFER
.print:
        lodsb
        mov ah, SYS_IO_PUTC
        int 30h
        loop .print

        sub dx, 512
        jbe .empty
        mov cx, dx
        inc bl
        jmp .read_sector

.empty:
        mov ah, SYS_EXIT
        int 30h

.not_found:
        mov si, FILE_NOT_FOUND
        jmp .output

.disk_err:
        mov si, DISK_ERROR
        jmp .output

.usage:
        mov si, USAGE

.output:
        mov ah, SYS_IO_PUTS
        int 30h
        mov ah, SYS_EXIT
        int 30h

USAGE db `Usage: cat <filename>\n\0`

%include "str_disk_error.asm"
%include "str_file_not_found.asm"
