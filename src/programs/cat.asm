        org 6000h

%include "constants.asm"

main:
        mov si, [EXEC_ARG]
        test si, si
        jz .usage

        mov ah, SYS_FS_FIND
        int 30h
        jc .not_found

        mov cx, [bx+14]        ; File size
        test cx, cx
        jz .empty
        mov bl, [bx+12]        ; Start sector

.read_sector:
        mov al, bl
        mov ah, SYS_FS_READ
        int 30h
        jc .disk_err

        ;; Print up to 512 bytes or remaining bytes
        mov dx, cx              ; Save remaining bytes
        cmp cx, 512
        jle .print_sector
        mov cx, 512
.print_sector:
        mov si, DISK_BUFFER
.print:
        lodsb
        cmp al, 0Ah             ; Convert \n to \r\n
        jne .putc
        push ax
        mov al, 0Dh
        mov ah, SYS_IO_PUTC
        int 30h
        pop ax
.putc:
        mov ah, SYS_IO_PUTC
        int 30h
        loop .print

        sub dx, 512
        jle .empty
        mov cx, dx              ; Remaining bytes
        inc bl                  ; Next sector
        jmp .read_sector

.empty:
        mov si, NEWLINE
        jmp .output

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

;;; Strings
USAGE db `Usage: cat <filename>\r\n\0`

%include "str_disk_error.asm"
%include "str_file_not_found.asm"
%include "str_newline.asm"
