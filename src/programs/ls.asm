        org 6000h

%include "constants.asm"

main:
        cld

        ;; Check for optional directory argument
        mov si, [EXEC_ARG]
        test si, si
        jz .list_root
        cmp byte [si], 0
        je .list_root

        ;; Argument given: find the directory entry
        mov ah, SYS_FS_FIND
        int 30h
        jc .not_found
        test byte [bx+DIR_OFF_FLAGS], FLAG_DIR
        jz .not_dir
        ;; Read the subdirectory's data sector
        mov al, [bx+DIR_OFF_SECTOR]
        mov ah, SYS_FS_READ
        int 30h
        jc .disk_err
        ;; List entries from the subdirectory sector
        mov bx, DISK_BUFFER
        mov cx, DIR_MAX_ENTRIES / DIR_SECTORS
        jmp .loop

.list_root:
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
        test byte [bx+DIR_OFF_FLAGS], FLAG_DIR
        jz .check_exec
        mov al, '/'
        mov ah, SYS_IO_PUTC
        int 30h
        jmp .no_suffix
        .check_exec:
        test byte [bx+DIR_OFF_FLAGS], FLAG_EXEC
        jz .no_suffix
        mov al, '*'
        mov ah, SYS_IO_PUTC
        int 30h
        .no_suffix:
        mov al, `\n`
        mov ah, SYS_IO_PUTC
        int 30h
        add bx, DIR_ENTRY_SIZE
        loop .loop

.try_next_sector:
        ;; Only iterate multiple sectors for root directory
        cmp byte [cur_sec], 0
        je .done                ; was listing a subdirectory (cur_sec=0)
        inc byte [cur_sec]
        mov al, [cur_sec]
        sub al, DIR_SECTOR
        cmp al, DIR_SECTORS
        jb .next_sector

.done:
        mov ah, SYS_EXIT
        int 30h

.not_found:
        mov si, MSG_NOT_FOUND
        jmp .error

.not_dir:
        mov si, MSG_NOT_DIR
        jmp .error

.disk_err:
        mov si, DISK_ERROR
        .error:
        mov ah, SYS_IO_PUTS
        int 30h
        mov ah, SYS_EXIT
        int 30h

cur_sec db 0

MSG_NOT_DIR   db `Not a directory\n\0`
MSG_NOT_FOUND db `Not found\n\0`

%include "str_disk_error.asm"
