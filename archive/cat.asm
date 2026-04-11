        org 0600h

%include "constants.asm"

main:
        cld
        mov si, [EXEC_ARG]
        test si, si
        jz .usage

        mov ah, SYS_FS_FIND
        int 30h
        jc .not_found

        test byte [bx+DIR_OFF_FLAGS], FLAG_DIR
        jnz .is_dir

        mov dx, [bx+DIR_OFF_SIZE]        ; File size in DX
        test dx, dx
        jz .empty
        mov cx, [bx+DIR_OFF_SECTOR]      ; Start sector in CX

.read_sector:
        mov ah, SYS_FS_READ              ; reads sector CX into DISK_BUFFER
        int 30h
        jc .disk_err

        ;; Print up to 512 bytes or remaining bytes
        push cx                 ; Save sector across print loop
        mov cx, 512
        cmp dx, 512
        jae .print_sector
        mov cx, dx
.print_sector:
        mov si, DISK_BUFFER
.print:
        lodsb
        mov ah, SYS_IO_PUTC
        int 30h
        loop .print
        pop cx                  ; Restore sector

        sub dx, 512
        jbe .empty
        inc cx                  ; Next sector
        jmp .read_sector

.empty:
        mov ah, SYS_EXIT
        int 30h

.is_dir:
        mov si, MSG_IS_DIR
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

;; Strings
MSG_IS_DIR db `Is a directory\n\0`
USAGE      db `Usage: cat <filename>\n\0`

%include "str_disk_error.asm"
%include "str_file_not_found.asm"
