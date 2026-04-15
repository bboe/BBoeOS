        org 0600h

%include "constants.asm"

main:
        cld

        ;; Require argument of the form "<srcname> <destname>"
        mov si, [EXEC_ARG]
        test si, si
        jz .usage

        ;; Find the space separating srcname and destname
        mov di, si
        .find_space:
        mov al, [di]
        test al, al
        jz .usage
        cmp al, ' '
        je .found_space
        inc di
        jmp .find_space

        .found_space:
        mov byte [di], 0       ; Null-terminate srcname
        inc di                 ; DI = destname
        test byte [di], 0FFh
        jz .usage
        mov [dest_name], di

        ;; Open source file for reading
        mov al, O_RDONLY
        mov ah, SYS_IO_OPEN
        int 30h
        jc .not_found
        mov [src_fd], ax

        ;; Get source file's permission flags via fstat
        mov bx, ax
        mov ah, SYS_IO_FSTAT
        int 30h
        mov [src_mode], al

        ;; Open dest file for writing (create new, with source permissions)
        mov si, [dest_name]
        mov al, O_WRONLY + O_CREAT
        mov dl, [src_mode]
        mov ah, SYS_IO_OPEN
        int 30h
        jc .dest_err
        mov [dest_fd], ax

        ;; Copy loop: read from src into buffer, write buffer to dest
.copy_loop:
        mov bx, [src_fd]
        mov di, copy_buf
        mov cx, 512
        mov ah, SYS_IO_READ
        int 30h
        test ax, ax
        jz .copy_done           ; EOF
        cmp ax, -1
        je .disk_err

        ;; Write the bytes we just read
        mov cx, ax
        mov bx, [dest_fd]
        mov si, copy_buf
        mov ah, SYS_IO_WRITE
        int 30h
        cmp ax, -1
        je .disk_err
        jmp .copy_loop

.copy_done:
        mov bx, [dest_fd]
        mov ah, SYS_IO_CLOSE
        int 30h
        mov bx, [src_fd]
        mov ah, SYS_IO_CLOSE
        int 30h
        jmp FUNCTION_EXIT

.dest_err:
        ;; Close src before reporting error
        mov bx, [src_fd]
        mov ah, SYS_IO_CLOSE
        int 30h
        mov si, MESSAGE_EXISTS
        mov cx, MESSAGE_EXISTS_LENGTH
        jmp .die

.disk_err:
        mov bx, [dest_fd]
        mov ah, SYS_IO_CLOSE
        int 30h
        mov bx, [src_fd]
        mov ah, SYS_IO_CLOSE
        int 30h
        mov si, MESSAGE_DISK_ERROR
        mov cx, MESSAGE_DISK_ERROR_LENGTH
        jmp .die

.not_found:
        mov si, MESSAGE_NOT_FOUND
        mov cx, MESSAGE_NOT_FOUND_LENGTH
        jmp .die

.usage:
        mov si, MESSAGE_USAGE
        mov cx, MESSAGE_USAGE_LENGTH

.die:
        jmp FUNCTION_DIE

;; Variables
dest_fd   dw 0
dest_name dw 0
src_fd    dw 0
src_mode db 0

;; Strings
MESSAGE_DISK_ERROR  db `Disk error\n`
MESSAGE_DISK_ERROR_LENGTH equ $ - MESSAGE_DISK_ERROR
MESSAGE_EXISTS    db `File already exists\n`
MESSAGE_EXISTS_LENGTH equ $ - MESSAGE_EXISTS
MESSAGE_NOT_FOUND db `File not found\n`
MESSAGE_NOT_FOUND_LENGTH equ $ - MESSAGE_NOT_FOUND
MESSAGE_USAGE     db `Usage: cp <srcname> <destname>\n`
MESSAGE_USAGE_LENGTH equ $ - MESSAGE_USAGE


;; Copy buffer (512 bytes, right after code+data)
copy_buf:
