        org 0600h

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
        ;; Set up to iterate the subdirectory's sectors
        mov ax, [bx+DIR_OFF_SECTOR]
        mov [cur_sec], ax
        add ax, DIR_SECTORS
        mov [end_sec], ax
        jmp .next_sector

.list_root:
        mov word [cur_sec], DIR_SECTOR
        mov word [end_sec], DIR_SECTOR + DIR_SECTORS

.next_sector:
        mov cx, [cur_sec]
        mov ah, SYS_FS_READ
        int 30h
        jc .disk_err

        mov bx, DISK_BUFFER
        mov cx, DIR_MAX_ENTRIES / DIR_SECTORS
.loop:
        cmp byte [bx], 0
        je .skip_entry         ; empty slot (hole) — skip
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
        .skip_entry:
        add bx, DIR_ENTRY_SIZE
        loop .loop

.try_next_sector:
        inc word [cur_sec]
        mov ax, [cur_sec]
        cmp ax, [end_sec]
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

cur_sec dw 0
end_sec dw 0

MSG_NOT_DIR   db `Not a directory\n\0`
MSG_NOT_FOUND db `Not found\n\0`

%include "str_disk_error.asm"
