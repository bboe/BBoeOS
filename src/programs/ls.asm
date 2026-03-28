        org 6000h

%include "constants.asm"

main:
        mov al, DIR_SECTOR
        mov ah, SYS_FS_READ
        int 30h
        jc .disk_err

        mov bx, DISK_BUFFER
.loop:
        cmp byte [bx], 0
        je .done
        mov si, bx
        mov ah, SYS_IO_PUTS
        int 30h
        mov al, `\n`
        mov ah, SYS_IO_PUTC
        int 30h
        add bx, DIR_ENTRY_SIZE
        jmp .loop

.done:
        mov ah, SYS_EXIT
        int 30h

.disk_err:
        mov si, DISK_ERROR
        jmp .output

.output:
        mov ah, SYS_IO_PUTS
        int 30h
        mov ah, SYS_EXIT
        int 30h

%include "str_disk_error.asm"
