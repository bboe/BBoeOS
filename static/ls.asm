        org 6000h

%include "constants.asm"

main:
        mov byte [cur_sec], DIR_SECTOR

.next_sector:
        mov al, [cur_sec]
        mov ah, SYS_FS_READ
        int 30h
        jc .disk_err

        mov bx, DISK_BUFFER
        mov cx, DIR_MAX_ENTRIES / DIR_SECTORS
.loop:
        cmp byte [bx], 0
        je .try_next_sector
        mov si, bx
        mov ah, SYS_IO_PUTS
        int 30h
        test byte [bx+DIR_OFF_FLAGS], FLAG_EXEC
        jz .no_star
        mov al, '*'
        mov ah, SYS_IO_PUTC
        int 30h
        .no_star:
        mov al, `\n`
        mov ah, SYS_IO_PUTC
        int 30h
        add bx, DIR_ENTRY_SIZE
        loop .loop

.try_next_sector:
        inc byte [cur_sec]
        mov al, [cur_sec]
        sub al, DIR_SECTOR
        cmp al, DIR_SECTORS
        jb .next_sector

.done:
        mov ah, SYS_EXIT
        int 30h

.disk_err:
        mov si, DISK_ERROR
        mov ah, SYS_IO_PUTS
        int 30h
        mov ah, SYS_EXIT
        int 30h

cur_sec db 0

%include "str_disk_error.asm"
